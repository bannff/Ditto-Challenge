# Self-improving coding agent specification

This directory is the design record for `autodev`: a ticket-driven coding agent that works in an isolated Git worktree, verifies changes with a target-repository test gate, reports structured evidence, and stores reusable lessons.

- `requirements.md` defines the required safety, workflow, isolation, refusal, memory, reporting, and interface behavior.
- `design.md` explains the fixed plumbing and the swap-in Strands node layer.
- `tasks.md` records the implementation plan and completion status.

Use this material with the repository-level `DESIGN.md` and local challenge requirements when evaluating scope or making architectural changes. Code and tests remain the source of truth for current behavior.
