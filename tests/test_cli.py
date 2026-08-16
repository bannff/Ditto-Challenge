"""CLI tests that run fully offline — the refusal path needs no models."""

from __future__ import annotations

import json

import pytest

from self_improving_coding_agent.cli import main


def _write_ticket(path, **overrides) -> str:
    ticket = {"id": "t-1", "repository": "unused", "request": "fix it"}
    ticket.update(overrides)
    path.write_text(json.dumps(ticket), encoding="utf-8")
    return str(path)


def test_run_refuses_underspecified_ticket(tmp_path, capsys):
    ticket_path = _write_ticket(tmp_path / "ticket.json")
    code = main(["run", "--ticket", ticket_path, "--repo", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "refused" in out


def test_run_missing_ticket_file_exits_2(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["run", "--ticket", str(tmp_path / "nope.json"), "--repo", str(tmp_path)])
    assert exc.value.code == 2
