"""In-memory data store for the orders service. Pure stdlib, no I/O, no network.

Holds users and their orders. Everything is deterministic and side-effect free apart
from mutating the Store instance you call it on.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


class NotFoundError(Exception):
    """Raised when a requested user or order does not exist."""


@dataclass(frozen=True)
class User:
    id: str
    name: str
    role: str = "user"  # "user" or "admin"


@dataclass(frozen=True)
class Order:
    id: str
    owner_id: str
    item: str
    quantity: int
    status: str = "open"  # open | shipped | cancelled


class Store:
    def __init__(self) -> None:
        self._users: dict[str, User] = {}
        self._orders: dict[str, Order] = {}
        self._seq = 0

    # --- users ---
    def add_user(self, user_id: str, name: str, role: str = "user") -> User:
        if user_id in self._users:
            raise ValueError(f"user already exists: {user_id!r}")
        user = User(id=user_id, name=name, role=role)
        self._users[user_id] = user
        return user

    def get_user(self, user_id: str) -> User:
        try:
            return self._users[user_id]
        except KeyError:
            raise NotFoundError(f"unknown user: {user_id!r}") from None

    # --- orders ---
    def create_order(self, owner_id: str, item: str, quantity: int) -> Order:
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        self._seq += 1
        order = Order(id=f"o{self._seq}", owner_id=owner_id, item=item, quantity=quantity)
        self._orders[order.id] = order
        return order

    def get_order(self, order_id: str) -> Order:
        try:
            return self._orders[order_id]
        except KeyError:
            raise NotFoundError(f"unknown order: {order_id!r}") from None

    def list_orders(self) -> list[Order]:
        """Every order in the store, ordered by id for stable output."""
        return [self._orders[oid] for oid in sorted(self._orders)]

    def set_status(self, order_id: str, status: str) -> Order:
        order = self.get_order(order_id)
        updated = replace(order, status=status)
        self._orders[order_id] = updated
        return updated
