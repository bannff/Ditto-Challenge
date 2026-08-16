"""FastMCP front door: the same ticket workflow the CLI runs, plus read-only context.

Thin glue over the existing plumbing — a tool that resolves a ticket, resources that
expose the taxonomy and recent run history, and a prompt that shapes a natural-language
request into a Ticket. All real work (safety, test-gate, budgets) lives in the workflow.
"""

from __future__ import annotations

import sqlite3
import uuid

from mcp.server.fastmcp import FastMCP

from .contracts import Ticket
from .ledger import Ledger
from .settings import get_settings
from .taxonomy import load_taxonomy
from .workflow import run_ticket as run_ticket_workflow

mcp = FastMCP("autodev")


def _new_ticket_id() -> str:
    return "tkt-" + uuid.uuid4().hex[:12]


def _recent_reports(limit: int = 10) -> list[dict]:
    try:
        reports = Ledger(get_settings().ledger_db).recent(limit)
    except sqlite3.Error:
        return []  # an unreadable/empty ledger is context we simply lack, not an error
    return [r.model_dump(mode="json") for r in reports]


def _ticket_prompt(description: str) -> str:
    return (
        "Turn the request below into a single Ticket JSON object with exactly these "
        "fields:\n"
        "- id: a short unique string\n"
        "- repository: path to the target repo\n"
        "- request: a clear, self-contained description of the work\n"
        "- domain: one tag (e.g. security, bugfix, feature, refactor, general)\n"
        "- acceptance_command: the shell command that must exit zero to accept the "
        "change, or null if none applies\n\n"
        "Return only the JSON object, no prose.\n\n"
        f"Request:\n{description}"
    )


@mcp.tool()
def run_ticket(
    repository: str,
    request: str,
    domain: str = "general",
    acceptance_command: str | None = None,
) -> dict:
    """Resolve a ticket against a target repo and return its structured run report."""
    ticket = Ticket(
        id=_new_ticket_id(),
        repository=repository,
        request=request,
        domain=domain,
        acceptance_command=acceptance_command,
    )
    return run_ticket_workflow(ticket).model_dump(mode="json")


@mcp.resource("taxonomy://current")
def taxonomy_resource() -> dict:
    """The fixed tag taxonomy: invariants and acceptance hints per domain tag."""
    return load_taxonomy().model_dump()


@mcp.resource("reports://recent")
def recent_reports_resource() -> list[dict]:
    """The last 10 run reports from the ledger; empty when there's no history."""
    return _recent_reports(10)


@mcp.prompt()
def file_a_ticket(description: str) -> str:
    """Shape a natural-language request into a Ticket-shaped JSON object."""
    return _ticket_prompt(description)


def main() -> None:
    mcp.run()
