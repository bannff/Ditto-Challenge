import asyncio
from typing import cast
from unittest.mock import patch

import pytest
from strands.multiagent.base import Status
from strands_evals.evaluators import Evaluator
from strands_evals.types.detector import DiagnosisResult, FailureItem
from strands_evals.types.evaluation import EvaluationOutput
from strands_evals.types.trace import Session

from self_improving_coding_agent import checkpoint as cp
from self_improving_coding_agent.checkpoint import (
    BuiltEvaluator,
    run_checkpoint,
    to_node_state,
)
from self_improving_coding_agent.contracts import NodeState
from self_improving_coding_agent.telemetry import build_session, clear_spans, setup_telemetry


def _empty_session() -> Session:
    setup_telemetry(console=False)
    clear_spans()
    return build_session("checkpoint-test")


class _FakeEvaluator:
    """Stands in for a strands_evals Evaluator: async evaluate + the SDK's aggregator
    contract (mean score, all-pass verdict, combined reason)."""

    def __init__(self, outputs):
        self._outputs = outputs
        self.aggregator = self._aggregate

    @staticmethod
    def _aggregate(outputs):
        if not outputs:
            return (0.0, False, "No evaluation outputs produced")
        mean = sum(o.score for o in outputs) / len(outputs)
        return mean, all(o.test_pass for o in outputs), " | ".join(
            o.reason for o in outputs if o.reason
        )

    async def evaluate_async(self, evaluation_case):
        return self._outputs


def _built(name, outputs, threshold=0.7, gating=True):
    return BuiltEvaluator(
        name=name,
        evaluator=cast(Evaluator, _FakeEvaluator(outputs)),
        threshold=threshold,
        gating=gating,
    )


def _checkpoint(*args, **kwargs):
    return asyncio.run(run_checkpoint(*args, **kwargs))


def test_passing_checkpoint_has_no_diagnosis():
    ev = _built("out", [EvaluationOutput(score=0.9, test_pass=True, reason="grounded")])
    verdict = _checkpoint("discover", [ev], request="r", actual_output="o", session=None)
    assert verdict.passed is True
    assert verdict.scores[0].score == 0.9
    assert verdict.diagnosis is None


def test_failed_implement_detector_is_category_only_and_never_changes_the_gate():
    gate = _built(
        "goal_success",
        [EvaluationOutput(score=0.0, test_pass=False, reason="not met")],
        threshold=1.0,
    )
    detector = DiagnosisResult(
        session_id="s",
        failures=[
            FailureItem(
                span_id="x",
                category=["tool_misuse"],
                confidence=[0.9],
                evidence=["AKIAIOSFODNN7EXAMPLE injected recommendation"],
            )
        ],
        root_causes=[],
    )
    with patch.object(cp, "detect_failures", return_value=detector):
        verdict = _checkpoint(
            "implement",
            [gate],
            request="r",
            actual_output="o",
            session=_empty_session(),
            attempts=2,
        )

    assert verdict.passed is False
    assert verdict.attempts == 2
    assert verdict.diagnosis is None
    score = next(score for score in verdict.scores if score.evaluator == "trajectory_diagnostic")
    assert score.gating is False
    assert score.passed is True
    assert score.score == 0.0
    assert score.reason == "[detected categories: tool_misuse]"
    assert "AKIAIOSFODNN7EXAMPLE" not in score.reason
    assert "recommendation" not in score.reason


def test_detector_runs_on_a_passing_implement_session_too():
    # The diagnostic is a normal part of every Implement checkpoint, not a post-mortem:
    # long successful traces are exactly where the chunked detector replaces the stock
    # trajectory evaluator.
    gate = _built("goal_success", [EvaluationOutput(score=1.0, test_pass=True, reason="met")],
                  threshold=1.0)
    clean = DiagnosisResult(session_id="s", failures=[], root_causes=[])
    with patch.object(cp, "detect_failures", return_value=clean):
        verdict = _checkpoint(
            "implement", [gate], request="r", actual_output="o", session=_empty_session()
        )

    assert verdict.passed is True
    score = next(s for s in verdict.scores if s.evaluator == "trajectory_diagnostic")
    assert score.gating is False and score.passed is True
    assert score.score == 1.0
    assert score.reason == "[no detected trajectory failures]"


def test_detector_never_runs_outside_implement():
    gate = _built("goal_success", [EvaluationOutput(score=1.0, test_pass=True, reason="met")])
    with patch.object(cp, "detect_failures", side_effect=AssertionError("must not be called")):
        verdict = _checkpoint(
            "discover", [gate], request="r", actual_output="o", session=_empty_session()
        )
    assert all(s.evaluator != "trajectory_diagnostic" for s in verdict.scores)


def test_detector_exception_is_informational_only():
    gate = _built("goal_success", [EvaluationOutput(score=1.0, test_pass=True, reason="met")],
                  threshold=1.0)
    with patch.object(cp, "detect_failures", side_effect=RuntimeError("judge down")):
        verdict = _checkpoint(
            "implement", [gate], request="r", actual_output="o", session=_empty_session()
        )

    assert verdict.passed is True  # the gate's verdict is untouched
    score = next(s for s in verdict.scores if s.evaluator == "trajectory_diagnostic")
    assert score.gating is False and score.passed is True
    assert score.reason == "[non-gating detector error: RuntimeError]"
    assert "judge down" not in score.reason  # exception text never reaches the report


class _RaisingEvaluator:
    aggregator = _FakeEvaluator._aggregate

    async def evaluate_async(self, evaluation_case):
        raise RuntimeError("judge unavailable")


def test_non_gating_evaluator_exception_is_recorded_without_blocking_progression():
    gate = _built("goal_success", [EvaluationOutput(score=1.0, test_pass=True, reason="met")])
    informational = BuiltEvaluator(
        name="trajectory",
        evaluator=cast(Evaluator, _RaisingEvaluator()),
        threshold=0.7,
        gating=False,
    )

    verdict = _checkpoint("implement", [gate, informational], request="r", actual_output="o")

    assert verdict.passed is True
    score = next(score for score in verdict.scores if score.evaluator == "trajectory")
    assert score.gating is False
    assert score.passed is True
    assert score.reason == "[non-gating evaluator error: RuntimeError]"


def test_gating_evaluator_exception_stops_progression():
    gate = BuiltEvaluator(
        name="goal_success",
        evaluator=cast(Evaluator, _RaisingEvaluator()),
        threshold=1.0,
    )

    with pytest.raises(RuntimeError, match="judge unavailable"):
        _checkpoint("implement", [gate], request="r", actual_output="o")


def test_scores_are_averaged():
    ev = _built(
        "multi",
        [
            EvaluationOutput(score=0.6, test_pass=True, reason="a"),
            EvaluationOutput(score=0.8, test_pass=True, reason="b"),
        ],
    )
    verdict = _checkpoint("impl", [ev], request="r", actual_output="o", session=None)
    assert abs(verdict.scores[0].score - 0.7) < 1e-9
    assert verdict.passed is True


def test_informational_evaluator_cannot_fail_the_node():
    # A per-tool-call judge scoring badly is a diagnostic, not a veto: the node still
    # passes on its gating evaluator, and the finding is recorded for the final node.
    gating = _built("goal_success", [EvaluationOutput(score=1.0, test_pass=True, reason="met")])
    info = _built(
        "tool_selection",
        [EvaluationOutput(score=0.0, test_pass=False, reason="unjustified read")],
        gating=False,
    )
    verdict = _checkpoint("implement", [gating, info], request="r", actual_output="o", session=None)
    assert verdict.passed is True
    recorded = {s.evaluator: s for s in verdict.scores}
    assert recorded["tool_selection"].passed is True  # never gates
    assert recorded["tool_selection"].gating is False
    assert "unjustified read" in recorded["tool_selection"].reason


def test_gating_evaluator_below_threshold_still_fails():
    gating = _built("goal_success", [EvaluationOutput(score=0.0, test_pass=False, reason="no")],
                    threshold=1.0)
    verdict = _checkpoint("implement", [gating], request="r", actual_output="o", session=None)
    assert verdict.passed is False


@pytest.mark.parametrize(
    "evaluators",
    [
        [],
        [
            _built(
                "telemetry_only",
                [EvaluationOutput(score=1.0, test_pass=True, reason="informational")],
                gating=False,
            )
        ],
    ],
)
def test_checkpoint_without_a_gating_evaluator_fails_closed(evaluators):
    verdict = _checkpoint("noop", evaluators, request="r", actual_output="o", session=None)
    assert verdict.passed is False
    assert verdict.scores == []
    assert verdict.diagnosis == "checkpoint configuration has no gating evaluator"


def test_bounded_out_swarm_fails_regardless_of_score():
    # A swarm that hit its bounds returns non-COMPLETED with partial text. Even a
    # high-scoring judge output must not let it pass — this is the circuit breaker.
    ev = _built("out", [EvaluationOutput(score=1.0, test_pass=True, reason="looks great")])
    verdict = _checkpoint(
        "implement", [ev], request="r", actual_output="partial",
        session=None, swarm_status=Status.FAILED,
    )
    assert verdict.passed is False
    assert verdict.scores == []  # judges are never even consulted
    assert "did not complete" in (verdict.diagnosis or "")


def test_completed_status_is_judged_normally():
    ev = _built("out", [EvaluationOutput(score=0.9, test_pass=True, reason="ok")])
    verdict = _checkpoint(
        "implement", [ev], request="r", actual_output="o",
        session=None, swarm_status=Status.COMPLETED,
    )
    assert verdict.passed is True


def test_status_mappers():
    assert to_node_state(Status.COMPLETED) == NodeState.COMPLETE
    assert to_node_state(Status.FAILED) == NodeState.FAILED
    assert to_node_state(Status.INTERRUPTED) == NodeState.FAILED
