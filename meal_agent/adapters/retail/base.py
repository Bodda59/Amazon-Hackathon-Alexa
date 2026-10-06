"""Retailer adapter contracts; checkout implementations must enforce approval."""

from __future__ import annotations

from typing import Protocol


class RetailerAdapter(Protocol):
    async def search_products(self, query: str) -> list[dict[str, object]]:
        """Search purchasable products without placing an order."""
        ...

    async def build_cart(self, product_ids: list[str]) -> dict[str, object]:
        """Build a reviewable cart; must not perform checkout."""
        ...

    async def checkout(self, cart_id: str, approval_token: str) -> dict[str, object]:
        """Place an order only after validating a server-issued approval token."""
        ...
