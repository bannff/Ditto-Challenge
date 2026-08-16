# Open findings

From adversarial review of the acceptance-gate hardening (security-engineer + qa-tester,
each reproduced against real code). Ordered by what I'd fix first. Every item has a test
already written; the `xfail(strict=True)` ones flip to failures the moment they're fixed.

## Root causes, not nine unrelated bugs

1. **A fail-open default in a fail-closed table** — `RunnerPolicy.allows_paths` defaults to
   `True`, so the interpreter row silently permits positionals. Causes #1 and #4.
2. **No filesystem sandbox** — the gate runs repo-authored code as our own user, so it can
   read/write anything we can. Causes #6 and #7; only the Uplevel process sandbox fixes it.
3. **Worktrees live inside the agent's own repo** (`.data/worktrees/`) — pytest's rootdir
   therefore resolves to *our* repo and it loads config from outside the jail. Causes #5.
4. **No `except` in `run_ticket`** — a refused-but-not-denylisted gate raises instead of
   returning `REFUSED`. Causes #8.

## 1. CRITICAL — an interpreter accepts a script, so the gate can run anything and still go green

`python3 bench.py` passes the policy: `allows_paths` defaults `True` and `_INTERPRETER`
never overrides it. Reproduced end to end — exit 0, wrote outside the worktree, read
`~/.aws` — a green gate that ran no test, so an unverified change ships.
Also allows bare `python3` (reads EOF from `stdin=DEVNULL`, exits 0).
**Fix:** `allows_paths=False` on `_INTERPRETER` and require `-m <module>`. One data change.
**Breaks:** nothing shipped. Tests: `tests/test_acceptance_policy.py` (2 xfail).

## 2. HIGH — the timeout can hang forever, so the budget ceiling isn't enforced

In `Worktree.run`, the `communicate()` *after* `_kill_group` has no timeout. A detached
grandchild holding the stdout pipe blocks it indefinitely (verified: still blocked at 40s
with `timeout=3`). A test fixture that starts a background server triggers this innocently.
Introduced by this session's hardening.
**Fix:** `communicate(timeout=5)` and accept partial output, or give `run()` a
`TemporaryFile` for stdout so there is no pipe to hold hostage.
**Breaks:** nothing.

## 3. HIGH — one shared HOME lets a hostile ticket poison later runs

`_isolated_home()` memoises a single temp dir per process, so gate code can plant
`$HOME/.../usercustomize.py` that every subsequent run auto-imports. Reproduced across two
runs. Violates "concurrent tickets don't clobber each other".
**Fix:** `PYTHONNOUSERSITE=1` (verified to neutralise it) plus a per-worktree HOME created
in `Worktree.create` and removed in `remove`. Add `PYTHONDONTWRITEBYTECODE=1`.
**Breaks:** nothing.

## 4. HIGH — `python3 -m unittest <dotted.name>` imports code from outside the worktree

The policy checks the positional as a *path* (`this` -> `<jail>/this`, absent but inside,
so it passes); unittest resolves it as an importable module via `sys.path` — stdlib,
site-packages, and our own editable `src/`. Verified: `-m unittest this` prints the Zen of
Python; `-m unittest self_improving_coding_agent.settings` imports our source. A straight
bypass of the `-m` check that refuses `python -m this`.
**Fix:** same as #1, but note `allows_paths=False` alone also refuses the legitimate
`python3 -m unittest discover`. Cleanest option: drop `unittest` and let pytest be the only
runner — that closes #1, #4 and the bare-interpreter case together.
Tests: `tests/test_acceptance_policy.py` (1 xfail, 3 params).

## 5. HIGH — the gate loads `conftest.py` from outside the worktree, and it persists

Worktrees sit under `<agent repo>/.data/worktrees/`, so pytest's rootdir is the agent repo
and it imports every `conftest.py` between there and the worktree. Verified both halves: a
target test plants a `conftest.py` two levels up, and a *later, unrelated* run's gate
executes it. One hostile ticket infects every subsequent gate. Our `addopts = -q` already
leaks in the same way.
**Fix:** move `worktrees_dir` outside the agent repo (e.g. a temp root), and/or pin
`rootdir`/`confcutdir` to the worktree. Config-level.
Test: `breach/deterministic/test_hostile_tickets.py` (xfail).

## 6. HIGH — repo-resident config controls the gate's verdict

With the *validated* command `pytest tests` on a red suite: adding `pytest.ini` with
`addopts = --co` gives exit 0; a `conftest.py` `pytest_runtest_makereport` hook rewrites
failures to passes. Both files are writable by the agent's own jailed tools and neither
trips the refusal denylist. Argv validation cannot see any of it.
**Fix (cheapest first):** delete `--co`/`--collect-only` from `_PYTEST.flags` (a gate that
cannot fail is not a gate); inject `-o addopts=` ourselves (verified to restore exit 1);
require evidence tests actually ran (parse the summary, or compare pass-count against the
base commit); refuse a diff that touches gate-controlling paths unless the ticket is about
them.
**Weakest remaining link even after all of that:** a `conftest.py` that rewrites its own
verdict — only the baseline comparison addresses it.

## 7. MEDIUM — a grandchild that calls `os.setsid()` survives the timeout

`killpg` cannot reach a process that leaves the group. Needs cgroups/a container — i.e. the
Uplevel sandbox. Harm is a leaked sleeper once #2 is fixed.
Test: `tests/test_worktree.py` (xfail).

## 8. MEDIUM — a refused gate crashes the run instead of refusing it

`make test` passes the text pre-check, so the run does its full LLM spend, then
`run_acceptance` raises `WorktreeError` and `run_ticket` has only a `finally` — the caller
gets an exception, with no `REFUSED` outcome and **no ledger row**. Our own target READMEs
still advertise `make test`. Same class as the NUL-byte crash (`pytest \x00` raises
`ValueError` from `Path.resolve`).
**Fix:** pre-flight `shlex.split` + `validate()` inside `should_refuse`, so an unrunnable
gate is `REFUSED` with a reason before any worktree or token spend; and return
`CommandResult(127, ...)` on `OSError` from `Popen`.
Tests: `breach/deterministic/test_hostile_tickets.py` (xfail, 3 params).

## 9. MEDIUM/LOW — hardening nits

- **Relative `PATH` entry** lets the worktree's own `./pytest` run. Fix: `shutil.which` over
  absolute entries only, exec the resolved absolute path.
- **Unicode dashes** (`–rootdir=/outside`) skip the flag branch and are validated as paths.
  Fix: reject non-ASCII/control characters in tokens (also fixes the NUL crash).
- **`_kill_group`**: `PermissionError` returns early and skips SIGKILL; prefer
  `os.killpg(proc.pid, ...)` and bail if the pgid equals our own.
- **Policy can't express "this flag's value is a path"** — `--tb /outside/tests` is accepted
  unchecked. Harmless today (no current valued flag takes a path) but it's the prerequisite
  for ever re-adding a runner. Fix: `path_valued_flags` + `positional_pattern` + a
  per-runner `env` field; flip `allows_paths` to default `False`.

## Not a hole (checked)

`python3 -m unittest discover -s /outside` is refused — but only because the interpreter row
permits zero flags, which also refuses the legitimate `-s tests`.

## Honest limit for DESIGN.md

The gate executes repo-authored code by design (`conftest.py` runs at collection), with our
UID and no filesystem sandbox: it can read and write any path our user can, regardless of
the worktree jail, and argv validation stops neither that nor network egress. `HOME`
redirection blocks `~`-relative credential reads, not absolute ones. The path jail is
name-based, so symlinks/hardlinks/mounts inside the worktree defeat it. The gate's verdict
is influenced by repo content, which is why shipping should additionally require evidence
that tests ran.


---

# Open findings — lesson memory & what Learn can see

Separate pass (GH #30, rolls up #29/#23). Reviewed by strands-expert + meta-architect
against installed SDK 1.52.0 and the steering; both corrected the original diagnosis, so
the corrections are recorded below rather than the first draft.

## The problems

1. **Junk is persisted as a lesson.** `workflow.py` builds the Lesson as
   `content=(wf.final_output.strip() or ...)` — whatever the last agent said. Live runs
   store the drafter's whole response: preamble ("Now I'll draft the lesson..."), markdown
   headers, and a self-congratulatory checklist. Visible in `report.json` and Mem0, i.e. in
   a graded artifact.
2. **Degraded runs persist the canned failure string.** `memory.store()` is called
   unconditionally, so a circuit-breaker run writes `DEGRADED_MESSAGE` (a fixed platform
   string, from `fallback.py`) as a "lesson" — and `retrieve()` later recalls it into new
   runs. Confirmed in a live trace.
3. **Gating judges' reasons never reach Learn.** `_compose_task` renders `s.reason` only for
   `not s.gating`. So the process judges (tool selection, param accuracy) do get through,
   but the *gating* ones (faithfulness, goal_success, coherence) arrive as bare
   `passed/failed`. A faithfulness failure's "why" is the most lesson-shaped text in a run.
4. **No tool-usage record survives a node.** `clear_spans()` runs at the start of every
   attempt, so earlier nodes' spans are gone by the time Learn runs. Lower severity than it
   looks — see correction (b).

## Fixes

1. **Emission contract, not text-stripping.** Critic emits the lesson wrapped in
   `<lesson>…</lesson>`; code extracts the delimited span; **no delimiter → no write**
   (fails closed). Tighten the `lesson_shape` rubric in `nodes.py` to require the delimiter
   so the existing self-heal loop enforces it *before* persistence.
   Rejected alternative: strip preamble/headers by pattern — hardcoded heuristics against
   model phrasing, ages badly, and fails **open** (stores the junk), which is the bug.
2. **Admission gate in `memory.py`.** `store()` already owns scrub + ≥0.95 dedup; shape
   admission is the same concern, so it goes there and returns whether it wrote. Keeps one
   gate instead of per-caller checks.
3. **Skip-on-degraded in `workflow.py`.** `wf.degraded` is run-level state only the
   orchestrator holds — guard `memory.store()` and leave `report.lesson=None`.
4. **Render gating reasons** in `_compose_task` (~3 lines in the loop that already exists).
   Best value-per-line in this list; do it first.
5. **Tool digest — optional, last.** Build from `EventLoopMetrics.tool_metrics`
   (`ToolMetrics.call_count / success_count / error_count`), reached via
   `swarm_result.results[*].get_agent_results()[*].metrics.tool_metrics`. Store as a field on
   `Verdict` (already per-node, already serialises into `RunReport`) — not a parallel dict on
   `RunReport`. Cut this if time is short; it adds precision, not a new dimension.

**Tests (the two that matter):** framing/preamble text is never admitted to memory; a
degraded run persists nothing (feed the literal `DEGRADED_MESSAGE`).

**Order:** (4) gating reasons → (1) delimiter contract → (2)+(3) stop persisting junk →
tests → skills rewrite → (5) digest only if the rest is clean.

## Corrections to the original diagnosis (don't repeat these)

- **(a) "Build the digest by walking spans with a Counter" — wrong twice.** The SDK already
  aggregates per-tool call/success/error counts in `EventLoopMetrics.tool_metrics`, off the
  swarm result we already hold; no span walk needed. And the proposed call site (after
  `run_checkpoint`) is **contaminated**: the eval judges are themselves Agents and emit their
  own `execute_tool` spans (`ToolSelectionRating`, `FaithfulnessRating` — visible in
  `scratch/trace_bug1_v2.log`), so a post-checkpoint span digest would attribute the judges'
  tool calls to the worker. The metrics path makes ordering irrelevant and removes the
  dependency on the process-global exporter (GH #25).
- **(b) "Learn sees no tool-call evidence" — overstated.** Non-gating process judges
  (tool selection, param accuracy) already reach Learn with their reasons, and on failure the
  detector's `diagnose_session` output travels via `Verdict.diagnosis`, which carries
  span-level failure detail. The real gap is narrower: gating judges' reasons.
- **(c) "Feed Learn the raw telemetry" — rejected.** One ticket's trace is ~8,700 lines;
  piping it into the node whose job is three sentences blows the context budget and degrades
  lesson quality (yields ticket-specific trivia, not durable rules).
- **`strands_evals` note:** `extract_agent_tools_used_from_trace()` drops the error flag, and
  `StrandsInMemorySessionMapper` maps a missing tool-status attribute to `error=None` — so a
  hard tool exception under-reports as success. Don't source failure counts from the Session.
