"""Nutrition verifier specialist: trusted facts, yield conversions, solving and claims."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from meal_agent.config import settings
from meal_agent.kg.neo4j_store import Neo4jKnowledgeGraph, Neo4jUnavailable, get_neo4j_knowledge_graph
from meal_agent.schemas import IngredientPortion, MacroTotals, NutritionTarget
from meal_agent.storage.domain_store import DomainStore
from meal_agent.tools.food_data import FoodDataClient
from meal_agent.tools.kg_tools import apply_yield_factor, check_meal_rules
from meal_agent.tools.nutrition import atwater_consistency, compute_macros
from meal_agent.tools.solver import solve_portions

logger = logging.getLogger(__name__)


class NutritionAgent:
    """Ground meal verification in USDA/Open Food Facts records and symbolic checks."""

    def __init__(
        self,
        store: DomainStore,
        food_data: FoodDataClient | None = None,
        knowledge_graph: Neo4jKnowledgeGraph | None = None,
    ) -> None:
        self.store = store
        self.food_data = food_data or FoodDataClient(store)
        self.knowledge_graph = knowledge_graph or get_neo4j_knowledge_graph()
        # In-memory cache of LLM nutrition estimates (only used when USDA/OFF has no record).
        self._estimate_cache: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _meets_target(
        totals: dict[str, float],
        target: dict[str, Any],
        tol_fraction: float = 0.05,
        tol_floor: float = 1.0,
    ) -> tuple[bool, list[str]]:
        """Min/max ranges decide pass/fail. An exact value only counts (with tolerance)
        for nutrients that have no range."""
        misses: list[str] = []
        for key in ("kcal", "protein_g", "carbs_g", "fat_g"):
            value = float(totals.get(key, 0.0))
            low, high = target.get(f"{key}_min"), target.get(f"{key}_max")
            if low is not None or high is not None:
                if low is not None and value < float(low) - 1e-6:
                    misses.append(f"{key} below minimum")
                if high is not None and value > float(high) + 1e-6:
                    misses.append(f"{key} above maximum")
            elif target.get(key) is not None:
                goal = float(target[key])
                if abs(value - goal) > max(abs(goal) * tol_fraction, tol_floor):
                    misses.append(f"{key} outside tolerance of exact target")
        return not misses, misses

    async def nutrition_lookup(self, food: str) -> dict[str, Any]:
        """Neo4j first, then USDA/Open Food Facts, then (optionally) a sanity-checked LLM estimate."""
        if self.knowledge_graph.configured:
            try:
                graph_profile = await self.knowledge_graph.nutrition_profile(food)
                if graph_profile is not None:
                    return graph_profile
            except Neo4jUnavailable:
                # USDA/Open Food Facts remains the trusted fallback if Neo4j is down.
                pass
        try:
            return await self.food_data.lookup(food)
        except Exception as exc:
            # A missing API key is a configuration problem, not a "food not found" problem.
            fallback_enabled = getattr(settings, "enable_nutrition_llm_fallback", True)
            if "USDA_API_KEY" in str(exc) or not fallback_enabled:
                raise
            logger.warning("USDA/OFF miss for %r (%s); trying LLM estimate", food, exc)
            try:
                return await self._llm_nutrition_estimate(food)
            except Exception:
                logger.warning("LLM nutrition estimate failed for %r", food, exc_info=True)
                raise exc

    async def _llm_nutrition_estimate(self, food: str) -> dict[str, Any]:
        """Last-resort per-100 g estimate. Rejected unless it passes basic physical sanity checks."""
        key = food.casefold().strip()
        if key in self._estimate_cache:
            return self._estimate_cache[key]
        from services.LLMs import LLM_GPT

        prompt = (
            "Estimate typical nutrition for 100 g of the food below, as sold (raw/uncooked "
            "unless the name says otherwise). Return ONLY JSON: "
            '{"kcal":number,"protein_g":number,"carbs_g":number,"fat_g":number}.\n'
            f"FOOD: {food}"
        )
        response = await asyncio.wait_for(LLM_GPT.ainvoke(prompt), timeout=8.0)
        raw = str(getattr(response, "content", response)).strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(raw)
        per = {k: float(data[k]) for k in ("kcal", "protein_g", "carbs_g", "fat_g")}

        # Sanity gates: reject impossible or internally inconsistent estimates.
        macro_mass = per["protein_g"] + per["carbs_g"] + per["fat_g"]
        atwater = 4 * per["protein_g"] + 4 * per["carbs_g"] + 9 * per["fat_g"]
        if (
            min(per.values()) < 0
            or macro_mass > 100.5
            or per["kcal"] > 900
            or abs(per["kcal"] - atwater) > max(0.2 * atwater, 15)
        ):
            raise ValueError(f"LLM nutrition estimate for {food!r} failed sanity checks")

        entry = {
            "per_100g": per,
            "source": "llm_estimate",
            "source_id": f"llm:{key}",
            "estimated": True,
        }
        self._estimate_cache[key] = entry
        return entry

    async def apply_yield_factors(
        self, food: str, method: str, grams: float, direction: str = "raw_to_cooked"
    ) -> float:
        if self.knowledge_graph.configured:
            try:
                factor = await self.knowledge_graph.cooking_yield_factor(food, method)
                if factor is not None:
                    if grams < 0:
                        raise ValueError("Grams cannot be negative.")
                    return grams * factor if direction == "raw_to_cooked" else grams / factor
            except Neo4jUnavailable:
                pass
        return apply_yield_factor(food, method, grams, direction=direction)

    _LABELS = {"kcal": "kcal", "protein_g": "g protein", "carbs_g": "g carbs", "fat_g": "g fat"}

    @classmethod
    def _build_suggestions(
        cls, deviation: dict[str, float], target: dict[str, Any], options: dict[str, Any]
    ) -> list[str]:
        tol = float(options.get("tolerance_fraction", 0.05))
        floor = float(options.get("tolerance_floor", 1.0))
        out: list[str] = []
        nutrients = {k.removesuffix("_min").removesuffix("_max") for k in deviation}
        for nutrient in sorted(nutrients):
            label = cls._LABELS.get(nutrient, nutrient)
            if f"{nutrient}_min" in deviation or f"{nutrient}_max" in deviation:
                under = -float(deviation.get(f"{nutrient}_min", 0.0))   # >0 means below the minimum
                over = float(deviation.get(f"{nutrient}_max", 0.0))     # >0 means above the maximum
                if under > 0:
                    out.append(f"Add about {under:.1f} {label} (below the minimum).")
                if over > 0:
                    out.append(f"Reduce about {over:.1f} {label} (above the maximum).")
            else:
                gap = float(deviation.get(nutrient, 0.0))
                allowed = max(abs(float(target.get(nutrient, 0.0))) * tol, floor)
                if gap < -allowed:
                    out.append(f"Add about {-gap:.1f} {label}.")
                elif gap > allowed:
                    out.append(f"Reduce about {gap:.1f} {label}.")
        return out

    async def solve_portions(
        self,
        items: list[dict[str, Any]],
        target: NutritionTarget,
        constraints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        enriched: list[dict[str, Any]] = []
        names = [str(i.get("name", "")) for i in items if not i.get("per_100g")]
        results = await asyncio.gather(*(self.nutrition_lookup(n) for n in names), return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                raise result
        foods = dict(zip(names, results))
        for item in items:
            value = dict(item)
            if not value.get("per_100g"):
                food = foods[str(value.get("name", ""))]
                value["per_100g"] = food["per_100g"]
                value["nutrition_source"] = food["source"]
                value["nutrition_source_id"] = food["source_id"]
            if value.get("portion_basis", "raw") == "cooked":
                method = value.get("cooking_method") or (constraints or {}).get("cooking_method")
                if not method:
                    return {
                        "passed": False,
                        "reason": f"A cooking method is required to convert cooked weight for {value.get('name', 'ingredient')!r}.",
                        "ingredients": [],
                        "estimated_foods": [n for n in names if foods[n].get("estimated")],
                    }
                try:
                    yield_factor = await self.apply_yield_factors(
                        str(value.get("name", "")), str(method), 100.0
                    ) / 100.0
                except ValueError as exc:
                    return {
                        "passed": False,
                        "reason": str(exc),
                        "ingredients": [],
                        "estimated_foods": [n for n in names if foods[n].get("estimated")],
                    }
                value["per_100g"] = {
                    key: float(nutrient) / yield_factor
                    for key, nutrient in value["per_100g"].items()
                }
            enriched.append(value)
        options = constraints or {}
        solved = await asyncio.to_thread(
            solve_portions,
            enriched,
            target,
            purchase_penalty=float(options.get("purchase_penalty", 0.05)),
            weights=options.get("weights"),
            tolerance_fraction=float(options.get("tolerance_fraction", 0.05)),
            tolerance_floor=float(options.get("tolerance_floor", 1.0)),
        )
        solved["estimated_foods"] = [n for n in names if foods[n].get("estimated")]
        if not solved.get("passed"):
            suggestions = self._build_suggestions(
                solved.get("deviation", {}), target.model_dump(mode="json", exclude_none=True), options
            )
            if not suggestions and solved.get("reason"):
                suggestions.append(str(solved["reason"]))
            solved["suggestions"] = suggestions
        return solved

    async def verify_candidate(
        self,
        user_id: str,
        ingredients: list[dict[str, Any]],
        target: NutritionTarget,
        profile: dict[str, Any],
        constraints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not ingredients:
            return {
                "status": "unverified",
                "passed": False,
                "reason": "The candidate meal has no ingredients.",
                "totals": None,
                "deviation": {},
                "violations": [],
                "suggestions": ["Compose a candidate with at least one ingredient."],
            }
        try:
            solved = await self.solve_portions(ingredients, target, constraints)
        except Exception as exc:
            logger.exception("Verification failed for %s", [i.get("name") for i in ingredients])
            return {
                "status": "not_configured" if "USDA_API_KEY" in str(exc) or "nutrition record" in str(exc) else "unverified",
                "passed": False,
                "reason": f"Could not ground the meal in nutrition data: {type(exc).__name__}.",
                "totals": None,
                "deviation": {},
                "violations": [],
                "suggestions": ["Check the food name or nutrition provider configuration."],
            }

        if not solved.get("ingredients"):
            return {
                "status": "unverified",
                "passed": False,
                "reason": solved.get("reason", "No portions could be solved."),
                "totals": solved.get("totals"),
                "deviation": solved.get("deviation", {}),
                "violations": solved.get("violations", []),
                "suggestions": solved.get("suggestions", []),
                "missing_capacity": self._missing_capacity(solved.get("deviation", {})),
                "slack": solved.get("slack", {}),
                "warnings": solved.get("warnings", []),
                "atwater": solved.get("atwater", {}),
                "target_atwater": solved.get("target_atwater"),
                "ingredients": solved.get("ingredients", []),
                "estimated_foods": solved.get("estimated_foods", []),
            }

        portions = [IngredientPortion.model_validate(item) for item in solved["ingredients"]]
        checked_ingredients = [
            {
                **portion.model_dump(mode="json"),
                "metadata": next(
                    (candidate.get("metadata", {}) for candidate in ingredients
                     if str(candidate.get("name", "")).casefold() == portion.name.casefold()),
                    {},
                ),
                "category": next(
                    (candidate.get("category", "") for candidate in ingredients
                     if str(candidate.get("name", "")).casefold() == portion.name.casefold()),
                    "",
                ),
            }
            for portion in portions
        ]
        rule_profile = {**profile, **(constraints or {})}
        rules = check_meal_rules(checked_ingredients, rule_profile)
        graph_rules: dict[str, Any] = {"status": "not_configured", "passed": True, "violations": [], "unknown_ingredients": []}
        if self.knowledge_graph.configured and (rule_profile.get("allergies") or rule_profile.get("diet")):
            try:
                graph_rules = await self.knowledge_graph.check_meal_rules(checked_ingredients, rule_profile)
            except Neo4jUnavailable as exc:
                graph_rules = {
                    "status": "unavailable",
                    "passed": False,
                    "violations": [f"Neo4j safety checks are unavailable: {exc}"],
                    "unknown_ingredients": [str(item.get("name", "unknown")) for item in checked_ingredients],
                }
        violations = list(dict.fromkeys(rules["violations"] + graph_rules.get("violations", [])))
        options = constraints or {}
        target_dict = target.model_dump(mode="json", exclude_none=True)
        has_ranges = any(key.endswith(("_min", "_max")) for key in target_dict)
        if has_ranges and solved.get("totals"):
            target_ok, _ = self._meets_target(
                solved["totals"],
                target_dict,
                float(options.get("tolerance_fraction", 0.05)),
                float(options.get("tolerance_floor", 1.0)),
            )
        else:
            target_ok = bool(solved.get("passed"))
        passed = target_ok and rules["passed"] and graph_rules.get("passed", True)
        deviations = solved.get("deviation", {})
        return {
            "status": "verified" if passed else "unmet",
            "passed": passed,
            "estimated_foods": solved.get("estimated_foods", []),
            "totals": solved.get("totals"),
            "deviation": deviations,
            "violations": violations,
            "suggestions": [] if passed else solved.get("suggestions", []) + rules.get("unknown_ingredients", []),
            "warnings": solved.get("warnings", []),
            "slack": solved.get("slack", {}),
            "missing_capacity": self._missing_capacity(deviations),
            "ingredients": [
                {
                    **portion.model_dump(mode="json"),
                    "nutrition_source": next(
                        (item.get("nutrition_source", "USDA/Open Food Facts") for item in solved["ingredients"]
                         if item["name"] == portion.name),
                        "USDA/Open Food Facts",
                    ),
                }
                for portion in portions
            ],
            "source": "USDA FoodData Central / Open Food Facts",
            "knowledge_graph": graph_rules.get("status", "not_configured"),
            "atwater": solved.get("atwater", atwater_consistency(
                MacroTotals.model_validate(solved["totals"])
            ) if solved.get("totals") else {}),
        }

    @staticmethod
    def _missing_capacity(deviation: dict[str, float]) -> dict[str, float]:
        gaps: dict[str, float] = {}
        for key, value in deviation.items():
            if key.endswith("_max"):
                continue
            nutrient = key.removesuffix("_min")
            has_range = f"{nutrient}_min" in deviation or f"{nutrient}_max" in deviation
            if key.endswith("_min"):
                if value < 0:
                    gaps[nutrient] = round(-value, 2)
            elif not has_range and value < 0:
                gaps[nutrient] = round(-value, 2)
        return gaps

    async def verify_claim(
        self, claim: str, ingredients: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Recompute numeric macro claims from trusted ingredient data."""
        portions: list[IngredientPortion] = []
        for ingredient in ingredients:
            food = await self.nutrition_lookup(str(ingredient["name"]))
            portions.append(
                IngredientPortion(
                    name=str(ingredient["name"]),
                    grams=float(ingredient["grams"]),
                    per_100g=food["per_100g"],
                )
            )
        totals = compute_macros(portions).model_dump(mode="json")
        aliases = {
            "kcal": r"(?:kcal|calories?)",
            "protein_g": r"protein(?:\s+of)?",
            "carbs_g": r"(?:carbs?|carbohydrates?)",
            "fat_g": r"fat",
        }
        verified_claims: list[dict[str, Any]] = []
        for macro, pattern in aliases.items():
            matches = list(re.finditer(
                rf"\b{pattern}\b\s*(?:[:=]\s*)?(?P<value>\d+(?:\.\d+)?)",
                claim,
                re.IGNORECASE,
            ))
            matches.extend(re.finditer(
                rf"(?P<value>\d+(?:\.\d+)?)\s*(?:g|grams?)?\s*(?:of\s+)?\b{pattern}\b",
                claim,
                re.IGNORECASE,
            ))
            seen_values: set[tuple[int, float]] = set()
            for match in matches:
                claimed = float(match.group("value"))
                marker = (match.start("value"), claimed)
                if marker in seen_values:
                    continue
                seen_values.add(marker)
                actual = totals[macro]
                verified_claims.append({
                    "macro": macro,
                    "claimed": claimed,
                    "computed": actual,
                    "passed": abs(claimed - actual) <= max(actual * 0.05, 1.0),
                })
        return {
            "passed": all(item["passed"] for item in verified_claims),
            "claims_found": len(verified_claims),
            "claims": verified_claims,
            "computed_totals": totals,
            "source": "USDA FoodData Central / Open Food Facts",
        }


_DEFAULT_AGENT: NutritionAgent | None = None


def get_nutrition_agent() -> NutritionAgent:
    global _DEFAULT_AGENT
    if _DEFAULT_AGENT is None:
        store = DomainStore(settings.workflow_database_path)
        _DEFAULT_AGENT = NutritionAgent(store)
    return _DEFAULT_AGENT


async def nutrition_lookup(food: str) -> dict[str, Any]:
    return await get_nutrition_agent().nutrition_lookup(food)


async def verify_candidate(
    user_id: str,
    ingredients: list[dict[str, Any]],
    target: NutritionTarget,
    profile: dict[str, Any] | None = None,
    constraints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return await get_nutrition_agent().verify_candidate(
        user_id, ingredients, target, profile or {}, constraints
    )