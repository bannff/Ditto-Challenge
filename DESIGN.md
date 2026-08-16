# DESIGN — autodev

`autodev` resolves typed, untrusted tickets without trusting the model to police itself. **Deterministic control** enforces isolation, budgets, test gates, and provenance; **elastic swarm intelligence** handles repository understanding and change work.

```text
                                   autodev workflow
┌──────────────────────────────────────────────────────────────────────────┐
│ Ticket ── preflight refusal ───────────────→ Refused RunReport           │
│             unsafe / vague / invalid gate                                │
│     │ pass                                                               │
│     ▼                                                                    │
│ isolated Git worktree: autodev/<run_id>                                  │
│     ▼                                                                    │
│ [ Discover ] → [ Implement ] → [ Verify ] → [ Learn ]                   │
│       elastic, role-based swarm at every stage                           │
│          │ eval checkpoint + failure detector                            │
│          └── diagnosis-informed retry, within a fixed budget             │
│                                                                          │
│ layered circuit breakers: handoffs · iterations · node/run timeouts     │
└──────────────────────────────────────────────────────────────────────────┘
        │                         │
        │ red, missing, or        │ green acceptance command
        │ unrunnable test gate    ▼
        ▼                    commit branch + RunReport
   revert; clean tree             │
        └───────────────┬─────────┘
                        ▼
       hash-chained run ledger → provenance gate → trusted lesson memory
                        └── offline `autodev replay <run_id>`
```

## 1. Graph for deterministic control; swarm for elastic intelligence

The graph owns **Discover → Implement → Verify → Learn**, allowed transitions, and budgets. Each stage uses builder, reviewer, and adversary roles in a bounded Strands swarm. Simple tickets can finish after one role finds sufficient evidence; cross-file or security-sensitive work can draw out role-based challenge and refinement. The deliberation is elastic; the safety path is not.

Every attempt produces a structured verdict. Passing verdicts advance; failing verdicts self-loop with a bounded, diagnosis-informed retry when useful diagnosis is available. Exhausted retries, non-completed swarms, or a missed deadline degrade safely instead of half-applying a change. Explicit limits bound handoffs, iterations, node time, execution time, and total run time.

## 2. Safety is enforced in code, not prompts

Preflight refuses unsafe, underspecified, or invalid tickets before a worktree exists. Passing tickets run only in an `autodev/<run_id>` Git worktree. File tools resolve paths through that jail; there is no general shell tool; Git hooks, fsmonitor, and external protocols are disabled.

The ticket’s test command is untrusted input. It is parsed without a shell, checked against a fail-closed policy, and run with stripped credentials, isolated `HOME`, no user site packages, and a bounded process group. The platform—not an agent report—runs the target test gate. Only a green gate, successful workflow, and non-degraded run commits. Every other outcome reverts and reports why.

Today this is credential isolation and worktree confinement, not an OS filesystem or egress sandbox. Production executes untrusted tests in a no-egress container or microVM with scoped credentials.

## 3. Learning that earns the right to persist

Discover retrieves relevant Mem0/FAISS lessons before planning; Learn distills one reusable rule. Writes are scrubbed, deduplicated, and code-controlled—agents can recall memory but cannot write it directly.

Key lifecycle, tool, test-gate, lesson, and outcome events enter a per-run SQLite SHA-256 hash chain with a recorded head, so modification, mid-chain deletion, and truncation of the stored record are each detectable. Tool arguments record a file's path but only a digest of its content, keeping arbitrary repository text out of a durable row that replay prints. A lesson is stored only if that chain verifies, the recorder observed no dropped writes, and the workflow did not degrade. Honest test failures may teach; breaker-tripped or unverifiable runs cannot poison later planning. `autodev replay <run_id>` verifies and prints the local record offline, with no model calls and no network. This is tamper-evident stored history, not signed provenance, crash recovery, or re-execution.

The self-improvement demo proves the read policy matters. In the orders-service IDOR scenario, a test names one vulnerable read path while a sibling summary path remains vulnerable. A control run can satisfy that narrow gate; a run primed with a prior lesson to inspect sibling read paths can secure both. Applying the lesson is model-mediated; writing, retrieving, and gating it are deterministic.

## 4. Supporting mechanisms

Pydantic v2 validates tickets, verdicts, reports, and lessons at ingress and egress, avoiding fragile dictionaries. A typed taxonomy provides domain invariants and acceptance hints as data. Nodes receive only job-specific skills, tools, policy lookup, and steering.

Memory captures what prior runs learned; the persistent Chroma policy KB provides stable repository-owned guidance. Ruff, Pyright, pytest, and Hypothesis test lint, types, behavior, and generated edge cases. `breach/` adds hostile-ticket, red-team, and chaos tests because a judge’s confidence is not evidence that a boundary held.

## 5. Production path and deliberate cuts

At scale, a coordinator schedules isolated, bounded runs with per-run cost ceilings and retained reports. A promotion path adds deploy → verify → rollback after the repository test gate; sandboxed workers and scoped identities stop bad changes from reaching production or ambient credentials.

I cut deployment infrastructure, containers, auth, UI, crash-resume, signed ledger blocks, and replay re-execution. The working front doors are the CLI and FastMCP server. The investment went into the graded core: an enforceable worktree/tool boundary, independent green-test gate, bounded recovery, demonstrable learning, and an auditable reason for every outcome.
