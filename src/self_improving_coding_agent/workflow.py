"""run_ticket — the orchestration that ties the plumbing together for one ticket.

The graded safety behaviors live here, in code, not in agent discretion: refusal is a
deterministic pre-check, the test-gate runs the declared acceptance command and blocks a
change that doesn't pass (reverting to a clean tree), and the lesson is persisted by code
for both outcomes even though the Learn agent distills its text.
"""

from __future__ import annotations

import uuid

from .contracts import AcceptanceResult, Lesson, Outcome, RunReport, Ticket
from .graph import WorkflowModels, default_models, run_workflow
from .kb import PolicyKB, make_query_policy_tool
from .ledger import Ledger
from .memory import LessonMemory, make_memory_tools
from .nodes import build_reference_nodes
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

    reason = should_refuse(ticket)
    if reason:
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
        primed = memory.retrieve(ticket.request)
        nodes = build_reference_nodes(
            worktree_tools=make_worktree_tools(worktree),
            policy_tool=make_query_policy_tool(kb),
            recall_tool=make_memory_tools(memory)[0],
            primed_lessons="\n".join(f"- {p}" for p in primed),
        )
        task = f"Ticket [{ticket.domain}] {ticket.id}: {ticket.request}"
        wf = run_workflow(
            nodes,
            task,
            models=models,
            status_cb=status_cb,
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

        lesson = Lesson(
            ticket_id=ticket.id,
            outcome=outcome,
            content=(wf.final_output.strip() or "no lesson produced"),
            tags=[ticket.domain],
        )
        memory.store(lesson)  # stored for both outcomes

        report = RunReport(
            run_id=run_id,
            ticket=ticket,
            branch=worktree.branch if keep_branch else None,
            outcome=outcome,
            verdicts=wf.verdicts,
            acceptance=acceptance,
            evidence=refusal or diff,  # the whole diff; the ledger bounds its own row
            lesson=lesson,
        )
        ledger.save(report)
        return report
    finally:
        worktree.remove(keep_branch=keep_branch)
