# DESIGN — autodev

`autodev` resolves one typed ticket against one repository. The design bet: put the
*intelligence* in swarms and the *authority* in deterministic subsystems, and never let the
two trade places. Seven subsystems, each with one job:

- **A pre-gate.** Deterministic, grammar-based triage into bug / feature / refuse before a
  worktree exists or a token is spent. *Why:* refusal is a first-class answer, not a failure
  mode — an unsafe ticket should cost nothing, and the cheapest decision is the one made
  before you start.
- **A graph for control** [Strands]. Discover → implement → verify → learn, in that order,
  owned by the platform. *Why:* the sequence is a property of the work, not a judgement
  call. An agent that can reorder its own stages can skip the one that verifies it.
- **A swarm for emergent intelligence, inside every node.** *Why:* effort tracks the repository instead of the config, and it scales to zero — because the model decides the task is done rather than a loop counter deciding for it. A cross-module change keeps handing off across a shared, automatically managed context window until it converges or hits its bound. Three agents, three *different model families* — an ensemble, not an echo — and it's two cheap models plus one strong one, so the expensive model only weighs in where it earns its cost. The third voice is adversarial by role: its job is to break the change before the gate does.
- **An eval harness on every node** [strands-evals], judged against *that* node's job — discovery on faithfulness to the repo, implementation on goal success against explicit criteria. *Why:* a bad step gets caught at the step. A failed checkpoint rolls the tree back and retries with the diagnosis attached, instead of a wrong turn compounding for three more stages. Long traces can't crash their own judge: session input is bounded, and
the trajectory diagnostic chunks itself [strands-evals detectors].
- **A deterministic circuit breaker.** Retries are finite; exhausting them degrades the run.
  *Why:* failure has to be bounded and honest — the alternative to stopping is shipping
  something half-applied, which is worse than shipping nothing.
- **A hash-chained ledger underneath.** Every block links to its predecessor's hash: the run
  is reconstructable, tamper-evident, and replayable from recorded model-call digests.
  *Why:* it makes a run *provable enough to learn from*. Memory accepts a lesson only from a
  run whose chain verifies — junk resistance, enforced cryptographically rather than hoped.
- **Strands for all of it.** Multi-agent orchestration, evals, skills, steering, sessions —
  the SDK, on Bedrock, models supplied by the environment. *Why:* every hand-rolled agent
  loop is code someone has to audit and nobody else has tested.
- **Contracts are versioned data** [Pydantic v2]. Every persisted contract carries
  `schema_version`, and the ledger's read path walks a migration adapter before validating —
  one registered transform per version step, fail-closed on gaps and on payloads from the
  future. *Why:* in production a schema upgrade must not break workflows already in flight,
  and the fix should be data (register a transform), not surgery on load paths. The v1
  registry is empty because v1 is the first schema; the chain-walk is live and tested.

## The loop

```text
  ticket (untrusted)
        │
        ▼
  deterministic preflight ─────────────────► refuse + report, no worktree ever created
        │
        ▼
  isolated git worktree, own branch autodev/<run_id>
        │
        ▼
 ┌───────────────────────────────────────────────────────────────────────────┐
 │ STRANDS GRAPH — every node is a 3-agent swarm across 3 model families     │
 │                                                                           │
 │      discover ────────► implement ────────► verify ────────► learn        │
 │          │                  │                  │               │          │
 │          └────────┬─────────┴─────────┬────────┘               │          │
 │                   ▼                   ▼                        ▼          │
 │      ┌──────────────────────────────────────────────────────────────┐     │
 │      │ EVALS after every node  (strands_evals)                      │     │
 │      │   gate:  goal success · faithfulness · relevance             │     │
 │      │   info:  tool choice · tool params · trajectory detector     │     │
 │      └──────────────────────────────────────────────────────────────┘     │
 │                   │                                                       │
 │        pass ──────┴─► checkpoint the tree ─► next node                    │
 │        fail ────────► RESTORE last checkpoint, retry with the diagnosis   │
 │        out of retries ─► CIRCUIT BREAKER ─► degraded, nothing ships       │
 └───────────────────────────────────┬───────────────────────────────────────┘
                                     ▼
        TWO ACCEPTANCE GATES, run by the platform, allowlisted and shell-free
          1. the ticket's own check        2. full-suite regression vs seed
                                     │
              ┌──────────────────────┴──────────────────────┐
        both green                                   anything else
   squash to one commit on                    reset to seed, clean tree,
   autodev/<run_id>, ship it                  drop checkpoint ref, ship nothing
              └──────────────────────┬──────────────────────┘
                                     ▼
 ┌───────────────────────────────────────────────────────────────────────────┐
 │ HASH-CHAINED LEDGER — every block links to the previous one's hash        │
 │   run start · node attempts · eval verdicts · gate results · model digests│
 │                                                                           │
 │   report.json · diff.patch · chain.json      offline-verifiable bundle    │
 │                    │                                                      │
 │                    └─► provenance gate ─► memory writes the lesson        │
 │                        (chain verifies + all blocks landed, not degraded) │
 └───────────────────────────────────────────────────────────────────────────┘
```

Two details where the obvious alternative is wrong. **The retry restores before it
retries** — otherwise attempt N+1 diagnoses attempt N's half-applied edits. Checkpoints sit
on private `refs/autodev/checkpoints/<run_id>` refs, never branches: recoverable state, not
something anyone merges by accident. And evals route retries but never decide
correctness. Only the test gate resolves a ticket. The per-tool-call judges are
deliberately informational: averaging ~200 of them once failed a node whose fix the
deterministic gate had already verified.

## Safety model

Ticket text and repository content are untrusted, and no prompt is asked to make them safe.

The model has read, write and list — no shell. Every path resolves through the worktree
jail; traversal, anything outside it, and git metadata are refused at the tool boundary, and
reads and writes are size-bounded.

The acceptance command is untrusted too. It is split without a shell and checked token by
token against a fail-closed policy: `pytest` only, a narrow flag set, paths inside the jail.
pytest's rootdir, config and collection scope are pinned to what the *target committed*, so
an agent-authored config cannot make a red suite look green by changing what gets collected.
The run gets `shell=False`, stripped credentials, an isolated `HOME`, no user site packages,
a killed process group and a timeout, and its output is append-only while captured — a test
cannot rewrite what it already printed.

And because a ticket's command may be narrowed to one test file, it never speaks for the
rest of the suite: the platform runs the target's *whole* suite through the same hardened
path at the seed (baseline) and after the change, and resolution requires no new failure
between the two. Red tests owned by other tickets don't block an unrelated fix; a test this
change broke always does.

The gates are the only thing that can resolve a ticket. Every other outcome reverts and
leaves the tree clean. An honest failure still teaches; a run cut short does not.

## Memory: write policy, read policy, and proof

Learn *proposes* one structured rule [Pydantic v2 schema, not prose] and holds no write
tool. The platform stores it only after bounding and scrubbing it, rejecting
near-duplicates, verifying the ledger chain, and confirming the run was neither degraded nor
replayed — that's the write policy, and it's what keeps the memory junk-resistant. The read
policy: Discover retrieves up to three relevant lessons by semantic similarity to the
ticket, and they're injected into both the planner and the builder.

Both stores are *semantic*, not relational: lessons live as vector embeddings [Mem0 + FAISS]
and policy knowledge in a RAG store [ChromaDB], so retrieval matches meaning rather than
keywords — a lesson learned on one ticket surfaces for a differently-worded cousin, and a
"bank" query knows a riverbank from Wells Fargo. That's what makes the memory an actual
behavior-changer instead of an append-only log.

The demo proves *retrieval changed behavior*, not just that storage happened: a control run
with no relevant lesson, a primed run given a rule that exists nowhere in the target repo,
both through the same gates — and a hidden check that only the primed run can satisfy.

## How this scales to production

- **Real repos:** the worktree jail, config pinning, and regression gate are
  repo-size-agnostic; what changes is the runner policy (more than pytest) and clone/fetch
  instead of local paths.
- **Deploy / verify / rollback:** ship = merge the gated branch behind a feature flag;
  verify = canary metrics as a second deterministic gate; rollback = the same revert
  machinery the loop already uses, pointed at the deploy.
- **Concurrency:** runs are already isolated by worktree and run ID; a queue in front and a
  per-repo lock on finalize is the remaining work.
- **Cost control:** the missing piece is a cumulative token/dollar ceiling per run and per
  tenant, enforced at the model wrapper (the seam already exists for recording).
- **Stopping a bad change:** nothing reaches prod from this system without both gates green,
  a human-reviewable branch, and a verifiable evidence bundle — the same artifacts a CI
  reviewer would demand.

## What I cut, and what is still true

I optimised for one enforceable local loop over broad repository support.

- **Not a sandbox.** Worktree confinement and credential stripping are not OS-level
  containment; production wants a no-egress container and scoped credentials. Two precise
  consequences: a hostile repo's *committed* git config can still reach `/bin/sh` via
  `filter.*.clean`, and a repo's `conftest.py` can turn a red suite green in one line.
- **Pytest only.** A wider runner policy is a wider shell surface.
- **The pre-gate is not language understanding.** Tuned for precision, because a false
  refusal exits zero and looks correct. The controls after it are what hold.
- **No spend ceiling.** Handoffs, iterations, wall clock and sizes are bounded; cumulative
  tokens are not.
- **Recovery reports a tree; it does not resume a run** — checking one out stays an operator
  decision, since it may hold code written from an untrusted ticket.
- **No control plane, local memory only.** No deploy, CI, auth, UI or scheduler.

Those cuts keep the graded behaviour narrow on purpose: refuse unsafe work, change code in
isolation, pass gates the platform runs before keeping anything, leave evidence someone can
check, and let a verified observation change a later run.
