from meal_agent.schemas import NutritionTarget

# (min share, max share, kcal per gram). Tunable defaults.
_SHARE = {"protein_g": (0.20, 0.30, 4.0), "carbs_g": (0.40, 0.55, 4.0), "fat_g": (0.20, 0.30, 9.0)}

def balance_target(target: NutritionTarget) -> tuple[NutritionTarget, list[str]]:
    data = target.model_dump(mode="json", exclude_none=True)
    kcal = data.get("kcal")
    if kcal is None and "kcal_min" in data and "kcal_max" in data:
        kcal = (data["kcal_min"] + data["kcal_max"]) / 2
    if kcal is None:
        return target, []

    free = [n for n in _SHARE if not any(k in data for k in (n, f"{n}_min", f"{n}_max"))]
    if len(free) == 3:  # calories only -> balanced plate
        for n, (lo, hi, kpg) in _SHARE.items():
            data[f"{n}_min"] = round(kcal * lo / kpg, 1)
            data[f"{n}_max"] = round(kcal * hi / kpg, 1)
        note = ("You didn't set macros, so I balanced it at roughly 20 to 30 percent protein, "
                "40 to 55 percent carbs and 20 to 30 percent fat.")
    elif "fat_g" in free:  # partial spec: only guard the degenerate case
        data["fat_g_max"] = round(kcal * 0.35 / 9.0, 1)
        note = "I capped fat at about 35 percent of calories."
    else:
        return target, []
    return NutritionTarget.model_validate(data), [note]