"""Can a finished run's last checkpointed tree be recovered, and what is it really?

This is the code path that reads the git hashes the chain records. Without it those hashes
are written, verified, and printed, but never acted on — a claim nothing depends on, which is
a claim that quietly becomes false.

The division of labour matters:

- **git names which state.** `refs/autodev/checkpoints/<run_id>` is written by
  `Worktree.checkpoint()` and never by the ledger, so it is the authority for which commit
  gets recovered. Choosing the commit from a ledger payload instead would let anyone who can
  write `.data/` point recovery at any commit in the repo.
- **the chain says whether you may have it.** It supplies the run's seed — which after the
  run exists nowhere else — for the ancestry check, and it corroborates that the commit git
  names is one this run actually recorded. Two independent stores having to agree is the
  point; disagreement refuses.

Deliberately read-only: recovery appends nothing to the chain. A closed run's recorded head
should keep attesting to what the run did, not to who looked at it afterwards.

Reporting only. It resolves and explains; it does not copy the tree out. That tree is
agent-authored content derived from a stranger's ticket, so putting it on disk for someone
means the next `pytest` in that directory runs code the agent wrote. The operator gets the
exact git command and makes that call themselves.
"""

from __future__ import annotations

from pathlib import Path

from .contracts import RUN_ID_RE, Block, BlockType, RecoveryDecision
from .ledger import Ledger
from .worktree import is_descendant, resolve_checkpoint


def _seed_of(blocks: list[Block]) -> str | None:
    """The commit the run started from: the first hash the chain ever carried.

    `run_ticket` calls `track_git(worktree.seed)` immediately after creating the worktree and
    before any node runs, so the earliest non-null `git_hash` in the chain is the seed. Later
    blocks carry checkpoints instead.
    """
    for block in blocks:
        if block.git_hash:
            return block.git_hash
    return None


def _last_checkpoint_block(blocks: list[Block]) -> Block | None:
    """The last node that passed its eval checkpoint and moved the recorded hash."""
    seed = _seed_of(blocks)
    found = None
    for block in blocks:
        if block.block_type is BlockType.VERDICT and block.git_hash and block.git_hash != seed:
            found = block
    return found


def plan_recovery(ledger: Ledger, repo: Path, run_id: str) -> RecoveryDecision:
    """Decide whether this run's checkpoint may be recovered. Pure: touches no filesystem."""
    if not RUN_ID_RE.match(run_id):
        return RecoveryDecision(
            run_id=run_id, allowed=False, reason=f"not a valid run id: {run_id!r}"
        )

    blocks = ledger.blocks(run_id)
    if not blocks:
        return RecoveryDecision(
            run_id=run_id, allowed=False, reason="no chain recorded for this run"
        )
    # verify_chain, deliberately not provenance: provenance also refuses a breaker-tripped
    # run, and a run cut short is exactly the one worth recovering from. Different question.
    chain = ledger.verify_chain(run_id)

    commit = resolve_checkpoint(repo, run_id)
    if commit is None:
        checkpointed = _last_checkpoint_block(blocks) is not None
        reason = (
            "the checkpoint ref is gone — pruned, garbage-collected, or dropped by a revert "
            "because the run's change was rejected"
            if checkpointed
            else "this run never checkpointed: no node passed its eval checkpoint with a change"
        )
        return RecoveryDecision(
            run_id=run_id,
            allowed=False,
            reason=reason,
            chain=chain,
            outcome=_outcome(ledger, run_id),
        )

    seed = _seed_of(blocks)
    if seed is None:
        return RecoveryDecision(
            run_id=run_id,
            allowed=False,
            reason="the chain records no starting commit, so the recovered state can't be bounded",
            chain=chain,
            commit=commit,
        )
    if not is_descendant(repo, seed, commit):
        return RecoveryDecision(
            run_id=run_id,
            allowed=False,
            reason=f"the ref points at {commit[:12]}, which is not part of this run's history",
            chain=chain,
            commit=commit,
        )

    recorded = _last_checkpoint_block(blocks)
    corroborated = recorded is not None and (
        recorded.git_hash == commit or is_descendant(repo, str(recorded.git_hash), commit)
    )
    # The one case where the two stores contradict each other and neither can be trusted to
    # settle it: the record doesn't verify AND it doesn't agree with the ref.
    if not chain.valid and not corroborated:
        return RecoveryDecision(
            run_id=run_id,
            allowed=False,
            reason=(
                f"the record is broken ({chain.reason}) and does not agree with the ref, "
                "so there is nothing that can vouch for this commit"
            ),
            chain=chain,
            commit=commit,
            corroborated=False,
        )

    return RecoveryDecision(
        run_id=run_id,
        allowed=True,
        reason=(
            "git's ref and the ledger agree on this commit"
            if corroborated
            else "recoverable, but the ledger's record of the last checkpoint does not match "
            "the ref — treat the tree with suspicion"
        ),
        commit=commit,
        node=str(recorded.payload.get("node")) if recorded else None,
        outcome=_outcome(ledger, run_id),
        chain=chain,
        corroborated=corroborated,
    )


def _outcome(ledger: Ledger, run_id: str) -> str | None:
    report = ledger.get(run_id)
    return str(report.outcome) if report else None
