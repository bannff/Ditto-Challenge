# autodev

A self-improving coding agent. Give it a typed ticket and it decides whether the work is
safe to attempt, does it on an isolated branch, proves it by running the target repo's own
test suite, reports what it did, and writes down a lesson that changes how it behaves next
time. If the tests don't pass, nothing ships.

The interesting part isn't the agent — it's what the agent *isn't allowed to do*. The model
gets three tools and no shell. Refusal, test execution, acceptance, rollback and evidence all
belong to the platform, because those are the properties worth being able to prove. A hostile
ticket can say whatever it likes; it still can't reach outside its worktree or talk its way
past a failing test.

Under the hood: a graph of Strands `Swarm` nodes on Bedrock, three model families per node,
an eval checkpoint after every node that can roll back and retry, a circuit breaker when
retries run out, and a hash-chained ledger that decides whether a run is trustworthy enough
to learn from. The node definitions are data — swapping the use case means writing new nodes,
not touching the engine.

`DESIGN.md` is the two-page architecture write-up, including what I deliberately cut and what
is still broken.

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
make setup     # uv sync, then .env from the example
```

Configuration is environment-driven. Set these values in `.env`:

| Variable | Purpose |
|---|---|
| `BEDROCK_MODEL_ID` | Builder model inference-profile ID |
| `BEDROCK_REVIEWER_MODEL_ID` | Reviewer model inference-profile ID |
| `BEDROCK_THIRD_MODEL_ID` | Optional third/adversarial model inference-profile ID |
| `BEDROCK_EMBED_MODEL_ID` | Embeddings model ID for memory |
| `AWS_REGION`, `AWS_PROFILE` | Bedrock region and credential profile |

All LLM calls use Bedrock, and every model comes from the environment — there are no model
IDs in source. The values in `.env.example` are the ones these results were produced with:

| Role | Model |
|---|---|
| Builder | `us.anthropic.claude-haiku-4-5-20251001-v1:0` |
| Reviewer | `us.amazon.nova-2-lite-v1:0` |
| Third voice / adversarial reviewer | `us.anthropic.claude-sonnet-5` |
| Embeddings (memory) | `amazon.titan-embed-text-v2:0` |

Three different model families on purpose: a reviewer that shares the builder's blind spots
agrees with it. Values must be current-generation cross-region inference-profile IDs; bare
model IDs and prior-generation models are rejected. Credentials resolve through the standard
boto3 chain, so any account with Bedrock access to these models works — swap the IDs for
whatever you have enabled.

## CLI core

Run one ticket against a repository:

```bash
uv run autodev run --ticket examples/tickets/bug-1-failing-test.json --repo <target-repo>
```

A ticket is JSON with `id`, `repository`, `request`, `domain`, and `acceptance_command`.
`--repo` overrides `repository`, so seed tickets remain portable. A resolved run commits
only to its isolated branch after the platform-run acceptance gate passes. A failed,
degraded, or refused run does not ship a partial change.

`examples/tickets/` holds nine tickets against two target apps: three bugs, three features, one
broken-access-control case, and two that must be refused. That is more than the brief asks for,
because the interesting cases are the ones that look like each other — a bug report whose wording
reads like an attack, and an attack phrased as an ordinary refactor.

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

## Recovering a run's last checkpointed tree

When a stage passes its evaluator checkpoint its tree is committed to a private
`refs/autodev/checkpoints/<run_id>`, so a failed attempt can be rolled back before the next
one starts. After the run, this reports what is still recoverable:

```bash
uv run autodev recover <run_id> --repo <target-repo>
```

Git's ref names which commit is recoverable; the ledger decides whether it may be used,
supplying the run's seed for an ancestry check and corroborating that the commit is one the
chain recorded. If the two disagree, recovery is refused. Offline — no model calls, no network.

It reports rather than checks the tree out, and prints the git command to do that yourself.
A recovered tree passed an evaluator checkpoint, **not the acceptance gate**, and holds code
the agent wrote from an untrusted ticket, so running its tests executes that code. A run whose
change was reverted has no recoverable checkpoint by design, and only the most recent refs are
kept, so this is a rollback aid rather than an archive.

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
handoffs, iterations, per-node and whole-run wall clock, and the size of what a run may read,
write, or commit. There is no cumulative *token* ceiling — a production version should add
one, since wall clock is a loose proxy for spend.

This is worktree confinement and credential isolation, not an operating-system filesystem
or egress sandbox. Production execution should use a no-egress container or microVM with
scoped credentials.

## Development

```bash
uv run ruff check src tests scripts
uv run pyright
uv run pytest
```

That last command runs the unit tests *and* the deterministic adversarial suite, about three
minutes in total. Both need to be in the default run: the adversarial half is what actually
proves the boundary holds, and it costs nothing to run because it needs no model and no AWS
credentials.

## Adversarial testing

The trust boundary is attacked at three levels, cheapest and most authoritative first.

| Layer | What it proves | Cost | Command |
|---|---|---|---|
| Deterministic | A hostile ticket cannot escape the jail, disable the gate, or leave a dirty tree | Free, keyless, seconds | included in `uv run pytest` |
| Red team | An adaptive LLM attacker cannot talk the swarm past its tool boundary | Real Bedrock, minutes | `uv run python breach/redteam_run.py` |
| Chaos | The loop degrades safely when its own tools fail mid-change | Real Bedrock, minutes | `uv run python breach/chaos_run.py` |

The deterministic layer drives `run_ticket()` end to end with hostile tickets and asserts the
invariants in code rather than asking a model whether they held: nothing written outside the
worktree, `main` untouched, a clean tree after a failed gate, refusal on unsafe input. Several
of its tests are `xfail(strict=True)` — each encodes a property the code does *not* yet hold,
so it turns into a failure the moment the hole closes, which is the signal to delete the marker.

Red team and chaos use the Strands evals SDK (`strands_evals.experimental.redteam` and
`strands_evals.chaos`) against the real Implement swarm, the node that holds the write tools.
They need credentials, so they are scripts rather than tests. Both write their transcript to
`scratch/`. `breach/README.md` has the detail.

`DESIGN.md` explains the control flow, learning policy, and deliberate production-scale
cuts. `demos/README.md` is the reviewer guide for generated artifacts.
