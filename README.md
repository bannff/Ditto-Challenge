# autodev — an ode to Strands

A self-improving coding agent that treats safety as an engineering problem (hence the sub systems), not a prompt.
Give it a ticket and it decides whether the work is safe to attempt, does it on an isolated
branch, proves it against the target's own tests, hands you the evidence, and writes down a
lesson that makes the next run better. If the tests don't pass, nothing ships. Ever.

The workflow is a tour of best practices, and I'll name them as we go:

- **A graph of swarms** [Strands]. Every graph node is a swarm. The graph gives you
  deterministic control — discover → implement → verify → learn, in that order, no agent
  gets to skip the step that checks its work. The swarm gives you elastic, emergent
  intelligence *inside* each node: a shared, automatically managed context window, handoffs,
  consensus. It scales to zero — a one-line fix is often one agent and one turn, because the
  model decides when it's done, not a loop counter. Swarms have inbuilt bounds on their collab (handoff/toolcall/etc).
- **Ensemble Technique** Three *different model families* per swarm, two cheap and
  one strong (frugality). A reviewer that shares the builder's blind spots just
  agrees with it; the third voice is explicitly adversarial — its job is to break the change (Adversarial Self Prompting).
- **Evals as infrastructure** [strands-evals]. An eval harness on every node, judged against
  *that node's* job — not one vibe-check over the whole workflow. Evals govern rollback and
  retry: a failed checkpoint restores the last known-good tree and retries with the diagnosis
  attached. Evals route; they never decide correctness — only the test gate does that.
- **A deterministic circuit breaker.** Retries are finite. When they're spent, the run
  degrades honestly instead of shipping something half-applied.
- **Semantic memory, not an append-only log** [Mem0 + FAISS]. Lessons are stored as real
  vector embeddings and retrieved by *meaning*, not keywords. Tell a relational DB you're
  headed to "the bank" and it can't tell Wells Fargo from a riverbank; semantic search can —
  which is how a lesson learned on one ticket surfaces for a differently-worded cousin.
  Better recall accuracy, fewer hallucinated "memories."
- **A knowledge base for RAG** [ChromaDB]. Policy and domain knowledge live in a vector
  store the agents query by similarity — answers grounded in retrieved fact instead of model
  vibes. Same win as the memory: semantic search beats a traditional DB row-match.
- **A hash-chained ledger** (yes, a mini blockchain — flex or die trying). Every decision block
  links to its predecessor's hash: tamper-evident history, offline replay, state recovery,
  and a provenance gate — memory only accepts a lesson from a run whose chain verifies. Running
  this subsystem on a decentralized device would make it incredibly resilient to tampering.
- **Everything is data** [Pydantic v2], enforced at ingress/egress — most changes are data,
  not code. Node definitions (agents, prompts, tools, evals, bounds) are config; swapping the
  use case means writing new nodes, never touching the engine. Every persisted contract is
  versioned, and the ledger's read path walks a **migration adapter** — one registered
  transform per version step — so a production schema upgrade never breaks in-flight
  workflows. Adding a migration = adding a dict entry, fail-closed on version gaps and on
  payloads from the future. (The v1 registry is empty because v1 is the first schema; the
  machinery is live and tested.)

Model choices live in `.env`, current frugal trio: Haiku 4.5 builds, Nova 2 Lite reviews,
Sonnet 5 plays the adversary. Swap in whatever your account has enabled.

`DESIGN.md` is the two-page architecture write-up, including what I deliberately cut and
what is still broken. I'd start there.

**Reviewers:** a real recorded run of every demo is already committed under `demos/` —
bug resolved, feature resolved, refusal, and the self-improvement before/after — each with
its trace log, report, diff, and hash chain, verifiable offline with
`scripts/verify_demo_artifacts.py` (no credentials needed). Regenerate any of them live
with the commands below.

## What I wish I had more time for

Each of these is bounded by something already in place:

- **A real spend ceiling.** Runs are bounded by handoffs, iterations, and wall clock, but
  there's no cumulative token/dollar cap. The seam already exists (every model call goes
  through one wrapper); production gets a per-run and per-tenant budget there.
- **OS-level sandboxing.** The worktree jail + credential stripping + isolated HOME hold
  the agent; they don't contain hostile *target code* at the kernel level. Production runs
  the gate inside a no-egress microVM. I know the two escapes this class allows — they're
  documented in DESIGN.md, not discovered by you.
- **Signed evidence.** The ledger is tamper-evident, not tamper-proof: whoever can rewrite
  a whole exported bundle can rewrite its recorded head. One KMS signature on the chain
  head closes it.
- **Known holes** A handful of adversarial tests are `xfail(strict=true)` —
  each encodes a hole I found but haven't closed, and the suite *fails* the moment a fix
  lands, so the marker can never go stale.
- **Memory hygiene at scale.** Lessons are scrubbed, deduplicated, and provenance-gated,
  but not repo-scoped or expiry-managed. Needs tenancy for a fleet.

## Two commands

```bash
make setup     # install, and create .env from the example
make demo      # the main demo: bug + feature + refusal on the harder target app
```

Fill in your Bedrock model IDs and AWS profile in `.env` between the two.

`make demo` runs the *app2* suite because that target is hard enough that the swarm actually
collaborates rather than one-shotting the fix. Everything else is one command each,
deliberately not chained — five back-to-back runs invites throttling, and each is more
legible watched on its own:

| Command | Shows | Bedrock |
|---|---|---|
| `make demo` | Bug resolved, feature resolved, ticket refused | yes |
| `make demo-selfimprove` | The before/after: same scenario, empty vs primed memory | yes |
| `make demo-app1` | The same three outcomes on the simpler app | yes |
| `make demo-ledger` | Rollback, recovery, tamper-evidence | **no** |
| `make test` | 700+ tests, including the adversarial suite | **no** |
| `make redteam` / `make chaos` | An LLM attacker, and injected tool failures | yes |

Every target is a one-line wrapper, so `uv run python scripts/demo.py --app app2` works just
as well — but it has to be `uv run` (or an activated venv), not bare `python3`, since the
dependencies live in the uv environment.

No AWS access? The two keyless rows still run, and the committed bundles under `demos/` are
real recorded runs you can verify offline with `scripts/verify_demo_artifacts.py`.

## What to evaluate

Four outcomes, all generated live, all independently checkable:

1. **Bug resolved** — a real patch, a retained branch, and both platform gates green.
2. **Feature resolved** — the same evidence for a feature request.
3. **Refusal** — an unsafe request declined *before any work begins*, with a reason and no
   branch. The refusal path gets the same care as the success path.
4. **Self-improvement** — the same scenario with empty and primed memory. Both pass the
   test gate; only the primed run satisfies a business rule that exists *nowhere in the
   repo* — proof the memory was retrieved and used, not just stored.

Don't take a committed run's word for it: regenerate with the commands above, then verify
the bundle offline.

## Prerequisites

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- AWS credentials with access to Bedrock in the selected account

Configuration is environment-driven — there are no model IDs in source, and no key ever
touches the repo. `make setup` copies `.env.example` to `.env`; set these:

| Variable | Purpose |
|---|---|
| `BEDROCK_MODEL_ID` | Builder model inference-profile ID |
| `BEDROCK_REVIEWER_MODEL_ID` | Reviewer model inference-profile ID |
| `BEDROCK_THIRD_MODEL_ID` | Optional third/adversarial model inference-profile ID |
| `BEDROCK_EMBED_MODEL_ID` | Embeddings model ID for memory |
| `AWS_REGION`, `AWS_PROFILE` | Bedrock region and credential profile |

The values in `.env.example` are the ones these results were produced with:

| Role | Model |
|---|---|
| Builder | `us.anthropic.claude-haiku-4-5-20251001-v1:0` |
| Reviewer | `us.amazon.nova-2-lite-v1:0` |
| Third voice / adversary | `us.anthropic.claude-sonnet-5` |
| Embeddings (memory) | `amazon.titan-embed-text-v2:0` |

Values must be current-generation cross-region inference-profile IDs; bare model IDs and
prior-generation models are rejected. Credentials resolve through the standard boto3 chain.

## CLI core

Run one ticket against a repository:

```bash
uv run autodev run --ticket target_apps/tickets/bug-1-failing-test.json --repo <target-repo>
```

A ticket is JSON with `id`, `repository`, `request`, `domain`, and `acceptance_command` —
and a ticket is *untrusted input written by a stranger*. The system treats it that way
everywhere.

A resolved run commits to its isolated branch only after **two** platform-run gates pass:

1. The ticket's own acceptance check.
2. A **full-suite regression gate** — the platform runs the target's *entire* test suite
   before the change (baseline, at the seed) and after it, and blocks any change that breaks
   a previously-green test. A ticket can narrow its own command to one friendly test file;
   it cannot narrow what the platform checks. Red tests owned by *other* tickets don't block
   an unrelated fix; a test *this* change broke always does.

Every report leads with a plain-English reviewer summary — what happened, which gates ran,
what to look at — before the structured JSON.

`target_apps/tickets/` holds nine tickets against two target apps: three bugs, three
features, one broken-access-control case, and two that must be refused. More than the brief
asks for, because the interesting cases are the ones that look like each other — a bug
report whose wording reads like an attack, and an attack phrased as an ordinary refactor.

```bash
uv run python scripts/demo.py --out demos/generated
uv run python scripts/demo_selfimprove.py --out demos/self-improvement
```

Both need Bedrock. The self-improvement command exits nonzero unless its control/primed
comparison actually proves the claim.

## Evidence bundles and offline verification

`--out` writes one canonical bundle per run — exactly six files:

| File | Evidence |
|---|---|
| `manifest.json` | Run ID, expected outcome, SHA-256 digest and byte count of every artifact |
| `trace.log` | Node lifecycle and evaluation trace |
| `report.json` | Outcome, both gate results, branch, verdicts, reviewer summary, lesson |
| `diff.patch` | The full scrubbed change |
| `chain.json` | Exported hash-chain blocks and recorded head |
| `chain.log` | Human-readable offline chain verification |

Verify any bundle with no model calls, no network, no credentials, no target repo:

```bash
uv run python scripts/verify_demo_artifacts.py demos/generated/<run-dir>
uv run python scripts/verify_demo_artifacts.py --self-improvement demos/self-improvement
```

The verifier rejects missing, symlinked, oversized, malformed, or non-canonical files;
checks manifest digests, cross-file outcome consistency, and the chain. For the
self-improvement root it also demands the `control=false, primed=true` contrast and binds
each run ID to its child bundle. Honest limit: a passing bundle proves *internal
consistency*. The chain is tamper-evident, not tamper-proof — someone who can rewrite the
whole exported directory can rewrite its recorded head too. No unsigned local artifact can
beat that; a production system signs the head.

For a local ledger record, the same offline chain walk:

```bash
uv run autodev replay <run_id>
```

## Recovering a run's last checkpointed tree

Every stage that passes its eval checkpoint gets committed to a private
`refs/autodev/checkpoints/<run_id>` ref — that's what rollback restores between retries.
After a run:

```bash
uv run autodev recover <run_id> --repo <target-repo>
```

Git's ref names the commit; the ledger corroborates it against the chain and the run's seed.
If they disagree, recovery is refused. It *reports* rather than checks out, and tells you
why: a recovered tree passed an eval checkpoint, **not the acceptance gate**, and holds code
written from an untrusted ticket — running its tests executes that code. Your call, made
with eyes open.

## Optional recording

Deterministic re-execution of model decisions, fixture repositories only:

```bash
uv run autodev run --ticket <ticket.json> --repo <fixture-repo> --record <cassette-id>
uv run autodev reexecute <ticket.json> --repo <fixture-repo> --cassette <cassette-id>
```

Off unless `CASSETTE_FIXTURE_ROOT` is configured, refused outside that root. Cassettes hold
unredacted prompts, so they're owner-only and never evidence. Re-execution makes no model
calls but still runs the real gate — and never commits or teaches memory, because recorded
output is not evidence.

## Safety and limits

The boundary is enforced in code, not requested in a prompt: explicit tools only (read,
write, list — no shell), a worktree jail on every path, shell-free allowlisted acceptance
parsing, stripped credentials, an isolated `HOME`, bounded process groups, and hard ceilings
on handoffs, iterations, per-node and whole-run wall clock, and read/write/commit sizes.

Honest limits, stated plainly: there is no cumulative *token* ceiling (wall clock is a loose
proxy for spend — production should add one), and this is worktree confinement plus
credential isolation, not an OS or egress sandbox. Production execution belongs in a
no-egress container or microVM with scoped credentials.

## Adversarial testing

I attacked my own trust boundary at three levels before asking anyone to trust it —
cheapest and most authoritative first:

| Layer | What it proves | Cost | Command |
|---|---|---|---|
| Deterministic | A hostile ticket can't escape the jail, disable the gate, or leave a dirty tree | Free, keyless, seconds | included in `uv run pytest` |
| Red team | An adaptive LLM attacker can't talk the swarm past its tool boundary | Real Bedrock, minutes | `uv run python hardening/redteam_run.py` |
| Chaos | The loop degrades safely when its own tools fail mid-change | Real Bedrock, minutes | `uv run python hardening/chaos_run.py` |

The deterministic layer drives `run_ticket()` end to end with hostile tickets and asserts
the invariants *in code* — nothing written outside the worktree, `main` untouched, a clean
tree after a failed gate — rather than asking a model whether it behaved. Several tests are
`xfail(strict=true)`: each encodes a hole I know about and haven't closed, and it turns into
a hard failure the moment the hole closes. Red team and chaos are the SDK's own primitives
(`strands_evals.experimental.redteam`, `strands_evals.chaos`) pointed at the real Implement
swarm — the node that holds the write tools. `hardening/README.md` has the detail.

## Development

```bash
uv run ruff check src tests scripts
uv run pyright
uv run pytest      # unit tests + the adversarial suite, ~3 minutes, no credentials
```

`DESIGN.md` explains the control flow, learning policy, and deliberate production-scale
cuts. `demos/README.md` is the reviewer guide for generated artifacts.
