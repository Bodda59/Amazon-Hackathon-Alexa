"""Recipe writer: turns a verified, gram-exact ingredient list into cooking steps."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from pydantic import BaseModel, Field

from meal_agent.config import settings

logger = logging.getLogger(__name__)

_NUTRITION_CLAIM = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:kcal|calories?|g\s+(?:of\s+)?(?:protein|carbs?|fat))\b", re.I
)


class Recipe(BaseModel):
    title: str = Field(max_length=120)
    prep_minutes: int = Field(ge=0, le=240)
    cook_minutes: int = Field(ge=0, le=240)
    steps: list[str] = Field(min_length=1, max_length=10)
    tips: list[str] = Field(default_factory=list, max_length=3)


def _words(name: str) -> list[str]:
    return [w for w in re.findall(r"[a-z]+", name.casefold()) if len(w) >= 3]


def _covers_all(recipe: Recipe, amounts: list[dict[str, Any]]) -> bool:
    text = " ".join([recipe.title, *recipe.steps]).casefold()
    return all(any(w in text for w in _words(a["name"])) for a in amounts)


def _template(name: str, amounts: list[dict[str, Any]], method: str, draft: list[str]) -> dict[str, Any]:
    weighed = ", ".join(f"{a['grams']} g {a['name']}" for a in amounts)
    steps = [f"Weigh out: {weighed}.",
             f"Cook the ingredients using this method: {method or 'pan-cook or bake until done'}.",
             "Season lightly with salt and pepper, combine, and serve."]
    return {"title": name or "Meal", "prep_minutes": 10, "cook_minutes": 20,
            "steps": steps, "tips": [], "source": "template"}


async def write_recipe(
    dish_name: str,
    ingredients: list[dict[str, Any]],
    cooking_method: str = "",
    draft_steps: list[str] | None = None,
    diet: str = "",
    timeout: float = 6.0,
) -> dict[str, Any]:
    amounts = [{"name": str(i["name"]), "grams": round(float(i["grams"]))}
               for i in ingredients if float(i.get("grams", 0)) > 0]
    fallback = _template(dish_name, amounts, cooking_method, draft_steps or [])
    if not amounts or not settings.enable_orchestrator_llm:
        return fallback
    try:
        from services.LLMs import LLM_GPT

        prompt = (
            "Write a short, practical home recipe as JSON only. Schema: "
            '{"title":str,"prep_minutes":int,"cook_minutes":int,"steps":[str],"tips":[str]}.\n'
            "Rules: use EXACTLY these ingredients and gram amounts and mention every one by name "
            "with its grams in the steps. You may add only water, salt, black pepper and common "
            "dried spices or herbs. Do NOT state calories or macros. Respect the diet. "
            "3 to 7 concise steps, readable aloud by a voice assistant.\n"
            f"DISH IDEA: {dish_name}\nPREFERRED METHOD: {cooking_method}\nDIET: {diet or 'none'}\n"
            f"INGREDIENTS: {amounts}"
        )
        response = await asyncio.wait_for(LLM_GPT.ainvoke(prompt), timeout=timeout)
        raw = str(getattr(response, "content", response)).strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        recipe = Recipe.model_validate_json(raw)
        if not _covers_all(recipe, amounts) or any(_NUTRITION_CLAIM.search(s) for s in recipe.steps):
            logger.warning("Recipe writer output rejected; using template")
            return fallback
        return {**recipe.model_dump(), "source": "llm"}
    except Exception as exc:
        logger.warning("Recipe writer failed (%s); using template", type(exc).__name__)
        return fallback