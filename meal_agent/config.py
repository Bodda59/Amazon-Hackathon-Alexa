"""Environment-backed application settings."""

from __future__ import annotations

import os
import logging
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

# USDA transmits API keys as query parameters; HTTPX's INFO logs would expose them.
logging.getLogger("httpx").setLevel(logging.WARNING)


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the MCP server and agent workflow."""

    app_name: str = os.getenv("APP_NAME", "meal-macro-agent")
    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = int(os.getenv("PORT", "8000"))
    user_id: str = os.getenv("MEAL_AGENT_USER_ID", "local-demo-user")
    max_repair_iterations: int = int(os.getenv("MAX_REPAIR_ITERATIONS", "20"))
    max_tool_calls: int = int(os.getenv("MAX_TOOL_CALLS", "100"))
    max_wall_clock_seconds: float = float(os.getenv("MAX_WALL_CLOCK_SECONDS", "20"))
    enable_orchestrator_llm: bool = os.getenv("ENABLE_ORCHESTRATOR_LLM", "false").lower() == "true"
    workflow_database_path: str = os.getenv("WORKFLOW_DATABASE_PATH", "data/meal-agent.sqlite3")
    usda_api_key: str = os.getenv("USDA_API_KEY", "")
    procurement_enabled: bool = os.getenv("PROCUREMENT_ENABLED", "false").lower() == "true"
    max_order_budget: float = float(os.getenv("MAX_ORDER_BUDGET", "50"))
    max_daily_order_budget: float = float(os.getenv("MAX_DAILY_ORDER_BUDGET", "100"))
    approval_ttl_minutes: int = int(os.getenv("APPROVAL_TTL_MINUTES", "15"))
    neo4j_uri: str = os.getenv("NEO4J_URI", "")
    neo4j_username: str = os.getenv("NEO4J_USERNAME", "")
    neo4j_password: str = os.getenv("NEO4J_PASSWORD", "")
    neo4j_database: str = os.getenv("NEO4J_DATABASE", "neo4j")
    redis_url: str = os.getenv("REDIS_URL", "")
    session_ttl_seconds: int = int(os.getenv("SESSION_TTL_SECONDS", "86400"))


settings = Settings()
