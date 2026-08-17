"""Run bounded Strands Swarm stages in a graph with evaluator-gated retries."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from strands.models.model import Model
from strands.multiagent import GraphBuilder, Swarm
from strands.multiagent.base import MultiAgentBase, MultiAgentResult, Status
from strands.multiagent.graph import GraphState
from strands.session import FileSessionManager

from .agent_plane import build_node_agents
from .checkpoint import BuiltEvaluator, run_checkpoint
from .contracts import NodeState, Outcome, Verdict
from .fallback import DEGRADED_MESSAGE, build_fallback_model
from .node import NodeConfig
from .settings import build_model, build_reviewer_model, get_settings
from .telemetry import build_session, clear_spans

StatusCallback = Callable[[dict], None]


@dataclass
class WorkflowModels:
    builder: Model
    reviewer: Model
    third: Model  # the third family in the multi-family swarm
    evaluator: Model
    fallback: Model


def default_models() -> WorkflowModels:
    # Built once, up front — never inside a running node. This graph's nodes run
    # sequentially (each stage feeds the next), so the reason is not a live race: boto client
    # creation isn't thread-safe, and a model built inside a node would put that construction
    # on whatever thread the SDK happens to use. Building here keeps the guarantee independent
    # of the execution order, which is what makes it safe to parallelise later — see the
    # per-run exporter note in issues.md #14, which is the thing that would actually block it.
    return WorkflowModels(
        builder=build_model(),
        reviewer=build_reviewer_model(),
        # temperature=None: the default third model (claude-sonnet-5) rejects the param.
        # Streaming is fine for the Claude/Nova trio; a family that rejects tools in
        # streaming mode (Llama/Mistral) would need streaming=False here.
        third=build_model(get_settings().third_model_id, temperature=None),
        evaluator=build_model(temperature=0),
        fallback=build_fallback_model(),
    )


@dataclass
class WorkflowResult:
    verdicts: list[Verdict] = field(default_factory=list)
    outputs: dict[str, str] = field(default_factory=dict)
    final_output: str = ""
    # The last node's structured value, when its NodeConfig declared an `output_model`.
    # Deliberately untyped here: the engine carries whatever schema the node asked for and
    # never learns what it means, the same way it never learns what a worktree is.
    final_structured: BaseModel | None = None
    outcome: Outcome = Outcome.INCONCLUSIVE
    degraded: bool = False


def _emit(
    cb: StatusCallback | None, node: str, state: NodeState, score: float | None = None
) -> None:
    if cb is not None:
        cb({"node": node, "state": str(state), "eval_score": score, "timestamp": time.time()})


def _avg(verdict: Verdict) -> float | None:
    if not verdict.scores:
        return None
    return sum(s.score for s in verdict.scores) / len(verdict.scores)


def _terminal_agent_result(result):
    """Return the terminal agent result, preferring SDK node history over result order."""
    results = getattr(result, "results", {}) or {}
    history = getattr(result, "node_history", None) or []
    names = [getattr(n, "node_id", n) for n in reversed(history)]
    names += [n for n in reversed(list(results)) if n not in names]
    for name in names:
        node_result = results.get(name)
        if node_result is None:
            continue
        try:
            agent_results = node_result.get_agent_results()
        except Exception:
            agent_results = []
        if agent_results:
            return agent_results[-1]
    return None


def _extract_output(result) -> tuple[str, BaseModel | None]:
    """Return the final turn's text and structured output."""
    agent_result = _terminal_agent_result(result)
    if agent_result is None:
        return str(result), None
    return str(agent_result), getattr(agent_result, "structured_output", None)


def _build_evaluators(node: NodeConfig, model: Model) -> list[BuiltEvaluator]:
    # All concrete strands_evals evaluators take a `model` kwarg (verified); the abstract
    # base doesn't declare it, so the type checker can't see it here.
    built = []
    for spec in node.evaluators:
        evaluator = spec.evaluator_cls(model=model, **spec.params)  # type: ignore[call-arg]
        if spec.trace_extractor is not None:
            # The concrete evaluators accept a trace_extractor on the base class only and
            # don't forward it from their own __init__, so scope it here.
            evaluator._trace_extractor = spec.trace_extractor
        built.append(BuiltEvaluator(spec.name, evaluator, spec.threshold, spec.gating))
    return built


def _compose_task(
    base: str, outputs: dict[str, str], prior_verdicts: list[Verdict], is_final: bool
) -> str:
    parts = [base]
    if is_final:
        # The final node distills the whole run (the durable lesson comes from here), so
        # it sees every stage's output — not just its predecessor's — plus the verdicts.
        for name, output in outputs.items():
            parts.append(f"\n\nOutput from {name}:\n{output}")
        if prior_verdicts:
            rendered = "\n".join(
                f"- {v.node}: {'passed' if v.passed else 'failed'} in {v.attempts} attempt(s)"
                + (f"; diagnosis: {v.diagnosis}" if v.diagnosis else "")
                for v in prior_verdicts
            )
            parts.append(f"\n\nEval results from earlier stages:\n{rendered}")
            # Non-gating judges (tool choice, parameter accuracy, trajectory) didn't fail
            # any node, but they observed how the work was done. Surface them here so the
            # lesson can be drawn from process, not just outcome.
            findings = "\n".join(
                f"- {v.node}/{s.evaluator} scored {s.score:.2f}: {s.reason}"
                for v in prior_verdicts
                for s in v.scores
                if not s.gating
            )
            if findings:
                parts.append(
                    "\n\nInformational judges on how the work was carried out (these gated "
                    "nothing; use them to spot process problems worth a lesson):\n"
                    f"{findings}"
                )
    elif outputs:
        prior_output = next(reversed(outputs.values()))
        parts.append(f"\n\nPrevious stage output:\n{prior_output}")
    return "".join(parts)


async def _run_swarm(agents, node: NodeConfig, task: str, session_manager=None):
    swarm = Swarm(
        agents,
        entry_point=agents[0],
        max_handoffs=node.max_handoffs,
        max_iterations=node.max_iterations,
        execution_timeout=node.execution_timeout,
        node_timeout=node.node_timeout,
        repetitive_handoff_detection_window=node.repetitive_handoff_detection_window,
        repetitive_handoff_min_unique_agents=node.repetitive_handoff_min_unique_agents,
        session_manager=session_manager,
    )
    result = await swarm.invoke_async(task)
    return _extract_output(result), result


@dataclass
class _RunContext:
    """Run-scoped state shared by the gate nodes: model roles, status stream, session
    naming, the outputs each stage feeds forward, and the wall-clock deadline."""

    models: WorkflowModels
    status_cb: StatusCallback | None
    prefix: str
    sessions_dir: Path
    base_task: str
    deadline_seconds: float | None
    # Optional workspace checkpointing, supplied by the caller so the engine never learns
    # what a worktree is. checkpoint(node) -> commit hash after a node passes; restore()
    # puts the tree back before an informed retry, so attempt N+1 starts from a known state
    # instead of inheriting attempt N's half-applied edits.
    checkpoint_cb: Callable[[str], str | None] | None = None
    restore_cb: Callable[[], None] | None = None
    start: float = field(default_factory=time.monotonic)
    outputs: dict[str, str] = field(default_factory=dict)
    verdicts: list[Verdict] = field(default_factory=list)

    @property
    def model_map(self) -> dict[str, Model]:
        return {
            "builder": self.models.builder,
            "reviewer": self.models.reviewer,
            "third": self.models.third,
        }

    def deadline_exceeded(self) -> bool:
        return (
            self.deadline_seconds is not None
            and time.monotonic() - self.start > self.deadline_seconds
        )


class _GateNode(MultiAgentBase):
    """One workflow stage as a graph node: a single swarm attempt plus its checkpoint.

    The informed-retry loop is the graph's self-loop edge, not a loop in here — each
    execution is one attempt, and the node keeps attempt count, verdict, and diagnosis
    across revisits (reset_on_revisit resets executor state, not these attributes)."""

    def __init__(self, config: NodeConfig, ctx: _RunContext, is_final: bool) -> None:
        super().__init__()
        self.id = f"gate-{config.name}"
        self.config = config
        self.ctx = ctx
        self.is_final = is_final
        self.evaluators = _build_evaluators(config, ctx.models.evaluator)
        self.attempts = 0
        self.verdict: Verdict | None = None
        self.output = ""
        self.structured: BaseModel | None = None
        self.degraded = False

    def _attempt_task(self) -> str:
        task = _compose_task(
            self.ctx.base_task, self.ctx.outputs, self.ctx.verdicts, self.is_final
        )
        if self.verdict is not None and self.verdict.diagnosis:
            task += f"\n\nThe last attempt failed. Fix this and try again: {self.verdict.diagnosis}"
        return task

    async def invoke_async(
        self, task: Any, invocation_state: dict[str, Any] | None = None, **kwargs: Any
    ) -> MultiAgentResult:
        node = self.config
        self.attempts += 1
        _emit(
            self.ctx.status_cb,
            node.name,
            NodeState.RUNNING if self.attempts == 1 else NodeState.REDO,
        )
        clear_spans()  # scope the checkpoint Session to this attempt
        attempt_task = self._attempt_task()
        agents = build_node_agents(node, self.ctx.model_map)  # fresh agents per attempt
        session_manager = FileSessionManager(
            session_id=f"{self.ctx.prefix}-{node.name}-{self.attempts}",
            storage_dir=str(self.ctx.sessions_dir),
        )
        (self.output, self.structured), swarm_result = await _run_swarm(
            agents, node, attempt_task, session_manager
        )
        session = build_session(f"{self.ctx.prefix}-{node.name}-{self.attempts}")
        self.verdict = await run_checkpoint(
            node.name,
            self.evaluators,
            request=attempt_task,
            actual_output=self.output,
            session=session,
            diagnose_model=self.ctx.models.evaluator,
            attempts=self.attempts,
            swarm_status=swarm_result.status,
        )

        if self.verdict.passed:
            self.ctx.outputs[node.name] = self.output
            self.ctx.verdicts.append(self.verdict)
            # Checkpoint the tree that earned this pass, so a later failure can roll back to
            # it rather than to the start of the run.
            if self.ctx.checkpoint_cb is not None:
                self.ctx.checkpoint_cb(node.name)
            _emit(self.ctx.status_cb, node.name, NodeState.COMPLETE, _avg(self.verdict))
        elif self.attempts > node.max_redos:
            # Retries spent — circuit breaker: degrade with a fixed message, no extra
            # model calls. No outgoing edge fires after this, so the graph stops here.
            self.degraded = True
            self.output = DEGRADED_MESSAGE
            self.structured = None  # a degraded node produced no result to hand on
            self.ctx.outputs[node.name] = DEGRADED_MESSAGE
            self.ctx.verdicts.append(self.verdict)
            _emit(self.ctx.status_cb, node.name, NodeState.FAILED, _avg(self.verdict))
        elif self.ctx.restore_cb is not None:
            # This attempt failed and another is coming. Put the tree back to the last
            # checkpoint first: without this, the informed retry starts on top of whatever
            # the failed attempt left behind, and diagnoses a tree nobody intended.
            self.ctx.restore_cb()

        # The graph node itself always completes; pass/fail routing happens on the edges,
        # which read the structured verdict — a checkpoint failure is not a node crash.
        # The swarm's real usage/metrics carry through so GraphResult totals stay honest.
        return MultiAgentResult(
            status=Status.COMPLETED,
            results={},
            accumulated_usage=swarm_result.accumulated_usage,
            accumulated_metrics=swarm_result.accumulated_metrics,
        )


def _retry_condition(gate: _GateNode) -> Callable[[GraphState], bool]:
    def should_retry(state: GraphState) -> bool:
        return (
            not gate.ctx.deadline_exceeded()
            and gate.verdict is not None
            and not gate.verdict.passed
            and gate.attempts <= gate.config.max_redos
        )

    return should_retry


def _advance_condition(gate: _GateNode) -> Callable[[GraphState], bool]:
    def should_advance(state: GraphState) -> bool:
        # Deadline is checked between nodes so the run degrades cleanly, never mid-change.
        return (
            not gate.ctx.deadline_exceeded()
            and gate.verdict is not None
            and gate.verdict.passed
        )

    return should_advance


def _build_graph(gates: list[_GateNode], deadline_seconds: float | None):
    builder = GraphBuilder()
    for gate in gates:
        builder.add_node(gate, gate.config.name)
    builder.set_entry_point(gates[0].config.name)
    for i, gate in enumerate(gates):
        builder.add_edge(gate.config.name, gate.config.name, condition=_retry_condition(gate))
        if i + 1 < len(gates):
            builder.add_edge(
                gate.config.name, gates[i + 1].config.name, condition=_advance_condition(gate)
            )
    # Revisit = redo: reset the SDK's executor state each time, keep our attempt state.
    builder.reset_on_revisit(True)
    # Backstops only — the edge conditions are the real bounds (they stop between nodes,
    # never mid-change; these catch anything that slips past them).
    builder.set_max_node_executions(sum(g.config.max_redos + 1 for g in gates))
    if deadline_seconds is not None:
        builder.set_execution_timeout(deadline_seconds)
    return builder.build()


def _assemble(gates: list[_GateNode], ctx: _RunContext) -> WorkflowResult:
    result = WorkflowResult()
    ran = [g for g in gates if g.attempts > 0]
    result.verdicts = list(ctx.verdicts)
    result.outputs = dict(ctx.outputs)

    # A gate whose last verdict failed, yet never spent its retries, was abandoned between
    # attempts — the deadline ran out mid-ladder. The gate itself never saw that, so it
    # recorded neither the failed verdict nor a FAILED state. Without this, such a run
    # reports SUCCESS on an unverified final attempt.
    recorded = {id(v) for v in ctx.verdicts}
    abandoned = [
        g for g in ran if not g.degraded and g.verdict is not None and not g.verdict.passed
    ]
    for gate in abandoned:
        verdict = gate.verdict
        if verdict is not None and id(verdict) not in recorded:
            result.verdicts.append(verdict)
        _emit(ctx.status_cb, gate.config.name, NodeState.FAILED)

    result.degraded = bool(abandoned) or any(g.degraded for g in ran)
    if not result.degraded and len(ran) < len(gates):
        # No node tripped its breaker but the graph stopped early: the deadline ran out
        # between nodes. Mark the first node that never got to run.
        first_unrun = gates[len(ran)]
        _emit(ctx.status_cb, first_unrun.config.name, NodeState.FAILED)
        result.degraded = True
    result.final_output = ran[-1].output if ran else ""
    result.final_structured = ran[-1].structured if ran else None
    failed = result.degraded or any(not v.passed for v in result.verdicts)
    result.outcome = Outcome.FAILURE if failed else Outcome.SUCCESS
    return result


async def _run_workflow(nodes: list[NodeConfig], ctx: _RunContext) -> WorkflowResult:
    gates = [
        _GateNode(node, ctx, is_final=i == len(nodes) - 1) for i, node in enumerate(nodes)
    ]
    if ctx.deadline_exceeded():
        _emit(ctx.status_cb, gates[0].config.name, NodeState.FAILED)
        return WorkflowResult(degraded=True, outcome=Outcome.FAILURE)
    graph = _build_graph(gates, ctx.deadline_seconds)
    await graph.invoke_async(ctx.base_task)
    return _assemble(gates, ctx)


def run_workflow(
    nodes: list[NodeConfig],
    task: str,
    *,
    models: WorkflowModels | None = None,
    status_cb: StatusCallback | None = None,
    session_prefix: str = "run",
    sessions_dir: Path | str | None = None,
    deadline_seconds: float | None = None,
    checkpoint_cb: Callable[[str], str | None] | None = None,
    restore_cb: Callable[[], None] | None = None,
) -> WorkflowResult:
    resolved = models or default_models()
    storage = Path(sessions_dir) if sessions_dir else get_settings().sessions_dir
    storage.mkdir(parents=True, exist_ok=True)
    ctx = _RunContext(
        models=resolved,
        status_cb=status_cb,
        prefix=session_prefix,
        sessions_dir=storage,
        base_task=task,
        deadline_seconds=deadline_seconds,
        checkpoint_cb=checkpoint_cb,
        restore_cb=restore_cb,
    )
    return asyncio.run(_run_workflow(nodes, ctx))
