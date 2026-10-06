"""Linear-programming portion solver with deterministic rounded re-verification."""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy.optimize import linprog

from meal_agent.schemas import IngredientPortion, MacroTotals, NutritionTarget
from meal_agent.tools.nutrition import (
    atwater_consistency,
    atwater_target_consistency,
    verify_macros,
)

MACRO_KEYS = ("kcal", "protein_g", "carbs_g", "fat_g")


def solve_portions(
    items: list[dict[str, Any]],
    target: NutritionTarget,
    *,
    purchase_penalty: float = 0.05,
    weights: dict[str, float] | None = None,
    tolerance_fraction: float = 0.05,
    tolerance_floor: float = 1.0,
) -> dict[str, Any]:
    """Solve portions for candidate ingredients using verified per-100g facts.

    Each candidate needs ``name`` and ``per_100g``; portion bounds, stock status,
    available grams, and unit cost are optional. The caller must attach trusted
    nutrition facts before solving; this function never estimates food facts.
    """
    if not items:
        return {"passed": False, "reason": "No candidate ingredients were supplied."}
    if purchase_penalty < 0:
        raise ValueError("purchase_penalty cannot be negative.")

    validated: list[dict[str, Any]] = []
    for item in items:
        name = str(item.get("name", "")).strip()
        if not name:
            return {"passed": False, "reason": "Candidate ingredient is missing a name."}
        profile = item.get("per_100g")
        if not isinstance(profile, dict):
            return {"passed": False, "reason": f"Trusted nutrition data is missing for {name!r}."}
        try:
            nutrients = {key: float(profile[key]) for key in MACRO_KEYS}
            min_g = float(item.get("min_g", 0))
            max_g = float(item.get("max_g", item.get("available_g", 500)))
            if item.get("in_stock", True):
                max_g = min(max_g, float(item.get("available_g", max_g)))
            cost = float(item.get("cost_per_g", 0.0))
        except (KeyError, TypeError, ValueError) as exc:
            return {"passed": False, "reason": f"Invalid nutrient or portion bound for {name!r}: {exc}"}
        if min_g < 0 or max_g < min_g or cost < 0 or any(value < 0 for value in nutrients.values()):
            return {"passed": False, "reason": f"Invalid non-negative portion or nutrition bounds for {name!r}."}
        validated.append(
            {
                **item,
                "name": name,
                "per_100g": nutrients,
                "min_g": min_g,
                "max_g": max_g,
                "cost_per_g": cost,
            }
        )

        GOAL_WEIGHT = 0.05   # pull toward the preferred value; far weaker than a constraint violation

    constraints: list[tuple[str, str, float]] = []   # (macro, "min"/"max", threshold), slack-penalized
    goals: list[tuple[str, float]] = []              # (macro, preferred value), soft pull
    for key in MACRO_KEYS:
        exact = getattr(target, key)
        minimum = getattr(target, f"{key}_min")
        maximum = getattr(target, f"{key}_max")
        low = float(minimum) if minimum is not None else None
        high = float(maximum) if maximum is not None else None
        if low is not None or high is not None:
            # Ranges are authoritative. A small inner margin keeps integer rounding inside the range.
            margin = max(0.01 * (high if high is not None else low), 0.5)
            if low is not None and high is not None:
                margin = min(margin, (high - low) / 4)
            if low is not None:
                constraints.append((key, "min", low + margin))
            if high is not None:
                constraints.append((key, "max", max(0.0, high - margin)))
            if exact is not None:
                goals.append((key, float(exact)))
            elif low is not None and high is not None:
                goals.append((key, (low + high) / 2))
        elif exact is not None:
            tolerance = max(float(exact) * tolerance_fraction, tolerance_floor)
            constraints.extend(
                [(key, "min", max(0.0, float(exact) - tolerance)), (key, "max", float(exact) + tolerance)]
            )
            goals.append((key, float(exact)))        # aim at the value, not the tolerance edge

    item_count = len(validated)
    slack_count = len(constraints)
    goal_count = len(goals)
    goal_base = item_count + slack_count
    n_vars = goal_base + 2 * goal_count              # [portions | slack | goal_over | goal_under]

    objective = np.zeros(n_vars)
    for index, item in enumerate(validated):
        objective[index] = purchase_penalty * item["cost_per_g"] if not item.get("in_stock", True) else 0.0
        objective[index] += 1e-7
    for index, (macro, _direction, threshold) in enumerate(constraints):
        weight = float((weights or {}).get(macro, 1.0))
        if weight <= 0:
            raise ValueError(f"Weight for {macro} must be positive.")
        objective[item_count + index] = weight / max(abs(threshold), tolerance_floor)
    for index, (macro, goal) in enumerate(goals):
        weight = float((weights or {}).get(macro, 1.0))
        scale = GOAL_WEIGHT * weight / max(abs(goal), tolerance_floor)
        objective[goal_base + index] = scale                    # over the goal
        objective[goal_base + goal_count + index] = scale       # under the goal

    a_ub = np.zeros((slack_count, n_vars))
    b_ub = np.zeros(slack_count)
    for row, (macro, direction, threshold) in enumerate(constraints):
        coefficients = np.asarray([item["per_100g"][macro] / 100.0 for item in validated])
        if direction == "min":
            a_ub[row, :item_count] = -coefficients
            a_ub[row, item_count + row] = -1.0
            b_ub[row] = -threshold
        else:
            a_ub[row, :item_count] = coefficients
            a_ub[row, item_count + row] = -1.0
            b_ub[row] = threshold

    a_eq = np.zeros((goal_count, n_vars))
    b_eq = np.zeros(goal_count)
    for row, (macro, goal) in enumerate(goals):
        a_eq[row, :item_count] = [item["per_100g"][macro] / 100.0 for item in validated]
        a_eq[row, goal_base + row] = -1.0                       # - over
        a_eq[row, goal_base + goal_count + row] = 1.0           # + under
        b_eq[row] = goal

    bounds = [(item["min_g"], item["max_g"]) for item in validated] + [(0, None)] * (slack_count + 2 * goal_count)
    result = linprog(
        objective,
        A_ub=a_ub if slack_count else None,
        b_ub=b_ub if slack_count else None,
        A_eq=a_eq if goal_count else None,
        b_eq=b_eq if goal_count else None,
        bounds=bounds,
        method="highs",
    )
    if not result.success or result.x is None:
        return {"passed": False, "reason": f"Portion optimization failed: {result.message}"}

    portions: list[IngredientPortion] = []
    for index, item in enumerate(validated):
        grams = round(float(result.x[index]))
        if grams <= 0:
            continue
        portions.append(
            IngredientPortion(
                name=item["name"],
                grams=grams,
                per_100g=MacroTotals(**item["per_100g"]),
            )
        )

    verification = verify_macros(
        portions,
        target,
        tolerance_fraction=tolerance_fraction,
        tolerance_floor=tolerance_floor,
    )
    slack: dict[str, dict[str, float]] = {}
    for index, (macro, direction, _threshold) in enumerate(constraints):
        amount = float(result.x[item_count + index])
        if amount > 1e-9:
            slack.setdefault(macro, {})["under" if direction == "min" else "over"] = amount
    totals = verification.totals.model_dump(mode="json")
    target_atwater = atwater_target_consistency(target)
    warnings = list(verification.warnings)
    if target_atwater and not target_atwater["within_tolerance"]:
        warnings.append(
            "The exact calorie target is inconsistent with the exact protein/carbohydrate/fat targets under 4/4/9 Atwater factors."
        )
    return {
        "passed": verification.passed,
        "ingredients": [portion.model_dump(mode="json") for portion in portions],
        "totals": totals,
        "deviation": verification.deviation,
        "slack": slack,
        "violations": verification.violations,
        "suggestions": verification.suggestions,
        "warnings": warnings,
        "atwater": atwater_consistency(verification.totals),
        "target_atwater": target_atwater,
        "reason": None if verification.passed else "Rounded portions do not satisfy every target constraint.",
        "objective": float(result.fun),
    }
