"""Schema migrations for persisted contracts.

Every persisted contract carries `schema_version`, stamped at write time. This is the read
side: before a stored payload is validated against today's model, it is walked forward one
version at a time through registered transforms. Migrations are data — a dict entry per
(kind, from_version) — so upgrading a schema in production means registering a transform,
not editing load paths, and workflows persisted under the old shape keep loading flawlessly.

The registry for a first-version schema is empty on purpose: there is nothing to migrate
*from* yet. The machinery is exercised by tests that register synthetic transforms and prove
a legacy payload walks the chain. Fail-closed: a payload newer than the code, or older with
no registered path forward, raises rather than half-loads.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .contracts import SCHEMA_VERSION

Payload = dict[str, Any]
Transform = Callable[[Payload], Payload]

# (kind, from_version) -> transform producing the (from_version + 1) shape.
# Adding a schema change means bumping SCHEMA_VERSION and adding one row here.
MIGRATIONS: dict[tuple[str, int], Transform] = {}


class MigrationError(ValueError):
    """A persisted payload cannot be brought to the current schema."""


def upgrade(kind: str, payload: Payload, *, target: int = SCHEMA_VERSION) -> Payload:
    """Walk `payload` forward to `target`, one registered transform per version step.

    Returns the payload unchanged when it is already current. Raises MigrationError for a
    payload from the future (written by newer code) or one with no registered path — both
    must fail loudly rather than validate by luck.
    """
    version = payload.get("schema_version")
    if not isinstance(version, int):
        raise MigrationError(f"{kind}: persisted payload carries no schema_version")
    if version > target:
        raise MigrationError(
            f"{kind}: payload is schema v{version}, newer than this code's v{target}"
        )
    while version < target:
        transform = MIGRATIONS.get((kind, version))
        if transform is None:
            raise MigrationError(
                f"{kind}: no migration registered from v{version} toward v{target}"
            )
        payload = transform(dict(payload))
        payload["schema_version"] = version + 1
        version += 1
    return payload
