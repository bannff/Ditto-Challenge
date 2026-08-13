---
inclusion: always
---

# Security best practices

Security here is not a feature bolted on — it's the graded core of this project (safe
autonomy is 35% of the rubric). Two themes: never leak secrets, and never trust input.

## Secrets and configuration

Credentials, API keys, and any value that varies by environment (model IDs, region,
profile) come from the environment or a gitignored `.env`, never a literal in source.

- Manage config with `pydantic-settings` (`BaseSettings`), not raw `pydantic.BaseModel`
  and not scattered `os.environ.get()` calls. One typed `get_settings()` singleton is
  the single source; everything reads from it.
- Wrap real secrets in `pydantic.SecretStr` so they mask in logs and tracebacks. Unwrap
  only at the call site with `.get_secret_value()`.
- Never give a `SecretStr` field a default. Missing required config should fail loudly at
  startup, not fall back to a baked-in value.
- `.env` is gitignored; commit a `.env.example` with placeholder values so required keys
  are discoverable without leaking real ones.
- Never log or persist a raw secret — scrub at every persistence/log boundary
  (`scrub_text` at the Mem0, chromadb, and ledger write sites).

## Untrusted input is the trust boundary

A ticket (and any repo content) is untrusted input authored by a stranger. The boundary
is enforced in code at the tool layer, not in a prompt that asks the model to behave.

- The agent acts only through explicit tools; each tool is where a safety check lives.
- Work stays inside the target worktree — no writes or commands outside it, no `main`.
- No `shell=True`; pass subprocess args as a list; validate/sanitize anything derived
  from ticket text before it reaches a shell, path, or query.
- Bound every run (iterations / wall-clock / tokens). Degrade gracefully at the ceiling;
  never half-apply a change.
- Refusal is a correct outcome. Unsafe, out-of-scope, or underspecified tickets are
  declined with a reason — not force-attempted.
- Least privilege: give each agent/tool only the capabilities it needs.
