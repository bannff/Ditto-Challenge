"""The swap-in layer: node definitions as data.

A NodeConfig fully describes one stage of the workflow — its swarm's agents, the
evaluators that gate it, its skills/steering, and its bounds. The graph engine consumes a
list of these; changing the use case means writing new NodeConfigs, not touching the
engine. Nothing here executes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from strands_evals.evaluators import Evaluator


@dataclass
class AgentSpec:
    name: str
    system_prompt: str
    role: str = "builder"  # selects the model: "builder" or "reviewer" (ensemble)
    tools: list[Any] = field(default_factory=list)


@dataclass
class EvaluatorSpec:
    """Declares an evaluator that runs after a node. The engine injects the shared model at
    build time (evaluator_cls(model=..., **params)); the spec never mints its own.

    gating=False means the evaluator still runs and its findings are recorded and carried
    to the final node, but it cannot fail the node. Per-tool-call judges are diagnostics —
    they answer "was this one call justified", not "did this node succeed" — so averaging
    them into a gate punishes the exploration a hard ticket requires."""

    name: str
    evaluator_cls: type[Evaluator]
    params: dict[str, Any] = field(default_factory=dict)
    threshold: float = 0.7
    gating: bool = True
    # Optional scoping for trace/tool-level judges (e.g. ignore plugin-injected tool
    # calls). The concrete SDK evaluators don't forward this through their __init__, so the
    # engine attaches it after construction.
    trace_extractor: Any = None


@dataclass
class NodeConfig:
    name: str
    agents: list[AgentSpec]
    evaluators: list[EvaluatorSpec] = field(default_factory=list)

    # Node-scoped plugins, shared by every agent in the node (per SPEC 2c).
    # steering_prompt is the natural-language guidance LLMSteeringHandler enforces on tool
    # calls (Proceed / Guide / Interrupt); None means no steering on this node.
    skill_paths: list[Path] = field(default_factory=list)
    steering_prompt: str | None = None
    shared_tools: list[Any] = field(default_factory=list)
    # Optional schema for the node's answer. When set, every agent in the node gets it as
    # its `structured_output_model`, so whichever one ends the swarm returns a parsed object
    # instead of prose — the SDK forces the schema tool only at `end_turn`, so an agent that
    # hands off is unaffected. Node-scoped rather than per-AgentSpec because any agent can
    # be the terminal one: a swarm is free to stop wherever it likes, and in practice this
    # one usually stops at its entry point.
    #
    # This is how a node states its contract instead of asking for it. "Output only the
    # lesson, and stop" was in the critic's prompt and still produced 200 words of
    # thinking-out-loud, because the run never reached the critic.
    #
    # A schema here MUST be unfailable: no min/max, no pattern, no custom validator. A forced
    # structured-output call that fails validation comes back as a tool *error*, not an
    # exception, so the event loop recurses with forced mode still latched and the model is
    # asked for the same rejected value until wall-clock runs out (measured: 312 calls to a
    # RecursionError). Validate at your own write boundary instead, where a bad value is free.
    output_model: type[BaseModel] | None = None
    # Extra agent-plane plugins for every agent in this node. Empty in normal operation;
    # the adversarial harness uses it to attach a fault injector (strands_evals chaos).
    extra_plugins: list[Any] = field(default_factory=list)
    # Agent-plane hook providers (Agent(hooks=...), a different seam from plugins). The
    # run recorder rides here so tool calls reach the ledger without the engine knowing
    # a ledger exists.
    hooks: list[Any] = field(default_factory=list)
    # Optional (inner_model, node_name, agent_name) -> Model. Wraps each agent's model so its
    # calls can be recorded; a model request is only visible at the model seam, never from a
    # hook. None means the model is used as-is.
    model_wrapper: Any = None

    # Circuit breaker. execution_timeout bounds one swarm attempt (wall-clock);
    # node_timeout bounds a single agent step within the swarm; max_redos bounds how many
    # times the self-heal loop re-runs the whole node after a failed checkpoint before the
    # circuit breaker degrades the run.
    max_handoffs: int = 12
    max_iterations: int = 12
    execution_timeout: float = 600.0
    node_timeout: float = 300.0
    max_redos: int = 2

    # Ping-pong damper (SDK-native, off by default upstream — 0/0 disables). Trips the
    # swarm when the last `window` turns involve fewer than `min_unique` distinct agents;
    # two agents politely bouncing is pure token burn since every handoff target starts
    # state-reset and re-reads its context.
    repetitive_handoff_detection_window: int = 8
    repetitive_handoff_min_unique_agents: int = 3
