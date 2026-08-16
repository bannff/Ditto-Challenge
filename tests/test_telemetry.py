from self_improving_coding_agent import telemetry
from self_improving_coding_agent.telemetry import (
    build_session,
    clear_spans,
    finished_spans,
    setup_telemetry,
)


def test_setup_is_idempotent():
    assert setup_telemetry(console=False) is setup_telemetry(console=False)


def test_finished_spans_is_list_and_clear_is_safe():
    setup_telemetry(console=False)
    assert isinstance(finished_spans(), list)
    clear_spans()
    assert finished_spans() == []


def test_build_session_from_no_spans_returns_session():
    setup_telemetry(console=False)
    clear_spans()
    session = build_session("empty-run")
    assert session is not None
    assert type(session).__name__ == "Session"


def test_finished_spans_empty_before_setup(monkeypatch):
    monkeypatch.setattr(telemetry, "_exporter", None)
    assert finished_spans() == []
