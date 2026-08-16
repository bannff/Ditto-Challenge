"""A tiny in-memory inventory/store library. Pure stdlib, no I/O, no network.

Model a store's stock as SKU-keyed items and answer questions about it:
value on hand, what's running low, and so on. Everything is deterministic and
side-effect free apart from mutating the Inventory instance you call it on.
"""

from __future__ import annotations

from dataclasses import dataclass


class InventoryError(Exception):
    """Raised when an operation is invalid (unknown SKU, bad quantity, ...)."""


@dataclass(frozen=True)
class Item:
    sku: str
    name: str
    quantity: int
    unit_price: float
    discontinued: bool = False

    def __post_init__(self) -> None:
        if not self.sku:
            raise InventoryError("sku must be non-empty")
        if self.quantity < 0:
            raise InventoryError("quantity must be non-negative")
        if self.unit_price < 0:
            raise InventoryError("unit_price must be non-negative")

    @property
    def line_value(self) -> float:
        return self.quantity * self.unit_price


class Inventory:
    def __init__(self) -> None:
        self._items: dict[str, Item] = {}

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, sku: object) -> bool:
        return sku in self._items

    def items(self) -> list[Item]:
        """All items, ordered by SKU for stable output."""
        return [self._items[sku] for sku in sorted(self._items)]

    def get_item(self, sku: str) -> Item:
        try:
            return self._items[sku]
        except KeyError:
            raise InventoryError(f"unknown sku: {sku!r}") from None

    def add_item(
        self, sku: str, name: str, quantity: int, unit_price: float, discontinued: bool = False
    ) -> Item:
        """Register a brand-new SKU. Use adjust_quantity to change stock later."""
        if sku in self._items:
            raise InventoryError(f"sku already exists: {sku!r}")
        item = Item(
            sku=sku, name=name, quantity=quantity, unit_price=unit_price, discontinued=discontinued
        )
        self._items[sku] = item
        return item

    def remove_item(self, sku: str) -> Item:
        """Drop a SKU entirely and return the removed item."""
        item = self.get_item(sku)
        del self._items[sku]
        return item

    def adjust_quantity(self, sku: str, delta: int) -> Item:
        """Change stock on hand by delta (negative to sell/consume).

        Refuses to drive stock negative rather than silently clamping.
        """
        item = self.get_item(sku)
        new_quantity = item.quantity + delta
        if new_quantity < 0:
            raise InventoryError(
                f"cannot reduce {sku!r} below zero "
                f"(have {item.quantity}, delta {delta})"
            )
        updated = Item(
            sku=item.sku,
            name=item.name,
            quantity=new_quantity,
            unit_price=item.unit_price,
        )
        self._items[sku] = updated
        return updated

    def set_price(self, sku: str, unit_price: float) -> Item:
        item = self.get_item(sku)
        updated = Item(
            sku=item.sku,
            name=item.name,
            quantity=item.quantity,
            unit_price=unit_price,
        )
        self._items[sku] = updated
        return updated

    def total_value(self) -> float:
        """Total retail value of everything on hand."""
        # BUG (bug-2): sums the unit price but ignores how many are in stock,
        # so a shelf of 100 widgets is valued the same as a single one.
        return sum(item.unit_price for item in self._items.values())

    def low_stock(self, threshold: int) -> list[Item]:
        """Items at or below threshold, ordered by SKU.

        "At or below" means an item sitting exactly on the threshold counts as
        low and should appear in the reorder list.
        """
        # BUG (bug-1): strict < drops items sitting exactly on the threshold,
        # so a threshold of 5 misses an item with quantity == 5.
        return [item for item in self.items() if item.quantity < threshold]

    def needs_reorder(self, threshold: int) -> list[Item]:
        """SKUs to reorder: items at or below the threshold, ordered by SKU."""
        # BUG (bug-3): strict < repeats low_stock's off-by-one and drops items sitting
        # exactly on the threshold.
        return [item for item in self.items() if item.quantity < threshold]
