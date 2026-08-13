from self_improving_coding_agent.kb import PolicyKB, make_query_policy_tool


def test_seed_is_idempotent_and_queryable(tmp_path):
    kb = PolicyKB(tmp_path / "chroma")
    n = kb.seed()
    assert n > 0
    assert kb.seed() == n  # seeding twice does not duplicate
    hits = kb.query("how should I store API keys and credentials")
    assert hits
    assert any("secret" in h.lower() or "credential" in h.lower() for h in hits)


def test_query_policy_tool_returns_text(tmp_path):
    kb = PolicyKB(tmp_path / "chroma")
    kb.seed()
    query_policy = make_query_policy_tool(kb)
    result = query_policy("avoid shell injection in subprocess")
    assert isinstance(result, str)
    assert "subprocess" in result.lower() or "shell" in result.lower()
