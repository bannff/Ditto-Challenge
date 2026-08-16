"""Pydantic v2 contracts validated at every ingress/egress boundary."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, computed_field

SCHEMA_VERSION = 1

# prev_hash of a run's first block. Chains are per-run: a global head would serialize
# concurrent tickets, and the hard requirement is that they never clobber each other.
GENESIS_HASH = "0" * 64

# A run id becomes a path component (worktree dir, session dir, cassette file), and it can
# arrive from argv, so it is validated before it is ever joined to a path. Canonical here
# because more than one module needs the same guarantee.
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _now() -> datetime:
    return datetime.now(UTC)


class Outcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    INCONCLUSIVE = "inconclusive"
    REFUSED = "refused"


class NodeState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    REDO = "redo"
    FAILED = "failed"


class Ticket(BaseModel):
    id: str
    repository: str
    request: str
    domain: str = "general"
    acceptance_command: str | None = None
    created_at: datetime = Field(default_factory=_now)


class EvaluatorScore(BaseModel):
    evaluator: str
    score: float
    threshold: float
    passed: bool
    reason: str = ""
    gating: bool = True  # False = informational only; recorded but can't fail the node


class Verdict(BaseModel):
    schema_version: int = SCHEMA_VERSION
    node: str
    passed: bool
    attempts: int = 1
    scores: list[EvaluatorScore] = Field(default_factory=list)
    diagnosis: str | None = None


class Lesson(BaseModel):
    schema_version: int = SCHEMA_VERSION
    ticket_id: str
    outcome: Outcome
    content: str
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)


class AcceptanceResult(BaseModel):
    command: str
    exit_code: int
    output_tail: str = ""

    @computed_field
    @property
    def passed(self) -> bool:
        return self.exit_code == 0


class RunReport(BaseModel):
    schema_version: int = SCHEMA_VERSION
    run_id: str
    ticket: Ticket
    branch: str | None = None
    worktree: str | None = None
    outcome: Outcome = Outcome.INCONCLUSIVE
    verdicts: list[Verdict] = Field(default_factory=list)
    acceptance: AcceptanceResult | None = None
    evidence: str = ""
    lesson: Lesson | None = None
    created_at: datetime = Field(default_factory=_now)


class BlockType(StrEnum):
    """What a ledger block records. Add a kind only when a real event emits it."""

    RUN_START = "run_start"
    NODE_ATTEMPT = "node_attempt"
    TOOL_CALL = "tool_call"
    VERDICT = "verdict"
    BREAKER_TRIP = "breaker_trip"
    ACCEPTANCE_GATE = "acceptance_gate"
    MODEL_CALL = "model_call"
    LESSON_WRITE = "lesson_write"
    LESSON_REFUSED = "lesson_refused"
    RUN_END = "run_end"


class Block(BaseModel):
    """One append-only decision record, chained to its predecessor by hash.

    `content_hash` covers the payload *and* `prev_hash`, so altering any earlier block
    invalidates every block after it. Hashes and timestamps live in their own fields —
    never inside `payload`, which is scrubbed before hashing.
    """

    schema_version: int = SCHEMA_VERSION
    run_id: str
    seq: int
    block_type: BlockType
    payload: dict[str, Any] = Field(default_factory=dict)
    git_hash: str | None = None  # worktree HEAD when the block was written, if known
    prev_hash: str = GENESIS_HASH
    content_hash: str = ""
    created_at: datetime = Field(default_factory=_now)


class ChainStatus(BaseModel):
    run_id: str
    valid: bool
    length: int = 0
    broken_at: int | None = None
    reason: str | None = None


class ProvenanceDecision(BaseModel):
    """Whether a run's record is trustworthy enough to learn from."""

    allowed: bool
    reason: str
    chain: ChainStatus | None = None


class TaxonomyTag(BaseModel):
    name: str
    invariants: list[str] = Field(default_factory=list)
    acceptance_hints: list[str] = Field(default_factory=list)


class Taxonomy(BaseModel):
    version: int = 1
    tags: dict[str, TaxonomyTag] = Field(default_factory=dict)

    def get(self, tag: str) -> TaxonomyTag | None:
        return self.tags.get(tag)
