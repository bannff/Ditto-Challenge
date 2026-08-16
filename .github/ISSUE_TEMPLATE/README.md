# Issue forms

These forms make GitHub issues usable as autodev’s work ledger. A ticket should state the problem, relevant context, and a verifiable outcome before anyone or any agent starts changing code.

- `bug_report.yml` captures a defect, reproduction steps, and optional redacted logs.
- `feature_request.yml` captures motivation, proposed behavior, and acceptance criteria.
- `task.yml` tracks non-bug work and labels the affected area.
- `agent_task.yml` adds an objective, context, size estimate, agent constraints, and a definition of done for work delegated to the specialist agent team.
- `config.yml` controls GitHub’s issue-form behavior.

Do not put credentials, raw `.env` values, or other secrets in an issue.
