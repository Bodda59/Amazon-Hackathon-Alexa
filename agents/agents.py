"""
agents.py — the seven kitchen agents (no purchasing).

Every agent now returns a `details` payload in its events so run.py can show
exactly what it received, what it did, and what it produced.
"""

from __future__ import annotations

import json
import re
from typing import Any

from services.LLMs import LLM_GPT

from tools.kitchen_tools import (
    get_inventory, update_item, deduct_ingredients, get_low_stock, get_expiring,
    get_exclusions, get_preferences, add_exclusion, add_preference, remove_rule,
    get_context_packet, get_meal_history,
    lookup_food, calc_meal, check_targets, add_custom_food,
    diff_needs, restock_suggestions, build_list,
    format_voice_summary, build_card, image_lookup,
    log_event,
)

# ==========================================================================
# Constants
# ==========================================================================

MAX_PLANNING_ATTEMPTS = 3

# ==========================================================================
# Helpers
# ==========================================================================

def _call(tool, **kwargs):
    return tool.invoke(kwargs)


def _truncate(obj, limit: int = 2000) -> Any:
    """Trim huge payloads so trace output stays readable."""
    s = json.dumps(obj, default=str)
    if len(s) <= limit:
        return obj
    return {"__truncated__": True, "preview": s[:limit] + "..."}

# ==========================================================================
# LLM plumbing
# ==========================================================================

def _chat(messages: list[dict]) -> str:
    llm = LLM_GPT
    if hasattr(llm, "invoke"):
        resp = llm.invoke(messages)
        return getattr(resp, "content", None) or str(resp)
    if callable(llm):
        resp = llm(messages)
        return getattr(resp, "content", None) or str(resp)
    raise RuntimeError("LLM_GPT has no .invoke() and isn't callable")


def _json_from_llm(system: str, user: str) -> tuple[Any, str]:
    """Returns (parsed_json, raw_text)."""
    raw = _chat([{"role": "system", "content": system},
                 {"role": "user", "content": user}]).strip()
    cleaned = raw
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    for o, c in (("{", "}"), ("[", "]")):
        i, j = cleaned.find(o), cleaned.rfind(c)
        if i != -1 and j > i:
            cleaned = cleaned[i:j + 1]
            break
    return json.loads(cleaned), raw

# ==========================================================================
# 1. ORCHESTRATOR (LLM)
# ==========================================================================

def orchestrator(state: dict) -> dict:
    packet = _call(get_context_packet, include_history=10)
    summary = {
        "profile": packet["profile"],
        "inventory_count": len(packet["inventory"]),
        "exclusions": [e["item"] for e in packet["exclusions"]],
        "top_likes": [p["item"] for p in packet["preferences"]
                      if p["sentiment"] == "like"][:5],
    }
    system = (
        "You are the Orchestrator of a kitchen planning team. Parse the "
        "user's request into a structured plan. Return JSON: "
        '{"meal_type":"breakfast|lunch|dinner|snack", '
        '"calorie_target": number|null, '
        '"steps":["pantry","preference","planner","nutrition"], '
        '"notes":"short reason"}. '
        "Always include pantry, preference, planner, nutrition. "
        "Include shopping only if the user wants to buy things."
    )
    user = (f"User request: {state['user_request']}\n"
            f"Context summary: {json.dumps(summary, default=str)}")

    raw_response = ""
    try:
        plan, raw_response = _json_from_llm(system, user)
    except Exception as e:
        plan = {"meal_type": "dinner", "calorie_target": None,
                "steps": ["pantry", "preference", "planner", "nutrition"],
                "notes": f"fallback after LLM error: {e}"}
        raw_response = f"<error: {e}>"

    _call(log_event,
          entity="orchestrator", action="insert", actor="orchestrator",
          after={"request": state["user_request"], "plan": plan})

    details = {
        "inputs": {
            "user_request": state["user_request"],
            "context_summary": _truncate(summary, 800),
        },
        "llm": {
            "system_prompt": system,
            "user_prompt": _truncate(user, 2000),
            "raw_response": raw_response,
        },
        "output": {"plan": plan},
    }

    return {
        "plan": plan,
        "meal_type": plan.get("meal_type", "dinner"),
        "calorie_target": plan.get("calorie_target"),
        "context_packet": packet,
        "planning_attempts": 0,
        "previous_rejections": [],
        "events": [{"agent": "orchestrator",
                    "summary": f"planned {plan.get('meal_type')} at "
                               f"{plan.get('calorie_target')} kcal, "
                               f"steps={plan.get('steps')}",
                    "details": details}],
    }

# ==========================================================================
# 2. PANTRY (Code)
# ==========================================================================

def pantry(state: dict) -> dict:
    low = _call(get_low_stock)
    expiring = _call(get_expiring, within_days=7)

    deduction_result = None
    chosen = state.get("chosen_meal")
    if chosen and chosen.get("ingredients"):
        deduction_result = _call(deduct_ingredients,
                                 ingredients=chosen["ingredients"])

    details = {
        "inputs": {"inventory_item_count": None},
        "tool_calls": [
            {"tool": "get_low_stock", "result_count": len(low["items"])},
            {"tool": "get_expiring", "within_days": 7,
             "result_count": len(expiring["items"])},
        ],
        "low_stock": low["items"],
        "expiring": expiring["items"],
        "deduction": deduction_result,
    }
    updates = {
        "pantry_status": {"low_stock": low["items"],
                          "expiring": expiring["items"]},
        "events": [{"agent": "pantry",
                    "summary": f"{len(low['items'])} low, "
                               f"{len(expiring['items'])} expiring",
                    "details": details}],
    }
    if deduction_result is not None:
        updates["deduction_result"] = deduction_result
    return updates

# ==========================================================================
# 3. PREFERENCE FILTER (Code)
# ==========================================================================

def preference_filter(state: dict) -> dict:
    # existing code unchanged
    exclusions_before = _call(get_exclusions)["exclusions"]
    preferences_before = _call(get_preferences)["preferences"]

    exclusions = exclusions_before
    preferences = preferences_before
    req = state["user_request"].lower()
    applied: list[dict] = []

    for pattern in (
        r"\bno\s+([a-z ]+?)(?:[,.]|$)",
        r"allergic to\s+([a-z ]+?)(?:[,.]|$)",
        r"can'?t eat\s+([a-z ]+?)(?:[,.]|$)",
    ):
        m = re.search(pattern, req)
        if m:
            item = m.group(1).strip()
            if item and item not in {e["item"] for e in exclusions}:
                r = _call(add_exclusion, item=item,
                          reason="stated in user request")
                applied.append({"kind": "add_exclusion", "item": item,
                                "pattern_matched": pattern, "result": r})
                exclusions = _call(get_exclusions)["exclusions"]

    for pattern, sentiment in (
        (r"i love\s+([a-z ]+?)(?:[,.]|$)", "like"),
        (r"i like\s+([a-z ]+?)(?:[,.]|$)", "like"),
        (r"i hate\s+([a-z ]+?)(?:[,.]|$)", "dislike"),
        (r"i dislike\s+([a-z ]+?)(?:[,.]|$)", "dislike"),
    ):
        m = re.search(pattern, req)
        if m:
            item = m.group(1).strip()
            if item:
                r = _call(add_preference, item=item, sentiment=sentiment,
                          weight=7.0, notes="stated in user request")
                applied.append({"kind": "add_preference", "item": item,
                                "sentiment": sentiment,
                                "pattern_matched": pattern, "result": r})
                preferences = _call(get_preferences)["preferences"]

    details = {
        "inputs": {"user_request_lowercased": req},
        "exclusions_before": exclusions_before,
        "exclusions_after": exclusions,
        "preferences_before": preferences_before,
        "preferences_after": preferences,
        "rule_changes_applied": applied,
    }

    return {
        "active_exclusions": exclusions,
        "active_preferences": preferences,
        "applied_rule_changes": applied,
        "events": [{"agent": "preference_filter",
                    "summary": f"{len(exclusions)} exclusions, "
                               f"{len(preferences)} preferences, "
                               f"{len(applied)} rule changes",
                    "details": details}],
    }

# ==========================================================================
# 3.5 MACRO EXTRACTOR (Code)
# ==========================================================================
def macro_extractor(state: dict) -> dict:
    """Extract calorie and macro constraints from the user's request.
    Returns a dict with optional keys: calorie_target, protein_g, carbs_g, fat_g.
    Also records an event for traceability.
    """
    req = state.get("user_request", "").lower()
    macro = {}
    # Calories
    cal_match = re.search(r"(\d+)\s*(kcal|calories?)", req)
    if cal_match:
        macro["calorie_target"] = int(cal_match.group(1))
    # Protein
    prot_match = re.search(r"(\d+)\s*(g|grams?)?\s*(?:of\s+)?protein", req)
    if prot_match:
        macro["protein_g"] = int(prot_match.group(1))
    # Carbs
    carb_match = re.search(r"(\d+)\s*(g|grams?)?\s*(?:of\s+)?carb", req)
    if carb_match:
        macro["carbs_g"] = int(carb_match.group(1))
    # Fat
    fat_match = re.search(r"(\d+)\s*(g|grams?)?\s*(?:of\s+)?fat", req)
    if fat_match:
        macro["fat_g"] = int(fat_match.group(1))
    # Store in state for downstream agents
    if macro:
        state["macro_constraints"] = macro
    # Log event
    _call(log_event, entity="macro_extractor", action="insert", actor="macro_extractor", after=macro)
    return {"macro_constraints": macro, "events": [{"agent": "macro_extractor", "summary": f"extracted {macro}", "details": {"macro": macro}}]}

# ==========================================================================
# 3.6 MACRO FEASIBILITY (Code)
# ==========================================================================
def macro_feasibility(state: dict) -> dict:
    """Validate that the requested macros and calories are feasible with inventory.
    Returns a dict with a boolean ``feasible`` and a ``reason``.
    """
    macro = state.get("macro_constraints", {})
    # If no explicit macros, nothing to validate
    if not macro:
        return {"feasible": True, "events": []}
    # Determine if macro grams (protein, carbs, fat) are provided
    macro_keys = {"protein_g", "carbs_g", "fat_g"}
    has_macro_grams = any(k in macro for k in macro_keys)
    protein = macro.get("protein_g", 0)
    carbs = macro.get("carbs_g", 0)
    fat = macro.get("fat_g", 0)
    macro_cal = protein * 4 + carbs * 4 + fat * 9
    target_cal = macro.get("calorie_target")
    # Tolerance of ±100 calories
    cal_tolerance = 100
    # If macro grams are provided and a calorie target, verify calories match; otherwise skip calorie check
    if has_macro_grams and target_cal is not None:
        cal_ok = abs(target_cal - macro_cal) <= cal_tolerance
    else:
        cal_ok = True
    feasible = cal_ok
    reason = "" if cal_ok else f"macro calories {macro_cal} differ from target {target_cal} by >{cal_tolerance}"
    # Store feasibility result for later use
    state["macro_feasibility"] = {"feasible": feasible, "reason": reason}
    _call(log_event, entity="macro_feasibility", action="insert", actor="macro_feasibility", after={"feasible": feasible, "reason": reason})
    return {"feasible": feasible, "reason": reason, "events": [{"agent": "macro_feasibility", "summary": f"feasible={feasible}", "details": {"macro": macro, "target_cal": target_cal, "macro_cal": macro_cal, "reason": reason}}]}

# ==========================================================================
# 4. PLANNER (LLM) — retries with feedback from previous rejections
# ==========================================================================

def planner(state: dict) -> dict:
    packet = state.get("context_packet") or _call(get_context_packet)
    exclusions = [e["item"] for e in state.get("active_exclusions", packet["exclusions"])]
    prefs = state.get("active_preferences", packet["preferences"])
    likes = [p["item"] for p in prefs if p["sentiment"] == "like"]
    dislikes = [p["item"] for p in prefs if p["sentiment"] == "dislike"]

    # ---- retry state ----
    attempt = int(state.get("planning_attempts", 0)) + 1
    previous = state.get("previous_rejections") or []

    inventory = [{"item": r["item"], "quantity": r["quantity"],
                  "unit": r["unit"], "expiry": r["expiry"]}
                 for r in packet["inventory"] if r["quantity"] > 0]
    recent = [{"meal_name": m["meal_name"], "status": m["status"],
               "reason": m["reason"]} for m in packet["meal_history"][:10]]

    # Build system prompt dynamically based on macro constraints
    macro = state.get("macro_constraints", {})
    base_prompt = (
        "You are the Planner. Invent 2-3 meals using primarily the inventory "
        "given. HARD RULES: never use an exclusion. Prefer likes, avoid "
        "dislikes when alternatives exist. Prioritise items that expire soon."
    )
    if macro:
        # User supplied explicit macro / calorie constraints
        parts = []
        if macro.get("calorie_target") is not None:
            parts.append(f"Target total calories: {macro['calorie_target']} kcal.")
        if macro.get("protein_g") is not None:
            parts.append(f"Target protein: {macro['protein_g']} g.")
        if macro.get("carbs_g") is not None:
            parts.append(f"Target carbs: {macro['carbs_g']} g.")
        if macro.get("fat_g") is not None:
            parts.append(f"Target fat: {macro['fat_g']} g.")
        macro_prompt = " ".join(parts)
        # Allow ±100 calories tolerance, don't enforce macro split percentages
        base_prompt += f" {macro_prompt} You may be up to ±100 kcal from the target."
    else:
        # Default behavior when no explicit macros are given
        base_prompt += " Aim for the calorie_target ±200 kcal and get protein/carbs/fat within ±20% of a standard dinner split."
    system = base_prompt + ' Return JSON: {"meals":[{"name":str,"meal_type":str,"ingredients":[{"item":str,"quantity":num,"unit":str}],"servings":num,"why":str}]}'


    retry_block = ""
    if previous:
        bullet_lines = "\n".join(
            f"  - {p.get('meal', '?')}: {p.get('reason', '')}"
            for p in previous
        )
        retry_block = (
            f"\n\n=== RETRY ATTEMPT {attempt} of {MAX_PLANNING_ATTEMPTS} ===\n"
            "Your previous meals were ALL REJECTED by the nutrition critic:\n"
            f"{bullet_lines}\n\n"
            "You MUST produce 2-3 COMPLETELY NEW meals that fix these issues. "
            "Rules for this retry:\n"
            "  · Do NOT repeat the same meal names or ingredient combinations.\n"
            "  · If calories were too low, INCREASE portions or add a "
            "calorie-dense item (oil, cheese, rice, nut butter).\n"
            "  · If calories were too high, DECREASE portions.\n"
            "  · If fat was too high, CUT oil/cheese/butter/nuts.\n"
            "  · If carbs were too high, reduce rice/bread/oats/banana.\n"
            "  · If protein was too low, INCREASE meat/fish/greek yogurt/beans.\n"
            "  · If protein was too high, reduce portions of protein sources.\n"
            "  · Only use ingredients already in the inventory list.\n"
            f"  · Target: {state.get('calorie_target') or 'no specific target'} kcal.\n"
        )

    user = json.dumps({
        "meal_type": state.get("meal_type", "dinner"),
        "calorie_target": state.get("calorie_target"),
        "inventory": inventory, "exclusions": exclusions,
        "likes": likes, "dislikes": dislikes, "recent_meals": recent,
        "attempt": attempt,
    }, default=str) + retry_block

    raw_response = ""
    try:
        data, raw_response = _json_from_llm(system, user)
        meals_raw = data.get("meals", [])
    except Exception as e:
        meals_raw = []
        raw_response = f"<error: {e}>"
        _call(log_event, entity="planner", action="insert",
              actor="planner", after={"error": str(e)})

    # Scrub excluded items that slipped through
    blocked = {x.lower() for x in exclusions}
    safe, scrubbed = [], []
    for meal in meals_raw:
        ings = []
        removed = []
        for i in meal.get("ingredients", []):
            name = i.get("item", "").lower()
            if any(b in name for b in blocked):
                removed.append(i)
            else:
                ings.append(i)
        if removed:
            scrubbed.append({"meal": meal.get("name"), "removed": removed})
        if ings:
            meal["ingredients"] = ings
            safe.append(meal)

    details = {
        "inputs": {
            "attempt": attempt,
            "meal_type": state.get("meal_type"),
            "calorie_target": state.get("calorie_target"),
            "inventory_count": len(inventory),
            "exclusions": exclusions,
            "likes": likes,
            "dislikes": dislikes,
            "recent_meals_count": len(recent),
            "previous_rejections_count": len(previous),
            "previous_rejections": previous,
        },
        "llm": {
            "system_prompt": system,
            "user_prompt": _truncate(user, 4000),
            "raw_response": raw_response,
        },
        "proposed_raw": meals_raw,
        "scrubbed_by_safety_net": scrubbed,
        "proposed_safe": safe,
    }

    summary = (f"attempt {attempt}/{MAX_PLANNING_ATTEMPTS}: "
               f"{len(meals_raw)} proposed, {len(safe)} kept")
    if previous:
        summary = f"[RETRY] " + summary

    return {
        "proposed_meals": safe,
        "planning_attempts": attempt,
        "events": [{"agent": "planner",
                    "summary": summary,
                    "details": details}],
    }

# ==========================================================================
# 5. NUTRITION CRITIC (Code)
# ==========================================================================

def nutrition_critic(state: dict) -> dict:
    meal_type = state.get("meal_type", "dinner")
    proposals = state.get("proposed_meals", [])
    evaluated = []
    traces = []

    # Build inventory map from context packet for validation
    inventory = {i["item"].lower(): i for i in state.get("context_packet", {}).get("inventory", [])}

    for meal in proposals:
        meal_name = meal.get("name", "(unnamed)")

        calc_input = {
            "ingredients": meal.get("ingredients", []),
            "servings": float(meal.get("servings", 1) or 1),
        }
        calc = _call(calc_meal, **calc_input)

        trace = {
            "meal": meal_name,
            "input": calc_input,
            "calc_ok": calc.get("ok"),
        }

        if not calc.get("ok"):
            trace["verdict"] = "reject"
            trace["reason"] = calc.get("error")
            traces.append(trace)
            evaluated.append({**meal, "verdict": "reject",
                              "reason": calc.get("error")})
            continue

        trace["resolved_ingredients"] = [
            {
                "input": line["input_item"],
                "matched": line["matched_food"],
                "source": line.get("source"),
                "qty": f"{line['quantity']}{line['unit']}",
                "grams": line["resolved_grams"],
                "factor": line["factor"],
                "kcal": line["calories"],
                "P": line["protein_g"],
                "C": line["carbs_g"],
                "F": line["fat_g"],
                "confidence": line["confidence"],
            }
            for line in calc["ingredients"]
        ]
        trace["unmatched"] = calc["unmatched"]
        trace["recipe_total"] = calc["recipe_total"]
        trace["servings"] = calc["servings"]
        trace["per_serving"] = calc["per_serving"]
        trace["avg_confidence"] = calc["confidence"]

        # Inventory validation: ensure each ingredient exists in inventory
        missing_in_inventory = []
        for ing in meal.get("ingredients", []):
            name_lc = ing.get("item", "").lower()
            inv = inventory.get(name_lc)
            if not inv or float(inv.get("quantity", 0)) <= 0:
                missing_in_inventory.append(name_lc)
        if missing_in_inventory:
            trace["inventory_missing"] = missing_in_inventory

        # Check nutritional targets based on user-defined macros (if any) or profile defaults
        macro_constraints = state.get("macro_constraints")
        if macro_constraints:
            # Build target based on user‑provided macro / calorie constraints
            target = {"passes": True, "checks": []}
            # Calorie target check (±100 kcal tolerance)
            target_cal = macro_constraints.get("calorie_target")
            if target_cal is not None:
                actual_cal = calc["per_serving"].get("calories")
                cal_ok = abs(actual_cal - target_cal) <= 100
                target["passes"] = cal_ok
                target["checks"].append({
                    "macro": "calories",
                    "actual": actual_cal,
                    "target": target_cal,
                    "band": [target_cal - 100, target_cal + 100],
                    "pass": cal_ok,
                })
            # Protein target check (±5g tolerance)
            protein_target = macro_constraints.get("protein_g")
            if protein_target is not None:
                actual_protein = calc["per_serving"].get("protein_g")
                protein_ok = abs(actual_protein - protein_target) <= 5
                target["passes"] = target["passes"] and protein_ok
                target["checks"].append({
                    "macro": "protein_g",
                    "actual": actual_protein,
                    "target": protein_target,
                    "band": [protein_target - 5, protein_target + 5],
                    "pass": protein_ok,
                })
            # Carbs target check (±5g tolerance)
            carbs_target = macro_constraints.get("carbs_g")
            if carbs_target is not None:
                actual_carbs = calc["per_serving"].get("carbs_g")
                carbs_ok = abs(actual_carbs - carbs_target) <= 5
                target["passes"] = target["passes"] and carbs_ok
                target["checks"].append({
                    "macro": "carbs_g",
                    "actual": actual_carbs,
                    "target": carbs_target,
                    "band": [carbs_target - 5, carbs_target + 5],
                    "pass": carbs_ok,
                })
            # Fat target check (±5g tolerance)
            fat_target = macro_constraints.get("fat_g")
            if fat_target is not None:
                actual_fat = calc["per_serving"].get("fat_g")
                fat_ok = abs(actual_fat - fat_target) <= 5
                target["passes"] = target["passes"] and fat_ok
                target["checks"].append({
                    "macro": "fat_g",
                    "actual": actual_fat,
                    "target": fat_target,
                    "band": [fat_target - 5, fat_target + 5],
                    "pass": fat_ok,
                })
            # If macro grams were provided, macro_feasibility already handled them
        else:
            target = _call(check_targets,
                           meal_totals=calc["per_serving"], meal_type=meal_type)
        trace["target_check"] = {
            "meal_type": meal_type,
            "share_of_day": target.get("share_of_day"),
            "checks": target.get("checks", []),
            "passes": target.get("passes"),
        }

        # Macro feasibility result (if validated earlier)
        macro_feas = state.get("macro_feasibility", {"feasible": True})
        # Relaxed rejection: allow up to 2 unmatched (salt/pepper)
        low_conf = calc["confidence"] < 0.35
        too_many_misses = len(calc["unmatched"]) > 2
        verdict = "accept" if (
            target["passes"]
            and macro_feas.get("feasible", True)
            and not low_conf
            and not too_many_misses
            and not missing_in_inventory
        ) else "reject"


        reasons = []
        if not target["passes"]:
            reasons.append("missed target: " + ", ".join(
                f"{c['macro']}={c['actual']}(band {c['band']})"
                for c in target["checks"] if not c["pass"]))
        if calc["unmatched"]:
            reasons.append(f"unresolved: "
                           f"{[u.get('item', u.get('input')) for u in calc['unmatched']]}")
        if calc["confidence"] < 0.35:
            reasons.append(f"low confidence ({calc['confidence']})")
        if missing_in_inventory:
            reasons.append(f"missing from inventory: {', '.join(missing_in_inventory)}")

        reason_text = "; ".join(reasons) if reasons else "meets targets"
        trace["verdict"] = verdict
        trace["reason"] = reason_text
        traces.append(trace)

        evaluated.append({
            **meal,
            "nutrition": calc["per_serving"],
            "recipe_total": calc["recipe_total"],
            "target_check": target,
            "confidence": calc["confidence"],
            "verdict": verdict,
            "reason": reason_text,
        })

    accepted = [m for m in evaluated if m["verdict"] == "accept"]
    chosen = max(accepted, key=lambda m: m["nutrition"].get("protein_g", 0)) \
             if accepted else None

    # Build feedback for planner retry
    if not chosen:
        previous_rejections = [
            {"meal": m.get("name", "?"), "reason": m.get("reason", "")}
            for m in evaluated
        ]
    else:
        previous_rejections = []

    details = {
        "meal_type": meal_type,
        "traces": traces,
        "summary": {
            "evaluated": len(evaluated),
            "accepted": len(accepted),
            "rejected": len(evaluated) - len(accepted),
            "chosen": chosen["name"] if chosen else None,
        },
    }

    return {
        "evaluated_meals": evaluated,
        "chosen_meal": chosen,
        "previous_rejections": previous_rejections,
        "events": [{"agent": "nutrition_critic",
                    "summary": f"{len(evaluated)} evaluated, "
                               f"{len(accepted)} accepted, "
                               f"chosen={chosen['name'] if chosen else 'none'}",
                    "details": details}],
    }

# ==========================================================================
# 6. SHOPPING (Code)
# ==========================================================================

def shopping(state: dict) -> dict:
    chosen = state.get("chosen_meal")
    if not chosen:
        return {"events": [{"agent": "shopping",
                            "summary": "no chosen meal, skipped",
                            "details": {"reason": "chosen_meal is None"}}]}

    needs = chosen.get("ingredients", [])
    diff = _call(diff_needs, needs=needs)
    restock = _call(restock_suggestions)["suggestions"]

    to_buy = []
    for s in diff["short"]:
        to_buy.append({"item": s["item"], "quantity": s["missing"],
                       "unit": s["unit"], "priority": 2,
                       "notes": f"needed for {chosen['name']}"})
    existing = {x["item"].lower() for x in to_buy}
    for r in restock:
        if r["item"].lower() not in existing:
            to_buy.append({"item": r["item"], "quantity": r["quantity"],
                           "unit": r["unit"], "priority": r["priority"],
                           "notes": r["reason"]})

    build = (_call(build_list, items=to_buy, source="missing")
             if to_buy else {"created_ids": []})

    details = {
        "chosen_meal": chosen["name"],
        "needs": needs,
        "diff_covered": diff["covered"],
        "diff_short": diff["short"],
        "restock_suggestions": restock,
        "final_shopping_list": to_buy,
        "created_ids": build.get("created_ids", []),
    }

    return {
        "needs": needs,
        "covered": diff["covered"],
        "short": diff["short"],
        "restock": restock,
        "shopping_list": to_buy,
        "shopping_list_ids": build.get("created_ids", []),
        "events": [{"agent": "shopping",
                    "summary": f"{len(diff['short'])} short, "
                               f"{len(restock)} restock → "
                               f"{len(to_buy)} items on list",
                    "details": details}],
    }

# ==========================================================================
# 7. PRESENTER (LLM)
# ==========================================================================

def presenter(state: dict) -> dict:
    chosen = state.get("chosen_meal")
    attempts = int(state.get("planning_attempts", 0))

    if not chosen:
        # Give-up path
        if attempts >= MAX_PLANNING_ATTEMPTS:
            msg = (
                f"Based on your current inventory and the constraints you've "
                f"set, I couldn't find a meal that fits after "
                f"{attempts} attempts. You may need to restock some items, "
                f"relax the calorie target, or loosen a preference."
            )
        else:
            msg = "I couldn't find a meal that fits your rules today."

        return {
            "voice_summary": msg,
            "card": None,
            "events": [{"agent": "presenter",
                        "summary": f"gave up after {attempts} attempts",
                        "details": {
                            "reason": "no chosen meal",
                            "attempts": attempts,
                            "max_attempts": MAX_PLANNING_ATTEMPTS,
                            "message": msg,
                        }}],
        }

    voice = _call(format_voice_summary, meal=chosen,
                  profile=state.get("context_packet", {}).get("profile"))
    image = _call(image_lookup, query=chosen["name"])

    system = (
        "You are the Presenter. Write 2-3 short spoken sentences about the "
        "meal. Confirm it respects hard exclusions and highlights why it fits "
        "today (expiring items, macro fit). Do not invent numbers — use only "
        "what's provided."
    )
    user = json.dumps({
        "meal": chosen,
        "exclusions": [e["item"] for e in state.get("active_exclusions", [])],
        "profile": state.get("context_packet", {}).get("profile"),
    }, default=str)

    raw_response = ""
    try:
        spoken = _chat([{"role": "system", "content": system},
                        {"role": "user", "content": user}]).strip()
        raw_response = spoken
    except Exception as e:
        spoken = voice["text"]
        raw_response = f"<error: {e}>"

    card = _call(build_card, meal=chosen, image_url=image["url"])["card"]

    details = {
        "chosen_meal": {
            "name": chosen["name"],
            "nutrition": chosen.get("nutrition"),
            "confidence": chosen.get("confidence"),
            "reason": chosen.get("reason"),
        },
        "attempts_used": attempts,
        "deterministic_voice": voice["text"],
        "llm": {
            "system_prompt": system,
            "user_prompt": _truncate(user, 2000),
            "raw_response": raw_response,
        },
        "image": image,
        "final_card": card,
    }

    return {
        "voice_summary": spoken,
        "card": card,
        "events": [{"agent": "presenter",
                    "summary": f"wrote {len(spoken)} chars after "
                               f"{attempts} attempt(s), "
                               f"card title='{card.get('title')}'",
                    "details": details}],
    }
