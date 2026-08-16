# Judge Presentation Plan

## Goal

Present `autodev` as a small, safe, honest coding-agent loop. The primary proof is offline-reviewable evidence for the four required scenarios: a resolved bug, a resolved feature, a refused unsafe ticket, and self-improvement that changes later behavior. A short recording is a guided tour of that evidence, not a substitute for it.

## Scope discipline

Keep the CLI, enforced worktree/tool boundary, independent test gate, bounded graph, structured report, durable lesson memory, and compact production-path discussion in `DESIGN.md`.

Remove or de-emphasize judge-facing links to legacy UI, HTTP/SSE, telemetry, deployment, CI, and governance plans. No UI, cloud deployment, container, or new infrastructure work is in scope. Keep the explicit current limitation: worktree and credential confinement are not an OS-level no-egress sandbox; production would use a scoped-credential sandbox worker.

## Canonical evidence bundles

Both live demo scripts will write the same scrubbed run-bundle format:

```text
manifest.json
trace.log
report.json
diff.patch
chain.json
chain.log
```

`manifest.json` records the version, ticket/scenario identity, expected outcome, and digests of persisted artifacts. `chain.json` exports the typed ledger evidence needed for offline chain verification; `chain.log` is the human-readable replay. The manifest and writer reject unsafe artifact paths and scrub every persisted artifact.

A successful bundle must have a successful `RunReport`, a committed isolated branch, a passing acceptance gate, a meaningful source diff, and a verified chain. A refusal bundle must have a refusal reason and no shipped branch.

## Self-improvement proof

`scripts/demo_selfimprove.py --out DIR` will save a `control/` and a `primed/` run bundle plus a top-level contrast file. It exits nonzero unless both runs are successful with green acceptance gates, the control misses the hidden business rule, and the primed run satisfies it. This demonstrates memory changing later behavior rather than merely storing history.

## Offline verification

An offline verifier will validate manifest integrity, safe filenames, artifact digests, `RunReport` consistency, outcome-specific semantics, and ledger-chain evidence. It deliberately does not claim to re-execute a live model run. Focused tests cover valid success/refusal/contrast evidence and tampering, path traversal, missing chain data, false-success, and bytecode-only-diff rejection.

## Generation and review workflow

1. Run focused artifact tests and project quality checks.
2. On a Bedrock-configured machine, generate live evidence into a temporary directory.
3. Run the offline verifier against candidate bug/feature/refusal and self-improvement bundles.
4. Inspect the verified artifacts and only then replace committed demo evidence.
5. Record a 5–7 minute asciinema that walks the verified bug, feature, refusal, self-improvement contrast, and optional offline ledger demonstration.

The final judge path is: README → DESIGN → committed evidence bundles → optional recording → reproducible scripts.
