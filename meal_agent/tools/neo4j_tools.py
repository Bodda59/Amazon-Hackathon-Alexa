"""Internal Neo4j knowledge-graph tools used by composer and nutrition agents."""

from __future__ import annotations

from typing import Any

from meal_agent.kg.neo4j_store import (
    Neo4jKnowledgeGraph,
    Neo4jUnavailable,
    get_neo4j_knowledge_graph,
)
from meal_agent.tools.kg_tools import check_meal_rules as check_curated_rules


def _graph(knowledge_graph: Neo4jKnowledgeGraph | None) -> Neo4jKnowledgeGraph:
    return knowledge_graph or get_neo4j_knowledge_graph()


async def neo4j_healthcheck(
    knowledge_graph: Neo4jKnowledgeGraph | None = None,
) -> dict[str, Any]:
    """Check Neo4j credentials/connectivity without requiring schema or sample data."""
    return await _graph(knowledge_graph).verify_connectivity()


async def ingredient_facts(
    name: str,
    knowledge_graph: Neo4jKnowledgeGraph | None = None,
) -> dict[str, Any]:
    """Fetch one ingredient node, nutrient profile and linked symbolic facts."""
    graph = _graph(knowledge_graph)
    if not graph.configured:
        return {"status": "not_configured", "name": name, "facts": None}
    try:
        return {"status": "ok", "name": name, "facts": await graph.ingredient_facts(name)}
    except Neo4jUnavailable as exc:
        return {"status": "unavailable", "name": name, "reason": str(exc)}


async def kg_check_rules(
    ingredients: list[dict[str, Any]],
    profile: dict[str, Any],
    knowledge_graph: Neo4jKnowledgeGraph | None = None,
) -> dict[str, Any]:
    """Use Neo4j rules when configured; curated rules remain the local fallback."""
    graph = _graph(knowledge_graph)
    if not graph.configured:
        return {**check_curated_rules(ingredients, profile), "source": "curated_fallback"}
    try:
        return await graph.check_meal_rules(ingredients, profile)
    except Neo4jUnavailable as exc:
        fallback = check_curated_rules(ingredients, profile)
        needs_graph = bool(profile.get("allergies") or profile.get("diet"))
        if needs_graph:
            fallback["passed"] = False
            fallback["violations"] = list(fallback["violations"]) + [
                f"Neo4j rules are unavailable; requested diet/allergen safety cannot be confirmed ({exc})."
            ]
        return {**fallback, "status": "unavailable", "source": "curated_fallback"}


async def graph_substitutions(
    ingredient: str,
    diet: str | None = None,
    knowledge_graph: Neo4jKnowledgeGraph | None = None,
) -> list[dict[str, Any]]:
    """Return Neo4j SUBSTITUTES alternatives, including ratio/caveat properties."""
    graph = _graph(knowledge_graph)
    if not graph.configured:
        return []
    try:
        return await graph.suggest_substitutions(ingredient, diet)
    except Neo4jUnavailable:
        return []


async def graph_yield_factor(
    food: str,
    method: str,
    knowledge_graph: Neo4jKnowledgeGraph | None = None,
) -> dict[str, Any]:
    """Return an explicit Neo4j cooked/raw yield factor, or signal missing knowledge."""
    graph = _graph(knowledge_graph)
    if not graph.configured:
        return {"status": "not_configured", "factor": None}
    try:
        factor = await graph.cooking_yield_factor(food, method)
        return {"status": "ok" if factor is not None else "not_found", "factor": factor}
    except Neo4jUnavailable as exc:
        return {"status": "unavailable", "factor": None, "reason": str(exc)}
