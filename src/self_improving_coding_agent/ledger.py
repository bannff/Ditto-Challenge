"""Durable SQLite run ledger: structured run history plus a per-run hash chain.

Two tables, one responsibility — the durable record of what a run did. `runs` holds the
final RunReport; `blocks` is an append-only, hash-chained sequence of the decisions that
produced it. The chain makes the record *tamper-evident*: any later edit to a stored block
breaks `verify_chain`, and `provenance` turns that into an operational answer — whether a
run is trustworthy enough for its lesson to enter memory.

Honest scope: this detects modification, deletion, and truncation of a *stored* record —
hashes chain, and a recorded head pins the length. It does not sign blocks (there is no key
material here) and it trusts the writer, so evidence forged at the source is chain-valid,
and a block that was never written leaves no gap (the recorder counts its own dropped
writes for exactly that reason). Rollback state is git's job — a block carries the git hash
rather than a copy of the tree.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .contracts import (
    GENESIS_HASH,
    Block,
    BlockType,
    ChainStatus,
    ProvenanceDecision,
    RunReport,
)
from .scrub import scrub_text

# A run's diff is unbounded (a wide feature touches many files), but a history row isn't a
# blob store. Only the stored copy is capped, and it says so — callers and artifact writers
# get the whole diff. Truncating silently would hand a reviewer a patch that looks complete.
MAX_EVIDENCE_CHARS = 20_000
_TRUNCATED = "\n\n[diff truncated at {limit} chars for run history — full diff on branch {b}]"

# A block is a record, not a blob store. Bounded runs means bounded records too.
MAX_BLOCK_FIELD_CHARS = 2_000

# Block kinds that mean the run's own machinery stopped it short, so the record of *why*
# it reached its conclusion is incomplete. Data, not branches — add a kind to the set.
UNTRUSTWORTHY_BLOCKS = frozenset({BlockType.BREAKER_TRIP})


def _bound(text: str) -> str:
    if len(text) <= MAX_BLOCK_FIELD_CHARS:
        return text
    return text[:MAX_BLOCK_FIELD_CHARS] + f"…[truncated at {MAX_BLOCK_FIELD_CHARS} chars]"


def _defang(text: str) -> str:
    """Escape control characters, newlines included.

    The chain is an audit surface: `autodev replay` prints these payloads, and a value
    carrying CR or an ANSI cursor-move could scroll back and overwrite the VERIFIED/BROKEN
    verdict a human is reading. Neutralised here, before hashing, so every consumer of a
    block gets the safe form rather than each one having to remember.
    """
    return "".join(ch if ch.isprintable() else repr(ch)[1:-1] for ch in text)


def _clean(value: Any) -> Any:
    """Scrub, defang, and bound every string in a payload, at any depth.

    Order matters: scrub runs on the original text so multiline shapes (a PEM private key)
    still match, then control characters are escaped, then the result is capped — capping
    last means a redaction can never be split so that half a secret survives.
    """
    if isinstance(value, str):
        return _bound(_defang(scrub_text(value)))
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return _bound(_defang(scrub_text(str(value))))


def block_hash(
    *,
    run_id: str,
    seq: int,
    block_type: BlockType,
    payload: dict[str, Any],
    git_hash: str | None,
    created_at: datetime,
    prev_hash: str,
) -> str:
    """sha256 over the block's canonical body, including prev_hash — that link is what
    makes the chain tamper-evident rather than a list of independent hashes."""
    body = json.dumps(
        {
            "run_id": run_id,
            "seq": seq,
            "block_type": str(block_type),
            "payload": payload,
            "git_hash": git_hash,
            "created_at": created_at.isoformat(),
            "prev_hash": prev_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _bound_evidence(evidence: str, branch: str | None) -> str:
    if len(evidence) <= MAX_EVIDENCE_CHARS:
        return evidence
    where = branch or "(none — the run did not commit)"
    return evidence[:MAX_EVIDENCE_CHARS] + _TRUNCATED.format(limit=MAX_EVIDENCE_CHARS, b=where)


def _scrub_report(report: RunReport) -> RunReport:
    data = report.model_dump(mode="json")
    data["evidence"] = _bound_evidence(scrub_text(data.get("evidence", "")), data.get("branch"))
    # ticket.request is untrusted stranger text; diagnosis/reason are model-generated from
    # the node output and can echo repo secrets. Scrub every free-text field, not just three.
    data["ticket"]["request"] = scrub_text(data["ticket"].get("request", ""))
    if data.get("acceptance"):
        data["acceptance"]["output_tail"] = scrub_text(data["acceptance"].get("output_tail", ""))
    if data.get("lesson"):
        data["lesson"]["content"] = scrub_text(data["lesson"]["content"])
    for verdict in data.get("verdicts", []):
        if verdict.get("diagnosis"):
            verdict["diagnosis"] = scrub_text(verdict["diagnosis"])
        for score in verdict.get("scores", []):
            score["reason"] = scrub_text(score.get("reason", ""))
    return RunReport.model_validate(data)


class Ledger:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Appends must serialize: every block needs the current head's hash. In-process
        # that's this lock; across processes it's BEGIN IMMEDIATE on the write below.
        self._append_lock = threading.Lock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init(self) -> None:
        with self._connect() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    ticket_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    report_json TEXT NOT NULL
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS blocks (
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    block_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    git_hash TEXT,
                    prev_hash TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    PRIMARY KEY (run_id, seq)
                )
                """
            )
            # The head is what makes truncation detectable. Without a recorded length and
            # tip hash, lopping off the last blocks leaves a shorter chain that verifies
            # perfectly — and the tail is exactly where a breaker trip and the outcome live.
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS chain_heads (
                    run_id TEXT PRIMARY KEY,
                    length INTEGER NOT NULL,
                    head_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def save(self, report: RunReport) -> RunReport:
        scrubbed = _scrub_report(report)
        with self._connect() as c:
            c.execute(
                "INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?, ?)",
                (
                    scrubbed.run_id,
                    scrubbed.ticket.id,
                    str(scrubbed.outcome),
                    scrubbed.schema_version,
                    scrubbed.created_at.isoformat(),
                    scrubbed.model_dump_json(),
                ),
            )
        return scrubbed

    def get(self, run_id: str) -> RunReport | None:
        with self._connect() as c:
            row = c.execute(
                "SELECT report_json FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return self._load(row[0]) if row else None

    def recent(self, limit: int = 10) -> list[RunReport]:
        with self._connect() as c:
            rows = c.execute(
                "SELECT report_json FROM runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._load(j) for (j,) in rows]

    def _load(self, report_json: str) -> RunReport:
        return RunReport.model_validate(json.loads(report_json))

    # ---- hash chain -------------------------------------------------------------

    def append_block(
        self,
        run_id: str,
        block_type: BlockType,
        payload: dict[str, Any] | None = None,
        *,
        git_hash: str | None = None,
    ) -> Block:
        """Append one block to this run's chain and return it, hash included.

        The payload is scrubbed and bounded *before* hashing, so what is hashed is exactly
        what is stored — otherwise every verification would fail on its own redactions.
        """
        clean = _clean(payload or {})
        with self._append_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT seq, content_hash FROM blocks WHERE run_id = ? "
                    "ORDER BY seq DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
                seq = row[0] + 1 if row else 0
                prev_hash = row[1] if row else GENESIS_HASH
                block = Block(
                    run_id=run_id,
                    seq=seq,
                    block_type=block_type,
                    payload=clean,
                    git_hash=git_hash,
                    prev_hash=prev_hash,
                )
                block.content_hash = block_hash(
                    run_id=block.run_id,
                    seq=block.seq,
                    block_type=block.block_type,
                    payload=block.payload,
                    git_hash=block.git_hash,
                    created_at=block.created_at,
                    prev_hash=block.prev_hash,
                )
                conn.execute(
                    "INSERT INTO blocks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        block.run_id,
                        block.seq,
                        str(block.block_type),
                        json.dumps(block.payload),
                        block.git_hash,
                        block.prev_hash,
                        block.content_hash,
                        block.created_at.isoformat(),
                        block.schema_version,
                    ),
                )
                # Same transaction as the block: the head can never lag or run ahead.
                conn.execute(
                    "INSERT INTO chain_heads VALUES (?, ?, ?, ?) ON CONFLICT(run_id) "
                    "DO UPDATE SET length = ?, head_hash = ?, updated_at = ?",
                    (
                        run_id,
                        seq + 1,
                        block.content_hash,
                        block.created_at.isoformat(),
                        seq + 1,
                        block.content_hash,
                        block.created_at.isoformat(),
                    ),
                )
                conn.execute("COMMIT")
            finally:
                conn.close()
        return block

    def blocks(self, run_id: str) -> list[Block]:
        with self._connect() as c:
            rows = c.execute(
                "SELECT run_id, seq, block_type, payload_json, git_hash, prev_hash, "
                "content_hash, created_at, schema_version FROM blocks "
                "WHERE run_id = ? ORDER BY seq",
                (run_id,),
            ).fetchall()
        return [
            Block(
                run_id=r[0],
                seq=r[1],
                block_type=BlockType(r[2]),
                payload=json.loads(r[3]),
                git_hash=r[4],
                prev_hash=r[5],
                content_hash=r[6],
                created_at=datetime.fromisoformat(r[7]),
                schema_version=r[8],
            )
            for r in rows
        ]

    def head(self, run_id: str) -> tuple[int, str] | None:
        with self._connect() as c:
            row = c.execute(
                "SELECT length, head_hash FROM chain_heads WHERE run_id = ?", (run_id,)
            ).fetchone()
        return (row[0], row[1]) if row else None

    def verify_chain(self, run_id: str) -> ChainStatus:
        """Recompute every hash and every link. Offline, deterministic, no network."""
        chain = self.blocks(run_id)
        if not chain:
            return ChainStatus(
                run_id=run_id, valid=False, reason="no blocks recorded for this run"
            )
        prev_hash = GENESIS_HASH
        for expected_seq, block in enumerate(chain):
            if block.seq != expected_seq:
                return ChainStatus(
                    run_id=run_id,
                    valid=False,
                    length=len(chain),
                    broken_at=block.seq,
                    reason=f"sequence gap: expected block {expected_seq}, found {block.seq}",
                )
            if block.prev_hash != prev_hash:
                return ChainStatus(
                    run_id=run_id,
                    valid=False,
                    length=len(chain),
                    broken_at=block.seq,
                    reason="broken link: prev_hash does not match the preceding block",
                )
            recomputed = block_hash(
                run_id=block.run_id,
                seq=block.seq,
                block_type=block.block_type,
                payload=block.payload,
                git_hash=block.git_hash,
                created_at=block.created_at,
                prev_hash=block.prev_hash,
            )
            if recomputed != block.content_hash:
                return ChainStatus(
                    run_id=run_id,
                    valid=False,
                    length=len(chain),
                    broken_at=block.seq,
                    reason="content hash mismatch: this block was altered after it was written",
                )
            prev_hash = block.content_hash

        # Links are intact — but a chain can be intact and still be a prefix of the real
        # one. Compare against the recorded head to catch blocks removed from the end.
        recorded = self.head(run_id)
        if recorded is None:
            return ChainStatus(
                run_id=run_id,
                valid=False,
                length=len(chain),
                reason="no recorded head for this run, so its length cannot be trusted",
            )
        expected_length, expected_head = recorded
        if len(chain) != expected_length or chain[-1].content_hash != expected_head:
            return ChainStatus(
                run_id=run_id,
                valid=False,
                length=len(chain),
                broken_at=chain[-1].seq,
                reason=(
                    f"chain is truncated: {len(chain)} blocks present, "
                    f"{expected_length} were written"
                ),
            )
        return ChainStatus(run_id=run_id, valid=True, length=len(chain))

    def provenance(self, run_id: str) -> ProvenanceDecision:
        """Is this run's record trustworthy enough to learn from?

        Two independent reasons to say no: the chain doesn't verify (the record was altered
        or is incomplete), or the run's own machinery cut it short, so its conclusions were
        never reached. A run that *failed honestly* passes — failure lessons are the
        valuable ones; only unverifiable runs are refused.
        """
        status = self.verify_chain(run_id)
        if not status.valid:
            return ProvenanceDecision(
                allowed=False,
                reason=f"chain integrity failed: {status.reason}",
                chain=status,
            )
        tripped = [b for b in self.blocks(run_id) if b.block_type in UNTRUSTWORTHY_BLOCKS]
        if tripped:
            where = ", ".join(str(b.payload.get("node", "?")) for b in tripped)
            return ProvenanceDecision(
                allowed=False,
                reason=(
                    f"run was cut short by its circuit breaker at [{where}]; its conclusions "
                    "were never reached, so there is nothing verified to learn"
                ),
                chain=status,
            )
        return ProvenanceDecision(
            allowed=True,
            reason=f"chain verified across {status.length} blocks; no breaker trip",
            chain=status,
        )
