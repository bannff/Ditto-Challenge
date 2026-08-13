---
inclusion: always
---

# Agent team

This repo ships its own specialist agents in `.kiro/agents/` (and mirrored in `.agents/`
for editors that read that folder). They travel with the repo so any contributor —
human or agent — gets the same reviewers. Use them; don't reinvent their judgment inline.

## Roster

- **build-lead** — orchestrator and primary session agent. Owns the plan and the fixed
  plumbing. Delegates specialist questions to the agents below rather than guessing.
- **meta-architect** — the gatekeeper. Validates a plan or change against the steering
  (polymorphic, data-driven, SRP, SDK-first, no over-engineering) and the product scope
  *before* code is written. Blocks non-compliant designs.
- **strands-expert** — the SDK authority. Checks Strands/`strands_tools`/`strands_evals`
  usage is correct and idiomatic, and that nothing bespoke reimplements what the SDK
  provides. Verifies against the installed source.
- **implementer** — the builder. Writes lean business logic that follows the steering and
  consults the SMEs (meta-architect, strands-expert, security-engineer) when a decision
  is outside its lane. Doesn't write tests or docs.
- **qa-tester** — tests the high-value behavior: the safety boundary, the test-gate, the
  budget ceiling, refusal, and self-improvement before/after. Not trivial-getter tests.
- **security-engineer** — reviews for high-value security issues only (trust-boundary
  escapes, injection, secret leakage, sandbox holes). Ignores style nits.

## Routing

- Designing or changing architecture -> meta-architect gates it first.
- Any Strands API question or "should this be custom?" -> strands-expert.
- Writing feature/plumbing code -> implementer, escalating to the relevant SME.
- Verifying behavior, especially safety and self-improvement -> qa-tester.
- Anything touching the trust boundary, subprocess, secrets, or the worktree jail ->
  security-engineer.

Keep reviews high-value. These agents flag what matters (correctness, safety, scope,
SDK-adherence), not cosmetic nits that ruff/pyright already catch.

## GitHub as the work ledger

GitHub is the control plane. Work is tracked as issues, not just chat.

- Found a bug, gap, or follow-up while working? File it as a GitHub issue (`gh issue
  create`) instead of only mentioning it. Bugs use the bug form, new work the feature/task
  or agent-task form. Don't fix unrelated things inline — file and move on.
- One issue = one unit of work. Branch and PR reference it (`Closes #N`); merging the PR
  closes the issue. Never commit straight to `main`.
- Scope every issue to the rubric. If it's out of scope or over-engineering, say so and
  close it rather than building it.
- Never paste secrets or raw `.env` values into an issue, PR, or comment.
