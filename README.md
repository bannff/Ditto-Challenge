# autodev

`autodev` is a CLI coding agent for typed tickets. It works in an isolated Git worktree,
classifies unsafe or insufficient requests as refusals, applies a change only after the
target repository's acceptance command passes, and reports the outcome. Its review surface
is the CLI and the evidence it produces.

## What to evaluate

Generate and inspect four outcomes:

1. **Bug resolved** — a real patch, a retained branch, and a passed acceptance gate.
2. **Feature resolved** — the same evidence for a feature request.
3. **Refusal** — an unsafe request declined before work begins, with a reason and no branch.
4. **Self-improvement** — the same scenario run with empty and primed memory; both pass the
   acceptance gate, while only the primed run satisfies the memory-only check.

Do not treat a successful live run as pre-existing evidence. Generate it with the commands
below, then verify the resulting bundle offline.

## Prerequisites

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- AWS credentials with access to Bedrock in the selected account

```bash
uv sync
cp .env.example .env
```

Configuration is environment-driven. Set these values in `.env`:

| Variable | Purpose |
|---|---|
| `BEDROCK_MODEL_ID` | Builder model inference-profile ID |
| `BEDROCK_REVIEWER_MODEL_ID` | Reviewer model inference-profile ID |
| `BEDROCK_THIRD_MODEL_ID` | Optional third/adversarial model inference-profile ID |
| `BEDROCK_EMBED_MODEL_ID` | Embeddings model ID for memory |
| `AWS_REGION`, `AWS_PROFILE` | Bedrock region and credential profile |

All LLM calls use Bedrock. Model values must be current-generation Bedrock cross-region
inference-profile IDs supplied by the environment; bare model IDs and prior-generation
models are not supported.

## CLI core

Run one ticket against a repository:

```bash
uv run autodev run --ticket examples/tickets/bug-1-failing-test.json --repo <target-repo>
```

A ticket is JSON with `id`, `repository`, `request`, `domain`, and `acceptance_command`.
`--repo` overrides `repository`, so seed tickets remain portable. A resolved run commits
only to its isolated branch after the platform-run acceptance gate passes. A failed,
degraded, or refused run does not ship a partial change.

The included target apps and tickets provide the required bug, feature, and refusal cases:

```bash
uv run python scripts/demo.py --out demos/generated
```

Generate the self-improvement comparison separately:

```bash
uv run python scripts/demo_selfimprove.py --out demos/self-improvement
```

Both commands need configured Bedrock access. The self-improvement command exits nonzero
unless its control/primed comparison meets its stated evidence conditions.

## Evidence bundles and offline verification

`--out` writes one canonical bundle per run. Each individual run directory contains exactly these six files:

| File | Evidence |
|---|---|
| `manifest.json` | Run ID, expected outcome, and SHA-256 digest and byte count for every other artifact |
| `trace.log` | Node lifecycle and evaluation trace |
| `report.json` | Structured outcome, test-gate result, branch, verdicts, and lesson |
| `diff.patch` | Full scrubbed change evidence from the report |
| `chain.json` | Exported hash-chain blocks and recorded head |
| `chain.log` | Human-readable offline chain verification output |

Verify an individual bundle without model calls, network access, credentials, or the target
repository:

```bash
uv run python scripts/verify_demo_artifacts.py demos/generated/bug-1-failing-test
uv run python scripts/verify_demo_artifacts.py demos/generated/refuse-unsafe
```

A self-improvement evidence root instead contains `contrast.json` and `control/` and
`primed/` child bundles. Verify the complete comparison with:

```bash
uv run python scripts/verify_demo_artifacts.py --self-improvement demos/self-improvement
```

Offline verification rejects missing, symlinked, oversized, malformed, or non-canonical
files; checks manifest digests, cross-file outcome consistency, and the exported hash chain.
For a self-improvement root, it also validates the required `control=false`, `primed=true`
contrast and binds each contrast run ID to its child bundle. A passing bundle establishes only
internal consistency. Its manifest and recorded chain head are self-contained and unsigned, so
an attacker who can modify the exported directory can replace them with the artifacts. It does
not provide tamper evidence or post-export tamper detection against that attacker, or
authenticated origin.

For a local ledger record, this CLI command performs the same offline chain walk:

```bash
uv run autodev replay <run_id>
```

## Optional recording

For deterministic re-execution of model decisions against fixture repositories only:

```bash
uv run autodev run --ticket <ticket.json> --repo <fixture-repo> --record <cassette-id>
uv run autodev reexecute <ticket.json> --repo <fixture-repo> --cassette <cassette-id>
```

Recording is off unless `CASSETTE_FIXTURE_ROOT` is configured, and it is refused outside
that root. Cassettes contain unredacted prompts and are owner-only; they are not evidence
bundles and should not be used for arbitrary repositories. Re-execution makes no model
calls, but still runs the acceptance gate and never commits or teaches memory.

## Safety and limits

Ticket text and repository content are untrusted. The enforced boundary uses explicit tools,
a worktree jail, shell-free acceptance-command parsing, stripped credentials, an isolated
`HOME`, and bounded process groups. Only the platform's green acceptance gate can resolve a
change; all other outcomes revert and leave the working tree clean. Runs are bounded by
handoffs, iterations, timeouts, and token budgets.

This is worktree confinement and credential isolation, not an operating-system filesystem
or egress sandbox. Production execution should use a no-egress container or microVM with
scoped credentials.

## Development

```bash
uv run ruff check src tests scripts
uv run pyright
uv run pytest
```

`DESIGN.md` explains the control flow, learning policy, and deliberate production-scale
cuts. `demos/README.md` is the reviewer guide for generated artifacts.
