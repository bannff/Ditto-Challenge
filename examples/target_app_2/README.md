# target_app_2

A small, self-contained multi-user **orders service** (pure stdlib, no server or DB) used
as a harder target repo for `autodev`. It spans a few modules on purpose — `store.py`
(data), `auth.py` (sessions + the `require_owner` object-level check), `service.py`
(business ops), `api.py` (an HTTP-flavored front door that maps domain errors to status
codes) — so a real bug requires reading across files rather than spotting a one-liner.

The seeded flaw is an **IDOR / broken object-level authorization**: some operations
enforce ownership and others do not. Finding it means comparing sibling operations and
reasoning about the check that's *absent*, not reading a comment that gives it away.

Two read paths load an order by id and skip the ownership check — `get_order`
(`GET /orders/<id>`) and `order_summary` (`GET /orders/<id>/summary`). The bug ticket and
its acceptance test only name the first, so a minimal fix passes the gate while the second
stays open. That gap is deliberate: it's what the self-improvement demo measures, where
"sweep every read path" is a lesson that lives only in memory.

Test with `pytest` (or `make test`); full gate with `make check` (`ruff check . && pytest -q`).
