from strands import tool

from self_improving_coding_agent.nodes import build_reference_nodes


@tool
def _fake_policy(query: str) -> str:
    """stub policy tool"""
    return "no policy"


@tool
def _fake_read(path: str) -> str:
    """stub read tool"""
    return ""


def test_reference_nodes_shape():
    nodes = build_reference_nodes(
        worktree_tools=[_fake_read], policy_tool=_fake_policy, primed_lessons="prior lesson"
    )
    names = [n.name for n in nodes]
    assert names == ["discover", "implement", "verify", "learn"]
    # Implement advances on GoalSuccess alone; other nodes retain their own gates.
    for node in nodes:
        assert {agent.role for agent in node.agents} == {"builder", "reviewer", "third"}
        expected_gates = 1 if node.name == "implement" else 2
        gating_evaluators = [evaluator for evaluator in node.evaluators if evaluator.gating]
        assert len(gating_evaluators) >= expected_gates
    # only implement carries the tool-call steering interceptor
    implement = nodes[1]
    assert implement.steering_prompt is not None
    assert all(n.steering_prompt is None for n in nodes if n.name != "implement")


def test_per_tool_call_judges_are_informational_and_scoped():
    # Tool-level judges must never gate a node (a few unjustified reads isn't a failure),
    # and must be scoped so plugin/infra tool calls aren't judged as agent decisions.
    nodes = build_reference_nodes(worktree_tools=[_fake_read], policy_tool=_fake_policy)
    tool_specs = [
        e for n in nodes for e in n.evaluators if "tool_" in e.name or e.name == "trajectory"
    ]
    assert tool_specs, "expected per-tool-call judges to still run"
    for spec in tool_specs:
        assert spec.gating is False
    # GoalSuccess is the sole implement gate; all quality and trajectory judges inform it.
    implement = next(n for n in nodes if n.name == "implement")
    gates = {e.name for e in implement.evaluators if e.gating}
    assert gates == {"goal_success"}


def test_primed_lessons_injected_into_discover():
    nodes = build_reference_nodes(
        worktree_tools=[], policy_tool=_fake_policy, primed_lessons="raise swarm bounds"
    )
    assert "raise swarm bounds" in nodes[0].agents[0].system_prompt


def test_no_primed_lessons_is_clean():
    nodes = build_reference_nodes(worktree_tools=[], policy_tool=_fake_policy)
    assert "lessons from past runs" not in nodes[0].agents[0].system_prompt


def test_recall_tool_reaches_every_node_read_only():
    nodes = build_reference_nodes(
        worktree_tools=[], policy_tool=_fake_policy, recall_tool=_fake_read
    )
    by_name = {n.name: n for n in nodes}
    # Every node can READ memory (recall) — including Learn, so it can dedup before writing.
    assert _fake_read in by_name["discover"].agents[0].tools
    assert _fake_read in by_name["implement"].shared_tools
    assert _fake_read in by_name["verify"].agents[0].tools
    assert _fake_read in by_name["learn"].agents[0].tools
    # There is no memory-WRITE tool anywhere; persistence is code-gated (junk-resistance).
