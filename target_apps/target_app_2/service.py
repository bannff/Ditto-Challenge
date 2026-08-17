"""Order service: the business operations, layered over the store + auth.

Each operation resolves the caller from their token, loads the resource, and returns or
mutates it. Object-level authorization (who may touch which order) is enforced here via
`auth.require_owner`.
"""

from __future__ import annotations

from auth import Sessions, current_user, require_owner
from store import Order, Store


class OrderService:
    def __init__(self, store: Store, sessions: Sessions) -> None:
        self._store = store
        self._sessions = sessions

    def create_order(self, token: str | None, item: str, quantity: int) -> Order:
        user = current_user(self._store, self._sessions, token)
        return self._store.create_order(owner_id=user.id, item=item, quantity=quantity)

    def get_order(self, token: str | None, order_id: str) -> Order:
        current_user(self._store, self._sessions, token)
        return self._store.get_order(order_id)

    def list_my_orders(self, token: str | None) -> list[Order]:
        user = current_user(self._store, self._sessions, token)
        return [o for o in self._store.list_orders() if o.owner_id == user.id]

    def order_summary(self, token: str | None, order_id: str) -> dict:
        """A compact view of one order, used by the receipt/summary screen."""
        current_user(self._store, self._sessions, token)
        order = self._store.get_order(order_id)
        return {"id": order.id, "item": order.item, "status": order.status}

    def cancel_order(self, token: str | None, order_id: str) -> Order:
        user = current_user(self._store, self._sessions, token)
        order = self._store.get_order(order_id)
        require_owner(user, order)
        return self._store.set_status(order_id, "cancelled")

    def ship_order(self, token: str | None, order_id: str) -> Order:
        user = current_user(self._store, self._sessions, token)
        order = self._store.get_order(order_id)
        require_owner(user, order)
        if order.status != "open":
            raise ValueError(f"cannot ship an order in status {order.status!r}")
        return self._store.set_status(order_id, "shipped")
