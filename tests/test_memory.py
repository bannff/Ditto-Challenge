from unittest.mock import patch

from self_improving_coding_agent.contracts import Lesson, Outcome


@patch("self_improving_coding_agent.memory.aws_credentials", return_value={})
@patch("self_improving_coding_agent.memory.Memory")
def test_store_scrubs_and_stores_verbatim(MockMemory, _creds):
    from self_improving_coding_agent.memory import USER_ID, LessonMemory

    inst = MockMemory.from_config.return_value
    inst.search.return_value = {"results": []}  # dedup lookup finds nothing
    LessonMemory().store(
        Lesson(
            ticket_id="T-1",
            outcome=Outcome.FAILURE,
            content="rotate password=SUPERSECRETVALUE12345 now",
            tags=["x"],
        )
    )
    stored = inst.add.call_args.args[0]
    kwargs = inst.add.call_args.kwargs
    assert "SUPERSECRETVALUE12345" not in stored
    assert kwargs["infer"] is False
    assert kwargs["user_id"] == USER_ID
    assert kwargs["metadata"]["outcome"] == "failure"
    assert kwargs["metadata"]["ticket_id"] == "T-1"


@patch("self_improving_coding_agent.memory.aws_credentials", return_value={})
@patch("self_improving_coding_agent.memory.Memory")
def test_store_skips_near_duplicate(MockMemory, _creds):
    from self_improving_coding_agent.memory import LessonMemory

    inst = MockMemory.from_config.return_value
    inst.search.return_value = {"results": [{"memory": "same", "score": 0.99}]}
    LessonMemory().store(
        Lesson(ticket_id="T-dup", outcome=Outcome.SUCCESS, content="same lesson again")
    )
    inst.add.assert_not_called()  # near-identical lesson is not re-stored


@patch("self_improving_coding_agent.memory.aws_credentials", return_value={})
@patch("self_improving_coding_agent.memory.Memory")
def test_retrieve_parses_results_and_scopes_user(MockMemory, _creds):
    from self_improving_coding_agent.memory import LessonMemory

    inst = MockMemory.from_config.return_value
    inst.search.return_value = {
        "results": [{"memory": "lesson one"}, {"memory": ""}, {"memory": "lesson two"}]
    }
    out = LessonMemory().retrieve("q", limit=5)
    assert out == ["lesson one", "lesson two"]
    assert inst.search.call_args.kwargs["filters"] == {"user_id": "autodev"}
