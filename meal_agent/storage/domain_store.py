"""SQLite repositories for inventory, nutrition cache, profiles, meals and procurement."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4


class DomainStore:
    """Small single-file SQLite store with user-scoped writes and audit records."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).expanduser()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    profile_json TEXT NOT NULL DEFAULT '{}',
                    daily_targets_json TEXT NOT NULL DEFAULT '{}',
                    preferences_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS inventory_items (
                    item_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    unit TEXT NOT NULL,
                    grams_estimate REAL,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    expiry TEXT,
                    source TEXT NOT NULL DEFAULT 'user',
                    last_confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    reserved_grams REAL NOT NULL DEFAULT 0,
                    barcode TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(user_id, name, unit, expiry)
                );
                CREATE INDEX IF NOT EXISTS idx_inventory_user ON inventory_items(user_id);
                CREATE TABLE IF NOT EXISTS inventory_events (
                    event_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    item_id TEXT,
                    event_type TEXT NOT NULL,
                    quantity REAL,
                    unit TEXT,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TRIGGER IF NOT EXISTS inventory_events_no_update
                    BEFORE UPDATE ON inventory_events BEGIN
                    SELECT RAISE(ABORT, 'inventory_events is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS inventory_events_no_delete
                    BEFORE DELETE ON inventory_events BEGIN
                    SELECT RAISE(ABORT, 'inventory_events is append-only');
                END;
                CREATE TABLE IF NOT EXISTS inventory_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    plan_id TEXT,
                    grams REAL NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_reservations_user_status ON inventory_reservations(user_id,status,expires_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_active_plan_reservation
                    ON inventory_reservations(user_id,plan_id,item_id) WHERE status='active' AND plan_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS nutrition_cache (
                    query_key TEXT PRIMARY KEY,
                    food_json TEXT NOT NULL,
                    cached_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS meal_history (
                    meal_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    plan_id TEXT,
                    meal_json TEXT NOT NULL,
                    consumed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_meal_history_user ON meal_history(user_id, consumed_at);
                CREATE TABLE IF NOT EXISTS meal_plan_items (
                    item_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    grams REAL NOT NULL,
                    item_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, plan_id, name)
                );
                CREATE INDEX IF NOT EXISTS idx_plan_items_user_plan ON meal_plan_items(user_id, plan_id);
                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id TEXT PRIMARY KEY,
                    profile_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS macro_log (
                    log_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    meal_id TEXT NOT NULL,
                    totals_json TEXT NOT NULL,
                    logged_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS carts (
                    cart_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    cart_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    total REAL NOT NULL DEFAULT 0,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    cart_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    used_at TEXT
                );
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    cart_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    order_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS procurement_audit (
                    audit_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    cart_id TEXT,
                    event_type TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS tool_call_traces (
                    trace_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    plan_id TEXT,
                    agent TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    output_json TEXT NOT NULL,
                    duration_ms REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_tool_trace_session
                    ON tool_call_traces(user_id, session_id, created_at);
                CREATE TRIGGER IF NOT EXISTS tool_call_traces_no_update
                    BEFORE UPDATE ON tool_call_traces BEGIN
                    SELECT RAISE(ABORT, 'tool_call_traces is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS tool_call_traces_no_delete
                    BEFORE DELETE ON tool_call_traces BEGIN
                    SELECT RAISE(ABORT, 'tool_call_traces is append-only');
                END;
                CREATE TABLE IF NOT EXISTS preference_events (
                    event_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    plan_id TEXT,
                    preference TEXT NOT NULL,
                    polarity INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

    @staticmethod
    def _ensure_user(connection: sqlite3.Connection, user_id: str) -> None:
        connection.execute(
            "INSERT INTO users(user_id) VALUES(?) ON CONFLICT(user_id) DO NOTHING",
            (user_id,),
        )

    def record_inventory_event(
        self,
        connection: sqlite3.Connection,
        user_id: str,
        item_id: str | None,
        event_type: str,
        quantity: float | None,
        unit: str | None,
        details: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO inventory_events VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (str(uuid4()), user_id, item_id, event_type, quantity, unit,
             json.dumps(details or {}, separators=(",", ":"), ensure_ascii=False)),
        )

    def list_inventory(self, user_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            self._expire_reservations(connection, user_id)
            rows = connection.execute(
                "SELECT * FROM inventory_items WHERE user_id = ? ORDER BY expiry IS NULL, expiry, name",
                (user_id,),
            ).fetchall()
        return [self._inventory_row(row) for row in rows]

    @staticmethod
    def _inventory_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json"))
        item["available_grams"] = max(
            0.0, (item.get("grams_estimate") or 0.0) - item.get("reserved_grams", 0.0)
        )
        return item

    def upsert_inventory(self, user_id: str, item: dict[str, Any]) -> dict[str, Any]:
        name = str(item["name"]).strip().lower()
        if not name:
            raise ValueError("Inventory item name cannot be empty.")
        quantity = float(item["quantity"])
        if quantity <= 0:
            raise ValueError("Inventory quantity must be positive.")
        unit = str(item.get("unit", "g")).strip().lower()
        metadata = item.get("metadata", {})
        with self._connection() as connection:
            self._ensure_user(connection, user_id)
            existing = connection.execute(
                "SELECT * FROM inventory_items WHERE user_id=? AND name=? AND unit=? AND expiry IS ?",
                (user_id, name, unit, item.get("expiry")),
            ).fetchone()
            if existing is None:
                existing = connection.execute(
                    """SELECT * FROM inventory_items WHERE user_id=? AND name=?
                       AND confidence < 0.5 ORDER BY last_confirmed_at LIMIT 1""",
                    (user_id, name),
                ).fetchone()
            if existing:
                item_id = existing["item_id"]
                is_clarification = float(existing["confidence"]) < 0.5
                next_quantity = quantity if is_clarification else float(existing["quantity"]) + quantity
                grams = item.get("grams_estimate")
                next_grams = (
                    float(grams)
                    if is_clarification and grams is not None
                    else (float(existing["grams_estimate"] or 0) + float(grams))
                    if grams is not None
                    else existing["grams_estimate"]
                )
                connection.execute(
                    """UPDATE inventory_items SET quantity=?, unit=?, grams_estimate=?, confidence=?, source=?,
                       last_confirmed_at=CURRENT_TIMESTAMP, barcode=COALESCE(?, barcode), metadata_json=?
                       WHERE item_id=? AND user_id=?""",
                    (next_quantity, unit, next_grams, float(item.get("confidence", 1.0)),
                     str(item.get("source", "user")), item.get("barcode"),
                     json.dumps(metadata, separators=(",", ":"), ensure_ascii=False), item_id, user_id),
                )
            else:
                item_id = str(uuid4())
                connection.execute(
                    """INSERT INTO inventory_items
                       (item_id,user_id,name,quantity,unit,grams_estimate,confidence,expiry,source,barcode,metadata_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (item_id, user_id, name, quantity, unit, item.get("grams_estimate"),
                     float(item.get("confidence", 1.0)), item.get("expiry"),
                     str(item.get("source", "user")), item.get("barcode"),
                     json.dumps(metadata, separators=(",", ":"), ensure_ascii=False)),
                )
            self.record_inventory_event(
                connection, user_id, item_id, "upsert", quantity, unit, item
            )
            row = connection.execute(
                "SELECT * FROM inventory_items WHERE item_id=? AND user_id=?", (item_id, user_id)
            ).fetchone()
        return self._inventory_row(row)

    def consume_inventory(self, user_id: str, name: str, quantity: float, unit: str = "g") -> dict[str, Any]:
        if quantity <= 0:
            raise ValueError("Consumption quantity must be positive.")
        with self._connection() as connection:
            self._expire_reservations(connection, user_id)
            row = connection.execute(
                "SELECT * FROM inventory_items WHERE user_id=? AND lower(name)=lower(?) AND unit=? ORDER BY expiry IS NULL, expiry LIMIT 1",
                (user_id, name.strip(), unit.strip().lower()),
            ).fetchone()
            if row is None:
                raise LookupError(f"No inventory item named {name!r} with unit {unit!r}.")
            unit_key = unit.strip().lower()
            gram_factors = {
                "g": 1.0, "gram": 1.0, "grams": 1.0,
                "kg": 1000.0, "kilogram": 1000.0, "kilograms": 1000.0,
                "mg": 0.001, "oz": 28.349523125, "lb": 453.59237,
                "lbs": 453.59237, "pound": 453.59237, "pounds": 453.59237,
            }
            if row["grams_estimate"] is not None:
                consumed_grams = (
                    quantity * gram_factors[unit_key]
                    if unit_key in gram_factors
                    else float(row["grams_estimate"]) * quantity / float(row["quantity"])
                )
                available_grams = float(row["grams_estimate"]) - float(row["reserved_grams"])
                if consumed_grams > available_grams + 1e-9:
                    raise ValueError("Cannot consume reserved or unavailable inventory.")
            remaining = float(row["quantity"]) - quantity
            if remaining < -1e-9:
                raise ValueError("Cannot consume more than the available quantity.")
            grams_remaining = row["grams_estimate"]
            if grams_remaining is not None and row["quantity"]:
                grams_remaining = max(0.0, grams_remaining * remaining / row["quantity"])
            connection.execute(
                "UPDATE inventory_items SET quantity=?, grams_estimate=?, last_confirmed_at=CURRENT_TIMESTAMP WHERE item_id=? AND user_id=?",
                (max(0.0, remaining), grams_remaining, row["item_id"], user_id),
            )
            self.record_inventory_event(connection, user_id, row["item_id"], "consume", quantity, unit)
            updated = connection.execute(
                "SELECT * FROM inventory_items WHERE item_id=?", (row["item_id"],)
            ).fetchone()
        return self._inventory_row(updated)

    def reserve_inventory(self, user_id: str, reservations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        reserved: list[dict[str, Any]] = []
        with self._connection() as connection:
            self._expire_reservations(connection, user_id)
            for reservation in reservations:
                grams = float(reservation["grams"])
                row = connection.execute(
                    "SELECT * FROM inventory_items WHERE user_id=? AND lower(name)=lower(?) ORDER BY expiry IS NULL, expiry LIMIT 1",
                    (user_id, reservation["name"]),
                ).fetchone()
                if row is None or row["grams_estimate"] is None:
                    raise LookupError(f"No gram estimate available for {reservation['name']!r}.")
                plan_id = reservation.get("plan_id")
                if plan_id:
                    existing = connection.execute(
                        """SELECT reservation_id,grams,expires_at FROM inventory_reservations
                           WHERE user_id=? AND item_id=? AND plan_id=? AND status='active'""",
                        (user_id, row["item_id"], plan_id),
                    ).fetchone()
                    if existing:
                        reserved.append({"reservation_id": existing["reservation_id"], "item_id": row["item_id"], "name": row["name"], "grams": existing["grams"], "expires_at": existing["expires_at"]})
                        continue
                available = float(row["grams_estimate"]) - float(row["reserved_grams"])
                if grams <= 0 or grams > available:
                    raise ValueError(f"Insufficient unreserved stock for {reservation['name']!r}.")
                connection.execute(
                    "UPDATE inventory_items SET reserved_grams=reserved_grams+? WHERE item_id=? AND user_id=?",
                    (grams, row["item_id"], user_id),
                )
                reservation_id = str(uuid4())
                expires_at = (datetime.now(timezone.utc) + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
                connection.execute(
                    """INSERT INTO inventory_reservations
                       (reservation_id,user_id,item_id,plan_id,grams,expires_at,status)
                       VALUES(?,?,?,?,?,?,'active')""",
                    (reservation_id, user_id, row["item_id"], plan_id, grams, expires_at),
                )
                self.record_inventory_event(connection, user_id, row["item_id"], "reserve", grams, "g")
                reserved.append({"reservation_id": reservation_id, "item_id": row["item_id"], "name": row["name"], "grams": grams, "expires_at": expires_at})
        return reserved

    @staticmethod
    def _expire_reservations(connection: sqlite3.Connection, user_id: str) -> None:
        expired = connection.execute(
            "SELECT item_id,SUM(grams) AS grams FROM inventory_reservations WHERE user_id=? AND status='active' AND expires_at <= CURRENT_TIMESTAMP GROUP BY item_id",
            (user_id,),
        ).fetchall()
        for row in expired:
            connection.execute(
                "UPDATE inventory_items SET reserved_grams=MAX(0,reserved_grams-?) WHERE item_id=? AND user_id=?",
                (row["grams"], row["item_id"], user_id),
            )
        connection.execute(
            "UPDATE inventory_reservations SET status='expired' WHERE user_id=? AND status='active' AND expires_at <= CURRENT_TIMESTAMP",
            (user_id,),
        )

    def release_inventory_reservations(
        self,
        user_id: str,
        *,
        reservation_id: str | None = None,
        plan_id: str | None = None,
    ) -> int:
        if not reservation_id and not plan_id:
            raise ValueError("Provide a reservation_id or plan_id.")
        with self._connection() as connection:
            query = "SELECT * FROM inventory_reservations WHERE user_id=? AND status='active' AND expires_at > CURRENT_TIMESTAMP"
            params: list[Any] = [user_id]
            if reservation_id:
                query += " AND reservation_id=?"
                params.append(reservation_id)
            if plan_id:
                query += " AND plan_id=?"
                params.append(plan_id)
            rows = connection.execute(query, params).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE inventory_items SET reserved_grams=MAX(0,reserved_grams-?) WHERE item_id=? AND user_id=?",
                    (row["grams"], row["item_id"], user_id),
                )
                connection.execute(
                    "UPDATE inventory_reservations SET status='released' WHERE reservation_id=? AND user_id=?",
                    (row["reservation_id"], user_id),
                )
                self.record_inventory_event(connection, user_id, row["item_id"], "release_reservation", row["grams"], "g", {"reservation_id": row["reservation_id"], "plan_id": row["plan_id"]})
        return len(rows)

    def expiring_soon(self, user_id: str, days: int = 3) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM inventory_items WHERE user_id=? AND expiry IS NOT NULL
                   AND date(expiry) <= date('now', ?) AND date(expiry) >= date('now')
                   ORDER BY date(expiry)""",
                (user_id, f"+{max(0, days)} days"),
            ).fetchall()
        return [self._inventory_row(row) for row in rows]

    def cache_get(self, key: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT food_json FROM nutrition_cache WHERE query_key=? AND cached_at >= datetime('now','-30 days')",
                (key,),
            ).fetchone()
        return json.loads(row["food_json"]) if row else None

    def cache_put(self, key: str, food: dict[str, Any]) -> None:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO nutrition_cache(query_key,food_json,cached_at) VALUES(?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(query_key) DO UPDATE SET food_json=excluded.food_json,cached_at=CURRENT_TIMESTAMP""",
                (key, json.dumps(food, separators=(",", ":"), ensure_ascii=False)),
            )

    def get_profile(self, user_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT profile_json FROM user_profiles WHERE user_id=?", (user_id,)
            ).fetchone()
        return json.loads(row["profile_json"]) if row else {}

    def set_profile(self, user_id: str, profile: dict[str, Any]) -> None:
        with self._connection() as connection:
            self._ensure_user(connection, user_id)
            preferences = {
                key: profile[key]
                for key in ("dislikes", "preferred_cuisines", "preferred_brands")
                if key in profile
            }
            connection.execute(
                """UPDATE users SET profile_json=?,preferences_json=?,updated_at=CURRENT_TIMESTAMP
                   WHERE user_id=?""",
                (json.dumps(profile, separators=(",", ":"), ensure_ascii=False),
                 json.dumps(preferences, separators=(",", ":"), ensure_ascii=False), user_id),
            )
            connection.execute(
                """INSERT INTO user_contexts(user_id,profile_json,macro_status_json,updated_at)
                   VALUES(?,?, '{}',CURRENT_TIMESTAMP) ON CONFLICT(user_id) DO NOTHING""",
                (user_id, "{}"),
            )
            connection.execute(
                """INSERT INTO user_profiles(user_id,profile_json,updated_at)
                   VALUES(?,?,CURRENT_TIMESTAMP) ON CONFLICT(user_id) DO UPDATE SET
                   profile_json=excluded.profile_json,updated_at=CURRENT_TIMESTAMP""",
                (user_id, json.dumps(profile, separators=(",", ":"), ensure_ascii=False)),
            )

    def set_macro_targets(self, user_id: str, targets: dict[str, Any]) -> None:
        """Set daily nutrition targets while preserving the independent user profile."""
        with self._connection() as connection:
            self._ensure_user(connection, user_id)
            connection.execute(
                "UPDATE users SET daily_targets_json=?,updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
                (json.dumps(targets, separators=(",", ":"), ensure_ascii=False), user_id),
            )
            connection.execute(
                """INSERT INTO user_contexts(user_id,profile_json,macro_status_json,updated_at)
                   VALUES(?, '{}', ?, CURRENT_TIMESTAMP) ON CONFLICT(user_id) DO UPDATE SET
                   macro_status_json=excluded.macro_status_json,updated_at=CURRENT_TIMESTAMP""",
                (user_id, json.dumps(targets, separators=(",", ":"), ensure_ascii=False)),
            )

    def get_user_context(self, user_id: str) -> dict[str, Any]:
        """Combine profile and current macro ledger for agent context."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT profile_json,daily_targets_json FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
            fallback = connection.execute(
                "SELECT profile_json FROM user_profiles WHERE user_id=?", (user_id,)
            ).fetchone()
        profile = json.loads(row["profile_json"]) if row else json.loads(fallback["profile_json"]) if fallback else {}
        macro_status = self.nutrition_status(user_id)
        targets = json.loads(row["daily_targets_json"]) if row else {}
        return {"profile": profile, "macro_status": {**targets, **macro_status}}

    def save_meal_plan_items(
        self, user_id: str, plan_id: str, ingredients: list[dict[str, Any]]
    ) -> None:
        """Replace normalized line items for the user's proposed/verified plan."""
        with self._connection() as connection:
            self._ensure_user(connection, user_id)
            connection.execute(
                "DELETE FROM meal_plan_items WHERE user_id=? AND plan_id=?",
                (user_id, plan_id),
            )
            for ingredient in ingredients:
                name = str(ingredient.get("name", "")).strip()
                grams = float(ingredient.get("grams", ingredient.get("min_g", 0)))
                if not name:
                    continue
                connection.execute(
                    """INSERT INTO meal_plan_items(item_id,user_id,plan_id,name,grams,item_json)
                       VALUES(?,?,?,?,?,?)""",
                    (str(uuid4()), user_id, plan_id, name, grams,
                     json.dumps(ingredient, separators=(",", ":"), ensure_ascii=False)),
                )

    def record_tool_call(
        self,
        *,
        user_id: str,
        session_id: str,
        plan_id: str | None,
        agent: str,
        tool_name: str,
        inputs: dict[str, Any],
        output: dict[str, Any],
        duration_ms: float,
    ) -> None:
        """Append a redacted tool trace for evaluation/debugging."""
        def redact(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    key: "[REDACTED]" if any(
                        marker in key.casefold()
                        for marker in ("token", "password", "api_key", "secret")
                    ) else redact(item)
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [redact(item) for item in value]
            if isinstance(value, str):
                return value[:4000]
            return value

        with self._connection() as connection:
            self._ensure_user(connection, user_id)
            connection.execute(
                """INSERT INTO tool_call_traces
                   (trace_id,user_id,session_id,plan_id,agent,tool_name,input_json,output_json,duration_ms)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (str(uuid4()), user_id, session_id, plan_id, agent, tool_name,
                 json.dumps(redact(inputs), separators=(",", ":"), ensure_ascii=False),
                 json.dumps(redact(output), separators=(",", ":"), ensure_ascii=False),
                 max(0.0, float(duration_ms))),
            )

    def list_tool_call_traces(self, user_id: str, session_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM tool_call_traces WHERE user_id=? AND session_id=? ORDER BY created_at,trace_id",
                (user_id, session_id),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["inputs"] = json.loads(item.pop("input_json"))
            item["output"] = json.loads(item.pop("output_json"))
            result.append(item)
        return result

    def record_preference_feedback(
        self,
        user_id: str,
        preference: str,
        polarity: int,
        *,
        plan_id: str | None = None,
        source: str = "user_feedback",
        details: dict[str, Any] | None = None,
    ) -> None:
        if polarity not in {-1, 1}:
            raise ValueError("Preference polarity must be -1 or 1.")
        with self._connection() as connection:
            self._ensure_user(connection, user_id)
            connection.execute(
                """INSERT INTO preference_events
                   (event_id,user_id,plan_id,preference,polarity,source,details_json)
                   VALUES(?,?,?,?,?,?,?)""",
                (str(uuid4()), user_id, plan_id, preference.strip().casefold(), polarity,
                 source, json.dumps(details or {}, separators=(",", ":"), ensure_ascii=False)),
            )

    def get_preference_summary(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT preference,SUM(polarity) AS score,COUNT(*) AS observations
                   FROM (SELECT preference,polarity FROM preference_events WHERE user_id=?
                         ORDER BY created_at DESC LIMIT ?)
                   GROUP BY preference ORDER BY score DESC,observations DESC""",
                (user_id, min(max(1, limit), 100)),
            ).fetchall()
        return [dict(row) for row in rows]

    def shopping_candidates(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return a per-user candidate catalog (empty until products are loaded)."""
        # Retail product integrations will populate this table in a later adapter.
        return []

    def get_history(self, user_id: str, limit: int = 12) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT meal_json FROM meal_history WHERE user_id=? ORDER BY consumed_at DESC LIMIT ?",
                (user_id, min(max(limit, 1), 100)),
            ).fetchall()
        return [json.loads(row["meal_json"]) for row in rows]

    def log_meal(self, user_id: str, meal: dict[str, Any], totals: dict[str, Any], plan_id: str | None = None) -> str:
        meal_id = str(uuid4())
        with self._connection() as connection:
            self._ensure_user(connection, user_id)
            connection.execute(
                "INSERT INTO meal_history(meal_id,user_id,plan_id,meal_json) VALUES(?,?,?,?)",
                (meal_id, user_id, plan_id, json.dumps(meal, separators=(",", ":"), ensure_ascii=False)),
            )
            connection.execute(
                "INSERT INTO macro_log(log_id,user_id,meal_id,totals_json) VALUES(?,?,?,?)",
                (str(uuid4()), user_id, meal_id, json.dumps(totals, separators=(",", ":"))),
            )
        return meal_id

    def log_verified_plan(self, user_id: str, plan: dict[str, Any]) -> dict[str, Any]:
        """Log only a verified plan and atomically consume matching measured stock."""
        verification = plan.get("verification", {})
        if verification.get("passed") is not True or not isinstance(verification.get("totals"), dict):
            raise ValueError("Only a deterministically verified meal can be logged.")
        meal = plan.get("meal", {})
        ingredients = meal.get("ingredients", [])
        meal_id = str(uuid4())
        consumed: list[dict[str, Any]] = []
        not_consumed: list[dict[str, Any]] = []
        with self._connection() as connection:
            self._ensure_user(connection, user_id)
            existing_log = connection.execute(
                "SELECT meal_id FROM meal_history WHERE user_id=? AND plan_id=?",
                (user_id, plan.get("plan_id")),
            ).fetchone()
            if existing_log is not None:
                return {
                    "meal_id": existing_log["meal_id"],
                    "consumed": [],
                    "not_consumed": [],
                    "already_logged": True,
                }
            connection.execute(
                "INSERT INTO meal_history(meal_id,user_id,plan_id,meal_json) VALUES(?,?,?,?)",
                (meal_id, user_id, plan.get("plan_id"), json.dumps(meal, separators=(",", ":"), ensure_ascii=False)),
            )
            connection.execute(
                "INSERT INTO macro_log(log_id,user_id,meal_id,totals_json) VALUES(?,?,?,?)",
                (str(uuid4()), user_id, meal_id, json.dumps(verification["totals"], separators=(",", ":"))),
            )
            for ingredient in ingredients:
                grams = float(ingredient.get("grams", 0))
                name = str(ingredient.get("name", ""))
                if grams <= 0 or not name:
                    continue
                row = connection.execute(
                    """SELECT * FROM inventory_items 
                       WHERE user_id=? AND grams_estimate IS NOT NULL AND (
                           lower(name) = lower(?)
                           OR instr(lower(name), lower(?)) > 0
                           OR instr(lower(?), lower(name)) > 0
                       )
                       ORDER BY expiry IS NULL, expiry LIMIT 1""",
                    (user_id, name, name, name),
                ).fetchone()
                if row is None:
                    not_consumed.append({"name": name, "grams": grams, "reason": "No measured inventory match."})
                    continue
                own_reservation = None
                if plan.get("plan_id"):
                    own_reservation = connection.execute(
                        """SELECT * FROM inventory_reservations WHERE user_id=? AND item_id=?
                           AND plan_id=? AND status='active' AND expires_at > CURRENT_TIMESTAMP""",
                        (user_id, row["item_id"], plan["plan_id"]),
                    ).fetchone()
                own_reserved_grams = float(own_reservation["grams"]) if own_reservation else 0.0
                unreserved = float(row["grams_estimate"]) - float(row["reserved_grams"])
                if grams > unreserved + own_reserved_grams:
                    not_consumed.append({"name": name, "grams": grams, "reason": "Insufficient measured unreserved inventory."})
                    continue
                consumed_reserved = min(grams, own_reserved_grams)
                if consumed_reserved > 0 and own_reservation is not None:
                    remaining_reserved = own_reserved_grams - consumed_reserved
                    connection.execute(
                        "UPDATE inventory_items SET reserved_grams=MAX(0,reserved_grams-?) WHERE item_id=? AND user_id=?",
                        (consumed_reserved, row["item_id"], user_id),
                    )
                    connection.execute(
                        "UPDATE inventory_reservations SET grams=?,status=? WHERE reservation_id=? AND user_id=?",
                        (remaining_reserved, "consumed" if remaining_reserved <= 1e-9 else "active", own_reservation["reservation_id"], user_id),
                    )
                next_grams = max(0.0, float(row["grams_estimate"]) - grams)
                quantity = float(row["quantity"])
                if row["unit"] in {"g", "gram", "grams"}:
                    next_quantity = max(0.0, quantity - grams)
                elif row["unit"] in {"kg", "kilogram", "kilograms"}:
                    next_quantity = max(0.0, quantity - grams / 1000.0)
                else:
                    next_quantity = max(0.0, quantity * next_grams / max(float(row["grams_estimate"]), 1e-9))
                
                if next_grams <= 0.001 or next_quantity <= 0.001:
                    connection.execute(
                        "DELETE FROM inventory_items WHERE item_id=? AND user_id=?",
                        (row["item_id"], user_id),
                    )
                else:
                    connection.execute(
                        "UPDATE inventory_items SET quantity=?,grams_estimate=?,last_confirmed_at=CURRENT_TIMESTAMP WHERE item_id=? AND user_id=?",
                        (next_quantity, next_grams, row["item_id"], user_id),
                    )
                self.record_inventory_event(connection, user_id, row["item_id"], "meal_consumed", grams, "g", {"meal_id": meal_id, "plan_id": plan.get("plan_id")})
                consumed.append({"name": name, "grams": int(grams) if grams == int(grams) else grams})
        return {"meal_id": meal_id, "consumed": consumed, "not_consumed": not_consumed}

    def get_latest_pending_cart(self, user_id: str, plan_id: str | None = None) -> dict[str, Any] | None:
        with self._connection() as connection:
            if plan_id:
                row = connection.execute(
                    "SELECT * FROM carts WHERE user_id=? AND plan_id=? AND status='pending_approval' ORDER BY created_at DESC LIMIT 1",
                    (user_id, plan_id),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM carts WHERE user_id=? AND status='pending_approval' ORDER BY created_at DESC LIMIT 1",
                    (user_id,),
                ).fetchone()
            if row:
                return {
                    "cart_id": row["cart_id"],
                    "plan_id": row["plan_id"],
                    "cart": json.loads(row["cart_json"]),
                    "status": row["status"],
                    "total": row["total"],
                    "idempotency_key": row["idempotency_key"],
                }
            return None

    def nutrition_status(self, user_id: str) -> dict[str, float]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT totals_json FROM macro_log WHERE user_id=? AND date(logged_at)=date('now')",
                (user_id,),
            ).fetchall()
            context = connection.execute(
                "SELECT macro_status_json FROM user_contexts WHERE user_id=?", (user_id,)
            ).fetchone()
        totals = {"kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0}
        for row in rows:
            entry = json.loads(row["totals_json"])
            for key in totals:
                totals[key] += float(entry.get(key, 0))
        targets = json.loads(context["macro_status_json"]) if context else {}
        result = {f"consumed_{key}": value for key, value in totals.items()}
        for key, value in totals.items():
            if targets.get(key) is not None:
                result[f"remaining_{key}"] = max(0.0, float(targets[key]) - value)
        return result

    def create_cart(self, user_id: str, plan_id: str, cart: dict[str, Any], total: float, idempotency_key: str) -> str:
        cart_id = str(uuid4())
        with self._connection() as connection:
            self._ensure_user(connection, user_id)
            existing = connection.execute(
                "SELECT cart_id FROM carts WHERE idempotency_key=? AND user_id=?",
                (idempotency_key, user_id),
            ).fetchone()
            if existing:
                return str(existing["cart_id"])
            connection.execute(
                "INSERT INTO carts(cart_id,user_id,plan_id,cart_json,status,total,idempotency_key) VALUES(?,?,?,?,?,?,?)",
                (cart_id, user_id, plan_id, json.dumps(cart, separators=(",", ":")), "pending_approval", total, idempotency_key),
            )
        return cart_id

    def get_cart(self, user_id: str, cart_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM carts WHERE user_id=? AND cart_id=?", (user_id, cart_id)
            ).fetchone()
        if row is None:
            return None
        cart = dict(row)
        cart["cart"] = json.loads(cart.pop("cart_json"))
        return cart

    def update_cart(
        self, user_id: str, cart_id: str, cart: dict[str, Any], total: float
    ) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE carts SET cart_json=?,total=?,updated_at=CURRENT_TIMESTAMP
                   WHERE user_id=? AND cart_id=? AND status='pending_approval'""",
                (json.dumps(cart, separators=(",", ":"), ensure_ascii=False), total, user_id, cart_id),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    "UPDATE approvals SET status='revoked' WHERE user_id=? AND cart_id=? AND status='pending'",
                    (user_id, cart_id),
                )
        return cursor.rowcount == 1

    def cancel_cart(self, user_id: str, cart_id: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE carts SET status='cancelled',updated_at=CURRENT_TIMESTAMP
                   WHERE user_id=? AND cart_id=? AND status='pending_approval'""",
                (user_id, cart_id),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    "UPDATE approvals SET status='revoked' WHERE user_id=? AND cart_id=? AND status='pending'",
                    (user_id, cart_id),
                )
        return cursor.rowcount == 1

    def get_order_status(self, user_id: str, cart_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT order_json FROM orders WHERE user_id=? AND cart_id=? ORDER BY created_at DESC LIMIT 1",
                (user_id, cart_id),
            ).fetchone()
            cart = connection.execute(
                "SELECT status FROM carts WHERE user_id=? AND cart_id=?",
                (user_id, cart_id),
            ).fetchone()
        if row is not None:
            return json.loads(row["order_json"])
        return {"status": cart["status"]} if cart is not None else None

    def save_approval(self, user_id: str, cart_id: str, token_hash: str, expires_at: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO approvals(approval_id,user_id,cart_id,token_hash,status,expires_at) VALUES(?,?,?,?,?,?)",
                (str(uuid4()), user_id, cart_id, token_hash, "pending", expires_at),
            )

    def consume_approval(self, user_id: str, cart_id: str, token_hash: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE approvals SET status='used',used_at=CURRENT_TIMESTAMP
                   WHERE user_id=? AND cart_id=? AND token_hash=? AND status='pending'
                   AND expires_at > CURRENT_TIMESTAMP""",
                (user_id, cart_id, token_hash),
            )
        return cursor.rowcount == 1

    def consume_latest_approval(self, user_id: str, cart_id: str) -> bool:
        """Consume a pending approval after the authenticated graph session received explicit yes."""
        with self._connection() as connection:
            row = connection.execute(
                """SELECT approval_id FROM approvals WHERE user_id=? AND cart_id=?
                   AND status='pending' AND expires_at > CURRENT_TIMESTAMP
                   ORDER BY created_at DESC LIMIT 1""",
                (user_id, cart_id),
            ).fetchone()
            if row is None:
                return False
            cursor = connection.execute(
                """UPDATE approvals SET status='used',used_at=CURRENT_TIMESTAMP
                   WHERE approval_id=? AND user_id=? AND status='pending'
                   AND expires_at > CURRENT_TIMESTAMP""",
                (row["approval_id"], user_id),
            )
        return cursor.rowcount == 1

    def audit_procurement(
        self, user_id: str, cart_id: str | None, event_type: str, details: dict[str, Any]
    ) -> None:
        with self._connection() as connection:
            self._ensure_user(connection, user_id)
            connection.execute(
                "INSERT INTO procurement_audit VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)",
                (str(uuid4()), user_id, cart_id, event_type,
                 json.dumps(details, separators=(",", ":"), ensure_ascii=False)),
            )

    def daily_order_total(self, user_id: str) -> float:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT order_json FROM orders WHERE user_id=? AND date(created_at)=date('now')",
                (user_id,),
            ).fetchall()
        return sum(float(json.loads(row["order_json"]).get("total", 0)) for row in rows)

    def get_order_by_idempotency(self, user_id: str, idempotency_key: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT order_json FROM orders WHERE user_id=? AND idempotency_key=?",
                (user_id, idempotency_key),
            ).fetchone()
        return json.loads(row["order_json"]) if row else None

    def finish_mock_cart(
        self,
        user_id: str,
        cart_id: str,
        idempotency_key: str,
        order: dict[str, Any],
    ) -> dict[str, Any]:
        with self._connection() as connection:
            cart_row = connection.execute(
                "SELECT cart_json FROM carts WHERE user_id=? AND cart_id=?",
                (user_id, cart_id),
            ).fetchone()
            persisted_order = {
                **order,
                "cart": json.loads(cart_row["cart_json"]) if cart_row else {},
            }
            connection.execute(
                "INSERT OR IGNORE INTO orders(order_id,user_id,cart_id,idempotency_key,order_json) VALUES(?,?,?,?,?)",
                (str(uuid4()), user_id, cart_id, idempotency_key,
                 json.dumps(persisted_order, separators=(",", ":"), ensure_ascii=False)),
            )
            row = connection.execute(
                "SELECT order_json FROM orders WHERE idempotency_key=? AND user_id=?",
                (idempotency_key, user_id),
            ).fetchone()
            connection.execute(
                "UPDATE carts SET status='mock_ordered',updated_at=CURRENT_TIMESTAMP WHERE cart_id=? AND user_id=?",
                (cart_id, user_id),
            )
        return json.loads(row["order_json"])

    def write_mock_order(self, user_id: str, cart_id: str, idempotency_key: str, order: dict[str, Any]) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO orders(order_id,user_id,cart_id,idempotency_key,order_json)
                   VALUES(?,?,?,?,?)""",
                (str(uuid4()), user_id, cart_id, idempotency_key,
                 json.dumps(order, separators=(",", ":"), ensure_ascii=False)),
            )
            row = connection.execute(
                "SELECT order_json FROM orders WHERE idempotency_key=? AND user_id=?",
                (idempotency_key, user_id),
            ).fetchone()
        return json.loads(row["order_json"])
