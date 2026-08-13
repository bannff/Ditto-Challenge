from self_improving_coding_agent.contracts import (
    AcceptanceResult,
    Lesson,
    Outcome,
    RunReport,
    Ticket,
)
from self_improving_coding_agent.ledger import Ledger


def _report(run_id="r1", evidence="", tail="", lesson_text=""):
    return RunReport(
        run_id=run_id,
        ticket=Ticket(id="t1", repository="/repo", request="do it"),
        outcome=Outcome.SUCCESS,
        evidence=evidence,
        acceptance=AcceptanceResult(command="pytest", exit_code=0, output_tail=tail),
        lesson=Lesson(ticket_id="t1", outcome=Outcome.SUCCESS, content=lesson_text or "ok"),
    )


def test_save_and_get_round_trips(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.save(_report())
    got = ledger.get("r1")
    assert got is not None
    assert got.run_id == "r1"
    assert got.ticket.id == "t1"


def test_write_scrubs_free_text(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.save(
        _report(
            evidence="leaked ssn 123-45-6789",
            tail="key AKIAIOSFODNN7EXAMPLE",
            lesson_text="password = hunter2secret",
        )
    )
    got = ledger.get("r1")
    assert got is not None
    assert "123-45-6789" not in got.evidence
    assert got.acceptance is not None and "AKIAIOSFODNN7EXAMPLE" not in got.acceptance.output_tail
    assert got.lesson is not None and "hunter2secret" not in got.lesson.content


def test_recent_orders_newest_first(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.save(_report(run_id="old"))
    ledger.save(_report(run_id="new"))
    ids = [r.run_id for r in ledger.recent(limit=5)]
    assert set(ids) == {"old", "new"}


def test_missing_run_returns_none(tmp_path):
    assert Ledger(tmp_path / "ledger.db").get("nope") is None
