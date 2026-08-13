import subprocess
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
    # allowlisted runner, but '&&' is a literal arg — the second command never runs
    worktree.run_acceptance("pytest --co -q && rm -rf .")
    assert worktree.root.exists()
    assert (worktree.root / "app.py").exists()


def test_child_env_has_no_secrets(worktree, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "SHOULD_NOT_LEAK_123")
    result = worktree.run(
        ["python", "-c", "import os;print(os.environ.get('AWS_SECRET_ACCESS_KEY',''))"]
    )
    assert "SHOULD_NOT_LEAK_123" not in result.output


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
