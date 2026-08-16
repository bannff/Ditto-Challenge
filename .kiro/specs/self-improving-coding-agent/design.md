# Design

## Overview

`autodev` resolves a typed, untrusted ticket against a target repository and reports the
outcome, learning a durable lesson each run. The design separates **use-case-agnostic
fixed plumbing** (graph mechanics, eval-checkpoint + self-heal loop, circuit breakers,
Mem0 memory, chromadb KB, FastMCP server, telemetry, scrubbing, Pydantic contracts) from
a **thin swap-in node layer** (the node definitions: swarm prompts + models, skill +
steering content, tools, and evaluators per node). The plumbing takes node definitions as
data/config so swapping the use case is swapping data, not rewriting plumbing.

The reference use case — Discover -> Implement -> Verify -> Learn — exercises the plumbing
end-to-end. It is the reference for building and testing the machinery, not a hardcoded
requirement of it.

Bedrock is the only LLM backend; model IDs and region come from typed settings sourced
from the environment. Every graded hard requirement (trust boundary, test-gate, bounded
runs, isolation, refusal, self-improvement, structured report) is enforced in code at a
tool or plumbing boundary, not in a prompt.

## Architecture

### Two layers

- **Fixed plumbing** — the `GraphBuilder` graph, the per-node eval checkpoint and
  self-heal redo, circuit breakers via Swarm bounds, Mem0, chromadb, FastMCP, telemetry,
  scrubbing, contracts. Built to consume node definitions as config.
- **Swap-in node layer** — each node's `Swarm` (agent prompts + models), its
  `AgentSkills` + `LLMSteeringHandler` content, its tools, and its evaluators. Node count
  and sequence are data; the machinery running them is untouched.

### Graph of domain-swarm nodes

- **Outer workflow**: a static `strands.multiagent.GraphBuilder` graph. Node count is
  driven by the use case; the reference is four (Discover, Implement, Verify, Learn).
- **Every node is a `Swarm`** with `max_handoffs`, `max_iterations`, and
  `execution_timeout` set explicitly per node.
- **Ensemble**: at least one node (Discover or Implement) uses two distinct
  `BedrockModel` instances across its swarm — a capable builder model and an independent
  reviewer model. Disagreement is evidence for a repair pass within that swarm.
- **Final node** (Learn): a `Swarm` guided by a skill file that reads the eval results
  collected from every node, closes out the run (store outcome to the ledger), distills a
  lesson, files it to Mem0, and updates the policy KB only if warranted — agentically, not
  via hardcoded branching.

### Eval checkpoint + self-heal

- When a node's `Swarm` finishes, `strands_evals` LLM-as-a-judge evaluators run against
  its work (Bedrock calls), producing a score + reason. This is the checkpoint gate.
- Good score -> advance. Poor score -> redo the node (self-heal). Each node's eval
  results carry forward into the final node's context.
- On a failed checkpoint, a `strands_evals` detector (`diagnose_session`, gated by
  `DiagnosisTrigger.ON_FAILURE`) diagnoses which span failed and why and recommends a
  fix. That recommendation is fed into the redo pass and is what the final node distills
  into a "what-to-avoid" lesson.
- Evaluator selection is per node from the indicators that node cares about
  (`ToolSelectionAccuracy`, `ToolParameterAccuracy`, `Correctness`, `Faithfulness`,
  `Helpfulness`, `Trajectory`, `GoalSuccessRate`). `OutputEvaluator` (OUTPUT_LEVEL) with
  a custom rubric is the no-Session fallback for a fast first checkpoint.

### Circuit breaker

The Swarm limits ARE the circuit breaker. `max_handoffs` / `max_iterations` /
`execution_timeout` trip on a stuck or looping node; when tripped, the node falls back to
a local/stubbed model call instead of retrying against Bedrock indefinitely. This is the
stop condition for the self-heal redo loop — a node redoes only until its own Swarm limits
trip.

### Session/trace wiring (shared prerequisite)

Trace-level evaluators and detectors need a `Session` built from OpenTelemetry traces, not
a plain string. `StrandsInMemorySessionMapper` over captured spans is built once and
powers both the trace-level evaluators and the detectors — the foundation the whole
checkpoint mechanic sits on.

## Components and Interfaces

Each module owns exactly one concern, mirroring the repo's existing file boundaries.

- **contracts** (`contracts.py`, built) — Pydantic v2 models validated at every
  ingress/egress boundary; `schema_version` active. Also used to validate every MCP tool
  input/output.
- **settings** (`settings.py`, built) — one typed `pydantic-settings` `get_settings()`
  singleton; Bedrock model IDs/region and secrets sourced from env/`.env`, secrets wrapped
  in `SecretStr`.
- **scrub** (`scrub.py`, built) — regex `scrub_text()` redacting structured PII/secrets
  (SSNs, card numbers, AWS keys, generic secrets, DOB-to-year). Applied at every
  persistence/log call site (Mem0 `add()`, chromadb insert, ledger write).
- **taxonomy** (`taxonomy.py`, built) — `knowledge/taxonomy.yaml` loaded into the
  `Taxonomy` model; fixed tag lookup of invariants and acceptance hints. Distinct from the
  KB; not a vector store.
- **ledger** (`ledger.py`, built) — SQLite durable run history (run records, evidence,
  outcome). Not vector data. Holds the inert, clearly-labelled `schema_version` migration
  seam.
- **kb** (`kb.py`, built) — chromadb `PersistentClient`, one collection seeded from a
  short security policy doc; `query_policy` similarity search. Read-mostly; final node may
  add a durable fact.
- **memory** — Mem0 via native `strands_tools.mem0_memory`, `llm`/`embedder` configured
  `provider: "aws_bedrock"`, local FAISS vector store. Write-both-outcomes lesson policy;
  primes Discover via `search` before a run.
- **worktree** — creates/tears down the isolated branch/worktree per run; enforces that
  all writes and commands stay inside it; leaves the tree clean on exit. The trust-boundary
  jail.
- **telemetry + session mapper** — `StrandsTelemetry().setup_console_exporter()`; in-memory
  span ring buffer; `StrandsInMemorySessionMapper` producing the `Session` for evaluators
  and detectors. Separate from the UI status stream.
- **node config** — the data shape describing a node: its swarm (agent prompts + models),
  skill + steering content, tools, and evaluators. The swap-in layer; consumed by the
  graph builder.
- **graph** — shared machinery that builds the `GraphBuilder` graph from node configs,
  runs each node's swarm, runs the checkpoint, drives the self-heal redo within circuit
  breaker bounds, and threads eval results forward.
- **reference nodes** — the Discover / Implement / Verify / Learn node definitions
  (prompts, models, skills, steering, tools, evaluators) that prove the plumbing.
- **MCP server** — one FastMCP server: `run_ticket` tool (wraps the CLI's `run_ticket()`),
  taxonomy / last-N-reports resource, "file a ticket" prompt. Node-lifecycle status +
  telemetry span HTTP endpoints for the optional UI.
- **CLI** — `autodev run --ticket <file> --repo <path>`; parses/validates the ticket into
  a `Ticket`, calls `run_ticket()`, prints the `RunReport`.

## Data Models

Pydantic v2, validated at every boundary; `schema_version` real and active.

- **Ticket** — `id`, `repository`, `request`, `domain`, `acceptance_command`,
  `created_at`. The untrusted input.
- **EvaluatorScore** — `evaluator`, `score`, `threshold`, `passed`, `reason`. One
  evaluator's checkpoint result.
- **Verdict** — `schema_version`, `node`, `passed`, `attempts`, `scores:
  list[EvaluatorScore]`, `diagnosis`. Per-node checkpoint outcome including redo count and
  detector diagnosis.
- **AcceptanceResult** — `command`, `exit_code`, `output_tail`, computed `passed`
  (exit_code == 0). The test-gate evidence.
- **Lesson** — `schema_version`, `ticket_id`, `outcome`, `content`, `tags`, `created_at`.
  The distilled, scrubbed memory record filed to Mem0 (independent of Mem0's internal
  storage).
- **RunReport** — `schema_version`, `run_id`, `ticket`, `branch`, `worktree`, `outcome`,
  `verdicts: list[Verdict]`, `acceptance`, `evidence`, `lesson`, `created_at`. The
  structured egress artifact.
- **Taxonomy / TaxonomyTag** — `Taxonomy(version, tags: dict[str, TaxonomyTag])`;
  `TaxonomyTag(name, invariants, acceptance_hints)`. Fixed domain rules.
- **Outcome** / **NodeState** — enums: `success|failure|inconclusive` and
  `pending|running|complete|redo|failed`.

## Error Handling

- **Budget ceiling / stuck node** — the Swarm bounds trip the circuit breaker; the node
  falls back to a local/stubbed model and stops redoing. The run degrades gracefully:
  partial progress is reported, no change is half-applied.
- **Failed test-gate** — the change is reverted/abandoned, the outcome is `failure` or
  `inconclusive`, the working tree is left clean, and the `RunReport` states why.
- **Failed checkpoint** — the detector diagnoses the failure; the redo pass is told what
  went wrong; if redos exhaust the bounds, the breaker takes over.
- **Refusal** — unsafe/out-of-scope/underspecified tickets short-circuit to a refusal
  `RunReport` with a reason and no repo modification.
- **Trust-boundary violation** — any path/command resolving outside the worktree, any
  `shell=True`, or any attempt to touch `main` or disable checks is rejected at the tool
  layer.
- **Untrusted content** — file/command/web/ticket content is treated as data; embedded
  "instructions" are ignored.
- **Persistence** — secrets/PII are scrubbed at every ledger/Mem0/chromadb/telemetry write
  so a leak cannot cross the persistence boundary.

## Testing Strategy

- **Property tests (Hypothesis)** — contract round-trips (`Ticket`, `RunReport`,
  `Verdict`, `EvaluatorScore`, `Lesson`), scrub patterns (secrets/PII never survive
  `scrub_text`), and `Verdict`/`EvaluatorScore` shape validation. (Built for contracts,
  scrub, taxonomy, ledger, kb.)
- **Safety-boundary tests** — a hostile ticket cannot escape the worktree, invoke a shell,
  reach `main`, or disable its checks; refusal path returns a clean report.
- **Test-gate tests** — a change that breaks the target's suite is reverted and reported as
  not resolved, tree left clean.
- **Budget tests** — a looping node trips its Swarm bounds and the circuit breaker fires
  rather than looping unbounded.
- **Self-improvement before/after** — run a ticket, let it learn, then show a later
  related run retrieving the lesson and doing better (including a lesson from a run that
  didn't go well changing later behavior).
- **Quality gates** — `ruff check` (zero warnings), `pyright` (zero errors), `pytest` +
  Hypothesis green, run before every commit and in CI.
