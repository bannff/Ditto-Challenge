"""Legible end-to-end demo: watch the flow and see why it works.

Runs autodev against the bundled target app and prints, for each run:
  - a live node-lifecycle trace (each Swarm node gated by an eval, plus any redo/fork/fail)
  - a RunReport summary (outcome, the deterministic test-gate result, per-node verdicts,
    the diff it produced, and the lesson it stored).

Usage:
  uv run python scripts/demo.py                    # app1 suite: bug -> feature -> refuse
  uv run python scripts/demo.py --app app2         # app2 suite: IDOR -> feature -> refuse
  uv run python scripts/demo.py --app all          # both suites
  uv run python scripts/demo.py <ticket.json>      # a single ticket (its app is inferred)
  uv run python scripts/demo.py --out demos/latest # also write per-ticket artifact bundles
For the self-improvement before/after, see scripts/demo_selfimprove.py.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from self_improving_coding_agent.contracts import RunReport, Ticket
from self_improving_coding_agent.scrub import scrub_text
from self_improving_coding_agent.workflow import run_ticket

ROOT = Path(__file__).resolve().parents[1]
TARGET_APP = ROOT / "examples" / "target_app"
TARGET_APP_2 = ROOT / "examples" / "target_app_2"
TICKETS = ROOT / "examples" / "tickets"

# Two target apps: the inventory library (fast, cheap) and the orders service (harder —
# a cross-file IDOR). Tickets are written against one or the other, so each ticket
# declares its app here; --target overrides for anything not listed.
SUITES = {
    "app1": (TARGET_APP, ["bug-1-failing-test.json", "feature-1-acceptance-test.json",
                          "refuse-unsafe.json"]),
    "app2": (TARGET_APP_2, ["idor-1-broken-access.json", "feature-3-admin-list.json",
                            "refuse-disable-authz.json"]),
}
_TARGET_BY_TICKET = {name: app for app, names in SUITES.values() for name in names}

_MARK = {"running": "▶", "complete": "✓", "redo": "↻", "failed": "✗", "pending": "·"}


def materialize(dest: Path, target: Path = TARGET_APP) -> Path:
    # Ignore bytecode on the way in and on the way back out: without both, running the
    # target's tests turns every demo diff into .pyc binary churn.
    shutil.copytree(
        target, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    (dest / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n")
    for cmd in (["init", "-q"], ["config", "user.email", "demo@autodev"],
                ["config", "user.name", "autodev demo"], ["add", "-A"],
                ["commit", "-q", "-m", "seed target app"]):
        subprocess.run(["git", "-C", str(dest), *cmd], check=True)
    return dest


def _format_event(event: dict) -> str:
    score = event.get("eval_score")
    suffix = f"  eval={score:.2f}" if isinstance(score, float) else ""
    mark = _MARK.get(event["state"], "?")
    return f"    {mark} {event['node']:<18} {event['state']}{suffix}"


def _summary(report: RunReport) -> None:
    print(f"\n  outcome: {report.outcome.upper()}")
    if report.outcome == "refused":
        print(f"  refused because: {report.evidence}")
        return
    if report.acceptance:
        state = "passed" if report.acceptance.passed else "FAILED"
        print(f"  test-gate: {report.acceptance.command} -> exit "
              f"{report.acceptance.exit_code} ({state})")
    for v in report.verdicts:
        d = f"  diagnosis: {v.diagnosis}" if v.diagnosis else ""
        print(f"    - {v.node:<12} {'pass' if v.passed else 'fail'} ({v.attempts} attempt(s)){d}")
    if report.branch:
        print(f"  change committed to branch: {report.branch}")
    if report.lesson:
        print(f"  lesson stored: {report.lesson.content[:160].strip()}...")


def _write_artifacts(report: RunReport, dest: Path, trace: str) -> None:
    # A judge-inspectable bundle: human trace, machine-readable report, and the complete
    # diff (the ledger caps its own history row; what a reviewer reads is never clipped).
    # Every artifact crosses a persistence boundary, so each is scrubbed before write.
    artifacts = [
        ("trace.log", trace),
        ("report.json", report.model_dump_json(indent=2)),
        ("diff.patch", report.evidence),
    ]
    dest.mkdir(parents=True, exist_ok=True)
    for name, content in artifacts:
        (dest / name).write_text(scrub_text(content))


def run(ticket_path: Path, out_dir: Path | None = None, target: Path = TARGET_APP) -> None:
    ticket = Ticket.model_validate(json.loads(ticket_path.read_text()))
    print(f"\n{'=' * 70}\nTICKET {ticket.id} [{ticket.domain}]: {ticket.request[:80]}\n{'=' * 70}")
    repo = materialize(Path(tempfile.mkdtemp(prefix="autodev_")) / "repo", target)
    ticket = ticket.model_copy(update={"repository": str(repo)})

    lines: list[str] = []

    def trace(event: dict) -> None:
        line = _format_event(event)
        print(line)
        lines.append(line)

    report = run_ticket(ticket, status_cb=trace, telemetry_console=False)
    _summary(report)
    if out_dir is not None:
        _write_artifacts(report, out_dir / ticket.id, "\n".join(lines) + "\n")


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser(description="Legible end-to-end autodev demo.")
    p.add_argument("ticket", nargs="?", help="single ticket json; default runs a suite")
    p.add_argument("--out", type=Path, help="write per-ticket artifact bundles under this dir")
    p.add_argument("--app", choices=[*SUITES, "all"], default="app1",
                   help="which target app's suite to run (default: app1)")
    p.add_argument("--target", type=Path,
                   help="override the target app dir (inferred from the ticket otherwise)")
    args = p.parse_args(argv)

    if args.ticket:
        path = Path(args.ticket)
        target = args.target or _TARGET_BY_TICKET.get(path.name, TARGET_APP)
        run(path, args.out, target)
        return
    names = [*SUITES] if args.app == "all" else [args.app]
    for key in names:
        app, tickets = SUITES[key]
        for name in tickets:
            run(TICKETS / name, args.out, args.target or app)


if __name__ == "__main__":
    main(sys.argv[1:])
