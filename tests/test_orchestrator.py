"""Tests for bounded routing, clarification resume, persistence, and finish safety."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from meal_agent.graph.guards import finish
from meal_agent.graph import supervisor
from meal_agent.graph.supervisor import PlannerDecision, build_meal_graph, run_plan_meal
from meal_agent.graph.tools import OrchestratorTools
from meal_agent.schemas import NutritionTarget
from meal_agent.storage.workflow_store import WorkflowStore


class FakeTools:
    def __init__(self, *, ask_first: bool = False) -> None:
        self.ask_first = ask_first
        self.state: dict[str, Any] | None = None
        self.calls: list[str] = []
        self.composer_calls = 0
        self.verifier_calls = 0
        self.composer_contexts: list[tuple[dict[str, Any], dict[str, Any]]] = []

    async def get_session_state(self, user_id: str, session_id: str) -> dict[str, Any] | None:
        if self.state and self.state["user_id"] == user_id and self.state["session_id"] == session_id:
            return copy.deepcopy(self.state)
        return None

    async def get_user_context(self, user_id: str) -> dict[str, Any]:
        self.calls.append("get_user_context")
        return {"profile": {"diet": "none"}, "macro_status": {"kcal_remaining": 1000}}

    async def save_plan(self, state: dict[str, Any]) -> None:
        self.state = copy.deepcopy(state)
        self.calls.append("save_plan")

    async def call_inventory_agent(self, state: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("inventory")
        return {"status": "ok", "items": [{"name": "chicken", "quantity_g": 300}]}

    async def call_composer_agent(self, state: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("compose")
        self.composer_calls += 1
        self.composer_contexts.append(
            (copy.deepcopy(state.get("profile", {})), copy.deepcopy(state.get("macro_status", {})))
        )
        if self.ask_first and self.composer_calls == 1:
            return {"status": "needs_user", "question": "Is the chicken raw or cooked?"}
        return {
            "status": "ok",
            "ingredients": [{"name": "chicken", "grams": 120}],
        }

    async def call_verifier_agent(self, state: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("verify")
        self.verifier_calls += 1
        passed = self.verifier_calls > 1
        return {
            "status": "verified" if passed else "unmet",
            "passed": passed,
            "verification": {
                "passed": passed,
                "totals": {"kcal": 200, "protein_g": 46, "carbs_g": 0, "fat_g": 4},
                "deviation": {"protein_g_min": 1 if passed else -4},
                "violations": [],
                "suggestions": [] if passed else ["Increase protein portion."],
            },
            "ingredients": [{"name": "chicken", "grams": 120}],
        }

    async def call_procurement_agent(self, state: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("procure")
        return {"status": "not_configured"}

    async def ask_user(self, state: dict[str, Any], question: str) -> dict[str, Any]:
        state["pending_question"] = question
        state["status"] = "needs_user"
        return {"status": "needs_user", "question": question, "spoken_summary": question}

    def finish(self, verified: bool, result: dict[str, Any]) -> dict[str, Any]:
        return finish(verified, result)


class OrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router_patch = patch.object(supervisor, "_llm_decision", new=AsyncMock(return_value=None))
        self.router_patch.start()
        self.addCleanup(self.router_patch.stop)

    def test_repairs_candidate_then_finishes_only_after_verification(self) -> None:
        import asyncio

        tools = FakeTools()
        result = asyncio.run(
            run_plan_meal(
                "user-1",
                NutritionTarget(protein_g_min=45),
                meal_type="dinner",
                tools=tools,
                max_repairs=1,
            )
        )
        self.assertEqual(result["status"], "verified")
        self.assertTrue(result["verification"]["passed"])
        self.assertEqual(tools.calls.count("inventory"), 1)
        self.assertEqual(tools.calls.count("compose"), 2)
        self.assertEqual(tools.calls.count("verify"), 2)
        self.assertNotIn("procure", tools.calls)
        self.assertEqual(tools.composer_contexts[0][0], {"diet": "none"})
        self.assertEqual(tools.composer_contexts[0][1], {"kcal_remaining": 1000})
        self.assertTrue(any(task["status"] == "completed" for task in result["plan"]))

    def test_compiled_stategraph_contains_orchestrator_specialists_and_terminal_nodes(self) -> None:
        graph = build_meal_graph(FakeTools())
        node_names = set(graph.get_graph().nodes)
        self.assertTrue(
            {"orchestrator", "inventory", "compose", "verify", "procure", "ask_user", "finish", "terminate"}
            <= node_names
        )

    def test_unconfigured_services_return_honest_failure_not_a_meal(self) -> None:
        import asyncio

        class UnconfiguredTools(FakeTools):
            async def call_inventory_agent(self, state: dict[str, Any]) -> dict[str, Any]:
                self.calls.append("inventory")
                return {"status": "not_configured"}

        result = asyncio.run(
            run_plan_meal("user-1", NutritionTarget(kcal=600), tools=UnconfiguredTools())
        )
        self.assertEqual(result["status"], "not_configured")
        self.assertNotIn("meal", result)

    def test_verifier_error_does_not_trigger_a_purchase_approval(self) -> None:
        import asyncio

        class VerifierErrorTools(FakeTools):
            async def call_verifier_agent(self, state: dict[str, Any]) -> dict[str, Any]:
                self.calls.append("verify")
                return {
                    "status": "error",
                    "passed": False,
                    "reason": "USDA request failed",
                    "spoken_summary": "Nutrition could not be verified.",
                }

        tools = VerifierErrorTools()
        result = asyncio.run(
            run_plan_meal(
                "user-1",
                NutritionTarget(kcal=700, protein_g_min=60, carbs_g_max=30),
                tools=tools,
            )
        )
        self.assertNotEqual(result["status"], "needs_user")
        self.assertNotIn("procure", tools.calls)
        self.assertNotIn("meal", result)

    def test_budget_exhaustion_is_bounded_and_does_not_claim_success(self) -> None:
        import asyncio

        tools = FakeTools()
        result = asyncio.run(
            run_plan_meal(
                "user-1",
                NutritionTarget(kcal=600),
                tools=tools,
                max_tool_calls=6,
            )
        )
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertNotIn("meal", result)
        self.assertLessEqual(tools.state["tool_calls"], 6)
        self.assertEqual(tools.state["status"], "budget_exhausted")

    def test_repair_budget_ends_with_diagnostics_without_unverified_meal(self) -> None:
        import asyncio

        class AlwaysFailTools(FakeTools):
            async def call_verifier_agent(self, state: dict[str, Any]) -> dict[str, Any]:
                self.calls.append("verify")
                self.verifier_calls += 1
                deviation = -5 if self.verifier_calls == 1 else -15
                return {
                    "status": "unmet",
                    "passed": False,
                    "verification": {
                        "passed": False,
                        "totals": {"kcal": 400, "protein_g": 30, "carbs_g": 20, "fat_g": 10},
                        "deviation": {"protein_g_min": deviation},
                        "violations": [],
                        "suggestions": ["Add a verified protein source."],
                    },
                }

        tools = AlwaysFailTools()
        result = asyncio.run(
            run_plan_meal(
                "user-1",
                NutritionTarget(protein_g_min=45),
                tools=tools,
                max_repairs=1,
            )
        )
        self.assertEqual(result["status"], "best_effort")
        self.assertNotIn("meal", result)
        self.assertEqual(result["closest_attempt"]["deviation"]["protein_g_min"], -5)
        self.assertEqual(tools.calls.count("procure"), 1)

    def test_router_cannot_finish_early_and_bypass_guard(self) -> None:
        import asyncio

        async def premature_finish(state: Any, allowed: list[str]) -> PlannerDecision:
            return PlannerDecision(action="finish", rationale="Try to skip verification")

        result = asyncio.run(
            run_plan_meal(
                "user-1",
                NutritionTarget(kcal=600),
                tools=FakeTools(),
                decision_provider=premature_finish,
            )
        )
        self.assertEqual(result["status"], "unable_to_plan")
        self.assertNotIn("meal", result)

    def test_router_can_skip_inventory_for_a_pantry_independent_request(self) -> None:
        import asyncio

        async def choose_without_pantry(state: Any, allowed: list[str]) -> PlannerDecision:
            if state.get("inventory_result") is None and "compose" in allowed:
                return PlannerDecision(action="compose", rationale="Request is pantry-independent")
            return PlannerDecision(action=allowed[0])

        tools = FakeTools()
        result = asyncio.run(
            run_plan_meal(
                "user-1",
                NutritionTarget(protein_g_min=45),
                request="Suggest a high-protein meal; inventory is not relevant.",
                tools=tools,
                decision_provider=choose_without_pantry,
            )
        )
        self.assertEqual(result["status"], "verified")
        self.assertNotIn("inventory", tools.calls)
        self.assertTrue(
            any(task["task"] == "load_inventory" and task["status"] == "skipped" for task in result["plan"])
        )

    def test_clarification_is_persisted_and_resumed(self) -> None:
        import asyncio

        tools = FakeTools(ask_first=True)
        first = asyncio.run(
            run_plan_meal("user-1", NutritionTarget(protein_g_min=40), tools=tools)
        )
        self.assertEqual(first["status"], "needs_user")
        self.assertEqual(first["question"], "Is the chicken raw or cooked?")

        resumed = asyncio.run(
            run_plan_meal(
                "user-1",
                NutritionTarget(protein_g_min=40),
                session_id=first["session_id"],
                answer="Raw",
                tools=tools,
            )
        )
        self.assertEqual(resumed["status"], "verified")
        self.assertGreaterEqual(tools.composer_calls, 2)

    def test_inventory_answer_from_composer_question_is_written_to_sqlite_and_workflow_resumes(self) -> None:
        import asyncio

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "inventory-resume.sqlite3"
            toolbox = OrchestratorTools(WorkflowStore(database))
            composer_calls = 0
            observed_inventory: list[str] = []

            async def compose(state: dict[str, Any]) -> dict[str, Any]:
                nonlocal composer_calls
                composer_calls += 1
                names = [item["name"] for item in (state.get("inventory_result") or {}).get("items", [])]
                observed_inventory.extend(names)
                if not names:
                    return {
                        "status": "needs_user",
                        "question": "Which foods or ingredients do you have on hand?",
                    }
                return {"status": "ok", "ingredients": [{"name": "chicken breast", "min_g": 50, "max_g": 200}]}

            async def verify(state: dict[str, Any]) -> dict[str, Any]:
                return {
                    "status": "verified",
                    "passed": True,
                    "verification": {
                        "passed": True,
                        "totals": {"kcal": 500, "protein_g": 50, "carbs_g": 20, "fat_g": 10},
                        "deviation": {},
                        "violations": [],
                        "suggestions": [],
                    },
                    "ingredients": [{"name": "chicken breast", "grams": 150}],
                }

            toolbox.call_composer_agent = AsyncMock(side_effect=compose)
            toolbox.call_verifier_agent = AsyncMock(side_effect=verify)
            session_id = "pantry-clarification-session"
            target = NutritionTarget(protein_g_min=40)

            first = asyncio.run(run_plan_meal("user-1", target, session_id=session_id, tools=toolbox))
            self.assertEqual(first["status"], "needs_user", first)
            saved = WorkflowStore(database).get_session_state("user-1", session_id)
            self.assertEqual(saved["asked_from_action"], "inventory")

            second = asyncio.run(run_plan_meal(
                "user-1", target, session_id=session_id,
                answer=("I have 1 kg of chicken breast, 100 eggs, 20 kg of basmati rice, "
                        "butter, 20 pieces of 100 gm Greek yogurt"),
                tools=toolbox,
            ))
            self.assertEqual(second["status"], "needs_user")
            pantry = toolbox.domain_store.list_inventory("user-1")
            self.assertEqual(
                {item["name"] for item in pantry},
                {"chicken breast", "eggs", "basmati rice", "greek yogurt"},
            )
            self.assertEqual(next(item for item in pantry if item["name"] == "greek yogurt")["grams_estimate"], 2000)

            final = asyncio.run(run_plan_meal(
                "user-1", target, session_id=session_id, answer="100 grams", tools=toolbox
            ))
            saved_after = WorkflowStore(database).get_session_state("user-1", session_id)
            self.assertEqual(final["status"], "verified", {"result": final, "observed_inventory": observed_inventory, "composer_calls": toolbox.call_composer_agent.await_count, "saved_after": saved_after})
            self.assertEqual(
                {item["name"] for item in toolbox.domain_store.list_inventory("user-1")},
                {"chicken breast", "eggs", "basmati rice", "greek yogurt", "butter"},
            )
            self.assertIn("chicken breast", observed_inventory)

    def test_real_sqlite_checkpointer_persists_and_resumes_graph_state(self) -> None:
        import asyncio

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "graph.sqlite3"
            toolbox = OrchestratorTools(WorkflowStore(database))
            toolbox.get_user_context = AsyncMock(return_value={"profile": {}, "macro_status": {}})
            toolbox.call_inventory_agent = AsyncMock(
                return_value={"status": "ok", "items": [{"name": "chicken", "grams_estimate": 500, "available_grams": 500}]}
            )
            toolbox.call_composer_agent = AsyncMock(side_effect=[
                {"status": "needs_user", "question": "Is the chicken raw or cooked?"},
                {"status": "ok", "ingredients": [{"name": "chicken", "min_g": 50, "max_g": 200}]},
            ])
            toolbox.call_verifier_agent = AsyncMock(return_value={
                "status": "verified",
                "passed": True,
                "verification": {
                    "passed": True,
                    "totals": {"kcal": 200, "protein_g": 45, "carbs_g": 0, "fat_g": 4},
                    "deviation": {"protein_g_min": 0},
                    "violations": [],
                    "suggestions": [],
                },
                "ingredients": [{"name": "chicken", "grams": 120}],
            })
            session = "checkpoint-resume-session"

            first = asyncio.run(
                run_plan_meal(
                    "user-1",
                    NutritionTarget(protein_g_min=40),
                    session_id=session,
                    tools=toolbox,
                )
            )
            self.assertEqual(first["status"], "needs_user")
            saved = WorkflowStore(database).get_session_state("user-1", session)
            self.assertEqual(saved["status"], "needs_user")
            self.assertTrue(saved["decision_history"])

            resumed = asyncio.run(
                run_plan_meal(
                    "user-1",
                    NutritionTarget(protein_g_min=40),
                    session_id=session,
                    answer="Raw",
                    tools=toolbox,
                )
            )
            self.assertEqual(resumed["status"], "verified")
            self.assertTrue(resumed["verification"]["passed"])
            self.assertEqual(toolbox.call_composer_agent.await_count, 2)
            self.assertEqual(toolbox.call_verifier_agent.await_count, 1)

    def test_example_trace_repairs_in_stock_then_approves_mock_procurement_and_reverifies(self) -> None:
        import asyncio

        class ExampleTools(FakeTools):
            def __init__(self) -> None:
                super().__init__()
                self.verifications: list[dict[str, Any]] = []

            async def call_inventory_agent(self, state: dict[str, Any]) -> dict[str, Any]:
                self.calls.append("inventory_list+expiring_soon")
                return {
                    "status": "ok",
                    "items": [
                        {"name": "chicken", "available_grams": 300, "expiry": None},
                        {"name": "eggs", "available_grams": 300, "expiry": None},
                        {"name": "spinach", "available_grams": 150, "expiry": "soon"},
                        {"name": "olive oil", "available_grams": 30, "expiry": None},
                        {"name": "greek yogurt", "available_grams": 400, "expiry": None},
                    ],
                    "expiring_soon": [{"name": "spinach", "days": 2}],
                }

            async def call_composer_agent(self, state: dict[str, Any]) -> dict[str, Any]:
                self.calls.append("compose")
                self.composer_calls += 1
                ingredients = ["chicken", "eggs", "spinach", "olive oil"]
                if self.composer_calls > 1:
                    ingredients.append("greek yogurt")
                return {
                    "status": "ok",
                    "name": "Chicken and egg scramble with spinach",
                    "ingredients": [{"name": item, "min_g": 0, "max_g": 250} for item in ingredients],
                }

            async def call_verifier_agent(self, state: dict[str, Any]) -> dict[str, Any]:
                self.calls.append("verify")
                self.verifier_calls += 1
                if self.verifier_calls == 1:
                    protein, passed = 49, False
                elif self.verifier_calls == 2:
                    protein, passed = 57, False
                else:
                    protein, passed = 60, True
                result = {
                    "status": "verified" if passed else "unmet",
                    "passed": passed,
                    "verification": {
                        "passed": passed,
                        "totals": {"kcal": 696 if self.verifier_calls < 3 else 696, "protein_g": protein, "carbs_g": 28, "fat_g": 31},
                        "deviation": {"protein_g_min": protein - 60, "kcal": -4},
                        "missing_capacity": {} if passed else {"protein_g": 60 - protein},
                        "violations": [],
                        "suggestions": [] if passed else ["Add a compact high-protein item."],
                    },
                    "ingredients": [{"name": name, "grams": 100} for name in ["chicken", "eggs", "spinach", "olive oil"]],
                }
                self.verifications.append(result)
                return result

            async def call_procurement_agent(self, state: dict[str, Any]) -> dict[str, Any]:
                self.calls.append("procure")
                previous = state.get("procurement_result") or {}
                if previous.get("status") == "approval_required":
                    if state.get("user_answer", "").casefold() == "yes":
                        return {"status": "mock_ordered", "order": {"cart": {"products": [{"ingredient": "cottage cheese", "pack_size_g": 250}]}}}
                    return {"status": "approval_declined"}
                return {
                    "status": "approval_required",
                    "cart_id": "cart-example",
                    "approval_token": "one-time-example-token",
                    "products": [{"name": "Cottage cheese, 250 g", "price": 2.79}],
                    "total": 2.79,
                    "mock": True,
                    "spoken_summary": "Review the mock cart and explicitly approve it.",
                }

        tools = ExampleTools()
        first = asyncio.run(
            run_plan_meal(
                "user-1",
                NutritionTarget(kcal=700, protein_g_min=60, carbs_g_max=30),
                meal_type="dinner",
                session_id="trace-example",
                tools=tools,
                max_repairs=1,
            )
        )
        self.assertEqual(first["status"], "needs_user")
        self.assertEqual(first["procurement"]["cart_id"], "cart-example")
        self.assertNotIn("approval_token", first["procurement"])
        self.assertEqual(tools.verifications[0]["verification"]["missing_capacity"]["protein_g"], 11)
        self.assertEqual(tools.verifications[1]["verification"]["missing_capacity"]["protein_g"], 3)

        final = asyncio.run(
            run_plan_meal(
                "user-1",
                NutritionTarget(kcal=700, protein_g_min=60, carbs_g_max=30),
                session_id="trace-example",
                answer="yes",
                tools=tools,
                max_repairs=1,
            )
        )
        self.assertEqual(final["status"], "verified")
        self.assertTrue(final["verification"]["passed"])
        agent_calls = [
            call for call in tools.calls
            if call in {"inventory_list+expiring_soon", "compose", "verify", "procure"}
        ]
        self.assertEqual(agent_calls, [
            "inventory_list+expiring_soon", "compose", "verify", "compose", "verify",
            "procure", "procure", "verify",
        ])

    def test_finish_requires_passed_verification_with_totals(self) -> None:
        with self.assertRaises(ValueError):
            finish(True, {"status": "verified", "verification": {"passed": True}})
        with self.assertRaises(ValueError):
            finish(
                False,
                {
                    "status": "best_effort",
                    "verification": {"passed": False, "totals": {}},
                },
            )

    def test_sqlite_session_state_persists_and_is_user_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workflow.sqlite3"
            store = WorkflowStore(path)
            state = {
                "user_id": "user-1",
                "session_id": "session-1",
                "plan_id": "plan-1",
                "status": "running",
                "plan": [],
            }
            store.save_plan(state)
            store.save_user_context(
                "user-1", {"diet": "vegetarian"}, {"kcal_remaining": 900}
            )
            reopened = WorkflowStore(path)
            self.assertEqual(reopened.get_session_state("user-1", "session-1"), state)
            self.assertIsNone(reopened.get_session_state("other-user", "session-1"))
            self.assertIsNone(reopened.get_plan("other-user", "plan-1"))
            self.assertEqual(
                reopened.get_user_context("user-1"),
                {"profile": {"diet": "vegetarian"}, "macro_status": {"kcal_remaining": 900}},
            )

    def test_redis_session_cache_uses_user_hashed_keys_and_expiry(self) -> None:
        class MemoryRedis:
            def __init__(self) -> None:
                self.values: dict[str, str] = {}
                self.expirations: dict[str, int] = {}

            def get(self, key: str) -> str | None:
                return self.values.get(key)

            def setex(self, key: str, ttl: int, value: str) -> None:
                self.values[key] = value
                self.expirations[key] = ttl

            def close(self) -> None:
                return None

        import json

        with tempfile.TemporaryDirectory() as directory:
            redis = MemoryRedis()
            store = WorkflowStore(
                Path(directory) / "redis-session.sqlite3",
                redis_url="redis://fake",
                session_ttl_seconds=7200,
                redis_client_factory=lambda: redis,
            )
            state = {
                "user_id": "user-a",
                "session_id": "session-a",
                "plan_id": "plan-a",
                "status": "needs_user",
            }
            store.save_plan(state)
            self.assertEqual(store.get_session_state("user-a", "session-a"), state)
            key = next(iter(redis.values))
            self.assertNotIn("user-a", key)
            self.assertNotIn("session-a", key)
            self.assertEqual(redis.expirations[key], 7200)

    def test_sqlite_session_snapshot_expires_after_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "expired.sqlite3"
            store = WorkflowStore(path, redis_url="", session_ttl_seconds=60)
            state = {
                "user_id": "user-a",
                "session_id": "session-expired",
                "plan_id": "plan-expired",
                "status": "needs_user",
            }
            store.save_plan(state)
            with store._connection() as connection:
                connection.execute(
                    "UPDATE workflow_sessions SET updated_at=datetime('now','-2 minutes') WHERE user_id=? AND session_id=?",
                    ("user-a", "session-expired"),
                )
            self.assertIsNone(store.get_session_state("user-a", "session-expired"))

    def test_workflow_snapshots_redact_approval_bearer_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = WorkflowStore(Path(directory) / "redact.sqlite3", redis_url="")
            state = {
                "user_id": "user-a",
                "session_id": "session-a",
                "plan_id": "plan-a",
                "status": "needs_user",
                "procurement_result": {
                    "status": "approval_required",
                    "approval_token": "sensitive-example",
                },
            }
            store.save_plan(state)
            saved = store.get_session_state("user-a", "session-a")
            self.assertEqual(saved["procurement_result"]["approval_token"], "[REDACTED]")


if __name__ == "__main__":
    unittest.main()
