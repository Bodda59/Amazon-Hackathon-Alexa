"""Tests for SQLite domain agents, USDA normalization, solver, and mock procurement."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx

from meal_agent.agents.inventory import InventoryAgent
from meal_agent.agents.nutrition import NutritionAgent
from meal_agent.agents.procurement import ProcurementAgent
from meal_agent.agents.tracking import TrackingAgent
from meal_agent.agents.composer import _fallback_candidate
from meal_agent.graph.tools import OrchestratorTools
from meal_agent.adapters.retail.mock import MockRetailer
from meal_agent.schemas import InventoryImage, NutritionTarget
from meal_agent.storage.domain_store import DomainStore
from meal_agent.storage.workflow_store import WorkflowStore
from meal_agent.storage.workflow_store import WorkflowStore
from meal_agent.tools.food_data import FoodDataClient
from meal_agent.tools.kg_tools import apply_yield_factor, check_meal_rules
from meal_agent.tools.solver import solve_portions


class IntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Path(self.tempdir.name) / "agents.sqlite3"
        self.domain = DomainStore(self.database)
        self.workflow = WorkflowStore(self.database)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_usda_fooddata_search_and_cache(self) -> None:
        payload = {
            "foods": [
                {
                    "fdcId": 123,
                    "description": "Chicken, breast, raw",
                    "dataType": "Foundation",
                    "foodNutrients": [
                        {"nutrientId": 1008, "value": 120},
                        {"nutrientId": 1003, "value": 22.5},
                        {"nutrientId": 1005, "value": 0},
                        {"nutrientId": 1004, "value": 2.6},
                    ],
                }
            ]
        }
        request = AsyncMock(return_value=httpx.Response(200, json=payload))
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload)))
        try:
            provider = FoodDataClient(self.domain, api_key="test-key", http_client=client)
            first = asyncio.run(provider.lookup("chicken breast raw"))
            second = asyncio.run(provider.lookup("chicken breast raw"))
        finally:
            asyncio.run(client.aclose())
        self.assertEqual(first["source"], "USDA FoodData Central")
        self.assertEqual(first["source_id"], "123")
        self.assertEqual(first["per_100g"]["protein_g"], 22.5)
        self.assertEqual(second, first)

    def test_solver_finds_portions_and_reverifies_rounded_macros(self) -> None:
        result = solve_portions(
            [
                {
                    "name": "chicken breast",
                    "per_100g": {"kcal": 120, "protein_g": 23, "carbs_g": 0, "fat_g": 2.6},
                    "min_g": 30,
                    "max_g": 250,
                    "in_stock": True,
                    "available_g": 200,
                },
                {
                    "name": "brown rice",
                    "per_100g": {"kcal": 370, "protein_g": 8, "carbs_g": 77, "fat_g": 3},
                    "min_g": 0,
                    "max_g": 100,
                    "in_stock": True,
                    "available_g": 100,
                },
            ],
            NutritionTarget(protein_g_min=40, kcal_max=500),
        )
        self.assertTrue(result["passed"])
        self.assertTrue(result["ingredients"])
        self.assertGreaterEqual(result["totals"]["protein_g"], 40)
        self.assertLessEqual(result["totals"]["kcal"], 500)

    def test_inventory_voice_parse_upsert_consume_reserve_and_user_isolation(self) -> None:
        async def exercise() -> None:
            agent = InventoryAgent(self.domain)
            parsed = await agent.parse_utterance("I bought 1 kg chicken and a dozen eggs")
            self.assertEqual(parsed["status"], "parsed")
            self.assertEqual({item["name"] for item in parsed["items"]}, {"chicken", "eggs"})
            self.assertFalse(parsed["needs_clarification"])
            for item in parsed["items"]:
                await agent.inventory_upsert("user-1", item)
            rows = await agent.inventory_list("user-1")
            self.assertEqual(len(rows), 2)
            self.assertEqual(await agent.inventory_list("user-2"), [])
            reserved = await agent.inventory_reserve("user-1", [{"name": "chicken", "grams": 200}])
            self.assertEqual(reserved[0]["grams"], 200)
            chicken = next(row for row in await agent.inventory_list("user-1") if row["name"] == "chicken")
            self.assertEqual(chicken["grams_estimate"], 1000)
            self.assertEqual(chicken["available_grams"], 800)
            with self.assertRaises(ValueError):
                await agent.inventory_consume("user-1", "chicken", 0.9, "kg")
            consumed = await agent.inventory_consume("user-1", "chicken", 0.5, "kg")
            self.assertEqual(consumed["grams_estimate"], 500)

        asyncio.run(exercise())

    def test_low_confidence_inventory_requires_clarification_before_write(self) -> None:
        async def exercise() -> None:
            agent = InventoryAgent(self.domain)
            parsed = await agent.parse_utterance("I have some chicken")
            self.assertTrue(parsed["needs_clarification"])
            image_result = await agent.parse_receipt(InventoryImage(data="%%%", mime_type="image/jpeg"))
            self.assertEqual(image_result["status"], "invalid_input")
            result = await agent.manage_inventory("user-1", "add", "I have some chicken")
            self.assertEqual(result["status"], "needs_user")
            self.assertEqual(await agent.inventory_list("user-1"), [])

        asyncio.run(exercise())

    def test_diet_rules_fail_closed_and_yield_conversion_is_deterministic(self) -> None:
        result = check_meal_rules([{"name": "chicken breast"}], {"diet": "vegan"})
        self.assertFalse(result["passed"])
        self.assertTrue(result["violations"])
        self.assertAlmostEqual(apply_yield_factor("chicken", "grill", 200), 144)
        with self.assertRaises(ValueError):
            apply_yield_factor("mystery food", "fry", 100)

    def test_logging_verified_meal_consumes_its_reservation_and_updates_macros(self) -> None:
        self.domain.upsert_inventory(
            "user-1",
            {"name": "chicken", "quantity": 300, "unit": "g", "grams_estimate": 300},
        )
        self.domain.set_macro_targets("user-1", {"kcal": 2000, "protein_g": 120})
        self.domain.reserve_inventory(
            "user-1", [{"name": "chicken", "grams": 120, "plan_id": "plan-verified"}]
        )
        plan = {
                "plan_id": "plan-verified",
                "verification": {
                    "passed": True,
                    "totals": {"kcal": 250, "protein_g": 46, "carbs_g": 0, "fat_g": 5},
                },
                "meal": {"ingredients": [{"name": "chicken", "grams": 120}]},
            }
        logged = self.domain.log_verified_plan("user-1", plan)
        duplicate = self.domain.log_verified_plan("user-1", plan)
        self.assertEqual(logged["consumed"], [{"name": "chicken", "grams": 120}])
        self.assertTrue(duplicate["already_logged"])
        inventory = self.domain.list_inventory("user-1")[0]
        self.assertEqual(inventory["grams_estimate"], 180)
        self.assertEqual(inventory["reserved_grams"], 0)
        status = self.domain.nutrition_status("user-1")
        self.assertEqual(status["consumed_kcal"], 250)
        self.assertEqual(status["remaining_kcal"], 1750)

    def test_long_term_tables_plan_items_tool_traces_and_preference_memory(self) -> None:
        self.domain.save_meal_plan_items(
            "user-1", "plan-memory", [{"name": "spinach", "grams": 80}]
        )
        self.domain.record_tool_call(
            user_id="user-1",
            session_id="session-memory",
            plan_id="plan-memory",
            agent="procurement",
            tool_name="request_approval",
            inputs={"cart_id": "cart-1", "approval_token": "should-not-be-here"},
            output={"status": "approval_required"},
            duration_ms=12,
        )
        trace = self.domain.list_tool_call_traces("user-1", "session-memory")
        self.assertEqual(trace[0]["tool_name"], "request_approval")
        self.assertEqual(trace[0]["inputs"]["approval_token"], "[REDACTED]")
        self.domain.record_preference_feedback("user-1", "spicy", 1)
        self.domain.record_preference_feedback("user-1", "salmon", -1)
        preference_memory = self.domain.get_preference_summary("user-1")
        self.assertEqual({item["preference"] for item in preference_memory}, {"spicy", "salmon"})

    def test_graph_saves_plan_lines_reserves_proposed_stock_and_releases_on_abandonment(self) -> None:
        self.domain.upsert_inventory(
            "user-1", {"name": "chicken", "quantity": 300, "unit": "g", "grams_estimate": 300}
        )
        tools = OrchestratorTools(self.workflow)
        state = {
            "user_id": "user-1",
            "session_id": "session-reserve",
            "plan_id": "plan-reserve",
            "candidate": {"ingredients": [{"name": "chicken", "min_g": 100, "max_g": 150}]},
            "verification": {
                "passed": False,
                "ingredients": [{"name": "chicken", "grams": 120}],
                "totals": {"kcal": 250, "protein_g": 40, "carbs_g": 0, "fat_g": 5},
            },
            "procurement_result": {"status": "approval_required"},
            "final_result": {"status": "needs_user"},
        }
        asyncio.run(tools.save_plan(state))
        inventory = self.domain.list_inventory("user-1")[0]
        self.assertEqual(inventory["available_grams"], 180)
        self.assertTrue(self.workflow.get_plan("user-1", "plan-reserve"))
        self.assertTrue(
            self.domain.list_tool_call_traces("user-1", "session-reserve") == []
        )
        state["final_result"] = {"status": "best_effort"}
        asyncio.run(tools.save_plan(state))
        inventory = self.domain.list_inventory("user-1")[0]
        self.assertEqual(inventory["available_grams"], 300)

    def test_inventory_reservation_expires_and_audit_events_are_append_only(self) -> None:
        item = self.domain.upsert_inventory(
            "user-1", {"name": "rice", "quantity": 500, "unit": "g", "grams_estimate": 500}
        )
        reservation = self.domain.reserve_inventory(
            "user-1", [{"name": "rice", "grams": 200, "plan_id": "plan-expire"}]
        )[0]
        with self.domain._connection() as connection:
            connection.execute(
                "UPDATE inventory_reservations SET expires_at=datetime('now','-1 minute') WHERE reservation_id=?",
                (reservation["reservation_id"],),
            )
        inventory = self.domain.list_inventory("user-1")[0]
        self.assertEqual(inventory["available_grams"], 500)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.domain._connection() as connection:
                connection.execute("DELETE FROM inventory_events WHERE user_id=?", ("user-1",))

    def test_procurement_uses_current_graph_state_before_plan_snapshot_is_saved(self) -> None:
        async def exercise() -> dict[str, Any]:
            graph_tools = OrchestratorTools(self.workflow)
            return await graph_tools.call_procurement_agent({
                "user_id": "user-1",
                "session_id": "session-current",
                "plan_id": "plan-current",
                "candidate": {"ingredients": []},
                "verification": {"passed": False, "ingredients": [], "missing_capacity": {"protein_g": 11}},
            })

        result = asyncio.run(exercise())
        self.assertEqual(result["status"], "approval_required")
        self.assertEqual(result["products"][0]["ingredient"], "cottage cheese")

    def test_verifier_accepts_missing_or_null_procurement_result(self) -> None:
        async def exercise() -> dict[str, Any]:
            client = AsyncMock()
            client.lookup = AsyncMock(return_value={
                "name": "fixture food",
                "per_100g": {"kcal": 120, "protein_g": 23, "carbs_g": 0, "fat_g": 2.6},
                "source": "test fixture",
                "source_id": "fixture",
                "verified": True,
            })
            from meal_agent.agents.nutrition import NutritionAgent
            tools = OrchestratorTools(self.workflow)
            tools.nutrition_agent = NutritionAgent(self.domain, client)
            state = {
                "user_id": "user-1",
                "target": {"protein_g_min": 20},
                "candidate": {"ingredients": [{"name": "fixture food", "min_g": 100, "max_g": 100}]},
                "profile": {},
                "constraints": {},
                "procurement_result": None,
            }
            return await tools.call_verifier_agent(state)

        result = asyncio.run(exercise())
        self.assertIn(result["status"], {"verified", "unmet"})
        self.assertNotEqual(result["status"], "error")

    def test_graph_procurement_resume_executes_mock_order_after_yes_without_stored_token(self) -> None:
        async def exercise() -> tuple[dict[str, Any], dict[str, Any]]:
            agent = ProcurementAgent(
                self.domain, self.workflow, MockRetailer(), mock_checkout_enabled=True
            )
            tools = OrchestratorTools(self.workflow, procurement_agent=agent)
            state: dict[str, Any] = {
                "user_id": "user-1",
                "session_id": "session-graph-procurement",
                "plan_id": "plan-graph-procurement",
                "candidate": {"ingredients": []},
                "verification": {
                    "passed": False,
                    "ingredients": [],
                    "missing_capacity": {"protein_g": 9},
                },
                "procurement_result": None,
                "user_answer": "",
            }
            approval = await tools.call_procurement_agent(state)
            self.assertEqual(approval["status"], "approval_required")
            state["procurement_result"] = {
                key: value for key, value in approval.items() if key != "approval_token"
            }
            state["user_answer"] = "yes"
            order = await tools.call_procurement_agent(state)
            return approval, order

        approval, order = asyncio.run(exercise())
        self.assertIn("approval_token", approval)
        self.assertEqual(order["status"], "mock_ordered")
        self.assertTrue(order["order"]["delivered_inventory"])
        self.assertTrue(any(item["name"] == "cottage cheese" for item in self.domain.list_inventory("user-1")))

    def test_inventory_clarification_answer_is_persisted_then_full_inventory_is_reloaded(self) -> None:
        tools = OrchestratorTools(self.workflow)
        state = {
            "user_id": "user-1",
            "asked_from_action": "inventory",
            "user_answer": (
                "I have 1 kg of chicken breast, 100 eggs, 20 kg of basmati rice, "
                "butter, 20 pieces of 100 gm Greek yogurt"
            ),
            "inventory_result": {
                "items": [{"name": "butter", "confidence": 0.25, "grams_estimate": None}]
            },
            "pending_inventory_item": {"name": "butter"},
        }
        partial = asyncio.run(tools.call_inventory_agent(state))
        self.assertEqual(partial["status"], "needs_user")
        saved_names = {item["name"] for item in self.domain.list_inventory("user-1")}
        self.assertEqual(saved_names, {"chicken breast", "eggs", "basmati rice", "greek yogurt"})
        self.assertEqual(next(item for item in self.domain.list_inventory("user-1") if item["name"] == "greek yogurt")["grams_estimate"], 2000)

        state["inventory_result"] = {"items": partial["items"]}
        state["pending_inventory_item"] = next(
            item for item in partial["items"] if item["name"] == "butter"
        )
        state["user_answer"] = "100 grams"
        resolved = asyncio.run(tools.call_inventory_agent(state))
        self.assertEqual(resolved["status"], "ok")
        self.assertEqual(
            {item["name"] for item in resolved["items"]},
            {"chicken breast", "eggs", "basmati rice", "greek yogurt", "butter"},
        )
        self.assertEqual(
            next(item for item in resolved["items"] if item["name"] == "butter")["grams_estimate"],
            100,
        )

    def test_tracking_agent_sets_daily_goals_and_dietary_profile(self) -> None:
        agent = TrackingAgent(self.domain, self.workflow)
        asyncio.run(agent.set_targets("user-1", {"kcal": 2100, "protein_g": 130}))
        asyncio.run(agent.set_profile("user-1", {"diet": "vegetarian", "allergies": ["peanut"]}))
        context = self.domain.get_user_context("user-1")
        self.assertEqual(context["profile"]["diet"], "vegetarian")
        self.assertEqual(context["profile"]["allergies"], ["peanut"])
        status = self.domain.nutrition_status("user-1")
        self.assertEqual(status["remaining_kcal"], 2100)

    def test_procurement_uses_mock_catalog_and_server_stored_approval(self) -> None:
        plan_id = "plan-1"
        self.workflow.save_plan(
            {
                "user_id": "user-1",
                "session_id": "session-1",
                "plan_id": plan_id,
                "candidate": {"ingredients": [{"name": "chicken", "min_g": 100}]},
                "verification": {
                    "passed": False,
                    "ingredients": [{"name": "chicken", "grams": 150}],
                    "missing_capacity": {},
                },
                "final_result": {},
            }
        )
        agent = ProcurementAgent(self.domain, self.workflow, MockRetailer())
        first = asyncio.run(agent.shop_for_meal("user-1", plan_id))
        self.assertEqual(first["status"], "approval_required")
        self.assertTrue(first["mock"])
        self.assertEqual(first["products"][0]["id"], "mock-chicken-500")
        denied = asyncio.run(
            agent.shop_for_meal(
                "user-1",
                plan_id,
                confirm=True,
                approval_token=first["approval_token"],
                cart_id=first["cart_id"],
            )
        )
        self.assertEqual(denied["status"], "checkout_disabled")
        retry = asyncio.run(
            agent.shop_for_meal(
                "user-1",
                plan_id,
                confirm=True,
                approval_token=first["approval_token"],
                cart_id=first["cart_id"],
            )
        )
        self.assertEqual(retry["status"], "approval_invalid")

    def test_mock_approval_checkout_delivers_inventory_for_final_reverification(self) -> None:
        plan_id = "plan-protein-gap"
        self.workflow.save_plan({
            "user_id": "user-1",
            "session_id": "session-protein-gap",
            "plan_id": plan_id,
            "candidate": {"ingredients": []},
            "verification": {
                "passed": False,
                "ingredients": [],
                "missing_capacity": {"protein_g": 11},
            },
            "final_result": {},
        })
        agent = ProcurementAgent(
            self.domain, self.workflow, MockRetailer(), mock_checkout_enabled=True
        )
        cart = asyncio.run(agent.shop_for_meal("user-1", plan_id))
        self.assertEqual(cart["status"], "approval_required")
        self.assertTrue(any(step["tool"].startswith("product_search") for step in cart["trace_steps"]))
        self.assertEqual(cart["products"][0]["ingredient"], "cottage cheese")
        completed = asyncio.run(agent.shop_for_meal(
            "user-1", plan_id, confirm=True,
            approval_token=cart["approval_token"], cart_id=cart["cart_id"],
        ))
        self.assertEqual(completed["status"], "mock_ordered")
        stock = self.domain.list_inventory("user-1")
        cottage_cheese = next(item for item in stock if item["name"] == "cottage cheese")
        self.assertEqual(cottage_cheese["grams_estimate"], 250)
        self.assertTrue(completed["order"]["cart"]["products"])

    def test_graph_explicit_yes_consumes_server_side_pending_approval(self) -> None:
        plan_id = "plan-graph-yes"
        self.workflow.save_plan({
            "user_id": "user-1",
            "session_id": "session-graph-yes",
            "plan_id": plan_id,
            "candidate": {"ingredients": []},
            "verification": {"passed": False, "ingredients": [], "missing_capacity": {"protein_g": 5}},
            "final_result": {},
        })
        agent = ProcurementAgent(
            self.domain, self.workflow, MockRetailer(), mock_checkout_enabled=True
        )
        cart = asyncio.run(agent.shop_for_meal("user-1", plan_id))
        result = asyncio.run(agent.shop_for_meal(
            "user-1", plan_id, confirm=True, cart_id=cart["cart_id"],
            explicit_confirmation=True,
        ))
        self.assertEqual(result["status"], "mock_ordered")
        self.assertTrue(result["order"]["delivered_inventory"])

    def test_nutrition_agent_returns_gap_feedback_from_real_solver(self) -> None:
        client = AsyncMock()
        client.lookup = AsyncMock(return_value={
            "name": "plain chicken",
            "per_100g": {"kcal": 120, "protein_g": 23, "carbs_g": 0, "fat_g": 2.6},
            "source": "USDA fixture",
            "source_id": "fixture",
            "verified": True,
        })
        nutrition = NutritionAgent(self.domain, client)
        result = asyncio.run(
            nutrition.verify_candidate(
                "user-1",
                [{"name": "plain chicken", "min_g": 0, "max_g": 50, "available_g": 50}],
                NutritionTarget(protein_g_min=40),
                {},
            )
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["missing_capacity"]["protein_g"], 28.5)
        self.assertGreater(result.get("slack", {}).get("protein_g", {}).get("under", 0), 0)
        self.assertTrue(result["suggestions"])

    def test_solver_reports_atwater_and_slack_diagnostics(self) -> None:
        result = solve_portions(
            [{
                "name": "fixture food",
                "per_100g": {"kcal": 250, "protein_g": 10, "carbs_g": 10, "fat_g": 10},
                "min_g": 0,
                "max_g": 100,
                "in_stock": True,
                "available_g": 100,
            }],
            NutritionTarget(kcal=100, protein_g=10, carbs_g=10, fat_g=10),
        )
        self.assertIsNotNone(result["atwater"])
        self.assertFalse(result["target_atwater"]["within_tolerance"])
        self.assertTrue(any("Atwater" in warning for warning in result["warnings"]))

    def test_composer_uses_expiring_food_then_adds_unused_in_stock_protein_on_feedback(self) -> None:
        pantry = [
            {"name": "spinach", "available_grams": 150, "expiry": "2026-10-03"},
            {"name": "chicken", "available_grams": 300},
            {"name": "eggs", "available_grams": 300},
            {"name": "greek yogurt", "available_grams": 400},
            {"name": "olive oil", "available_grams": 30},
            {"name": "rice", "available_grams": 500},
        ]
        first = _fallback_candidate("high-protein dinner", pantry, {}, [])
        first_names = {item.name for item in first.ingredients}
        self.assertIn("spinach", first_names)
        self.assertNotIn("greek yogurt", first_names)
        repaired = _fallback_candidate(
            "high-protein dinner",
            pantry,
            {},
            [],
            {
                "candidate_ingredients": [item.model_dump() for item in first.ingredients],
                "missing_capacity": {"protein_g": 11},
            },
        )
        self.assertIn("greek yogurt", {item.name for item in repaired.ingredients})

    def test_cooked_portions_use_explicit_yield_and_claims_are_recomputed(self) -> None:
        client = AsyncMock()
        client.lookup = AsyncMock(return_value={
            "name": "chicken breast raw",
            "per_100g": {"kcal": 106, "protein_g": 22.5, "carbs_g": 0, "fat_g": 1.9},
            "source": "USDA fixture",
            "source_id": "fixture",
            "verified": True,
        })
        nutrition = NutritionAgent(self.domain, client)
        solved = asyncio.run(
            nutrition.solve_portions(
                [{
                    "name": "chicken breast",
                    "portion_basis": "cooked",
                    "cooking_method": "grill",
                    "min_g": 50,
                    "max_g": 100,
                }],
                NutritionTarget(protein_g_min=30),
            )
        )
        self.assertTrue(solved["passed"])
        self.assertGreaterEqual(solved["totals"]["protein_g"], 30)
        claim = asyncio.run(
            nutrition.verify_claim(
                "This serving has 45 g protein and 212 kcal.",
                [{"name": "chicken breast", "grams": 100}],
            )
        )
        self.assertEqual(claim["claims_found"], 2)
        self.assertFalse(claim["passed"])


if __name__ == "__main__":
    unittest.main()
