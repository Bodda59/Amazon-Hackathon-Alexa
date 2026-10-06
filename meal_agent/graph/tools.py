"""Callable orchestrator tools for the specialist agents and workflow state."""

from __future__ import annotations

import asyncio
from typing import Any

from meal_agent.agents.composer import propose_meal
from meal_agent.agents.inventory import InventoryAgent
from meal_agent.agents.procurement import ProcurementAgent
from meal_agent.agents.nutrition import NutritionAgent
from meal_agent.graph.guards import finish as finish_guard
from meal_agent.schemas import NutritionTarget
from meal_agent.storage.domain_store import DomainStore
from meal_agent.storage.workflow_store import WorkflowStore

import re

def _normalize_reply(text: str) -> str:
    return re.sub(r"[^a-z ]", "", text.casefold()).strip()

_YES = {"yes", "yes please", "yes order it", "yes place the order", "place the order",
        "go ahead", "go ahead and order", "approve", "approved", "confirm", "ok", "okay", "sure"}
_NO = {"no", "nope", "cancel", "decline", "stop", "no thanks", "dont"}


class OrchestratorTools:
    """Toolbox used by the supervisor; external side effects remain guarded."""

    def __init__(
        self,
        store: WorkflowStore,
        procurement_agent: ProcurementAgent | None = None,
    ) -> None:
        self.store = store
        self.domain_store = DomainStore(store.database_path)
        self.inventory_agent = InventoryAgent(self.domain_store)
        self.nutrition_agent = NutritionAgent(self.domain_store)
        self.procurement_agent = procurement_agent or ProcurementAgent(
            self.domain_store, self.store
        )

    async def call_inventory_agent(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("asked_from_action") == "inventory" and state.get("user_answer"):
            uncertain = next(
                (
                    item
                    for item in (state.get("inventory_result") or {}).get("items", [])
                    if float(item.get("confidence", 1.0)) < 0.5
                    or item.get("grams_estimate") is None
                ),
                None,
            )
            uncertain = uncertain or state.get("pending_inventory_item")
            free_text = state["user_answer"]
            if uncertain and str(uncertain.get("name", "")).casefold() not in free_text.casefold():
                free_text = f"{free_text} {uncertain.get('name', '')}"
            update = await self.inventory_agent.manage_inventory(
                state["user_id"], "update", free_text=free_text
            )
            if update.get("status") != "ok":
                return update
            items, expiring = await asyncio.gather(
                self.inventory_agent.manage_inventory(state["user_id"], "list"),
                self.inventory_agent.expiring_soon(state["user_id"], days=3),
            )
            items["expiring_soon"] = expiring
            items["trace_steps"] = [
                {"tool": "inventory_upsert_from_clarification", "inputs": {"item": uncertain.get("name") if uncertain else None}, "output": {"status": update.get("status"), "saved_count": len(update.get("items", []))}},
                {"tool": "inventory_list", "inputs": {}, "output": {"count": len(items.get("items", []))}},
                {"tool": "expiring_soon", "inputs": {"days": 3}, "output": {"items": expiring}},
            ]
            return items
        result, expiring = await asyncio.gather(
            self.inventory_agent.manage_inventory(state["user_id"], "list"),
            self.inventory_agent.expiring_soon(state["user_id"], days=3),
        )
        result["expiring_soon"] = expiring
        result["trace_steps"] = [
            {"tool": "inventory_list", "inputs": {}, "output": {"count": len(result.get("items", []))}},
            {"tool": "expiring_soon", "inputs": {"days": 3}, "output": {"items": expiring}},
        ]
        uncertain = next(
            (
                item
                for item in result.get("items", [])
                if float(item.get("confidence", 1.0)) < 0.5
                or item.get("grams_estimate") is None
            ),
            None,
        )
        if uncertain:
            result["question"] = f"How much {uncertain.get('name', 'of this item')} do you have? Its inventory quantity is uncertain."
            result["clarification_needed"] = True
        return result

    async def call_composer_agent(self, state: dict[str, Any]) -> dict[str, Any]:
        inventory_result = state.get("inventory_result", {})
        preferences = await asyncio.to_thread(
            self.domain_store.get_preference_summary, state["user_id"]
        )
        constraints = dict(state.get("constraints") or {})
        constraints["nutrition_target"] = state.get("target")
        if preferences:
            constraints["learned_preferences"] = preferences
        feedback = dict(state.get("verification") or {})
        feedback["candidate_ingredients"] = (state.get("candidate") or {}).get("ingredients", [])
        return await propose_meal(
            request=state.get("request", ""),
            inventory=inventory_result.get("items", []),
            constraints=constraints,
            feedback=feedback,
            user_id=state["user_id"],
            store=self.domain_store,
        )

    async def call_verifier_agent(self, state: dict[str, Any]) -> dict[str, Any]:
        target = NutritionTarget.model_validate(state["target"])
        candidate = state.get("candidate") or {}
        candidate_items = [dict(item) for item in candidate.get("ingredients", [])]
        procurement_result = state.get("procurement_result") or {}
        if procurement_result.get("status") == "mock_ordered":
            cart = (procurement_result.get("order") or {}).get("cart") or {}
            for product in cart.get("products", []):
                ingredient = str(product.get("ingredient", "")).strip()
                if ingredient and not any(
                    ingredient.casefold() == str(item.get("name", "")).casefold()
                    for item in candidate_items
                ):
                    candidate_items.append({
                        "name": ingredient,
                        "role": "protein" if ingredient in {"cottage cheese", "protein powder", "chicken", "tofu"} else "ingredient",
                        "min_g": 0,
                        "max_g": float(product.get("pack_size_g", 300)),
                    })
        stock = await asyncio.to_thread(self.domain_store.list_inventory, state["user_id"])
        own_reservations = state.get("inventory_reservations", [])
        for item in candidate_items:
            matches = [
                s for s in stock
                if s.get("grams_estimate") is not None
                and _names_match(str(s["name"]), str(item.get("name", "")))
            ]
            if matches:
                matched_names = {str(m["name"]).casefold() for m in matches}
                own_reserved = sum(
                    float(r.get("grams", 0)) for r in own_reservations
                    if str(r.get("name", "")).casefold() in matched_names
                )
                item["available_g"] = sum(float(m.get("available_grams", 0)) for m in matches) + own_reserved
                item["in_stock"] = True
        return await self.nutrition_agent.verify_candidate(
            user_id=state["user_id"],
            ingredients=candidate_items,
            target=target,
            profile=state.get("profile", {}),
            constraints={
                **state.get("constraints", {}),
                **(
                    {"cooking_method": candidate["cooking_method"]}
                    if candidate.get("cooking_method")
                    else {}
                ),
            },
        )

    async def call_procurement_agent(self, state: dict[str, Any]) -> dict[str, Any]:
        agent = self.procurement_agent
        previous = state.get("procurement_result") or {}
        if previous.get("status") == "approval_required":
            answer = _normalize_reply(str(state.get("user_answer", "")))
            if answer in _YES:
                return await agent.shop_for_meal(
                    state["user_id"], state["plan_id"], confirm=True,
                    cart_id=previous.get("cart_id"), explicit_confirmation=True,
                )
            if answer in _NO:
                cart_id = previous.get("cart_id")
                if cart_id:
                    await asyncio.to_thread(self.domain_store.cancel_cart, state["user_id"], cart_id)
                await asyncio.to_thread(
                    self.domain_store.audit_procurement,
                    state["user_id"], cart_id, "approval_declined", {"answer": answer[:80]},
                )
                return {"status": "approval_declined",
                        "spoken_summary": "The mock cart was not approved and no order was placed."}
            # Unclear reply: keep the cart pending and ask again.
            return {**previous,
                    "question": "Please say yes to place the order, or no to cancel it.",
                    "spoken_summary": "Please say yes to place the order, or no to cancel it."}
            return await agent.shop_for_meal(
                state["user_id"], state["plan_id"], confirm=True,
                cart_id=previous.get("cart_id"),
                explicit_confirmation=True,
            )
        return await agent.shop_for_meal(
            state["user_id"], state["plan_id"], confirm=False, plan_state=state
        )

    async def get_session_state(self, user_id: str, session_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.store.get_session_state, user_id, session_id)

    async def get_user_context(self, user_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.domain_store.get_user_context, user_id)

    async def ask_user(self, state: dict[str, Any], question: str) -> dict[str, Any]:
        state["pending_question"] = question
        state["status"] = "needs_user"
        return {
            "status": "needs_user",
            "question": question,
            "session_id": state["session_id"],
            "spoken_summary": question,
        }

    async def save_plan(self, state: dict[str, Any]) -> None:
        plan = state.get("final_result", {})
        procurement_pending = (
            plan.get("status") == "needs_user"
            and (state.get("procurement_result") or {}).get("status") == "approval_required"
        )
        should_reserve = plan.get("status") == "verified" or procurement_pending
        verification = state.get("verification") or {}
        ingredients = plan.get("meal", {}).get("ingredients", []) if plan.get("status") == "verified" else verification.get("ingredients", [])
        if ingredients:
            await asyncio.to_thread(
                self.domain_store.save_meal_plan_items,
                state["user_id"], state["plan_id"], ingredients,
            )
        final_result = state.get("final_result", {})
        if should_reserve:
            try:
                if state.get("inventory_reservations_initialized"):
                    await asyncio.to_thread(
                        self.domain_store.release_inventory_reservations,
                        state["user_id"],
                        plan_id=state["plan_id"],
                    )
                inventory = await asyncio.to_thread(
                    self.domain_store.list_inventory, state["user_id"]
                )
                reservations: list[dict[str, Any]] = []
                for ingredient in ingredients:
                    grams = float(ingredient.get("grams", 0))
                    name = str(ingredient.get("name", ""))
                    match = next(
                        (
                            item
                            for item in inventory
                            if name.casefold() in str(item.get("name", "")).casefold()
                            or str(item.get("name", "")).casefold() in name.casefold()
                        ),
                        None,
                    )
                    if (
                        match is not None
                        and match.get("grams_estimate") is not None
                        and grams > 0
                        and grams <= float(match.get("available_grams", 0))
                    ):
                        reservations.append(
                            {"name": match["name"], "grams": grams, "plan_id": state["plan_id"]}
                        )
                state["inventory_reservations"] = await asyncio.to_thread(
                    self.domain_store.reserve_inventory, state["user_id"], reservations
                ) if reservations else []
                state["inventory_reservations_initialized"] = True
            except (KeyError, TypeError, ValueError, LookupError):
                # A verified meal remains valid when inventory is absent or approximate.
                state["inventory_reservations"] = []
                state["inventory_reservations_initialized"] = True
        elif plan.get("status") not in {"verified", "needs_user"} and state.get("inventory_reservations_initialized"):
            try:
                await asyncio.to_thread(
                    self.domain_store.release_inventory_reservations,
                    state["user_id"],
                    plan_id=state["plan_id"],
                )
            except (ValueError, LookupError):
                pass
        await asyncio.to_thread(self.store.save_plan, state)

    async def record_tool_trace(
        self,
        state: dict[str, Any],
        agent: str,
        tool_name: str,
        inputs: dict[str, Any],
        output: dict[str, Any],
        duration_ms: float,
    ) -> None:
        def redact(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    key: "[REDACTED]" if any(secret in key.casefold() for secret in ("token", "password", "api_key", "secret")) else redact(item)
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [redact(item) for item in value]
            if isinstance(value, str):
                return value[:4000]
            return value

        await asyncio.to_thread(
            self.domain_store.record_tool_call,
            user_id=state["user_id"],
            session_id=state["session_id"],
            plan_id=state.get("plan_id"),
            agent=agent,
            tool_name=tool_name,
            inputs=redact(inputs),
            output=redact(output),
            duration_ms=duration_ms,
        )

    def finish(self, verification_passed: bool, result: dict[str, Any]) -> dict[str, Any]:
        """Final response gate: only a passing, violation-free meal can be returned."""
        return finish_guard(verification_passed, result)


def _names_match(stored: str, wanted: str) -> bool:
    a, b = set(stored.casefold().split()), set(wanted.casefold().split())
    return bool(a and b) and (a <= b or b <= a)