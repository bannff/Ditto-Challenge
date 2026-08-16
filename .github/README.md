# GitHub project controls

This folder keeps the repository’s GitHub-side workflow next to the code it governs. It supports autodev’s issue-led work process: work is scoped in an issue, reviewed in a pull request, and checked before merge.

- `CODEOWNERS` assigns review ownership.
- `pull_request_template.md` asks for the linked issue, validation evidence, and safety/scope checks.
- `ISSUE_TEMPLATE/` provides forms for bugs, features, general tasks, and structured agent tasks.
- `workflows/` contains CI plus issue, project, and pull-request automation.

These files do not run autodev itself; they define how changes to autodev are proposed and checked.
