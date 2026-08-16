import subprocess
import time
from pathlib import Path

import pytest

from self_improving_coding_agent.worktree import Worktree, WorktreeError, WorktreeEscape


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)
    return path


@pytest.fixture
def worktree(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    wt = Worktree.create(repo, "T-001", tmp_path / "worktrees")
    yield wt
    wt.remove()


def test_isolated_branch_and_worktree(worktree):
    assert worktree.branch == "autodev/T-001"
    assert (worktree.root / "app.py").exists()


def test_safe_path_allows_inside(worktree):
    assert worktree.root in worktree.safe_path("src/new.py").parents


@pytest.mark.parametrize("escape", ["../outside.txt", "../../etc/passwd", "/etc/passwd"])
def test_safe_path_rejects_escape(worktree, escape):
    with pytest.raises(WorktreeEscape):
        worktree.safe_path(escape)


@pytest.mark.parametrize("run_id", ["../evil", "a/b", "..", ".hidden/../../x"])
def test_create_rejects_bad_run_id(tmp_path, run_id):
    repo = _init_repo(tmp_path / "repo")
    with pytest.raises(WorktreeError):
        Worktree.create(repo, run_id, tmp_path / "worktrees")


def test_acceptance_rejects_non_allowlisted(worktree):
    for cmd in ("curl http://evil", "git push --force", "rm -rf /"):
        with pytest.raises(WorktreeError):
            worktree.run_acceptance(cmd)


def test_acceptance_blocks_inline_code(worktree):
    with pytest.raises(WorktreeError):
        worktree.run_acceptance('python -c "import os"')


def test_acceptance_no_shell_operator_execution(worktree):
    # No shell, so '&&' could only ever be a literal argument — and the policy refuses the
    # smuggled tokens outright rather than passing them through to the runner.
    with pytest.raises(WorktreeError):
        worktree.run_acceptance("pytest --co -q && rm -rf .")
    assert worktree.root.exists()
    assert (worktree.root / "app.py").exists()


def test_child_env_has_no_secrets(worktree, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "SHOULD_NOT_LEAK_123")
    result = worktree.run(
        ["python", "-c", "import os;print(os.environ.get('AWS_SECRET_ACCESS_KEY',''))"]
    )
    assert "SHOULD_NOT_LEAK_123" not in result.output


def test_child_env_isolates_home_from_credentials(worktree):
    # Child test code must not reach the real ~/.aws / ~/.ssh via HOME.
    result = worktree.run(["python", "-c", "import os;print(os.environ['HOME'])"])
    assert result.output.strip() != str(Path.home())


def test_repo_hooks_are_disabled_on_create(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    hook = repo / ".git" / "hooks" / "post-checkout"
    sentinel = tmp_path / "pwned"
    hook.write_text(f"#!/bin/sh\ntouch {sentinel}\n")
    hook.chmod(0o755)
    wt = Worktree.create(repo, "T-hook", tmp_path / "worktrees")
    try:
        assert not sentinel.exists()  # hooksPath=/dev/null blocked the hook
    finally:
        wt.remove()


def test_remove_leaves_no_worktree(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    wt = Worktree.create(repo, "T-002", tmp_path / "worktrees")
    root = wt.root
    wt.remove()
    assert not root.exists()


def _outlive_the_budget(worktree, tmp_path, label: str, *, setsid: bool = False) -> Path:
    """Run a gate that spawns a helper and then hangs past its timeout.

    Returns the path the grandchild writes *after* the timeout has passed, so a caller
    asserts on whether it survived. `setsid` makes the grandchild leave the process group.
    """
    started = tmp_path / f"{label}-started"
    survived = tmp_path / f"{label}-survived"
    grandchild = (
        ("import os; os.setsid()\n" if setsid else "")
        + f"import pathlib,time\npathlib.Path({str(started)!r}).write_text('x')\n"
        f"time.sleep(3)\npathlib.Path({str(survived)!r}).write_text('x')\n"
    )
    spawner = (
        "import subprocess,sys,time\n"
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}])\n"
        "time.sleep(600)\n"  # the parent outlives its budget on purpose
    )
    result = worktree.run(["python3", "-c", spawner], timeout=2)

    assert result.exit_code == 124
    assert "timed out" in result.output
    assert started.exists(), "the grandchild never ran, so this proves nothing"
    time.sleep(5)  # longer than the grandchild's own sleep
    return survived


def test_timeout_kills_the_whole_process_group(worktree, tmp_path):
    # A gate that spawns a helper must not leave it running past the budget: the child gets
    # its own session, so the timeout kills the group, not just the direct child.
    survived = _outlive_the_budget(worktree, tmp_path, "grandchild")
    assert not survived.exists(), "a detached grandchild outlived the timeout"


@pytest.mark.xfail(strict=True, reason=(
    "FINDING: the timeout kills the child's process group, but a grandchild that calls "
    "os.setsid() leaves that group and survives the budget ceiling. The gate runs the "
    "target's own (untrusted) test code, so a ticket's repo can leave a process running "
    "after the run is reported done — the wall-clock bound is not enforced on anything that "
    "detaches. Repro: a test that Popen's a helper calling os.setsid() then sleeping; "
    "worktree.run(..., timeout=2) returns 124 while the helper keeps running. A durable fix "
    "needs an OS-level container/cgroup (the DESIGN 'Uplevel' FS/process sandbox); killpg "
    "alone cannot reach a process that reparents itself."
))
def test_timeout_reaches_a_grandchild_that_left_the_process_group(worktree, tmp_path):
    survived = _outlive_the_budget(worktree, tmp_path, "escapee", setsid=True)
    assert not survived.exists(), "a re-sessioned grandchild outlived the timeout"
