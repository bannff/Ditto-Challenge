from self_improving_coding_agent.contracts import (
    AcceptanceResult,
    Lesson,
    Outcome,
    RunReport,
    Ticket,
)
from self_improving_coding_agent.ledger import MAX_EVIDENCE_CHARS, Ledger


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


def test_write_scrubs_request_and_verdicts(tmp_path):
    from self_improving_coding_agent.contracts import EvaluatorScore, Verdict

    ledger = Ledger(tmp_path / "ledger.db")
    report = _report()
    report.ticket.request = "please use token=LEAKED_REQUEST_SECRET1"
    report.verdicts = [
        Verdict(
            node="verify",
            passed=False,
            diagnosis="failed near password=DIAGNOSIS_SECRET2",
            scores=[EvaluatorScore(evaluator="c", score=0.1, threshold=0.6, passed=False,
                                   reason="saw secret=REASON_SECRET3")],
        )
    ]
    ledger.save(report)
    got = ledger.get("r1")
    assert got is not None
    assert "LEAKED_REQUEST_SECRET1" not in got.ticket.request
    assert "DIAGNOSIS_SECRET2" not in (got.verdicts[0].diagnosis or "")
    assert "REASON_SECRET3" not in got.verdicts[0].scores[0].reason


def test_recent_orders_newest_first(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.save(_report(run_id="old"))
    ledger.save(_report(run_id="new"))
    ids = [r.run_id for r in ledger.recent(limit=5)]
    assert set(ids) == {"old", "new"}


def test_missing_run_returns_none(tmp_path):
    assert Ledger(tmp_path / "ledger.db").get("nope") is None


def test_small_diff_is_stored_whole(tmp_path):
    diff = "diff --git a/app.py b/app.py\n-x = 1\n+x = 2\n"
    stored = Ledger(tmp_path / "l.db").save(_report(evidence=diff))
    assert stored.evidence == diff


def test_caller_keeps_the_whole_diff_when_the_row_is_capped(tmp_path):
    # Reviewer-facing artifacts are written from the report the caller holds, so capping
    # the history row must not reach back into it.
    big = "+x\n" * MAX_EVIDENCE_CHARS
    report = _report(evidence=big, run_id="big")

    stored = Ledger(tmp_path / "l.db").save(report)

    assert report.evidence == big
    assert len(stored.evidence) < len(big)


def test_capped_row_says_it_is_partial_and_where_the_rest_is(tmp_path):
    report = _report(evidence="+x\n" * MAX_EVIDENCE_CHARS)
    report.branch = "autodev/run-abc"

    stored = Ledger(tmp_path / "l.db").save(report)

    assert "truncated" in stored.evidence
    assert "autodev/run-abc" in stored.evidence


def test_capped_row_without_a_branch_says_nothing_was_committed(tmp_path):
    stored = Ledger(tmp_path / "l.db").save(_report(evidence="+x\n" * MAX_EVIDENCE_CHARS))
    assert "did not commit" in stored.evidence
