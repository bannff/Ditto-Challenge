# Implementation Plan

Ordered so the fixed plumbing is built before the swap-in node layer: contracts and
settings first, safety and persistence primitives next, then telemetry and the graph
machinery, then the reference nodes, then the external surfaces. Tasks proven by existing
modules and passing tests are marked done.

- [x] 1. Pydantic v2 contracts at every boundary
  - `Ticket`, `EvaluatorScore`, `Verdict`, `AcceptanceResult`, `Lesson`, `RunReport`,
    `Taxonomy`/`TaxonomyTag`, `Outcome`/`NodeState`, active `schema_version`.
  - Hypothesis round-trip and shape-validation property tests.
  - _Requirements: 8.1, 8.3, 5.4, 11.2, 11.3_

- [x] 2. Typed settings singleton
  - `pydantic-settings` `get_settings()`; Bedrock model IDs/region from env; secrets in
    `SecretStr` with no defaults; `.env` gitignored, `.env.example` committed.
  - _Requirements: 1.5_

- [x] 3. Scrub / PII redaction
  - Regex `scrub_text()` for SSNs, card numbers, AWS keys, generic secrets, DOB-to-year.
  - Hypothesis property tests that secrets never survive scrubbing.
  - _Requirements: 1.4, 7.4_

- [x] 4. Taxonomy loader
  - `knowledge/taxonomy.yaml` -> `Taxonomy` model; fixed tag lookup of invariants and
    acceptance hints; tests.
  - _Requirements: 6.1, 9.4_

- [x] 5. SQLite run ledger
  - Durable run history/evidence; scrub applied at write; inert `schema_version` migration
    seam; tests.
  - _Requirements: 4.4, 8.1, 8.3_

- [x] 6. chromadb policy KB
  - `PersistentClient`, one collection seeded from a short security policy doc;
    `query_policy` similarity search; tests.
  - _Requirements: 7.5_

- [x] 7. Worktree isolation (trust-boundary jail)
  - Create/tear down a per-run branch/worktree; never touch `main`.
  - Enforce that all writes and command execution stay inside the worktree; reject paths
    resolving outside it.
  - Subprocess via arg list, no `shell=True`; sanitize ticket-derived values.
  - Leave the working tree clean on every exit path.
  - _Requirements: 1.1, 1.2, 1.3, 1.6, 4.1, 4.2, 4.3, 2.3_

- [x] 8. Mem0 memory backed by Bedrock
  - Native `strands_tools.mem0_memory`; `llm`/`embedder` `provider: "aws_bedrock"`; local
    FAISS.
  - Write-both-outcomes lesson policy with scrub at `add()`; `search` retrieval to prime
    Discover.
  - Tests for write/read policy and junk-resistance.
  - _Requirements: 7.1, 7.2, 7.3, 7.4_

- [x] 9. Telemetry + session mapper
  - `StrandsTelemetry().setup_console_exporter()`; in-memory span ring buffer;
    `StrandsInMemorySessionMapper` producing the `Session` for evaluators/detectors.
  - _Requirements: 10.1, 10.2, 10.4_

- [x] 10. Node-lifecycle status stream
  - Emit `{node, state, eval_score, timestamp}` on node start/complete/redo/fail, on a
    channel separate from the OTEL pipe.
  - _Requirements: 10.3_

- [x] 11. Eval checkpoint + detector machinery
  - Run `strands_evals` LLM-as-a-judge evaluators after a node; produce `EvaluatorScore`s
    and a `Verdict`.
  - On failure run `diagnose_session` (`DiagnosisTrigger.ON_FAILURE`); capture diagnosis
    into the `Verdict`.
  - `OutputEvaluator` fallback path for a no-Session first checkpoint.
  - _Requirements: 6.2, 6.3, 6.4_

- [x] 12. Node config (swap-in data shape)
  - Data structure describing a node's swarm (prompts + models), skill + steering content,
    tools, and evaluators, consumed by the graph builder.
  - _Requirements: 6.1, 6.7_

- [x] 13. Graph machinery + circuit breaker
  - Build the `GraphBuilder` graph from node configs; run each node's `Swarm` with explicit
    `max_handoffs`/`max_iterations`/`execution_timeout`.
  - Drive the self-heal redo within Swarm bounds; on trip, fall back to a local/stubbed
    model; thread eval results forward to the final node.
  - Degrade gracefully at the ceiling; never half-apply a change.
  - _Note: implemented as a thin async Swarm driver rather than `GraphBuilder` — an
    eval-gated redo can't be expressed via GraphBuilder's sync edge predicates. Deviation
    tracked for DESIGN.md (issue #27)._
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 6.1, 6.5, 6.6_

- [x] 14. Agent plane: skills + steering per node
  - Attach `AgentSkills` and `LLMSteeringHandler` to every agent, scoped one set per node;
    no bare `Agent(system_prompt=...)`.
  - Steering interceptor enforces tool-call guardrails (Proceed/Guide/Interrupt).
  - _Requirements: 6.7, 1.1_

- [x] 15. Session checkpointing
  - Wire `FileSessionManager` into swarm construction; store session id + conversation
    state to resume.
  - _Requirements: 4.2_

- [x] 16. Reference nodes (swap-in layer)
  - [x] 16.1 Discover node — repo understanding, Mem0 lesson priming, `query_policy`,
        classify bug/feature/refuse.
    - _Requirements: 5.1, 5.2, 5.3, 6.1, 7.2_
  - [x] 16.2 Implement node — apply the change in the worktree via `shell`/`file_read`/
        `file_write`/`editor`; ensemble builder + reviewer models.
    - _Requirements: 2.4, 6.1_
  - [x] 16.3 Verify node — run the acceptance command as the test-gate; revert on red;
        record `AcceptanceResult`.
    - _Note: the authoritative test-gate, revert, and `AcceptanceResult` are enforced in
      `run_ticket` (deterministic), not in the verify node's agent, which summarizes
      evidence. Behavior satisfies the requirement._
    - _Requirements: 2.1, 2.2, 2.3, 2.5_
  - [x] 16.4 Learn node — read collected eval results, close out the run, distill a lesson
        both ways, file to Mem0, update KB only if warranted, emit `RunReport`.
    - _Note: distill-both-ways, Mem0 write, and `RunReport` are done; the "update KB if
      warranted" write path (`add_policy`) exists but is not yet wired to a node tool._
    - _Requirements: 7.1, 7.5, 8.1, 8.4_

- [x] 17. CLI front door
  - `autodev run --ticket <file> --repo <path>`; validate into `Ticket`; call
    `run_ticket()`; print `RunReport`; refusal reported as a typed report.
  - _Requirements: 9.1, 5.4, 8.1_

- [x] 18. FastMCP server
  - `run_ticket` tool wrapping the same `run_ticket()`; contracts validate tool
    input/output; taxonomy / last-N-reports resource; "file a ticket" prompt.
  - Node-lifecycle status + telemetry span HTTP endpoints.
  - _Requirements: 9.2, 9.3, 9.4, 10.3_

- [x] 19. Target app + seed tickets (fixtures)
  - Self-contained ~200-400 LOC app with a one-command test suite.
  - >= 4 tickets: 2 bugs (one with a reproducing failing test, one without), 2 features
    (one with an acceptance test, one spec-only), and 1 that must be refused.
  - _Requirements: 2.1, 2.4, 5.1, 5.3_

- [ ] 20. Quality gates + CI
  - `ruff check` (zero warnings), `pyright` (zero errors), `pytest` + Hypothesis green
    locally; minimal GitHub Actions running the same three checks.
  - _Requirements: 11.1, 11.2, 11.3_

- [ ] 21. Before/after self-improvement demo
  - Script/logs showing a bug resolved, a feature resolved, a ticket refused, and a later
    run doing better from a stored lesson (including a lesson from a run that didn't go
    well changing behavior).
  - _Requirements: 7.3_
