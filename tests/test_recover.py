"""Recovery: the code path that finally reads the git hashes the chain records.

The division under test — git's ref decides *which* commit, the chain decides *whether* you
may have it — is what makes the recorded hashes load-bearing instead of decorative. So the
tests that matter are the disagreements: a re-pointed ref, a chain that doesn't verify, a
foreign commit, and a run whose rejected work was correctly dropped.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from self_improving_coding_agent.contracts import BlockType
from self_improving_coding_agent.ledger import Ledger
from self_improving_coding_agent.recover import plan_recovery
from self_improving_coding_agent.recorder import RunRecorder
from self_improving_coding_agent.worktree import Worktree, resolve_checkpoint

RUN = "run-rec01"


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for cmd in (["init", "-q"], ["config", "user.email", "t@t.t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(path), *cmd], check=True)
    (path / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)
    return path


def _run_that_checkpointed(tmp_path, run_id=RUN, revert=False):
    """A run as run_ticket records one: seed hash first, then a VERDICT carrying a checkpoint."""
    repo = _init_repo(tmp_path / "repo")
    ledger = Ledger(tmp_path / "ledger.db")
    recorder = RunRecorder(ledger, run_id)
    recorder.append(BlockType.RUN_START, {"ticket_id": "t1"})

    wt = Worktree.create(repo, run_id, tmp_path / "wt")
    recorder.track_git(wt.seed)
    recorder.record_status({"node": "discover", "state": "complete"})

    (wt.root / "app.py").write_text("x = 2  # the change\n")
    commit = wt.checkpoint("implement")
    recorder.track_git(commit)
    recorder.record_status({"node": "implement", "state": "complete"})

    if revert:
        wt.revert()
    return repo, ledger, wt, commit


# ---- the happy path ------------------------------------------------------------


def test_a_checkpointed_run_is_recoverable_and_both_stores_agree(tmp_path):
    repo, ledger, _, commit = _run_that_checkpointed(tmp_path)

    decision = plan_recovery(ledger, repo, RUN)

    assert decision.allowed
    assert decision.commit == commit
    assert decision.node == "implement"
    assert decision.corroborated is True
    assert decision.chain is not None and decision.chain.valid


def test_the_commit_comes_from_gits_ref_not_the_ledger(tmp_path):
    """An unsigned SQLite row must not choose what gets recovered. If it did, anyone who can
    write .data/ could point recovery at any commit in the repo."""
    repo, ledger, _, commit = _run_that_checkpointed(tmp_path)
    # Rewrite the ledger's idea of the checkpoint to the seed.
    conn = __import__("sqlite3").connect(ledger.db_path)
    with conn:
        conn.execute("UPDATE blocks SET git_hash = ? WHERE run_id = ?", ("0" * 40, RUN))
    conn.close()

    decision = plan_recovery(ledger, repo, RUN)

    assert decision.commit == commit  # still git's answer
    assert resolve_checkpoint(repo, RUN) == commit


def test_a_tampered_ledger_that_still_agrees_by_ancestry_is_flagged_not_trusted(tmp_path):
    repo, ledger, _, _ = _run_that_checkpointed(tmp_path)
    conn = __import__("sqlite3").connect(ledger.db_path)
    with conn:
        conn.execute("UPDATE blocks SET payload_json = ? WHERE run_id = ? AND seq = 0",
                     ('{"ticket_id": "rewritten"}', RUN))
    conn.close()

    decision = plan_recovery(ledger, repo, RUN)

    assert decision.chain is not None and not decision.chain.valid
    # The ref still corroborates, so recovery is offered — with the broken chain reported.
    assert decision.allowed
    assert decision.corroborated is True


# ---- refusals ------------------------------------------------------------------


def test_recovery_is_refused_when_the_stores_contradict_each_other(tmp_path):
    """Broken chain AND a ref that the record can't vouch for: nothing can settle which is
    lying, so recovery refuses rather than guessing."""
    repo, ledger, _, _ = _run_that_checkpointed(tmp_path)
    conn = __import__("sqlite3").connect(ledger.db_path)
    with conn:
        conn.execute("DELETE FROM blocks WHERE run_id = ? AND seq = 1", (RUN,))
        conn.execute("UPDATE blocks SET git_hash = NULL WHERE run_id = ?", (RUN,))
    conn.close()

    decision = plan_recovery(ledger, repo, RUN)

    assert not decision.allowed


def test_a_reverted_run_is_not_recoverable(tmp_path):
    """revert() drops the checkpoint ref, so work the gate rejected cannot be resurrected —
    and the message says which of the two reasons applies."""
    repo, ledger, _, _ = _run_that_checkpointed(tmp_path, revert=True)

    decision = plan_recovery(ledger, repo, RUN)

    assert not decision.allowed
    assert "rejected" in decision.reason or "gone" in decision.reason


def test_a_run_that_never_checkpointed_says_so_distinctly(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    ledger = Ledger(tmp_path / "ledger.db")
    recorder = RunRecorder(ledger, "run-nocp")
    recorder.append(BlockType.RUN_START, {"ticket_id": "t1"})
    wt = Worktree.create(repo, "run-nocp", tmp_path / "wt")
    recorder.track_git(wt.seed)
    recorder.record_status({"node": "discover", "state": "complete"})

    decision = plan_recovery(ledger, repo, "run-nocp")

    assert not decision.allowed
    assert "never checkpointed" in decision.reason


def test_an_unknown_run_is_refused(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    decision = plan_recovery(Ledger(tmp_path / "l.db"), repo, "run-nope")
    assert not decision.allowed
    assert "no chain" in decision.reason


@pytest.mark.parametrize("bad", ["../../etc/passwd", "a/b", "-f", "", "run id"])
def test_a_malformed_run_id_is_refused_before_it_reaches_git_or_a_path(tmp_path, bad):
    repo = _init_repo(tmp_path / "repo")
    decision = plan_recovery(Ledger(tmp_path / "l.db"), repo, bad)
    assert not decision.allowed
    assert "valid run id" in decision.reason


def test_a_ref_repointed_outside_the_run_is_refused(tmp_path):
    """Recovering an arbitrary commit is not recovery. Only this run's own history counts."""
    repo = _init_repo(tmp_path / "repo")
    first = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    (repo / "app.py").write_text("x = 50\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "second"], check=True)

    ledger = Ledger(tmp_path / "ledger.db")
    recorder = RunRecorder(ledger, RUN)
    recorder.append(BlockType.RUN_START, {"ticket_id": "t1"})
    wt = Worktree.create(repo, RUN, tmp_path / "wt")  # seed is the SECOND commit
    recorder.track_git(wt.seed)
    (wt.root / "app.py").write_text("x = 2\n")
    recorder.track_git(wt.checkpoint("implement"))
    recorder.record_status({"node": "implement", "state": "complete"})

    # `first` is an ancestor of the seed, not a descendant — not a state this run produced.
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", wt.checkpoint_ref, first], check=True
    )

    decision = plan_recovery(ledger, repo, RUN)
    assert decision.allowed is False
    assert "not part of this run's history" in decision.reason


# ---- read-only -----------------------------------------------------------------


def test_planning_recovery_writes_nothing_to_the_chain(tmp_path):
    """A closed run's recorded head should keep attesting to what the run did, not to who
    looked at it afterwards."""
    repo, ledger, _, _ = _run_that_checkpointed(tmp_path)
    before = ledger.head(RUN)

    plan_recovery(ledger, repo, RUN)
    plan_recovery(ledger, repo, RUN)

    assert ledger.head(RUN) == before
    assert ledger.verify_chain(RUN).valid


def test_planning_recovery_touches_no_filesystem(tmp_path):
    repo, ledger, _, _ = _run_that_checkpointed(tmp_path)
    before = sorted(p.name for p in repo.iterdir())

    plan_recovery(ledger, repo, RUN)

    assert sorted(p.name for p in repo.iterdir()) == before


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
