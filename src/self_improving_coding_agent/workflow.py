"""run_ticket — the orchestration that ties the plumbing together for one ticket.

The graded safety behaviors live here, in code, not in agent discretion: refusal is a
deterministic pre-check, the test-gate runs the declared acceptance command and blocks a
change that doesn't pass (reverting to a clean tree), and the lesson is persisted by code
for both outcomes even though the Learn agent distills its text.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from .cassette import Cassette, model_wrapper
from .contracts import (
    AcceptanceResult,
    BlockType,
    Lesson,
    LessonDraft,
    Outcome,
    ProvenanceDecision,
    RunReport,
    Ticket,
)
from .graph import WorkflowModels, WorkflowResult, default_models, run_workflow
from .kb import PolicyKB, make_query_policy_tool
from .ledger import Ledger
from .memory import LessonMemory, make_memory_tools
from .nodes import build_reference_nodes
from .recorder import RunRecorder
from .refusal import should_refuse
from .settings import get_settings
from .telemetry import setup_telemetry
from .tools import make_worktree_tools
from .worktree import Worktree, WorktreeError

# Whole-run wall-clock ceiling. Per-attempt Swarm timeouts bound each step; this bounds
# the entire multi-node run so a confused ticket can't burn unbounded wall-clock/cost.
RUN_DEADLINE_SECONDS = 1800.0

# What a stored rule has to be. Applied here rather than on the LessonDraft contract, where a
# constraint is a model-reachable loop; see LessonDraft. The cap matters because every stored
# rule is embedded and searched on every later run, so length is a recall cost, not cosmetics.
_MIN_RULE_CHARS = 16
_MAX_RULE_CHARS = 600


def _new_run_id() -> str:
    return "run-" + uuid.uuid4().hex[:12]


def _refusal_reason(
    provenance: ProvenanceDecision,
    intact: bool,
    degraded: bool,
    replaying: bool,
    has_rule: bool = True,
) -> str:
    """Why memory was not allowed to learn from this run. Most specific reason first.

    `has_rule` is checked last on purpose. A degraded or breaker-tripped run has no rule
    *because* it was cut short, so reporting the missing rule would name the symptom and hide
    the cause. It is the reason only when nothing else went wrong.
    """
    if replaying:
        return "a replayed run re-derives a recorded lesson; it is not a fresh observation"
    if not provenance.allowed:
        return provenance.reason
    if not intact:
        return "part of the run's record failed to write, so the record can't be trusted"
    if degraded:
        return "the run degraded, so its conclusions were never verified"
    if not has_rule:
        return "the Learn node produced no usable rule, so there is nothing to remember"
    return "refused"


def _lesson_content(wf: WorkflowResult) -> str | None:
    """The rule the Learn node produced, or None if it produced nothing usable.

    The node's `output_model` is what makes this a field rather than a guess. Persisting
    `final_output` instead stored the agent's whole closing turn, so a lesson read "Good -
    I've recalled the existing lessons..." and memory served that back on later runs.

    Returning None rather than a placeholder is the point: "no rule" has to be a *reason not
    to teach*, checked where that decision is made. Every path that loses the schema today
    also degrades the run, so a placeholder would be unreachable — but only by coincidence of
    two distant mechanisms, and a value that is stored correctly by accident is one refactor
    from being stored wrongly.

    The length policy lives here, not on the contract: a constraint on the schema field is
    reachable by the model and loops the forced tool call (see `LessonDraft`). Here an
    over-long rule costs one truncation.
    """
    draft = wf.final_structured
    if not isinstance(draft, LessonDraft):
        return None
    rule = " ".join(draft.rule.split())
    if len(rule) < _MIN_RULE_CHARS:  # a fragment is not a rule
        return None
    return rule[:_MAX_RULE_CHARS]


def run_ticket(
    ticket: Ticket,
    *,
    models: WorkflowModels | None = None,
    status_cb=None,
    kb: PolicyKB | None = None,
    memory: LessonMemory | None = None,
    ledger: Ledger | None = None,
    telemetry_console: bool = True,
    cassette: Cassette | None = None,
) -> RunReport:
    settings = get_settings()
    settings.ensure_dirs()
    run_id = _new_run_id()
    ledger = ledger or Ledger(settings.ledger_db)
    recorder = RunRecorder(ledger, run_id)
    # A replayed run drives its tools from recorded model output. Recorded output is not
    # evidence, so such a run is a verification harness: it never ships and never teaches.
    replaying = cassette is not None and cassette.mode == "replay"
    recorder.append(
        BlockType.RUN_START,
        {"ticket_id": ticket.id, "domain": ticket.domain, "replaying": replaying},
    )

    reason = should_refuse(ticket)
    if reason:
        # The refusal path gets a chain too: a declined ticket is a real outcome and has
        # to be as auditable as a resolved one.
        recorder.append(BlockType.RUN_END, {"outcome": str(Outcome.REFUSED), "reason": reason})
        report = RunReport(run_id=run_id, ticket=ticket, outcome=Outcome.REFUSED, evidence=reason)
        ledger.save(report)
        return report

    setup_telemetry(console=telemetry_console)
    models = models or default_models()
    if kb is None:
        kb = PolicyKB(settings.chroma_dir)
        kb.seed()
    memory = memory or LessonMemory()

    worktree = Worktree.create(ticket.repository, run_id, settings.worktrees_dir)
    Worktree.prune_checkpoints(Path(ticket.repository).resolve())
    keep_branch = False
    try:
        recorder.track_git(worktree.seed)
        primed = memory.retrieve(ticket.request)
        nodes = build_reference_nodes(
            worktree_tools=make_worktree_tools(worktree),
            policy_tool=make_query_policy_tool(kb),
            recall_tool=make_memory_tools(memory)[0],
            primed_lessons="\n".join(f"- {p}" for p in primed),
        )
        for node in nodes:  # tool calls reach the ledger; the engine stays ledger-unaware
            node.hooks = [recorder.for_node(node.name)]
            node.model_wrapper = model_wrapper(recorder.wrap_model, cassette)
        task = f"Ticket [{ticket.domain}] {ticket.id}: {ticket.request}"
        checkpoints: list[str] = []

        def checkpoint(node_name: str) -> str | None:
            """Commit the tree that just passed, and point the chain at it."""
            commit = worktree.checkpoint(node_name)
            if commit is not None:
                checkpoints.append(commit)
                recorder.track_git(commit)  # the VERDICT block references restorable state
            return commit

        def restore() -> None:
            """Before an informed retry, put the tree back to the last known-good state."""
            worktree.restore(checkpoints[-1] if checkpoints else worktree.seed)

        wf = run_workflow(
            nodes,
            task,
            models=models,
            status_cb=recorder.status_callback(status_cb),
            session_prefix=run_id,
            deadline_seconds=RUN_DEADLINE_SECONDS,
            checkpoint_cb=checkpoint,
            restore_cb=restore,
        )

        acceptance = None
        refusal: str | None = None
        if ticket.acceptance_command:
            try:
                r = worktree.run_acceptance(ticket.acceptance_command)
            except WorktreeError as e:
                # An unrunnable gate is a refusal, not a crash: nothing is verified, so
                # nothing ships, and the run still lands in the ledger with a reason.
                refusal = str(e)
            else:
                acceptance = AcceptanceResult(
                    command=ticket.acceptance_command,
                    exit_code=r.exit_code,
                    output_tail=r.output[-2000:],
                )
            recorder.append(
                BlockType.ACCEPTANCE_GATE,
                {
                    "command": ticket.acceptance_command,
                    "exit_code": acceptance.exit_code if acceptance else None,
                    "passed": acceptance.passed if acceptance else False,
                    "refused": refusal,
                },
            )

        # A change is only "resolved" if the target's tests actually ran and passed.
        # No acceptance command => nothing was verified => never shipped (INCONCLUSIVE).
        gate_passed = acceptance is not None and acceptance.passed
        success = wf.outcome == Outcome.SUCCESS and gate_passed and not wf.degraded
        if success:
            outcome = Outcome.SUCCESS
        elif refusal is not None:
            outcome = Outcome.REFUSED
        elif acceptance is None:
            outcome = Outcome.INCONCLUSIVE
        else:
            outcome = Outcome.FAILURE

        diff = worktree.diff()
        if success and not replaying:
            worktree.commit(f"autodev: resolve {ticket.id}")
            keep_branch = True
        else:
            worktree.revert()  # a change that isn't verified is never shipped
        recorder.track_git(worktree.head_hash())

        # No usable rule means no lesson object at all, rather than a placeholder one that
        # every gate below would have to remember to reject.
        rule = _lesson_content(wf)
        lesson = (
            Lesson(ticket_id=ticket.id, outcome=outcome, content=rule, tags=[ticket.domain])
            if rule
            else None
        )
        # Provenance gate: memory asks the ledger whether this run may teach it anything.
        # An honest failure still teaches — only a run whose record can't be trusted is
        # refused. Three independent signals, all required, because each covers the others'
        # blind spot: the chain verifies (nothing was altered or truncated), every block we
        # tried to write landed (an omitted block leaves a chain that still verifies), and
        # the workflow itself didn't degrade (first-hand, not laundered through the ledger).
        provenance = ledger.provenance(run_id)
        may_learn = (
            lesson is not None
            and provenance.allowed
            and recorder.intact
            and not wf.degraded
            and not replaying
        )
        if may_learn and lesson is not None:
            memory.store(lesson)
            recorder.append(
                BlockType.LESSON_WRITE, {"ticket_id": ticket.id, "outcome": str(outcome)}
            )
        else:
            recorder.append(
                BlockType.LESSON_REFUSED,
                {
                    "reason": _refusal_reason(
                        provenance,
                        recorder.intact,
                        wf.degraded,
                        replaying,
                        has_rule=lesson is not None,
                    ),
                    "chain_verified": provenance.allowed,
                    "record_complete": recorder.intact,
                    "workflow_degraded": wf.degraded,
                    "replaying": replaying,
                    "rule_produced": lesson is not None,
                },
            )

        report = RunReport(
            run_id=run_id,
            ticket=ticket,
            branch=worktree.branch if keep_branch else None,
            outcome=outcome,
            verdicts=wf.verdicts,
            acceptance=acceptance,
            evidence=refusal or diff,  # the whole diff; the ledger bounds its own row
            # Only set when memory actually took it: an empty lesson on the report means
            # this run taught nothing, which is the honest reading.
            lesson=lesson if may_learn else None,
        )
        recorder.append(
            BlockType.RUN_END,
            {"outcome": str(outcome), "branch": report.branch, "learned": may_learn},
        )
        ledger.save(report)
        return report
    finally:
        worktree.remove(keep_branch=keep_branch)
