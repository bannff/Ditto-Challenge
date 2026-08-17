"""Record what a run did as hash-chained ledger blocks.

Two capture seams, both already in the plumbing: the workflow's `status_cb` for node
lifecycle (attempt / verdict / breaker trip) and a Strands hook for per-tool-call detail.
Node lifecycle can't come from hooks — a tripped breaker is the *absence* of further work —
and tool calls can't come from the status stream, so both seams are needed.

Tool calls are captured with `AfterToolCallEvent` rather than mined from OTEL spans: spans
only carry tool arguments under a specific semconv opt-in and pass them through the SDK's
own redaction, so they are a lossy, config-dependent copy. The hook fires even when a call
errors or steering cancels it, so interventions land in the record too.

This module only writes; it decides nothing. It never raises into a run — a broken ledger
must not kill real work — but a write that fails is counted (`drops`/`intact`) rather than
swallowed, because an omitted block leaves a chain that still verifies.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry
from strands.models.model import Model

from .contracts import BlockType, NodeState
from .ledger import Ledger
from .model_record import RecordingModel

# Which node lifecycle state becomes which block. Data, not branches.
_STATE_BLOCKS = {
    NodeState.RUNNING: BlockType.NODE_ATTEMPT,
    NodeState.REDO: BlockType.NODE_ATTEMPT,
    NodeState.COMPLETE: BlockType.VERDICT,
    NodeState.FAILED: BlockType.BREAKER_TRIP,
}

# Tool hooks carry model-controlled input and arbitrary tool output. The durable ledger and
# deep-dive callback receive only this fixed metadata; content, argument names, IDs, status,
# cancellation text, and exception text are intentionally excluded.
_TOOL_CATEGORIES = {
    "read_file": "read_file",
    "list_files": "list_files",
    "write_file": "write_file",
    "recall_lessons": "recall_lessons",
    "query_policy": "query_policy",
}
_KNOWN_NODES = frozenset({"discover", "implement", "verify", "learn"})


def _tool_category(value: Any) -> str:
    return _TOOL_CATEGORIES.get(value, "unknown") if isinstance(value, str) else "unknown"


def _node_category(value: str) -> str:
    return value if value in _KNOWN_NODES else "unknown"


def _tool_event(node: str, event: AfterToolCallEvent) -> dict[str, Any]:
    tool_use = event.tool_use if isinstance(event.tool_use, dict) else {}
    cancelled = event.cancel_message is not None
    failed = event.exception is not None
    completed = not cancelled and not failed
    return {
        "node": _node_category(node),
        "tool": _tool_category(tool_use.get("name")),
        "completed": completed,
        "cancelled": cancelled,
        "error_category": "cancelled" if cancelled else "tool_error" if failed else "none",
    }


class NodeToolRecorder(HookProvider):
    """Records one node's tool calls. Pass to `Agent(hooks=[...])`.

    Node-scoped on purpose: the node name is fixed when this is created, so attribution
    can't drift. A single recorder with a mutable "current node" would be correct only
    while nodes run one at a time, and that is not a property worth depending on.
    """

    def __init__(self, recorder: RunRecorder, node: str) -> None:
        self._recorder = recorder
        self._node = node

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self._on_tool_call)

    def _on_tool_call(self, event: AfterToolCallEvent) -> None:
        payload = _tool_event(self._node, event)
        self._recorder.append(BlockType.TOOL_CALL, payload)
        self._recorder._emit(
            {
                "kind": "tool",
                "status": "completed" if payload["completed"] else payload["error_category"],
                "node": payload["node"],
                "category": payload["tool"],
            }
        )


class RunRecorder:
    """Writes one run's chain. Use `for_node(name)` to get the per-node tool hook, and
    wrap the workflow's status callback with `status_callback()` for node lifecycle."""

    def __init__(
        self, ledger: Ledger, run_id: str, event_callback=None
    ) -> None:
        self._ledger = ledger
        self._run_id = run_id
        self._event_callback = event_callback
        self._git_hash: str | None = None
        self.drops = 0

    def for_node(self, node: str) -> NodeToolRecorder:
        return NodeToolRecorder(self, node)

    def wrap_model(self, inner: Model, node: str, agent: str) -> Model:
        """Wrap a model so its calls are hashed into the chain. One wrapper per agent, so
        the call ordinal is per-agent and survives swarm handoffs."""
        return RecordingModel(
            inner,
            lambda payload: self.append(BlockType.MODEL_CALL, payload),
            node=node,
            agent=agent,
        )

    @property
    def intact(self) -> bool:
        """True when every block this run tried to write actually landed.

        A dropped write leaves no trace in the chain — `seq` comes from the current head,
        so an omitted block is not a gap and the remaining chain still verifies. That makes
        silence indistinguishable from health, so the count is kept here and the caller
        must consult it before trusting the record.
        """
        return self.drops == 0

    def track_git(self, git_hash: str | None) -> None:
        """Blocks written from here on reference this commit."""
        self._git_hash = git_hash

    def append(
        self, block_type: BlockType, payload: dict[str, Any] | None = None
    ) -> None:
        # Recording must never sink a run — but a failed write is counted, never ignored.
        try:
            self._ledger.append_block(
                self._run_id, block_type, payload, git_hash=self._git_hash
            )
        except Exception:  # noqa: BLE001 - a broken ledger must not kill real work
            self.drops += 1

    # ---- seam 1: node lifecycle via the existing status stream -------------------

    def record_status(self, event: dict) -> None:
        state = event.get("state")
        block_type = _STATE_BLOCKS.get(NodeState(state)) if state else None
        if block_type is None:
            return
        self.append(
            block_type,
            {
                "node": str(event.get("node", "unknown")),
                "state": str(state),
                "eval_score": event.get("eval_score"),
            },
        )
        node = event.get("node")
        safe_node = _node_category(node) if isinstance(node, str) else "unknown"
        self._emit(
            {
                "kind": "node",
                "status": str(state),
                "node": safe_node,
                "score": event.get("eval_score"),
            }
        )

    def observe(self, event: dict[str, Any]) -> None:
        self._emit(event)

    def _emit(self, event: dict[str, Any]) -> None:
        if self._event_callback is None:
            return
        with suppress(Exception):  # observability must never affect the run
            self._event_callback({"run_id": self._run_id, **event})

    def status_callback(self, downstream=None):
        """Wrap a caller's status callback so recording is transparent to it."""

        def callback(event: dict) -> None:
            self.record_status(event)
            if downstream is not None:
                downstream(event)

        return callback
