# Demo target repositories

This folder contains the small target repositories and typed tickets used to demonstrate autodev’s full workflow. The demos copy a target into a scratch Git repository before each run, so autodev can create its isolated run branch and leave the original fixture unchanged.

## Simple target: `target_app/`

`target_app` is a compact inventory library with one main module. It is the fast, easy-to-read target for showing the basic loop:

- diagnose and fix an inclusive-boundary bug in `low_stock`;
- add `total_quantity` from an existing acceptance test;
- implement a specification-only feature such as `top_items`;
- refuse a clearly unsafe request.

Its tickets make it easy to see the normal **Discover → Implement → Verify → Learn** flow without cross-module investigation.

## Complex target: `target_app_2/`

`target_app_2` is a small multi-user orders service split across storage, authentication, service, and API modules. It exercises the same workflow on work that requires agents to compare related code paths and collaborate across files:

- fix the seeded broken object-level authorization (IDOR) in an order read path;
- add an admin-only order-listing endpoint;
- refuse a request to remove authorization checks.

The IDOR fixture intentionally includes a related summary read path not named by the acceptance test. `demo_selfimprove.py` uses that gap to show adaptive swarm collaboration: a control run can satisfy the named test, while a run primed with a prior lesson should inspect sibling read paths and secure both.

## Tickets and demos

`tickets/` holds the JSON inputs. `scripts/demo.py` runs either app’s standard bug, feature, and refusal sequence:

```bash
uv run python scripts/demo.py            # inventory app
uv run python scripts/demo.py --app app2 # orders service
uv run python scripts/demo.py --app all  # both
```

For the memory before/after demonstration:

```bash
uv run python scripts/demo_selfimprove.py            # orders-service IDOR scenario
uv run python scripts/demo_selfimprove.py --app app1 # inventory scenario
```

Each target also has a `Makefile` with `test` and `check` targets for local fixture validation.
