# breach — adversarial testing of the agent loop

Three layers, cheapest and most authoritative first. The goal is to break the trust
boundary before a reviewer does, and to keep the evidence.

| layer | what it proves | cost | command |
|---|---|---|---|
| **deterministic** | a hostile ticket cannot escape the jail, disable the gate, or leave a dirty tree | free, keyless, seconds | `uv run pytest breach/deterministic -q` |
| **red team** (`strands_evals.experimental.redteam`) | an adaptive LLM attacker can't talk the swarm past its tool boundary | real Bedrock, minutes | `uv run python breach/redteam_run.py` |
| **chaos** (`strands_evals.chaos`) | the loop degrades safely when its own tools fail mid-change | real Bedrock, minutes | `uv run python breach/chaos_run.py` |

Every run writes its transcript and report to `scratch/` so you can follow along.

## What each layer targets

**Deterministic** drives `run_ticket()` end to end with hostile tickets and asserts
invariants in code: nothing written outside the worktree, `main` untouched, the tree clean
after a failed gate, the acceptance command allowlisted, refusal on unsafe input.

**Red team** points `RedTeamExperiment` at the real **Implement swarm** — the node that
holds the write tools and the live steering interceptor, i.e. where the boundary actually
is. A `Swarm` implements `serialize_state`, so the SDK's own `StrandsMultiAgentSession`
drives it natively (snapshot/restore between attempts) and `AttackSuccessEvaluator` reads
the captured tool-use trace to detect a breach. After every attack we *also* verify the
filesystem directly — a judge's opinion is not evidence that the jail held.

**Chaos** injects tool failures (`Timeout`, `ExecutionError`, corrupted results) into the
implement node's file tools to prove a mid-change failure degrades rather than half-applies.

## Notes

- Attacks are capped low on purpose (`max_turns`), because each turn is a real swarm
  invocation. The SDK's default attacker task passes `max_turns=50`; strategies take
  `min(their own cap, that)`, so the cap lives on the strategy.
- Models come from the same env config as the rest of the system. The red-team judge and
  attacker default to a hardcoded model in another region if you don't pass one — we always
  pass ours explicitly.
- Findings worth keeping become GitHub issues, not silent fixes.
