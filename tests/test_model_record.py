"""Model-call records: digests in the chain, and never a payload.

The load-bearing test here is the negative one — a model request contains repo source,
ticket text, tool results, and the primed lessons from memory, so the thing that must be
true is that none of it reaches a block.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from pydantic import BaseModel

from _doubles import STRUCTURED_VALUE, FakeModel
from self_improving_coding_agent.agent_plane import build_node_agents
from self_improving_coding_agent.contracts import BlockType
from self_improving_coding_agent.ledger import Ledger
from self_improving_coding_agent.model_record import RecordingModel
from self_improving_coding_agent.node import AgentSpec, NodeConfig
from self_improving_coding_agent.recorder import RunRecorder

RUN = "run-model"
SECRET_SOURCE = "ENCRYPTION_KEY = 'nQw8vZ2pL5xR7tY1'"  # matches no scrub pattern


class Shape(BaseModel):
    value: str = "x"


def _ledger(tmp_path) -> Ledger:
    return Ledger(tmp_path / "ledger.db")


def _drain(model, messages, tool_specs=None, system_prompt=None) -> list[dict]:
    """Run a stream to completion. Sync wrapper per the repo's convention (test_checkpoint)."""

    async def collect() -> list[dict]:
        return [e async for e in model.stream(messages, tool_specs, system_prompt)]

    return asyncio.run(collect())


def _drain_structured(model, output_model, prompt) -> None:
    async def collect() -> None:
        async for _ in model.structured_output(output_model, prompt):
            pass

    asyncio.run(collect())


def _wrap(ledger: Ledger, inner, node="implement", agent="builder") -> RecordingModel:
    recorder = RunRecorder(ledger, RUN)
    wrapped = recorder.wrap_model(inner, node, agent)
    assert isinstance(wrapped, RecordingModel)
    return wrapped


def _model_calls(ledger: Ledger) -> list[dict]:
    return [b.payload for b in ledger.blocks(RUN) if b.block_type == BlockType.MODEL_CALL]


# ---- the negative test that matters ------------------------------------------


def test_no_part_of_a_model_call_payload_reaches_the_chain(tmp_path):
    ledger = _ledger(tmp_path)
    model = _wrap(ledger, FakeModel(text="the fix is to change the boundary check"))

    _drain(
        model,
        [{"role": "user", "content": [{"text": f"here is the file:\n{SECRET_SOURCE}"}]}],
        [{"name": "write_file"}],
        f"You are a builder.\nLessons from memory:\n- {SECRET_SOURCE}",
    )

    stored = json.dumps(ledger.blocks(RUN)[0].payload)
    assert SECRET_SOURCE not in stored
    assert "nQw8vZ2pL5xR7tY1" not in stored
    assert "here is the file" not in stored
    assert "the fix is to change the boundary check" not in stored  # nor the response
    assert "You are a builder" not in stored


def test_a_call_records_three_digests_and_its_bounds(tmp_path):
    ledger = _ledger(tmp_path)
    model = _wrap(ledger, FakeModel())

    _drain(model, [{"role": "user", "content": [{"text": "go"}]}], None, "sys")

    call = _model_calls(ledger)[0]
    assert len(call["request_hash"]) == 64
    assert len(call["system_hash"]) == 64
    assert len(call["response_hash"]) == 64
    assert call["node"] == "implement"
    assert call["agent"] == "builder"
    assert call["call"] == 1
    assert call["stop_reason"] == "end_turn"
    assert call["input_tokens"] == 11
    assert call["output_tokens"] == 22


# ---- divergence detection ----------------------------------------------------


def test_the_same_request_hashes_the_same(tmp_path):
    ledger = _ledger(tmp_path)
    model = _wrap(ledger, FakeModel())
    messages = [{"role": "user", "content": [{"text": "identical"}]}]

    _drain(model, messages, None, "sys")
    _drain(model, messages, None, "sys")

    first, second = _model_calls(ledger)
    assert first["request_hash"] == second["request_hash"]
    assert first["call"] == 1 and second["call"] == 2  # ordinal still distinguishes them


def test_a_changed_conversation_changes_the_request_hash(tmp_path):
    ledger = _ledger(tmp_path)
    model = _wrap(ledger, FakeModel())

    _drain(model, [{"role": "user", "content": [{"text": "plan A"}]}], None, "sys")
    _drain(model, [{"role": "user", "content": [{"text": "plan B"}]}], None, "sys")

    first, second = _model_calls(ledger)
    assert first["request_hash"] != second["request_hash"]
    assert first["system_hash"] == second["system_hash"]  # priming didn't move


def test_growing_memory_moves_only_the_system_hash(tmp_path):
    """Primed lessons live in the system prompt and grow every run. Hashing them apart from
    the request is what stops 'memory grew' from reading as 'behavior diverged'."""
    ledger = _ledger(tmp_path)
    model = _wrap(ledger, FakeModel())
    messages = [{"role": "user", "content": [{"text": "same task"}]}]

    _drain(model, messages, None, "You are a builder.\nLessons:\n- one")
    _drain(model, messages, None, "You are a builder.\nLessons:\n- one\n- two")

    first, second = _model_calls(ledger)
    assert first["request_hash"] == second["request_hash"]
    assert first["system_hash"] != second["system_hash"]


def test_tool_availability_is_part_of_the_request(tmp_path):
    ledger = _ledger(tmp_path)
    model = _wrap(ledger, FakeModel())
    messages = [{"role": "user", "content": [{"text": "go"}]}]

    _drain(model, messages, [{"name": "read_file"}], "sys")
    _drain(model, messages, [{"name": "read_file"}, {"name": "write_file"}], "sys")

    first, second = _model_calls(ledger)
    assert first["request_hash"] != second["request_hash"]


def test_tool_order_does_not_cause_spurious_divergence(tmp_path):
    # Registry iteration order varies; a differing hash for the same tool set would make
    # divergence detection cry wolf.
    ledger = _ledger(tmp_path)
    model = _wrap(ledger, FakeModel())
    messages = [{"role": "user", "content": [{"text": "go"}]}]

    _drain(model, messages, [{"name": "a"}, {"name": "b"}], "sys")
    _drain(model, messages, [{"name": "b"}, {"name": "a"}], "sys")

    first, second = _model_calls(ledger)
    assert first["request_hash"] == second["request_hash"]


# ---- transparency ------------------------------------------------------------


def test_the_wrapper_passes_events_through_untouched(tmp_path):
    inner = FakeModel(text="hello")
    events = _drain(
        _wrap(_ledger(tmp_path), inner), [{"role": "user", "content": [{"text": "hi"}]}]
    )

    assert events[1]["contentBlockDelta"]["delta"]["text"] == "hello"
    assert events[-2]["messageStop"]["stopReason"] == "end_turn"
    assert len(events) == 5


def test_the_inner_model_sees_the_real_messages(tmp_path):
    # Digesting must not mean rewriting: the model still gets exactly what it was sent.
    inner = FakeModel()
    messages = [{"role": "user", "content": [{"text": SECRET_SOURCE}]}]

    _drain(_wrap(_ledger(tmp_path), inner), messages)

    assert inner.seen_messages[0] == messages


def test_config_passes_through_to_the_inner_model(tmp_path):
    inner = FakeModel(model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0")
    wrapped = _wrap(_ledger(tmp_path), inner)
    assert wrapped.get_config()["model_id"] == inner.model_id


def test_a_call_that_fails_mid_stream_is_still_recorded(tmp_path):
    """An unfinished call is exactly what an audit wants to see, so the record is written
    in a finally rather than after a clean return."""

    class Exploding(FakeModel):
        async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
            yield {"messageStart": {"role": "assistant"}}
            raise RuntimeError("bedrock threw")

    ledger = _ledger(tmp_path)
    model = _wrap(ledger, Exploding())

    with pytest.raises(RuntimeError):
        _drain(model, [{"role": "user", "content": [{"text": "go"}]}])

    call = _model_calls(ledger)[0]
    assert call["stop_reason"] is None  # never reached a stop
    assert ledger.verify_chain(RUN).valid


def test_structured_output_is_recorded_too(tmp_path):
    ledger = _ledger(tmp_path)
    model = _wrap(ledger, FakeModel())

    _drain_structured(model, Shape, [{"role": "user", "content": []}])

    call = _model_calls(ledger)[0]
    assert call["kind"] == "structured_output"
    assert call["output_model"] == "Shape"
    assert STRUCTURED_VALUE not in json.dumps(call)  # the value itself is not stored


# ---- wiring ------------------------------------------------------------------


def test_each_agent_gets_its_own_wrapper_so_ordinals_are_per_agent(tmp_path):
    recorder = RunRecorder(_ledger(tmp_path), RUN)
    node = NodeConfig(
        name="implement",
        agents=[
            AgentSpec(name="builder", system_prompt="b", role="builder"),
            AgentSpec(name="reviewer", system_prompt="r", role="reviewer"),
        ],
        model_wrapper=recorder.wrap_model,
    )
    shared = FakeModel()

    agents = build_node_agents(node, {"builder": shared, "reviewer": shared})

    assert agents[0].model is not agents[1].model  # not the shared instance
    assert all(isinstance(a.model, RecordingModel) for a in agents)


def test_a_node_without_a_wrapper_uses_the_model_directly(tmp_path):
    shared = FakeModel()
    node = NodeConfig(name="n", agents=[AgentSpec(name="a", system_prompt="p")])

    assert build_node_agents(node, {"builder": shared})[0].model is shared


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))


def test_the_whole_model_surface_delegates(tmp_path):
    """A wrapper that answered any of these from the base class would silently change
    behavior it has no business touching — `stateful` gates the Agent's conversation and
    context manager validation, `context_window_limit` drives context compression."""

    class Opinionated(FakeModel):
        @property
        def stateful(self) -> bool:
            return True

        @property
        def context_window_limit(self) -> int | None:
            return 4321

        async def count_tokens(
            self, messages, tool_specs=None, system_prompt=None, system_prompt_content=None
        ) -> int:
            return 99

        def estimate_utilization(self, input_tokens: int) -> float:
            return 0.5

    inner = Opinionated()
    wrapped = _wrap(_ledger(tmp_path), inner)

    assert wrapped.stateful is True
    assert wrapped.context_window_limit == 4321
    assert asyncio.run(wrapped.count_tokens([])) == 99
    assert wrapped.estimate_utilization(10) == 0.5


def test_update_config_reaches_the_inner_model(tmp_path):
    class Configurable(FakeModel):
        def __init__(self) -> None:
            super().__init__()
            self.updates: list[dict] = []

        def update_config(self, **model_config: Any) -> None:
            self.updates.append(model_config)

    inner = Configurable()
    _wrap(_ledger(tmp_path), inner).update_config(temperature=0.1)
    assert inner.updates == [{"temperature": 0.1}]
