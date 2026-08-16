# Kiro project configuration

This folder is the Kiro-specific layer of the repository’s agent configuration. It makes the project’s workflow, specialist roles, and design records available to Kiro while keeping the application code in `src/`.

- `agents/` defines the Kiro-facing specialist team, including permissions and project resources.
- `steering/` contains always-loaded rules for product scope, security, engineering practices, the agent team, and Strands usage.
- `specs/` stores the requirements, design, and implementation plan for the self-improving coding agent.
- `hooks/` is reserved for optional Kiro lifecycle hooks.

The portable agent-role mirror lives in `.agents/`; this directory adds Kiro-specific configuration.
