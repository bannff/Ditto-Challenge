"""Authentication (token -> user) and authorization helpers.

Sessions map an opaque token to a user id. `require_owner` is the object-level access
check: a caller may act on their own orders, and admins may act on any. Callers are
responsible for invoking it before returning or mutating a resource they loaded by id.
"""

from __future__ import annotations

import secrets

from store import Order, Store, User


class AuthError(Exception):
    """Missing or invalid credentials (maps to 401)."""


class ForbiddenError(Exception):
    """Authenticated but not allowed to touch this resource (maps to 403)."""


class Sessions:
    def __init__(self) -> None:
        self._by_token: dict[str, str] = {}

    def issue(self, user_id: str) -> str:
        token = secrets.token_hex(8)
        self._by_token[token] = user_id
        return token

    def user_id_for(self, token: str | None) -> str:
        if not token or token not in self._by_token:
            raise AuthError("missing or invalid token")
        return self._by_token[token]


def current_user(store: Store, sessions: Sessions, token: str | None) -> User:
    """Resolve the caller from their token, or raise AuthError."""
    user_id = sessions.user_id_for(token)
    return store.get_user(user_id)


def require_owner(user: User, order: Order) -> None:
    """Object-level authorization: the caller must own the order (admins may act on any).

    Raises ForbiddenError when a non-admin caller does not own the order.
    """
    if user.role == "admin":
        return
    if order.owner_id != user.id:
        raise ForbiddenError(f"{user.id!r} may not access order {order.id!r}")
