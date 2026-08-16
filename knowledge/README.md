# Repository-owned agent knowledge

This folder contains versioned guidance supplied to autodev at runtime. It separates stable, reviewable knowledge from ticket text and model prompts, so the agent can retrieve policy and stage-specific instructions without hardcoding them into workflow plumbing.

- `taxonomy.yaml` defines domain tags, their invariants, and acceptance hints.
- `policies/` contains read-only engineering and security guidance loaded into the policy knowledge base.
- `skills/` contains node-scoped Strands skill packages for the reference workflow.

Keep this content actionable, non-secret, and general enough to apply across target repositories.
