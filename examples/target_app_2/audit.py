"""Fixed-shape audit events for authorization denials."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol


@dataclass(frozen=True)
class AuditEvent:
    event_type: Literal["authorization_denied"] = field(
        default="authorization_denied", init=False
    )
    action: Literal["order.read"] = field(default="order.read", init=False)
    outcome: Literal["denied"] = field(default="denied", init=False)


class AuditSink(Protocol):
    def record(self, event: AuditEvent) -> None: ...


class InMemoryAuditLog:
    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    @property
    def events(self) -> tuple[AuditEvent, ...]:
        return tuple(self._events)

    def record(self, event: AuditEvent) -> None:
        self._events.append(event)
