from self_improving_coding_agent.fallback import build_fallback_model
from self_improving_coding_agent.graph import WorkflowModels, run_workflow
from self_improving_coding_agent.node import AgentSpec, NodeConfig


def _models() -> WorkflowModels:
    m = build_fallback_model()
    return WorkflowModels(
        builder=m, reviewer=m, third=m, evaluator=m, fallback=build_fallback_model()
    )


def test_run_persists_session_state(tmp_path):
    sessions = tmp_path / "sessions"
    node = NodeConfig(name="discover", agents=[AgentSpec(name="a", system_prompt="do")])
    run_workflow([node], "resolve", models=_models(), session_prefix="run-x", sessions_dir=sessions)
    # FileSessionManager wrote resumable state for the node's swarm.
    assert sessions.exists()
    assert any(sessions.rglob("*")), "expected session files to be written"
