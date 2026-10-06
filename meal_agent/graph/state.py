"""Typed working state for a single meal-planning run."""

from __future__ import annotations

from typing import Any, TypedDict


class MealPlanningState(TypedDict, total=False):
    user_id: str
    plan_id: str
    session_id: str
    request: str
    target: dict[str, float | int | None]
    profile: dict[str, Any]
    macro_status: dict[str, Any]
    constraints: dict[str, Any]
    meal_type: str | None
    inventory: list[dict[str, Any]]
    inventory_result: dict[str, Any]
    candidate: dict[str, Any]
    candidate_result: dict[str, Any]
    verification: dict[str, Any]
    best_attempt: dict[str, Any]
    procurement_result: dict[str, Any]
    plan: list[dict[str, Any]]
    next_action: str
    last_action: str
    last_result: dict[str, Any]
    decision_history: list[dict[str, Any]]
    pending_question: str
    asked_from_action: str
    pending_inventory_item: dict[str, Any]
    user_answer: str
    iterations: int
    repair_iterations: int
    max_repair_iterations: int
    tool_calls: int
    max_tool_calls: int
    max_wall_clock_seconds: float
    started_at: float
    status: str
    final_result: dict[str, Any]
