# Strands SDK — working notes for this repo

Verified against the installed versions on 2026-08-13:

- strands-agents 1.52.0
- strands-agents-tools 0.8.6
- strands-agents-evals 1.1.1
- mem0ai 2.0.18, chromadb 1.5.9

Everything below has been import-checked against those versions. Treat it as the
source of truth for how this project wires Strands together.

## Models — Bedrock only, inference profiles only

Bare model IDs (`anthropic.claude-...`) throw `ValidationException`. Use a cross-region
inference profile ID and pull it from the environment, never a literal in source:

```python
import os
from strands.models import BedrockModel

model = BedrockModel(model_id=os.environ["BEDROCK_MODEL_ID"], region_name=os.environ["AWS_REGION"])
```

Use current-generation models only. Confirmed callable in the build account (us-east-1):

- `us.anthropic.claude-haiku-4-5-20251001-v1:0` — default builder, fast/cheap
- `us.amazon.nova-2-lite-v1:0` — good independent reviewer for the ensemble (different family)
- current Claude Sonnet/Opus profiles for heavier reasoning nodes

Do not use `claude-3-*`, `nova-*-v1:0`, or other prior-gen IDs.

For the ensemble node, build two `BedrockModel` instances from different families
(e.g. haiku-4-5 builder + nova-2-lite reviewer) so disagreement is real signal.

## Agents carry plugins — never a bare Agent

Plugins are agent-level and attach to `Agent`:

```python
from strands.vended_plugins.skills import AgentSkills
from strands.vended_plugins.steering import LLMSteeringHandler
```

- `AgentSkills` — modular skill files the agent activates on demand.
- `LLMSteeringHandler` — runtime tool-call interceptor (Proceed / Guide / Interrupt),
  a live guardrail on tool use, not a system-prompt string.

`Swarm(plugins=...)` is a separate `MultiAgentPlugin` plane and is hooks-only. Do not
put Skills or Steering there. Scope one skill set + one steering config per node and
share it across all of that node's swarm agents.

## Graph of swarms

```python
from strands.multiagent import GraphBuilder, Swarm
```

Each node is a `Swarm`. Set the bounds explicitly per node — they are the circuit
breaker, not just tuning:

```python
# nodes is the first positional arg (a list[Agent]) — NOT a keyword `agents=`.
Swarm([...], entry_point=..., max_handoffs=..., max_iterations=...,
      execution_timeout=..., node_timeout=..., session_manager=...)
```

Verified defaults: `max_handoffs=20`, `max_iterations=20`, `execution_timeout=900.0`,
`node_timeout=300.0`. Set `node_timeout` explicitly too — it bounds a single agent step.
`session_manager=` goes on the Swarm (agents must NOT carry their own). Tune the bounds up
for nodes doing real multi-step work (default 6/6 caused false `INCONCLUSIVE` in the prior
build).

## Evals — LLM-as-judge checkpoints

```python
from strands_evals import evaluators
```

Confirmed classes: `OutputEvaluator`, `CorrectnessEvaluator`, `FaithfulnessEvaluator`,
`HelpfulnessEvaluator`, `ToolSelectionAccuracyEvaluator`, `ToolParameterAccuracyEvaluator`,
`TrajectoryEvaluator`, `GoalSuccessRateEvaluator`, plus Coherence/Conciseness/Refusal/etc.

- `OutputEvaluator` (OUTPUT_LEVEL) takes plain `actual_output` text — no Session needed.
  Use it for the first fast checkpoint before the trace wiring is ready.
- Trace/session-level evaluators (Correctness, ToolParameterAccuracy, Trajectory,
  GoalSuccessRate, ...) need a `Session` built from OTEL spans.

## Session wiring powers both trace evaluators and detectors

```python
from strands_evals.mappers import StrandsInMemorySessionMapper
from strands_evals import detectors  # detect_failures, analyze_root_cause, diagnose_session
```

Build the Session once from captured spans; it feeds both the trace-level evaluators and
the detectors. Run detectors only on a failed checkpoint (`DiagnosisTrigger.ON_FAILURE`).
The detector's fix recommendation is what the self-heal redo pass gets told, and what the
final node distills into a lesson.

## Telemetry

```python
from strands.telemetry import StrandsTelemetry
StrandsTelemetry().setup_console_exporter()   # spans to stdout, zero extra deps
```

`setup_otlp_exporter()` is a one-line swap for a real collector, gated behind the
`strands-agents[otel]` extra. The console pipe is for observability + eval input; the UI
live feed is a separate purpose-built channel, do not conflate them.

## Tools

```python
from strands_tools import shell, file_read, file_write, editor, mem0_memory
```

Gotchas:
- `shell` needs `BYPASS_TOOL_CONSENT=true` in the environment.
- `strands_tools.mem0_memory` hard-imports `opensearchpy` at module load. Install
  `opensearch-py` even when using the FAISS backend, or the import fails.
- Don't hand-roll tools `strands_tools` already provides.

## Mem0 on Bedrock

Route Mem0's LLM (distillation) and embedder through Bedrock via boto3 — same account and
credentials as everything else, no second API key:

```python
config = {
    "llm": {"provider": "aws_bedrock", "config": {"model": os.environ["BEDROCK_MODEL_ID"]}},
    "embedder": {"provider": "aws_bedrock", "config": {"model": os.environ["BEDROCK_EMBED_MODEL_ID"]}},
    "vector_store": {"provider": "faiss", "config": {"path": "..."}},
}
```

Vectors live locally in FAISS on disk — only the LLM/embedding calls go to Bedrock.

## Checkpointing

```python
from strands.session import FileSessionManager
```

Pass `session_manager=` into swarm construction to persist session id + conversation
state for resume.

## Environment

- AWS creds refresh: `./scripts/refresh-creds.sh` (reads `ADA_ACCOUNT`/`ADA_ROLE`/
  `ADA_PROVIDER`/`AWS_PROFILE` from the gitignored `.env` — no account IDs in source).
- The shell env may default `AWS_PROFILE` to something else — the script and app read it
  from `.env`; export `AWS_PROFILE=default` if running commands by hand.
- The `default` profile has no region; `AWS_REGION` comes from `.env` (us-east-1 here).
- Run everything through `uv run` so it uses the pinned 3.12 venv.
