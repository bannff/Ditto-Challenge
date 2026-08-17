"""The schema migration chain: versioned payloads walk forward, everything else fails loudly.

SCHEMA_VERSION is 1 and the production registry is empty (nothing to migrate *from* yet), so
these tests register synthetic transforms to prove the machinery: an old payload upgrades
through every step in order, and unknown or future versions refuse rather than half-load.
"""

from __future__ import annotations

import pytest

from self_improving_coding_agent import migrations
from self_improving_coding_agent.contracts import SCHEMA_VERSION, Outcome, RunReport, Ticket
from self_improving_coding_agent.ledger import Ledger
from self_improving_coding_agent.migrations import MIGRATIONS, MigrationError, upgrade


@pytest.fixture(autouse=True)
def _isolated_registry():
    saved = dict(MIGRATIONS)
    MIGRATIONS.clear()
    yield
    MIGRATIONS.clear()
    MIGRATIONS.update(saved)


def test_current_payload_passes_through_unchanged():
    payload = {"schema_version": SCHEMA_VERSION, "value": "x"}
    assert upgrade("thing", payload) == payload


def test_legacy_payload_walks_every_step_in_order(monkeypatch):
    monkeypatch.setattr(migrations, "SCHEMA_VERSION", 3)
    MIGRATIONS[("thing", 1)] = lambda p: {
        k: v for k, v in p.items() if k != "old_name"
    } | {"renamed": p["old_name"]}
    MIGRATIONS[("thing", 2)] = lambda p: {**p, "added": "default"}

    out = upgrade("thing", {"schema_version": 1, "old_name": "kept"}, target=3)

    assert out == {"schema_version": 3, "renamed": "kept", "added": "default"}


def test_future_payload_is_refused():
    with pytest.raises(MigrationError, match="newer than this code"):
        upgrade("thing", {"schema_version": SCHEMA_VERSION + 1})


def test_gap_in_the_chain_is_refused():
    with pytest.raises(MigrationError, match="no migration registered from v0"):
        upgrade("thing", {"schema_version": 0})


def test_missing_version_stamp_is_refused():
    with pytest.raises(MigrationError, match="no schema_version"):
        upgrade("thing", {"value": "x"})


def test_a_legacy_ledger_row_loads_through_the_chain(tmp_path):
    """End to end at the real read boundary: a report persisted under an old schema shape
    loads under today's model once its migration is registered."""
    ledger = Ledger(tmp_path / "ledger.db")
    ticket = Ticket(
        id="T-old", repository=str(tmp_path), request="a ticket long enough to be valid"
    )
    report = RunReport(run_id="run-" + "a" * 12, ticket=ticket, outcome=Outcome.REFUSED,
                       evidence="refused: example")
    ledger.save(report)

    # Rewrite the stored row as a synthetic v0: the summary field under its "old" name.
    import json
    import sqlite3
    with sqlite3.connect(tmp_path / "ledger.db") as c:
        (row,) = c.execute("SELECT report_json FROM runs").fetchone()
        data = json.loads(row)
        data["schema_version"] = 0
        data["synopsis"] = data.pop("summary")
        c.execute("UPDATE runs SET report_json = ?", (json.dumps(data),))

    with pytest.raises(MigrationError):  # no path registered yet: fails loudly, not by luck
        ledger.get(report.run_id)

    MIGRATIONS[("run_report", 0)] = lambda p: {
        k: v for k, v in p.items() if k != "synopsis"
    } | {"summary": p["synopsis"]}
    loaded = ledger.get(report.run_id)
    assert loaded is not None
    assert loaded.schema_version == SCHEMA_VERSION
    assert loaded.run_id == report.run_id
