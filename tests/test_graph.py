import asyncio
from unittest.mock import patch

from self_improving_coding_agent import graph
from self_improving_coding_agent.contracts import Outcome, Verdict
from self_improving_coding_agent.fallback import build_fallback_model
from self_improving_coding_agent.graph import WorkflowModels, run_workflow
from self_improving_coding_agent.node import AgentSpec, NodeConfig


def _models() -> WorkflowModels:
    m = build_fallback_model()
    return WorkflowModels(
        builder=m, reviewer=m, third=m, evaluator=m, fallback=build_fallback_model()
    )


def _node(name="discover", max_redos=1):
    return NodeConfig(
        name=name,
        agents=[AgentSpec(name=f"{name}-a", system_prompt="do")],
        max_redos=max_redos,
    )


def test_happy_path_no_evaluators_runs_offline():
    result = run_workflow([_node("discover"), _node("verify")], "resolve ticket", models=_models())
    assert result.outcome == Outcome.SUCCESS
    assert set(result.outputs) == {"discover", "verify"}
    assert result.degraded is False
    assert result.verdicts[0].passed is True


def test_failing_checkpoint_trips_breaker_after_max_redos():
    node = _node(max_redos=1)

    async def always_fail(node_name, evaluators, **kw):
        return Verdict(node=node_name, passed=False, attempts=kw["attempts"], diagnosis="nope")

    with patch.object(graph, "run_checkpoint", side_effect=always_fail):
        result = run_workflow([node], "t", models=_models())

    assert result.degraded is True
    assert result.outcome == Outcome.FAILURE
    assert result.verdicts[0].attempts == node.max_redos + 1  # ran initial + redos, then tripped


def test_self_heal_passes_on_second_attempt():
    calls = {"n": 0}

    async def fail_then_pass(node_name, evaluators, **kw):
        calls["n"] += 1
        return Verdict(node=node_name, passed=calls["n"] >= 2, attempts=kw["attempts"])

    with patch.object(graph, "run_checkpoint", side_effect=fail_then_pass):
        result = run_workflow([_node(max_redos=2)], "t", models=_models())

    assert result.degraded is False
    assert result.outcome == Outcome.SUCCESS
    assert result.verdicts[0].attempts == 2


def test_retries_exhausted_then_breaker_degrades():
    # No fork: when a node's informed retries are spent, the circuit breaker degrades the
    # run gracefully rather than looping.
    main = NodeConfig(
        name="impl",
        agents=[AgentSpec(name="a", system_prompt="do")],
        max_redos=1,
    )

    async def always_fail(node_name, evaluators, **kw):
        return Verdict(node=node_name, passed=False, attempts=kw["attempts"])

    with patch.object(graph, "run_checkpoint", side_effect=always_fail):
        result = run_workflow([main], "t", models=_models())

    assert result.degraded is True
    assert result.outcome == Outcome.FAILURE
    assert result.verdicts[0].attempts == main.max_redos + 1


def test_wall_clock_deadline_degrades_before_next_node():
    # A zero-second deadline trips before the first node runs -> graceful degrade, no work.
    result = run_workflow(
        [_node("discover"), _node("verify")], "t", models=_models(), deadline_seconds=0.0
    )
    assert result.degraded is True
    assert result.outcome == Outcome.FAILURE
    assert result.verdicts == []


def test_status_events_emitted():
    events = []
    run_workflow([_node("discover")], "t", models=_models(), status_cb=events.append)
    assert any(e["state"] == "running" for e in events)
    assert any(e["state"] == "complete" for e in events)
    assert all({"node", "state", "eval_score", "timestamp"} <= e.keys() for e in events)


def test_final_node_task_includes_all_stage_outputs_and_verdicts():
    seen = {}
    original = graph._compose_task

    def spy(base, outputs, verdicts, is_final):
        task = original(base, outputs, verdicts, is_final)
        if is_final:
            seen["task"] = task
        return task

    with patch.object(graph, "_compose_task", side_effect=spy):
        run_workflow(
            [_node("discover"), _node("implement"), _node("learn")], "t", models=_models()
        )

    # The final node distills the lesson, so it must see every stage's output — not
    # just its predecessor's — plus the verdict summary.
    assert "Output from discover:" in seen["task"]
    assert "Output from implement:" in seen["task"]
    assert "Eval results from earlier stages:" in seen["task"]


def test_retry_reruns_same_node_via_graph_self_loop():
    order = []

    async def fail_first_discover(node_name, evaluators, **kw):
        order.append(node_name)
        passed = not (node_name == "discover" and order.count("discover") == 1)
        return Verdict(node=node_name, passed=passed, attempts=kw["attempts"])

    with patch.object(graph, "run_checkpoint", side_effect=fail_first_discover):
        result = run_workflow(
            [_node("discover", max_redos=2), _node("verify")], "t", models=_models()
        )

    assert order == ["discover", "discover", "verify"]  # exactly one revisit, then advance
    assert result.outcome == Outcome.SUCCESS
    assert result.verdicts[0].attempts == 2


def test_ping_pong_damper_reaches_the_swarm():
    captured = {}
    real_swarm = graph.Swarm

    def spying_swarm(*args, **kwargs):
        captured.update(kwargs)
        return real_swarm(*args, **kwargs)

    node = _node("discover")
    node.repetitive_handoff_detection_window = 6
    node.repetitive_handoff_min_unique_agents = 2
    with patch.object(graph, "Swarm", side_effect=spying_swarm):
        run_workflow([node], "t", models=_models())

    assert captured["repetitive_handoff_detection_window"] == 6
    assert captured["repetitive_handoff_min_unique_agents"] == 2


def test_node_abandoned_between_attempts_degrades_instead_of_reporting_success():
    """The deadline can expire while a node still has retries left. The gate never learns
    that — it only saw a failed checkpoint — so nothing marked the run degraded and its
    last unverified attempt became the result. That reported SUCCESS on unverified work."""
    node = _node(max_redos=3)  # retries remain, so the breaker never trips

    async def slow_failure(node_name, evaluators, **kw):
        await asyncio.sleep(0.4)  # outlive the deadline below
        return Verdict(node=node_name, passed=False, attempts=kw["attempts"], diagnosis="nope")

    states = []
    with patch.object(graph, "run_checkpoint", side_effect=slow_failure):
        result = run_workflow(
            [node],
            "t",
            models=_models(),
            status_cb=lambda e: states.append(e["state"]),
            deadline_seconds=0.2,
        )

    assert result.degraded is True
    assert result.outcome == Outcome.FAILURE
    assert result.verdicts and result.verdicts[0].passed is False  # the failure is recorded
    assert "failed" in states  # so a breaker-trip block reaches the ledger


def test_a_passing_node_checkpoints_the_tree_that_earned_it():
    taken = []
    result = run_workflow(
        [_node("discover"), _node("verify")],
        "t",
        models=_models(),
        checkpoint_cb=lambda name: taken.append(name) or f"hash-{name}",
    )

    assert result.outcome == Outcome.SUCCESS
    assert taken == ["discover", "verify"]


def test_a_failed_attempt_is_rolled_back_before_the_informed_retry():
    """Without this the retry starts on top of whatever the failed attempt left behind, and
    diagnoses a tree nobody intended."""
    node = _node(max_redos=2)
    calls = {"n": 0}
    restores = []

    async def fail_then_pass(node_name, evaluators, **kw):
        calls["n"] += 1
        passed = calls["n"] > 2  # fail, retry, retry, then pass
        return Verdict(node=node_name, passed=passed, attempts=kw["attempts"])

    with patch.object(graph, "run_checkpoint", side_effect=fail_then_pass):
        result = run_workflow(
            [node],
            "t",
            models=_models(),
            restore_cb=lambda: restores.append(1),
        )

    assert result.outcome == Outcome.SUCCESS
    assert len(restores) == 2  # once before each retry, never after the pass


def test_no_rollback_once_the_breaker_has_tripped():
    # Retries are spent, so there is no next attempt to prepare a tree for.
    node = _node(max_redos=1)
    restores = []

    async def always_fail(node_name, evaluators, **kw):
        return Verdict(node=node_name, passed=False, attempts=kw["attempts"])

    with patch.object(graph, "run_checkpoint", side_effect=always_fail):
        result = run_workflow(
            [node], "t", models=_models(), restore_cb=lambda: restores.append(1)
        )

    assert result.degraded is True
    assert len(restores) == node.max_redos  # one per retry, none for the trip itself


def test_a_failed_node_is_not_checkpointed():
    node = _node(max_redos=0)
    taken = []

    async def always_fail(node_name, evaluators, **kw):
        return Verdict(node=node_name, passed=False, attempts=kw["attempts"])

    with patch.object(graph, "run_checkpoint", side_effect=always_fail):
        run_workflow([node], "t", models=_models(), checkpoint_cb=lambda n: taken.append(n))

    assert taken == []  # only a tree that passed its checkpoint gets committed


def test_the_engine_runs_without_any_checkpointing():
    # The graph must not require a workspace: both callbacks default to None.
    result = run_workflow([_node("discover")], "t", models=_models())
    assert result.outcome == Outcome.SUCCESS
