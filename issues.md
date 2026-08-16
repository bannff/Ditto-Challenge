# Findings

Everything adversarial review has turned up, open first. Struck-through items are fixed and
verified; they stay on the list so the history is legible. Every item has a test —
`xfail(strict=True)` ones flip to failures the moment the hole closes.

---

# OPEN

## Fix order

1. ~~**#1 `@argfile`**~~ — done (and it grew: see the four sub-findings under it).
2. ~~**#2 `.git` write → shell RCE**~~ — done; surfaced **#2b/#2c/#2d**, and closed #6 en route.
3. ~~**#2b agent-written ini reaches outside the jail**~~ — done; also fixed **#2e**, a
   false-failure our own hardening had introduced.
4. **#3 regression: a shipped ticket is refused** — breaks a graded demo. **NEXT.**
4. **#4 empty PATH**, **#5 predictable worktrees base**, **#6 git HOME lies**, **#7 stray
   processes** — one-to-three lines each; #4 and #5 were introduced by the last pass.
5. **#8 unbounded output**, **#9 audit gap**, **#10 forgeable output**, **#12 lesson hygiene**.
6. **#11 conftest owns the verdict**, **#13 no FS/egress sandbox** — need design answers, not
   more argv validation.

## ~~1. CRITICAL — `pytest @argfile` bypasses the entire flag allowlist~~ FIXED

~~pytest sets `fromfile_prefix_chars="@"`, so a token starting with `@` was an argv splice: it
looked like a positional to us, resolved inside the jail, and pytest then inserted the file's
lines as options. Verified on a red suite: `@opts.txt` containing `--collect-only` → **exit 0**,
and a later `-o addopts=` in the file overrode our forced one. Also an out-of-jail read
(`pytest @/tmp/creds` echoed the contents into the ledger via pytest's error output).~~
**Fixed**, and re-review of the fix found two more holes in the same surface, both also fixed:
- ~~`@` was only dangerous in positionals~~ — argparse expands argfiles across the **whole**
  argv, recursively, so `-k @file` splices too. Now refused on every token, plus the attached
  `--flag=@file` spelling (inert today, refused anyway rather than trusting the runner).
- ~~**Positionals were fail-open**~~ — "not a flag" meant "treat as a path". Replaced
  `allows_paths` with `positional_pattern`: a row opts in by declaring what a path *is*, so
  `''`, `a=b`, `tests;id`, `tests/*.py`, `~/.ssh` are refused by shape.
- ~~**F1: an unresolved absolute positional defeated `--confcutdir`.**~~ `_check_path` validated
  the resolved path but we executed the token as written, and pytest doesn't resolve symlinks —
  so `pytest /tmp/<jail>/tests` (macOS `/tmp`, or a symlinked base) sat outside a resolved
  confcutdir and pulled in an ancestor's `conftest.py`. Positionals are now rewritten to the
  canonical jail-relative path we actually checked.
- ~~**F2: `--rootdir` doesn't stop ini discovery.**~~ pytest still walked up to find a config
  file; an ancestor ini needs only `pythonpath` to put attacker directories on `sys.path`
  (verified: red suite → exit 0, outside module imported). The gate now pins `-c` to the
  target's own config when it has one (`pytest.ini` / `pyproject.toml` / `tox.ini` /
  `setup.cfg`, checked for a real pytest section) and otherwise to an empty ini in the run's
  own HOME. Verified: a target's `pyproject.toml` markers are still honoured.

Effective argv is now
`pytest -o addopts= -o testpaths= -p no:cacheprovider -c <config> --rootdir=<jail> --confcutdir=<jail> …`.
All 29 legitimate forms and all 7 seed tickets unaffected; `@` regression tests added (they
were missing entirely, which the fix review caught).

## ~~2. HIGH — `.git` is inside the jail, making `write_file` a code-execution primitive~~ FIXED

~~`safe_path` was pure containment, so `<root>/.git` was "inside" and allowed. In a linked
worktree `.git` is a *file* (a gitdir pointer): overwrite it and every later `_git()` call
targets a repo the agent wrote, where `filter.*.clean` and `diff.*.textconv` run through
`/bin/sh`. Reproduced with only `write_file` calls — our own `git add -A` executed an attacker
command. `core.worktree` also aimed `revert()`'s `clean -fd` at a directory outside the
worktree (verified: it deleted a file out there), and a clobbered pointer made
`worktree remove` fail, stranding the worktree.~~
**Fixed with two independent controls**, because either alone leaves the other route open:
- the file tools refuse any path with a `.git` component, case-folded and at any depth
  (`casefold` is required, not paranoia: APFS is case-insensitive while `resolve()` keeps the
  spelling, so `.GIT/config` opened `.git/config`);
- every in-worktree git call pins `--git-dir`/`--work-tree` to the gitdir recorded at creation,
  so a pointer written by something *other* than the agent (gate code runs as our uid) is inert
  and `core.worktree` cannot re-aim anything. `remove()` also restores the pointer and retries.
`_GIT_HARDENING` gained `core.excludesFile=/dev/null` and `core.attributesFile=/dev/null`,
which also closes #6. 27 regression tests; 11 legitimate `.git*` forms (`.gitignore`,
`.gitattributes`, `.github/workflows/…`, `src/git/plumbing.py`) verified unaffected.
**Residual:** with the gitdir pinned, git reads the *target repo's own* local config, which we
don't author and git has no flag to ignore. A hostile repo shipped with `filter.*` already set
still executes — same class as #13 (we run the target's code by design).

## ~~2b. HIGH — `_gate_config()` adopts an agent-written ini that reaches outside the jail~~ FIXED

~~`_gate_config()` preferred the target's own config so a legitimate target keeps its markers
and `filterwarnings` — but the agent can *write* that file. Three classes of escape, all
verified through the real tool path:~~
- ~~**write:** `log_file = /outside/x.log` — pytest `os.makedirs()` the parent and opens the
  path. Worse than a stray file: with `log_file_mode = a` and `log_file_format = %(message)s`
  the *content* is the test's own logging calls, i.e. arbitrary attacker-chosen text appended
  to `~/.zshenv` with a green gate.~~
- ~~**import:** `pythonpath = /outside` put a directory on `sys.path`; an outside module was
  imported and executed by the gate.~~
- ~~**verdict:** `python_files = test_green.py` (or `python_functions`, `norecursedirs`) simply
  doesn't collect the failing file, so a red suite exits 0 — no `addopts`/`testpaths` needed.~~
**Fixed structurally, not key by key:** the gate now trusts only config that shipped with the
target at this run's **base commit** (`self._seed`, not HEAD — `checkpoint()` commits the
agent's own work, so a HEAD-anchored check is useless after one checkpoint). Unmodified → the
target's file in place, so relative keys like `pythonpath = src` still resolve; modified or
deleted → the committed blob copied into the run's HOME; never shipped → an empty ini of ours.
That closes the keys nobody has enumerated yet, which a forced `-o` per key cannot: pytest 8.4
registers 47 ini keys and 9.1 registers 56.
`-o log_file=` is also forced, since no gate needs it. `-o pythonpath=` deliberately **not**
forced — it breaks a legitimate src-layout target, and once config is seed-pinned `pythonpath`
is target-committed content, i.e. the same trust level as the `conftest.py` we execute anyway.
**Residual:** the target's *committed* config is trusted, so a hostile repo can still put a
directory on `sys.path` through it (same class as #13). An agent edit to pytest config is
ignored by the gate for that run — a false refusal, never a false pass.

## ~~2e. Our own hardening made ordinary targets unrunnable~~ FIXED

Introduced by #1's `-p no:cacheprovider`. Unregistering the plugin makes `cache_dir` an
*unknown* ini key, so a target that sets `cache_dir` **and** `filterwarnings = error` — both
ordinary — turns that warning into `INTERNALERROR`, exit 3, no tests ran. Verified: exit 0
normally, exit 3 under our gate. Every ticket against such a repo would be unrunnable, and
pytest 9's `strict_config` widens it.
**Fixed:** keep cacheprovider enabled and force `cache_dir` into the run's private HOME via a
new `{home}`-templated forced arg. Same containment (nothing written in the jail, tree stays
clean), no unknown-key warning. Verified: the target runs again, cache lands in HOME, and
`is_clean()` is still true afterwards.

## 2c. MEDIUM (new) — an agent-written `.gitignore` hides a file from our own evidence

`write_file(".gitignore", "stash.py")` + `write_file("stash.py", …)`: `is_clean()` returns True,
the file is absent from the diff, it isn't committed, and `clean -fd` leaves it on disk. So a
"verified" branch can omit a file the gate relied on. Sibling of #6 but from *inside* the jail,
so `core.excludesFile` doesn't reach it.
**Fix:** use `status --porcelain --ignored` (or `add -Af`) when deciding cleanliness/evidence.

## 2d. LOW (new) — a tool escape crashes instead of refusing

`write_file("service.py/evil.py")` raises `FileExistsError` out of the tool, contradicting the
tools.py docstring ("escapes return an error string instead of crashing"). Same class as the
NUL crash: an unexpected exception type escaping the tool boundary.

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
