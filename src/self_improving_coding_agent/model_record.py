"""Record every model call into the run's hash chain — hashes only, never payloads.

This is the seam hooks can't reach: `BeforeModelCallEvent` carries no request fields at all,
so the only way to see what was actually sent is to wrap the `Model`. `FallbackModel` is the
precedent for that shape — a local `Model` subclass is the SDK's designated extension point,
and nothing in strands / strands_tools / strands_evals provides a record-replay primitive.

What lands in the chain is three digests per call and nothing else. That is deliberate: a
model request is repository source, ticket text, tool results, *and* the primed lessons from
memory. Storing it would be a far wider version of the secret sink we closed for
`write_file.content`, and `scrub_text` is shape-based — it cannot make arbitrary repo source
safe to persist. Digests keep the audit value with none of the exposure:

- `request_hash`  — the conversation and the tools offered. Diverges when behavior diverges.
- `system_hash`   — the system prompt, which carries primed lessons. Diverges when *memory*
                    changed, which is expected between runs and is why it is hashed apart
                    from the request rather than smeared into it.
- `response_hash` — what the model said, plus its stop reason and any tools it chose.

Splitting request from system is what makes divergence detection useful: a differing
`system_hash` says priming moved, a differing `request_hash` under an identical `system_hash`
says the run genuinely took a different path, and the block ordinal says where.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncGenerator, AsyncIterable, Callable
from typing import Any, TypeVar

from pydantic import BaseModel
from strands.models.model import Model
from strands.types.content import Message
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolSpec

T = TypeVar("T", bound=BaseModel)


def _digest(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def digest_request(messages: list[Message], tool_specs: list[ToolSpec] | None) -> str:
    """The conversation plus the tools offered — what changes when behavior changes."""
    return _digest({"messages": messages, "tools": _tool_names(tool_specs)})


def digest_system(system_prompt: Any) -> str:
    return _digest(_system_text(system_prompt))


def _system_text(system_prompt: Any) -> str:
    """The system prompt is either a string or a list of content blocks."""
    if system_prompt is None:
        return ""
    if isinstance(system_prompt, str):
        return system_prompt
    return json.dumps(system_prompt, sort_keys=True, default=str)


def _tool_names(tool_specs: list[ToolSpec] | None) -> list[str]:
    # Names only, sorted: the full schemas are stable boilerplate, and their order varies
    # with registry iteration, which would produce spurious divergence.
    return sorted(str(spec.get("name", "")) for spec in (tool_specs or []))


class _StreamSummary:
    """Accumulates a response into the few facts worth hashing."""

    def __init__(self) -> None:
        self.text: list[str] = []
        self.tools: list[str] = []
        self.stop_reason: str | None = None
        self.usage: dict[str, Any] = {}

    def observe(self, event: StreamEvent) -> None:
        if "contentBlockStart" in event:
            tool_use = (event["contentBlockStart"].get("start") or {}).get("toolUse") or {}
            if tool_use.get("name"):
                self.tools.append(str(tool_use["name"]))
        elif "contentBlockDelta" in event:
            delta = event["contentBlockDelta"].get("delta") or {}
            if "text" in delta:
                self.text.append(str(delta["text"]))
        elif "messageStop" in event:
            reason = event["messageStop"].get("stopReason")
            self.stop_reason = str(reason) if reason is not None else None
        elif "metadata" in event:
            self.usage = dict(event["metadata"].get("usage") or {})

    def response_hash(self) -> str:
        return _digest(
            {
                "text": "".join(self.text),
                "tools": self.tools,
                "stop_reason": self.stop_reason,
            }
        )


class RecordingModel(Model):
    """Wraps a model so every call leaves a digest in the chain.

    Transparent by construction: it yields the inner model's events through untouched and
    adds no behavior. `emit` receives one payload per completed call and is expected to
    append a block; it must never raise into the stream, which is why the recorder's own
    append already swallows and counts its failures.
    """

    def __init__(
        self,
        inner: Model,
        emit: Callable[[dict[str, Any]], None],
        *,
        node: str,
        agent: str,
    ) -> None:
        self._inner = inner
        self._emit = emit
        self._node = node
        self._agent = agent
        # Per-instance, and one instance per agent: Swarm's reset_executor_state() restores
        # messages/state/model_state but never touches the model, so this survives handoffs
        # and attempts, which is what makes the ordinal meaningful.
        self._calls = 0

    # Every part of the Model surface delegates. A wrapper that answered these from the base
    # class would quietly change behavior it has no business changing: `stateful` gates the
    # Agent's conversation/context manager validation, and `context_window_limit` drives
    # context compression.
    def update_config(self, **model_config: Any) -> None:
        self._inner.update_config(**model_config)

    def get_config(self) -> Any:
        return self._inner.get_config()

    @property
    def stateful(self) -> bool:
        return self._inner.stateful

    @property
    def context_window_limit(self) -> int | None:
        return self._inner.context_window_limit

    async def count_tokens(
        self,
        messages: list[Message],
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        system_prompt_content: Any = None,
    ) -> int:
        return await self._inner.count_tokens(
            messages, tool_specs, system_prompt, system_prompt_content
        )

    def estimate_utilization(self, input_tokens: int) -> float:
        return self._inner.estimate_utilization(input_tokens)

    def _model_id(self) -> str:
        config = self._inner.get_config()
        if isinstance(config, dict):
            return str(config.get("model_id", "unknown"))
        return "unknown"

    def _record(self, kind: str, request: dict[str, Any], response: dict[str, Any]) -> None:
        self._calls += 1
        self._emit(
            {
                "node": self._node,
                "agent": self._agent,
                "call": self._calls,
                "kind": kind,
                "model_id": self._model_id(),
                **request,
                **response,
            }
        )

    async def stream(
        self,
        messages: list[Message],
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        request = {
            "request_hash": digest_request(messages, tool_specs),
            "system_hash": digest_system(system_prompt),
        }
        summary = _StreamSummary()
        try:
            async for event in self._inner.stream(
                messages, tool_specs, system_prompt, **kwargs
            ):
                summary.observe(event)
                yield event
        finally:
            # In a finally so a call that errors or is cancelled mid-stream still leaves a
            # record — an unfinished call is exactly the kind of thing an audit wants.
            self._record(
                "stream",
                request,
                {
                    "response_hash": summary.response_hash(),
                    "stop_reason": summary.stop_reason,
                    "tools_called": summary.tools,
                    "input_tokens": summary.usage.get("inputTokens"),
                    "output_tokens": summary.usage.get("outputTokens"),
                },
            )

    async def structured_output(
        self,
        output_model: type[T],
        prompt: list[Message],
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, T | Any], None]:
        request = {
            "request_hash": _digest({"messages": prompt, "tools": [output_model.__name__]}),
            "system_hash": digest_system(system_prompt),
        }
        output: Any = None
        try:
            async for event in self._inner.structured_output(
                output_model, prompt, system_prompt, **kwargs
            ):
                output = event.get("output", output)
                yield event
        finally:
            self._record(
                "structured_output",
                request,
                {
                    "response_hash": _digest(
                        output.model_dump(mode="json")
                        if hasattr(output, "model_dump")
                        else str(output)
                    ),
                    "output_model": output_model.__name__,
                },
            )
