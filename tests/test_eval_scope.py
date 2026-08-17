from datetime import UTC, datetime

from strands_evals.types.trace import (
    AgentInvocationSpan,
    Session,
    SpanInfo,
    ToolCall,
    ToolExecutionSpan,
    ToolResult,
    Trace,
)

from self_improving_coding_agent.eval_scope import (
    _MAX_MESSAGE_CHARS,
    _MAX_SESSION_CHARS,
    _MAX_TOOL_ARG_CHARS,
    _MAX_TOOL_RESULT_CHARS,
    _TRUNCATION_MARK,
    BoundedSessionExtractor,
)


def _span_info() -> SpanInfo:
    now = datetime.now(UTC)
    return SpanInfo(session_id="s", span_id="a1", start_time=now, end_time=now)


def _session(*, result_chars: int, prompt_chars: int) -> Session:
    agent = AgentInvocationSpan(
        span_info=_span_info(),
        user_prompt="p" * prompt_chars,
        agent_response="r" * prompt_chars,
        available_tools=[],
    )
    tool = ToolExecutionSpan(
        span_info=_span_info(),
        tool_call=ToolCall(name="read_file", arguments={"path": "x" * 900, "n": 3}),
        tool_result=ToolResult(content="c" * result_chars),
        agent_span_id="a1",
    )
    return Session(
        traces=[Trace(spans=[agent, tool], trace_id="t", session_id="s")], session_id="s"
    )


def test_oversized_fields_are_clipped_but_the_trajectory_shape_survives():
    # A gating session-level judge gets ONE prompt from the whole session; unbounded
    # tool results are what crashed the live app2 primed run at 202k tokens.
    out = BoundedSessionExtractor().extract(_session(result_chars=500_000, prompt_chars=50_000))

    assert len(out.session_history) == 1  # every turn still present
    ctx = out.session_history[0]
    assert len(ctx.user_prompt.text) <= _MAX_MESSAGE_CHARS + len(_TRUNCATION_MARK)
    assert ctx.user_prompt.text.endswith(_TRUNCATION_MARK)
    (execution,) = ctx.tool_execution_history or []
    assert len(execution.tool_result.content) <= _MAX_TOOL_RESULT_CHARS + len(_TRUNCATION_MARK)
    assert len(execution.tool_call.arguments["path"]) <= _MAX_TOOL_ARG_CHARS + len(
        _TRUNCATION_MARK
    )
    assert execution.tool_call.arguments["n"] == 3  # non-strings untouched
    assert execution.tool_call.name == "read_file"  # the call itself is still visible


def test_turn_count_blowups_are_elided_to_task_plus_recent_tail():
    # The SDK duplicates a trace's full tool history into EVERY agent context, so a
    # 3-agent swarm's turn count — not any one field — is what pushed the live primed
    # judge prompt past 200k tokens. The budget keeps the opening task and the endgame.
    spans = [
        AgentInvocationSpan(
            span_info=_span_info(),
            user_prompt=f"turn-{i}: " + "p" * 3_500,
            agent_response=f"resp-{i}: " + "r" * 3_500,
            available_tools=[],
        )
        for i in range(60)
    ]
    tools = [
        ToolExecutionSpan(
            span_info=_span_info(),
            tool_call=ToolCall(name="read_file", arguments={"path": "f"}),
            tool_result=ToolResult(content="c" * 1_400),
        )
        for _ in range(20)
    ]
    session = Session(
        traces=[Trace(spans=[*spans, *tools], trace_id="t", session_id="s")], session_id="s"
    )

    out = BoundedSessionExtractor().extract(session)

    total = sum(len(c.model_dump_json()) for c in out.session_history)
    assert total <= _MAX_SESSION_CHARS + 1_000  # budget holds (marker adds a little)
    assert out.session_history[0].user_prompt.text.startswith("turn-0")  # task kept
    assert out.session_history[-1].user_prompt.text.startswith("turn-59")  # endgame kept
    assert any("elided for evaluation" in c.user_prompt.text for c in out.session_history)


def test_small_sessions_pass_through_unmarked():
    out = BoundedSessionExtractor().extract(_session(result_chars=100, prompt_chars=100))

    ctx = out.session_history[0]
    assert _TRUNCATION_MARK not in ctx.user_prompt.text
    (execution,) = ctx.tool_execution_history or []
    assert _TRUNCATION_MARK not in execution.tool_result.content
