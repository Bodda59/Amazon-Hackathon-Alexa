"""Optional Neo4j knowledge-graph adapter for meal facts and symbolic rules.

Expected graph shape (names can be stored as ``name`` or ``canonical_name``):

    (:Ingredient)-[:HAS_PROFILE]->(:NutrientProfile)
    (:Ingredient)-[:CONTAINS_ALLERGEN]->(:Allergen)
    (:Ingredient)-[:VIOLATES_DIET]->(:Diet)
    (:Ingredient)-[:SUBSTITUTES {ratio, caveat}]->(:Ingredient)
    (:Ingredient)-[:PAIRS_WELL_WITH]->(:Ingredient)
    (:Ingredient)-[:YIELD {factor}]->(:CookingMethod)

All Cypher statements are fixed here and all user-provided values are parameters.
"""

from __future__ import annotations

import asyncio
from typing import Any

from meal_agent.config import settings


class Neo4jUnavailable(RuntimeError):
    """Neo4j is not configured or could not be reached."""


class Neo4jKnowledgeGraph:
    """Async Neo4j adapter; optional until connection settings and graph data exist."""

    def __init__(
        self,
        uri: str | None = None,
        username: str | None = None,
        password: str | None = None,
        database: str | None = None,
        *,
        driver: Any | None = None,
    ) -> None:
        self.uri = uri if uri is not None else settings.neo4j_uri
        self.username = username if username is not None else settings.neo4j_username
        self.password = password if password is not None else settings.neo4j_password
        self.database = database if database is not None else settings.neo4j_database
        self._driver = driver
        self._owns_driver = driver is None
        self._driver_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.uri and self.username and self.password)

    async def _get_driver(self) -> Any:
        if not self.configured:
            raise Neo4jUnavailable(
                "Neo4j is optional but not configured. Set NEO4J_URI, NEO4J_USERNAME, and NEO4J_PASSWORD."
            )
        if self._driver is None:
            async with self._driver_lock:
                if self._driver is None:
                    from neo4j import AsyncGraphDatabase
                    try:
                        self._driver = AsyncGraphDatabase.driver(
                            self.uri,
                            auth=(self.username, self.password),
                        )
                    except Exception as exc:
                        raise Neo4jUnavailable(
                            f"Neo4j driver setup failed ({type(exc).__name__})."
                        ) from exc
        return self._driver

    async def _query(self, cypher: str, **parameters: Any) -> list[dict[str, Any]]:
        driver = await self._get_driver()
        try:
            records, _summary, _keys = await driver.execute_query(
                cypher,
                parameters_=parameters,
                database_=self.database,
            )
        except Exception as exc:
            raise Neo4jUnavailable(
                f"Neo4j query failed ({type(exc).__name__}); verify the connection and graph schema."
            ) from exc
        return [dict(record) for record in records]

    async def verify_connectivity(self) -> dict[str, Any]:
        """Test credentials/connectivity without requiring any schema to exist."""
        try:
            driver = await self._get_driver()
            await driver.verify_connectivity()
            return {"status": "connected", "database": self.database}
        except Neo4jUnavailable as exc:
            return {"status": "not_configured", "reason": str(exc)}
        except Exception as exc:
            return {
                "status": "unavailable",
                "reason": f"Neo4j connectivity failed ({type(exc).__name__}).",
            }

    async def ingredient_facts(self, name: str) -> dict[str, Any] | None:
        """Load one ingredient, nutrient profile, allergen/diet edges, and yield rules."""
        rows = await self._query(
            """
            MATCH (i:Ingredient)
            WHERE toLower(coalesce(i.canonical_name, i.name, '')) = toLower($name)
            OPTIONAL MATCH (i)-[:HAS_PROFILE]->(p:NutrientProfile)
            OPTIONAL MATCH (i)-[:IN_CATEGORY]->(c:Category)
            OPTIONAL MATCH (i)-[:CONTAINS_ALLERGEN]->(a:Allergen)
            OPTIONAL MATCH (i)-[:VIOLATES_DIET]->(d:Diet)
            OPTIONAL MATCH (i)-[y:`YIELD`]->(m:CookingMethod)
            RETURN i.name AS name,
                   i.canonical_name AS canonical_name,
                   coalesce(c.canonical_name, c.name, i.category) AS category,
                   coalesce(i.allergen_data_complete, false) AS allergen_data_complete,
                   coalesce(i.diet_data_complete, false) AS diet_data_complete,
                   collect(DISTINCT CASE WHEN a IS NULL THEN null ELSE coalesce(a.canonical_name, a.name) END) AS allergens,
                   collect(DISTINCT CASE WHEN d IS NULL THEN null ELSE coalesce(d.canonical_name, d.name) END) AS violates_diets,
                   collect(DISTINCT CASE WHEN m IS NULL THEN null ELSE {method: coalesce(m.canonical_name, m.name), factor: y.factor} END) AS yields,
                   p AS nutrition_profile
            """,
            name=name.strip(),
        )
        if not rows:
            return None
        row = rows[0]
        profile = row.get("nutrition_profile")
        if profile is not None:
            profile = dict(profile)
            nutrients = {
                "kcal": profile.get("kcal", profile.get("energy_kcal")),
                "protein_g": profile.get("protein_g", profile.get("protein")),
                "carbs_g": profile.get("carbs_g", profile.get("carbohydrates_g")),
                "fat_g": profile.get("fat_g", profile.get("fat_g_per_100g")),
            }
            if all(value is not None for value in nutrients.values()):
                profile["per_100g"] = {key: float(value) for key, value in nutrients.items()}
        return {
            "name": row.get("name") or name,
            "canonical_name": row.get("canonical_name"),
            "category": row.get("category"),
            "allergen_data_complete": bool(row.get("allergen_data_complete")),
            "diet_data_complete": bool(row.get("diet_data_complete")),
            "allergens": [value for value in row.get("allergens", []) if value],
            "violates_diets": [value for value in row.get("violates_diets", []) if value],
            "yields": [value for value in row.get("yields", []) if value],
            "nutrition_profile": profile,
        }

    async def nutrition_profile(self, name: str) -> dict[str, Any] | None:
        """Return only a graph profile explicitly marked as trusted and sourced."""
        facts = await self.ingredient_facts(name)
        if facts is None:
            return None
        profile = facts.get("nutrition_profile")
        if not isinstance(profile, dict) or not profile.get("per_100g"):
            return None
        if profile.get("verified") is not True or not profile.get("source"):
            return None
        return {
            "name": facts["name"],
            "per_100g": profile["per_100g"],
            "source": str(profile["source"]),
            "source_id": str(profile.get("source_id", "neo4j")),
            "verified": True,
        }

    async def check_meal_rules(
        self, ingredients: list[dict[str, Any]], profile: dict[str, Any]
    ) -> dict[str, Any]:
        """Evaluate diet/allergen edges and report unknown graph coverage explicitly."""
        facts: list[dict[str, Any]] = []
        for ingredient in ingredients:
            name = str(ingredient.get("name", ""))
            fact = await self.ingredient_facts(name)
            if fact is None:
                facts.append({"name": name, "known": False})
            else:
                facts.append({**fact, "known": True})

        allergy_set = {str(item).casefold().replace(" ", "_") for item in profile.get("allergies", [])}
        diet = str(profile.get("diet", "")).casefold()
        violations: list[str] = []
        unknown: list[str] = []
        for fact in facts:
            name = str(fact["name"])
            if not fact["known"]:
                if allergy_set or diet in {"vegan", "plant-based", "vegetarian", "halal", "kosher"}:
                    unknown.append(name.casefold())
                continue
            allergens = {str(item).casefold().replace(" ", "_") for item in fact["allergens"]}
            if allergy_set and not fact["allergen_data_complete"]:
                unknown.append(name.casefold())
            for allergen in allergy_set & allergens:
                violations.append(f"{name} contains the declared allergen {allergen} according to Neo4j.")
            violated_diets = {str(item).casefold() for item in fact["violates_diets"]}
            if diet and diet in violated_diets:
                violations.append(f"{name} violates the {diet} diet according to Neo4j.")
            if (
                diet
                and diet in {"vegan", "plant-based", "vegetarian", "halal", "kosher"}
                and diet not in violated_diets
                and not fact["diet_data_complete"]
            ):
                unknown.append(name.casefold())
        unknown = sorted(set(unknown))
        violations.extend(
            f"Neo4j diet/allergen coverage is incomplete for {name}; safety cannot be verified."
            for name in unknown
        )
        return {
            "status": "ok",
            "passed": not violations,
            "violations": violations,
            "unknown_ingredients": unknown,
            "source": "Neo4j",
        }

    async def suggest_substitutions(
        self, ingredient: str, diet: str | None = None
    ) -> list[dict[str, Any]]:
        rows = await self._query(
            """
            MATCH (source:Ingredient)-[r:SUBSTITUTES]->(alternative:Ingredient)
            WHERE toLower(coalesce(source.canonical_name, source.name, '')) = toLower($name)
            OPTIONAL MATCH (alternative)-[:VIOLATES_DIET]->(d:Diet)
            WITH alternative, r, collect(DISTINCT toLower(coalesce(d.canonical_name, d.name))) AS violated_diets
            WHERE $diet = '' OR NOT $diet IN violated_diets
            RETURN alternative.name AS name,
                   coalesce(r.ratio, 1.0) AS ratio,
                   r.caveat AS caveat,
                   r.macro_equivalent AS macro_equivalent
            ORDER BY coalesce(r.preference_score, 0) DESC, alternative.name
            """,
            name=ingredient.strip(),
            diet=(diet or "").casefold(),
        )
        return rows

    async def cooking_yield_factor(self, food: str, method: str) -> float | None:
        rows = await self._query(
            """
            MATCH (i:Ingredient)-[r:`YIELD`]->(m:CookingMethod)
            WHERE toLower(coalesce(i.canonical_name, i.name, '')) = toLower($food)
              AND toLower(coalesce(m.canonical_name, m.name, '')) = toLower($method)
            RETURN r.factor AS factor
            LIMIT 1
            """,
            food=food.strip(),
            method=method.strip(),
        )
        if not rows or rows[0].get("factor") is None:
            return None
        factor = float(rows[0]["factor"])
        return factor if factor > 0 else None

    async def pairs_well_with(self, ingredient: str) -> list[str]:
        rows = await self._query(
            """
            MATCH (i:Ingredient)-[:PAIRS_WELL_WITH]-(paired:Ingredient)
            WHERE toLower(coalesce(i.canonical_name, i.name, '')) = toLower($name)
            RETURN DISTINCT paired.name AS name
            ORDER BY name
            """,
            name=ingredient.strip(),
        )
        return [str(row["name"]) for row in rows if row.get("name")]

    async def close(self) -> None:
        if self._driver is not None and self._owns_driver:
            await self._driver.close()
            self._driver = None


_DEFAULT_GRAPH: Neo4jKnowledgeGraph | None = None


def get_neo4j_knowledge_graph() -> Neo4jKnowledgeGraph:
    global _DEFAULT_GRAPH
    if _DEFAULT_GRAPH is None:
        _DEFAULT_GRAPH = Neo4jKnowledgeGraph()
    return _DEFAULT_GRAPH
