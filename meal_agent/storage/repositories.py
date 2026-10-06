"""Repository interfaces for per-user durable state."""

from __future__ import annotations

from typing import Any, Protocol


class InventoryRepository(Protocol):
    async def list_items(self, user_id: str) -> list[dict[str, Any]]:
        """Return inventory rows isolated to one authenticated user."""
        ...

    async def upsert_item(self, user_id: str, item: dict[str, Any]) -> None:
        """Create or update one inventory item and record an audit event."""
        ...
