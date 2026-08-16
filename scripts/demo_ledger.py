"""The hash-chained ledger, demonstrated offline — no AWS credentials, no network.

This runs the real `Ledger`, `RunRecorder`, and `render_replay` code paths against a
throwaway database, with only the agent work itself stood in for. So the hashes, the chain
verification, the tamper detection and the provenance decisions you see are the shipped
implementation, not a narration of it.

The acts:
  1. A resolved run leaves a verifiable chain, and replay walks it.
  2. Editing the record is detected, at the exact block.
  3. Deleting the end of the record is detected too (links alone can't see this).
  4. The provenance gate: which runs are allowed to teach memory, and which are refused.
  5. A run whose record was altered teaches nothing either.
  6. Rollback: a failed attempt is put back to the last good tree before the retry.
  7. Recovery: what is still recoverable after a run ends, and what deliberately isn't.

Acts 6 and 7 need a real git repository, so they build a throwaway one. Nothing here touches
your repo, your `.data/`, or your configured worktrees directory: the repo, its worktree base,
and the ledger all live inside a single TemporaryDirectory that is deleted on the way out, and
the last act *checks* that containment rather than asking you to take it on trust. Re-running
any other demo afterwards is unaffected.

Usage:
  uv run python scripts/demo_ledger.py                      # every act, offline
  uv run python scripts/demo_ledger.py --out demos/ledger   # also save the transcript
  uv run python scripts/demo_ledger.py --from-run <run_id>  # replay a real recorded run
"""

from __future__ import annotations

import argparse
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from self_improving_coding_agent.cli import render_replay
from self_improving_coding_agent.contracts import BlockType, NodeState
from self_improving_coding_agent.ledger import Ledger
from self_improving_coding_agent.recorder import RunRecorder, _record_args
from self_improving_coding_agent.recover import plan_recovery
from self_improving_coding_agent.settings import get_settings
from self_improving_coding_agent.worktree import BRANCH_PREFIX, Worktree

GIT_HASH = "4f2c8a1b9e7d3c5a6b8f0d2e4a6c8e0b2d4f6a8c"


def _rule(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def _step(text: str) -> None:
    print(f"\n-- {text}")


def _resolved_run(ledger: Ledger, run_id: str) -> RunRecorder:
    """Exactly the block sequence workflow.py emits for a ticket that gets resolved.

    Written through RunRecorder, using the same status events the graph streams, so this is
    the real capture path — not blocks hand-placed to look tidy.
    """
    recorder = RunRecorder(ledger, run_id)
    recorder.track_git(GIT_HASH)
    recorder.append(BlockType.RUN_START, {"ticket_id": "bug-1-failing-test", "domain": "inventory"})
    for node in ("discover", "implement", "verify", "learn"):
        recorder.record_status({"node": node, "state": str(NodeState.RUNNING)})
        if node == "implement":
            # A write_file call, recorded through the same projection the live SDK hook
            # uses: the path verbatim, the file content only as size + digest.
            recorder.append(
                BlockType.TOOL_CALL,
                {
                    "node": node,
                    "tool": "write_file",
                    "args": _record_args(
                        "write_file",
                        {"path": "inventory.py", "content": "def reorder_level(qty):\n    ..."},
                    ),
                    "status": "success",
                },
            )
        recorder.record_status({"node": node, "state": str(NodeState.COMPLETE), "eval_score": 0.9})
    recorder.append(
        BlockType.ACCEPTANCE_GATE,
        {"command": "pytest tests", "exit_code": 0, "passed": True},
    )
    recorder.append(
        BlockType.LESSON_WRITE, {"ticket_id": "bug-1-failing-test", "outcome": "success"}
    )
    recorder.append(BlockType.RUN_END, {"outcome": "success", "learned": True})
    return recorder


def _tripped_run(ledger: Ledger, run_id: str) -> None:
    """A run whose circuit breaker cut it short: retries spent, node degraded."""
    recorder = RunRecorder(ledger, run_id)
    recorder.track_git(GIT_HASH)
    recorder.append(BlockType.RUN_START, {"ticket_id": "bug-2-no-test", "domain": "inventory"})
    recorder.record_status({"node": "discover", "state": str(NodeState.COMPLETE)})
    for state in (NodeState.RUNNING, NodeState.REDO, NodeState.REDO):
        recorder.record_status({"node": "implement", "state": str(state)})
    recorder.record_status({"node": "implement", "state": str(NodeState.FAILED), "eval_score": 0.3})


def _honest_failure(ledger: Ledger, run_id: str) -> None:
    """A run that finished cleanly but failed its test-gate. Nothing was cut short."""
    recorder = RunRecorder(ledger, run_id)
    recorder.append(BlockType.RUN_START, {"ticket_id": "bug-3-pitfall", "domain": "inventory"})
    for node in ("discover", "implement", "verify", "learn"):
        recorder.record_status({"node": node, "state": str(NodeState.COMPLETE), "eval_score": 0.8})
    recorder.append(
        BlockType.ACCEPTANCE_GATE,
        {"command": "pytest tests", "exit_code": 1, "passed": False},
    )


def _edit_stored_block(ledger: Ledger, run_id: str, seq: int, payload: dict) -> None:
    conn = sqlite3.connect(ledger.db_path)
    with conn:
        conn.execute(
            "UPDATE blocks SET payload_json = ? WHERE run_id = ? AND seq = ?",
            (json.dumps(payload), run_id, seq),
        )
    conn.close()


def _delete_from(ledger: Ledger, run_id: str, seq: int) -> None:
    conn = sqlite3.connect(ledger.db_path)
    with conn:
        conn.execute("DELETE FROM blocks WHERE run_id = ? AND seq >= ?", (run_id, seq))
    conn.close()


def act_one(ledger: Ledger) -> None:
    _rule("ACT 1 — a resolved run leaves a chain you can verify offline")
    run_id = "run-demo-resolved"
    recorder = _resolved_run(ledger, run_id)
    print(f"\nrecorded {len(ledger.blocks(run_id))} blocks; dropped writes: {recorder.drops}")
    _step("autodev replay run-demo-resolved")
    exit_code = render_replay(ledger, run_id)
    tool_block = next(b for b in ledger.blocks(run_id) if b.block_type == BlockType.TOOL_CALL)
    print(f"\n(replay exit code: {exit_code} — a verified chain exits 0)")
    print(
        f"note block {tool_block.seq}: the file's CONTENT is recorded as a size + digest, never"
        "\nthe text itself, so repo secrets can't ride into a durable row that replay prints."
    )


def act_two(ledger: Ledger) -> None:
    _rule("ACT 2 — editing the record is detected, at the exact block")
    run_id = "run-demo-resolved"
    gate = next(b for b in ledger.blocks(run_id) if b.block_type == BlockType.ACCEPTANCE_GATE)
    print(
        f"\nThe most valuable lie a run could tell: block {gate.seq} says the test-gate passed."
        "\nRewriting it directly in SQLite, behind the ledger's API, to say it failed..."
    )
    _edit_stored_block(
        ledger, run_id, gate.seq, {"command": "pytest tests", "exit_code": 1, "passed": False}
    )
    _step("autodev replay run-demo-resolved   (after tampering)")
    exit_code = render_replay(ledger, run_id)
    print(f"\n(replay exit code: {exit_code} — a broken chain exits nonzero)")


def act_three(ledger: Ledger) -> None:
    _rule("ACT 3 — deleting the end of the record is detected too")
    run_id = "run-demo-truncate"
    _resolved_run(ledger, run_id)
    length = len(ledger.blocks(run_id))
    print(
        f"\n{length} blocks recorded. Hash links alone cannot catch a tail deletion: a shorter"
        "\nchain still links perfectly end to end. The recorded head is what closes it —"
        "\nand the tail is exactly where a breaker trip and the outcome live."
    )
    _delete_from(ledger, run_id, 5)
    status = ledger.verify_chain(run_id)
    print(f"\nafter deleting blocks 5+:  valid={status.valid}")
    print(f"                           reason: {status.reason}")
    print(f"                           recorded head says: {ledger.head(run_id)}")


def act_four(ledger: Ledger) -> None:
    _rule("ACT 4 — the provenance gate: which runs may teach memory")
    cases = [
        ("run-demo-resolved-2", "resolved cleanly", _resolved_run),
        ("run-demo-honest-fail", "failed its test-gate, but finished honestly", _honest_failure),
        ("run-demo-tripped", "circuit breaker cut it short", _tripped_run),
    ]
    for run_id, _, build in cases:
        build(ledger, run_id)

    print("\nMemory asks the ledger before accepting any lesson:\n")
    for run_id, label, _ in cases:
        decision = ledger.provenance(run_id)
        verdict = "LEARNS" if decision.allowed else "REFUSED"
        print(f"  {verdict:<8} {label}")
        print(f"           {decision.reason}\n")
    print(
        "A failure that ran to completion still teaches — those are the useful lessons.\n"
        "A run cut short never reached its conclusions, so there is nothing verified to\n"
        "learn from it, and its guesswork stays out of the store where it would steer\n"
        "every later run."
    )


def act_five_tampered_chain_refuses(ledger: Ledger) -> None:
    _rule("ACT 5 — and a run whose record was tampered with teaches nothing either")
    decision = ledger.provenance("run-demo-resolved")  # tampered in Act 2
    print(f"\nrun-demo-resolved (edited in Act 2):  allowed={decision.allowed}")
    print(f"  {decision.reason}")
    print(
        "\nSo the same chain that makes the record auditable also decides whether the run\n"
        "is allowed to change future behavior. That is the ledger acting, not just recording."
    )


def _throwaway_repo(base: Path) -> Path:
    """A git repo that exists only for this demo, inside the temp dir."""
    repo = base / "target"
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ["init", "-q"],
        ["config", "user.email", "demo@autodev"],
        ["config", "user.name", "autodev demo"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "inventory.py").write_text(
        "def low_stock(items, threshold):\n"
        "    return [i for i in items if i.quantity < threshold]\n"
    )
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "seed"], check=True)
    return repo


def _show(wt, label: str) -> None:
    body = (wt.root / "inventory.py").read_text().strip().splitlines()[-1].strip()
    debris = sorted(p.name for p in wt.root.glob("*.py") if p.name != "inventory.py")
    print(f"  {label:<22} {body}")
    if debris:
        print(f"  {'':<22} leftover files: {debris}")


def act_six_rollback(base: Path) -> tuple[Path, str, str]:
    _rule("ACT 6 — a failed attempt is rolled back before the retry, not built on")
    repo = _throwaway_repo(base)
    wt = Worktree.create(repo, "run-demo-rollback", base / "worktrees")
    print(f"\nthrowaway repo: {repo}\nseed commit:    {wt.seed[:12]}")

    _step("attempt 1 — a good change that passes its evaluator checkpoint")
    (wt.root / "inventory.py").write_text(
        "def low_stock(items, threshold):\n"
        "    return [i for i in items if i.quantity <= threshold]\n"
    )
    checkpoint = wt.checkpoint("implement")
    _show(wt, "tree now:")
    print(f"  checkpointed as        {checkpoint[:12] if checkpoint else None}")

    _step("attempt 2 — a change that fails its checkpoint, leaving debris behind")
    (wt.root / "inventory.py").write_text(
        "def low_stock(items, threshold):\n    return BROKEN_SENTINEL\n"
    )
    (wt.root / "scratch_notes.py").write_text("# half-applied scaffolding\n")
    _show(wt, "tree now:")

    _step("the retry boundary restores the last good tree (this is restore_cb firing)")
    wt.restore(checkpoint or wt.seed)
    _show(wt, "tree now:")
    print(
        "\n  Without this, attempt 3 would start on top of BROKEN_SENTINEL plus the leftover\n"
        "  file, and the informed retry would diagnose a tree nobody intended."
    )

    _step("and a final revert goes all the way back to the seed, not to the checkpoint")
    wt.revert()
    _show(wt, "tree now:")
    print(
        f"  clean: {wt.is_clean()} | diff empty: {wt.diff() == ''}\n"
        "\n  Revert anchors to the seed on purpose. Once a checkpoint exists, HEAD *is* that\n"
        "  checkpoint, so `reset --hard HEAD` would keep the very change it must discard —\n"
        "  'a change that breaks tests is reverted' would quietly become 'reverted to the\n"
        "  last unverified checkpoint'."
    )
    wt.remove(keep_branch=False)
    return repo, "run-demo-rollback", wt.seed


def act_seven_recovery(base: Path, ledger: Ledger) -> Path:
    _rule("ACT 7 — what is still recoverable after a run ends, and what deliberately isn't")
    repo = _throwaway_repo(base / "recovery")

    # A run that checkpointed and did NOT ship: recoverable.
    kept = "run-demo-kept"
    recorder = RunRecorder(ledger, kept)
    recorder.append(BlockType.RUN_START, {"ticket_id": "bug-1", "domain": "inventory"})
    wt = Worktree.create(repo, kept, base / "worktrees")
    recorder.track_git(wt.seed)
    recorder.record_status({"node": "discover", "state": str(NodeState.COMPLETE)})
    (wt.root / "inventory.py").write_text("def low_stock(i, t):\n    return i <= t\n")
    recorder.track_git(wt.checkpoint("implement"))
    recorder.record_status({"node": "implement", "state": str(NodeState.COMPLETE)})
    recorder.record_status({"node": "verify", "state": str(NodeState.FAILED)})
    recorder.append(BlockType.RUN_END, {"outcome": "failure"})
    wt.remove(keep_branch=False)

    # A run whose change was rejected and reverted: deliberately NOT recoverable.
    dropped = "run-demo-reverted"
    recorder2 = RunRecorder(ledger, dropped)
    recorder2.append(BlockType.RUN_START, {"ticket_id": "bug-2", "domain": "inventory"})
    wt2 = Worktree.create(repo, dropped, base / "worktrees")
    recorder2.track_git(wt2.seed)
    # A block carrying the seed hash, before any checkpoint moves it — the workflow always
    # emits discover's attempt first, and that is what marks the run's starting commit.
    recorder2.record_status({"node": "discover", "state": str(NodeState.COMPLETE)})
    (wt2.root / "inventory.py").write_text("def low_stock(i, t):\n    return REJECTED\n")
    recorder2.track_git(wt2.checkpoint("implement"))
    recorder2.record_status({"node": "implement", "state": str(NodeState.COMPLETE)})
    wt2.revert()  # the gate said no
    recorder2.append(BlockType.RUN_END, {"outcome": "failure"})
    wt2.remove(keep_branch=False)

    for run_id, blurb in (
        (kept, "checkpointed, then the run failed at verify without shipping"),
        (dropped, "checkpointed, then the change was rejected and reverted"),
    ):
        _step(f"autodev recover {run_id}   ({blurb})")
        decision = plan_recovery(ledger, repo, run_id)
        verdict = "RECOVERABLE" if decision.allowed else "NOT RECOVERABLE"
        print(f"  {verdict}")
        print(f"  {decision.reason}")
        if decision.allowed and decision.commit:
            print(f"  commit {decision.commit[:12]} from node '{decision.node}'")
            print(f"  ledger corroborates git's ref: {decision.corroborated}")

    print(
        "\n  git's ref names WHICH commit — the ref store is written by the workflow, never by\n"
        "  the ledger, so a row in an unsigned database cannot choose what gets recovered.\n"
        "  The chain says WHETHER you may have it: it supplies the run's seed, which after the\n"
        "  run exists nowhere else, and confirms the commit is one it recorded.\n"
        "\n  The reverted run has nothing to recover by design. Work the gate rejected must not\n"
        "  be one command away from coming back."
    )
    return repo


def _containment_check(temp_base: Path) -> None:
    _rule("CONTAINMENT — this demo checked its own blast radius")
    here = Path(__file__).resolve().parents[1]
    settings = get_settings()

    refs = subprocess.run(
        ["git", "-C", str(here), "for-each-ref", "--format=%(refname)", "refs/autodev/*"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    worktrees = subprocess.run(
        ["git", "-C", str(here), "worktree", "list"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.splitlines()

    # What leaked, not how many worktrees exist. A contributor checking out a second branch
    # under .worktrees/ is ordinary, and counting rows made this assert fail on their setup
    # while saying "the demo left a worktree" — blaming the demo for someone else's checkout.
    # The demo's own worktrees are the ones on an autodev branch or under its temp base.
    leaked = [
        line
        for line in worktrees
        if f"[{BRANCH_PREFIX}" in line or str(temp_base) in line
    ]

    print(f"\n  this repo's refs/autodev/*        {refs or 'none — nothing was written here'}")
    print(f"  this repo's leaked worktrees     {leaked or 'none — all were removed'}")
    print(f"  configured worktrees dir used    no (demo used {temp_base / 'worktrees'})")
    print(f"  real ledger written              no (demo used its own db under {temp_base})")
    print(f"  real ledger path untouched       {settings.ledger_db}")
    assert not refs, "demo leaked a checkpoint ref into this repo"
    assert not leaked, f"demo left a worktree registered in this repo: {leaked}"
    print(
        "\n  Everything above lived in one temp directory that is now gone, so re-running any\n"
        "  other demo is unaffected."
    )


def _honest_limits() -> None:
    _rule("HONEST LIMITS")
    print(
        "\nThis is tamper-EVIDENT, not tamper-proof:\n"
        "  - Blocks are unsigned. There is no key material here, and the writer is trusted,\n"
        "    so evidence forged at the SOURCE is chain-valid. Signing blocks with a per-node\n"
        "    key is the mesh-native next step (DESIGN.md Uplevel), deliberately not built.\n"
        "  - Rollback references git rather than reimplementing it: a block carries the\n"
        "    worktree's commit hash, so the ledger proves WHICH state was verified and git\n"
        "    restores it. Git is already a content-addressed Merkle DAG and works offline.\n"
        "  - Replay audits a recorded run. It does not re-execute decisions; that needs every\n"
        "    model call captured, which is also Uplevel."
    )


def _all_acts(ledger: Ledger, temp_base: Path) -> None:
    print(
        "Offline hash-ledger demo — no AWS credentials, no network, no model calls.\n"
        "Real Ledger / RunRecorder / Worktree / replay / recover code, against a throwaway\n"
        f"database and a throwaway git repo under {temp_base}."
    )
    for act in (act_one, act_two, act_three, act_four, act_five_tampered_chain_refuses):
        act(ledger)
    act_six_rollback(temp_base)
    act_seven_recovery(temp_base, ledger)
    _containment_check(temp_base)
    _honest_limits()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Offline hash-ledger demo (no credentials).")
    parser.add_argument("--out", type=Path, help="also save the transcript under this dir")
    parser.add_argument("--from-run", help="replay a real recorded run from the live ledger")
    args = parser.parse_args(argv)

    if args.from_run:
        return render_replay(Ledger(get_settings().ledger_db), args.from_run)

    with tempfile.TemporaryDirectory(prefix="autodev_ledger_demo_") as tmp:
        base = Path(tmp)
        ledger = Ledger(base / "demo-ledger.db")
        if args.out is None:
            _all_acts(ledger, base)
            return 0
        # Show the transcript and keep a copy, so a judge can read the saved one instead.
        args.out.mkdir(parents=True, exist_ok=True)
        transcript = args.out / "ledger-demo.log"
        buffer = io.StringIO()
        original = sys.stdout
        try:
            sys.stdout = _Tee(original, buffer)
            _all_acts(ledger, base)
        finally:
            sys.stdout = original
        transcript.write_text(buffer.getvalue())
        print(f"\ntranscript saved to {transcript}")
    return 0


class _Tee:
    """Write to the terminal and a buffer at once, so --out captures what you just read."""

    def __init__(self, *streams) -> None:
        self._streams = streams

    def write(self, text: str) -> int:
        for stream in self._streams:
            stream.write(text)
        return len(text)

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
