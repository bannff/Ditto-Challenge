"""Test doubles shared across test modules.

Lives here rather than in one test file so a second module doesn't have to import from a
first. Resolved by pytest's default prepend import mode, which puts `tests/` on `sys.path` —
note that `from tests._doubles import ...` would NOT work, since `tests/` is deliberately not
a package.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterable
from typing import Any

from strands.models.model import Model

# A value distinctive enough that a leak into a durable record is unmistakable in an assert.
STRUCTURED_VALUE = "verdict-payload-should-not-persist"


class FakeModel(Model):
    """Minimal inner model: yields text, then a stop reason and usage."""

    def __init__(self, text: str = "done", model_id: str = "test-model") -> None:
        self.text = text
        self.model_id = model_id
        self.seen_messages: list[Any] = []

    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> dict[str, Any]:
        return {"model_id": self.model_id}

    async def stream(
        self, messages, tool_specs=None, system_prompt=None, **kwargs
    ) -> AsyncIterable[Any]:
        self.seen_messages.append(messages)
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockDelta": {"delta": {"text": self.text}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 11, "outputTokens": 22, "totalTokens": 33},
                "metrics": {"latencyMs": 5},
            }
        }

    async def structured_output(
        self, output_model, prompt, system_prompt=None, **kwargs
    ) -> AsyncGenerator[Any, None]:
        yield {"output": output_model(value=STRUCTURED_VALUE)}
