# Test suite

The keyless test suite verifies autodev’s application behavior without requiring Bedrock credentials. It complements the live demo scripts by testing deterministic contracts and mocked or stubbed workflow boundaries.

Coverage includes:

- contracts, taxonomy, redaction, settings-adjacent behavior, ledger persistence, memory, and policy retrieval;
- Strands agent construction, node configuration, graph checkpoints, retries, fallback behavior, telemetry, and recorder hooks;
- deterministic refusal, acceptance-command validation, worktree confinement, isolated branches, cleanup, and test-gate outcomes;
- CLI and MCP entry points, demo helpers, and reference-node behavior.

Run the suite with:

```bash
uv run pytest
```

The quality gates for this repository are:

```bash
uv run ruff check src tests scripts
uv run pyright
uv run pytest
```

Live Bedrock-backed behavior is demonstrated by `scripts/demo.py` and `scripts/demo_selfimprove.py`, not required for this suite.
