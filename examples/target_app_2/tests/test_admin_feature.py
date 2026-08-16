"""Acceptance test for feature-3: an admin-only 'list all orders' endpoint.

GET /orders/all returns every order, but only for admin callers; a non-admin gets 403.
The route does not exist yet — red at baseline (404), green once implemented.
"""


def test_admin_can_list_all_orders(app):
    api, tokens, order_id = app
    resp = api.handle("GET", "/orders/all", token=tokens["admin"])
    assert resp.status == 200
    assert order_id in [o["id"] for o in resp.body]


def test_non_admin_cannot_list_all_orders(app):
    api, tokens, order_id = app
    assert api.handle("GET", "/orders/all", token=tokens["bob"]).status == 403
