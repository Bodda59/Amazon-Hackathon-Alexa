"""Daily macro status and verified meal logging agent."""

from __future__ import annotations

import asyncio
from typing import Any

from meal_agent.config import settings
from meal_agent.storage.domain_store import DomainStore
from meal_agent.storage.workflow_store import WorkflowStore


class TrackingAgent:
    def __init__(self, domain_store: DomainStore, workflow_store: WorkflowStore) -> None:
        self.domain_store = domain_store
        self.workflow_store = workflow_store

    async def status(self, user_id: str) -> dict[str, Any]:
        totals = await asyncio.to_thread(self.domain_store.nutrition_status, user_id)
        profile = await asyncio.to_thread(self.domain_store.get_profile, user_id)
        return {
            "status": "ok",
            "totals": totals,
            "targets": profile.get("daily_macro_targets", {}),
            "spoken_summary": "Today's macro totals are ready." if totals else "No meals have been logged today.",
        }

    async def set_targets(self, user_id: str, targets: dict[str, Any]) -> dict[str, Any]:
        valid_keys = {"kcal", "protein_g", "carbs_g", "fat_g"}
        if not targets or set(targets) - valid_keys:
            return {
                "status": "invalid_input",
                "spoken_summary": "Provide one or more daily targets for kcal, protein_g, carbs_g, or fat_g.",
            }
        normalized = {key: float(value) for key, value in targets.items()}
        if any(value <= 0 for value in normalized.values()):
            return {"status": "invalid_input", "spoken_summary": "Daily macro targets must be positive."}
        await asyncio.to_thread(self.domain_store.set_macro_targets, user_id, normalized)
        return {
            "status": "ok",
            "targets": normalized,
            "spoken_summary": "Your daily nutrition targets were saved.",
        }

    async def set_profile(self, user_id: str, profile: dict[str, Any]) -> dict[str, Any]:
        allowed = {"diet", "allergies", "dislikes", "preferred_cuisines", "preferred_brands"}
        if set(profile) - allowed:
            return {"status": "invalid_input", "spoken_summary": "Profile fields must be diet, allergies, dislikes, preferred_cuisines, or preferred_brands."}
        if "allergies" in profile and not isinstance(profile["allergies"], list):
            return {"status": "invalid_input", "spoken_summary": "Allergies must be provided as a list."}
        await asyncio.to_thread(self.domain_store.set_profile, user_id, profile)
        return {"status": "ok", "profile": profile, "spoken_summary": "Your dietary profile was saved."}

    async def record_feedback(
        self, user_id: str, preference: str, polarity: int, plan_id: str | None = None
    ) -> dict[str, Any]:
        if not preference.strip() or polarity not in {-1, 1}:
            return {
                "status": "invalid_input",
                "spoken_summary": "Provide a food or preference and whether you liked it or disliked it.",
            }
        await asyncio.to_thread(
            self.domain_store.record_preference_feedback,
            user_id,
            preference,
            polarity,
            plan_id=plan_id,
        )
        return {
            "status": "ok",
            "preference": preference,
            "polarity": polarity,
            "spoken_summary": "I saved that meal preference for future suggestions.",
        }

    async def log_plan(self, user_id: str, plan_id: str) -> dict[str, Any]:
        plan = await asyncio.to_thread(self.workflow_store.get_plan, user_id, plan_id)
        if plan is None:
            return {"status": "not_found", "spoken_summary": "I could not find a saved meal plan for your account."}
        final_result = plan.get("final_result", {})
        if final_result.get("status") != "verified":
            return {"status": "unverified", "spoken_summary": "Only a verified completed meal plan can be logged."}
        logged = await asyncio.to_thread(self.domain_store.log_verified_plan, user_id, final_result)
        totals = await asyncio.to_thread(self.domain_store.nutrition_status, user_id)
        return {
            "status": "already_logged" if logged.get("already_logged") else "ok",
            "meal_id": logged["meal_id"],
            "consumed_inventory": logged["consumed"],
            "inventory_not_consumed": logged["not_consumed"],
            "totals": totals,
            "spoken_summary": "This verified meal was already logged." if logged.get("already_logged") else "The verified meal was logged and measured inventory was updated where it matched.",
        }


_DEFAULT_AGENT: TrackingAgent | None = None


def get_tracking_agent() -> TrackingAgent:
    global _DEFAULT_AGENT
    if _DEFAULT_AGENT is None:
        _DEFAULT_AGENT = TrackingAgent(
            DomainStore(settings.workflow_database_path),
            WorkflowStore(settings.workflow_database_path),
        )
    return _DEFAULT_AGENT


async def nutrition_status(
    user_id: str,
    action: str = "status",
    plan_id: str | None = None,
    targets: dict[str, Any] | None = None,
    profile: dict[str, Any] | None = None,
    preference: str | None = None,
    liked: bool | None = None,
) -> dict[str, Any]:
    agent = get_tracking_agent()
    if action == "status":
        return await agent.status(user_id)
    if action == "log":
        if not plan_id:
            return {"status": "needs_user", "spoken_summary": "Provide the verified plan ID you consumed."}
        return await agent.log_plan(user_id, plan_id)
    if action == "set_targets":
        return await agent.set_targets(user_id, targets or {})
    if action == "set_profile":
        return await agent.set_profile(user_id, profile or {})
    if action == "feedback":
        if liked is None or preference is None:
            return {"status": "needs_user", "spoken_summary": "Tell me which food or preference you liked or disliked."}
        return await agent.record_feedback(user_id, preference, 1 if liked else -1, plan_id)
    return {"status": "invalid_input", "spoken_summary": "Use action=status or action=log."}
