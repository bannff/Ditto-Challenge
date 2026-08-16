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

import hashlib
from typing import Any

from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry

from .contracts import BlockType, NodeState
from .ledger import Ledger

# Which node lifecycle state becomes which block. Data, not branches.
_STATE_BLOCKS = {
    NodeState.RUNNING: BlockType.NODE_ATTEMPT,
    NodeState.REDO: BlockType.NODE_ATTEMPT,
    NodeState.COMPLETE: BlockType.VERDICT,
    NodeState.FAILED: BlockType.BREAKER_TRIP,
}

# How each tool's arguments are recorded. "verbatim" keeps the value; "digest" keeps only
# its size and a short hash. File *content* is digested on purpose: it is arbitrary target
# repo text, and a durable audit row that `replay` prints is the wrong place for it — a
# repo secret that no scrub pattern happens to match would land there. Size plus digest
# still answers the audit questions (which file, how big, did it change between attempts).
# An unlisted tool records its argument names only.
_RECORDED_ARGS: dict[str, dict[str, str]] = {
    "read_file": {"path": "verbatim"},
    "list_files": {"path": "verbatim"},
    "write_file": {"path": "verbatim", "content": "digest"},
    "recall_lessons": {"query": "verbatim"},
    "query_policy": {"query": "verbatim"},
}


def _digest(value: Any) -> str:
    text = value if isinstance(value, str) else str(value)
    return f"{len(text)} chars, sha256:{hashlib.sha256(text.encode()).hexdigest()[:12]}"


def _record_args(tool_name: str | None, args: Any) -> dict[str, Any]:
    if not isinstance(args, dict):
        return {}
    policy = _RECORDED_ARGS.get(tool_name or "")
    if policy is None:
        return {"arg_names": sorted(str(k) for k in args)}
    return {
        key: (_digest(value) if policy.get(key) == "digest" else value)
        for key, value in args.items()
        if key in policy
    }


def _tool_status(result: Any) -> str | None:
    if isinstance(result, dict):
        status = result.get("status")
        return str(status) if status is not None else None
    return None


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
        tool_use = event.tool_use or {}
        self._recorder.append(
            BlockType.TOOL_CALL,
            {
                "node": self._node,
                "tool": tool_use.get("name"),
                "tool_use_id": tool_use.get("toolUseId"),
                "args": _record_args(tool_use.get("name"), tool_use.get("input")),
                "status": _tool_status(event.result),
                "cancelled": event.cancel_message,
                "error": str(event.exception) if event.exception is not None else None,
            },
        )


class RunRecorder:
    """Writes one run's chain. Use `for_node(name)` to get the per-node tool hook, and
    wrap the workflow's status callback with `status_callback()` for node lifecycle."""

    def __init__(self, ledger: Ledger, run_id: str) -> None:
        self._ledger = ledger
        self._run_id = run_id
        self._git_hash: str | None = None
        self.drops = 0

    def for_node(self, node: str) -> NodeToolRecorder:
        return NodeToolRecorder(self, node)

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

    def status_callback(self, downstream=None):
        """Wrap a caller's status callback so recording is transparent to it."""

        def callback(event: dict) -> None:
            self.record_status(event)
            if downstream is not None:
                downstream(event)

        return callback
