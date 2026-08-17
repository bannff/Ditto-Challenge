"""Reproduces the low_stock off-by-one. Red at baseline, green once fixed."""

from inventory import Inventory


def test_low_stock_includes_items_at_threshold():
    inv = Inventory()
    inv.add_item("A1", "Widget", 5, 1.0)   # exactly on the threshold
    inv.add_item("B2", "Gadget", 4, 1.0)   # below
    inv.add_item("C3", "Gizmo", 6, 1.0)    # above

    low = {item.sku for item in inv.low_stock(5)}

    assert low == {"A1", "B2"}
