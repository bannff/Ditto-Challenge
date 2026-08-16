# Findings

Everything adversarial review has turned up, open first. Struck-through items are fixed and
verified; they stay on the list so the history is legible. Every item has a test —
`xfail(strict=True)` ones flip to failures the moment the hole closes.

---

# OPEN

## Fix order

1. **#1 `@argfile`** — critical, one line, defeats every other control in the gate.
2. **#2 `.git` write → shell RCE** — high, ~3 lines.
3. **#3 regression: a shipped ticket is refused** — breaks a graded demo.
4. **#4 empty PATH**, **#5 predictable worktrees base**, **#6 git HOME lies**, **#7 stray
   processes** — one-to-three lines each; #4 and #5 were introduced by the last pass.
5. **#8 unbounded output**, **#9 audit gap**, **#10 forgeable output**, **#12 lesson hygiene**.
6. **#11 conftest owns the verdict**, **#13 no FS/egress sandbox** — need design answers, not
   more argv validation.

## 1. CRITICAL — `pytest @argfile` bypasses the entire flag allowlist

pytest sets `fromfile_prefix_chars="@"`, so a token starting with `@` is not a flag to us but
*is* an argv splice to pytest. `@opts.txt` looks like a positional, resolves inside the jail,
and is allowed — then pytest reads the file and inserts its lines as options. Verified on a
fully red suite: `pytest -o addopts= -q @opts.txt tests` → **exit 0** with `--collect-only`
inside the file. A later `-o addopts=…` in the file also overrides our forced one. So `--co`,
`-p`, `--rootdir`, `-c` are all back, and `write_file` can author the argfile inside the
worktree.
Also an out-of-jail **read**: `pytest @/tmp/creds` puts the file's contents into pytest's error
message → `output_tail` → the ledger (`scrub_text` blunts but doesn't guarantee).
**Fix:** refuse a positional starting with `@`; better, add `positional_pattern` to
`RunnerPolicy` and require positionals to match before the path check — the field the last pass
dismissed as "harmless today". It wasn't.

## 2. HIGH — `.git` is inside the jail, making `write_file` a code-execution primitive

`safe_path` is pure containment, so `<root>/.git` is "inside" and allowed. In a linked worktree
`.git` is a *file* (a gitdir pointer). Overwrite it and every later `_git()` call targets a repo
the agent controls. `_GIT_HARDENING` blocks hooks/fsmonitor/`protocol.ext` but **not `filter.*`
/ `diff.*.textconv`, which git runs through `/bin/sh`**. Reproduced end to end with only
`safe_path`-legal writes: our own `git add -A` executed an attacker command. `core.worktree`
also aims `revert()`'s `reset --hard` / `clean -fd` outside the worktree. Pre-existing.
**Fix:** refuse any path with a `.git` component (case-folded) in `safe_path`; optionally pass
`--git-dir <real gitdir>` on every `_git` call so a clobbered pointer is inert.

## 3. REGRESSION — a legitimate shipped ticket is refused, and it breaks a demo

`examples/tickets/bug-3-pitfall.json` (an ordinary off-by-one report) is REFUSED: the denylist
pattern `(disabl|remov|delete|drop|…)\w*.{0,40}(test|check|…)` matches the innocent span
*"dropped. Fix the boundary so it is inclusive. test[s/test_reorder.py]"*. That ticket is the
app1 scenario in `demo_selfimprove.py`, so `--app app1` refuses both runs and the before/after
**measures nothing**. Uncovered because `test_refusal.py` only checks a hand-written sentence
and the policy tests only check tickets' *commands*.
**Fix:** require an imperative object rather than mere word proximity, and add a test asserting
no non-`refuse-*` seed ticket is refused.

## 4. MEDIUM — the absolute-only PATH filter can empty PATH, which means "exec from cwd"

`_safe_path_entries()` returns `""` when the parent PATH is unset or wholly relative;
`subprocess` then resolves the runner against **cwd** — the worktree. Verified: a planted
`./pytest` executed. Re-arms the exact hole the filter was added to close (`env -i`, container
entrypoints, systemd units). Introduced by the last pass.
**Fix:** `… or os.defpath`; ideally `shutil.which` in the parent and exec an absolute path.

## 5. MEDIUM (HIGH on Linux) — predictable worktrees base, adopted even if symlinked

`$TMPDIR/autodev-worktrees` is per-user 0700 on macOS, but with `TMPDIR` unset (Linux, CI,
containers) it is the fixed path `/tmp/autodev-worktrees` in a world-writable dir.
`ensure_dirs()` uses `mkdir(exist_ok=True)` with no ownership or symlink check (and `exist_ok`
never applies a mode to an existing dir), and `Worktree.create` resolves `base_dir` before its
containment check — so a pre-created symlink is silently adopted and every worktree lands where
an attacker chose: a write foothold in the tree the gate executes. `.data/worktrees` had none of
this. Introduced by the last pass.
**Fix:** uid in the name, create `mode=0o700`, refuse a base that is a symlink or not ours.

## 6. MEDIUM — the shared git HOME lets gate code make `is_clean()` lie

`GIT_CONFIG_GLOBAL=/dev/null` covers `~/.gitconfig` but git still reads
`$HOME/.config/git/{ignore,attributes}`. The gate child runs as the same uid and can locate the
shared `autodev_home_*` dir. Verified: after planting an `ignore` file, `is_clean()` returned
True with an untracked file present — so the diff omits attacker files, `git add -A` skips them,
`clean -fd` leaves them. Evidence integrity, persistent for the process.
**Fix:** add `core.excludesFile=/dev/null` and `core.attributesFile=/dev/null` to
`_GIT_HARDENING` (verified).

## 7. MEDIUM — a gate that exits cleanly leaves its spawned processes running

`_kill_group` runs only on `TimeoutExpired`, so a suite that starts a background helper and
returns 0 leaves it alive indefinitely, after the run is reported done. Broader than the known
`setsid` escape: no setsid, no timeout needed.
**Fix:** call `_kill_group` in a `finally` on every path.

## 8. MEDIUM — gate output is unbounded

`run()` reads the whole temp file into one string; only `output[-2000:]` is ever used. Verified:
a gate emitting ~300MB produced a 300M-character string. The `TemporaryFile` change fixed the
hang but moved the unbounded read onto disk as well as memory, and "bounded runs" is a hard
requirement.
**Fix:** seek to the last ~64KB instead of reading whole; optionally `RLIMIT_FSIZE`.

## 9. LOW — the recorded command doesn't reproduce the verdict

`AcceptanceResult.command` stores the ticket's string while `resolve()` executes different argv
(`-o addopts=` injected, `python -m pytest` rewritten). On a repo with `addopts = --co` the gate
correctly reports exit 1 while the recorded command, run by hand, exits 0 — opposite answers on
exactly the repo the forced arg defends against.
**Fix:** record the effective argv; `resolve()` already returns it.

## 10. LOW — the child can forge its own output

The sink is opened `w+` and inherited as fd 1, so the child can seek to 0 and overwrite what it
printed (verified: real output replaced with "1 passed"). It cannot change the exit code, so the
verdict holds, but the human-readable evidence is forgeable.
**Fix:** give the child an append-only write fd; keep a separate read fd.

## 11. The weakest link — repo-resident `conftest.py` decides the gate's verdict

`-o addopts=` closed the *ini* half. The conftest half is untouched: three one-liners in
committed target content each turn a red suite green — a `pytest_runtest_makereport` wrapper
rewriting `rep.outcome`, `pytest_sessionfinish` setting `session.exitstatus = 0`, and
`pytest.exit(returncode=0)`. The gate's own exit code is attacker-controlled, and the file need
not be authored by the agent. Hostile `testpaths` also selects a green subset (adding
`-o testpaths=` to `forced_args` fixes that specific case).
**No argv fix reaches this.** Honest answers: (a) compare pass/fail counts against the base
commit — noting this does *not* catch a hook present at both commits — or (b) the process
sandbox in DESIGN §Uplevel, where the verdict arrives on an independent channel.

## 12. Memory hygiene — the stored lesson is the agent's chatty preamble

`workflow.py` persists `wf.final_output` verbatim, so a lesson reads *"Perfect. Now I'll draft
the durable lesson…"* instead of the lesson. Directly degrades the self-improvement dimension:
future recalls surface framing text. Observed in both app1 and app2 live runs.
**Fix:** have Learn emit only the lesson (or extract it), and reject a lesson that fails the
existing `lesson_shape` rubric before persisting.

## 13. No filesystem/egress sandbox (GH #28)

The gate executes repo-authored code as our own uid, so it can read/write any path we can and
reach the network, regardless of the worktree jail. Root cause behind #2, #6, #11 and the
`setsid` escape. Durable fix is the process-level jail (network-off, FS-confined) in DESIGN
§Uplevel — deliberately not built for this deliverable.

## 14. Telemetry span buffer is process-global (GH #25)

`telemetry._exporter` plus `clear_spans()`/`build_session()` are correct only because nodes run
sequentially; any in-process concurrency would cross runs' spans. Must become a per-run exporter
before parallelising.

## 15. Housekeeping

- Three `xfail` notes in `test_hostile_tickets.py` are **stale**: the `--co`/`python3` rows now
  fail on `acceptance is not None` (the gate is refused, so there's no exit code) rather than
  for their stated reason. Rewrite to assert REFUSED — a strict xfail passing for the wrong
  reason is the one thing this convention can't self-detect.
- Per-run HOME leaks when a `Worktree` is abandoned without `remove()` (+8 per breach run, since
  HOME is allocated in `__init__`); the shared `autodev_home_*` is never cleaned (139 on this
  machine).
- `server.py`'s MCP prompt still asks for "the shell command that must exit zero" with no hint
  that pytest is the only runner, so the MCP path mints tickets that get refused. Target-app
  READMEs still advertise `make test`.
- The gate resolves `pytest` from the filtered PATH (a user-level 3.14 install here), not the
  project venv. Fine for the bundled targets; a target with its own deps would 127/4.
- Stale empty `.data/worktrees/` left behind after the move — cosmetic.

## 16. DESIGN.md corrections required

- "acceptance command is allowlisted, **shell-free**, no inline code" — #1 makes the allowlist
  bypassable for arbitrary pytest options; #2 reaches `/bin/sh` via git filters.
- "`safe_path()` refuses any escape" — true for containment, but `.git` is *inside* the jail and
  inside is enough (#2).
- "name-based jail defeated by symlinks created inside" — understated in our favour: `safe_path`
  resolves, so symlinks pointing out are refused. The real residual is pytest collecting
  *through* an in-worktree symlink.
- Accurate, keep: the gate executes repo-authored code by design; no FS/egress sandbox; `setsid`
  escapes the killpg bound while the caller stays bounded.

---

# FIXED

## Acceptance gate

- ~~**`make -C /outside all` escapes the jail.**~~ `make` ran recipes through a shell against an
  outside Makefile. Allowlist shrunk to pytest only.
- ~~**`python3 -m pip install <pkg>` fetches remote code.**~~ The `-m` module allowlist, then the
  interpreter rows entirely, were removed.
- ~~**An interpreter accepts a script (`python3 bench.py`).**~~ Green gate that ran no test and
  wrote outside the worktree. `python`/`python3` deleted from the table; `allows_paths` default
  flipped to False so a row must opt in.
- ~~**`python3 -m unittest <dotted.name>` imports code from outside the jail.**~~ Positional was
  path-checked but resolved by unittest through `sys.path`. Closed with the interpreter rows.
- ~~**A bare `python3` is a green gate.**~~ `stdin=DEVNULL` → EOF → exit 0. Closed likewise.
- ~~**`--co`/`--collect-only` allowed.**~~ Collection exits 0 on a red suite; a gate that cannot
  fail is not a gate. Flags deleted.
- ~~**Repo `pytest.ini` `addopts` silenced a red suite.**~~ `forced_args=("-o","addopts=")`,
  prepended after the ticket's own argv is validated; `-o` kept out of ticket-facing flags.
  Verified against `pytest.ini`, `tox.ini`, `setup.cfg`, `pyproject.toml`. (conftest half → #11.)
- ~~**Unicode look-alike dashes skipped the flag branch** (`–rootdir=/outside`).~~ Every token
  must be printable ASCII.
- ~~**An embedded NUL crashed `Path.resolve()`** instead of refusing.~~ Same rule; nothing now
  reaches the path check that can raise a non-`AcceptanceRejected`.
- ~~**A refused gate crashed the run** instead of refusing it.~~ `should_refuse` pre-flights the
  policy before any spend; `run_ticket` catches `WorktreeError` → `REFUSED` + reason + ledger row.
- ~~**Only `argv[0]` was checked.**~~ Every token is now validated, fail-closed, and positional
  paths must resolve inside the worktree.

## Subprocess and isolation

- ~~**The post-timeout read hung forever.**~~ A detached grandchild holding the stdout pipe
  blocked `communicate()` indefinitely, so the wall-clock bound didn't exist. Output now goes to
  a temp file; verified exit 124 in 2.0s, including the `setsid` shape. (Bounding the *size* of
  that output → #8.)
- ~~**`_kill_group` skipped SIGKILL** on `PermissionError`.~~ Suppressed rather than returned;
  signals `proc.pid` directly and never signals our own group.
- ~~**A shared HOME let one run poison later ones** via `usercustomize.py`.~~ Per-`Worktree` HOME
  plus `PYTHONNOUSERSITE=1`; verified distinct per run and user-site disabled. (Git's HOME-derived
  files → #6; leak on abandoned worktrees → #15.)
- ~~**The gate loaded `conftest.py` from outside the worktree, persistently.**~~ Worktrees lived
  under `<agent repo>/.data/worktrees`, so pytest's rootdir was our own repo. `worktrees_dir` is
  now an env-overridable absolute path outside the repo; verified rootdir is the worktree and
  planted parent/grandparent `conftest.py` files are not executed. (New exposure → #5.)
- ~~**A missing/unrunnable binary raised.**~~ Returns `CommandResult(127, …)`.
- ~~**Child inherited operator stdin.**~~ `stdin=subprocess.DEVNULL`.

## Workflow, evals, memory

- ~~**A bounded-out swarm could still pass the gate.**~~ `Swarm` returns non-`COMPLETED` with
  partial text; the driver ignored it and judged the fragment on score alone. Status now fails the
  node regardless of judge score.
- ~~**Per-tool-call judges could veto correct work.**~~ Averaging ~200 per-call judgments failed a
  node whose fix was verified by the deterministic gate. Tool/param/trajectory judges are now
  informational (they still run, and their findings reach the Learn node); the gate is
  goal-success + faithfulness.
- ~~**Judge scoring included our own plugin calls.**~~ 48 `_LLMSteering`/`skills` spans were rated
  as if they were agent decisions. A custom `TraceExtractor` scopes tool judges to real agent tools.
- ~~**Our aggregation discarded the SDK's verdict.**~~ Hand-rolled mean ignored
  `EvaluationOutput.test_pass`; now uses the SDK's own aggregator.
- ~~**Sync `.evaluate` inside the async driver** spawned a thread + event loop per evaluator.~~
  Now `evaluate_async`.
- ~~**The degraded path spun up a stub swarm to emit a constant.**~~ Returns the constant directly.
- ~~**The fork's `strong` role could KeyError the fallback map** (GH #26).~~ Fork removed entirely
  after live testing showed it firing on false failures and timing out; ladder is retry → breaker.
- ~~**DESIGN claimed GraphBuilder couldn't express an eval-gated redo** (GH #27).~~ Disproved with
  a runnable PoC; the engine now *is* a GraphBuilder graph with custom gate nodes.
- ~~**Memory could store the same lesson twice.**~~ Near-duplicate skip at similarity ≥ 0.95.
  (Chatty-preamble content → #12.)
- ~~**Dead code / unused dependency.**~~ Removed `kb.add_policy`, the `__init__.main()` stub and
  its console script, the inert ledger `_migrate`, and `opensearch-py`.
