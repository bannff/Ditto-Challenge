# demos — inspect a run without running anything

Saved output from real and offline runs, committed so the work can be reviewed by reading.
Every file here is scrubbed on write (`scrub_text` at the artifact boundary).

## Start here if you have no AWS credentials

`ledger/ledger-demo.log` — the full transcript of the offline hash-ledger demo. Five acts:
a resolved run's chain verifying, a tampered block being pinpointed, a deleted tail being
caught, the provenance gate allowing/refusing three different runs, and a plain statement of
what the design does *not* claim.

Reproduce it in about two seconds, no credentials, no network:

```bash
uv run python scripts/demo_ledger.py
```

## Bundles from live runs

`latest/<ticket_id>/` holds one directory per ticket resolved against a bundled target app:

| file | what it is |
|---|---|
| `trace.log` | the node-lifecycle trace as it happened — each swarm node, its eval score, any redo |
| `report.json` | the full `RunReport`: outcome, per-node verdicts, the test-gate's real exit code, the lesson |
| `diff.patch` | the complete change the run produced (unclipped — the ledger caps only its own history row) |
| `chain.log` | the run's hash chain, verified, as `autodev replay` renders it |

`chain.log` appears in bundles generated after the hash chain landed; older bundles predate it.

Regenerate with credentials:

```bash
uv run python scripts/demo.py --out demos/latest
```

## What each demo is meant to show

| command | the claim it demonstrates |
|---|---|
| `scripts/demo_ledger.py` | tamper-evident record; provenance gate keeps untrustworthy runs out of memory (offline) |
| `scripts/demo.py` | the loop end to end — a bug resolved, a feature resolved, a ticket refused |
| `scripts/demo_selfimprove.py` | memory changing a later run: same ticket, empty memory vs primed |
| `breach/` | the adversarial harness — hostile tickets provably can't do harm |
| `autodev replay <run_id>` | audit any recorded run offline, exit nonzero if its record was altered |
