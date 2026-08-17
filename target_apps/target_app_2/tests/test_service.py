"""Baseline behavior of the orders service. Green at baseline."""


def test_create_and_get_own_order(app):
    api, tokens, order_id = app
    resp = api.handle("GET", f"/orders/{order_id}", token=tokens["alice"])
    assert resp.status == 200
    assert resp.body["item"] == "widget"
    assert resp.body["quantity"] == 3


def test_list_only_returns_my_orders(app):
    api, tokens, order_id = app
    assert api.handle("GET", "/orders", token=tokens["bob"]).body == []
    mine = api.handle("GET", "/orders", token=tokens["alice"]).body
    assert [o["id"] for o in mine] == [order_id]


def test_requires_authentication(app):
    api, tokens, order_id = app
    assert api.handle("GET", f"/orders/{order_id}").status == 401


def test_cancel_own_order(app):
    api, tokens, order_id = app
    resp = api.handle("POST", f"/orders/{order_id}/cancel", token=tokens["alice"])
    assert resp.status == 200
    assert resp.body["status"] == "cancelled"


def test_cancel_another_users_order_is_forbidden(app):
    api, tokens, order_id = app  # order owned by alice
    assert api.handle("POST", f"/orders/{order_id}/cancel", token=tokens["bob"]).status == 403


def test_unknown_order_is_404(app):
    api, tokens, order_id = app
    assert api.handle("GET", "/orders/nope", token=tokens["alice"]).status == 404
