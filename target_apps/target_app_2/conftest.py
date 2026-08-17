# Presence of this file puts target_app_2/ on sys.path so tests can import the modules.
import pytest
from api import Api
from auth import Sessions
from service import OrderService
from store import Store


@pytest.fixture
def app():
    """A seeded service: alice, bob, and an admin, with one open order owned by alice.

    Returns (api, tokens_by_user, alice_order_id).
    """
    store = Store()
    sessions = Sessions()
    store.add_user("alice", "Alice")
    store.add_user("bob", "Bob")
    store.add_user("admin", "Admin", role="admin")
    api = Api(OrderService(store, sessions))
    tokens = {u: sessions.issue(u) for u in ("alice", "bob", "admin")}
    order_id = api.handle(
        "POST", "/orders", token=tokens["alice"], body={"item": "widget", "quantity": 3}
    ).body["id"]
    return api, tokens, order_id
