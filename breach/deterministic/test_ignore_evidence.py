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

import shutil
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


def test_a_file_named_like_a_pathspec_cannot_cancel_its_own_staging(repo, tmp_path: Path):  # noqa: F811
    """`--` ends *option* parsing, not git *pathspec* magic.

    So a file the agent names `:(exclude)helper.py` is a live exclusion pathspec when the
    surfaced paths are handed to `add -Af -- <paths>`: it cancels the positive pathspec beside
    it, git stages nothing and exits 0, and the entire fix above is undone by one extra write.
    `:(glob)…` is worse — rc 128, the whole add aborts. `:(literal)` on every surfaced path is
    what makes the argv a list of filenames again.
    """
    wt, write_file = _tools(repo, tmp_path, "magic")
    try:
        write_file(".gitignore", "*.py\n")  # untrusted rule: hides both files below
        write_file(HIDDEN, HIDDEN_BODY)
        write_file(f":(exclude){HIDDEN}", "decoy\n")

        assert HIDDEN in wt.diff(), "an exclusion pathspec removed the file from the evidence"
        assert wt.commit("ship") is True
        assert HIDDEN in _git(repo, "ls-tree", "-r", "--name-only", wt.branch)
    finally:
        wt.remove()


def test_deleting_a_negation_line_does_not_hide_new_files(repo, tmp_path: Path):  # noqa: F811
    """Trust has to cover removals, not just additions.

    The allow-list shape (`*` then `!*.py`) is common, and dropping the `!*.py` line leaves a
    pattern that *did* ship with the target now hiding every new `.py` file. The agent authored
    no rule at all, so a per-pattern check on additions alone sees nothing wrong.
    """
    _seed_ignore(repo, "*\n!*.py\n!.gitignore\n")
    wt, write_file = _tools(repo, tmp_path, "denegate")
    try:
        write_file(".gitignore", "*\n!.gitignore\n")  # !*.py removed
        write_file(HIDDEN, HIDDEN_BODY)
        assert HIDDEN in wt.diff()
        assert wt.commit("ship") is True
        assert HIDDEN in _git(repo, "ls-tree", "-r", "--name-only", wt.branch)
    finally:
        wt.remove()


def test_ordinary_un_ignored_build_output_does_not_refuse_a_verified_fix(
    repo,  # noqa: F811
    tmp_path: Path,
):
    """The hidden-files ceiling bounds the *surfaced* set, not the working tree.

    Measured against the whole tree, a target whose test run drops an un-ignored `htmlcov/`
    refused a verified one-line fix — and said "hide 0 path(s)" while doing it, naming a cause
    that wasn't happening. It also fired inside `checkpoint()`, silently losing rollback points.
    """
    wt, write_file = _tools(repo, tmp_path, "cov")
    try:
        write_file("service.py", "# a one-line fix\n")
        coverage = wt.root / "htmlcov"
        coverage.mkdir()
        for i in range(MAX_HIDDEN_FILES + 50):
            (coverage / f"page{i}.html").write_text("x")
        assert wt._why_not_committable(wt._ignored_by_this_run()) is None
        assert wt.commit("ship") is True
        assert wt.checkpoint("verify") is None  # already committed, nothing left to checkpoint
    finally:
        wt.remove()


def test_the_cost_walk_stays_inside_the_jail_and_stays_bounded(repo, tmp_path: Path):  # noqa: F811
    """`is_dir()` follows a symlink, so `rglob` would enumerate a directory outside the jail —
    and those names and sizes then decide whether the run refuses, handing a target repo a way
    to force a failure. The walk also has to stop at the first ceiling it passes, since
    `commit()` has no timeout and this is the only unbounded step on the ship path."""
    outside = tmp_path / "outside"
    outside.mkdir()
    for i in range(40):
        (outside / f"blob{i}.bin").write_text("y" * 10_000)

    wt, _ = _tools(repo, tmp_path, "escape")
    try:
        (wt.root / "link").symlink_to(outside)
        files, size = wt._tree_cost(["link"])
        assert (files, size) == (0, 0), "the walk followed a symlink out of the worktree"

        many = wt.root / "many"
        many.mkdir()
        for i in range(MAX_HIDDEN_FILES * 5):
            (many / f"f{i}.txt").write_text("")
        walked, _ = wt._tree_cost(["many"])
        assert walked <= MAX_HIDDEN_FILES + 2, "the walk did not stop at the ceiling"
    finally:
        wt.remove()


def test_a_rename_cannot_smuggle_an_absolute_path_out_of_status(repo, tmp_path: Path):  # noqa: F811
    """`status -z` emits a rename's old path as a bare field with no status prefix, so shaving
    three characters off it yields `/old.py` — and `self.root / "/old.py"` is `/old.py`, because
    pathlib discards the base for an absolute operand. Nothing can drive rename detection today
    (no delete or rename tool), which is exactly why the guard belongs in now."""
    wt, write_file = _tools(repo, tmp_path, "rename")
    try:
        # The old path has to sit in a subdirectory for this to bite: `"pkg/old.py"[3:]` is
        # `/old.py`, absolute, while a top-level `"old.py"[3:]` is the harmless `.py`.
        write_file("pkg/old_module.py", "V = 1\n")
        assert wt.checkpoint("setup") is not None  # `git mv` needs a tracked file
        subprocess.run(
            ["git", "-C", str(wt.root), "mv", "pkg/old_module.py", "pkg/new_module.py"],
            check=True,
            capture_output=True,
        )
        raw = wt._wt_git("status", "--porcelain", "-z").stdout.split("\0")
        assert any(f.startswith("R") for f in raw), f"git did not record a rename: {raw}"

        # Two separate guarantees, because there are two independent guards and either alone
        # would make the weaker assertion pass: the old-path field is not read as an entry at
        # all, and no path that survives can escape the jail when joined.
        paths = wt._pending_paths()
        assert paths == ["pkg/new_module.py"], f"the old path leaked in as an entry: {paths}"
        assert all(not Path(p).is_absolute() and ".." not in Path(p).parts for p in paths), paths
        assert all(wt.root in (wt.root / p).parents for p in paths), paths
    finally:
        wt.remove()


def test_a_refusal_is_not_reported_for_a_later_unrelated_commit(repo, tmp_path: Path):  # noqa: F811
    """`last_commit_refusal` is read after the fact, so it has to be cleared on entry.

    Otherwise a ceiling tripped by an early `checkpoint()` was still readable once the agent had
    tidied up, and the run reported FAILURE citing a limit it no longer exceeded.
    """
    _seed_ignore(repo, "vendor/\n")
    wt, write_file = _tools(repo, tmp_path, "stale")
    try:
        write_file(".gitignore", "vendor/\n*\n")
        vendor = wt.root / "vendor"
        vendor.mkdir(exist_ok=True)
        for i in range(MAX_HIDDEN_FILES + 20):
            (vendor / f"f{i}.txt").write_text("x")
        assert wt.commit("try") is False
        assert wt.last_commit_refusal is not None

        # Undo everything: the tree is now exactly the seed again.
        subprocess.run(["git", "-C", str(wt.root), "checkout", "--", "."], capture_output=True)
        shutil.rmtree(vendor, ignore_errors=True)
        (wt.root / ".gitignore").write_text("vendor/\n")

        assert wt.commit("try again") is False  # genuinely nothing to commit
        assert wt.last_commit_refusal is None, "a stale reason outlived the tree that caused it"
    finally:
        wt.remove()


def test_leading_whitespace_makes_a_different_ignore_rule(repo, tmp_path: Path):  # noqa: F811
    """gitignore drops *trailing* whitespace only, so ` leading.py` and `leading.py` are two
    rules. Stripping both ends let an agent-authored rule inherit the seed's trust."""
    _seed_ignore(repo, " leading.py\n")
    wt, write_file = _tools(repo, tmp_path, "space")
    try:
        write_file(".gitignore", " leading.py\nleading.py\n")
        write_file("leading.py", "V = 1\n")
        assert "leading.py" in wt._ignored_by_this_run()
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
