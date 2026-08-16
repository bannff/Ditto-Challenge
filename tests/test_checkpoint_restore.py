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
    WorktreeError,
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


# ---- evidence integrity --------------------------------------------------------
# The diff is the graded artifact. Both of these shrank it silently rather than erroring.


def test_a_new_file_appears_in_the_diff(tmp_path):
    """`git diff HEAD` ignores untracked files, so a ticket that adds a module produced an
    EMPTY diff — the report claimed a change with no evidence of it."""
    _, wt = _worktree(tmp_path)
    (wt.root / "greeting.py").write_text("def hello():\n    return 'hi'\n")

    diff = wt.diff()

    assert "greeting.py" in diff
    assert "def hello()" in diff


def test_a_new_file_in_a_new_directory_appears_too(tmp_path):
    _, wt = _worktree(tmp_path)
    (wt.root / "pkg").mkdir()
    (wt.root / "pkg" / "mod.py").write_text("VALUE = 42\n")

    assert "VALUE = 42" in wt.diff()


def test_the_agent_cannot_author_gitattributes(tmp_path):
    """One line marking a path `-diff` turns a real change into 'Binary files differ' in the
    evidence, and a `filter=` entry runs a command through /bin/sh for whoever runs git in
    that tree next. It is git config that ships inside the tree, so no -c flag disables it."""
    _, wt = _worktree(tmp_path)

    for candidate in (".gitattributes", "sub/.gitattributes", "./.GITATTRIBUTES"):
        with pytest.raises(WorktreeError):
            wt.safe_path(candidate)


def test_a_gitattributes_the_target_shipped_cannot_hide_a_change(tmp_path):
    # The agent can't write one, but a target repo may legitimately ship one — the diff must
    # still show content rather than letting an in-tree attribute decide what a reviewer reads.
    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitattributes").write_text("app.py -diff\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "attrs"], check=True)
    wt = Worktree.create(repo, "run-attrs", tmp_path / "wt")
    (wt.root / "app.py").write_text("x = 999  # the change a reviewer must see\n")

    assert "the change a reviewer must see" in wt.diff()


# ---- revert really discards ----------------------------------------------------


def test_revert_drops_the_checkpoint_ref_so_rejected_work_is_unrecoverable(tmp_path):
    """Otherwise 'a change that breaks tests is reverted, not shipped' is true of the working
    tree and false of the object store: the rejected tree stays reachable in the target repo."""
    repo, wt = _worktree(tmp_path)
    (wt.root / "app.py").write_text("the change the gate rejected\n")
    assert wt.checkpoint("implement") is not None
    assert _refs(repo) == [wt.checkpoint_ref]

    wt.revert()

    assert _refs(repo) == []


def test_a_successful_run_keeps_its_checkpoint_ref(tmp_path):
    # revert() is only called on non-success paths, so a green run's checkpoints survive for
    # recovery; this pins that the ref deletion is scoped to revert and not to teardown.
    repo, wt = _worktree(tmp_path)
    (wt.root / "app.py").write_text("x = 2\n")
    wt.checkpoint("implement")

    wt.remove(keep_branch=True)

    assert _refs(repo) == [wt.checkpoint_ref]


# ---- shipping a checkpointed change --------------------------------------------
# The regression checkpoints introduced: once a node checkpoints, the tree is clean, so a
# bare commit() returns False and a caller reading that as "the agent made no change"
# discards a verified, gate-passing change and reports the ticket unresolved.


def test_a_checkpointed_change_still_ships(tmp_path):
    _, wt = _worktree(tmp_path)
    (wt.root / "app.py").write_text("x = 2  # the fix\n")
    assert wt.checkpoint("implement") is not None
    assert wt.is_clean()  # the precondition that broke shipping
    assert wt.commit("would report nothing") is False  # what the caller used to see

    assert wt.finalize("autodev: resolve T-1") is True
    assert wt.has_committed_change()


def test_shipping_collapses_checkpoints_into_one_honest_commit(tmp_path):
    """Checkpoint messages say the acceptance gate never ran — false once it has. And one
    commit against the seed is the cleanest diff for a reviewer."""
    repo, wt = _worktree(tmp_path)
    (wt.root / "app.py").write_text("step = 1\n")
    wt.checkpoint("implement")
    (wt.root / "app.py").write_text("step = 2\n")
    wt.checkpoint("implement")

    wt.finalize("autodev: resolve T-1")

    log = subprocess.run(
        ["git", "-C", str(wt.root), "log", "--format=%s"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\n")
    assert log[0] == "autodev: resolve T-1"
    assert not any("checkpoint" in line for line in log)
    diff = subprocess.run(
        ["git", "-C", str(repo), "diff", f"{wt.seed}..{wt.branch}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "step = 2" in diff


def test_a_run_that_changed_nothing_still_reports_nothing(tmp_path):
    # The case the caller's INCONCLUSIVE outcome exists for must keep working.
    _, wt = _worktree(tmp_path)
    assert wt.finalize("autodev: resolve T-1") is False
    assert not wt.has_committed_change()


def test_uncommitted_work_after_the_last_checkpoint_still_ships(tmp_path):
    repo, wt = _worktree(tmp_path)
    (wt.root / "app.py").write_text("checkpointed = True\n")
    wt.checkpoint("implement")
    (wt.root / "extra.py").write_text("added_after_the_checkpoint = True\n")

    assert wt.finalize("autodev: resolve T-1") is True

    diff = subprocess.run(
        ["git", "-C", str(repo), "diff", f"{wt.seed}..{wt.branch}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "checkpointed = True" in diff
    assert "added_after_the_checkpoint" in diff


def test_shipping_clears_the_checkpoint_ref(tmp_path):
    # Recovery is for work that did NOT ship; the ref would otherwise dangle at a superseded
    # tree while the branch carries the real thing.
    repo, wt = _worktree(tmp_path)
    (wt.root / "app.py").write_text("x = 2\n")
    wt.checkpoint("implement")
    assert _refs(repo) == [wt.checkpoint_ref]

    wt.finalize("autodev: resolve T-1")

    assert _refs(repo) == []
