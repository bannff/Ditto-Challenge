# DESIGN — autodev

`autodev` - an agentic workflow - takes a typed, untrusted **ticket**, resolves it against a target repo on an isolated git worktree, self-verifies with the target's **own tests**, reports a structured `RunReport`, and stores durable **lesson**. 

The agentic workflow is elastic (it's a graph of swarms) and leverages several best practices (consensus/ensemble technique/self reflection/adversarial prompting/evaluation mechanisms). It's data driven - has a taxonomy enforced through PyndanticV2 model schema (saves models tokens) - a node is data (prompt,
models, skills, steering, tools, evaluators), not code. LLM cals through Bedrock, via the Strands Orchestration Suite.

```
ticket ─▶ refusal gate (deterministic, pre-worktree)
             │ pass
             ▼
   ┌───────────────── per-run git worktree (branch autodev/<run_id>) ─────────────────┐
   │  DISCOVER ──▶ IMPLEMENT ──▶ VERIFY ──▶ LEARN   (each a 3-agent trio)               │
   │     │           │            │           │                                        │
   │     ▼           ▼            ▼           ▼                                         │
   │  eval checkpoint after each node ─ pass? advance : self-heal                       │
   │     └─ retry (diagnosis-informed, max_redos) ─▶ circuit breaker (degrade + stop)   │
   │  every decision above appends a block to the run's hash chain (§9)                │
   └───────────────────────────────────────────────────────────────────────────────────┘
             │
             ▼
   test-gate (target's real exit code) ─ pass → commit + RunReport ; fail → revert, clean tree
             │
             ▼
   provenance gate ─ chain verifies + record complete + not degraded ? store lesson : refuse
```

## 1. Workflow — Strands graph-of-swarms
**Graph for events that must happen; Swarm for emergent intelligence.** A static sequence
**Discover → Implement → Verify → Learn**, expressed natively as a Strands `GraphBuilder`
graph. Each stage is a `MultiAgentBase` node that runs a `Swarm` attempt — a
**3-agent trio** (builder + reviewer + adversary) that converges on one result. Swarms are elastic and can scale to zero or collab back and forth on harder problems. They are self bounded by inbuilt circuit breakers (see below).
Nodes have eval checkpoints (Strands-Evals Suite - also below) that always run, carrying the `Verdict` as structured node state. Edges read that verdict
directly: a self-loop re-runs a failed node, an advance edge moves on when it passes — no
routing decision is ever parsed out of agent text.
- **Circuit breakers [bounding]** — Swarm's built-in `max_handoffs` / `max_iterations` /
  `execution_timeout` / `node_timeout` cap every attempt, and its repetitive-handoff detector
  (off by default upstream) trips a swarm whose recent turns cycle among too few agents; a
  whole-run wall-clock deadline is checked between nodes (degrade cleanly, never mid-change),
  with the graph's `set_max_node_executions` and `set_execution_timeout` as SDK-level backstops. A bounded-out
  swarm returns non-`COMPLETED` and **fails the gate regardless of judge score**.
- **Informed retry [resilience]** — on a failed checkpoint, the self-loop edge re-runs the node
  with the detector's diagnosis injected (`max_redos`); if retries exhaust, no edge fires and the
  run degrades gracefully with a fixed message — no fork, no loop. (We tried a fork to a stronger
  model; live-testing showed it fired on false failures and timed out, so retry + breaker is the
  leaner, more reliable ladder.)

## 2. Agents — skills + memory + knowledge base 
Every agent carries node-scoped plugins at the agent plane: `AgentSkills` (domain how-to),
read-only **memory** recall, and **KB** policy lookup. Implement also runs a live
`LLMSteeringHandler` that guides/interrupts unsafe tool calls as they happen.
- **Memory + KB are both vector stores [semantic search + embeddings]** — Mem0/FAISS for lessons,
  chromadb for policy; both queried by Bedrock-embedded similarity search.
- **Multi-model, multi-vendor swarms [ensemble technique]** — Claude Haiku (builder) + Amazon Nova (reviewer) [Frugal]
  + Claude Sonnet (adversary); disagreement across models/vendors is the repair signal. Roles are
  env-driven (`BEDROCK_*_MODEL_ID`) — swap the trio in config, not code.

## 3. Eval harness — real-time, per-node, self-healing
After each node an `strands_evals` LLM-as-judge checkpoint scores the result and gates the
self-heal loop; on failure `diagnose_session` names which span failed, why, and the fix, which
feeds the retry. 2–3 out-of-box evaluators per node, chosen for the node's use case and scored off the
captured OTEL `Session`: Discover — faithfulness + response-relevance + tool-selection; Implement
— **goal-success** + faithfulness + tool-param accuracy; Verify — faithfulness + coherence; Learn
— a one-lesson rubric + conciseness. Implement uses `GoalSuccessRate` (judges goal attainment over
the whole session) rather than a "did this attempt edit a file" rubric, so an already-correct
worktree after a retry passes instead of false-failing.

## 4. Deterministic safety — enforced in code, not prompts
- **Input sanitization** — refusal is a deterministic denylist pre-check (unsafe/underspecified),
  *before* any worktree exists; `acceptance_command` is scanned too.
- **Regex scrubbing** — `scrub_text` redacts secrets/PII (private keys, tokens, cloud keys, SSN)
  at every write boundary (ledger, Mem0). Best-effort by shape, a second layer — not a guarantee.
- **Process isolation** — worktree jail (`safe_path()` refuses any escape; git hooks/fsmonitor
  disabled); the test subprocess runs with AWS/Bedrock creds stripped and `HOME` emptied, so
  untrusted test code can't read our keys; acceptance command is allowlisted, shell-free, no inline
  code. Test-gate is the target's **real exit code**; fail → revert to a clean tree, never ship red.
  Honest limit: this is credential *isolation*, not an FS/egress sandbox (see §Uplevel).

## 5. Recursive learning — memory that changes later runs [Embeded Vector Store]
One scrubbed lesson per run (both outcomes), written by code with `infer=False` (verbatim,
deterministic), gated through Learn alone and **deduped** (skip if similarity ≥ 0.95) for
junk-resistance. A run must also *earn* the right to teach — see the provenance gate in §9.
Reads: Discover is primed with recalled lessons before it plans, and Learn
distills from every stage's output plus the per-node verdicts — not just its predecessor's
summary — so the lesson reflects the whole run. **Proof
(live, same ticket):** a business rule that exists *only* in a prior-failure lesson — "reorder
lists exclude discontinued items" — is absent from the repo. Control (empty memory) passes the
gate but misses the rule; primed (lesson present) recalls and satisfies it. Memory supplies
knowledge absent from the code — a measured behavior change, not an append-only log.

## 6. Testing & contracts
- **pyright** — zero type errors across `src` + `tests`.
- **hypothesis** — property tests for the invariants that must hold on any input (scrub
  idempotency/coverage, contract round-trips, refusal).
- **Pydantic v2** — every ingress/egress is a validated model (`Ticket`, `RunReport`, `Verdict`,
  `Lesson`); `pydantic-settings` is the single typed config source. `ruff` gates lint/imports.

## 7. Taxonomy — extendable contract
Domain tags (invariants + acceptance hints) load from `taxonomy.yaml` into a typed `Taxonomy`
contract and are exposed as an MCP resource. Adding a domain is data, not code.

## 8. MCP surface
A FastMCP server exposes the whole loop as a **tool** (`run_ticket`), **resources** (current
taxonomy, recent run history), and a **prompt** that shapes free text into a valid `Ticket` — so
an MCP client (or the CLI) drives real ticket resolution while all safety/gate logic stays in the
workflow.

## 9. Hash-chained ledger — the record controls behavior [tamper-evident, offline]
The run ledger is not just history; it decides something. Every decision a run makes —
node attempt, tool call, verdict, breaker trip, acceptance gate, lesson — appends a **block**
to a per-run hash chain (`content_hash = sha256(payload ‖ prev_hash)`, plus a recorded head
pinning the chain's length). Chains are **per run**, never global: a single head would
serialize concurrent tickets. Capture rides seams that already exist — the SDK's
`AfterToolCallEvent` hook for tool calls, the workflow's status stream for node lifecycle —
so the engine never learns a ledger exists.

- **Provenance-gated memory writes.** Before a lesson is stored, memory asks the ledger
  whether the run may teach it anything. Three signals, all required, each covering the
  others' blind spot: the chain verifies, every attempted block actually landed (an *omitted*
  block leaves no gap, so the recorder counts its own dropped writes), and the workflow
  didn't degrade (first-hand, not laundered through the ledger). An honest **failure still
  teaches** — those are the valuable lessons; only a run whose record can't be trusted is
  refused, and the refusal is itself a block. This is what keeps a breaker-tripped run's
  guesswork out of the store where it would steer every later run.
- **Offline replay.** `autodev replay <run_id>` walks a chain with no model calls, no
  network, and no repo access: it recomputes every hash, names the exact block where a chain
  broke, and prints the decision sequence. An audit that works on a disconnected machine.
  Because replay is the audit surface, control characters are escaped inside the block
  (before hashing) — untrusted text carrying `CR`/ANSI could otherwise scroll back over the
  VERIFIED/BROKEN verdict a human is reading.
- **Rollback references git, it doesn't reimplement it.** Git is already a content-addressed
  Merkle DAG and works fully offline, so a block carries the worktree's commit hash instead
  of a copy of the tree: the ledger proves *which* state was verified, `git` restores it.
- **Honest limits.** This detects modification, mid-chain deletion, and truncation of a
  *stored* record. It is **not tamper-proof**: blocks are unsigned (no key material here) and
  the writer is trusted, so evidence forged at the source is chain-valid — see the acceptance
  gate's `conftest.py` hole. File *content* is recorded as size + digest, never verbatim, so a
  repo secret matching no scrub pattern can't ride into a durable row. Signing blocks with a
  per-node key (the mesh-native version) and tier-2 replay that re-executes recorded model
  I/O are both design-only.

## Uplevel — production (design only)
Seams exist; production swaps backends without touching the loop. Theme: **privilege separation
by process**.
- **Sandbox the worker** — run test execution in a container/microVM (gVisor/Firecracker), no
  ambient creds, no egress except a scoped model endpoint. This closes the §4 honest limit
  (same-UID test code can currently read host files by absolute path / reach the network).
- **Scoped worker credential** — short-lived STS role limited to `bedrock:InvokeModel` in a
  low-privilege account; keep ledger/memory/orchestration identity out of the worker process.
- **Memory at scale** — hot/cold lifecycle + poisoning defenses (decay, review) beyond
  single-write + dedup.
- **Long-running resume** — each swarm persists its session (`FileSessionManager`); add a typed
  outer checkpoint (node, attempt, session id, verdicts, worktree identity, budget) +
  `resume(run_id)` that reopens the worktree. Strands resume is at-least-once, so steps must be
  idempotent — the worktree jail + clean-revert already give that. Not built: crash-resume isn't
  graded, and the isolation/idempotency asked for is covered.
- **Schema-migration adapter** — records are version-stamped today; a long-lived deployment adds an
  adapter that upgrades an older record to the current shape before validation, so an in-flight v1
  workflow still loads after a v2 deploy. Stamped now, translation deferred until a v2 exists
  (rather than ship an inert no-op).
- **Signed blocks + tier-2 replay** — sign each block with a per-node key so the chain is
  tamper-evident across an untrusted mesh, not just against later edits to our own database;
  and record model I/O so replay can re-*execute* a run's decisions offline rather than only
  audit them. Both are seams off §9, deliberately unbuilt: signing needs key management, and
  full re-execution needs every model call captured.
- **Observability** — swap the console span exporter for an OTLP collector (one line).
- **Deploy safety** — a real deploy → verify → rollback stage after the test-gate.

## Cut for time
The Next.js UI (backend channels exist — status stream + span endpoint; CLI and FastMCP server are
the working front doors) and the standalone Eval SOP artifact. The self-improvement demo is real
but not fully deterministic: the write side is deterministic; the read-and-apply side is
model-mediated.
