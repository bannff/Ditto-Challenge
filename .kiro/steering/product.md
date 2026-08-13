# Product

**What we're building.** `autodev` — a CLI/service that, given a typed ticket,
autonomously resolves it against a target repo and reports the outcome. For each ticket:
classify (bug / feature / refuse), understand the repo, act on an isolated branch/worktree
(never `main`), self-verify by running the target's test suite, report a structured
result (what it did, diff, test output, plain-English summary), and learn a durable lesson
that measurably changes later runs.

Source of truth: `Reqs.md` (the challenge) and `SPEC.md` (the v3 build plan). This file is
the quick-reference; when they conflict, `Reqs.md` wins on *what's graded*.

**What actually gets graded (weight it accordingly):**

| Dimension | Weight | Excellent looks like |
|---|---|---|
| Safe autonomy | 35% | Real enforced trust boundary; test-gate that blocks; budgets; graceful refusal; a hostile ticket provably can't do harm |
| Agent-loop design | 30% | Clean understand -> act -> verify loop; explicit tool boundary; recovers from a failed step instead of flailing |
| Self-improvement | 20% | Memory that demonstrably changes a later run; thoughtful write/read policy; junk-resistant |
| Judgment & communication | 15% | Scoped to budget; honest about limits; DESIGN.md shows production thinking; clean readable code |

**Hard requirements (non-negotiable):**
- Untrusted ticket text can never make the agent exfiltrate secrets, run destructive
  commands, disable its own checks, escalate, or act outside the target repo.
- Test-gate before "done": no change is resolved unless the target's tests pass; a change
  that breaks tests is reverted, not shipped; the working tree is left clean either way.
- Bounded runs: hard ceiling on iterations / wall-clock / tokens; degrade gracefully.
- Idempotent & isolated: re-running a ticket doesn't corrupt state; concurrent tickets
  don't clobber each other.
- Refusal is a valid, correct outcome.

**Deliverables:** the platform + a small self-contained target app (~200-400 LOC) with a
one-command test suite + at least 4 seed tickets (2 bugs, 2 features, 1 that must be
refused); a short demo (bug resolved, feature resolved, ticket refused, self-improvement
before/after); and DESIGN.md (<=2 pages) — the most closely read artifact.

**Explicitly out of scope — do NOT spend time here:** real cloud deploy, containers, CI,
auth systems, polished UI. Over-scoping into infra while the core loop is shaky is a
documented red flag. "Ship" means the verified change is on a branch with a clean diff +
a report.

**Green flags to aim for:** find a subtle way the loop could hurt a repo and defend against
it; make the refusal path as considered as the success path; a genuinely convincing
before/after self-improvement demo; a DESIGN.md you could hand to a teammate.
