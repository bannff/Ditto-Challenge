"""A thin HTTP-flavored front door over OrderService — no real socket, so it stays
deterministic and unit-testable. `handle` maps a (method, path, token, body) request to a
(status, body) Response, translating domain exceptions to status codes:

  401 unauthenticated · 403 forbidden · 404 not found · 400 bad request · 200/201 ok

Routes:
  POST /orders                 create an order        -> 201
  GET  /orders                 list the caller's own  -> 200
  GET  /orders/<id>            fetch one order        -> 200
  GET  /orders/<id>/summary    compact receipt view   -> 200
  POST /orders/<id>/cancel     cancel one order       -> 200
  POST /orders/<id>/ship       ship one order         -> 200
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from auth import AuthError, ForbiddenError
from service import OrderService
from store import NotFoundError, Order


@dataclass(frozen=True)
class Response:
    status: int
    body: Any


def _order_body(order: Order) -> dict:
    return asdict(order)


class Api:
    def __init__(self, service: OrderService) -> None:
        self._service = service

    def handle(
        self,
        method: str,
        path: str,
        token: str | None = None,
        body: dict | None = None,
    ) -> Response:
        try:
            return self._route(method, path, token, body or {})
        except AuthError as e:
            return Response(401, {"error": str(e)})
        except ForbiddenError as e:
            return Response(403, {"error": str(e)})
        except NotFoundError as e:
            return Response(404, {"error": str(e)})
        except ValueError as e:
            return Response(400, {"error": str(e)})

    def _route(self, method: str, path: str, token: str | None, body: dict) -> Response:
        parts = [p for p in path.strip("/").split("/") if p]

        if parts == ["orders"]:
            if method == "POST":
                order = self._service.create_order(
                    token, body.get("item", ""), body.get("quantity", 0)
                )
                return Response(201, _order_body(order))
            if method == "GET":
                orders = self._service.list_my_orders(token)
                return Response(200, [_order_body(o) for o in orders])

        elif len(parts) == 2 and parts[0] == "orders" and method == "GET":
            order = self._service.get_order(token, parts[1])
            return Response(200, _order_body(order))

        elif len(parts) == 3 and parts[0] == "orders" and method == "POST":
            order_id, action = parts[1], parts[2]
            if action == "cancel":
                return Response(200, _order_body(self._service.cancel_order(token, order_id)))
            if action == "ship":
                return Response(200, _order_body(self._service.ship_order(token, order_id)))

        elif len(parts) == 3 and parts[0] == "orders" and parts[2] == "summary" \
                and method == "GET":
            return Response(200, self._service.order_summary(token, parts[1]))

        return Response(404, {"error": f"no route for {method} {path}"})
