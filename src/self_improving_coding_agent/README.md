# autodev package

This package implements autodev’s ticket-resolution platform. A run accepts a typed ticket, refuses unsafe or underspecified work before creating a worktree, runs the bounded Strands workflow in an isolated branch, applies the deterministic acceptance gate, records an auditable result, and stores a reusable lesson.

## Main areas

- **Workflow and agent loop:** `workflow.py`, `graph.py`, `node.py`, `nodes.py`, `agent_plane.py`, `checkpoint.py`, `fallback.py`, and `eval_scope.py` assemble configurable Strands swarm stages, evaluation checkpoints, retries, and circuit breakers.
- **Trust boundary:** `refusal.py`, `acceptance_policy.py`, `worktree.py`, and `tools.py` enforce deterministic refusal, restricted test commands, worktree confinement, and jailed file operations.
- **Contracts and configuration:** `contracts.py`, `settings.py`, `taxonomy.py`, and `scrub.py` define validated data, environment-backed configuration, domain rules, and redaction.
- **Durable context:** `ledger.py`, `recorder.py`, `memory.py`, and `kb.py` provide run records, tool/node capture, lesson memory, and policy retrieval.
- **Interfaces and observability:** `cli.py`, `server.py`, and `telemetry.py` expose the CLI/MCP front doors and workflow tracing.

`workflow.run_ticket()` is the main orchestration seam. The CLI and MCP server both call it so refusal, isolation, test-gating, reporting, and learning behavior stay consistent across entry points.
