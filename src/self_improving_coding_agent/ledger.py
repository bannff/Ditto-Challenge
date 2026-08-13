"""Durable SQLite run ledger. Not a vector store — just structured run history."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .contracts import SCHEMA_VERSION, RunReport
from .scrub import scrub_text


def _scrub_report(report: RunReport) -> RunReport:
    data = report.model_dump(mode="json")
    data["evidence"] = scrub_text(data.get("evidence", ""))
    if data.get("acceptance"):
        data["acceptance"]["output_tail"] = scrub_text(data["acceptance"].get("output_tail", ""))
    if data.get("lesson"):
        data["lesson"]["content"] = scrub_text(data["lesson"]["content"])
    return RunReport.model_validate(data)


def _migrate(data: dict, from_version: int) -> dict:
    # Forward-looking seam: when SCHEMA_VERSION advances past 1, translate older rows
    # here before validation. Not exercised today — only version 1 exists. Kept as an
    # explicit upgrade point so old ledgers stay readable.
    return data


class Ledger:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init(self) -> None:
        with self._connect() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    ticket_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    report_json TEXT NOT NULL
                )
                """
            )

    def save(self, report: RunReport) -> RunReport:
        scrubbed = _scrub_report(report)
        with self._connect() as c:
            c.execute(
                "INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?, ?)",
                (
                    scrubbed.run_id,
                    scrubbed.ticket.id,
                    str(scrubbed.outcome),
                    scrubbed.schema_version,
                    scrubbed.created_at.isoformat(),
                    scrubbed.model_dump_json(),
                ),
            )
        return scrubbed

    def get(self, run_id: str) -> RunReport | None:
        with self._connect() as c:
            row = c.execute(
                "SELECT report_json, schema_version FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return self._load(*row) if row else None

    def recent(self, limit: int = 10) -> list[RunReport]:
        with self._connect() as c:
            rows = c.execute(
                "SELECT report_json, schema_version FROM runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._load(j, v) for j, v in rows]

    def _load(self, report_json: str, stored_version: int) -> RunReport:
        data = json.loads(report_json)
        if stored_version < SCHEMA_VERSION:
            data = _migrate(data, stored_version)
        return RunReport.model_validate(data)
