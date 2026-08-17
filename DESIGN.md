# DESIGN — autodev

`autodev` resolves one typed ticket against one repository. Seven subsystems, each with one job:

- **A pre-gate.** Deterministic, grammar-based triage into bug / feature / refuse before a
  worktree exists or a token is spent. *Why:* refusal should be a first-class answer, not a
  failure mode — an unsafe ticket ought to cost nothing, and the cheapest decision is the one
  made before you start.
- **A graph for control.** Discover → implement → verify → learn, in that order, owned by the
  platform. *Why:* the sequence is a property of the work, not a judgement call. An agent that
  can reorder its own stages can skip the one that verifies, and a fixed spine is what makes a
  run reproducible and auditable.
- **A swarm for intelligence, inside every node.** *Why:* effort tracks the repository instead
  of the config, and it scales to zero — a one-line off-by-one is often one agent and one turn,
  because the model decides the task is done rather than a loop counter deciding for it. A
  change spanning modules keeps handing off across a shared, automatically managed context
  window until it converges or hits its bound. The three agents are three *different model
  families*, so the review is an ensemble rather than an echo — and it is two cheap models plus
  one strong one, so the expensive model only weighs in where it earns its cost.
- **An eval harness on every node,** judged against *that* node's job — discovery on
  faithfulness to the repo, implementation on goal success. *Why:* a bad step gets caught at the
  step. A failed checkpoint rolls the tree back and retries with the diagnosis attached, instead
  of a wrong turn compounding for three more stages.
- **A deterministic circuit breaker.** Retries are finite; exhausting them ends the run as
  degraded. *Why:* failure has to be bounded and honest — the alternative to stopping is
  shipping something half-applied, which is worse than shipping nothing.
- **A hash-chained ledger underneath.** Every block links to its predecessor's hash, so a run is
  reconstructable, tamper-evident, and replayable from recorded model-call digests. *Why:* it is
  what makes a run *provable enough to learn from*. Memory accepts a lesson only from a run
  whose chain verifies, which is how the system stays junk-resistant as it teaches itself.
- **Strands for all of it.** The agent plane is the SDK — multi-agent orchestration, the eval
  harness, skills, steering, sessions — on Bedrock, with models supplied by the environment.
  *Why:* every hand-rolled agent loop is code someone has to audit and nobody else has tested.

The rule the design keeps returning to: the model reads and edits through three tools and
nothing else. Refusal, execution, acceptance, rollback and evidence belong to the platform,
because those are the properties worth being able to prove.

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
 │      │   info:  tool choice · tool params · trajectory              │     │
 │      └──────────────────────────────────────────────────────────────┘     │
 │                   │                                                       │
 │        pass ──────┴─► checkpoint the tree ─► next node                    │
 │        fail ────────► RESTORE last checkpoint, retry with the diagnosis   │
 │        out of retries ─► CIRCUIT BREAKER ─► degraded, nothing ships       │
 └───────────────────────────────────┬───────────────────────────────────────┘
                                     ▼
              ACCEPTANCE GATE — the target repo's own test suite,
              run by the platform, allowlisted and shell-free
                                     │
              ┌──────────────────────┴──────────────────────┐
           exit 0                                    anything else
   squash to one commit on                    reset to seed, clean tree,
   autodev/<run_id>, ship it                  drop checkpoint ref, ship nothing
              └──────────────────────┬──────────────────────┘
                                     ▼
 ┌───────────────────────────────────────────────────────────────────────────┐
 │ HASH-CHAINED LEDGER — every block links to the previous one's hash        │
 │   run start · node attempts · eval verdicts · gate result · model digests │
 │                                                                           │
 │   report.json · diff.patch · chain.json      offline-verifiable bundle    │
 │                    │                                                      │
 │                    └─► provenance gate ─► memory writes the lesson        │
 │                        (chain verifies + all blocks landed, not degraded) │
 └───────────────────────────────────────────────────────────────────────────┘
```

Two details where the obvious alternative is wrong. **The retry restores before it retries** —
otherwise attempt N+1 diagnoses attempt N's half-applied edits. Checkpoints sit on private
`refs/autodev/checkpoints/<run_id>` refs, never branches, so they are recoverable state and not
something anyone merges by accident; a resolved run squashes to one post-gate commit, because
the checkpoint messages say the gate never ran and that has just stopped being true. And
**evals route retries but never decide correctness** — only the test gate resolves a ticket. The
per-tool-call judges are deliberately informational: averaging ~200 of them once failed a node
whose fix the deterministic gate had already verified.

## Safety model

Ticket text and repository content are untrusted, and no prompt is asked to make them safe.

The model has read, write and list — no shell. Every path resolves through the worktree jail;
traversal, anything outside it, and git metadata (`.git`, `.gitattributes`) are refused at the
tool boundary, and reads and writes are size-bounded.

The acceptance command is untrusted too. It is split without a shell and checked token by token
against a fail-closed policy: `pytest` only, a narrow flag set, paths inside the jail. pytest's
rootdir, config and collection scope are pinned to what the *target committed*, so an
agent-authored config cannot make a red suite look green by changing what gets collected. The
run gets `shell=False`, stripped credentials, an isolated `HOME`, no user site packages, a
killed process group and a timeout, and its output is append-only while captured — a test
cannot rewrite what it already printed.

A ticket's command may be narrowed to one test file, so it never speaks for the rest of the
suite: the platform also runs the target's *whole* suite through the same hardened path, at
the seed (baseline) and after the change, and resolution requires no new failure between the
two. Red tests owned by other tickets don't block an unrelated fix; a test this change broke
always does.

The gate is the only thing that can resolve a ticket. Every other outcome reverts and leaves
the tree clean. An honest failure still teaches; a run cut short does not.

## Evidence and learning

Each run writes a scrubbed report plus a chained record: lifecycle events, eval verdicts, gate
evidence, tool metadata, and *digests* of model calls rather than the calls themselves, since
prompts carry repository source and recalled lessons.
`scripts/verify_demo_artifacts.py <bundle>` checks the file set, schemas, manifest digests,
cross-file invariants and the chain with no model, network, credentials or target repo. The
chain is tamper-**evident**, not tamper-proof — it catches edited blocks, broken links and
truncation, but anyone who can rewrite a bundle can rewrite its recorded head too.

Learn *proposes* one structured rule and holds no write tool. The platform stores it only after
bounding and scrubbing it, rejecting near-duplicates, verifying the chain, and confirming the
run was neither degraded nor replayed. Discover retrieves up to three lessons before planning,
so the demo measures *retrieval*, not storage: a control run with no relevant lesson, a primed
run given one absent from the target repo, both through the same gate, and a check that only
passes when the lesson was actually used.

## What I cut, and what is still true

I optimised for one enforceable local loop over broad repository support.

- **Not a sandbox.** Worktree confinement and credential stripping are not OS-level
  containment; production wants a no-egress container and scoped credentials. Two consequences
  worth naming precisely, both the same class: a hostile repo's *committed* git config can
  still reach `/bin/sh` via `filter.*.clean` (we pin `--git-dir` so the agent can't author one,
  but git has no flag to ignore a repo's own), and a repo's `conftest.py` can turn a red suite
  green in one line — no argv validation reaches a pytest hook.
- **Pytest only.** A wider runner policy is a wider shell surface.
- **The pre-gate is not language understanding.** Tuned for precision, because a false refusal
  exits zero and looks like a correct outcome. The controls after it are what hold.
- **No spend ceiling.** Handoffs, iterations, wall clock and read/write/commit sizes are
  bounded; cumulative tokens are not.
- **Recovery reports a tree, it does not resume a run** — and checking one out stays an
  operator decision, since it may hold code written from an untrusted ticket.
- **No control plane, local memory only.** No deploy, CI, auth, UI or scheduler.

Those cuts keep the graded behaviour narrow on purpose: refuse unsafe work, change code in
isolation, pass a test gate the platform runs before keeping anything, leave evidence someone
can check, and let a verified observation change a later run.
