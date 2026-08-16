"""Eval checkpoint: score a finished node, decide pass/fail, diagnose on failure.

This is the gate that drives the self-heal loop. It also owns the boundary between SDK
types and our contracts (Status -> Outcome/NodeState, EvaluationOutput -> EvaluatorScore)
so nothing else has to know the SDK's shapes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from strands.models.model import Model
from strands.multiagent.base import Status
from strands_evals.detectors import ConfidenceLevel, diagnose_session
from strands_evals.evaluators import Evaluator
from strands_evals.types.detector import DiagnosisResult
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput
from strands_evals.types.trace import Session

from .contracts import EvaluatorScore, NodeState, Verdict

_STATE_BY_STATUS = {
    Status.PENDING: NodeState.PENDING,
    Status.EXECUTING: NodeState.RUNNING,
    Status.COMPLETED: NodeState.COMPLETE,
    Status.FAILED: NodeState.FAILED,
    Status.INTERRUPTED: NodeState.FAILED,
}


def to_node_state(status: Status) -> NodeState:
    return _STATE_BY_STATUS.get(status, NodeState.PENDING)


@dataclass
class BuiltEvaluator:
    """An evaluator already constructed with the shared model, plus its gate threshold."""

    name: str
    evaluator: Evaluator
    threshold: float
    gating: bool = True


def _aggregate(be: BuiltEvaluator, outputs: list[EvaluationOutput]) -> EvaluatorScore:
    """Fold an evaluator's per-item outputs into one score using the SDK's own aggregator,
    which yields the mean score plus the evaluator's all-pass verdict. Non-gating
    evaluators are recorded (with their verdict) but never fail the node."""
    score, all_pass, reason = be.evaluator.aggregator(outputs)
    verdict = "all-pass" if all_pass else "some items failed"
    return EvaluatorScore(
        evaluator=be.name,
        score=score,
        threshold=be.threshold,
        passed=score >= be.threshold if be.gating else True,
        reason=f"[{verdict}] {reason}"[:500],
        gating=be.gating,
    )


def _format_diagnosis(result: DiagnosisResult) -> str | None:
    """Render the detector's full picture — which span failed, why, and the fix — not just
    the fix recommendation, so a failed checkpoint is legible to the redo and the report."""
    rca = {r.failure_span_id: r for r in result.root_causes}
    lines = []
    for failure in result.failures:
        parts = [f"[{failure.span_id}] {', '.join(failure.category) or 'failure'}"]
        if failure.evidence:
            parts.append(f"evidence: {failure.evidence[0]}")
        cause = rca.get(failure.span_id)
        if cause is not None:
            parts.append(
                f"root cause ({cause.causality}/{cause.completion_status}): "
                f"{cause.root_cause_explanation}"
            )
            parts.append(f"fix ({cause.fix_type}): {cause.fix_recommendation}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)[:1500] or None


async def run_checkpoint(
    node_name: str,
    built_evaluators: list[BuiltEvaluator],
    *,
    request: str,
    actual_output: str,
    session: Session | None = None,
    diagnose_model: Model | str | None = None,
    attempts: int = 1,
    swarm_status: Status | None = None,
) -> Verdict:
    # Circuit-breaker signal comes first: a swarm that hit its bounds (max_iterations /
    # timeout / handoffs) returns non-COMPLETED with partial text. That never passes,
    # regardless of what the judges think of the partial output.
    if swarm_status is not None and swarm_status != Status.COMPLETED:
        return Verdict(
            node=node_name,
            passed=False,
            attempts=attempts,
            diagnosis=(
                f"swarm did not complete: terminal status {to_node_state(swarm_status)} — "
                "it hit its bounds before finishing; no partial output is accepted."
            ),
        )

    data = EvaluationData(
        input=request,
        actual_output=actual_output,
        actual_trajectory=session,
        name=node_name,
    )
    # evaluate_async avoids the per-evaluator worker-thread + throwaway-event-loop the
    # sync .evaluate spins up from inside this already-async driver.
    outputs = await asyncio.gather(*(be.evaluator.evaluate_async(data) for be in built_evaluators))
    scores = [
        _aggregate(be, out) for be, out in zip(built_evaluators, outputs, strict=True)
    ]
    passed = all(s.passed for s in scores if s.gating)

    diagnosis = None
    if not passed and session is not None:
        # ON_FAILURE: only diagnose a failed checkpoint. Best-effort — a detector error
        # must not sink the run.
        try:
            result = diagnose_session(
                session, model=diagnose_model, confidence_threshold=ConfidenceLevel.MEDIUM
            )
            diagnosis = _format_diagnosis(result)
        except Exception:
            diagnosis = None

    return Verdict(
        node=node_name, passed=passed, attempts=attempts, scores=scores, diagnosis=diagnosis
    )
