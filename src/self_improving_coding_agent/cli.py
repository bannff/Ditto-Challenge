"""CLI front door: `autodev run --ticket <json> --repo <path>`.

Thin glue over run_ticket — parse args, load the ticket, print the report, map the
outcome to an exit code. All real work (safety, test-gate, budgets) lives in the workflow.
"""

from __future__ import annotations

import argparse
import json

from .contracts import Outcome, Ticket
from .ledger import Ledger
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
    report = run_ticket(ticket)
    print(f"run {report.run_id}: {report.outcome}")
    print(report.model_dump_json(indent=2))
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autodev")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="resolve a ticket against a target repo")
    run.add_argument("--ticket", required=True, help="path to a ticket JSON file")
    run.add_argument("--repo", required=True, help="path to the target repository")
    run.set_defaults(func=_cmd_run)

    replay = sub.add_parser(
        "replay", help="verify and walk a recorded run's hash chain (offline, no model calls)"
    )
    replay.add_argument("run_id", help="the run to replay, e.g. run-0799fa72cfb5")
    replay.set_defaults(func=_cmd_replay)

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
