"""Server tests — fully offline. The run_ticket tool needs Bedrock, so it's never called;
we test the pure helpers and that the FastMCP app constructs with its primitives wired."""

from __future__ import annotations

import asyncio

from self_improving_coding_agent import server


def test_file_a_ticket_prompt_mentions_ticket_fields():
    text = server._ticket_prompt("make the login page load faster")
    assert text.strip()
    for field in ("id", "repository", "request", "domain", "acceptance_command"):
        assert field in text
    assert "make the login page load faster" in text


def test_taxonomy_resource_returns_json_with_security_tag():
    data = server.taxonomy_resource()
    assert isinstance(data, dict)
    assert "security" in data["tags"]


def test_recent_reports_tolerates_missing_ledger(tmp_path, monkeypatch):
    from self_improving_coding_agent import settings as settings_mod

    monkeypatch.setattr(
        settings_mod.get_settings(), "data_dir", tmp_path / "nope", raising=False
    )
    assert server._recent_reports() == []


def test_new_ticket_id_is_unique_and_prefixed():
    ids = {server._new_ticket_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(i.startswith("tkt-") for i in ids)


def test_app_registers_tool_resource_and_prompt():
    tools = asyncio.run(server.mcp.list_tools())
    resources = asyncio.run(server.mcp.list_resources())
    prompts = asyncio.run(server.mcp.list_prompts())

    assert "run_ticket" in {t.name for t in tools}
    assert {"taxonomy://current", "reports://recent"} <= {str(r.uri) for r in resources}
    assert "file_a_ticket" in {p.name for p in prompts}
