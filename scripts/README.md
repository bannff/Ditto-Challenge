# Developer scripts

These scripts demonstrate or support autodev outside the packaged CLI. They are intentionally separate from `src/` because they materialize fixtures, print walkthroughs, or refresh local development credentials.

- `demo.py` copies a bundled target into a scratch Git repository and runs a bug, feature, and refusal sequence. It supports the inventory app, the orders service, both suites, individual tickets, and optional inspectable artifact bundles.
- `demo_selfimprove.py` runs the same ticket with empty and primed lesson memory, then applies a hidden check to show whether recalled knowledge improved the change.
- `refresh-creds.sh` refreshes local AWS credentials through the configured `ada` values.

Examples:

```bash
uv run python scripts/demo.py
uv run python scripts/demo.py --app app2 --out demos/latest
uv run python scripts/demo_selfimprove.py
```
