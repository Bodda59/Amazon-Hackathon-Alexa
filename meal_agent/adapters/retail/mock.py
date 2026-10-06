"""Deterministic mock retailer; it never contacts a retailer or places a real order."""

from __future__ import annotations

from typing import Any

_MOCK_PRODUCTS = [
    {"id": "mock-chicken-500", "name": "Chicken breast, 500 g", "price": 7.49, "pack_size_g": 500, "ingredient": "chicken"},
    {"id": "mock-tofu-400", "name": "Firm tofu, 400 g", "price": 3.29, "pack_size_g": 400, "ingredient": "tofu"},
    {"id": "mock-rice-1000", "name": "Brown rice, 1 kg", "price": 4.49, "pack_size_g": 1000, "ingredient": "rice"},
    {"id": "mock-spinach-200", "name": "Spinach, 200 g", "price": 2.99, "pack_size_g": 200, "ingredient": "spinach"},
    {"id": "mock-yogurt-500", "name": "Greek yogurt, 500 g", "price": 4.19, "pack_size_g": 500, "ingredient": "yogurt"},
    {"id": "mock-cottage-250", "name": "Cottage cheese, 250 g", "price": 2.79, "pack_size_g": 250, "ingredient": "cottage cheese"},
    {"id": "mock-protein-powder-300", "name": "Whey protein powder, 300 g", "price": 9.99, "pack_size_g": 300, "ingredient": "protein powder"},
    {"id": "mock-eggs-12", "name": "Eggs, dozen", "price": 4.99, "pack_size_g": 600, "ingredient": "eggs"},
]


class MockRetailer:
    async def search_products(self, query: str) -> list[dict[str, object]]:
        """Search a clearly marked fake catalog; no external calls are made."""
        terms = set(query.casefold().split())
        matches = [
            {**product, "mock": True}
            for product in _MOCK_PRODUCTS
            if terms & set(str(product["ingredient"]).casefold().split())
            or terms & set(str(product["name"]).casefold().split())
        ]
        return matches

    async def build_cart(self, product_ids: list[str]) -> dict[str, object]:
        selected = [product for product in _MOCK_PRODUCTS if product["id"] in set(product_ids)]
        return {
            "status": "ready_for_review",
            "products": [{**product, "mock": True} for product in selected],
            "total": round(sum(float(product["price"]) for product in selected), 2),
            "currency": "USD",
            "mock": True,
        }

    async def checkout(self, cart_id: str, approval_token: str) -> dict[str, object]:
        """Reject calls outside the procurement service's server-side approval guard."""
        del cart_id, approval_token
        return {"status": "disabled", "reason": "Call the procurement agent; direct adapter checkout is forbidden."}
