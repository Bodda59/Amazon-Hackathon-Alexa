"""Deterministic diet/allergen checks, substitutions, and cooking-yield factors."""

from __future__ import annotations

import re
from typing import Any

_ALLERGEN_KEYWORDS = {
    "milk": {"milk", "dairy", "cheese", "yogurt", "butter", "whey", "casein"},
    "egg": {"egg", "eggs", "mayonnaise"},
    "fish": {"fish", "salmon", "tuna", "cod", "anchovy"},
    "shellfish": {"shrimp", "prawn", "crab", "lobster", "shellfish"},
    "peanut": {"peanut", "groundnut"},
    "tree_nut": {"almond", "cashew", "walnut", "pecan", "pistachio", "hazelnut"},
    "soy": {"soy", "tofu", "tempeh", "edamame"},
    "wheat": {"wheat", "flour", "bread", "pasta", "couscous", "seitan"},
    "sesame": {"sesame", "tahini"},
}
_MEAT_WORDS = {"beef", "pork", "lamb", "chicken", "turkey", "duck", "meat", "bacon", "ham", "sausage", "gelatin"}
_FISH_WORDS = {"fish", "salmon", "tuna", "cod", "anchovy", "shrimp", "prawn", "crab", "lobster"}
_ANIMAL_WORDS = _MEAT_WORDS | _FISH_WORDS | {"milk", "cheese", "yogurt", "butter", "egg", "eggs", "honey", "whey", "casein"}
_SUBSTITUTIONS = {
    "chicken": ["turkey breast", "firm tofu", "tempeh", "seitan"],
    "beef": ["turkey", "lentils", "tempeh"],
    "milk": ["unsweetened soy milk", "oat milk"],
    "yogurt": ["soy yogurt", "cottage cheese"],
    "rice": ["quinoa", "cauliflower rice"],
    "egg": ["tofu scramble", "chickpea flour"],
    "butter": ["olive oil", "plant-based butter"],
}
# Cooked/raw yield ratios: cooked grams divided by raw grams.
_YIELD_FACTORS = {
    "chicken": {"roast": 0.75, "bake": 0.75, "grill": 0.72, "boil": 0.78, "saute": 0.75},
    "turkey": {"roast": 0.75, "bake": 0.75, "grill": 0.72},
    "beef": {"roast": 0.72, "grill": 0.72, "saute": 0.75},
    "pork": {"roast": 0.72, "bake": 0.75, "grill": 0.72},
    "rice": {"boil": 3.0, "steam": 3.0},
    "pasta": {"boil": 2.3},
    "quinoa": {"boil": 2.8},
    "spinach": {"saute": 0.35, "boil": 0.4},
    "broccoli": {"steam": 0.9, "roast": 0.8},
}


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.casefold()))


def _ingredient_allergens(ingredient: dict[str, Any]) -> set[str] | None:
    name = str(ingredient.get("name", ""))
    metadata = ingredient.get("metadata", {})
    explicit = metadata.get("allergens") if isinstance(metadata, dict) else None
    if explicit is not None:
        return {str(value).casefold().replace(" ", "_") for value in explicit}
    tokens = _tokens(name)
    detected = {allergen for allergen, words in _ALLERGEN_KEYWORDS.items() if tokens & words}
    known = bool(tokens & (_ANIMAL_WORDS | set().union(*_ALLERGEN_KEYWORDS.values())))
    if not known and str(ingredient.get("category", "")).casefold() in {
        "fruit", "vegetable", "grain", "legume", "herb", "spice", "oil"
    }:
        return set()
    return detected if known else None


def check_meal_rules(ingredients: list[dict[str, Any]], profile: dict[str, Any]) -> dict[str, Any]:
    """Check common diet/allergen policies, failing closed on unknown sensitive facts."""
    allergies = {str(value).casefold().replace(" ", "_") for value in profile.get("allergies", [])}
    diet = str(profile.get("diet", "") or "").casefold()
    violations: list[str] = []
    unknown: list[str] = []
    for ingredient in ingredients:
        name = str(ingredient.get("name", "unknown ingredient"))
        tokens = _tokens(name)
        allergens = _ingredient_allergens(ingredient)
        if allergens is None and allergies:
            unknown.append(name)
        elif allergens is not None:
            for allergen in allergies & allergens:
                violations.append(f"{name} may contain the declared allergen {allergen}.")
        is_meat = bool(tokens & _MEAT_WORDS)
        is_fish = bool(tokens & _FISH_WORDS)
        is_animal = bool(tokens & _ANIMAL_WORDS)
        if diet in {"vegan", "plant-based"} and is_animal:
            violations.append(f"{name} is not compatible with a vegan diet.")
        elif diet in {"vegetarian", "ovo-lacto vegetarian", "lacto-ovo vegetarian"} and (is_meat or is_fish):
            violations.append(f"{name} is not compatible with a vegetarian diet.")
        elif diet in {"pescatarian", "pescetarian"} and is_meat:
            violations.append(f"{name} is not compatible with a pescatarian diet.")
        if diet in {"halal", "kosher"}:
            metadata = ingredient.get("metadata", {})
            certifications = {str(value).casefold() for value in metadata.get("certifications", [])}
            if is_meat and diet not in certifications:
                violations.append(f"{name} lacks verified {diet} certification.")
            elif not is_fish and diet not in certifications:
                unknown.append(name)
    if unknown:
        violations.extend(f"Diet/allergen facts are unknown for {name}; cannot verify safety." for name in sorted(set(unknown)))
    return {"status": "ok", "passed": not violations, "violations": violations, "unknown_ingredients": sorted(set(unknown))}


def suggest_substitutions(ingredient: str, diet: str | None = None) -> list[str]:
    """Suggest common alternatives; no macro-equivalence is implied."""
    options = list(_SUBSTITUTIONS.get(ingredient.casefold().strip(), []))
    if (diet or "").casefold() in {"vegan", "plant-based"}:
        options = [option for option in options if option not in {"cottage cheese", "turkey breast", "turkey"}]
    return options


def cooking_yield_factor(food: str, method: str) -> float | None:
    """Return cooked/raw mass yield, or None when no explicit rule is available."""
    food_key = food.casefold().strip()
    method_tokens = re.findall(r"[a-z]+", method.casefold())
    factors = _YIELD_FACTORS.get(food_key)
    if factors is None:
        food_tokens = set(_tokens(food_key))
        factors = next(
            (rules for key, rules in _YIELD_FACTORS.items() if key in food_tokens),
            None,
        )
    if factors is None:
        return None
    return next((factors[token] for token in method_tokens if token in factors), None)


def apply_yield_factor(food: str, method: str, grams: float, *, direction: str = "raw_to_cooked") -> float:
    """Convert mass using known cooking yields; reject unknown factor/method."""
    if grams < 0:
        raise ValueError("Grams cannot be negative.")
    factor = cooking_yield_factor(food, method)
    if factor is None or factor <= 0:
        raise ValueError(f"No yield factor for {food!r} cooked by {method!r}.")
    if direction == "raw_to_cooked":
        return grams * factor
    if direction == "cooked_to_raw":
        return grams / factor
    raise ValueError("direction must be 'raw_to_cooked' or 'cooked_to_raw'.")
