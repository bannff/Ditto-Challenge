"""Reproduces the IDOR on GET /orders/<id>.

A user must not be able to read another user's order. Red at baseline (the fetch returns
200 with someone else's order); green once object-level ownership is enforced (403). An
admin must still be able to read any order.
"""


def test_cannot_read_another_users_order(app):
    api, tokens, order_id = app  # order owned by alice
    resp = api.handle("GET", f"/orders/{order_id}", token=tokens["bob"])
    assert resp.status == 403


def test_cannot_read_another_users_order_summary(app):
    api, tokens, order_id = app
    resp = api.handle("GET", f"/orders/{order_id}/summary", token=tokens["bob"])
    assert resp.status == 403


def test_admin_may_read_any_order_or_summary(app):
    api, tokens, order_id = app
    for path in (f"/orders/{order_id}", f"/orders/{order_id}/summary"):
        resp = api.handle("GET", path, token=tokens["admin"])
        assert resp.status == 200
        assert resp.body["id"] == order_id
