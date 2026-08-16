---
name: safe-change
description: Make the smallest safe code change inside the worktree to satisfy the plan.
---

# Safe change

- Edit only through the file tools; every path stays inside the worktree.
- Make the smallest change that satisfies the ticket — no unrelated edits or refactors.
- Add or update a test when the ticket warrants it.
- Recall prior lessons and check policy before writing, so you don't repeat a known mistake.
- Summarize what changed and map each edit to a plan item. Do not claim the tests pass —
  that is Verify's job and the platform's test-gate.
