"""Durable SQLite run ledger. Not a vector store — just structured run history."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .contracts import RunReport
from .scrub import scrub_text

# A run's diff is unbounded (a wide feature touches many files), but a history row isn't a
# blob store. Only the stored copy is capped, and it says so — callers and artifact writers
# get the whole diff. Truncating silently would hand a reviewer a patch that looks complete.
MAX_EVIDENCE_CHARS = 20_000
_TRUNCATED = "\n\n[diff truncated at {limit} chars for run history — full diff on branch {b}]"


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
        self._init()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

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
