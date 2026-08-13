# autodev

A self-improving coding agent: given a typed ticket, it resolves the work against a
target repo on an isolated worktree, self-verifies by running the target's tests, reports
a structured result, and stores a durable lesson that changes later runs. Built as
use-case-agnostic plumbing (a graph of Strands `Swarm` nodes on Bedrock, with an
eval checkpoint and self-heal after every node) plus a thin swap-in node layer.

See `Reqs.md` for the challenge, `SPEC.md` for the build plan, and
`.kiro/specs/self-improving-coding-agent/` for the requirements/design/tasks.

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
| `BEDROCK_REVIEWER_MODEL_ID` | independent reviewer model for the ensemble |
| `BEDROCK_EMBED_MODEL_ID` | embeddings model (Mem0) |
| `AWS_REGION`, `AWS_PROFILE` | Bedrock region and profile |
| `ADA_ACCOUNT`, `ADA_ROLE`, `ADA_PROVIDER` | used by `scripts/refresh-creds.sh` |

Models must be current-generation Bedrock cross-region inference profiles (bare model IDs
are rejected by Bedrock).

## Development

```bash
uv run ruff check src tests
uv run pyright
uv run pytest
```

## Layout

```
src/self_improving_coding_agent/   # contracts, settings, scrub, taxonomy, ledger, kb, ...
knowledge/                         # taxonomy.yaml + policy docs seeded into the KB
.kiro/steering/                    # project conventions (loaded automatically by Kiro)
.kiro/agents/  .agents/            # the specialist agent team, travels with the repo
.kiro/specs/                       # spec-driven requirements / design / tasks
.github/                           # GitHub control plane: agent-task issue forms + triage/auto-merge workflows
```
