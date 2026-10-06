"""FastMCP entry point exposing the small Alexa+-facing tool surface."""

from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

import argparse
from pathlib import Path
from typing import Literal
import uvicorn
from mcp.server.fastmcp import FastMCP

from meal_agent.agents.inventory import manage_inventory as inventory_manage
from meal_agent.agents.procurement import shop_for_meal as run_shop_for_meal
from meal_agent.agents.tracking import nutrition_status as run_nutrition_status
from meal_agent.config import settings
from meal_agent.graph.supervisor import run_plan_meal
from meal_agent.schemas import InventoryImage, MCPToolResponse, NutritionTarget

_UI_DIRECTORY = Path(__file__).resolve().parents[1] / "ui"

mcp = FastMCP(
    settings.app_name,
    instructions=(
        "Meal and macro planning MCP server. Use goal-level tools. Treat status as "
        "authoritative: a meal is not verified until status is verified, and shopping "
        "must never happen without explicit user confirmation."
    ),
    stateless_http=True,
    json_response=True,
    host=settings.host,
    port=settings.port,
    streamable_http_path="/mcp",
)

def build_asgi_app():
    app = mcp.streamable_http_app()
    return CORSMiddleware(
        app,
        allow_origins=["http://localhost:8000", "http://127.0.0.1:8000"],
        allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
        allow_headers=["*"],
        expose_headers=["Mcp-Session-Id", "mcp-session-id"],
        max_age=600,
    )

def _response(result: dict[str, object], resource_uri: str) -> MCPToolResponse:
    """Normalize specialist results into structured, voice-first MCP output."""
    result_status = str(result.get("status", "unknown"))
    spoken_summary = str(result.get("spoken_summary", "The request has been processed."))
    data = {key: value for key, value in result.items() if key not in {"status", "spoken_summary"}}
    return MCPToolResponse(
        status=result_status,
        spoken_summary=spoken_summary,
        resourceUri=resource_uri,
        data=data,
    )


def _read_ui(filename: str) -> str:
    html = (_UI_DIRECTORY / filename).read_text(encoding="utf-8")
    css = (_UI_DIRECTORY / "shared.css").read_text(encoding="utf-8")
    js = (_UI_DIRECTORY / "shared.js").read_text(encoding="utf-8")
    html = html.replace('<link rel="stylesheet" href="shared.css">', f"<style>{css}</style>")
    return html.replace('<script src="shared.js"></script>', f"<script>{js}</script>")


@mcp.tool(
    description=(
        "Create a meal plan for calorie and/or macro goals. Pass target as an object "
        "using exact fields (kcal, protein_g, carbs_g, fat_g) and/or bounds (for "
        "example protein_g_min=45 or carbs_g_max=30); omit unconstrained nutrients. "
        "Optionally pass meal_type, request, and dietary/preferences constraints. "
        "Set constraints.use_inventory=false when pantry state is irrelevant. "
        "Use session_id and answer to resume a clarification. A result is not a "
        "verified meal unless status is "
        "verified and verification.passed is true."
    ),
    meta={"ui": {"resourceUri": "ui://meal-card"}},
    structured_output=True,
)
async def plan_meal(
    target: NutritionTarget,
    meal_type: str | None = None,
    constraints: dict[str, object] | None = None,
    request: str | None = None,
    session_id: str | None = None,
    answer: str | None = None,
) -> MCPToolResponse:
    """Plan a meal from a target; returns structured data, a spoken summary, and UI URI."""
    result = await run_plan_meal(
        settings.user_id,
        target,
        meal_type,
        constraints,
        request=request,
        session_id=session_id,
        answer=answer,
    )
    return _response(result, "ui://meal-card")


@mcp.tool(
    description=(
        "List kitchen inventory, check expiring items, or submit an inventory update. "
        "Use action=list or expiring for questions; use action=add, update, consume, "
        "reserve, release, or barcode for inventory operations. For reserve pass a JSON "
        "list with name and grams in free_text; for release pass reservation_id or "
        "plan_id as JSON; for barcode pass the barcode in free_text. "
        "Use confirm=true with the returned receipt-item JSON in free_text to save "
        "receipt proposals. Optional image must contain base64 data and mime_type."
    ),
    meta={"ui": {"resourceUri": "ui://inventory-card"}},
    structured_output=True,
)
async def manage_inventory(
    action: Literal["list", "expiring", "add", "update", "consume", "reserve", "release", "barcode"],
    free_text: str | None = None,
    image: InventoryImage | None = None,
    confirm: bool = False,
) -> MCPToolResponse:
    """Manage inventory via voice/text or an optional receipt image."""
    result = await inventory_manage(settings.user_id, action, free_text, image, confirm)
    return _response(result, "ui://inventory-card")


@mcp.tool(
    description=(
        "Shop for missing ingredients from an existing plan. Set confirm=true with "
        "user_approved=true to approve purchase and deliver items to inventory; "
        "set decline=true to refuse and cancel the cart. Shopping uses a mock "
        "catalog and immediately updates inventory upon approved delivery."
    ),
    meta={"ui": {"resourceUri": "ui://cart-approval"}},
    structured_output=True,
)
async def shop_for_meal(
    plan_id: str,
    confirm: bool = False,
    cart_id: str | None = None,
    approval_token: str | None = None,
    user_approved: bool = False,
    decline: bool = False,
) -> MCPToolResponse:
    """Prepare a mock cart, confirm checkout with inventory delivery, or refuse/decline."""
    if decline:
        result = await run_shop_for_meal(
            plan_id,
            confirm=False,
            user_id=settings.user_id,
            cart_id=cart_id,
            decline=True,
        )
        return _response(result, "ui://cart-approval")

    if confirm and not user_approved:
        return _response(
            {
                "status": "approval_required",
                "spoken_summary": "The user must explicitly approve this cart before checkout can be attempted.",
            },
            "ui://cart-approval",
        )
    result = await run_shop_for_meal(
        plan_id,
        confirm,
        user_id=settings.user_id,
        approval_token=approval_token,
        cart_id=cart_id,
        explicit_confirmation=user_approved,
        mock_checkout_enabled=True,
    )
    return _response(result, "ui://cart-approval")


@mcp.tool(
    description=(
        "Read today's remaining macro budget with action=status, or log a consumed "
        "meal with action=log and its plan_id. Set daily targets with action=set_targets "
        "and a targets object; save dietary preferences/allergies with action=set_profile "
        "and a profile object; record liked/disliked foods with action=feedback, "
        "preference, and liked. Logging requires a saved verified plan and updates "
        "SQLite macro totals plus matching measured pantry stock."
    ),
    meta={"ui": {"resourceUri": "ui://nutrition-status"}},
    structured_output=True,
)
async def nutrition_status(
    action: Literal["status", "log", "set_targets", "set_profile", "feedback"] = "status",
    plan_id: str | None = None,
    targets: dict[str, float] | None = None,
    profile: dict[str, object] | None = None,
    preference: str | None = None,
    liked: bool | None = None,
) -> MCPToolResponse:
    """Read or update nutrition tracking and return structured, voice-first output."""
    return _response(
        await run_nutrition_status(
            settings.user_id, action, plan_id, targets, profile, preference, liked
        ),
        "ui://nutrition-status",
    )


@mcp.resource("ui://meal-card", name="Meal plan card", mime_type="text/html;profile=mcp-app")
def meal_card_resource() -> str:
    """Provide the meal-plan MCP App resource referenced by plan_meal."""
    return _read_ui("meal-card.html")


@mcp.resource("ui://inventory-card", name="Inventory card", mime_type="text/html;profile=mcp-app")
def inventory_card_resource() -> str:
    """Provide the inventory MCP App resource referenced by manage_inventory."""
    return _read_ui("inventory-card.html")


@mcp.resource("ui://cart-approval", name="Cart approval", mime_type="text/html;profile=mcp-app")
def cart_approval_resource() -> str:
    """Provide the approval MCP App resource referenced by shop_for_meal."""
    return _read_ui("cart-approval.html")


@mcp.resource("ui://nutrition-status", name="Nutrition status", mime_type="text/html;profile=mcp-app")
def nutrition_status_resource() -> str:
    """Provide the nutrition-status MCP App resource referenced by nutrition_status."""
    return _read_ui("nutrition-status.html")


def main() -> None:
    parser = argparse.ArgumentParser(description="Meal Macro Planner MCP server")
    parser.add_argument("--transport", choices=("streamable-http",), default="streamable-http")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    args = parser.parse_args()

    app = build_asgi_app()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
