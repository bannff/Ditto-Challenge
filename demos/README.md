# Demo evidence

Use this directory for generated review artifacts, not as a claim that a live run has
already succeeded. Generate the four required outcomes, then verify each bundle offline.

## Generate the evidence

With Bedrock configuration and AWS credentials available:

```bash
# Bug resolved, feature resolved, and refusal
uv run python scripts/demo.py --out demos/generated

# Control vs primed-memory self-improvement comparison
uv run python scripts/demo_selfimprove.py --out demos/self-improvement
```

The first command materializes the bundled target app into scratch Git repositories and runs
its selected tickets. The second exits nonzero unless both runs pass their acceptance gates
and the primed run, unlike the control, passes the memory-only check.

## Verify without running the agent

Each individual generated run directory must contain exactly this canonical bundle:

| File | Purpose |
|---|---|
| `manifest.json` | Expected outcome and SHA-256 digests/byte counts for the other five files |
| `trace.log` | Node lifecycle and evaluation trace |
| `report.json` | Structured outcome and test-gate evidence |
| `diff.patch` | Full scrubbed patch evidence |
| `chain.json` | Exported decision chain and recorded head |
| `chain.log` | Human-readable chain verification output |

Verify individual bundles as needed:

```bash
uv run python scripts/verify_demo_artifacts.py demos/generated/bug-1-failing-test
uv run python scripts/verify_demo_artifacts.py demos/generated/feature-1-acceptance-test
uv run python scripts/verify_demo_artifacts.py demos/generated/refuse-unsafe
```

A self-improvement evidence root contains `contrast.json` plus `control/` and `primed/`
child bundles, rather than a six-file run bundle. Verify the complete comparison with:

```bash
uv run python scripts/verify_demo_artifacts.py --self-improvement demos/self-improvement
```

The verifier is offline: it needs no model calls, network access, credentials, or target
repository. It checks the canonical artifact set, safe file shape and size, strict JSON,
manifest hashes, outcome consistency, success/refusal evidence rules, and the exported
hash chain. For a self-improvement root, it verifies both child bundles, the required
`control=false`, `primed=true` contrast, and binds each contrast run ID to its child run.
A passing bundle establishes only internal consistency. Its manifest and recorded chain head
are self-contained and unsigned, so an attacker who can modify the exported directory can
replace them with the artifacts. It does not provide tamper evidence or post-export tamper
detection against that attacker, or authenticated origin.

## Optional local ledger demonstration

The ledger demonstration exercises tamper detection and the learning provenance gate without
Bedrock:

```bash
uv run python scripts/demo_ledger.py
```

It is supporting offline behavior, not a substitute for the four required CLI outcomes.
For a generated local run, inspect its stored chain with:

```bash
uv run autodev replay <run_id>
```

## Optional model recording

`autodev run --record <cassette-id>` is only for fixture repositories below the configured
`CASSETTE_FIXTURE_ROOT`. A cassette stores unredacted model prompts, is owner-only, and is
not part of a review bundle. `autodev reexecute` uses that cassette without model calls,
still runs the acceptance gate, and never commits or teaches memory.
