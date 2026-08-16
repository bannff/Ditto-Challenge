"""CLI front door: `autodev run --ticket <json> --repo <path>`.

Thin glue over run_ticket — parse args, load the ticket, print the report, map the
outcome to an exit code. All real work (safety, test-gate, budgets) lives in the workflow.
"""

from __future__ import annotations

import argparse
import json

from .contracts import Outcome, Ticket
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autodev")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="resolve a ticket against a target repo")
    run.add_argument("--ticket", required=True, help="path to a ticket JSON file")
    run.add_argument("--repo", required=True, help="path to the target repository")
    run.set_defaults(func=_cmd_run)

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
