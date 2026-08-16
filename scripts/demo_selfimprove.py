"""Before/after self-improvement demo — a lesson that isn't in the code.

The same ticket is resolved twice against the same repo. The repo reveals only the bug the
ticket describes. Each scenario also has a business rule that lives ONLY in memory, which a
prior run learned the hard way.

- CONTROL (empty memory): fixes what the ticket says, passes its gate — but can't know the
  business rule, so it misses it.
- PRIMED (prior lesson in memory): recalls the rule and satisfies it too.

Both pass the acceptance gate; a hidden post-run check (not in the repo the agent sees)
measures the difference. That's memory supplying knowledge the repo doesn't contain.

Usage:
  uv run python scripts/demo_selfimprove.py            # app2 (orders/IDOR) — the default
  uv run python scripts/demo_selfimprove.py --app app1 # app1 (inventory/off-by-one)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from demo import TARGET_APP, TARGET_APP_2, materialize  # same scripts/ dir

from self_improving_coding_agent.contracts import Lesson, Outcome, RunReport, Ticket
from self_improving_coding_agent.memory import LessonMemory
from self_improving_coding_agent.workflow import run_ticket

ROOT = Path(__file__).resolve().parents[1]
TICKETS = ROOT / "examples" / "tickets"


def _committed(repo: Path, branch: str, path: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "show", f"{branch}:{path}"],
        capture_output=True, text=True, check=False,
    ).stdout


@dataclass(frozen=True)
class Scenario:
    """One before/after case: which ticket, which app, the memory-only rule, and the
    hidden check that measures whether the rule was honored."""

    ticket: Path
    target: Path
    recall_query: str
    rule: str
    check_label: str
    check: Callable[[Path, str], bool]


def _inventory_excludes_discontinued(repo: Path, branch: str) -> bool:
    code = _committed(repo, branch, "inventory.py")
    ns: dict = {}
    exec(compile(code, "inventory.py", "exec"), ns)  # noqa: S102 — our own fixture
    inv = ns["Inventory"]()
    inv.add_item("A", "a", 1, 1.0)
    inv.add_item("D", "d", 1, 1.0, discontinued=True)
    return "D" not in [i.sku for i in inv.needs_reorder(5)]


def _summary_path_also_locked(repo: Path, branch: str) -> bool:
    """Did the fix sweep EVERY read path, or only the one the ticket named?

    The acceptance test covers GET /orders/<id>. The sibling summary path reads the same
    resource and is just as exposed — the memory-only rule says to close both.
    """
    service = _committed(repo, branch, "service.py")
    body = service.split("def order_summary", 1)
    if len(body) < 2:
        return False
    # the next def ends the method
    method = body[1].split("\n    def ", 1)[0]
    return "require_owner" in method


SCENARIOS = {
    "app1": Scenario(
        ticket=TICKETS / "bug-3-pitfall.json",
        target=TARGET_APP,
        recall_query="fix needs_reorder",
        rule=(
            "Business rule for this org (deliberately NOT written in the code): a reorder "
            "list must EXCLUDE discontinued items (Item.discontinued is True). A prior run "
            "shipped discontinued lines in a reorder and triggered a bad purchase. Whenever "
            "you touch Inventory.needs_reorder, filter out discontinued items as well."
        ),
        check_label="excludes discontinued?",
        check=_inventory_excludes_discontinued,
    ),
    "app2": Scenario(
        ticket=TICKETS / "idor-1-broken-access.json",
        target=TARGET_APP_2,
        recall_query="fix broken object-level authorization on an order read path",
        rule=(
            "Hard-won rule from a prior run (deliberately NOT written in the code): when you "
            "close a broken object-level authorization hole, sweep EVERY read path for that "
            "resource, not just the one the ticket names. A prior run fixed get_order but "
            "left OrderService.order_summary (GET /orders/<id>/summary) reading the same "
            "order with no ownership check, so the leak survived the fix and the incident "
            "reopened. Apply require_owner to every operation that loads an order by id."
        ),
        check_label="summary read path also locked?",
        check=_summary_path_also_locked,
    ),
}


def _run(label: str, scenario: Scenario, memory: LessonMemory) -> tuple[RunReport, Path]:
    ticket = Ticket.model_validate(json.loads(scenario.ticket.read_text()))
    repo = materialize(Path(tempfile.mkdtemp(prefix=f"autodev_{label}_")) / "repo",
                       scenario.target)
    ticket = ticket.model_copy(update={"repository": str(repo)})
    report = run_ticket(ticket, memory=memory, telemetry_console=False)
    accept = report.acceptance.exit_code if report.acceptance else "n/a"
    print(f"[{label}] outcome={report.outcome} acceptance_exit={accept}")
    return report, repo


def _verify(scenario: Scenario, report: RunReport, repo: Path) -> bool | None:
    if report.branch is None:
        return None  # nothing shipped, so there's nothing to measure
    return scenario.check(repo, report.branch)


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser(description="Before/after self-improvement demo.")
    p.add_argument("--app", choices=[*SCENARIOS], default="app2")
    scenario = SCENARIOS[p.parse_args(argv).app]

    control_mem = LessonMemory(storage_dir=Path(tempfile.mkdtemp(prefix="mem_control_")))
    primed_mem = LessonMemory(storage_dir=Path(tempfile.mkdtemp(prefix="mem_primed_")))
    primed_mem.store(
        Lesson(ticket_id="prior-failed-run", outcome=Outcome.FAILURE, content=scenario.rule,
               tags=["bugfix"])
    )

    print("=== memory-only knowledge the primed run recalls (not in the repo) ===")
    for lesson in primed_mem.retrieve(scenario.recall_query):
        print(" -", lesson)
    print()

    control_report, control_repo = _run("control", scenario, control_mem)
    primed_report, primed_repo = _run("primed", scenario, primed_mem)

    print(f"\n=== hidden business-rule check ({scenario.check_label}) ===")
    print(f"[control] {_verify(scenario, control_report, control_repo)}")
    print(f"[primed]  {_verify(scenario, primed_report, primed_repo)}")
    print(
        "\nBoth passed the acceptance gate; the primed run also satisfied the memory-only "
        "business rule the control run had no way to know."
    )


if __name__ == "__main__":
    main(sys.argv[1:])
