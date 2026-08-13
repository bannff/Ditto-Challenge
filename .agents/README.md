# .agents

Portable, tool-agnostic agent definitions and knowledge that travel with the repo.
Editors that read an `.agents/` folder (VS Code and similar) pick these up directly; the
canonical Kiro copies with full permission/tool config live in `.kiro/agents/`.

## The team

- **build-lead** — orchestrator; owns the plan and delegates to the specialists.
- **meta-architect** — architecture gatekeeper; enforces the steering and product scope.
- **strands-expert** — Strands SDK authority; no bespoke reimplementation.
- **implementer** — writes the lean business logic, consults the SMEs.
- **qa-tester** — tests the high-value behavior (safety, test-gate, budgets, memory).
- **security-engineer** — high-signal security review of the trust boundary.

## Knowledge

- `strands-knowledge.md` — verified source of truth for how this repo wires Strands
  (models, plugins, swarms/graph, evals, telemetry, Mem0-on-Bedrock). Kiro's
  `.kiro/steering/strands.md` links to this same file so there's one copy, not two.

Markdown with YAML front-matter so definitions are readable by humans and common agent
tooling formats.
