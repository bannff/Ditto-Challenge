from inventory import Inventory


def _stocked():
    inv = Inventory()
    inv.add_item("A", "alpha", 5, 1.0)   # exactly on threshold -> should reorder
    inv.add_item("B", "bravo", 2, 1.0)   # below threshold -> should reorder
    inv.add_item("C", "charlie", 9, 1.0)  # above threshold -> should not
    return inv


def test_reorder_is_boundary_inclusive():
    skus = [i.sku for i in _stocked().needs_reorder(5)]
    assert "A" in skus  # quantity == threshold must be included
    assert "B" in skus
    assert "C" not in skus
