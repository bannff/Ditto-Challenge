# Requirements

## Introduction

`autodev` is a self-improving coding agent: given a typed, untrusted ticket, it
autonomously resolves the work item against a target repository and reports the outcome.
For each ticket the system classifies the work (bug / feature / refuse), understands the
repo, acts on an isolated branch/worktree (never `main`), self-verifies by running the
target's test suite, reports a structured result, and distills a durable lesson that
measurably changes later runs.

The machinery is use-case-agnostic fixed plumbing plus a thin swap-in node layer: a
graph of Strands `Swarm` nodes on Bedrock, with an LLM-as-a-judge eval checkpoint and a
self-heal redo loop after every node, bounded by each node's Swarm limits acting as the
circuit breaker. These requirements are derived from `Reqs.md` (the graded hard
requirements and rubric) and the acceptance demo in `SPEC.md`. Grading weight —
safe autonomy 35%, agent-loop design 30%, self-improvement 20%, judgment and
communication 15% — is reflected in the ordering and emphasis below.

## Requirements

### Requirement 1: Enforced trust boundary against untrusted ticket text

**User Story:** As a repository owner, I want ticket text treated as untrusted input
that can only act through guarded tools, so that a hostile ticket cannot exfiltrate
secrets, run destructive commands, disable checks, escalate privileges, or act outside
the target repo.

#### Acceptance Criteria

1. WHEN the agent takes any action THEN the system SHALL route it through an explicit
   tool, and each tool SHALL enforce its own safety check rather than relying on prompt
   instructions.
2. WHEN a tool receives a file path or command derived from ticket text THEN the system
   SHALL confine all writes and command execution to the target worktree and SHALL
   reject any path or operation that resolves outside it.
3. WHEN a subprocess is invoked THEN the system SHALL pass arguments as a list without
   `shell=True` and SHALL validate or sanitize any ticket-derived value before it
   reaches a shell, path, or query.
4. WHEN text is written to the ledger, telemetry, Mem0, or the KB THEN the system SHALL
   apply `scrub_text` so that secrets and structured PII are redacted at every
   persistence and log boundary.
5. WHEN a secret or environment-varying value (model ID, region, credentials) is needed
   THEN the system SHALL read it from settings sourced from the environment and SHALL
   NOT contain a literal secret in source.
6. IF a ticket instructs the agent to operate on `main`, escape the worktree, or disable
   its own verification THEN the system SHALL refuse that action rather than perform it.

### Requirement 2: Test-gate before a change is reported as done

**User Story:** As a reviewer, I want no change marked resolved unless the target's tests
pass, so that a change that breaks tests is never shipped and the working tree is always
left clean.

#### Acceptance Criteria

1. WHEN a change has been applied THEN the system SHALL run the target repo's acceptance
   command and SHALL capture its exit code and output.
2. IF the acceptance command exits non-zero THEN the system SHALL treat the run as not
   resolved, revert/abandon the change, and report the failure with its reason.
3. WHEN a run ends for any reason THEN the system SHALL leave the working tree clean.
4. WHEN the ticket warrants it THEN the system SHALL add or extend tests as part of the
   change, and those tests SHALL be included in the gated run.
5. WHEN the acceptance result is recorded THEN the system SHALL derive `passed` from an
   exit code of zero and SHALL surface it in the `RunReport`.

### Requirement 3: Bounded runs with graceful degradation

**User Story:** As an operator, I want a hard ceiling on iterations, wall-clock, and
tokens, so that a confused agent cannot loop forever or burn unbounded cost.

#### Acceptance Criteria

1. WHEN each node's `Swarm` is constructed THEN the system SHALL set `max_handoffs`,
   `max_iterations`, and `execution_timeout` explicitly per node.
2. WHEN a node reaches its Swarm limit THEN the system SHALL trip the circuit breaker and
   fall back to a local/stubbed model call instead of retrying against Bedrock
   indefinitely.
3. WHEN the self-heal redo loop runs THEN the system SHALL bound redos by the node's
   Swarm limits and SHALL stop redoing once those limits trip.
4. IF a budget ceiling is hit THEN the system SHALL degrade gracefully by reporting
   partial progress and SHALL NOT half-apply a change.

### Requirement 4: Idempotent and isolated execution

**User Story:** As an operator running many tickets, I want each run isolated and
re-runnable, so that re-running a ticket does not corrupt state and concurrent tickets do
not clobber each other.

#### Acceptance Criteria

1. WHEN a ticket run starts THEN the system SHALL create a dedicated branch/worktree for
   that run and SHALL perform all changes there, never on `main`.
2. WHEN the same ticket is run again THEN the system SHALL NOT corrupt prior state or
   leave residue that changes the outcome.
3. WHEN multiple tickets run concurrently THEN the system SHALL keep each run's worktree,
   branch, and persisted records separate so they do not clobber one another.
4. WHEN a run completes THEN the system SHALL record a durable, structured entry in the
   SQLite ledger keyed to that run.

### Requirement 5: Refusal as a valid, correct outcome

**User Story:** As a repository owner, I want the agent to decline tickets that are
unsafe, out of scope, or underspecified, so that refusal is a considered path rather than
a forced, risky attempt.

#### Acceptance Criteria

1. WHEN a ticket is unsafe, out of scope, or underspecified THEN the system SHALL refuse
   it and SHALL report a clear reason.
2. WHEN a ticket is refused THEN the system SHALL NOT modify the target repo and SHALL
   leave the working tree clean.
3. WHEN the work item is received THEN the system SHALL classify it as bug, feature, or
   refuse before acting.
4. WHEN a refusal occurs THEN the system SHALL emit a typed `RunReport` describing the
   refusal outcome, the same as any other run.

### Requirement 6: Understand -> act -> verify agent loop with real recovery

**User Story:** As a reviewer, I want a clear understand -> plan -> act -> verify ->
(retry|finish) loop that learns from a failed step, so that the agent recovers instead of
blindly repeating a bad attempt.

#### Acceptance Criteria

1. WHEN a ticket is accepted THEN the system SHALL run a static graph of `Swarm` nodes
   (e.g. Discover -> Implement -> Verify -> Learn) where each node performs one stage of
   the loop.
2. WHEN a node's `Swarm` finishes THEN the system SHALL run an LLM-as-a-judge eval
   checkpoint against that node's work, producing a score and reason.
3. IF a node's checkpoint does not pass THEN the system SHALL redo that node before the
   graph advances, and SHALL run a detector on failure to diagnose which span failed and
   why.
4. WHEN a node is redone THEN the system SHALL feed the detector's fix recommendation
   into the redo pass so the next attempt is informed by the last failure.
5. WHEN a node's checkpoint passes THEN the system SHALL advance the graph to the next
   node.
6. WHEN each node completes THEN the system SHALL carry its eval results forward into the
   final node's context.
7. WHEN an agent acts THEN the system SHALL do so via agents carrying `AgentSkills` and
   `LLMSteeringHandler` plugins scoped per node, not via a bare `Agent(system_prompt=...)`.

### Requirement 7: Durable self-improvement that changes later runs

**User Story:** As a repository owner, I want a durable memory that measurably changes
behavior across runs, so that later runs on similar tickets do better because of what was
stored, not just accumulate a log.

#### Acceptance Criteria

1. WHEN a run ends THEN the final node SHALL distill a lesson for BOTH outcomes — a
   pattern that worked on success, and what happened and why on failure — and SHALL file
   it via Mem0 backed by Bedrock.
2. WHEN a new ticket arrives THEN the system SHALL prime the Discover node with a Mem0
   similarity lookup for relevant prior lessons before the graph runs.
3. WHEN a lesson from a prior run is retrieved THEN the system SHALL demonstrably change
   the later run's behavior (fewer iterations, avoids a repeat mistake, or reuses a
   discovered fact).
4. WHEN a lesson is written THEN the system SHALL apply a write policy that stores only
   what is worth remembering and SHALL scrub the content before persistence to keep
   memory junk- and secret-resistant.
5. WHEN a run surfaces a genuinely new, durable policy-relevant fact THEN the final node
   MAY add it to the chromadb policy KB using the same distill-then-file pattern; most
   runs SHALL only read from the KB.

### Requirement 8: Structured RunReport and evidence

**User Story:** As a reviewer, I want a typed, structured result for every run, so that I
can see what the agent did, the diff, the test output, and a plain-English summary.

#### Acceptance Criteria

1. WHEN a run completes THEN the system SHALL emit a `RunReport` validated by the
   Pydantic contracts, carrying run id, ticket, branch/worktree, outcome, per-node
   verdicts, acceptance result, evidence, and the distilled lesson.
2. WHEN a run fails THEN the system SHALL report why it failed and confirm the repo was
   left clean.
3. WHEN any contract is persisted or read back THEN the system SHALL carry an active
   `schema_version` field.
4. WHEN a report is produced THEN it SHALL include a plain-English summary a reviewer
   could act on.

### Requirement 9: External control surface (CLI and MCP)

**User Story:** As a user, I want to trigger a run from the CLI or an MCP client, so that
the same workflow is reachable through more than one front door.

#### Acceptance Criteria

1. WHEN a user runs `autodev run --ticket <file> --repo <path>` THEN the system SHALL
   execute the full graph and emit the `RunReport`.
2. WHEN an MCP client calls the `run_ticket` tool THEN the system SHALL invoke the same
   `run_ticket()` function the CLI uses and SHALL return the `RunReport` as JSON.
3. WHEN an MCP tool receives input or returns output THEN the system SHALL validate it
   through the same Pydantic contracts, with no separate contract layer.
4. WHEN an MCP client requests it THEN the system SHALL expose the taxonomy and/or the
   last N run reports as a readable MCP resource and SHALL offer a "file a ticket" prompt
   template.

### Requirement 10: Telemetry and node-lifecycle status feed

**User Story:** As an operator, I want observability spans and a live node-lifecycle
stream, so that I can watch the run progress and feed trace-level evaluators.

#### Acceptance Criteria

1. WHEN the workflow runs THEN the system SHALL emit OTEL spans via the console exporter
   for observability.
2. WHEN trace-level evaluators or detectors run THEN the system SHALL supply a `Session`
   built from captured spans via `StrandsInMemorySessionMapper`.
3. WHEN each node starts, completes, redoes, or fails THEN the system SHALL emit a
   `{node, state, eval_score, timestamp}` status event on a channel separate from the
   OTEL span pipe.
4. WHEN documenting deployment THEN the system SHALL note that a real OTLP collector is a
   one-line swap and SHALL NOT require standing one up for the submission.

### Requirement 11: Quality gates

**User Story:** As a maintainer, I want lint, type, and property-test gates enforced, so
that the codebase stays clean and the contracts and scrub patterns are verified.

#### Acceptance Criteria

1. WHEN code is committed THEN the system SHALL pass `ruff check` with zero warnings and
   `pyright` with zero errors.
2. WHEN tests run THEN the system SHALL include Hypothesis property tests for contract
   round-trips, scrub patterns, and `Verdict`/`EvaluatorScore` shape validation, and they
   SHALL be green.
3. WHEN a feature or bug fix ships THEN it SHALL ship with a test.
