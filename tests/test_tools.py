import subprocess
from pathlib import Path

import pytest

from self_improving_coding_agent.tools import make_worktree_tools
from self_improving_coding_agent.worktree import Worktree


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
def tools(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    wt = Worktree.create(repo, "T-tools", tmp_path / "worktrees")
    by_name = {t.tool_name: t for t in make_worktree_tools(wt)}
    yield by_name
    wt.remove()


def test_read_and_write_inside_jail(tools):
    assert "wrote" in tools["write_file"]("src/new.py", "print('hi')\n")
    assert tools["read_file"]("src/new.py") == "print('hi')\n"
    assert "app.py" in tools["list_files"](".")


def test_write_outside_jail_is_refused(tools):
    result = tools["write_file"]("../escape.py", "evil")
    assert result.startswith("refused")


def test_read_outside_jail_is_refused(tools):
    assert tools["read_file"]("/etc/passwd").startswith("refused")


def test_read_missing_file(tools):
    assert "no such file" in tools["read_file"]("does/not/exist.py")
