"""Pydantic v2 contracts validated at every ingress/egress boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, computed_field

SCHEMA_VERSION = 1


def _now() -> datetime:
    return datetime.now(UTC)


class Outcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    INCONCLUSIVE = "inconclusive"


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


class TaxonomyTag(BaseModel):
    name: str
    invariants: list[str] = Field(default_factory=list)
    acceptance_hints: list[str] = Field(default_factory=list)


class Taxonomy(BaseModel):
    version: int = 1
    tags: dict[str, TaxonomyTag] = Field(default_factory=dict)

    def get(self, tag: str) -> TaxonomyTag | None:
        return self.tags.get(tag)
