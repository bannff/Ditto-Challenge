"""Workspace checkpoints: rollback to verified state, and the revert trap they create.

The first test here is the important one. Adding checkpoint commits silently breaks
`revert()` if it stays anchored to HEAD, because HEAD becomes the checkpoint — so "a change
that breaks tests is reverted" would quietly mean "reverted to the last unverified
checkpoint". Nothing in the existing suite would have caught that.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from self_improving_coding_agent.worktree import (
    CHECKPOINT_REF_PREFIX,
    Worktree,
)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["init", "-q"],
        ["config", "user.email", "t@t.t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", "-C", str(path), *cmd], check=True)
    (path / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)
    return path


def _worktree(tmp_path, run_id="run-cp") -> tuple[Path, Worktree]:
    repo = _init_repo(tmp_path / "repo")
    return repo, Worktree.create(repo, run_id, tmp_path / "wt")


def _refs(repo: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo), "for-each-ref", "--format=%(refname)",
         f"{CHECKPOINT_REF_PREFIX}*"],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.split()


# ---- the regression checkpoints would otherwise introduce -----------------------


def test_revert_undoes_everything_even_after_a_checkpoint_commit(tmp_path):
    """`reset --hard HEAD` would keep the checkpointed change, because HEAD *is* the
    checkpoint. Revert has to anchor to the commit the run started from."""
    _, wt = _worktree(tmp_path)
    (wt.root / "app.py").write_text("x = 2  # a change that passed its eval\n")
    assert wt.checkpoint("implement") is not None
    (wt.root / "app.py").write_text("x = 3  # and then a change that broke the tests\n")

    wt.revert()

    assert (wt.root / "app.py").read_text() == "x = 1\n"  # back to the seed
    assert wt.is_clean()
    assert wt.diff() == ""  # so the reported evidence is empty, not a partial patch


def test_revert_removes_untracked_files_a_checkpoint_never_saw(tmp_path):
    _, wt = _worktree(tmp_path)
    (wt.root / "app.py").write_text("x = 2\n")
    wt.checkpoint("implement")
    (wt.root / "scratch_notes.txt").write_text("left behind\n")

    wt.revert()

    assert not (wt.root / "scratch_notes.txt").exists()
    assert wt.is_clean()


def test_the_seed_is_fixed_at_creation_not_re_read(tmp_path):
    # If the seed were re-read later it would drift onto the checkpoint, which is exactly
    # the bug the anchoring is meant to prevent.
    _, wt = _worktree(tmp_path)
    seed = wt.seed
    (wt.root / "app.py").write_text("x = 2\n")
    wt.checkpoint("implement")

    assert wt.seed == seed
    assert wt.head_hash() != seed


# ---- checkpoint and restore ----------------------------------------------------


def test_a_checkpoint_can_be_restored_after_a_bad_change(tmp_path):
    _, wt = _worktree(tmp_path)
    (wt.root / "app.py").write_text("good = True\n")
    commit = wt.checkpoint("implement")
    assert commit is not None
    (wt.root / "app.py").write_text("broken = True\n")
    (wt.root / "junk.py").write_text("junk\n")

    assert wt.restore(commit) is True

    assert (wt.root / "app.py").read_text() == "good = True\n"
    assert not (wt.root / "junk.py").exists()


def test_restoring_the_seed_is_allowed(tmp_path):
    # The first node can fail before any checkpoint exists, so the seed is the fallback.
    _, wt = _worktree(tmp_path)
    (wt.root / "app.py").write_text("bad\n")

    assert wt.restore(wt.seed) is True
    assert (wt.root / "app.py").read_text() == "x = 1\n"


def test_a_clean_tree_produces_no_checkpoint(tmp_path):
    # A node that changed nothing (discover, verify) shouldn't mint an empty commit.
    _, wt = _worktree(tmp_path)
    assert wt.checkpoint("discover") is None


def test_successive_checkpoints_each_restore_their_own_tree(tmp_path):
    _, wt = _worktree(tmp_path)
    (wt.root / "app.py").write_text("step = 1\n")
    first = wt.checkpoint("implement")
    (wt.root / "app.py").write_text("step = 2\n")
    second = wt.checkpoint("implement")

    assert first is not None and second is not None
    assert first != second
    assert wt.restore(first) is True
    assert (wt.root / "app.py").read_text() == "step = 1\n"
    assert wt.restore(second) is True
    assert (wt.root / "app.py").read_text() == "step = 2\n"


# ---- restore refuses anything that isn't ours ----------------------------------


@pytest.mark.parametrize(
    "bad",
    ["--upstream", "-f", "HEAD", "refs/heads/main", "; rm -rf /", "", "zzzz", "../../etc"],
)
def test_restore_refuses_a_hash_that_isnt_a_hash(tmp_path, bad):
    # The hash arrives from a ledger payload, so it is untrusted text. A leading dash would
    # otherwise be read by git as a flag.
    _, wt = _worktree(tmp_path)
    assert wt.restore(bad) is False


def test_restore_refuses_a_commit_from_outside_this_run(tmp_path):
    """Restoring an arbitrary commit is not recovery. Only this run's own history counts."""
    repo, wt = _worktree(tmp_path)
    other = _init_repo(tmp_path / "other")
    (other / "hostile.py").write_text("import os\n")
    subprocess.run(["git", "-C", str(other), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(other), "commit", "-q", "-m", "hostile"], check=True)
    foreign = subprocess.run(
        ["git", "-C", str(other), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert wt.restore(foreign) is False


def test_restore_refuses_a_commit_that_predates_the_run(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    first = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    (repo / "app.py").write_text("x = 99\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "second"], check=True)
    wt = Worktree.create(repo, "run-old", tmp_path / "wt")

    # `first` is an ancestor of the seed, not a descendant — not a state this run produced.
    assert wt.restore(first) is False


# ---- the ref namespace ---------------------------------------------------------


def test_checkpoints_are_not_branches(tmp_path):
    """A retained branch of unverified work is a merge accident waiting to happen."""
    repo, wt = _worktree(tmp_path)
    (wt.root / "app.py").write_text("x = 2\n")
    wt.checkpoint("implement")

    branches = subprocess.run(
        ["git", "-C", str(repo), "for-each-ref", "--format=%(refname)", "refs/heads/*"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()

    assert wt.checkpoint_ref in _refs(repo)
    assert not any(ref.startswith("refs/heads/autodev/") and "run-cp" in ref for ref in branches) \
        or wt.checkpoint_ref not in branches


def test_a_checkpoint_survives_the_run_branch_being_deleted(tmp_path):
    # The ref is what keeps recovery possible after a failed run tears its branch down.
    repo, wt = _worktree(tmp_path)
    (wt.root / "app.py").write_text("x = 2\n")
    commit = wt.checkpoint("implement")
    wt.remove(keep_branch=False)

    assert _refs(repo) == [wt.checkpoint_ref]
    shown = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-t", str(commit)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert shown.stdout.strip() == "commit"  # still reachable


def test_pruning_keeps_the_most_recent_refs_and_never_touches_branches(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    for i in range(5):
        wt = Worktree.create(repo, f"run-{i}", tmp_path / "wt")
        # Never the seed's own content, or the tree is clean and no checkpoint is made.
        (wt.root / "app.py").write_text(f"x = {i + 100}\n")
        wt.checkpoint("implement")
        wt.remove(keep_branch=False)
    assert len(_refs(repo)) == 5
    before = subprocess.run(
        ["git", "-C", str(repo), "for-each-ref", "--format=%(refname)", "refs/heads/*"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()

    removed = Worktree.prune_checkpoints(repo, keep=2)

    assert removed == 3
    assert len(_refs(repo)) == 2
    after = subprocess.run(
        ["git", "-C", str(repo), "for-each-ref", "--format=%(refname)", "refs/heads/*"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert after == before  # branches untouched


def test_pruning_a_repo_with_no_checkpoints_is_a_no_op(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    assert Worktree.prune_checkpoints(repo, keep=2) == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
