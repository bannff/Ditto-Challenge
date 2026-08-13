---
inclusion: always
---

# Strands conventions

This project is built on the Strands Agents SDK (1.52.0) with Bedrock as the only LLM
backend. The verified, import-checked details — model IDs, plugin imports, swarm/graph
wiring, evals, telemetry, Mem0-on-Bedrock config, and known gotchas — live in one place:

#[[file:.agents/strands-knowledge.md]]

Follow that file. The essentials that apply to every change:

- Bedrock only, current-generation inference-profile model IDs, pulled from env vars.
  Never bare model IDs, never prior-gen models.
- No bare `Agent(system_prompt=...)` — agents carry `AgentSkills` + `LLMSteeringHandler`
  at the agent plane. Scope one skill/steering set per node.
- Swarm bounds (`max_handoffs`, `max_iterations`, `execution_timeout`) are the circuit
  breaker; set them explicitly per node.
- Don't hand-roll what `strands_tools` / `strands_evals` already provide.
