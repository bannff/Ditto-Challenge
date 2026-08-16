from strands import Agent

from self_improving_coding_agent.fallback import build_fallback_model


def test_fallback_model_returns_degraded_response_without_bedrock():
    # No creds, no network — the stub must still produce a usable AgentResult.
    agent = Agent(model=build_fallback_model())
    result = agent("do something expensive")
    assert "Circuit breaker tripped" in str(result)


def test_fallback_config():
    assert build_fallback_model().get_config()["model_id"] == "local-fallback-stub"
