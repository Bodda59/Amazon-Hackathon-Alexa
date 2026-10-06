"""Trusted food nutrition providers: USDA FoodData Central and Open Food Facts."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from meal_agent.config import settings
from meal_agent.storage.domain_store import DomainStore

_USDA_SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
_USDA_FOOD_URL = "https://api.nal.usda.gov/fdc/v1/food/{fdc_id}"
_OFF_PRODUCT_URL = "https://world.openfoodfacts.org/api/v2/product/{barcode}.json"
_NUTRIENT_IDS = {1008: "kcal", 1003: "protein_g", 1005: "carbs_g", 1004: "fat_g"}
_NUTRIENT_NAMES = {
    "energy": "kcal",
    "energy (atwater general factors)": "kcal",
    "protein": "protein_g",
    "carbohydrates": "carbs_g",
    "carbohydrate, by difference": "carbs_g",
    "total lipid (fat)": "fat_g",
    "fat": "fat_g",
}


class FoodDataError(RuntimeError):
    """Nutrition provider could not return a trusted usable food profile."""


class FoodDataClient:
    """Async API client with a 30-day SQLite cache and USDA-first lookups."""

    def __init__(
        self,
        store: DomainStore,
        api_key: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 8.0,
    ) -> None:
        self.store = store
        self.api_key = api_key if api_key is not None else settings.usda_api_key
        self.http_client = http_client
        self.timeout_seconds = timeout_seconds

    async def _get_json(self, url: str, **kwargs: Any) -> Any:
        if self.http_client is not None:
            response = await self.http_client.get(url, timeout=self.timeout_seconds, **kwargs)
        else:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(url, **kwargs)
        response.raise_for_status()
        return response.json()

    async def lookup(self, food: str) -> dict[str, Any]:
        """Find the best USDA match, using Open Food Facts only when USDA is unavailable."""
        query = food.strip()
        if not query:
            raise ValueError("Food name cannot be empty.")
        cache_key = f"food:{query.casefold()}"
        cached = await asyncio.to_thread(self.store.cache_get, cache_key)
        if cached is not None:
            return cached
        usda_error: Exception | None = None
        try:
            if not self.api_key:
                raise FoodDataError("USDA_API_KEY is not configured.")
            result = await self._lookup_usda(query)
        except (httpx.HTTPError, FoodDataError, ValueError, KeyError, TypeError) as error:
            usda_error = error
            try:
                result = await self._lookup_open_food_facts(query)
            except (httpx.HTTPError, FoodDataError, ValueError, TypeError) as off_error:
                if usda_error is None:
                    raise
                raise FoodDataError(
                    f"USDA lookup failed ({type(usda_error).__name__}); "
                    f"Open Food Facts fallback failed ({type(off_error).__name__})."
                ) from off_error
        await asyncio.to_thread(self.store.cache_put, cache_key, result)
        return result

    async def lookup_barcode(self, barcode: str) -> dict[str, Any]:
        """Look up packaged food by barcode in Open Food Facts."""
        code = re.sub(r"\D", "", barcode)
        if len(code) not in {8, 12, 13, 14}:
            raise ValueError("Barcode must contain 8, 12, 13, or 14 digits.")
        cache_key = f"barcode:{code}"
        cached = await asyncio.to_thread(self.store.cache_get, cache_key)
        if cached is not None:
            return cached
        result = await self._lookup_open_food_facts(code, barcode=True)
        await asyncio.to_thread(self.store.cache_put, cache_key, result)
        return result

    async def _lookup_usda(self, query: str) -> dict[str, Any]:
        if not self.api_key:
            raise FoodDataError("USDA_API_KEY is not configured.")
        payload = await self._get_json(
            _USDA_SEARCH_URL,
            params={"api_key": self.api_key, "query": query, "pageSize": 8},
        )
        foods = payload.get("foods", [])
        if not foods:
            raise FoodDataError(f"USDA has no nutrition record matching {query!r}.")
        preferred = {"Foundation": 0, "SR Legacy": 1, "Survey (FNDDS)": 2, "Branded": 3}
        foods.sort(key=lambda item: preferred.get(item.get("dataType", ""), 10))
        selected = foods[0]
        nutrients: dict[str, float] = {}
        for nutrient in selected.get("foodNutrients", []):
            nutrient_id = nutrient.get("nutrientId") or nutrient.get("nutrient", {}).get("id")
            key = _NUTRIENT_IDS.get(nutrient_id)
            value = nutrient.get("value", nutrient.get("amount"))
            if key and value is not None:
                nutrients[key] = float(value)
        if len(nutrients) < 4:
            details = await self._get_json(
                _USDA_FOOD_URL.format(fdc_id=selected["fdcId"]),
                params={"api_key": self.api_key},
            )
            for nutrient in details.get("foodNutrients", []):
                nutrient_meta = nutrient.get("nutrient", {})
                nutrient_id = nutrient_meta.get("id")
                key = _NUTRIENT_IDS.get(nutrient_id)
                value = nutrient.get("amount")
                if key is None:
                    key = _NUTRIENT_NAMES.get(str(nutrient_meta.get("name", "")).casefold())
                if key and value is not None:
                    nutrients[key] = float(value)
        normalized = self._normalize_nutrients(nutrients)
        return {
            "name": selected.get("description", query),
            "per_100g": normalized,
            "source": "USDA FoodData Central",
            "source_id": str(selected.get("fdcId", "")),
            "data_type": selected.get("dataType"),
            "verified": True,
        }

    async def _lookup_open_food_facts(self, query: str, barcode: bool = False) -> dict[str, Any]:
        url = _OFF_PRODUCT_URL.format(barcode=query) if barcode else "https://world.openfoodfacts.org/cgi/search.pl"
        params: dict[str, str | int] = {"json": 1}
        if not barcode:
            params.update({"search_terms": query, "page_size": 5, "fields": "product_name,nutriments,code"})
        payload = await self._get_json(url, params=params)
        if barcode:
            if payload.get("status") != 1:
                raise FoodDataError(f"Open Food Facts has no product for barcode {query!r}.")
            product = payload.get("product", {})
        else:
            products = payload.get("products", [])
            if not products:
                raise FoodDataError(f"Open Food Facts has no product matching {query!r}.")
            product = products[0]
        nutrients = product.get("nutriments", {})
        normalized = self._normalize_nutrients(
            {
                "kcal": nutrients.get("energy-kcal_100g"),
                "protein_g": nutrients.get("proteins_100g"),
                "carbs_g": nutrients.get("carbohydrates_100g"),
                "fat_g": nutrients.get("fat_100g"),
            }
        )
        return {
            "name": product.get("product_name") or query,
            "per_100g": normalized,
            "source": "Open Food Facts",
            "source_id": str(product.get("code", query)),
            "verified": True,
        }

    @staticmethod
    def _normalize_nutrients(nutrients: dict[str, Any]) -> dict[str, float]:
        normalized = {key: float(nutrients[key]) for key in _NUTRIENT_IDS.values() if nutrients.get(key) is not None}
        missing = set(_NUTRIENT_IDS.values()) - set(normalized)
        if missing:
            raise FoodDataError(f"Nutrition record is missing required nutrients: {', '.join(sorted(missing))}.")
        if any(value < 0 for value in normalized.values()):
            raise FoodDataError("Nutrition record contains negative nutrient values.")
        return normalized


_DEFAULT_CLIENT: FoodDataClient | None = None


def get_food_data_client() -> FoodDataClient:
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = FoodDataClient(DomainStore(settings.workflow_database_path))
    return _DEFAULT_CLIENT


async def nutrition_lookup(food: str) -> dict[str, Any]:
    """Public deterministic nutrition lookup tool backed by USDA/cache."""
    return await get_food_data_client().lookup(food)


async def lookup_barcode(barcode: str) -> dict[str, Any]:
    """Public barcode lookup using Open Food Facts."""
    return await get_food_data_client().lookup_barcode(barcode)
