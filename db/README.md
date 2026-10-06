# State and persistence

The MVP follows the requested layers using SQLite for durable user/domain state and LangGraph's SQLite checkpointer for working state. Redis is optional for TTL session caching. PostgreSQL/pgvector is not required for this local single-node configuration; preference feedback is stored as normalized SQLite events and can later be migrated to PostgreSQL/pgvector.

Implemented SQLite tables include:

- `users`, `user_profiles`: dietary profile, allergens, taste/brand preferences, daily targets.
- `inventory_items`, `inventory_events`, `inventory_reservations`: quantities, confidence/source/expiry, append-only changes, reservation lease and expiry.
- `meal_plans` and `meal_plan_items`: workflow JSON snapshots and normalized proposed/verified meal lines.
- `meal_history`, `macro_log`: consumed meals and daily macro totals.
- `carts`, `approvals`, `orders`, `procurement_audit`: mock-only purchasing decisions and approval/idempotency records.
- `tool_call_traces`: redacted agent/tool inputs, outputs, and durations for evaluation.
- `preference_events`: optional like/dislike observations for composer personalization.
- `nutrition_cache`: USDA/Open Food Facts lookup cache.

`meal_agent/storage/workflow_store.py` persists plans and session snapshots in SQLite. If `REDIS_URL` is configured, active session state is also cached under a hashed user/session key with `SESSION_TTL_SECONDS` expiry; the SQLite snapshot is the fallback. The LangGraph checkpointer persists graph node state in SQLite as well.

Inventory used by a verified meal or a pending approval plan is reserved per user/plan and ingredient, with a six-hour expiry. Replacing a plan's proposed portions refreshes its reservation; declining/abandoning a plan releases it. Database operations scope reads/writes by `user_id`. Local `MEAL_AGENT_USER_ID` is only a development identity; production requires authenticated account linking.
