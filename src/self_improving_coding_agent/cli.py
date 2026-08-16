"""CLI front door: `autodev run --ticket <json> --repo <path>`.

Thin glue over run_ticket — parse args, load the ticket, print the report, map the
outcome to an exit code. All real work (safety, test-gate, budgets) lives in the workflow.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .cassette import Cassette, CassetteError
from .contracts import Outcome, Ticket
from .ledger import Ledger
from .recover import plan_recovery
from .settings import get_settings
from .workflow import run_ticket

_OK_OUTCOMES = {Outcome.SUCCESS, Outcome.REFUSED}


def _load_ticket(path: str, repo: str) -> Ticket:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("ticket JSON must be an object")
    data["repository"] = repo  # --repo makes tickets portable across checkouts
    return Ticket.model_validate(data)


def _cmd_run(args: argparse.Namespace) -> int:
    ticket = _load_ticket(args.ticket, args.repo)
    settings = get_settings()
    cassette = None
    if args.record:
        # Refused unless the target is under the configured fixture root: a cassette holds
        # unredacted prompts, so recording a real repository is not offered.
        cassette = Cassette.for_run(args.record, settings.cassettes_dir).open_for_record(
            Path(ticket.repository), settings.cassette_fixture_root
        )
        print(f"recording model I/O to {cassette.path} (unredacted, owner-only)")
    report = run_ticket(ticket, cassette=cassette)
    print(f"run {report.run_id}: {report.outcome}")
    print(report.model_dump_json(indent=2))
    return 0 if report.outcome in _OK_OUTCOMES else 1


def _cmd_reexecute(args: argparse.Namespace) -> int:
    """Re-run a recorded ticket's decisions from its cassette, with no model calls.

    The acceptance gate still runs for real — a replayed gate result would be a test-gate
    bypass — and the run is barred from committing or teaching memory, because recorded
    model output is not evidence.
    """
    settings = get_settings()
    ticket = _load_ticket(args.ticket, args.repo)
    try:
        cassette = Cassette.for_run(args.cassette, settings.cassettes_dir).load(
            Path(ticket.repository)
        )
    except CassetteError as e:
        print(f"cannot replay: {e}")
        return 1

    print(f"re-executing from {cassette.path} ({len(cassette)} recorded calls, offline)")
    report = run_ticket(ticket, cassette=cassette)
    print(f"replay run {report.run_id}: {report.outcome}")
    print(f"  committed: {report.branch or 'no — a replayed run never ships'}")
    print(f"  taught memory: {'yes' if report.lesson else 'no — recorded output is not evidence'}")
    return 0 if report.outcome in _OK_OUTCOMES else 1


def render_replay(ledger: Ledger, run_id: str, out=None) -> int:
    """Walk a recorded run's chain offline: verify every link, then print the decisions.

    No model calls, no network, no repo access — it reads the ledger and recomputes hashes.
    That is the point: a run can be audited from a disconnected machine, and a record that
    was edited after the fact cannot pass. Returns the exit code so callers (the CLI, the
    demo, the artifact writer) all render and judge a chain the same way.
    """
    emit = print if out is None else lambda line="": print(line, file=out)
    blocks = ledger.blocks(run_id)
    if not blocks:
        emit(f"no chain recorded for run {run_id}")
        return 1

    status = ledger.verify_chain(run_id)
    emit(f"replay {run_id} — {len(blocks)} blocks")
    emit()
    for block in blocks:
        broken = status.broken_at == block.seq
        mark = "!!" if broken else "  "
        git = f" git:{block.git_hash[:8]}" if block.git_hash else ""
        emit(f"{mark} [{block.seq:>3}] {block.block_type:<16}{git}  {block.content_hash[:12]}")
        for key, value in block.payload.items():
            if value is not None and value != "":
                emit(f"        {key}: {value}")
        if broken:
            emit(f"        ^^ CHAIN BROKEN HERE: {status.reason}")

    provenance = ledger.provenance(run_id)
    emit()
    emit(f"chain:      {'VERIFIED' if status.valid else 'BROKEN — ' + str(status.reason)}")
    emit(f"provenance: {'may teach memory' if provenance.allowed else 'refused'}")
    emit(f"            {provenance.reason}")
    return 0 if status.valid else 1


def _cmd_replay(args: argparse.Namespace) -> int:
    return render_replay(Ledger(get_settings().ledger_db), args.run_id)


def _cmd_recover(args: argparse.Namespace) -> int:
    """Report whether a finished run's last checkpointed tree can be recovered.

    Reports rather than materializes: the tree is agent-authored content derived from an
    untrusted ticket, so putting it on disk for someone means the next `pytest` in that
    directory runs code the agent wrote. The operator gets the exact command instead.
    """
    settings = get_settings()
    decision = plan_recovery(Ledger(settings.ledger_db), Path(args.repo).resolve(), args.run_id)

    print(f"recover {decision.run_id}")
    if decision.chain is not None:
        state = (
            f"VERIFIED across {decision.chain.length} blocks"
            if decision.chain.valid
            else f"BROKEN — {decision.chain.reason}"
        )
        print(f"  chain:    {state}")
    if decision.outcome:
        print(f"  run ended: {decision.outcome}")

    if not decision.allowed:
        print(f"\nnot recoverable: {decision.reason}")
        return 1

    print(f"  commit:   {decision.commit}")
    print(f"  from node: {decision.node or 'unknown'} — passed its EVAL checkpoint")
    print(f"  ledger agrees with git's ref: {'yes' if decision.corroborated else 'NO'}")
    print(f"\n{decision.reason}")
    # The claim a reader must not mistake. These commits are gated on LLM judges plus swarm
    # status, never on the target's own tests.
    print(
        "\nThis tree is UNVERIFIED: the acceptance gate never ran on it, so it is not "
        "shippable work.\nIt contains code the agent wrote from an untrusted ticket — running "
        "its tests executes that code."
    )
    print("\nTo inspect it yourself:")
    print(f"  git -C {args.repo} diff {decision.commit}")
    print(f"  git -C {args.repo} worktree add --detach /tmp/UNVERIFIED-{decision.run_id} "
          f"{decision.commit}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autodev")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="resolve a ticket against a target repo")
    run.add_argument("--ticket", required=True, help="path to a ticket JSON file")
    run.add_argument("--repo", required=True, help="path to the target repository")
    run.add_argument(
        "--record",
        metavar="CASSETTE_ID",
        help="record model I/O for later re-execution. Fixture repos only: a cassette is "
        "unredacted, so this is refused unless the target is under cassette_fixture_root.",
    )
    run.set_defaults(func=_cmd_run)

    reexec = sub.add_parser(
        "reexecute",
        help="re-run a recorded ticket's decisions from its cassette, offline (no model calls)",
    )
    reexec.add_argument("ticket", help="the same ticket json the cassette was recorded from")
    reexec.add_argument("--repo", required=True, help="path to the target repository")
    reexec.add_argument("--cassette", required=True, help="cassette id passed to --record")
    reexec.set_defaults(func=_cmd_reexecute)

    replay = sub.add_parser(
        "replay", help="verify and walk a recorded run's hash chain (offline, no model calls)"
    )
    replay.add_argument("run_id", help="the run to replay, e.g. run-0799fa72cfb5")
    replay.set_defaults(func=_cmd_replay)

    recover = sub.add_parser(
        "recover",
        help="report a finished run's last recoverable checkpoint (offline; does not write)",
    )
    recover.add_argument("run_id", help="the run to recover, e.g. run-0799fa72cfb5")
    recover.add_argument("--repo", required=True, help="path to the target repository")
    recover.set_defaults(func=_cmd_recover)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        parser.error(f"could not load ticket: {e}")  # exits 2, no traceback


if __name__ == "__main__":
    raise SystemExit(main())
