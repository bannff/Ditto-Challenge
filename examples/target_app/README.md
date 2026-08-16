# target_app

A tiny, self-contained inventory/store library (`inventory.py`, pure stdlib) used as the
target repo that `autodev` resolves tickets against. It tracks SKU-keyed stock and answers
questions about it: total value on hand, what's running low, and so on.

Test it with `pytest` (or `make test`); run the full lint + test gate with `make check`
(`ruff check . && pytest -q`).
