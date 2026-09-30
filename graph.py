"""
graph.py — 7 agents + planner/critic retry loop (max 3 attempts).
"""
from __future__ import annotations

from operator import add
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from agents import agents


class KitchenState(TypedDict, total=False):
    run_id: str
    user_request: str

    # Orchestrator
    plan: dict
    meal_type: str
    calorie_target: float | None
    context_packet: dict

    # Pantry
    pantry_status: dict
    deduction_result: dict

    # Preference filter
    active_exclusions: list
    active_preferences: list
    applied_rule_changes: list

    # Planner
    proposed_meals: list
    planning_attempts: int          # NEW

    # Nutrition critic
    evaluated_meals: list
    chosen_meal: dict | None
    previous_rejections: list       # NEW

    # Shopping
    needs: list
    covered: list
    short: list
    restock: list
    shopping_list: list
    shopping_list_ids: list

    # Presenter
    voice_summary: str
    card: dict | None

    # Accumulators
    events: Annotated[list, add]
    errors: Annotated[list, add]


MAX_PLANNING_ATTEMPTS = 3


MAX_PLANNING_ATTEMPTS = 3

def _route_after_nutrition(state):
    if state.get("chosen_meal"):
        steps = state.get("plan", {}).get("steps", [])
        return "shopping" if "shopping" in steps else "presenter"
    if state.get("planning_attempts", 0) < MAX_PLANNING_ATTEMPTS:
        return "planner"
    return "presenter"


def build_graph():
    g = StateGraph(KitchenState)

    g.add_node("orchestrator",      agents.orchestrator)
    g.add_node("pantry",            agents.pantry)
    g.add_node("preference_filter", agents.preference_filter)
    g.add_node("planner",           agents.planner)
    g.add_node("nutrition_critic",  agents.nutrition_critic)
    g.add_node("shopping",          agents.shopping)
    g.add_node("presenter",         agents.presenter)

    g.add_edge(START, "orchestrator")
    g.add_edge("orchestrator", "pantry")
    g.add_edge("pantry", "preference_filter")
    g.add_edge("preference_filter", "planner")
    g.add_edge("planner", "nutrition_critic")

    g.add_conditional_edges("nutrition_critic", _route_after_nutrition, {
        "shopping": "shopping",
        "presenter": "presenter",
        "planner": "planner",
    })
    g.add_edge("shopping", "presenter")
    g.add_edge("presenter", END)

    return g.compile()