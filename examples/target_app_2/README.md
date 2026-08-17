# target_app_2

A small, self-contained multi-user **orders service** (pure stdlib, no server or DB) used
as a harder target repo for `autodev`. It spans a few modules on purpose — `store.py`
(data), `auth.py` (sessions + the `require_owner` object-level check), `service.py`
(business ops), `api.py` (an HTTP-flavored front door that maps domain errors to status
codes) — so a real bug requires reading across files rather than spotting a one-liner.

Orders are accessed through owner-or-admin authorization. The service keeps the
authorization decision next to order operations, while the API stays responsible for
mapping domain errors to HTTP-style responses.

Test with `pytest` (or `make test`); full gate with `make check` (`ruff check . && pytest -q`).
