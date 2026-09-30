"""
nutrition.py — accurate meal nutrition. Every ingredient is resolved
against the real nutrition API (nutrition_api.search_food), not a
hardcoded table.
"""
from __future__ import annotations

import re

from agents import nutrition_api

MACROS = ("calories", "protein_g", "carbs_g", "fat_g",
          "fiber_g", "sugar_g", "sodium_mg")

MASS_TO_G = {
    "g": 1.0, "gram": 1.0, "grams": 1.0, "gr": 1.0, "kg": 1000.0,
    "kilogram": 1000.0, "kilograms": 1000.0, "mg": 0.001,
    "oz": 28.3495, "ounce": 28.3495, "ounces": 28.3495,
    "lb": 453.592, "lbs": 453.592, "pound": 453.592, "pounds": 453.592,
}
VOLUME_TO_ML = {
    "ml": 1.0, "milliliter": 1.0, "millilitre": 1.0,
    "l": 1000.0, "liter": 1000.0, "litre": 1000.0,
    "tsp": 4.92892, "teaspoon": 4.92892,
    "tbsp": 14.7868, "tablespoon": 14.7868, "tbs": 14.7868,
    "cup": 236.588, "cups": 236.588,
    "fl_oz": 29.5735, "floz": 29.5735,
    "pint": 473.176, "quart": 946.353, "gallon": 3785.41,
}
COUNT_UNITS = {"count", "each", "piece", "pieces", "whole", "unit", "pc"}
COUNT_WEIGHTS = {
    "egg": 50.0, "banana": 118.0, "lemon": 108.0, "lime": 67.0,
    "tomato": 123.0, "apple": 182.0, "orange": 131.0, "avocado": 150.0,
    "onion": 110.0, "potato": 173.0, "carrot": 61.0,
    "garlic": 3.0, "head of garlic": 45.0,
    "slice of bread": 28.0, "loaf": 400.0, "tortilla": 45.0,
    "can": 400.0, "steak": 225.0, "chicken breast": 174.0, "fillet": 170.0,
}
DENSITY = {
    "oil": 0.92, "olive oil": 0.92, "milk": 1.03, "water": 1.00,
    "broth": 1.00, "stock": 1.00, "honey": 1.42, "syrup": 1.33,
    "flour": 0.53, "sugar": 0.85, "rice": 0.85, "oats": 0.41,
    "butter": 0.91, "yogurt": 1.03, "greek yogurt": 1.03,
    "soy sauce": 1.15, "vinegar": 1.01,
    "almond butter": 1.09, "peanut butter": 1.09,
}

_QTY_UNIT_RE = re.compile(
    r"^\s*(?P<qty>\d+(?:\.\d+)?)\s*(?P<unit>[a-zA-Z_]+)?\s+(?P<name>.+?)\s*$"
)


import json

from services.LLMs import LLM_GPT


# ---------------------------------------------------------------------------
# LLM nutrition estimator — used only when the online DB has no match.
# Results are cached in food_cache with source='llm_estimate' so the LLM is
# asked at most once per unique food name (ever).
# ---------------------------------------------------------------------------

_ESTIMATE_PROMPT = (
    "You are a nutrition database. Estimate the nutrition of a food per 100g "
    "(or per 100ml for liquids). Use typical USDA or traditional-recipe values. "
    "Reply with STRICT JSON only — no prose, no markdown fences:\n"
    '{"name":"<canonical food name>","serving_size":100,"serving_unit":"g",'
    '"calories":<num>,"protein_g":<num>,"carbs_g":<num>,"fat_g":<num>,'
    '"fiber_g":<num or null>,"sugar_g":<num or null>,"sodium_mg":<num or null>,'
    '"confidence":<0.0-1.0>}\n'
    "Set confidence < 0.5 if you are guessing. Never return null for "
    "calories/protein/carbs/fat."
)


def _llm_estimate_food(name: str) -> dict | None:
    """Ask the LLM for per-100g nutrition. Returns a food_cache-shaped dict."""
    try:
        messages = [
            {"role": "system", "content": _ESTIMATE_PROMPT},
            {"role": "user", "content": f"Food: {name}"},
        ]
        if hasattr(LLM_GPT, "invoke"):
            resp = LLM_GPT.invoke(messages)
            raw = getattr(resp, "content", None) or str(resp)
        else:
            raw = LLM_GPT(messages)
            raw = getattr(raw, "content", None) or str(raw)

        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.lower().startswith("json"):
                raw = raw[4:]
        i, j = raw.find("{"), raw.rfind("}")
        if i != -1 and j > i:
            raw = raw[i:j + 1]
        data = json.loads(raw)
    except Exception as e:
        print(f"[nutrition] LLM estimate failed for {name!r}: {e}")
        return None

    # Normalize into a food_cache row
    return {
        "name": (data.get("name") or name).strip().lower(),
        "source": "llm_estimate",
        "source_id": "llm",
        "serving_size": float(data.get("serving_size", 100) or 100),
        "serving_unit": data.get("serving_unit", "g") or "g",
        "calories": float(data.get("calories") or 0),
        "protein_g": float(data.get("protein_g") or 0),
        "carbs_g": float(data.get("carbs_g") or 0),
        "fat_g": float(data.get("fat_g") or 0),
        "fiber_g": data.get("fiber_g"),
        "sugar_g": data.get("sugar_g"),
        "sodium_mg": data.get("sodium_mg"),
        "brand": None,
        "_llm_confidence": float(data.get("confidence") or 0.5),
    }


def parse_ingredient(text: str) -> dict:
    m = _QTY_UNIT_RE.match(text.strip())
    if not m:
        return {"item": text.strip(), "quantity": 1.0, "unit": "count"}
    return {"item": m.group("name").strip(),
            "quantity": float(m.group("qty")),
            "unit": (m.group("unit") or "count").lower()}


def _to_grams(quantity: float, unit: str, food: dict) -> float | None:
    u = (unit or "").lower().strip()
    food_unit = (food.get("serving_unit") or "g").lower()
    name_lc = (food.get("name") or "").lower()

    if u in MASS_TO_G:
        return quantity * MASS_TO_G[u]
    if u in VOLUME_TO_ML and food_unit in VOLUME_TO_ML:
        return quantity * VOLUME_TO_ML[u]
    if u in COUNT_UNITS:
        if food_unit in COUNT_UNITS:
            return quantity
        for key, grams in COUNT_WEIGHTS.items():
            if key in name_lc:
                return quantity * grams
        return None
    if u in VOLUME_TO_ML and food_unit == "g":
        ml = quantity * VOLUME_TO_ML[u]
        for key, dens in DENSITY.items():
            if key in name_lc:
                return ml * dens
        return None
    if u in MASS_TO_G and food_unit in VOLUME_TO_ML:
        g = quantity * MASS_TO_G[u]
        for key, dens in DENSITY.items():
            if key in name_lc and dens > 0:
                return g / dens
        return None
    return None


_LOOKUP_MEMO: dict[str, tuple[dict | None, float]] = {}


def _best_match(name: str) -> tuple[dict | None, float]:
    """
    1. In-memory memo
    2. Online DB (USDA → Open Food Facts, with local cache)
    3. LLM estimate (cached in food_cache for future runs)
    """
    key = name.lower().strip()
    if key in _LOOKUP_MEMO:
        return _LOOKUP_MEMO[key]

    # 1 + 2 — try the real database first
    results = nutrition_api.search_food(name, limit=3)
    if results:
        name_lc = key
        best, conf = results[0], 0.6
        for r in results:
            if r["name"] == name_lc:
                best, conf = r, 1.0
                break
            if name_lc in r["name"] or r["name"] in name_lc:
                best, conf = r, 0.85
                break
        _LOOKUP_MEMO[key] = (best, conf)
        return best, conf

    # 3 — LLM fallback
    print(f"[nutrition] no DB match for {name!r} — asking LLM")
    estimate = _llm_estimate_food(name)
    if estimate:
        # Cache it in kitchen.db so next run is free
        try:
            conn = nutrition_api._conn()
            row = dict(estimate)
            conf = row.pop("_llm_confidence", 0.5)
            row["raw_json"] = json.dumps({"llm": True})
            conn.execute(
                """INSERT OR REPLACE INTO food_cache
                       (name, source, source_id, serving_size, serving_unit,
                        calories, protein_g, carbs_g, fat_g,
                        fiber_g, sugar_g, sodium_mg, brand, raw_json)
                   VALUES (:name, :source, :source_id, :serving_size,
                           :serving_unit, :calories, :protein_g, :carbs_g,
                           :fat_g, :fiber_g, :sugar_g, :sodium_mg,
                           :brand, :raw_json)""",
                row,
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[nutrition] failed to cache LLM estimate: {e}")

        result = {k: v for k, v in estimate.items() if not k.startswith("_")}
        _LOOKUP_MEMO[key] = (result, 0.5)   # mark as estimated, not verified
        return result, 0.5

    _LOOKUP_MEMO[key] = (None, 0.0)
    return None, 0.0

def calc_meal(ingredients: list[dict | str], servings: float = 1.0) -> dict:
    """
    Resolve every ingredient against the online nutrition DB and sum macros.

    Returns: {ok, ingredients, recipe_total, per_serving, servings,
              unmatched, confidence}.
    """
    if servings <= 0:
        return {"ok": False, "error": "servings must be > 0"}

    totals = {k: 0.0 for k in MACROS}
    lines, unmatched, confs = [], [], []

    for raw in ingredients:
        ing = parse_ingredient(raw) if isinstance(raw, str) else dict(raw)
        name = (ing.get("item") or "").strip()
        qty = float(ing.get("quantity", 1.0) or 1.0)
        unit = (ing.get("unit") or "count").lower()
        if not name or qty <= 0:
            unmatched.append({"reason": "empty or non-positive", "input": raw})
            continue

        food, conf = _best_match(name)
        if not food:
            unmatched.append({"reason": "no match from nutrition API",
                              "item": name})
            continue

        grams = _to_grams(qty, unit, food)
        if grams is None:
            unmatched.append({"reason": f"cannot convert {qty}{unit}",
                              "item": name, "matched": food["name"]})
            continue

        factor = grams / float(food["serving_size"] or 100)
        line = {"input_item": name, "matched_food": food["name"],
                "source": food.get("source"), "source_id": food.get("source_id"),
                "quantity": qty, "unit": unit,
                "resolved_grams": round(grams, 2),
                "factor": round(factor, 4), "confidence": conf}
        for k in MACROS:
            v = (food.get(k) or 0) * factor
            line[k] = round(v, 2)
            totals[k] += v
        lines.append(line)
        confs.append(conf)

    recipe_total = {k: round(v, 2) for k, v in totals.items()}
    per_serving = {k: round(v / servings, 2) for k, v in totals.items()}
    confidence = round(sum(confs) / len(confs), 3) if confs else 0.0

    return {"ok": True, "ingredients": lines, "recipe_total": recipe_total,
            "per_serving": per_serving, "servings": servings,
            "unmatched": unmatched, "confidence": confidence}


def check_targets(meal_totals: dict, meal_type: str,
                  tolerance: float = 0.20,
                  calorie_tolerance_kcal: float = 200.0) -> dict:
    """Calories get ±200 kcal band; protein/carbs/fat get ±20% band."""
    import sqlite3
    from tools.db_path import KITCHEN_DB

    splits = {"breakfast": 0.25, "lunch": 0.30, "dinner": 0.35, "snack": 0.10}
    if meal_type not in splits:
        return {"ok": False, "error": "bad meal_type"}
    share = splits[meal_type]

    conn = sqlite3.connect(str(KITCHEN_DB)); conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()
    conn.close()
    if not row:
        return {"ok": False, "error": "no profile row"}

    targets = {
        "calories":  row["daily_calories"] * share,
        "protein_g": row["protein_g"] * share,
        "carbs_g":   row["carbs_g"] * share,
        "fat_g":     row["fat_g"] * share,
    }

    checks, passes = [], True
    for macro, target in targets.items():
        actual = float(meal_totals.get(macro, 0))
        if macro == "calories":
            lo = target - calorie_tolerance_kcal
            hi = target + calorie_tolerance_kcal
        else:
            lo = target * (1 - tolerance)
            hi = target * (1 + tolerance)
        ok = lo <= actual <= hi
        passes = passes and ok
        checks.append({
            "macro": macro, "actual": round(actual, 1),
            "target": round(target, 1),
            "band": [round(lo, 1), round(hi, 1)],
            "pass": ok,
        })
    return {"ok": True, "passes": passes, "checks": checks,
            "meal_type": meal_type, "share_of_day": share}