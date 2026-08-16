# DESIGN — autodev

`autodev` is a bounded CLI workflow for resolving typed, untrusted tickets against a target
repository. The design separates deterministic controls from model judgment: the platform
owns isolation, tool access, budgets, the acceptance gate, and evidence; bounded Strands
roles understand the repository and propose the change.

## Review path

The primary path is:

```text
Ticket → preflight → isolated worktree → Discover → Implement → Verify → Learn
                  ↘ refusal                         ↘ acceptance gate → report + bundle
```

A judge should establish four outcomes: a resolved bug, a resolved feature, a justified
refusal, and a before/after self-improvement comparison. For live runs, `scripts/demo.py`
creates the bug/feature/refusal evidence and `scripts/demo_selfimprove.py` creates the
control/primed comparison. Neither is asserted as successful until it has been generated
and its bundles pass offline verification.

## Deterministic control, bounded model work

Preflight rejects unsafe, invalid, or underspecified tickets before creating a worktree.
Passing work is confined to an `autodev/<run_id>` Git worktree; file operations are resolved
through that jail, and the agent has no general shell tool. The ticket's acceptance command
is untrusted input: it is parsed without a shell, checked against a fail-closed policy, and
run by the platform with stripped credentials, isolated `HOME`, no user site packages, and
a bounded process group.

Discover, Implement, Verify, and Learn use role-specific, bounded Strands swarms. Each stage
emits a structured verdict; a useful diagnosis may trigger a bounded retry. Explicit limits
on handoffs, iterations, node/runtime timeouts, and tokens stop flailing. Exhaustion,
incomplete work, or a red/unrunnable gate degrades safely: the change is reverted and the
report explains why. Only a completed workflow with a green acceptance gate retains its
branch.

A retry also resets the workspace. When a stage passes its evaluator checkpoint its tree is
committed to a private `refs/autodev/checkpoints/<run_id>` — never a branch, so it is not a
merge target — and a failed attempt is rolled back to that checkpoint before the next one
begins. Without this, an informed retry inherits the previous attempt's half-applied edits
and diagnoses a tree nobody intended. Reverting anchors to the commit the run started from
rather than to `HEAD`, because once a checkpoint exists `HEAD` *is* that checkpoint and
`reset --hard HEAD` would preserve the change it is meant to discard. These commits pass an
evaluator checkpoint, not the acceptance gate, and say so in their message; shipping squashes
them into a single commit so a retained branch carries exactly the change under one honest
message, and a run that is reverted also drops its checkpoint ref so rejected work is not
left reachable in the target's object store.

The boundary is worktree confinement plus credential isolation, not an OS filesystem or
egress sandbox. Production execution would put the same workflow in a no-egress container
or microVM with scoped credentials.

## Evidence and offline checks

A demo run written with `--out` produces an individual canonical, scrubbed bundle containing exactly these six files:

- `manifest.json`: expected outcome plus SHA-256 digest and byte count for all artifacts.
- `trace.log`: lifecycle and evaluator events.
- `report.json`: structured result, acceptance evidence, verdicts, branch, and lesson.
- `diff.patch`: the full scrubbed change evidence.
- `chain.json`: exported hash-chain blocks and recorded head.
- `chain.log`: readable offline verification of that chain.

`scripts/verify_demo_artifacts.py <bundle-dir>` needs no model, network, credentials, or
repository. It enforces the exact six-file canonical set and regular-file/size rules,
validates strict schemas, checks manifest digests and report/chain agreement, evaluates
success/refusal invariants, and recomputes the chain. A self-improvement evidence root instead
contains `contrast.json` and `control/` and `primed/` child bundles; verify it with:

```bash
uv run python scripts/verify_demo_artifacts.py --self-improvement demos/self-improvement
```

That offline check verifies both child bundles, the required `control=false`, `primed=true`
contrast, and each contrast run ID's binding to its child run. For an exported bundle, those
checks establish only internal consistency: its manifest and recorded chain head are
self-contained and unsigned. An attacker who can modify the exported directory can replace
them with the artifacts, so verification provides neither tamper evidence nor post-export
tamper detection against that attacker, and it does not authenticate origin. `autodev replay
<run_id>` similarly verifies and renders a local ledger record offline.

The ledger detects edits, sequence/link changes, and tail deletion through its recorded
head. It is internally tamper-evident, **not authenticated provenance**: blocks are unsigned,
the writer is trusted, and an attacker able to forge evidence at the source can produce a
chain-valid bundle. A verified chain also gates learning: altered, incomplete, or
breaker-tripped runs cannot write lessons; a complete honest failure can.

Each model call records three digests — the conversation, the system prompt, and the response
— and no payload, because a prompt carries repository source, ticket text, and the lessons
recalled from memory. Hashing the system prompt apart from the conversation is what lets
divergence be read: memory grows between runs, so a changed system digest under an unchanged
conversation digest means priming moved rather than behaviour.

`autodev recover <run_id>` is the path that reads the recorded git hashes back, and it splits
the question deliberately. Git's checkpoint ref names *which* commit is recoverable, since the
ref store is written by the workflow and never by the ledger; the chain decides *whether* it
may be used, supplying the run's seed — which after the run exists nowhere else — for an
ancestry check and corroborating that the commit is one it recorded. Two independent stores
must agree, and contradiction refuses. It reports rather than materializes: a recovered tree
is agent-authored content derived from an untrusted ticket, so writing it out for someone
means the next test run in that directory executes code the agent wrote. Recovery is
short-horizon by design — only the most recent checkpoint refs are kept and git reclaims the
rest — so it is a rollback aid, not an archive.

## Learning policy

Before planning, Discover retrieves relevant durable lessons. Learn distills at most one
scrubbed, deduplicated lesson, but agents cannot write memory directly. The self-improvement
demo measures the read policy rather than merely showing storage: a control run has empty
memory, while a primed run receives a prior rule absent from the repository. Both must pass
the target gate; the hidden check distinguishes whether the recalled rule changed the later
result.

## Deliberate cuts and production path

At production scale, a coordinator would schedule isolated runs with cost ceilings and
retained reports; a promotion path would add deploy, verify, and rollback after the repository
gate. The implementation deliberately excludes deployment infrastructure, containers, auth,
UI, crash-resume, and signed blocks. Recovery reports the last checkpointed tree and leaves
materializing it to the operator; it does not resume an interrupted run, and signing blocks
with a per-node key — the change that would make the chain authenticate origin rather than
only detect edits — is named here rather than built. Optional fixture-only cassette
re-execution exists for review, not as production crash recovery. Those cuts keep the
evaluation focused on the CLI's enforceable trust boundary, test gate, bounded recovery,
evidence, and measurable learning.
