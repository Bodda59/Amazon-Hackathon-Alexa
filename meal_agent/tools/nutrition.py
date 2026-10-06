"""Deterministic nutrition calculations; facts must come from trusted data."""

from __future__ import annotations

from meal_agent.schemas import IngredientPortion, MacroTotals, NutritionTarget, VerificationResult

MACRO_KEYS = ("kcal", "protein_g", "carbs_g", "fat_g")


def atwater_consistency(totals: MacroTotals) -> dict[str, float | bool]:
    """Compare labeled/database kcal with 4/4/9 Atwater energy as a warning signal."""
    estimated = 4 * totals.protein_g + 4 * totals.carbs_g + 9 * totals.fat_g
    difference = totals.kcal - estimated
    tolerance = max(estimated * 0.2, 50.0)
    return {
        "estimated_kcal": estimated,
        "difference_kcal": difference,
        "within_tolerance": abs(difference) <= tolerance,
    }


def atwater_target_consistency(target: NutritionTarget) -> dict[str, float | bool] | None:
    """Check a fully specified macro target against its stated kcal target."""
    if target.kcal is None or any(
        getattr(target, key) is None for key in ("protein_g", "carbs_g", "fat_g")
    ):
        return None
    estimated = 4 * float(target.protein_g) + 4 * float(target.carbs_g) + 9 * float(target.fat_g)
    difference = float(target.kcal) - estimated
    tolerance = max(estimated * 0.2, 50.0)
    return {
        "stated_kcal": float(target.kcal),
        "estimated_kcal": estimated,
        "difference_kcal": difference,
        "within_tolerance": abs(difference) <= tolerance,
    }


def compute_macros(ingredients: list[IngredientPortion]) -> MacroTotals:
    """Sum ingredient nutrition, scaling per-100g values by the portion size."""
    totals = {
        key: sum(
            getattr(item.per_100g, key) * item.grams / 100
            for item in ingredients
        )
        for key in MACRO_KEYS
    }
    return MacroTotals(**totals)


def verify_macros(
    ingredients: list[IngredientPortion],
    target: NutritionTarget,
    tolerance_fraction: float = 0.05,
    tolerance_floor: float = 1.0,
) -> VerificationResult:
    """Recompute totals and check every constrained target within tolerance."""
    totals = compute_macros(ingredients)
    deviations: dict[str, float] = {}
    passed = True
    for key in MACRO_KEYS:
        actual = getattr(totals, key)
        exact = getattr(target, key)
        minimum = getattr(target, f"{key}_min")
        maximum = getattr(target, f"{key}_max")
        has_range = minimum is not None or maximum is not None

        if exact is not None:
            deviation = actual - exact
            deviations[key] = deviation
            if not has_range:        # with a range, the exact value is only a goal, not a pass/fail test
                allowed = max(abs(exact) * tolerance_fraction, tolerance_floor)
                passed = passed and abs(deviation) <= allowed

        if minimum is not None:
            deviations[f"{key}_min"] = actual - minimum
            passed = passed and actual >= minimum

        if maximum is not None:
            deviations[f"{key}_max"] = actual - maximum
            passed = passed and actual <= maximum

    suggestions = [] if passed else ["Adjust portions and re-run deterministic verification."]
    energy = atwater_consistency(totals)
    warnings = []
    if not energy["within_tolerance"]:
        warnings.append(
            "USDA energy differs materially from 4/4/9 Atwater energy; check fiber, food match, cooking yield, or label rounding."
        )
    return VerificationResult(
        passed=passed,
        totals=totals,
        deviation=deviations,
        suggestions=suggestions,
        warnings=warnings,
    )
