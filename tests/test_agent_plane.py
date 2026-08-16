from self_improving_coding_agent.agent_plane import build_agent, build_node_agents
from self_improving_coding_agent.fallback import build_fallback_model
from self_improving_coding_agent.node import AgentSpec, NodeConfig


def _skill_dir(tmp_path):
    d = tmp_path / "skills" / "repo-scout"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: repo-scout\ndescription: locate relevant code\n---\n"
        "# Instructions\nLook first.\n"
    )
    return d


def test_build_agent_attaches_plugins_and_runs(tmp_path):
    agent = build_agent(
        AgentSpec(name="scout", system_prompt="find the code"),
        model=build_fallback_model(),
        skill_paths=[_skill_dir(tmp_path)],
        steering_prompt="Interrupt any tool call that writes outside the worktree.",
    )
    assert agent.name == "scout"
    # runs offline on the fallback model; skills+steering wired without a Bedrock call
    assert "Circuit breaker" in str(agent("start"))


def test_build_node_agents_selects_model_by_role():
    node = NodeConfig(
        name="discover",
        agents=[
            AgentSpec(name="builder-a", system_prompt="x", role="builder"),
            AgentSpec(name="reviewer-b", system_prompt="y", role="reviewer"),
        ],
    )
    models = {"builder": build_fallback_model(), "reviewer": build_fallback_model()}
    agents = build_node_agents(node, models)
    assert [a.name for a in agents] == ["builder-a", "reviewer-b"]
