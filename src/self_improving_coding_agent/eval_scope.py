"""Scope tool-level evaluators to the agent's own tools.

The stock TraceExtractor hands every `execute_tool` span to a tool-level judge, including
framework- and plugin-injected calls (the steering interceptor, the skills loader, the
judges' own rating tools). Those aren't agent decisions, and they aren't in the agent's
declared tool list, so the judge is asked whether a call to a tool that "doesn't exist"
was justified — it scores badly by construction and pollutes the result.
"""

from __future__ import annotations

from strands_evals.extractors.trace_extractor import TraceExtractor
from strands_evals.types.trace import (
    Context,
    EvaluationLevel,
    Session,
    SessionLevelInput,
    TextContent,
    ToolExecutionSpan,
)

# Plugin/infra tools the agent never chose to call, plus the evaluators' rating tools.
EXCLUDED_TOOLS = frozenset({"_LLMSteering", "skills"})
_EXCLUDED_SUFFIXES = ("Rating", "Score", "EvaluationOutput")


def is_agent_tool(name: str) -> bool:
    return name not in EXCLUDED_TOOLS and not name.endswith(_EXCLUDED_SUFFIXES)


class AgentToolsExtractor(TraceExtractor):
    """A TraceExtractor that only surfaces tool calls the agent actually chose to make."""

    def _find_tool_execution_spans(self, trace) -> list[ToolExecutionSpan]:
        return [
            span
            for span in super()._find_tool_execution_spans(trace)
            if is_agent_tool(span.tool_call.name)
        ]


# Bounds for a session-level judge's input. Two failure axes on a long swarm run: field
# size (whole-file tool results, repeated prompts) and turn count (3 agents x 16
# iterations of contexts). Both are capped — per-field clips first, then a whole-session
# character budget that keeps the opening task and the most recent turns and elides the
# middle. ~4 chars/token, so 200k chars is roughly a quarter of a 200k-token judge window.
_MAX_TOOL_RESULT_CHARS = 1_500
_MAX_TOOL_ARG_CHARS = 300
_MAX_MESSAGE_CHARS = 4_000
_MAX_EXECUTIONS_PER_CONTEXT = 20
_MAX_SESSION_CHARS = 200_000
_TRUNCATION_MARK = "\n[... truncated for evaluation ...]"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + _TRUNCATION_MARK


def _clip_context(context: Context) -> None:
    context.user_prompt.text = _clip(context.user_prompt.text, _MAX_MESSAGE_CHARS)
    context.agent_response.text = _clip(context.agent_response.text, _MAX_MESSAGE_CHARS)
    executions = context.tool_execution_history or []
    if len(executions) > _MAX_EXECUTIONS_PER_CONTEXT:
        context.tool_execution_history = executions = executions[-_MAX_EXECUTIONS_PER_CONTEXT:]
    for execution in executions:
        execution.tool_call.arguments = {
            key: _clip(value, _MAX_TOOL_ARG_CHARS) if isinstance(value, str) else value
            for key, value in execution.tool_call.arguments.items()
        }
        execution.tool_result.content = _clip(
            execution.tool_result.content, _MAX_TOOL_RESULT_CHARS
        )
        if execution.tool_result.error is not None:
            execution.tool_result.error = _clip(
                execution.tool_result.error, _MAX_TOOL_RESULT_CHARS
            )


def _elision_marker(elided: int) -> Context:
    return Context(
        user_prompt=TextContent(
            text=f"[... {elided} intermediate turn(s) elided for evaluation ...]"
        ),
        agent_response=TextContent(text="[elided]"),
    )


def _budget_contexts(contexts: list[Context]) -> list[Context]:
    """Keep the opening task and the most recent turns within the session budget."""
    sizes = [len(context.model_dump_json()) for context in contexts]
    if sum(sizes) <= _MAX_SESSION_CHARS or len(contexts) <= 2:
        return contexts
    remaining = _MAX_SESSION_CHARS - sizes[0]
    tail: list[Context] = []
    for context, size in zip(reversed(contexts[1:]), reversed(sizes[1:]), strict=True):
        if size > remaining:
            break
        tail.append(context)
        remaining -= size
    tail.reverse()
    elided = len(contexts) - 1 - len(tail)
    if elided <= 0:
        return contexts
    return [contexts[0], _elision_marker(elided), *tail]


class BoundedSessionExtractor(TraceExtractor):
    """Session-level extraction with per-field and whole-session size bounds.

    Session-level judges (GoalSuccessRateEvaluator) serialize the whole session into ONE
    judge prompt; no conversation manager can shrink an already-oversized single message,
    so an unbounded long trace crashes the judge with a context overflow. The judge's
    question is binary goal attainment, so the task and the endgame matter most: fields
    are clipped, then middle turns are elided until the session fits the budget.
    """

    def __init__(self) -> None:
        super().__init__(EvaluationLevel.SESSION_LEVEL)

    def extract(self, session: Session):
        result = super().extract(session)
        assert isinstance(result, SessionLevelInput)
        for context in result.session_history:
            _clip_context(context)
        result.session_history = _budget_contexts(result.session_history)
        return result
