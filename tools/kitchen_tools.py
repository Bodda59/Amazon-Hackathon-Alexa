"""
kitchen_tools.py — LangChain tools for the kitchen agents.

Only touches kitchen.db. No foods.db, no checkpoints.db.

Each tool is decorated with @tool so LangChain can:
  * derive the schema from the function signature,
  * pass the docstring to the LLM as the tool description,
  * validate inputs via the type hints.

Ownership is enforced by `TOOLS_BY_AGENT` — bind only that agent's tools.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from langchain_core.tools import tool
from agents import nutrition, nutrition_api

# ---------------------------------------------------------------------------
# DB plumbing — swap this path if you move kitchen.db
# ---------------------------------------------------------------------------

from tools.db_path import KITCHEN_DB, assert_kitchen_db


@contextmanager
def _db():
    assert_kitchen_db()                # loud error if missing
    conn = sqlite3.connect(str(KITCHEN_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _log(conn, entity, entity_id, action, actor,
         before=None, after=None) -> None:
    """Write an actor-attributed row to events_log.
    Assumes you've dropped the auto-triggers; if you kept them, this
    becomes a complementary row and `actor='system'` rows come from
    the trigger."""
    conn.execute(
        """INSERT INTO events_log
               (entity, entity_id, action, actor, before_json, after_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (entity, entity_id, action, actor,
         json.dumps(before, default=str) if before is not None else None,
         json.dumps(after, default=str) if after is not None else None),
    )


def _row(cur) -> dict | None:
    r = cur.fetchone()
    return dict(r) if r else None


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


# ===========================================================================
# PANTRY  (Code)  — reads inventory, writes inventory
# ===========================================================================

@tool
def get_inventory(
    item: str | None = None,
    category: str | None = None,
    location: str | None = None,
) -> dict:
    """List inventory rows.

    Args:
        item: Substring match on item name (case-insensitive).
        category: Exact category ('protein', 'dairy', 'produce',
            'pantry', 'bakery', 'legume', 'beverage', 'snack').
        location: Exact location ('fridge', 'freezer', 'pantry', 'counter').

    Returns {"ok": True, "items": [{...}]}.
    """
    sql, params = "SELECT * FROM inventory WHERE 1=1", []
    if item:
        sql += " AND item LIKE ?"; params.append(f"%{item}%")
    if category:
        sql += " AND category = ?"; params.append(category)
    if location:
        sql += " AND location = ?"; params.append(location)
    sql += " ORDER BY category, item"
    with _db() as conn:
        return {"ok": True, "items": _rows(conn.execute(sql, params))}


@tool
def update_item(
    item: str,
    quantity: float,
    unit: str | None = None,
    expiry: str | None = None,
    low_stock_threshold: float | None = None,
    category: str | None = None,
    location: str | None = None,
) -> dict:
    """Add a new inventory item, or update an existing one by name.

    Args:
        item: Item name (unique key — case-sensitive exact match).
        quantity: New quantity (or quantity for a new item).
        unit: 'g', 'ml', 'count', 'can', 'loaf', etc. Default 'g' for new items.
        expiry: ISO date 'YYYY-MM-DD' or None.
        low_stock_threshold: Restock trigger level. Default 0.
        category: 'protein', 'dairy', 'produce', 'pantry', 'bakery', etc.
        location: 'fridge', 'freezer', 'pantry', 'counter'.

    Returns {"ok": True, "item": {...}}.
    """
    with _db() as conn:
        existing = _row(conn.execute(
            "SELECT * FROM inventory WHERE item = ?", (item,)))
        if existing:
            sets, params = ["quantity = ?"], [quantity]
            if unit is not None:                sets.append("unit = ?");                params.append(unit)
            if expiry is not None:              sets.append("expiry = ?");              params.append(expiry)
            if low_stock_threshold is not None: sets.append("low_stock_threshold = ?"); params.append(low_stock_threshold)
            if category is not None:            sets.append("category = ?");            params.append(category)
            if location is not None:            sets.append("location = ?");            params.append(location)
            sets.append("updated_at = datetime('now')")
            params.append(existing["id"])
            conn.execute(f"UPDATE inventory SET {', '.join(sets)} WHERE id = ?", params)
            updated = _row(conn.execute("SELECT * FROM inventory WHERE id = ?",
                                        (existing["id"],)))
            _log(conn, "inventory", existing["id"], "update", "pantry",
                 existing, updated)
            return {"ok": True, "item": updated}

        cur = conn.execute(
            """INSERT INTO inventory
                   (item, quantity, unit, expiry, low_stock_threshold,
                    category, location)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (item, quantity, unit or "g", expiry,
             low_stock_threshold if low_stock_threshold is not None else 0,
             category, location),
        )
        new = _row(conn.execute("SELECT * FROM inventory WHERE id = ?",
                                (cur.lastrowid,)))
        _log(conn, "inventory", cur.lastrowid, "insert", "pantry", None, new)
        return {"ok": True, "item": new}


@tool
def deduct_ingredients(ingredients: list[dict]) -> dict:
    """Subtract ingredients from inventory atomically (e.g. after cooking).

    Args:
        ingredients: List of {"item": str, "quantity": float, "unit": str}.
            Quantities are subtracted as-is; pass the unit used by the
            inventory row for correct math.

    Quantities floor at 0 — never goes negative.
    Returns {"ok": True, "deducted": [...], "missing": [names not in inventory]}.
    """
    if not ingredients:
        return {"ok": False, "error": "ingredients list is empty"}

    with _db() as conn:
        changed, missing = [], []
        for ing in ingredients:
            name = ing.get("item")
            qty = float(ing.get("quantity", 0))
            if not name or qty <= 0:
                return {"ok": False, "error": f"bad line item: {ing}"}
            row = _row(conn.execute(
                "SELECT * FROM inventory WHERE item = ?", (name,)))
            if not row:
                missing.append(name); continue
            new_qty = max(0.0, float(row["quantity"]) - qty)
            conn.execute(
                "UPDATE inventory SET quantity = ?, updated_at = datetime('now') WHERE id = ?",
                (new_qty, row["id"]),
            )
            changed.append({"item": name, "from": row["quantity"], "to": new_qty})
            _log(conn, "inventory", row["id"], "update", "pantry",
                 {"item": name, "quantity": row["quantity"]},
                 {"item": name, "quantity": new_qty})
        return {"ok": True, "deducted": changed, "missing": missing}


@tool
def get_low_stock() -> dict:
    """Items at or below their low_stock_threshold, most urgent first.

    Returns {"ok": True, "items": [{item, quantity, unit, low_stock_threshold, category}]}.
    """
    with _db() as conn:
        rows = _rows(conn.execute(
            """SELECT id, item, quantity, unit, low_stock_threshold, category
               FROM inventory
               WHERE quantity <= low_stock_threshold
               ORDER BY (low_stock_threshold - quantity) DESC, item"""
        ))
    return {"ok": True, "items": rows}


@tool
def get_expiring(within_days: int = 7) -> dict:
    """Items whose expiry falls between today and today + within_days.

    Args:
        within_days: Look-ahead window in days (default 7, inclusive).

    Returns {"ok": True, "items": [{item, quantity, unit, expiry, ...}], "cutoff": ...}.
    """
    cutoff = (date.today() + timedelta(days=within_days)).isoformat()
    today = date.today().isoformat()
    with _db() as conn:
        rows = _rows(conn.execute(
            """SELECT id, item, quantity, unit, expiry, category, location
               FROM inventory
               WHERE expiry IS NOT NULL AND expiry <= ? AND expiry >= ?
               ORDER BY expiry, item""",
            (cutoff, today),
        ))
    return {"ok": True, "items": rows, "cutoff": cutoff}


# ===========================================================================
# PREFERENCE FILTER  (Code)  — reads rules, writes rules only when told
# ===========================================================================

@tool
def get_exclusions() -> dict:
    """Read every hard rule (things the planner must never use).

    Returns {"ok": True, "exclusions": [{id, item, reason, created_at}]}.
    """
    with _db() as conn:
        return {"ok": True, "exclusions": _rows(conn.execute(
            "SELECT id, item, reason, created_at FROM exclusions ORDER BY item"
        ))}


@tool
def get_preferences(sentiment: str | None = None) -> dict:
    """Read soft preferences (likes and dislikes with weights).

    Args:
        sentiment: 'like', 'dislike', or None for both.

    Returns {"ok": True, "preferences": [{id, item, sentiment, weight, notes}]},
    sorted by weight descending.
    """
    sql, params = ("SELECT id, item, sentiment, weight, notes FROM preferences",
                   [])
    if sentiment in ("like", "dislike"):
        sql += " WHERE sentiment = ?"; params.append(sentiment)
    sql += " ORDER BY weight DESC, item"
    with _db() as conn:
        return {"ok": True, "preferences": _rows(conn.execute(sql, params))}


@tool
def add_exclusion(item: str, reason: str) -> dict:
    """Add a hard exclusion (allergy, ethics, medical).

    Args:
        item: The item to exclude, lowercase (e.g. 'eggs', 'peanuts').
        reason: Why — shown in audit log and any refusal message.

    Idempotent: refuses if the item is already excluded.
    Returns {"ok": True, "item": ...} or {"ok": False, "error": ...}.
    """
    item = item.strip().lower()
    with _db() as conn:
        if conn.execute("SELECT 1 FROM exclusions WHERE item = ?",
                        (item,)).fetchone():
            return {"ok": False, "error": f"'{item}' is already excluded"}
        cur = conn.execute(
            "INSERT INTO exclusions (item, reason) VALUES (?, ?)",
            (item, reason),
        )
        _log(conn, "exclusions", cur.lastrowid, "insert", "preference",
             None, {"item": item, "reason": reason})
    return {"ok": True, "item": item}


@tool
def add_preference(
    item: str,
    sentiment: str,
    weight: float = 1.0,
    notes: str | None = None,
) -> dict:
    """Add or update a soft preference.

    Args:
        item: The item name, lowercase.
        sentiment: 'like' or 'dislike'.
        weight: Strength 0.0–10.0 (default 1.0). Higher = stronger.
        notes: Optional context.

    Upserts on (item, sentiment).
    Returns {"ok": True, "item": ..., "sentiment": ..., "weight": ...}.
    """
    if sentiment not in ("like", "dislike"):
        return {"ok": False, "error": "sentiment must be 'like' or 'dislike'"}
    if not 0 <= weight <= 10:
        return {"ok": False, "error": "weight must be between 0 and 10"}
    item = item.strip().lower()
    with _db() as conn:
        conn.execute(
            """INSERT INTO preferences (item, sentiment, weight, notes)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (item, sentiment) DO UPDATE
                   SET weight = excluded.weight, notes = excluded.notes""",
            (item, sentiment, weight, notes),
        )
        _log(conn, "preferences", None, "insert", "preference",
             None, {"item": item, "sentiment": sentiment, "weight": weight})
    return {"ok": True, "item": item, "sentiment": sentiment, "weight": weight}


@tool
def remove_rule(kind: str, item: str) -> dict:
    """Remove an exclusion or preference row.

    Args:
        kind: 'exclusion' or 'preference'.
        item: The item name to remove.

    Returns {"ok": True, "removed": [rows that were deleted]}.
    """
    if kind not in ("exclusion", "preference"):
        return {"ok": False, "error": "kind must be 'exclusion' or 'preference'"}
    table = "exclusions" if kind == "exclusion" else "preferences"
    item = item.strip().lower()
    with _db() as conn:
        rows = _rows(conn.execute(f"SELECT * FROM {table} WHERE item = ?",
                                  (item,)))
        if not rows:
            return {"ok": False, "error": f"no {kind} found for '{item}'"}
        conn.execute(f"DELETE FROM {table} WHERE item = ?", (item,))
        for r in rows:
            _log(conn, table, r.get("id"), "delete", "preference", r, None)
    return {"ok": True, "removed": rows}


# ===========================================================================
# PLANNER  (LLM)  — read-only helpers. No write tools exist for this agent.
# ===========================================================================

@tool
def get_context_packet(include_history: int = 20) -> dict:
    """The full read-only bundle the planner reasons over.

    Args:
        include_history: How many recent meal_history rows to include.

    Returns {
        "ok": True,
        "profile": {...},
        "inventory": [...],
        "exclusions": [{item, reason}],
        "preferences": [{item, sentiment, weight}],
        "meal_history": [...],
        "shopping_list": [...],
        "generated_at": iso-timestamp
    }.
    """
    with _db() as conn:
        profile = _row(conn.execute("SELECT * FROM profile WHERE id = 1"))
        return {
            "ok": True,
            "profile": profile,
            "inventory": _rows(conn.execute(
                "SELECT * FROM inventory ORDER BY category, item")),
            "exclusions": _rows(conn.execute(
                "SELECT item, reason FROM exclusions ORDER BY item")),
            "preferences": _rows(conn.execute(
                "SELECT item, sentiment, weight FROM preferences ORDER BY weight DESC")),
            "meal_history": _rows(conn.execute(
                """SELECT id, suggested_at, decided_at, meal_name, meal_type,
                          calories, protein_g, carbs_g, fat_g, status, reason
                   FROM meal_history
                   ORDER BY suggested_at DESC LIMIT ?""",
                (include_history,))),
            "shopping_list": _rows(conn.execute(
                """SELECT id, item, quantity, unit, source, status, priority
                   FROM shopping_list
                   WHERE status IN ('pending','approved','ordered')
                   ORDER BY priority, item""")),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }


@tool
def get_meal_history(limit: int = 30, status: str | None = None) -> dict:
    """Recent meal suggestions and their outcomes.

    Args:
        limit: Maximum rows (default 30).
        status: Optional filter — 'suggested', 'accepted', 'rejected',
            'cooked', or 'skipped'.

    Returns {"ok": True, "meals": [{...}]}.
    """
    sql = ("SELECT id, suggested_at, decided_at, meal_name, meal_type, "
           "calories, protein_g, carbs_g, fat_g, status, reason "
           "FROM meal_history")
    params: list = []
    if status:
        sql += " WHERE status = ?"; params.append(status)
    sql += " ORDER BY suggested_at DESC LIMIT ?"; params.append(limit)
    with _db() as conn:
        return {"ok": True, "meals": _rows(conn.execute(sql, params))}


# ===========================================================================
# SHOPPING  (Code)  — reads inventory, writes shopping_list
# ===========================================================================

@tool
def diff_needs(needs: list[dict]) -> dict:
    """Compare a meal's required ingredients against current inventory.

    Args:
        needs: List of {"item": str, "quantity": float, "unit": str} that
            the plan requires.

    Returns {
        "ok": True,
        "covered": [{item, needed, have}],
        "short":   [{item, needed, have, missing, unit}]
    }.
    """
    with _db() as conn:
        inv = {r["item"].lower(): dict(r)
               for r in conn.execute("SELECT item, quantity, unit FROM inventory")}

    covered, short = [], []
    for need in needs:
        name = (need.get("item") or "").strip()
        qty = float(need.get("quantity", 0))
        row = inv.get(name.lower())
        have = float(row["quantity"]) if row else 0.0
        if have >= qty:
            covered.append({"item": name, "needed": qty, "have": have})
        else:
            short.append({
                "item": name, "needed": qty, "have": have,
                "missing": round(qty - have, 3),
                "unit": need.get("unit") or (row["unit"] if row else "count"),
            })
    return {"ok": True, "covered": covered, "short": short}


@tool
def restock_suggestions() -> dict:
    """Build a draft restock list from low-stock + soon-to-expire items.

    Returns {"ok": True, "suggestions": [{item, quantity, unit, reason, priority}]}.
    Priority 1 = low stock, 2 = expiring.
    """
    with _db() as conn:
        low = _rows(conn.execute(
            """SELECT item, quantity, unit, low_stock_threshold
               FROM inventory WHERE quantity <= low_stock_threshold
               ORDER BY item"""))
        expiring = _rows(conn.execute(
            """SELECT item, quantity, unit, expiry FROM inventory
               WHERE expiry IS NOT NULL
                 AND expiry <= date('now', '+5 day')
               ORDER BY expiry"""))
    out, seen = [], set()
    for r in low:
        seen.add(r["item"])
        out.append({
            "item": r["item"],
            "quantity": max(r["low_stock_threshold"] * 2, 1),
            "unit": r["unit"],
            "reason": f"low stock ({r['quantity']:g} left, "
                      f"threshold {r['low_stock_threshold']:g})",
            "priority": 1,
        })
    for r in expiring:
        if r["item"] in seen:
            continue
        out.append({
            "item": r["item"], "quantity": r["quantity"],
            "unit": r["unit"], "reason": f"expiring {r['expiry']}",
            "priority": 2,
        })
    return {"ok": True, "suggestions": out}


@tool
def build_list(items: list[dict], source: str = "manual") -> dict:
    """Insert draft rows into shopping_list.

    Args:
        items: List of {
            "item": str,
            "quantity": float,
            "unit": str,
            "priority": int (1–5, default 3),
            "estimated_price": float | None,
            "notes": str | None
        }.
        source: 'missing' (needed for a recipe), 'restock' (fell below
            threshold), or 'manual'.

    Returns {"ok": True, "created_ids": [...], "count": n}.
    """
    if source not in ("missing", "restock", "manual"):
        return {"ok": False, "error": "source must be missing/restock/manual"}
    if not items:
        return {"ok": False, "error": "items list is empty"}

    created = []
    with _db() as conn:
        for it in items:
            cur = conn.execute(
                """INSERT INTO shopping_list
                       (item, quantity, unit, source, status, priority,
                        estimated_price, notes)
                   VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)""",
                (it["item"], float(it.get("quantity", 1)),
                 it.get("unit", "count"), source,
                 int(it.get("priority", 3)),
                 it.get("estimated_price"), it.get("notes")),
            )
            created.append(cur.lastrowid)
            _log(conn, "shopping_list", cur.lastrowid, "insert", "shopping",
                 None, it)
    return {"ok": True, "created_ids": created, "count": len(created)}




# ===========================================================================
# PRESENTER  (LLM)  — reads final result, writes nothing
# ===========================================================================

@tool
def format_voice_summary(meal: dict, profile: dict | None = None) -> dict:
    """Build a short spoken-style summary of a meal.

    Args:
        meal: A meal dict — expects 'name' or 'meal_name', and calories/
            protein either at the top level or under 'per_serving'.
        profile: Optional profile dict with 'daily_calories' for a % line.

    Returns {"ok": True, "text": str, "chars": int}.
    """
    name = meal.get("name") or meal.get("meal_name") or "this meal"
    kcal = meal.get("calories") or meal.get("per_serving", {}).get("calories", 0)
    p = meal.get("protein_g") or meal.get("per_serving", {}).get("protein_g", 0)

    parts = [f"Tonight I'd suggest {name}."]
    if kcal:
        parts.append(f"It's about {int(kcal)} calories")
        parts.append(f"with {int(p)} grams of protein." if p else ".")
    if profile and kcal:
        pct = round(100 * kcal / profile.get("daily_calories", 1))
        parts.append(f"That's roughly {pct} percent of your daily target.")
    text = " ".join(parts)
    return {"ok": True, "text": text, "chars": len(text)}


@tool
def build_card(meal: dict, image_url: str | None = None) -> dict:
    """Build a UI-ready card dict from a meal.

    Args:
        meal: Meal dict with name/meal_type/macros.
        image_url: Optional image URL.

    Returns {"ok": True, "card": {...}}.
    """
    return {"ok": True, "card": {
        "title": meal.get("name") or meal.get("meal_name"),
        "subtitle": meal.get("meal_type"),
        "calories": meal.get("calories") or meal.get("per_serving", {}).get("calories"),
        "protein_g": meal.get("protein_g") or meal.get("per_serving", {}).get("protein_g"),
        "carbs_g": meal.get("carbs_g") or meal.get("per_serving", {}).get("carbs_g"),
        "fat_g": meal.get("fat_g") or meal.get("per_serving", {}).get("fat_g"),
        "image_url": image_url,
        "reason": meal.get("reason"),
    }}


@tool
def image_lookup(query: str) -> dict:
    """Look up a representative image for a dish. STUB — replace with a real API.

    Args:
        query: Dish name (e.g. 'grilled salmon with broccoli').

    Returns {"ok": True, "url": ..., "note": "STUB"}.
    """
    slug = "".join(ch if ch.isalnum() else "-" for ch in query.lower()).strip("-")
    return {"ok": True,
            "url": f"https://images.example.com/meals/{slug}.jpg",
            "note": "STUB"}


# ===========================================================================
# CONTEXT (workflow step) — one tool that writes events_log in kitchen.db
# ===========================================================================

@tool
def log_event(
    entity: str,
    action: str = "insert",
    actor: str = "workflow",
    entity_id: int | None = None,
    before: dict | None = None,
    after: dict | None = None,
) -> dict:
    """Write an arbitrary row to events_log (for things that aren't table writes).

    Args:
        entity: Free-form entity name (e.g. 'orchestrator', 'graph_run').
        action: 'insert', 'update', or 'delete'.
        actor: Who acted — 'agent', 'user', 'system', or a specific agent name.
        entity_id: Optional numeric id.
        before: Optional before-state dict.
        after: Optional after-state dict.

    Returns {"ok": True}.
    """
    with _db() as conn:
        _log(conn, entity, entity_id, action, actor, before, after)
    return {"ok": True}





@tool
def lookup_food(name: str, limit: int = 3) -> dict:
    """Search the real online nutrition database (USDA FoodData Central,
    fallback Open Food Facts). Results are cached locally in kitchen.db.

    Args:
        name: Food name to search (e.g. 'chicken breast', 'greek yogurt').
        limit: Max results.

    Returns {"ok": True, "matches": [{name, source, serving_size, serving_unit,
             calories, protein_g, carbs_g, fat_g, ...}]}.
    """
    return {"ok": True, "matches": nutrition_api.search_food(name, limit=limit)}


@tool
def calc_meal(ingredients: list[dict], servings: float = 1.0) -> dict:
    """Resolve every ingredient against the online nutrition DB and sum macros.

    Args:
        ingredients: List of {"item": str, "quantity": float, "unit": str}
            OR free-text strings like "200g chicken breast".
        servings: How many servings the recipe makes (default 1).

    Returns {"ok": True, "ingredients": [...], "recipe_total": {...},
             "per_serving": {...}, "unmatched": [...], "confidence": 0-1}.
    Reject results whose confidence is low or whose unmatched list is non-empty.
    """
    return nutrition.calc_meal(ingredients=ingredients, servings=servings)


@tool
def check_targets(meal_totals: dict, meal_type: str,
                  tolerance: float = 0.20) -> dict:
    """Compare a meal's per-serving macros against the profile's daily targets.

    Args:
        meal_totals: Dict with at least calories/protein_g/carbs_g/fat_g.
        meal_type: 'breakfast' (25%), 'lunch' (30%), 'dinner' (35%), 'snack' (10%).
        tolerance: Allowed ± fraction around each target (default 0.20 = ±20%).

    Returns {"ok": True, "passes": bool, "checks": [{macro, actual, target, band, pass}]}.
    """
    return nutrition.check_targets(meal_totals, meal_type, tolerance)


@tool
def add_custom_food(name: str, calories: float, protein_g: float,
                    carbs_g: float, fat_g: float,
                    serving_size: float = 100, serving_unit: str = "g",
                    fiber_g: float | None = None, sugar_g: float | None = None,
                    sodium_mg: float | None = None,
                    brand: str | None = None) -> dict:
    """Store a user-defined food in the local cache (source='custom').

    Use for home recipes or anything the online DB doesn't have.

    Returns {"ok": True, "food_id": int}.
    """
    fid = nutrition_api.add_custom_food(
        name=name, calories=calories, protein_g=protein_g, carbs_g=carbs_g,
        fat_g=fat_g, serving_size=serving_size, serving_unit=serving_unit,
        fiber_g=fiber_g, sugar_g=sugar_g, sodium_mg=sodium_mg, brand=brand,
    )
    return {"ok": True, "food_id": fid}


# ===========================================================================
# Ownership map — bind only these lists into each agent
# ===========================================================================

TOOLS_BY_AGENT: dict[str, list] = {
    "orchestrator": [get_context_packet],
    "pantry": [get_inventory, update_item, deduct_ingredients,
               get_low_stock, get_expiring],
    "preference": [get_exclusions, get_preferences, add_exclusion,
                   add_preference, remove_rule],
    "planner": [get_context_packet, get_meal_history],
    "nutrition": [lookup_food, calc_meal, check_targets, add_custom_food],
    "shopping": [diff_needs, restock_suggestions, build_list],
    "presenter": [format_voice_summary, build_card, image_lookup],
    "workflow": [log_event],
}

def tools_for(agent: str) -> list:
    if agent not in TOOLS_BY_AGENT:
        raise KeyError(f"unknown agent '{agent}'. Known: {sorted(TOOLS_BY_AGENT)}")
    return TOOLS_BY_AGENT[agent]