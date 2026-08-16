# Policy documents

These Markdown files provide read-only policy context to autodev’s agent workflow. `PolicyKB` seeds the local Chroma collection from the security policy and exposes relevant matches through the `query_policy` tool.

- `security-best-practices.md` covers secret handling, safe subprocess use, parameterized queries, and scrubbing sensitive information before logging or persistence.

Policies are guidance for agent reasoning; enforcement still belongs in deterministic code at the worktree, tool, acceptance-policy, and persistence boundaries.
