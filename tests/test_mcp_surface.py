"""Contract tests for the public MCP tools, metadata, and resource links."""

import unittest
import tempfile
from pathlib import Path
import sys
from unittest.mock import patch

from meal_agent.graph.tools import OrchestratorTools
from meal_agent.server import mcp
from meal_agent.storage.workflow_store import WorkflowStore


EXPECTED_RESOURCES = {
    "ui://meal-card",
    "ui://inventory-card",
    "ui://cart-approval",
    "ui://nutrition-status",
}


class MCPSurfaceTests(unittest.TestCase):
    def test_server_runs_streamable_http_only(self) -> None:
        from meal_agent import server

        with patch.object(server.mcp, "run") as run_server, patch.object(
            sys, "argv", ["meal-agent-server"]
        ):
            server.main()
        run_server.assert_called_once_with(transport="streamable-http")

        with patch.object(server.mcp, "run") as run_server, patch.object(
            sys, "argv", ["meal-agent-server", "--transport", "stdio"]
        ):
            with self.assertRaises(SystemExit):
                server.main()
        run_server.assert_not_called()

    def test_public_tool_names_descriptions_and_ui_metadata(self) -> None:
        tools = mcp._tool_manager.list_tools()
        self.assertEqual(
            {tool.name for tool in tools},
            {"plan_meal", "manage_inventory", "shop_for_meal", "nutrition_status"},
        )
        self.assertTrue(all(tool.description for tool in tools))
        self.assertTrue(all(tool.output_schema for tool in tools))
        tool_resource_uris = {
            tool.meta["ui"]["resourceUri"]
            for tool in tools
            if tool.meta and tool.meta.get("ui")
        }
        self.assertEqual(tool_resource_uris, EXPECTED_RESOURCES)

    def test_each_ui_resource_is_registered_as_html(self) -> None:
        resources = mcp._resource_manager.list_resources()
        self.assertEqual({str(resource.uri) for resource in resources}, EXPECTED_RESOURCES)
        self.assertTrue(
            all(resource.mime_type == "text/html;profile=mcp-app" for resource in resources)
        )

    def test_advertised_ui_resource_can_be_read(self) -> None:
        import asyncio

        contents = asyncio.run(mcp.read_resource("ui://meal-card"))
        resource = next(iter(contents))
        self.assertIn("Meal plan", resource.content)
        self.assertEqual(resource.mime_type, "text/html;profile=mcp-app")

    def test_target_requires_at_least_one_nutrition_value(self) -> None:
        plan_tool = next(
            tool for tool in mcp._tool_manager.list_tools() if tool.name == "plan_meal"
        )
        schema = plan_tool.parameters
        target_ref = schema["properties"]["target"]["$ref"]
        target_definition = schema["$defs"][target_ref.rsplit("/", maxsplit=1)[-1]]
        self.assertIn("kcal", target_definition["properties"])
        self.assertIn("protein_g", target_definition["properties"])
        self.assertIn("protein_g_min", target_definition["properties"])
        self.assertIn("carbs_g_max", target_definition["properties"])

    def test_all_tools_return_structured_voice_first_envelopes(self) -> None:
        import asyncio

        calls = {
            "plan_meal": {
                "target": {"kcal": 600, "protein_g": 45},
                "meal_type": "dinner",
            },
            "manage_inventory": {
                "action": "add",
                "free_text": "I bought chicken",
                "image": {"data": "%%%invalid%%%", "mime_type": "image/jpeg"},
            },
            "shop_for_meal": {"plan_id": "demo-plan", "confirm": False},
            "nutrition_status": {"action": "status"},
        }

        async def invoke_all() -> dict[str, object]:
            results: dict[str, object] = {}
            for name, arguments in calls.items():
                _content, structured = await mcp._tool_manager.call_tool(
                    name, arguments, convert_result=True
                )
                results[name] = structured
            return results

        with tempfile.TemporaryDirectory() as directory:
            test_tools = OrchestratorTools(WorkflowStore(Path(directory) / "workflow.sqlite3"))
            async def no_plan(*args: object, **kwargs: object) -> dict[str, object]:
                return {
                    "status": "not_configured",
                    "spoken_summary": "Integration test stub.",
                }

            with (
                patch("meal_agent.graph.supervisor._DEFAULT_TOOLS", test_tools),
                patch("meal_agent.server.run_plan_meal", no_plan),
            ):
                results = asyncio.run(invoke_all())
        self.assertEqual(set(results), set(calls))
        for result in results.values():
            self.assertIsInstance(result, dict)
            assert isinstance(result, dict)
            self.assertIn("status", result)
            self.assertIn("spoken_summary", result)
            self.assertIn("resourceUri", result)
            self.assertIn("data", result)
            self.assertIn(result["resourceUri"], EXPECTED_RESOURCES)
        inventory_result = results["manage_inventory"]
        assert isinstance(inventory_result, dict)
        self.assertEqual(inventory_result["status"], "invalid_input")

    def test_purchase_confirmation_never_enables_unconfigured_checkout(self) -> None:
        import asyncio

        _content, result = asyncio.run(
            mcp._tool_manager.call_tool(
                "shop_for_meal",
                {"plan_id": "demo-plan", "confirm": True},
                convert_result=True,
            )
        )
        self.assertEqual(result["status"], "approval_required")
        self.assertEqual(result["resourceUri"], "ui://cart-approval")


if __name__ == "__main__":
    unittest.main()
