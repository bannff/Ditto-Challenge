# Deterministic security regression suite

These tests exercise autodev’s safety controls without calling Bedrock. They stub the LLM workflow but use the real refusal gate, acceptance-command policy, worktree jail, Git isolation, ledger, and revert/commit paths.

- `test_hostile_tickets.py` covers hostile and vague tickets, path and command escapes, environment isolation, test-gate behavior, clean rollback, branch isolation, concurrency, and bounded records.
- `test_hardening_pass_qa.py` adds regression checks for hardening work and strict expected-failure findings for protections that are not yet implemented.

Run the suite with:

```bash
uv run pytest breach/deterministic -q
```

A strict `xfail` is deliberate: it documents a known security gap and becomes a failure when the underlying condition changes, signaling that the finding should be revisited.
