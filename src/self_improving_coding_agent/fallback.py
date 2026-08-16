"""Circuit-breaker fallback model.

When a node trips its Swarm bounds, it falls back to this local stub instead of hammering
Bedrock again. It emits a fixed degraded response so the run ends gracefully with partial
progress rather than looping. It is deliberately dumb — no local inference, no framework.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterable
from typing import Any, TypeVar

from strands.models.model import Model
from strands.types.content import Message
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolSpec

T = TypeVar("T")

DEGRADED_MESSAGE = (
    "Circuit breaker tripped: this node exceeded its bounds and fell back to a local stub "
    "instead of retrying the model. Reporting partial progress; no change was applied."
)


class FallbackModel(Model):
    def __init__(self, message: str = DEGRADED_MESSAGE):
        self._message = message

    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> dict[str, Any]:
        return {"model_id": "local-fallback-stub"}

    async def stream(
        self,
        messages: list[Message],
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockDelta": {"delta": {"text": self._message}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}
        yield {"metadata": {
            "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
            "metrics": {"latencyMs": 0},
        }}

    async def structured_output(
        self,
        output_model: type[T],
        prompt: list[Message],
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, T | Any], None]:
        yield {"output": output_model.model_construct()}  # type: ignore[attr-defined]


def build_fallback_model() -> FallbackModel:
    return FallbackModel()
