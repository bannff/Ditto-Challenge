from self_improving_coding_agent.taxonomy import load_taxonomy


def test_default_taxonomy_loads_and_has_security_rules():
    tax = load_taxonomy()
    assert tax.version >= 1
    sec = tax.get("security")
    assert sec is not None
    assert any("secret" in inv.lower() for inv in sec.invariants)


def test_missing_tag_returns_none():
    assert load_taxonomy().get("does-not-exist") is None
