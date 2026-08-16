"""Regression guards for issues.md #2 — `.git` is inside the jail.

Containment alone was not enough: `<root>/.git` resolved inside the worktree, so the
agent's own `write_file` could replace the gitdir pointer with one aimed at a repo it had
just written. From there our own plumbing did the work — `filter.<name>.clean` runs through
/bin/sh on `git add`, and `core.worktree` re-aimed `revert()`'s `reset --hard` / `clean -fd`
at a directory outside the worktree.

Same harness as the files next door (stubbed graph, real jail). Two independent controls are
asserted separately, because either one alone would leave the other's route open:

1. the tool layer refuses any path with a `.git` component, case-folded;
2. every in-worktree git call pins `--git-dir`/`--work-tree`, so a pointer file written by
   something *other* than the agent (gate code runs as our uid — issues.md #13) is inert.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from test_hostile_tickets import (  # the shared harness lives next door
    _worktree,
    repo,  # noqa: F401 — imported fixture
)

from self_improving_coding_agent.tools import make_worktree_tools
from self_improving_coding_agent.worktree import WorktreeEscape

# Every spelling that reaches `<root>/.git` on a case-insensitive (APFS/NTFS) or
# ignorable-codepoint (HFS+) filesystem, plus the nested-repo variant.
DENIED = [
    ".git", ".git/config", ".git/hooks/pre-commit", ".git/info/attributes",
    ".GIT/config", ".Git/config", ".gIt", "sub/.git", "sub/.git/config",
    "a/b/../../.git/config", ".git.", ".git ", ".g\u200cit/config",
]

# Files whose names merely start with `.git`. A ticket may legitimately edit any of these,
# so the predicate has to match a whole component, never a prefix.
ALLOWED = [
    ".gitignore", ".gitattributes", ".gitmodules", ".gitkeep",
    ".github/workflows/ci.yml", ".github/CODEOWNERS", "src/.gitignore",
    "docs/.gitbook.yaml", "gitlab.py", "digit.py", "src/git/plumbing.py",
]


def _fake_repo(write_file, wt, config: str) -> None:
    """Stage a second, agent-owned repo inside the jail and point `.git` at it.

    Every step is an ordinary write_file call — HEAD, a branch ref, an alternates file so the
    real objects still resolve, the config carrying the payload, then the pointer.
    """
    real = subprocess.run(["git", "-C", str(wt.root), "rev-parse", "--absolute-git-dir"],
                          capture_output=True, text=True, check=False).stdout.strip()
    head = subprocess.run(["git", "-C", str(wt.root), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=False).stdout.strip()
    write_file("inner/HEAD", f"ref: refs/heads/{wt.branch}\n")
    write_file(f"inner/refs/heads/{wt.branch}", f"{head}\n")
    write_file("inner/objects/info/alternates", f"{Path(real).parent.parent / 'objects'}\n")
    write_file("inner/config", config)
    write_file(".git", "gitdir: ./inner\n")


def test_write_file_cannot_make_our_own_git_run_a_shell_command(repo: Path, tmp_path: Path):  # noqa: F811
    # The headline: a `clean` filter is executed by git through /bin/sh on `add`, so commit()
    # was an execution primitive for anything the agent could write.
    wt = _worktree(repo, tmp_path, "gitdir-rce")
    marker = tmp_path / "GIT_FILTER_EXECUTED"
    try:
        _, write_file, _ = make_worktree_tools(wt)
        _fake_repo(write_file, wt, "[core]\n\trepositoryformatversion = 0\n"
                                   f'[filter "p"]\n\tclean = touch {marker}; cat\n')
        write_file(".gitattributes", "* filter=p\n")
        write_file("service.py", "# edited\n")
        wt.is_clean()
        wt.diff()
        wt.commit("agent change")
        assert not marker.exists(), "our own git ran an attacker command through /bin/sh"
    finally:
        wt.remove()


def test_write_file_cannot_aim_revert_at_files_outside_the_jail(repo: Path, tmp_path: Path):  # noqa: F811
    # core.worktree re-points the work tree, so revert()'s `clean -fd` deleted a file outside
    # the worktree and is_clean() then reported success.
    wt = _worktree(repo, tmp_path, "gitdir-revert")
    victim_dir = tmp_path / "not-the-worktree"
    victim_dir.mkdir()
    victim = victim_dir / "precious.txt"
    victim.write_text("do not delete\n")
    try:
        _, write_file, _ = make_worktree_tools(wt)
        _fake_repo(write_file, wt, "[core]\n\trepositoryformatversion = 0\n"
                                   f"\tworktree = {victim_dir}\n")
        wt.revert()
        assert victim.exists(), "our own `clean -fd` deleted a file outside the worktree"
    finally:
        wt.remove()


@pytest.mark.parametrize("path", DENIED)
def test_git_metadata_is_refused_by_every_file_tool(repo: Path, tmp_path: Path, path: str):  # noqa: F811
    # `.git` is the repo's control plane, not source. Refused at the tool, in code.
    wt = _worktree(repo, tmp_path, f"denied-{abs(hash(path)) % 10**6}")
    try:
        read_file, write_file, list_files = make_worktree_tools(wt)
        with pytest.raises(WorktreeEscape):
            wt.safe_path(path)
        assert write_file(path, "x").startswith("refused:")
        assert read_file(path).startswith("refused:")
        assert list_files(path).startswith("refused:")
    finally:
        wt.remove()


@pytest.mark.parametrize("path", ALLOWED)
def test_files_whose_names_merely_start_with_git_still_work(repo: Path, tmp_path: Path,  # noqa: F811
                                                            path: str):
    # The predicate matches a whole component, so ordinary repo content is unaffected — a
    # ticket that edits .gitignore or a GitHub workflow must still be possible.
    wt = _worktree(repo, tmp_path, f"allowed-{abs(hash(path)) % 10**6}")
    try:
        read_file, write_file, _ = make_worktree_tools(wt)
        assert write_file(path, "content\n").startswith("wrote")
        assert read_file(path) == "content\n"
    finally:
        wt.remove()


def test_a_clobbered_pointer_written_outside_the_tools_is_inert(repo: Path, tmp_path: Path):  # noqa: F811
    # Independent derivation: gate code runs as our uid and can write `.git` directly, with
    # no tool involved. The pinned --git-dir/--work-tree means git never reads that file, so
    # our evidence and cleanup still describe the real repository.
    wt = _worktree(repo, tmp_path, "gitdir-pinned")
    marker = tmp_path / "PIN_BYPASSED"
    (wt.root / "inner").mkdir()
    (wt.root / "inner" / "HEAD").write_text(f"ref: refs/heads/{wt.branch}\n")
    (wt.root / "inner" / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n"
        f'[filter "p"]\n\tclean = touch {marker}; cat\n'
    )
    (wt.root / ".gitattributes").write_text("* filter=p\n")
    (wt.root / "service.py").write_text("# edited by the agent\n")  # a tracked file
    (wt.root / ".git").write_text("gitdir: ./inner\n")
    try:
        assert not wt.is_clean()  # git still sees the real repo, not the planted one
        assert "service.py" in wt.diff(), "the diff no longer describes the real repository"
        assert wt.head_hash()
        assert wt.commit("still the real repo") is True
        assert not marker.exists(), "the planted config was read despite the pin"
    finally:
        wt.remove()  # must succeed: a clobbered pointer used to strand the worktree
    assert not wt.root.exists(), "a clobbered `.git` left the worktree behind"
