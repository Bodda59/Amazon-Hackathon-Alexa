"""Neo4j adapter tests with a fake async driver; no live graph is required."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock

from meal_agent.agents.nutrition import NutritionAgent
from meal_agent.kg.neo4j_store import Neo4jKnowledgeGraph, Neo4jUnavailable
from meal_agent.schemas import NutritionTarget
from meal_agent.storage.domain_store import DomainStore


class FakeNeo4jDriver:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = records
        self.yield_records: list[dict[str, Any]] | None = None
        self.queries: list[tuple[str, dict[str, Any], str | None]] = []
        self.closed = False

    async def execute_query(
        self,
        query: str,
        *,
        parameters_: dict[str, Any],
        database_: str | None = None,
    ) -> tuple[list[dict[str, Any]], None, list[str]]:
        self.queries.append((query, parameters_, database_))
        if "RETURN r.factor AS factor" in query and self.yield_records is not None:
            return self.yield_records, None, []
        return self.records, None, []

    async def verify_connectivity(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class Neo4jTests(unittest.TestCase):
    def test_optional_connection_is_not_required_when_env_is_empty(self) -> None:
        graph = Neo4jKnowledgeGraph(uri="", username="", password="")
        self.assertFalse(graph.configured)
        health = asyncio.run(graph.verify_connectivity())
        self.assertEqual(health["status"], "not_configured")
        with self.assertRaises(Neo4jUnavailable):
            asyncio.run(graph.ingredient_facts("chicken"))

    def test_parameterized_queries_load_facts_rules_profiles_and_yields(self) -> None:
        profile = {
            "kcal": 120,
            "protein_g": 22,
            "carbs_g": 0,
            "fat_g": 2,
            "verified": True,
            "source": "USDA FoodData Central",
            "source_id": "fdc-123",
        }
        driver = FakeNeo4jDriver([
            {
                "name": "Peanut sauce",
                "canonical_name": "peanut sauce",
                "category": "sauce",
                "allergen_data_complete": True,
                "diet_data_complete": True,
                "allergens": ["peanut"],
                "violates_diets": [],
                "yields": [{"method": "saute", "factor": 0.8}],
                "nutrition_profile": profile,
            }
        ])
        driver.yield_records = [{"factor": 0.8}]
        graph = Neo4jKnowledgeGraph(
            "neo4j+s://example.invalid",
            "meal-agent",
            "unused-test-secret",
            "nutrition",
            driver=driver,
        )
        result = asyncio.run(graph.check_meal_rules(
            [{"name": "Peanut sauce"}], {"allergies": ["peanut"]}
        ))
        nutrition = asyncio.run(graph.nutrition_profile("Peanut sauce"))
        yield_factor = asyncio.run(graph.cooking_yield_factor("Peanut sauce", "saute"))
        self.assertFalse(result["passed"])
        self.assertIn("peanut", result["violations"][0])
        self.assertEqual(nutrition["source_id"], "fdc-123")
        self.assertEqual(nutrition["per_100g"]["protein_g"], 22)
        self.assertEqual(yield_factor, 0.8)
        self.assertTrue(all("Peanut sauce" not in query for query, _, _ in driver.queries))
        self.assertTrue(all(params.get("name") == "Peanut sauce" for _, params, _ in driver.queries if "name" in params))
        self.assertTrue(all(database == "nutrition" for _, _, database in driver.queries))

    def test_incomplete_allergen_coverage_fails_closed(self) -> None:
        driver = FakeNeo4jDriver([
            {
                "name": "Mystery seasoning",
                "canonical_name": "mystery seasoning",
                "category": "seasoning",
                "allergen_data_complete": False,
                "diet_data_complete": True,
                "allergens": [],
                "violates_diets": [],
                "yields": [],
                "nutrition_profile": None,
            }
        ])
        graph = Neo4jKnowledgeGraph("bolt://localhost", "user", "password", driver=driver)
        result = asyncio.run(graph.check_meal_rules(
            [{"name": "mystery seasoning"}], {"allergies": ["peanut"]}
        ))
        self.assertFalse(result["passed"])
        self.assertEqual(result["unknown_ingredients"], ["mystery seasoning"])

    def test_verified_graph_nutrition_profile_precedes_usda_call(self) -> None:
        driver = FakeNeo4jDriver([
            {
                "name": "tofu",
                "canonical_name": "tofu",
                "category": "legume",
                "allergen_data_complete": True,
                "diet_data_complete": True,
                "allergens": ["soy"],
                "violates_diets": [],
                "yields": [],
                "nutrition_profile": {
                    "kcal": 90,
                    "protein_g": 10,
                    "carbs_g": 2,
                    "fat_g": 5,
                    "verified": True,
                    "source": "USDA FoodData Central",
                    "source_id": "fdc-456",
                },
            }
        ])
        graph = Neo4jKnowledgeGraph("bolt://localhost", "user", "password", driver=driver)
        store = object()
        food_data = type("FoodData", (), {"lookup": AsyncMock(side_effect=AssertionError("USDA fallback should not run"))})()
        agent = NutritionAgent(store, food_data, graph)
        result = asyncio.run(agent.nutrition_lookup("tofu"))
        self.assertEqual(result["per_100g"]["protein_g"], 10)
        self.assertEqual(result["source_id"], "fdc-456")

    def test_nutrition_agent_combines_curated_and_neo4j_diet_rules(self) -> None:
        driver = FakeNeo4jDriver([
            {
                "name": "chicken breast",
                "canonical_name": "chicken breast",
                "category": "meat",
                "allergen_data_complete": True,
                "diet_data_complete": True,
                "allergens": [],
                "violates_diets": ["vegetarian"],
                "yields": [],
                "nutrition_profile": None,
            }
        ])
        graph = Neo4jKnowledgeGraph("bolt://localhost", "user", "password", driver=driver)
        food_data = type("FoodData", (), {"lookup": AsyncMock()})()
        agent = NutritionAgent(object(), food_data, graph)
        result = asyncio.run(agent.verify_candidate(
            "user-1",
            [{
                "name": "chicken breast",
                "min_g": 100,
                "max_g": 100,
                "per_100g": {"kcal": 165, "protein_g": 31, "carbs_g": 0, "fat_g": 3.6},
            }],
            NutritionTarget(protein_g_min=25),
            {"diet": "vegetarian"},
        ))
        self.assertFalse(result["passed"])
        self.assertEqual(result["knowledge_graph"], "ok")
        self.assertTrue(any("Neo4j" in issue for issue in result["violations"]))

    def test_unavailable_neo4j_fails_closed_for_requested_allergy_checks(self) -> None:
        class BrokenDriver(FakeNeo4jDriver):
            async def execute_query(self, *args: Any, **kwargs: Any) -> Any:
                raise ConnectionError("offline")

        graph = Neo4jKnowledgeGraph(
            "bolt://localhost", "user", "password", driver=BrokenDriver([])
        )
        agent = NutritionAgent(object(), AsyncMock(), graph)
        result = asyncio.run(agent.verify_candidate(
            "user-1",
            [{
                "name": "chicken breast",
                "min_g": 100,
                "max_g": 100,
                "per_100g": {"kcal": 165, "protein_g": 31, "carbs_g": 0, "fat_g": 3.6},
            }],
            NutritionTarget(protein_g_min=25),
            {"allergies": ["peanut"]},
        ))
        self.assertFalse(result["passed"])
        self.assertEqual(result["knowledge_graph"], "unavailable")
        self.assertTrue(any("unavailable" in issue for issue in result["violations"]))


if __name__ == "__main__":
    unittest.main()
