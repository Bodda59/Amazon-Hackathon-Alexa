"""
nutrition_api.py — real nutrition data from USDA FoodData Central
(fallback: Open Food Facts). Caches every hit into kitchen.db so
repeated lookups are instant and the workflow survives going offline.

Required env var: USDA_API_KEY (falls back to DEMO_KEY — 30 req/hour).
Get a free key:   https://fdc.nal.usda.gov/api-key-signup.html
"""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from tools.db_path import KITCHEN_DB, assert_kitchen_db

USDA_API_KEY = os.environ.get("USDA_API_KEY", "DEMO_KEY")
USDA_BASE = "https://api.nal.usda.gov/fdc/v1"
OFF_BASE = "https://world.openfoodfacts.org"
CACHE_TTL_DAYS = 90
USER_AGENT = "kitchen-agent/0.1 (personal meal planner)"

# USDA nutrient IDs
USDA_NUTRIENTS = {
    1008: "calories",     # Energy (kcal)
    1003: "protein_g",
    1005: "carbs_g",      # Carbohydrate, by difference
    1004: "fat_g",        # Total lipid (fat)
    1079: "fiber_g",
    2000: "sugar_g",
    1093: "sodium_mg",
}

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS food_cache (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    source        TEXT NOT NULL,           -- usda | openfoodfacts | custom
    source_id     TEXT,
    serving_size  REAL NOT NULL DEFAULT 100,
    serving_unit  TEXT NOT NULL DEFAULT 'g',
    calories      REAL NOT NULL,
    protein_g     REAL NOT NULL DEFAULT 0,
    carbs_g       REAL NOT NULL DEFAULT 0,
    fat_g         REAL NOT NULL DEFAULT 0,
    fiber_g       REAL,
    sugar_g       REAL,
    sodium_mg     REAL,
    brand         TEXT,
    raw_json      TEXT,
    fetched_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (name, source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_food_cache_name ON food_cache (name);
"""


def _conn() -> sqlite3.Connection:
    assert_kitchen_db()
    c = sqlite3.connect(str(KITCHEN_DB))
    c.row_factory = sqlite3.Row
    c.executescript(CACHE_SCHEMA)
    return c


def _get_json(url: str, params=None, timeout: int = 15):
    """Tiny stdlib HTTP GET -> JSON. doseq=True handles list params."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


@dataclass
class Food:
    name: str
    source: str
    source_id: str
    serving_size: float = 100.0
    serving_unit: str = "g"
    calories: float = 0.0
    protein_g: float = 0.0
    carbs_g: float = 0.0
    fat_g: float = 0.0
    fiber_g: float | None = None
    sugar_g: float | None = None
    sodium_mg: float | None = None
    brand: str | None = None
    raw: dict = field(default_factory=dict)

    def to_row(self) -> dict:
        return {
            "name": self.name, "source": self.source,
            "source_id": self.source_id,
            "serving_size": self.serving_size, "serving_unit": self.serving_unit,
            "calories": self.calories, "protein_g": self.protein_g,
            "carbs_g": self.carbs_g, "fat_g": self.fat_g,
            "fiber_g": self.fiber_g, "sugar_g": self.sugar_g,
            "sodium_mg": self.sodium_mg, "brand": self.brand,
            "raw_json": json.dumps(self.raw),
        }


# ---------------------------------------------------------------------------
# USDA FoodData Central
# ---------------------------------------------------------------------------

def _usda_search(query: str, limit: int = 5) -> list[dict]:
    """Search USDA for foods matching a name. Returns raw API items."""
    params = [
        ("api_key", USDA_API_KEY),
        ("query", query),
        ("pageSize", limit),
        ("dataType", "Foundation"),
        ("dataType", "SR Legacy"),
        ("dataType", "Survey (FNDDS)"),
        ("dataType", "Branded"),
        ("requireAllWords", "false"),
    ]
    try:
        data = _get_json(f"{USDA_BASE}/foods/search", params)
        return data.get("foods", [])
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        print(f"[usda] HTTP {e.code} for {query!r}: {body}")
        return []
    except Exception as e:
        print(f"[usda] search failed for {query!r}: {type(e).__name__}: {e}")
        return []


def _usda_to_food(item: dict) -> Food | None:
    name = (item.get("description") or "").strip().lower()
    if not name:
        return None

    nutrients: dict[int, float] = {}
    for n in item.get("foodNutrients", []) or []:
        nid = (n.get("nutrient") or {}).get("id") or n.get("nutrientId")
        amt = n.get("amount") if "amount" in n else n.get("value")
        if nid in USDA_NUTRIENTS and amt is not None:
            nutrients[nid] = float(amt)

    def macro(key: str) -> float | None:
        nid = next((k for k, v in USDA_NUTRIENTS.items() if v == key), None)
        return nutrients.get(nid)

    return Food(
        name=name,
        source="usda",
        source_id=str(item.get("fdcId") or ""),
        serving_size=100.0,
        serving_unit="g",
        calories=macro("calories") or 0.0,
        protein_g=macro("protein_g") or 0.0,
        carbs_g=macro("carbs_g") or 0.0,
        fat_g=macro("fat_g") or 0.0,
        fiber_g=macro("fiber_g"),
        sugar_g=macro("sugar_g"),
        sodium_mg=macro("sodium_mg"),
        brand=item.get("brandOwner") or item.get("brandName"),
        raw=item,
    )


# ---------------------------------------------------------------------------
# Open Food Facts (fallback)
# ---------------------------------------------------------------------------

def _off_search(query: str, limit: int = 5) -> list[dict]:
    try:
        data = _get_json(f"{OFF_BASE}/cgi/search.pl", {
            "search_terms": query, "search_simple": 1,
            "action": "process", "json": 1, "page_size": limit,
        })
        return data.get("products", [])
    except Exception as e:
        print(f"[off] search failed for {query!r}: {e}")
        return []


def _off_to_food(product: dict) -> Food | None:
    name = (product.get("product_name") or "").strip().lower()
    if not name:
        return None
    n = product.get("nutriments") or {}

    def num(k) -> float | None:
        val = n.get(k)
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    sodium = num("sodium_100g")

    return Food(
        name=name,
        source="openfoodfacts",
        source_id=str(product.get("code", "")),
        serving_size=100.0,
        serving_unit="g",
        calories=num("energy-kcal_100g") or 0.0,
        protein_g=num("proteins_100g") or 0.0,
        carbs_g=num("carbohydrates_100g") or 0.0,
        fat_g=num("fat_100g") or 0.0,
        fiber_g=num("fiber_100g"),
        sugar_g=num("sugars_100g"),
        sodium_mg=(sodium * 1000.0) if sodium is not None else None,
        brand=product.get("brands"),
        raw=product,
    )


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _cache_put(conn: sqlite3.Connection, food: Food) -> int:
    row = food.to_row()
    cur = conn.execute(
        """INSERT INTO food_cache
               (name, source, source_id, serving_size, serving_unit,
                calories, protein_g, carbs_g, fat_g,
                fiber_g, sugar_g, sodium_mg, brand, raw_json)
           VALUES (:name, :source, :source_id, :serving_size, :serving_unit,
                   :calories, :protein_g, :carbs_g, :fat_g,
                   :fiber_g, :sugar_g, :sodium_mg, :brand, :raw_json)
           ON CONFLICT (name, source, source_id) DO UPDATE SET
               calories = excluded.calories,
               protein_g = excluded.protein_g,
               carbs_g = excluded.carbs_g,
               fat_g = excluded.fat_g,
               fiber_g = excluded.fiber_g,
               sugar_g = excluded.sugar_g,
               sodium_mg = excluded.sodium_mg,
               raw_json = excluded.raw_json,
               fetched_at = datetime('now')""",
        row,
    )
    conn.commit()
    return cur.lastrowid


def _cache_get(conn: sqlite3.Connection, name: str) -> dict | None:
    q = name.lower().strip()
    # Match exact name OR substring match (e.g., 'apple' matches 'apples, raw, with skin')
    row = conn.execute(
        """SELECT * FROM food_cache 
           WHERE name = ? COLLATE NOCASE 
              OR name LIKE ? COLLATE NOCASE
           ORDER BY fetched_at DESC LIMIT 1""",
        (q, f"%{q}%"),
    ).fetchone()

    if not row:
        return None

    d = dict(row)
    try:
        # SQLite datetime('now') stores UTC naive timestamps
        fetched = datetime.fromisoformat(d["fetched_at"])
        if datetime.utcnow() - fetched > timedelta(days=CACHE_TTL_DAYS):
            return None
    except ValueError:
        pass

    d.pop("raw_json", None)
    return d


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search_food(name: str, *, use_cache: bool = True, limit: int = 5) -> list[dict]:
    """Look up a food. Order: cache → USDA → Open Food Facts."""
    name = name.strip()
    if not name:
        return []

    conn = _conn()
    try:
        if use_cache:
            hit = _cache_get(conn, name)
            if hit:
                return [hit]

        results: list[Food] = []
        for item in _usda_search(name, limit=limit):
            f = _usda_to_food(item)
            if f:
                results.append(f)
                break  # top hit is enough for a single lookup

        if not results:
            for p in _off_search(name, limit=limit):
                f = _off_to_food(p)
                if f:
                    results.append(f)
                    break

        out = []
        for f in results:
            _cache_put(conn, f)
            r = f.to_row()
            r.pop("raw_json", None)
            out.append(r)
        return out
    finally:
        conn.close()


def add_custom_food(*, name: str, calories: float, protein_g: float,
                    carbs_g: float, fat_g: float,
                    serving_size: float = 100, serving_unit: str = "g",
                    fiber_g: float | None = None, sugar_g: float | None = None,
                    sodium_mg: float | None = None,
                    brand: str | None = None) -> int:
    """Store a user-defined food in the cache (source='custom')."""
    conn = _conn()
    try:
        f = Food(name=name.lower().strip(), source="custom", source_id="",
                 serving_size=serving_size, serving_unit=serving_unit,
                 calories=calories, protein_g=protein_g, carbs_g=carbs_g,
                 fat_g=fat_g, fiber_g=fiber_g, sugar_g=sugar_g,
                 sodium_mg=sodium_mg, brand=brand)
        return _cache_put(conn, f)
    finally:
        conn.close()


def stats() -> dict:
    conn = _conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM food_cache").fetchone()[0]
        by_source = {r["source"]: r["n"] for r in conn.execute(
            "SELECT source, COUNT(*) AS n FROM food_cache GROUP BY source")}
        return {"total": total, "by_source": by_source}
    finally:
        conn.close()