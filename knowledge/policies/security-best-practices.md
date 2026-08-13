# Security best practices

- Never commit secrets, credentials, API keys, or tokens. Load them from environment variables or a gitignored .env file.
- Validate and sanitize all input passed to subprocesses. Never build a shell command by concatenating untrusted input.
- Do not use shell=True in subprocess calls. Pass arguments as a list so the shell cannot reinterpret them.
- Parameterize every database query. Never format user input directly into SQL.
- Scrub PII and secrets from anything that is logged or persisted.
