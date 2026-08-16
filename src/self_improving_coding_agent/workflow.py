"""run_ticket — the orchestration that ties the plumbing together for one ticket.

The graded safety behaviors live here, in code, not in agent discretion: refusal is a
deterministic pre-check, the test-gate runs the declared acceptance command and blocks a
change that doesn't pass (reverting to a clean tree), and the lesson is persisted by code
for both outcomes even though the Learn agent distills its text.
"""

from __future__ import annotations

import uuid

from .contracts import AcceptanceResult, BlockType, Lesson, Outcome, RunReport, Ticket
from .graph import WorkflowModels, default_models, run_workflow
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


def _new_run_id() -> str:
    return "run-" + uuid.uuid4().hex[:12]


def run_ticket(
    ticket: Ticket,
    *,
    models: WorkflowModels | None = None,
    status_cb=None,
    kb: PolicyKB | None = None,
    memory: LessonMemory | None = None,
    ledger: Ledger | None = None,
    telemetry_console: bool = True,
) -> RunReport:
    settings = get_settings()
    settings.ensure_dirs()
    run_id = _new_run_id()
    ledger = ledger or Ledger(settings.ledger_db)
    recorder = RunRecorder(ledger, run_id)
    recorder.append(
        BlockType.RUN_START, {"ticket_id": ticket.id, "domain": ticket.domain}
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
    keep_branch = False
    try:
        recorder.track_git(worktree.head_hash())
        primed = memory.retrieve(ticket.request)
        nodes = build_reference_nodes(
            worktree_tools=make_worktree_tools(worktree),
            policy_tool=make_query_policy_tool(kb),
            recall_tool=make_memory_tools(memory)[0],
            primed_lessons="\n".join(f"- {p}" for p in primed),
        )
        for node in nodes:  # tool calls reach the ledger; the engine stays ledger-unaware
            node.hooks = [recorder]
        task = f"Ticket [{ticket.domain}] {ticket.id}: {ticket.request}"
        wf = run_workflow(
            nodes,
            task,
            models=models,
            status_cb=recorder.status_callback(status_cb),
            session_prefix=run_id,
            deadline_seconds=RUN_DEADLINE_SECONDS,
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
        if success:
            worktree.commit(f"autodev: resolve {ticket.id}")
            keep_branch = True
        else:
            worktree.revert()  # a change that isn't verified is never shipped
        recorder.track_git(worktree.head_hash())

        lesson = Lesson(
            ticket_id=ticket.id,
            outcome=outcome,
            content=(wf.final_output.strip() or "no lesson produced"),
            tags=[ticket.domain],
        )
        # Provenance gate: memory asks the ledger whether this run may teach it anything.
        # An honest failure still teaches — only a run whose record can't be trusted is
        # refused. Three independent signals, all required, because each covers the others'
        # blind spot: the chain verifies (nothing was altered or truncated), every block we
        # tried to write landed (an omitted block leaves a chain that still verifies), and
        # the workflow itself didn't degrade (first-hand, not laundered through the ledger).
        provenance = ledger.provenance(run_id)
        may_learn = provenance.allowed and recorder.intact and not wf.degraded
        if may_learn:
            memory.store(lesson)
            recorder.append(
                BlockType.LESSON_WRITE, {"ticket_id": ticket.id, "outcome": str(outcome)}
            )
        else:
            recorder.append(
                BlockType.LESSON_REFUSED,
                {
                    "reason": provenance.reason if not provenance.allowed
                    else "the run degraded, so its conclusions were never verified",
                    "chain_verified": provenance.allowed,
                    "record_complete": recorder.intact,
                    "workflow_degraded": wf.degraded,
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
