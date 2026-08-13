---
inclusion: always
---

# Dev best practices

**Polymorphic over duplicated.** Prefer one general function or interface over many
near-duplicate ones. Logic that varies by domain, model, or node is data, not code —
parameterize it. The graph runs node definitions as config (prompts, model, skills,
steering, tools, evaluators are inputs to shared machinery). If you're about to copy a
function and change a couple of literals, add a parameter instead. Swapping the use case
should be swapping data, not rewriting plumbing.

**Data-driven.** Behavior comes from config and data, not hardcoded branches. Adding a
new case means adding data, not editing control flow.

**Single responsibility.** One module, one job. Mirror the existing file boundaries —
contracts, settings, scrub, taxonomy, ledger, kb, memory, worktree each own exactly one
concern. If a module starts doing two things, split it.

**SDK-first, not bespoke.** Strands does it all — use it. Reach for the Strands /
`strands_tools` / `strands_evals` primitive before writing anything custom. No
hand-rolled agent loops, tool frameworks, eval harnesses, retry logic, or session
management the SDK already provides. If tempted to build machinery, first prove the SDK
can't do it. Thin glue between SDK primitives is the goal.

**No over-engineering.** Build what the task needs and nothing more. No speculative
abstraction, no configurability nobody asked for, no defensive layers for inputs that
can't occur. The challenge explicitly penalizes over-scoping (real cloud deploy,
containers, heavy UI) while the core loop is shaky — keep the safe agent loop, the
test-gate, and the memory sharp; treat everything else as optional flex. When in doubt,
cut.

**Test-driven.** Write the test alongside or before the code. Contracts, scrub patterns,
and shape validation get Hypothesis property tests. A feature or bug fix ships with a
test.

**Toolchain (run before every commit).**
- `pydantic` v2 models at every ingress/egress boundary; `pydantic-settings` for config.
- `ruff check` — lint + import order, zero warnings.
- `pyright` — type check, zero errors.
- `pytest` + `hypothesis` — unit and property tests green.

**Lean, human-sounding code.** No docstring on every trivial function. No comments that
narrate what the code already says. No meta-commentary. Comment the *why* when it isn't
obvious, and nothing else.

**Verify for real.** A command exiting 0 is not proof of success. Check the actual output
against what was asked before calling something done.
