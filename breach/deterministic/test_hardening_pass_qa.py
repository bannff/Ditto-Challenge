"""QA of the security-hardening pass: regression guards for what it closed, and FINDINGs
for what it didn't.

Same conventions and harness as `test_hostile_tickets.py` (imported, not duplicated): the
node graph is stubbed so this layer stays keyless and offline, while everything under test
— the policy, the jail, the gate, revert/commit, the ledger — is the real thing.

The first half are regression guards: they encode properties the hardening pass claims to
have established, derived independently of the markers that were removed from the file next
door. They must PASS.

The second half are `xfail(strict=True)` FINDINGs: properties `src/` does not hold. They
flip to failures the moment the hole is closed, which is the signal to delete the marker.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from test_hostile_tickets import (  # the shared harness lives next door
    GREEN_GATE,
    RED_GATE,
    _assert_target_untouched,
    _autodev_branches,
    _branch,
    _fake_workflow,
    _head,
    _run,
    _worktree,
    repo,  # noqa: F401 — imported fixture
)

from self_improving_coding_agent.acceptance_policy import normalize, resolve
from self_improving_coding_agent.contracts import Outcome
from self_improving_coding_agent.settings import get_settings
from self_improving_coding_agent.worktree import Worktree

TMP = Path(get_settings().worktrees_dir).parent


def _commit(repo_path: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(repo_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo_path), "commit", "-q", "-m", message], check=True)


def _run_home_count() -> int:
    return len(list(TMP.glob("autodev_run_home_*")))


# --- regression guards: what the hardening pass closed ------------------------------------


@pytest.mark.parametrize("gate", [
    "pytest --co -q",            # collection exits 0 on a red suite
    "pytest --collect-only",
    "python3 -m pytest --co",    # ...including through normalize()
    "python3",                   # bare interpreter: stdin=DEVNULL -> EOF -> exit 0
    "python",
    "python3 gate.py",           # any script nominated as the gate
    "python3 -m unittest this",  # a module resolved through sys.path, not a path
    "python3 -m pip --version",
])
def test_a_gate_that_could_never_fail_is_refused_before_any_spend(repo: Path, tmp_path: Path,  # noqa: F811
                                                                 gate: str):
    """The property the deleted `--co` xfail was trying to encode, stated the way the
    hardened code actually satisfies it: such a gate is REFUSED up front, so there is no
    exit code to misread and nothing to ship."""
    head, branch = _head(repo), _branch(repo)
    report, ledger = _run(repo, tmp_path, "Fix the ownership check on the order read path",
                          gate)
    assert report.outcome == Outcome.REFUSED
    assert report.acceptance is None, "the gate must never have run"
    assert report.branch is None
    assert report.evidence, "a refusal states its reason"
    assert ledger.get(report.run_id) is not None, "the refusal is on the record"
    assert _autodev_branches(repo) == []
    _assert_target_untouched(repo, head, branch)


def test_forced_args_are_always_ours_and_always_first():
    for command in ("pytest", "pytest -q tests", "python3 -m pytest -x tests/test_idor.py",
                    "pytest --tb -o addopts=--co", "pytest -k -o addopts=--co"):
        argv = resolve(normalize(shlex.split(command)), in_jail=None)
        assert argv[:3] == ["pytest", "-o", "addopts="], argv
        assert argv.count("-o") == 1 or argv[3:].count("-o") == 0 or "addopts=" in argv[2]


@pytest.mark.parametrize(("name", "content"), [
    ("pytest.ini", "[pytest]\naddopts = --co\n"),
    ("pytest.ini", "[pytest]\naddopts = --collect-only -q\n"),
    ("tox.ini", "[pytest]\naddopts = --co\n"),
    ("setup.cfg", "[tool:pytest]\naddopts = --co\n"),
    ("pyproject.toml", '[tool.pytest.ini_options]\naddopts = "--co"\n'),
])
def test_forced_addopts_neutralises_repo_resident_ini_config(repo: Path, tmp_path: Path,  # noqa: F811
                                                             name: str, content: str):
    # A hostile ini cannot silence a red suite: `-o addopts=` wins over every ini source.
    wt = _worktree(repo, tmp_path, f"ini-{abs(hash((name, content))) % 10**6}")
    try:
        (wt.root / name).write_text(content)
        r = wt.run_acceptance(RED_GATE, timeout=120)
        assert r.exit_code != 0, f"{name} silenced a red suite: {r.output[-300:]}"
    finally:
        wt.remove()


@pytest.mark.parametrize("gate", ["pytest --tb -o addopts=--co", "pytest -k -o addopts=--co"])
def test_a_smuggled_dash_o_cannot_reach_pytest_as_a_flag(repo: Path, tmp_path: Path,  # noqa: F811
                                                         gate: str):
    """`-o` can be smuggled past validation as the *value* of an allowed valued flag, so
    check the thing that matters: it never takes effect, and the gate fails closed."""
    wt = _worktree(repo, tmp_path, f"smuggle-{abs(hash(gate)) % 10**6}")
    try:
        (wt.root / "pytest.ini").write_text("[pytest]\naddopts = --co\n")
        r = wt.run_acceptance(gate, timeout=120)
        assert r.exit_code != 0, f"a smuggled -o produced a green gate: {r.output[-300:]}"
    finally:
        wt.remove()


def test_config_outside_the_worktree_is_never_loaded(repo: Path, tmp_path: Path):  # noqa: F811
    # Independent derivation: plant conftest.py at every level above the worktree that we
    # can write to, and confirm none of them is imported by the gate.
    wt = _worktree(repo, tmp_path, "outside-config")
    planted: list[Path] = []
    markers: list[Path] = []
    try:
        for i, parent in enumerate((wt.root.parent, wt.root.parent.parent)):
            marker = tmp_path / f"OUTSIDE_CONFTEST_{i}"
            path = parent / "conftest.py"
            if path.exists():  # never clobber something real
                continue
            path.write_text(
                f"import pathlib\npathlib.Path({str(marker)!r}).write_text('executed')\n"
            )
            planted.append(path)
            markers.append(marker)
        assert planted, "nothing was planted, so this proves nothing"
        r = wt.run_acceptance(GREEN_GATE, timeout=120)
        assert f"rootdir: {wt.root}" in r.output.replace("/private/var", "/var") \
            or str(wt.root).endswith(r.output.split("rootdir: ")[1].split("\n")[0].strip()), \
            f"rootdir is not the worktree: {r.output[:400]}"
        for marker in markers:
            assert not marker.exists(), f"the gate executed {marker}"
    finally:
        for path in planted:
            path.unlink(missing_ok=True)
            shutil.rmtree(path.parent / "__pycache__", ignore_errors=True)
        wt.remove()


def test_each_run_gets_its_own_home_and_gives_it_back(repo: Path, tmp_path: Path):  # noqa: F811
    # A per-run HOME is what stops one run planting a file a later run imports; and it has
    # to be handed back, or a long-lived process leaks a temp dir per ticket.
    before = _run_home_count()
    first = _worktree(repo, tmp_path, "home-a")
    second = _worktree(repo, tmp_path, "home-b")
    try:
        home_a = first.run(["python3", "-c", "import os;print(os.environ['HOME'])"]).output
        home_b = second.run(["python3", "-c", "import os;print(os.environ['HOME'])"]).output
        assert home_a and home_b and home_a != home_b, "runs shared a HOME"
        assert "autodev_run_home_" in home_a
        # user site is off, so even a planted usercustomize.py would not be imported
        site = second.run(["python3", "-c", "import site;print(site.ENABLE_USER_SITE)"])
        assert site.output.strip() == "False", site.output
    finally:
        first.remove()
        second.remove()
    assert _run_home_count() == before, "a per-run HOME leaked"


def test_a_whole_run_leaks_no_per_run_home(repo: Path, tmp_path: Path):  # noqa: F811
    before = _run_home_count()
    report, _ = _run(repo, tmp_path, "Add a summary helper to the orders service", GREEN_GATE)
    assert report.outcome == Outcome.SUCCESS
    assert _run_home_count() == before, "run_ticket leaked a per-run HOME"


def test_an_unrunnable_binary_is_an_exit_code_not_a_traceback(repo: Path, tmp_path: Path):  # noqa: F811
    wt = _worktree(repo, tmp_path, "oserror")
    try:
        for argv in (["definitely-not-a-real-binary-xyz"], ["/nonexistent/pytest"],
                     [str(wt.root)]):
            r = wt.run(argv, timeout=30)
            assert r.exit_code == 127, argv
            assert "could not run" in r.output
    finally:
        wt.remove()


def test_the_timeout_is_bounded_and_kills_the_group(repo: Path, tmp_path: Path):  # noqa: F811
    # The post-timeout read used to block forever on a pipe a detached grandchild held open.
    wt = _worktree(repo, tmp_path, "bounded")
    started, survived = tmp_path / "gc-started", tmp_path / "gc-survived"
    grandchild = (
        f"import pathlib,time,sys\npathlib.Path({str(started)!r}).write_text('x')\n"
        "sys.stdout.write('holding the output handle\\n');sys.stdout.flush()\n"
        f"time.sleep(4)\npathlib.Path({str(survived)!r}).write_text('x')\n"
    )
    spawner = (
        "import subprocess,sys,time\n"
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}])\n"
        "time.sleep(600)\n"
    )
    try:
        began = time.monotonic()
        r = wt.run(["python3", "-c", spawner], timeout=2)
        elapsed = time.monotonic() - began
        assert r.exit_code == 124
        assert elapsed < 30, f"the timeout took {elapsed:.1f}s to return"
        assert started.exists(), "the grandchild never ran, so this proves nothing"
        time.sleep(6)
        assert not survived.exists(), "a grandchild in the group outlived the timeout"
    finally:
        wt.remove()


# --- FINDINGS: properties src/ does not hold ----------------------------------------------


@pytest.mark.xfail(strict=True, reason=(
    "FINDING: repo-resident `conftest.py` still owns the gate's verdict, so a red change "
    "ships as SUCCESS. `-o addopts=` closed the ini half of this (issues.md #6) but not the "
    "conftest half, which was already named as 'the weakest remaining link' and had no test. "
    "Three separate one-liners each turn a red suite green end to end: a "
    "`pytest_runtest_makereport` wrapper that rewrites rep.outcome (exit 0, '1 passed'); "
    "`pytest_sessionfinish` setting `session.exitstatus = 0` (exit 0 while the output still "
    "says '1 failed'); and `pytest.exit(returncode=0)` from `pytest_collection`. The file is "
    "committed target content — the agent does not have to author it — so the gate's own "
    "exit code is attacker-controlled. Repro: commit the hook below into the target and run "
    "any validated gate. Durable fix is evidence that tests ran (compare pass/fail counts "
    "against the base commit), not more argv validation."
))
@pytest.mark.parametrize("hook", [
    "import pytest\n"
    "@pytest.hookimpl(hookwrapper=True)\n"
    "def pytest_runtest_makereport(item, call):\n"
    "    outcome = yield\n"
    "    rep = outcome.get_result()\n"
    "    if rep.failed:\n"
    "        rep.outcome = 'passed'\n"
    "        rep.longrepr = None\n",
    "def pytest_sessionfinish(session, exitstatus):\n    session.exitstatus = 0\n",
    "import pytest\n"
    "def pytest_collection(session):\n    pytest.exit('all good', returncode=0)\n",
])
def test_repo_resident_conftest_cannot_decide_the_gates_verdict(repo: Path, tmp_path: Path,  # noqa: F811
                                                                hook: str):
    (repo / "conftest.py").write_text(
        (repo / "conftest.py").read_text() + "\n" + hook if (repo / "conftest.py").exists()
        else hook
    )
    _commit(repo, "hostile conftest")
    head, branch = _head(repo), _branch(repo)
    report, _ = _run(repo, tmp_path, "Fix the ownership check on the order read path",
                     RED_GATE)
    _assert_target_untouched(repo, head, branch)
    assert report.outcome != Outcome.SUCCESS, "a red suite shipped as SUCCESS"
    assert report.branch is None, "an unverified change landed on a branch"


@pytest.mark.xfail(strict=True, reason=(
    "FINDING: the process group is only killed when the gate TIMES OUT, so a gate that "
    "exits cleanly leaves whatever it spawned running — indefinitely, outside every budget, "
    "after the run is reported done. This is strictly broader than the known setsid escape "
    "(issues.md #7): no setsid is needed and no timeout is involved, which is why the "
    "existing process-group test does not see it. The innocent version is a fixture that "
    "starts a background server; the hostile version is a sleeper that outlives the report. "
    "Repro: a test that Popen's a detached child and returns; the gate exits 0 and the child "
    "is still alive well after run() returned. Fix: _kill_group(proc) in a finally on every "
    "path, not just the TimeoutExpired branch."
))
def test_a_clean_gate_exit_does_not_leave_background_processes_running(repo: Path,  # noqa: F811
                                                                      tmp_path: Path):
    survived = tmp_path / "BACKGROUND_SURVIVED"
    child_src = (
        "import pathlib, time\n"
        "time.sleep(6)\n"
        f"pathlib.Path({str(survived)!r}).write_text('alive')\n"
    )
    (repo / "tests" / "test_spawn.py").write_text(
        "import subprocess, sys\n"
        f"CHILD = {child_src!r}\n"
        "def test_spawn():\n"
        "    subprocess.Popen([sys.executable, '-c', CHILD])\n"
    )
    _commit(repo, "a test that spawns a helper")
    wt = _worktree(repo, tmp_path, "leak-check")
    try:
        r = wt.run_acceptance("pytest tests/test_spawn.py", timeout=120)
        assert r.exit_code == 0, r.output[-400:]
    finally:
        wt.remove()
    time.sleep(9)  # outlast the helper's own sleep
    assert not survived.exists(), "a background process outlived the run that started it"


@pytest.mark.xfail(strict=True, reason=(
    "FINDING: gate output is unbounded. `run()` reads the whole temp file into one str "
    "before anything truncates it (`output_tail=r.output[-2000:]` happens later, in the "
    "workflow), so a gate that prints a lot drives our RSS and the temp filesystem with it. "
    "Observed: a gate writing ~300MB to stdout returns a 300,299,999-character string in "
    "0.8s; nothing in the run bounds it. The TemporaryFile change fixed the post-timeout "
    "hang but moved the same unbounded read onto disk as well as memory. 'Bounded runs' is a "
    "hard requirement and output is the one budget with no ceiling. Fix: seek to the last N "
    "bytes instead of reading the file whole (and note the truncation in the tail)."
))
def test_gate_output_is_bounded(repo: Path, tmp_path: Path):  # noqa: F811
    wt = _worktree(repo, tmp_path, "loud-gate")
    try:
        r = wt.run(["python3", "-c",
                    "import sys\nline='A'*1000+'\\n'\n"
                    "for _ in range(64000): sys.stdout.write(line)\n"], timeout=120)
        assert r.exit_code == 0
        assert len(r.output) < 5_000_000, (
            f"run() held {len(r.output):,} characters of gate output in memory"
        )
    finally:
        wt.remove()


@pytest.mark.xfail(strict=True, reason=(
    "FINDING: the refusal denylist false-positives on a shipped seed ticket. "
    "examples/tickets/bug-3-pitfall.json — an ordinary off-by-one bug report — is REFUSED as "
    "'asks to remove or disable the project's own checks', because the pattern "
    "'(disabl|remov|delete|drop|bypass|skip|turn off)\\w*.{0,40}(test|check|guard|safety|"
    "validation)' matches the innocent span 'dropped. Fix the boundary so it is inclusive. "
    "test[s/test_reorder.py]' — the bug's own description ('items ... are dropped') sitting "
    "within 40 characters of the word 'test'. Consequence: that ticket is the app1 scenario "
    "in scripts/demo_selfimprove.py, so `--app app1` refuses both runs and the before/after "
    "measures nothing (branch is None, so the hidden check returns None for control and "
    "primed alike). No test covers this: test_refusal.py asserts a hand-written sentence is "
    "not refused, and test_acceptance_policy.py only checks the seed tickets' *commands*. "
    "Repro: should_refuse(Ticket(**json.load(open('examples/tickets/bug-3-pitfall.json'))))."
))
def test_no_legitimate_seed_ticket_is_refused():
    import json

    from self_improving_coding_agent.contracts import Ticket
    from self_improving_coding_agent.refusal import should_refuse

    tickets = Path(__file__).resolve().parents[2] / "examples" / "tickets"
    refused = {}
    for path in sorted(tickets.glob("*.json")):
        if path.name.startswith("refuse-"):
            continue  # these are supposed to be refused
        raw = json.loads(path.read_text())
        reason = should_refuse(Ticket.model_validate({**raw, "repository": "."}))
        if reason:
            refused[path.name] = reason
    assert not refused, f"legitimate seed tickets were refused: {refused}"


@pytest.mark.xfail(strict=True, reason=(
    "FINDING: AcceptanceResult.command records the ticket's string, not the argv we ran, so "
    "the report does not reproduce the verdict it reports. resolve() prepends "
    "('-o','addopts=') and nothing surfaces that: report.json / diff.patch / the demo output "
    "all print `pytest tests/...`. On exactly the repo the forced arg exists to defend "
    "against — one with `addopts = --co` in its ini — the gate correctly fails while the "
    "recorded command, run by hand, exits 0. A reviewer reproducing our own evidence gets "
    "the opposite answer, which is the worst version of a reporting gap. Fix: record the "
    "effective argv (or both) on AcceptanceResult; resolve() already returns it."
))
def test_the_recorded_command_reproduces_the_gates_verdict(repo: Path, tmp_path: Path):  # noqa: F811
    (repo / "pytest.ini").write_text("[pytest]\naddopts = --co\n")
    _commit(repo, "a repo whose ini would silence its own suite")
    report, _ = _run(repo, tmp_path, "Fix the ownership check on the order read path",
                     RED_GATE, fake=_fake_workflow(outcome=Outcome.FAILURE))
    assert report.acceptance is not None
    by_hand = subprocess.run(
        shlex.split(report.acceptance.command), cwd=repo,
        capture_output=True, text=True, check=False,
    )
    assert by_hand.returncode == report.acceptance.exit_code, (
        f"recorded {report.acceptance.command!r} exits {by_hand.returncode} by hand but the "
        f"gate reported {report.acceptance.exit_code}"
    )


@pytest.mark.xfail(strict=True, reason=(
    "FINDING: the new worktrees_dir default is a predictable name in a shared directory, and "
    "nothing checks who owns it. `Path(tempfile.gettempdir()) / 'autodev-worktrees'` is "
    "per-user and 0700 on macOS, but on Linux/CI/containers TMPDIR is usually unset, so "
    "gettempdir() is '/tmp' and the default becomes the fixed path '/tmp/autodev-worktrees' "
    "inside a world-writable sticky dir (verified: `env -u TMPDIR python -c` prints exactly "
    "that). ensure_dirs() does `mkdir(parents=True, exist_ok=True)` with no ownership or "
    "symlink check, and Worktree.create() resolves base_dir before its containment check, so "
    "a base dir another local user pre-created as a symlink is silently adopted and every "
    "worktree — target source, our diffs, and the tree the gate executes — lands where they "
    "choose. That is a write foothold inside the jail we are about to run tests in. The old "
    ".data/worktrees default was inside our own repo and had none of this exposure, so this "
    "is new with the move. Fix: include the uid in the name and create mode=0o700, or use "
    "mkdtemp; and refuse a base_dir that is a symlink or not owned by us. Repro below."
))
def test_a_symlinked_worktrees_base_is_not_adopted(repo: Path, tmp_path: Path):  # noqa: F811
    elsewhere = tmp_path / "attacker_controlled"
    elsewhere.mkdir()
    base = tmp_path / "worktrees-base"
    base.symlink_to(elsewhere, target_is_directory=True)  # pre-created by someone else
    wt = Worktree.create(repo, "symlinked-base", base)
    try:
        landed_in_attacker_dir = elsewhere in wt.root.parents or elsewhere == wt.root.parent
        assert not landed_in_attacker_dir, (
            f"the worktree was created under a symlinked base: {wt.root}"
        )
    finally:
        wt.remove()
