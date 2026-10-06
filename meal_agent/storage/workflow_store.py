"""SQLite persistence for orchestrator sessions and plans (single-node MVP)."""

from __future__ import annotations

import json
import hashlib
import copy
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from meal_agent.config import settings


class WorkflowStore:
    """Persist workflow snapshots with user-scoped session and plan identifiers."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        redis_url: str | None = None,
        session_ttl_seconds: int | None = None,
        redis_client_factory: Any | None = None,
    ) -> None:
        self.database_path = Path(database_path).expanduser()
        self.redis_url = settings.redis_url if redis_url is None else redis_url
        self.session_ttl_seconds = (
            settings.session_ttl_seconds
            if session_ttl_seconds is None
            else session_ttl_seconds
        )
        self.redis_client_factory = redis_client_factory
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @staticmethod
    def _session_key(user_id: str, session_id: str) -> str:
        digest = hashlib.sha256(f"{user_id}:{session_id}".encode()).hexdigest()
        return f"meal-agent:session:{digest}"

    def _redis_client(self) -> Any | None:
        if not self.redis_url:
            return None
        if self.redis_client_factory is not None:
            return self.redis_client_factory()
        from redis import Redis

        return Redis.from_url(self.redis_url, decode_responses=True, socket_connect_timeout=1)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_sessions (
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, session_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS user_contexts (
                    user_id TEXT PRIMARY KEY,
                    profile_json TEXT NOT NULL,
                    macro_status_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS meal_plans (
                    user_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, plan_id)
                )
                """
            )

    def get_session_state(self, user_id: str, session_id: str) -> dict[str, Any] | None:
        """Load one session only when it belongs to the supplied user."""
        redis_client = None
        if self.redis_url:
            try:
                redis_client = self._redis_client()
                cached = redis_client.get(self._session_key(user_id, session_id))
                if cached:
                    return json.loads(cached)
            except Exception:
                # SQLite remains an available local fallback if Redis is unavailable.
                pass
            finally:
                if redis_client is not None:
                    redis_client.close()
        with self._connection() as connection:
            row = connection.execute(
                """SELECT state_json, updated_at FROM workflow_sessions
                   WHERE user_id = ? AND session_id = ?""",
                (user_id, session_id),
            ).fetchone()
            if row:
                expired = connection.execute(
                    "SELECT datetime(?) <= datetime('now', ?) AS expired",
                    (row["updated_at"], f"-{max(1, self.session_ttl_seconds)} seconds"),
                ).fetchone()["expired"]
                if expired:
                    connection.execute(
                        "DELETE FROM workflow_sessions WHERE user_id=? AND session_id=?",
                        (user_id, session_id),
                    )
                    return None
        return json.loads(row["state_json"]) if row else None

    def get_plan(self, user_id: str, plan_id: str) -> dict[str, Any] | None:
        """Load one plan only when it belongs to the supplied user."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT state_json FROM meal_plans WHERE user_id = ? AND plan_id = ?",
                (user_id, plan_id),
            ).fetchone()
        return json.loads(row["state_json"]) if row else None

    def save_plan(self, state: dict[str, Any]) -> None:
        """Atomically persist a workflow snapshot by user, session, and plan."""
        user_id = state["user_id"]
        session_id = state["session_id"]
        plan_id = state["plan_id"]
        def redact(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    key: "[REDACTED]" if key.casefold() in {"approval_token", "access_token", "refresh_token"} else redact(item)
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [redact(item) for item in value]
            return value

        payload = json.dumps(redact(copy.deepcopy(state)), separators=(",", ":"), ensure_ascii=False)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO workflow_sessions (user_id, session_id, state_json, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, session_id) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, session_id, payload),
            )
            connection.execute(
                """
                INSERT INTO meal_plans (user_id, plan_id, session_id, state_json, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, plan_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    state_json = excluded.state_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, plan_id, session_id, payload),
            )
        redis_client = None
        if self.redis_url:
            try:
                redis_client = self._redis_client()
                redis_client.setex(
                    self._session_key(user_id, session_id),
                    max(1, self.session_ttl_seconds),
                    payload,
                )
            except Exception:
                # Durable SQLite snapshots remain authoritative when Redis is down.
                pass
            finally:
                if redis_client is not None:
                    redis_client.close()

    def get_user_context(self, user_id: str) -> dict[str, Any]:
        """Load the latest per-user profile and macro status for a new workflow."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT profile_json, macro_status_json FROM user_contexts WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return {"profile": {}, "macro_status": {}}
        return {
            "profile": json.loads(row["profile_json"]),
            "macro_status": json.loads(row["macro_status_json"]),
        }

    def save_user_context(
        self,
        user_id: str,
        profile: dict[str, Any],
        macro_status: dict[str, Any],
    ) -> None:
        """Upsert user profile and current macro status under the isolated user key."""
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO user_contexts (user_id, profile_json, macro_status_json, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    profile_json = excluded.profile_json,
                    macro_status_json = excluded.macro_status_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    user_id,
                    json.dumps(profile, separators=(",", ":"), ensure_ascii=False),
                    json.dumps(macro_status, separators=(",", ":"), ensure_ascii=False),
                ),
            )
