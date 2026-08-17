from __future__ import annotations

import asyncio

from strands.models.model import Model
from strands.multiagent import Status
from strands_evals.detectors import ConfidenceLevel, detect_failures
from strands_evals.evaluators import Evaluator
from strands_evals.types import EvaluationData, EvaluationOutput
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


class BuiltEvaluator:
    """An evaluator already constructed with the shared model, plus its gate threshold."""

    def __init__(
        self,
        name: str,
        evaluator: Evaluator,
        threshold: float,
        gating: bool = True,
        assertion: str | None = None,
    ) -> None:
        self.name = name
        self.evaluator = evaluator
        self.threshold = threshold
        self.gating = gating
        self.assertion = assertion


def _aggregate(be: BuiltEvaluator, outputs: list[EvaluationOutput]) -> EvaluatorScore:
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


def _error_score(be: BuiltEvaluator, error: Exception) -> EvaluatorScore:
    return EvaluatorScore(
        evaluator=be.name,
        score=0.0,
        threshold=be.threshold,
        passed=True,
        reason=f"[non-gating evaluator error: {type(error).__name__}]",
        gating=False,
    )


async def _detector_score(session: Session, model: Model | str | None) -> EvaluatorScore:
    # detect_failures replaces the stock TrajectoryEvaluator for long traces: it
    # serializes the session once, and when that would blow the judge's context it
    # falls back to the SDK's token-aware chunking (split_spans_by_tokens) and merges
    # per-chunk findings. It's a sync API, so run it off the event loop.
    try:
        result = await asyncio.to_thread(
            detect_failures,
            session,
            model=model,
            confidence_threshold=ConfidenceLevel.MEDIUM,
        )
    except Exception as error:
        return EvaluatorScore(
            evaluator="trajectory_diagnostic",
            score=0.0,
            threshold=1.0,
            passed=True,
            reason=f"[non-gating detector error: {type(error).__name__}]",
            gating=False,
        )
    categories = sorted({category for failure in result.failures for category in failure.category})
    reason = "[no detected trajectory failures]" if not categories else (
        "[detected categories: " + ", ".join(categories[:8]) + "]"
    )
    return EvaluatorScore(
        evaluator="trajectory_diagnostic",
        score=1.0 if not categories else 0.0,
        threshold=1.0,
        passed=True,
        reason=reason[:500],
        gating=False,
    )


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

    if not any(evaluator.gating for evaluator in built_evaluators):
        return Verdict(
            node=node_name,
            passed=False,
            attempts=attempts,
            diagnosis="checkpoint configuration has no gating evaluator",
        )

    def _data(be: BuiltEvaluator) -> EvaluationData:
        return EvaluationData(
            input=request,
            actual_output=actual_output,
            actual_trajectory=session,
            name=node_name,
            expected_assertion=be.assertion,
        )

    results = await asyncio.gather(
        *(be.evaluator.evaluate_async(_data(be)) for be in built_evaluators),
        return_exceptions=True,
    )
    scores: list[EvaluatorScore] = []
    for evaluator, result in zip(built_evaluators, results, strict=True):
        if isinstance(result, BaseException):
            if not isinstance(result, Exception) or evaluator.gating:
                raise result
            scores.append(_error_score(evaluator, result))
        else:
            scores.append(_aggregate(evaluator, result))
    passed = all(score.passed for score in scores if score.gating)
    # Trajectory diagnostic on every Implement session, pass or fail. Informational
    # only: it never touches `passed`, and its errors degrade to a non-gating score.
    if node_name == "implement" and session is not None:
        scores.append(await _detector_score(session, diagnose_model))

    return Verdict(node=node_name, passed=passed, attempts=attempts, scores=scores)
