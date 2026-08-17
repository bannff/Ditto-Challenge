# DESIGN — autodev

`autodev` resolves one ticket against one repository. It is a bounded ticket-resolution CLI,
not a general shell agent — the model reads and edits an isolated worktree through three
tools, and the *platform* owns refusal, execution, acceptance, rollback, and evidence.

That trade is deliberate. Giving the model less to do makes the interesting properties
testable: an unsafe ticket is declined before a worktree exists, and a change is kept only
after a test run the platform controls exits zero.

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

Preflight is conservative triage, not the security boundary — it declines the blatant before
spending a token. Everything after it assumes the ticket is hostile.

**Why the retry restores first.** Otherwise attempt N+1 diagnoses attempt N's half-applied
edits. Checkpoints live on private `refs/autodev/checkpoints/<run_id>` refs, never branches —
recoverable state, not something anyone merges by accident. A resolved run squashes to one
post-gate commit, because the checkpoint messages say the gate never ran and that stops
being true.

**Why three model families.** A reviewer that shares the builder's blind spots agrees with it.

**Why evals gate but don't decide.** LLM judges answer "did this node do its job" well and are
useless as a correctness oracle, so they route retries and nothing else. The per-tool-call
judges are informational on purpose: averaging ~200 of them once failed a node whose fix the
deterministic gate had already verified.

## Safety model

Ticket text and repository content are untrusted, and no prompt is asked to make them safe.

The model has read, write and list — no shell. Every path resolves through the worktree jail;
traversal, anything outside it, and git metadata (`.git`, `.gitattributes`) are refused at the
tool boundary. Reads and writes are size-bounded.

The acceptance command is untrusted too. It is split without a shell and checked token by
token against a fail-closed policy: `pytest` only, a narrow flag set, paths inside the jail.
pytest's rootdir, config, and collection scope are pinned to what the *target committed*, so
an agent-authored config cannot make a red suite look green by changing what gets collected.
The run itself gets `shell=False`, stripped credentials, an isolated `HOME`, no user site
packages, a killed process group, and a timeout. Its output is append-only while captured, so
a test cannot rewrite what it already printed.

The gate is authoritative and it is the only thing that can resolve a ticket. Every other
outcome reverts and leaves the tree clean. An honest failure still teaches; a run that was cut
short does not.

## Evidence

Every run writes a scrubbed report and a hash-chained record: lifecycle events, eval verdicts,
gate evidence, tool metadata, and *digests* of model calls rather than the calls themselves,
because prompts carry repository source and recalled lessons.

`scripts/verify_demo_artifacts.py <bundle>` checks the file set, schemas, manifest digests,
cross-file invariants and the chain with no model, network, credentials, or target repo.

The chain is tamper-**evident**, not tamper-proof: it catches edited blocks, broken links and
truncation, but it does not authenticate origin. Anyone who can rewrite a bundle can rewrite
its recorded head along with it.

## Learning

Discover retrieves up to three relevant lessons before planning. Learn *proposes* one
structured rule and has no write tool — the platform stores it only after bounding it,
scrubbing it, rejecting near-duplicates, verifying the ledger chain, and confirming the run was
neither degraded nor replayed. Memory learns from failures too; it does not learn from runs
whose record cannot be trusted.

The self-improvement demo measures retrieval rather than storage: a control run with no
relevant lesson, a primed run given one that is absent from the target repo, both through the
same gate, and a check that only passes when the lesson was actually used.

## What I cut, and what is still true

I optimised for one enforceable local loop over broad repository support.

- **Not a sandbox.** Worktree confinement and credential stripping are not OS-level
  containment. Production wants a no-egress container, scoped credentials, resource limits.
  Two consequences I can name precisely, both the same class: a hostile repo's *committed* git
  config can still reach `/bin/sh` via `filter.*.clean` (we pin `--git-dir` so the agent can't
  author one, but git has no flag to ignore a repo's own), and a repo's `conftest.py` can turn
  a red suite green in one line — no argv validation reaches a pytest hook.
- **Pytest only.** A wider runner policy means a wider shell surface.
- **Preflight is not language understanding.** Tuned for precision, because a false refusal
  exits zero and looks like a correct outcome. The controls after it are what hold.
- **No spend ceiling.** Handoffs, iterations, wall clock and read/write/commit sizes are
  bounded; cumulative tokens are not.
- **Recovery reports a tree, it doesn't resume a run** — and checking one out stays an
  operator decision, since it may hold code written from an untrusted ticket.
- **No control plane, local memory only.** No deploy, CI, auth, UI or scheduler; lessons are
  scrubbed and deduplicated but not repository-scoped.

Those cuts keep the evaluated behaviour narrow, which is the point: refuse unsafe work, change
code in isolation, pass a test gate the platform runs before keeping anything, leave evidence
someone can check, and let a verified observation change a later run.
