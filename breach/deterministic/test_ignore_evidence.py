"""An agent-authored ignore rule must not decide what our own evidence contains.

`.gitignore` is legitimately writable — adding a build artifact to it is real work, and it is
in the ALLOWED list next door for that reason. But git applies ignore rules to *our*
bookkeeping too, so a rule the agent writes edits what `is_clean()`, `diff()` and `add -A`
can see. The headline is not "a stray file survives": it is that the branch we ship stops
reproducing the verdict we reported. The gate goes green in the worktree and non-zero on the
branch, while the run says SUCCESS and the diff a judge reads omits the file responsible.

Trust is per *pattern* and anchored to the run's base commit, the same shape as
`_gate_config`'s: a rule stays in force if the line that matched shipped with the target. So
the legitimate cases below — a target that ignores `.venv/`, a ticket that appends `dist/` —
have to come out byte-identical, and those assertions matter more than the attack ones.

Same harness as the files next door: stubbed graph, real jail, real git, no model.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from test_hostile_tickets import (  # the shared harness lives next door
    GREEN_GATE,
    _assert_target_untouched,
    _branch,
    _fake_workflow,
    _git,
    _head,
    _run,
    _worktree,
    repo,  # noqa: F401 — imported fixture
)

from self_improving_coding_agent.contracts import Outcome
from self_improving_coding_agent.tools import make_worktree_tools
from self_improving_coding_agent.worktree import MAX_HIDDEN_FILES

# The file the hidden module is imported from, so the gate genuinely depends on it.
IMPORTER = "tests/test_hidden.py"
HIDDEN = "stash.py"
HIDDEN_BODY = "VALUE = 42\n"
IMPORTER_BODY = "from stash import VALUE\n\n\ndef test_value():\n    assert VALUE == 42\n"


def _seed_ignore(repo_path: Path, body: str) -> None:
    """Give the target a committed .gitignore, so its rules are seed-trusted."""
    (repo_path / ".gitignore").write_text(body)
    subprocess.run(["git", "-C", str(repo_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo_path), "commit", "-q", "-m", "ignore"], check=True)


def _tools(repo_path: Path, tmp_path: Path, name: str):
    wt = _worktree(repo_path, tmp_path, name)
    write_file = next(t for t in make_worktree_tools(wt) if t.tool_name == "write_file")
    return wt, write_file


# --- the attack ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ignore_body",
    [
        f"{HIDDEN}\n.gitignore\n",  # self-ignoring: also hides the rule that hides the file
        f"{HIDDEN}\n",  # the plain form; the is_clean() half needs no cleverness
    ],
)
def test_a_rule_this_run_wrote_cannot_hide_a_file_from_our_own_evidence(
    repo,  # noqa: F811
    tmp_path: Path,
    ignore_body: str,
):
    wt, write_file = _tools(repo, tmp_path, "hide")
    try:
        write_file(".gitignore", ignore_body)
        write_file(HIDDEN, HIDDEN_BODY)

        assert not wt.is_clean(), "a file this run hid is still a change this run made"
        assert HIDDEN in wt.diff(), "the reviewer's evidence omitted the hidden file"
        assert wt.commit("ship") is True
        tree = _git(repo, "ls-tree", "-r", "--name-only", wt.branch)
        assert HIDDEN in tree, "the shipped branch omitted the file"
    finally:
        wt.remove()


def test_the_shipped_branch_reproduces_the_verdict_we_reported(repo, tmp_path: Path):  # noqa: F811
    """The one a judge would run. Everything else here is a proxy for this.

    The ticket's own acceptance command passes inside the worktree because `stash.py` is
    present. Before the fix that file was ignored, so it never reached the branch, and the
    same command exited non-zero when re-run against what we shipped — while the report said
    SUCCESS with a diff that didn't mention it.
    """
    head, branch = _head(repo), _branch(repo)
    writes = {
        ".gitignore": f"{HIDDEN}\n.gitignore\n",
        HIDDEN: HIDDEN_BODY,
        IMPORTER: IMPORTER_BODY,
    }
    report, _ = _run(
        repo,
        tmp_path,
        "Add a value module and a test that imports it.",
        f"pytest {IMPORTER}",
        fake=_fake_workflow(writes=writes),
    )
    assert report.outcome == Outcome.SUCCESS
    assert report.branch is not None
    assert HIDDEN in report.evidence, "the report's own diff hid the file"

    # Re-run the ticket's gate against the branch we shipped, in a throwaway worktree.
    checkout = tmp_path / "verify"
    assert _git(repo, "worktree", "add", "-q", "--detach", str(checkout), report.branch) == ""
    try:
        gate = subprocess.run(
            ["python", "-m", "pytest", IMPORTER, "-q"],
            cwd=checkout,
            capture_output=True,
            text=True,
            check=False,
        )
        assert gate.returncode == 0, (
            f"the shipped branch does not pass the ticket's own gate:\n{gate.stdout}"
        )
    finally:
        _git(repo, "worktree", "remove", "--force", str(checkout))
    _assert_target_untouched(repo, head, branch)


def test_a_checkpoint_commits_the_hidden_file_too(repo, tmp_path: Path):  # noqa: F811
    """`commit()` has to stand on its own, because `checkpoint()` is the one caller that does
    not run `diff()` first.

    That ordering matters more than it looks: `diff()`'s `add -Nf` makes a surfaced path
    tracked, after which a plain `add -A` picks it up — so with the workflow's diff-then-commit
    sequence the staging inside `commit()` looks redundant. Via `checkpoint()` there is no
    `diff()`, the path is still untracked and still ignored, and the checkpoint silently omits
    it. A later `restore()` would then hand the next attempt a tree missing the file, which is
    the half-applied state checkpoints exist to prevent.
    """
    wt, write_file = _tools(repo, tmp_path, "ckhide")
    try:
        write_file(".gitignore", f"{HIDDEN}\n.gitignore\n")
        write_file(HIDDEN, HIDDEN_BODY)
        commit = wt.checkpoint("implement")  # no diff() call anywhere on this path
        assert commit is not None, "a tree with a change must produce a checkpoint"
        tree = _git(repo, "ls-tree", "-r", "--name-only", commit)
        assert HIDDEN in tree, "the checkpoint omitted the file the run hid"
        assert ".gitignore" in tree
    finally:
        wt.remove()


def test_a_nested_ignore_file_this_run_wrote_is_untrusted_whole(repo, tmp_path: Path):  # noqa: F811
    """Guards the `--ignored=matching` choice.

    With git's default `traditional` mode an untracked directory of ignored files collapses
    to a single `!! sub/` entry, which `check-ignore` then reports as *not* ignored — so this
    case would pass through unclassified and the file would stay hidden.
    """
    wt, write_file = _tools(repo, tmp_path, "nested")
    try:
        write_file("sub/.gitignore", "hidden.py\n")
        write_file("sub/hidden.py", "PAYLOAD = 1\n")
        assert not wt.is_clean()
        assert "sub/hidden.py" in wt.diff()
    finally:
        wt.remove()


def test_an_exclude_file_only_gate_code_could_write_is_untrusted(repo, tmp_path: Path):  # noqa: F811
    """`<repo>/.git/info/exclude` is outside the jail, so no tool can reach it — but gate
    code runs as our uid (issues.md #13), and `core.excludesFile=/dev/null` doesn't cover it.
    It has no seed blob, so every rule in it is untrusted and fails closed."""
    wt, write_file = _tools(repo, tmp_path, "excl")
    try:
        exclude = repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(f"{HIDDEN}\n")
        write_file(HIDDEN, HIDDEN_BODY)  # the tool itself never touches .git
        assert not wt.is_clean()
        assert HIDDEN in wt.diff()
    finally:
        wt.remove()


def test_a_rule_that_would_surface_a_whole_ignored_tree_refuses_instead_of_shipping_it(
    repo,  # noqa: F811
    tmp_path: Path,
):
    """The ceiling, and why it exists.

    Appending `*` to a committed `.gitignore` makes that pattern the winning one for every
    pre-existing ignored path too, so naive surfacing would commit the target's whole `.venv`.
    Refusing is strictly better for us *and* worse for an attacker than today's silent
    success: nothing ships, the tree is clean, and the report says why.
    """
    _seed_ignore(repo, "vendor/\n")
    head, branch = _head(repo), _branch(repo)
    writes = {"vendor/" + f"f{i}.txt": "x\n" for i in range(MAX_HIDDEN_FILES + 20)}
    writes[".gitignore"] = "vendor/\n*\n"
    writes[HIDDEN] = HIDDEN_BODY
    report, _ = _run(
        repo,
        tmp_path,
        "Vendor the dependency tree and ignore it.",
        GREEN_GATE,
        fake=_fake_workflow(writes=writes),
    )
    assert report.outcome == Outcome.FAILURE, "a tree that must not ship is not a success"
    assert report.branch is None, "nothing may be advertised as shippable"
    assert "not shipped" in report.evidence and "hide" in report.evidence
    assert len(report.evidence) < 100_000, "the refusal must not embed the tree it refused"
    _assert_target_untouched(repo, head, branch)


# --- the legitimate cases, which matter more ---------------------------------------------


def test_a_target_that_ignores_its_own_noise_is_completely_unaffected(repo, tmp_path: Path):  # noqa: F811
    """The regression that would make this fix worse than the bug.

    `.venv/`, `__pycache__/` and `.coverage` are ignored by a rule the *target* committed, so
    they stay ignored: absent from the diff, absent from the commit, and not making the tree
    look dirty. If this fails, every ordinary run starts reverting good work.
    """
    _seed_ignore(repo, ".venv/\n__pycache__/\n.coverage\n")
    wt, write_file = _tools(repo, tmp_path, "legit")
    try:
        for rel in (".venv/lib/x.py", "__pycache__/m.pyc", ".coverage"):
            target = wt.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("noise\n")
        assert wt._ignored_by_this_run() == []
        assert wt.is_clean(), "target-ignored noise must not read as a change"

        write_file("service.py", "# an ordinary edit\n")
        assert not wt.is_clean()
        diff = wt.diff()
        assert "service.py" in diff
        assert ".venv" not in diff and "__pycache__" not in diff and ".coverage" not in diff
        assert wt.commit("ordinary") is True
        tree = _git(repo, "ls-tree", "-r", "--name-only", wt.branch)
        assert ".venv/lib/x.py" not in tree and "__pycache__/m.pyc" not in tree
    finally:
        wt.remove()


def test_appending_a_rule_to_a_committed_ignore_file_keeps_the_existing_rules_trusted(
    repo,  # noqa: F811
    tmp_path: Path,
):
    """Why trust is per *pattern* and not per file.

    Adding `dist/` to a committed `.gitignore` is ordinary work. Were trust per-file, that one
    edit would untrust `.venv/` as well and dump the whole virtualenv into the diff — which is
    the `add -Af` failure mode, arriving by accident instead of by attack.
    """
    _seed_ignore(repo, ".venv/\n")
    wt, write_file = _tools(repo, tmp_path, "append")
    try:
        (wt.root / ".venv").mkdir(parents=True, exist_ok=True)
        (wt.root / ".venv" / "big.py").write_text("noise\n")
        write_file(".gitignore", ".venv/\ndist/\n")
        (wt.root / "dist").mkdir(parents=True, exist_ok=True)
        (wt.root / "dist" / "wheel.whl").write_text("built\n")

        surfaced = wt._ignored_by_this_run()
        assert ".venv/" not in surfaced, "a committed rule must survive an unrelated append"
        assert "dist/" in surfaced, "a rule this run added does not get to hide its output"
        diff = wt.diff()
        assert ".gitignore" in diff and ".venv/big.py" not in diff
    finally:
        wt.remove()


def test_the_reported_diff_covers_the_whole_run_not_just_since_the_last_checkpoint(
    repo,  # noqa: F811
    tmp_path: Path,
):
    """`diff()` is anchored to the seed for the same reason `revert()` is.

    `checkpoint()` commits the agent's own work, so with HEAD as the base the evidence
    describes only the tail of the run. Live runs checkpoint after every passing node, so this
    was understating nearly every report.
    """
    wt, write_file = _tools(repo, tmp_path, "ckpt")
    try:
        write_file("first.py", "FIRST = 1\n")
        assert wt.checkpoint("understand") is not None
        write_file("second.py", "SECOND = 2\n")
        diff = wt.diff()
        assert "second.py" in diff
        assert "first.py" in diff, "work already checkpointed vanished from the evidence"
    finally:
        wt.remove()


# --- the write ceiling -------------------------------------------------------------------


def test_an_oversized_write_is_refused_with_feedback_and_leaves_no_file(
    repo,  # noqa: F811
    tmp_path: Path,
):
    """Everything written here is committed into the *target's* object store and stays there.

    Refused rather than truncated: a truncated write would leave the gate verifying a file the
    agent didn't intend, which is worse than no write at all. The error names the limit so the
    agent can split the file instead of retrying the same call.
    """
    wt, write_file = _tools(repo, tmp_path, "big")
    try:
        result = write_file("huge.txt", "x" * (256 * 1024 + 1))
        assert result.startswith("refused:") and "limit" in result
        assert not (wt.root / "huge.txt").exists(), "a refused write must not land"
        assert wt.is_clean()
        ok = write_file("fine.txt", "x" * 1024)
        assert ok.startswith("wrote ")
    finally:
        wt.remove()
