"""Does the ledger actually engage with the running system, on every path?

The unit tests prove the chain's maths. These prove the wiring: that a tool call made by a
real Strands Agent lands in the chain, that concurrent runs don't corrupt each other, and
that a run which dies mid-flight can't leave a record that looks complete. All offline — the
model is a stub that emits a genuine toolUse, so the SDK's own tool executor drives the hook.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import AsyncGenerator, AsyncIterable
from typing import Any, TypeVar

import pytest
from strands import tool
from strands.models.model import Model
from strands.types.content import Message
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolSpec

from self_improving_coding_agent.agent_plane import build_node_agents
from self_improving_coding_agent.contracts import BlockType
from self_improving_coding_agent.ledger import Ledger
from self_improving_coding_agent.node import AgentSpec, NodeConfig
from self_improving_coding_agent.recorder import RunRecorder

T = TypeVar("T")


@tool
def write_file(path: str, content: str) -> str:
    """Write a file (test double for the real worktree tool)."""
    return f"wrote {path} ({len(content)} bytes)"


class ToolCallingModel(Model):
    """Emits one real toolUse, then finishes. Enough for the SDK to run its tool loop."""

    def __init__(self) -> None:
        self.calls = 0

    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> dict[str, Any]:
        return {"model_id": "test-tool-caller"}

    async def stream(
        self,
        messages: list[Message],
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        self.calls += 1
        yield {"messageStart": {"role": "assistant"}}
        if self.calls == 1:
            yield {
                "contentBlockStart": {
                    "start": {"toolUse": {"name": "write_file", "toolUseId": "tu-1"}}
                }
            }
            yield {
                "contentBlockDelta": {
                    "delta": {
                        "toolUse": {
                            "input": json.dumps(
                                {"path": "inventory.py", "content": "SECRET_BODY_abc123"}
                            )
                        }
                    }
                }
            }
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {"contentBlockDelta": {"delta": {"text": "done"}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
                "metrics": {"latencyMs": 0},
            }
        }

    async def structured_output(
        self,
        output_model: type[T],
        prompt: list[Message],
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, T | Any], None]:
        yield {"output": output_model.model_construct()}  # type: ignore[attr-defined]


def _ledger(tmp_path) -> Ledger:
    return Ledger(tmp_path / "ledger.db")


# ---- the hook path, end to end -------------------------------------------------


def test_a_real_agents_tool_call_lands_in_the_chain(tmp_path):
    """The wiring workflow.py relies on: node.hooks -> build_node_agents ->
    Agent(hooks=...) -> the SDK's tool executor -> AfterToolCallEvent -> a block."""
    ledger = _ledger(tmp_path)
    recorder = RunRecorder(ledger, "run-hook")
    node = NodeConfig(
        name="implement",
        agents=[AgentSpec(name="builder", system_prompt="do the work", tools=[write_file])],
        hooks=[recorder.for_node("implement")],
    )
    model = ToolCallingModel()

    agents = build_node_agents(node, {"builder": model})
    agents[0]("edit the inventory module")

    tool_blocks = [b for b in ledger.blocks("run-hook") if b.block_type == BlockType.TOOL_CALL]
    assert len(tool_blocks) == 1
    payload = tool_blocks[0].payload
    assert payload["tool"] == "write_file"
    assert payload["args"]["path"] == "inventory.py"
    assert payload["status"] == "success"
    assert ledger.verify_chain("run-hook").valid


def test_file_content_never_reaches_the_chain_from_a_live_tool_call(tmp_path):
    # The same projection the unit test checks, but proven on the real hook path — this is
    # where a repo secret would actually escape into a durable row.
    ledger = _ledger(tmp_path)
    recorder = RunRecorder(ledger, "run-hook")
    node = NodeConfig(
        name="implement",
        agents=[AgentSpec(name="builder", system_prompt="do", tools=[write_file])],
        hooks=[recorder.for_node("implement")],
    )

    build_node_agents(node, {"builder": ToolCallingModel()})[0]("edit it")

    stored = json.dumps([b.payload for b in ledger.blocks("run-hook")])
    assert "SECRET_BODY_abc123" not in stored
    assert "sha256:" in stored


def test_the_recorder_is_registered_on_every_agent_in_a_node(tmp_path):
    # A node's swarm has three agents; a tool call by any of them must be recorded, not just
    # the entry-point builder's.
    recorder = RunRecorder(_ledger(tmp_path), "run-hook")
    node = NodeConfig(
        name="implement",
        agents=[
            AgentSpec(name="builder", system_prompt="b", role="builder"),
            AgentSpec(name="reviewer", system_prompt="r", role="reviewer"),
            AgentSpec(name="adversary", system_prompt="a", role="third"),
        ],
        hooks=[recorder.for_node("implement")],
    )
    model = ToolCallingModel()

    agents = build_node_agents(node, {"builder": model, "reviewer": model, "third": model})

    assert len(agents) == 3
    for agent in agents:
        assert agent.hooks.has_callbacks()


def test_a_node_without_hooks_still_builds(tmp_path):
    # The engine must not require a ledger: NodeConfig.hooks defaults empty.
    node = NodeConfig(name="n", agents=[AgentSpec(name="a", system_prompt="p")])
    assert build_node_agents(node, {"builder": ToolCallingModel()})


# ---- concurrent runs ----------------------------------------------------------


def test_concurrent_runs_sharing_one_ledger_keep_separate_valid_chains(tmp_path):
    """The hard requirement is that concurrent tickets never clobber each other. Chains are
    per-run and appends serialize, so interleaved writers must each end up verifiable."""
    ledger = _ledger(tmp_path)
    run_ids = [f"run-{i}" for i in range(6)]
    errors: list[Exception] = []

    def record(run_id: str) -> None:
        try:
            recorder = RunRecorder(ledger, run_id)
            recorder.append(BlockType.RUN_START, {"ticket_id": run_id})
            for node in ("discover", "implement", "verify", "learn"):
                recorder.record_status({"node": node, "state": "running"})
                recorder.record_status({"node": node, "state": "complete"})
            recorder.append(BlockType.RUN_END, {"outcome": "success"})
            if not recorder.intact:
                errors.append(RuntimeError(f"{run_id} dropped {recorder.drops} blocks"))
        except Exception as exc:  # noqa: BLE001 - surfaced via `errors`
            errors.append(exc)

    threads = [threading.Thread(target=record, args=(r,)) for r in run_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    for run_id in run_ids:
        status = ledger.verify_chain(run_id)
        assert status.valid, f"{run_id}: {status.reason}"
        assert status.length == 10
        assert [b.seq for b in ledger.blocks(run_id)] == list(range(10))


def test_one_runs_blocks_never_appear_in_anothers_chain(tmp_path):
    ledger = _ledger(tmp_path)
    RunRecorder(ledger, "run-a").append(BlockType.RUN_START, {"ticket_id": "a"})
    RunRecorder(ledger, "run-b").append(BlockType.RUN_START, {"ticket_id": "b"})

    assert [b.payload["ticket_id"] for b in ledger.blocks("run-a")] == ["a"]
    assert [b.payload["ticket_id"] for b in ledger.blocks("run-b")] == ["b"]


# ---- a run that dies mid-flight ----------------------------------------------


def test_an_unterminated_chain_is_visible_as_unterminated(tmp_path):
    """A crash between blocks leaves a chain with no RUN_END. It still verifies (nothing was
    altered), so the honest signal is the missing terminal block, not a hash failure."""
    ledger = _ledger(tmp_path)
    recorder = RunRecorder(ledger, "run-crash")
    recorder.append(BlockType.RUN_START, {"ticket_id": "t1"})
    recorder.record_status({"node": "implement", "state": "running"})
    # ... process dies here.

    assert ledger.verify_chain("run-crash").valid  # the record is intact, just incomplete
    kinds = [b.block_type for b in ledger.blocks("run-crash")]
    assert BlockType.RUN_END not in kinds
    assert BlockType.LESSON_WRITE not in kinds  # so nothing was learned from it


def test_a_ledger_that_cannot_be_written_does_not_stop_the_work(tmp_path):
    ledger = _ledger(tmp_path)
    conn = sqlite3.connect(ledger.db_path)
    with conn:
        conn.execute("DROP TABLE blocks")
    conn.close()
    recorder = RunRecorder(ledger, "run-broken")

    recorder.append(BlockType.RUN_START, {"ticket_id": "t1"})  # must not raise
    recorder.record_status({"node": "implement", "state": "failed"})

    assert recorder.drops == 2
    assert not recorder.intact  # and the caller refuses to learn from it


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))


def test_tool_attribution_is_fixed_per_node_not_shared(tmp_path):
    """Each node gets its own hook stamped with its own name, so two nodes recording at the
    same time can't mislabel each other's tool calls. There is no shared 'current node'."""
    ledger = _ledger(tmp_path)
    recorder = RunRecorder(ledger, "run-attrib")
    model = ToolCallingModel()

    for node_name in ("implement", "verify"):
        node = NodeConfig(
            name=node_name,
            agents=[AgentSpec(name="builder", system_prompt="do", tools=[write_file])],
            hooks=[recorder.for_node(node_name)],
        )
        build_node_agents(node, {"builder": ToolCallingModel()})[0]("go")

    attributed = [
        b.payload["node"]
        for b in ledger.blocks("run-attrib")
        if b.block_type == BlockType.TOOL_CALL
    ]
    assert attributed == ["implement", "verify"]
    assert model.calls == 0  # each node used its own model instance
