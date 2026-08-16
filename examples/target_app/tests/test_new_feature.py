"""Acceptance test for feature-1: Inventory.total_quantity does not exist yet.

Red at baseline (AttributeError), green once the feature is implemented.
"""

from inventory import Inventory


def test_total_quantity_sums_units_on_hand():
    inv = Inventory()
    inv.add_item("A1", "Widget", 10, 2.5)
    inv.add_item("B2", "Gadget", 3, 9.0)

    assert inv.total_quantity() == 13


def test_total_quantity_empty_is_zero():
    assert Inventory().total_quantity() == 0
