# GitHub Actions workflows

These workflows automate repository checks and issue handling around autodev. The runtime agent remains local and Bedrock-backed; Actions provides repository governance rather than a second execution path.

- `ci.yml` runs the keyless quality gates on pull requests and pushes to `main`: Ruff, Pyright, and pytest.
- `triage.yml` labels new issues for triage and acknowledges issues explicitly assigned to Kiro.
- `project-automation.yml` adds new issues and pull requests to the configured GitHub Project when its repository secret is available; otherwise it explains the missing setup.
- `auto-merge.yml` enables squash auto-merge for pull requests opened by the repository owner.

CI intentionally has no Bedrock or AWS credentials. Live agent demos are run outside GitHub Actions.
