# Workflow skills

Each child directory is a Strands skill package attached to one reference workflow stage. The package’s `SKILL.md` is the authoritative instruction file; node configuration decides which skill a stage receives.

- `triage/` supports repository-grounded ticket classification and planning.
- `safe-change/` guides the smallest scoped implementation inside the isolated worktree.
- `evidence-check/` guides diff and test-evidence review before the platform’s authoritative gate.
- `lesson-writing/` guides one scrubbed, reusable lesson for future runs.

Skills guide model behavior. They do not replace the deterministic refusal, worktree, test-gate, or persistence controls.
