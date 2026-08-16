"""Scope tool-level evaluators to the agent's own tools.

The stock TraceExtractor hands every `execute_tool` span to a tool-level judge, including
framework- and plugin-injected calls (the steering interceptor, the skills loader, the
judges' own rating tools). Those aren't agent decisions, and they aren't in the agent's
declared tool list, so the judge is asked whether a call to a tool that "doesn't exist"
was justified — it scores badly by construction and pollutes the result.
"""

from __future__ import annotations

from strands_evals.extractors.trace_extractor import TraceExtractor
from strands_evals.types.trace import ToolExecutionSpan

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
