---
name: evidence-check
description: Judge whether a change is correct and complete, citing concrete evidence.
---

# Evidence check

- Inspect the diff and the tests with the read tools; base the verdict on what you actually
  see, not on what the change claims.
- Cite concrete evidence: exit codes, which tests pass or fail, specific diff hunks.
- The platform runs the authoritative test-gate — your job is to summarize the evidence and
  flag gaps (missing tests, unhandled cases), not to declare the gate yourself.
- Give an unambiguous correct / not-correct recommendation.
