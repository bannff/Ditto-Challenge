# autodev

A self-improving coding agent: given a typed ticket, it resolves the work against a
target repo on an isolated worktree, self-verifies by running the target's tests, reports
a structured result, and stores a durable lesson that changes later runs. Built as
use-case-agnostic plumbing (a graph of Strands `Swarm` nodes on Bedrock, with an
eval checkpoint and self-heal after every node) plus a thin swap-in node layer.

See `DESIGN.md` for the architecture and `.kiro/specs/self-improving-coding-agent/`
for the requirements/design/tasks.

## Prerequisites

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- AWS credentials with Bedrock access in the target account

## Setup

```bash
uv sync                       # install into a pinned 3.12 venv
cp .env.example .env          # then edit values as needed
./scripts/refresh-creds.sh    # refresh AWS creds via ada (reads ADA_* from .env)
```

Configuration is env-driven (`pydantic-settings`); nothing sensitive is hardcoded. The
only values you set are in `.env`:

| Variable | Purpose |
|---|---|
| `BEDROCK_MODEL_ID` | builder model (inference-profile ID) |
| `BEDROCK_REVIEWER_MODEL_ID` | independent reviewer model in the swarm trio |
| `BEDROCK_THIRD_MODEL_ID` | third model in the swarm trio (adversarial voice); optional |
| `BEDROCK_EMBED_MODEL_ID` | embeddings model (Mem0) |
| `AWS_REGION`, `AWS_PROFILE` | Bedrock region and profile |
| `ADA_ACCOUNT`, `ADA_ROLE`, `ADA_PROVIDER` | used by `scripts/refresh-creds.sh` |

Models must be current-generation Bedrock cross-region inference profiles (bare model IDs
are rejected by Bedrock).

## Usage

Resolve a ticket against a target repo:

```bash
uv run autodev run --ticket <ticket.json> --repo <path-to-repo>
```

Run the demo against the bundled target app (materializes it into a scratch git repo
first, since autodev only ever works on an isolated worktree). With no argument it walks a
bug, a feature, and a refusal, printing a live node-lifecycle trace (each node gated by its
eval score) and a RunReport summary for each:

```bash
uv run python scripts/demo.py                                  # bug -> feature -> refuse
uv run python scripts/demo.py examples/tickets/bug-1-failing-test.json  # a single ticket
uv run python scripts/demo.py --out demos/latest               # also save inspectable bundles
```

With `--out DIR`, each ticket also drops a bundle at `DIR/<ticket_id>/` — `trace.log` (the
node trace), `report.json` (the full RunReport), and `diff.patch` (the change it produced) —
so a judge can inspect a run without executing anything. Every file is scrubbed on the way out.

See the self-improvement before/after — the same ticket resolved with empty memory vs
with a prior lesson primed, where memory supplies a rule that isn't in the code:

```bash
uv run python scripts/demo_selfimprove.py
```

Expose the same workflow over MCP (stdio) — a `run_ticket` tool, taxonomy/recent-reports
resources, and a "file a ticket" prompt:

```bash
uv run autodev-mcp
```

Audit a finished run offline — verify its hash chain and walk its decisions with no model
calls, no network, and no repo access:

```bash
uv run autodev replay <run_id>
```

It recomputes every block hash, names the exact block if the chain was altered or truncated,
and reports whether the run's record was trustworthy enough to teach memory anything. Exits
nonzero on a broken chain.

A ticket is JSON: `{ "id", "repository", "request", "domain", "acceptance_command" }`.
`--repo` overrides `repository` so tickets are portable. `examples/tickets/` has samples —
two bugs, two features, and one the agent should refuse.

## Development

```bash
uv run ruff check src tests scripts
uv run pyright
uv run pytest
```

The same three gates run in CI on every push and PR. The unit suite is keyless, so CI
needs no AWS credentials — only the live demo does.

## Layout

```
src/self_improving_coding_agent/   # contracts, settings, scrub, taxonomy, ledger, kb, ...
knowledge/                         # taxonomy.yaml + policy docs seeded into the KB
examples/                          # target app (inventory lib) + seed tickets for the demo
scripts/                           # refresh-creds.sh, demo.py
.kiro/steering/                    # project conventions (loaded automatically by Kiro)
.kiro/agents/  .agents/            # the specialist agent team, travels with the repo
.kiro/specs/                       # spec-driven requirements / design / tasks
.github/                           # CI (ruff/pyright/pytest) + agent-task issue forms, triage, auto-merge
```
