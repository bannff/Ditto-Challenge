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
    """A unit of work, authored by a stranger. Every field here is untrusted input.

    The length bounds are a control, not tidiness. `request` is scanned by a table of
    regexes in `refusal.py` before a budget is armed, so an unbounded request makes the
    gate's cost unbounded too — one backtracking row anywhere in that table would be a hang
    reachable from the CLI. Capping the contract means no future row can reintroduce that,
    and a ticket nobody could read is not a ticket.
    """

    id: str
    repository: str
    request: str = Field(max_length=20_000)
    domain: str = "general"
    acceptance_command: str | None = Field(default=None, max_length=2_000)
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


class LessonDraft(BaseModel):
    """What the Learn node is asked to return: the rule, and nothing wrapped around it.

    A schema rather than an instruction, because the instruction demonstrably does not hold.
    The critic's prompt already said "Output only the lesson, and stop", but across every
    recorded run the learn swarm terminated after a *single* agent — it never handed off — so
    the refiner and critic that were supposed to strip the framing never ran, and whichever
    agent went first became the whole node. When that was the drafter, whose own prompt tells
    it to recall existing lessons first, the stored "lesson" opened with "Good - I've recalled
    the existing lessons and loaded the lesson-writing skill..." and buried the rule 200 words
    down. Memory then retrieved that preamble on later runs.

    The field carries **no validation constraints**, deliberately, and that is a safety
    property rather than laziness. When a forced structured-output tool call fails
    validation, the SDK returns a tool *error* instead of raising
    (`StructuredOutputTool.stream`), the event loop recurses on `tool_use`, and forced mode
    stays latched with only the schema tool on offer — so the model is asked for the same
    value, fails the same constraint, and loops. `force_attempted` guards the refusal path,
    not the invalid-value path. Measured with `min_length`/`max_length` here: 312 model calls
    to a `RecursionError`, bounded only by the 300s node timeout, times `max_redos`. That
    breaks the bounded-runs requirement, and a length cap is exactly the constraint a chatty
    model violates repeatedly.

    So the shape is the schema's job and the policy is ours: length and usability are applied
    at the write boundary in `workflow._lesson_content`, where a bad value costs nothing.
    Anything added here must be unfailable — describe it in `description`, don't validate it.
    """

    rule: str = Field(
        description=(
            "One durable, generalizable rule for a future run, phrased as an instruction, "
            "under 600 characters. No preamble, no restatement of the ticket, no file paths, "
            "secrets or run ids."
        ),
    )


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


class SuiteCheck(BaseModel):
    """The full-suite regression gate: the target's whole test suite, run by the platform
    before the workflow (baseline, at the seed commit) and again after the change.

    The fixtures — and any real backlog — legitimately carry tests that are red *before*
    the change (a bug's repro test, a spec-only feature's acceptance test for some other
    ticket), so "exit 0" is the wrong bar for the whole suite. The enforceable bar is: the
    change introduces no NEW failing test. `passed` means exactly that."""

    command: str
    exit_code: int
    output_tail: str = ""
    baseline_failures: list[str] = Field(default_factory=list)
    new_failures: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def passed(self) -> bool:
        return not self.new_failures


class RunReport(BaseModel):
    schema_version: int = SCHEMA_VERSION
    run_id: str
    ticket: Ticket
    branch: str | None = None
    worktree: str | None = None
    outcome: Outcome = Outcome.INCONCLUSIVE
    # Plain-English reviewer summary: one deterministic headline from the platform, then the
    # Verify stage's evidence-checking prose when the workflow produced it. Additive — a
    # report without one is still valid.
    summary: str = ""
    verdicts: list[Verdict] = Field(default_factory=list)
    # The ticket's declared acceptance check. Necessary but not sufficient: it may be
    # narrowed to one test file, so it proves the ticket's own criterion only.
    acceptance: AcceptanceResult | None = None
    # The platform's full-suite regression gate. Resolution requires BOTH: the ticket's
    # check green, and the whole suite carrying no new failure relative to the seed.
    suite: SuiteCheck | None = None
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


class RecoveryDecision(BaseModel):
    """Whether a run's last checkpointed tree can be recovered, and what it actually is.

    `commit` comes from git's ref, never from the ledger — the chain corroborates it rather
    than choosing it. `node` and `outcome` exist so a reader can't mistake a recovered tree
    for verified, shippable work: these commits passed a node's eval checkpoint, and the run
    they came from may well have failed.
    """

    run_id: str
    allowed: bool
    reason: str
    commit: str | None = None
    node: str | None = None
    outcome: str | None = None
    chain: ChainStatus | None = None
    # Whether the ledger's own record of the last checkpoint agrees with git's ref.
    corroborated: bool = False


class TaxonomyTag(BaseModel):
    name: str
    invariants: list[str] = Field(default_factory=list)
    acceptance_hints: list[str] = Field(default_factory=list)


class Taxonomy(BaseModel):
    version: int = 1
    tags: dict[str, TaxonomyTag] = Field(default_factory=dict)

    def get(self, tag: str) -> TaxonomyTag | None:
        return self.tags.get(tag)
