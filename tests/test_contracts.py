from datetime import datetime

from hypothesis import given
from hypothesis import strategies as st

from self_improving_coding_agent.contracts import (
    SCHEMA_VERSION,
    AcceptanceResult,
    EvaluatorScore,
    Lesson,
    Outcome,
    RunReport,
    Taxonomy,
    TaxonomyTag,
    Ticket,
    Verdict,
)


def test_run_report_round_trips():
    report = RunReport(
        run_id="r1",
        ticket=Ticket(id="t1", repository="/repo", request="do the thing"),
        verdicts=[
            Verdict(
                node="discover",
                passed=True,
                scores=[
                    EvaluatorScore(
                        evaluator="Correctness", score=0.9, threshold=0.7, passed=True
                    )
                ],
            )
        ],
        acceptance=AcceptanceResult(command="pytest", exit_code=0),
        lesson=Lesson(ticket_id="t1", outcome=Outcome.SUCCESS, content="worked"),
    )
    restored = RunReport.model_validate_json(report.model_dump_json())
    assert restored == report
    assert restored.schema_version == SCHEMA_VERSION
    assert restored.acceptance is not None and restored.acceptance.passed is True
    # computed field must cross the JSON boundary (MCP/UI consumers rely on it)
    assert report.model_dump()["acceptance"]["passed"] is True


@given(exit_code=st.integers(min_value=-5, max_value=5))
def test_acceptance_passed_iff_zero(exit_code):
    r = AcceptanceResult(command="x", exit_code=exit_code)
    assert r.passed == (exit_code == 0)


@given(
    ticket_id=st.text(min_size=1, max_size=20),
    content=st.text(min_size=1, max_size=200),
    outcome=st.sampled_from(list(Outcome)),
)
def test_lesson_round_trips(ticket_id, content, outcome):
    lesson = Lesson(ticket_id=ticket_id, outcome=outcome, content=content)
    restored = Lesson.model_validate_json(lesson.model_dump_json())
    assert restored.ticket_id == ticket_id
    assert restored.outcome == outcome
    assert isinstance(restored.created_at, datetime)


def test_taxonomy_lookup():
    tax = Taxonomy(tags={"security": TaxonomyTag(name="security", invariants=["no secrets"])})
    hit = tax.get("security")
    assert hit is not None and hit.invariants == ["no secrets"]
    assert tax.get("missing") is None
