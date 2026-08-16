"""Baseline behavior that is already correct. Green at baseline."""

import pytest
from inventory import Inventory, InventoryError, Item


def stocked() -> Inventory:
    inv = Inventory()
    inv.add_item("A1", "Widget", 10, 2.50)
    inv.add_item("B2", "Gadget", 3, 9.00)
    return inv


def test_add_and_get_item():
    inv = Inventory()
    item = inv.add_item("A1", "Widget", 10, 2.50)
    assert item == Item(sku="A1", name="Widget", quantity=10, unit_price=2.50)
    assert inv.get_item("A1").quantity == 10
    assert "A1" in inv
    assert len(inv) == 1


def test_add_duplicate_sku_rejected():
    inv = stocked()
    with pytest.raises(InventoryError):
        inv.add_item("A1", "Widget", 5, 1.0)


def test_get_unknown_sku_raises():
    inv = Inventory()
    with pytest.raises(InventoryError):
        inv.get_item("nope")


def test_remove_item():
    inv = stocked()
    removed = inv.remove_item("A1")
    assert removed.sku == "A1"
    assert "A1" not in inv
    with pytest.raises(InventoryError):
        inv.remove_item("A1")


def test_adjust_quantity_up_and_down():
    inv = stocked()
    assert inv.adjust_quantity("A1", 5).quantity == 15
    assert inv.adjust_quantity("A1", -6).quantity == 9


def test_adjust_below_zero_rejected():
    inv = stocked()
    with pytest.raises(InventoryError):
        inv.adjust_quantity("B2", -4)


def test_set_price():
    inv = stocked()
    assert inv.set_price("A1", 3.0).unit_price == 3.0


def test_items_sorted_by_sku():
    inv = stocked()
    assert [item.sku for item in inv.items()] == ["A1", "B2"]


def test_line_value():
    assert Item("A1", "Widget", 4, 2.5).line_value == 10.0


def test_negative_quantity_item_rejected():
    with pytest.raises(InventoryError):
        Item("A1", "Widget", -1, 1.0)
