#!/usr/bin/env python3
"""
kitchen.db — schema + test data for the meal-planning agent.

Creates (or rebuilds) a SQLite database with these tables:

    profile         one row: default calorie + macro targets
    inventory       what's in the kitchen, with expiry + low-stock threshold
    exclusions      HARD rules ("no eggs") — never broken by the agent
    preferences     SOFT likes/dislikes with a weight
    meal_history    what was suggested / accepted / rejected and why
    shopping_list   missing + restock items with status
    orders          each order: items, total, status, approval
    payments        provider payment id, status, mode (test|live) — NO card data
    events_log      every change, for debugging and undo

Every INSERT/UPDATE/DELETE on the first eight tables is auto-logged into
events_log by SQLite triggers, so the log can never be bypassed.

Usage:
    python init_kitchen_db.py            # create + seed (safe to re-run)
    python init_kitchen_db.py --reset    # drop everything and rebuild
    python init_kitchen_db.py --no-seed  # schema only

Requires: Python 3.8+ (stdlib only). SQLite must have JSON1 enabled,
which is the default in every CPython build since 3.9.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "kitchen.db"

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
PRAGMA journal_mode = WAL;

-- ---------------------------------------------------------------------------
-- profile : single row (id = 1). Default targets the agent plans against.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS profile (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    name            TEXT    NOT NULL,
    daily_calories  INTEGER NOT NULL,
    protein_g       REAL    NOT NULL,
    carbs_g         REAL    NOT NULL,
    fat_g           REAL    NOT NULL,
    fiber_g         REAL,
    sugar_g         REAL,
    sodium_mg       REAL,
    notes           TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- inventory : what is physically in the kitchen right now.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inventory (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    item                 TEXT NOT NULL,
    quantity             REAL NOT NULL DEFAULT 0,
    unit                 TEXT NOT NULL DEFAULT 'g',
    category             TEXT,                      -- protein / produce / dairy / pantry
    location             TEXT,                      -- fridge / freezer / pantry / counter
    expiry               TEXT,                      -- ISO date 'YYYY-MM-DD', nullable
    low_stock_threshold  REAL NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (item, unit)
);

-- ---------------------------------------------------------------------------
-- exclusions : HARD rules. The agent must never violate these, ever.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS exclusions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item        TEXT NOT NULL UNIQUE,               -- 'eggs', 'peanuts', 'pork'
    reason      TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- preferences : SOFT signals, scored. Higher weight = stronger feeling.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS preferences (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item        TEXT NOT NULL,
    sentiment   TEXT NOT NULL CHECK (sentiment IN ('like', 'dislike')),
    weight      REAL NOT NULL DEFAULT 1.0 CHECK (weight >= 0 AND weight <= 10),
    notes       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (item, sentiment)
);

-- ---------------------------------------------------------------------------
-- meal_history : the feedback loop. What was suggested, what happened, why.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meal_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    suggested_at  TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at    TEXT,
    meal_name     TEXT NOT NULL,
    meal_type     TEXT CHECK (meal_type IN ('breakfast', 'lunch', 'dinner', 'snack')),
    calories      REAL,
    protein_g     REAL,
    carbs_g       REAL,
    fat_g         REAL,
    status        TEXT NOT NULL DEFAULT 'suggested'
                  CHECK (status IN ('suggested', 'accepted', 'rejected', 'cooked', 'skipped')),
    reason        TEXT,                             -- why accepted / rejected
    recipe_json   TEXT CHECK (recipe_json IS NULL OR json_valid(recipe_json))
);

-- ---------------------------------------------------------------------------
-- shopping_list : things to buy. 'missing' = needed for a recipe,
-- 'restock' = fell below the inventory threshold.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shopping_list (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    item              TEXT NOT NULL,
    quantity          REAL NOT NULL DEFAULT 1,
    unit              TEXT NOT NULL DEFAULT 'count',
    source            TEXT NOT NULL DEFAULT 'manual'
                      CHECK (source IN ('missing', 'restock', 'manual')),
    status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'approved', 'ordered', 'purchased', 'cancelled')),
    priority          INTEGER NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
    estimated_price   REAL,
    notes             TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- orders : a basket the agent wants to place. Needs human approval.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    provider      TEXT,                             -- 'instacart', 'amazon_fresh', ...
    items_json    TEXT NOT NULL CHECK (json_valid(items_json)),
    subtotal      REAL NOT NULL DEFAULT 0,
    delivery_fee  REAL NOT NULL DEFAULT 0,
    total         REAL NOT NULL DEFAULT 0,
    currency      TEXT NOT NULL DEFAULT 'USD',
    status        TEXT NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft', 'pending_approval', 'approved', 'rejected',
                                    'placed', 'delivered', 'cancelled')),
    approval      TEXT NOT NULL DEFAULT 'pending'
                  CHECK (approval IN ('pending', 'approved', 'rejected')),
    approved_by   TEXT,
    approved_at   TEXT,
    notes         TEXT
);

-- ---------------------------------------------------------------------------
-- payments : ONLY provider-side identifiers. Card numbers never touch this
-- database — the payment provider holds them and returns an opaque id.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payments (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id             INTEGER REFERENCES orders(id) ON DELETE SET NULL,
    provider             TEXT NOT NULL,             -- 'stripe', 'paypal', ...
    provider_payment_id  TEXT NOT NULL UNIQUE,      -- 'pi_3TEST...' — opaque, not a card
    amount               REAL NOT NULL,
    currency             TEXT NOT NULL DEFAULT 'USD',
    status               TEXT NOT NULL DEFAULT 'requires_payment_method'
                         CHECK (status IN ('requires_payment_method', 'requires_confirmation',
                                           'processing', 'succeeded', 'failed',
                                           'refunded', 'cancelled')),
    mode                 TEXT NOT NULL DEFAULT 'test' CHECK (mode IN ('test', 'live')),
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- events_log : append-only audit trail. Written by triggers, not by hand.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entity       TEXT NOT NULL,                     -- table name
    entity_id    INTEGER,
    action       TEXT NOT NULL CHECK (action IN ('insert', 'update', 'delete')),
    actor        TEXT NOT NULL DEFAULT 'system',    -- 'agent' | 'user' | 'system'
    before_json  TEXT CHECK (before_json IS NULL OR json_valid(before_json)),
    after_json   TEXT CHECK (after_json IS NULL OR json_valid(after_json)),
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_inventory_expiry      ON inventory (expiry);
CREATE INDEX IF NOT EXISTS idx_inventory_item        ON inventory (item);
CREATE INDEX IF NOT EXISTS idx_meal_history_status   ON meal_history (status);
CREATE INDEX IF NOT EXISTS idx_meal_history_date     ON meal_history (suggested_at);
CREATE INDEX IF NOT EXISTS idx_shopping_status       ON shopping_list (status);
CREATE INDEX IF NOT EXISTS idx_orders_status         ON orders (status);
CREATE INDEX IF NOT EXISTS idx_payments_order        ON payments (order_id);
CREATE INDEX IF NOT EXISTS idx_events_entity         ON events_log (entity, entity_id);
CREATE INDEX IF NOT EXISTS idx_events_created        ON events_log (created_at);
"""

# Tables whose changes are mirrored into events_log.
LOGGED_TABLES = [
    "profile",
    "inventory",
    "exclusions",
    "preferences",
    "meal_history",
    "shopping_list",
    "orders",
    "payments",
]

# Dropped in this order (children before parents).
ALL_TABLES = [
    "events_log",
    "payments",
    "orders",
    "shopping_list",
    "meal_history",
    "preferences",
    "exclusions",
    "inventory",
    "profile",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_connection(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Open the kitchen database with sane defaults."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _json_object_sql(columns, prefix: str) -> str:
    """Build `json_object('a', NEW.a, 'b', NEW.b, ...)` for a trigger body."""
    args = ", ".join(f"'{c}', {prefix}.{c}" for c in columns)
    return f"json_object({args})"


def install_event_triggers(conn: sqlite3.Connection) -> None:
    """
    Attach AFTER INSERT/UPDATE/DELETE triggers to every mutable table so that
    events_log captures the full before/after state automatically.
    """
    for table in LOGGED_TABLES:
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        new_json = _json_object_sql(columns, "NEW")
        old_json = _json_object_sql(columns, "OLD")

        conn.executescript(f"""
        CREATE TRIGGER IF NOT EXISTS trg_{table}_insert
        AFTER INSERT ON {table}
        BEGIN
            INSERT INTO events_log (entity, entity_id, action, actor, after_json)
            VALUES ('{table}', NEW.id, 'insert', 'system', {new_json});
        END;

        CREATE TRIGGER IF NOT EXISTS trg_{table}_update
        AFTER UPDATE ON {table}
        BEGIN
            INSERT INTO events_log (entity, entity_id, action, actor,
                                    before_json, after_json)
            VALUES ('{table}', NEW.id, 'update', 'system', {old_json}, {new_json});
        END;

        CREATE TRIGGER IF NOT EXISTS trg_{table}_delete
        AFTER DELETE ON {table}
        BEGIN
            INSERT INTO events_log (entity, entity_id, action, actor, before_json)
            VALUES ('{table}', OLD.id, 'delete', 'system', {old_json});
        END;
        """)
    conn.commit()


def log_event(conn, entity, entity_id=None, action="insert", actor="system",
              before=None, after=None) -> None:
    """Manual escape hatch for events that aren't table writes."""
    conn.execute(
        """INSERT INTO events_log (entity, entity_id, action, actor, before_json, after_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            entity,
            entity_id,
            action,
            actor,
            json.dumps(before) if before is not None else None,
            json.dumps(after) if after is not None else None,
        ),
    )


def backup_database(conn: sqlite3.Connection, destination: Path | str) -> None:
    """Safe hot backup (works while the DB is in use)."""
    destination = Path(destination)
    with sqlite3.connect(str(destination)) as dest:
        conn.backup(dest)


# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

def seed_test_data(conn: sqlite3.Connection) -> None:
    today = date.today()
    now = datetime.now()

    def day(offset: int) -> str:
        return (today + timedelta(days=offset)).isoformat()

    def stamp(days: int = 0, hours: int = 0) -> str:
        return (now + timedelta(days=days, hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    # -- profile (exactly one row) ------------------------------------------
    conn.execute(
        """INSERT INTO profile
               (id, name, daily_calories, protein_g, carbs_g, fat_g,
                fiber_g, sugar_g, sodium_mg, notes)
           VALUES (1, 'Alex', 2200, 150, 220, 73, 30, 50, 2300,
                   'Cutting ~0.5 lb/week. High protein, moderate carbs, no shellfish.')"""
    )

    # -- inventory -----------------------------------------------------------
    inventory = [
        # item,                qty,   unit,   category, location, expiry,  threshold
        ("Chicken breast",     800,   "g",    "protein", "fridge",  day(3),   300),
        ("Salmon fillet",      300,   "g",    "protein", "freezer", day(60),  200),
        ("Greek yogurt",       500,   "g",    "dairy",   "fridge",  day(9),   200),
        ("Whole milk",        1000,   "ml",   "dairy",   "fridge",  day(5),   500),
        ("Cheddar cheese",     200,   "g",    "dairy",   "fridge",  day(14),  100),
        ("Baby spinach",       120,   "g",    "produce", "fridge",  day(2),   100),
        ("Broccoli",           300,   "g",    "produce", "fridge",  day(4),   150),
        ("Tomatoes",             4,   "count","produce", "fridge",  day(5),     2),
        ("Bananas",              5,   "count","produce", "counter", day(4),     3),
        ("Lemons",               2,   "count","produce", "fridge",  day(7),     1),
        ("Garlic",               1,   "head", "produce", "pantry",  day(20),    1),
        ("Jasmine rice",      1500,   "g",    "pantry",  "pantry",  day(365), 500),
        ("Rolled oats",        750,   "g",    "pantry",  "pantry",  day(200), 300),
        ("Black beans",          2,   "can",  "pantry",  "pantry",  day(500),   3),
        ("Almond butter",      250,   "g",    "pantry",  "pantry",  day(120), 100),
        ("Olive oil",          400,   "ml",   "pantry",  "pantry",  day(400), 100),
        ("Coffee beans",       200,   "g",    "pantry",  "pantry",  day(90),  250),
        ("Sourdough bread",      1,   "loaf", "bakery",  "counter", day(2),     1),
    ]
    conn.executemany(
        """INSERT INTO inventory
               (item, quantity, unit, category, location, expiry, low_stock_threshold)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        inventory,
    )

    # -- exclusions (hard rules) --------------------------------------------
    exclusions = [
        ("eggs",      "Allergy — hives within 30 min. HARD RULE, never suggest or order."),
        ("peanuts",   "Severe allergy (anaphylaxis). Never suggest, never order."),
        ("shellfish", "Allergy — shrimp, crab, lobster, mussels, clams."),
        ("pork",      "Religious/ethical. HARD RULE."),
    ]
    conn.executemany(
        "INSERT INTO exclusions (item, reason) VALUES (?, ?)", exclusions
    )

    # -- preferences (soft signals) -----------------------------------------
    preferences = [
        # item,               sentiment, weight, notes
        ("salmon",            "like",     9.0,  "Could eat weekly"),
        ("chicken breast",    "like",     8.0,  "Weeknight staple"),
        ("spicy food",        "like",     8.0,  "Chili crisp, sriracha, gochujang"),
        ("greek yogurt",      "like",     7.0,  None),
        ("coffee",            "like",     7.0,  "Morning, non-negotiable"),
        ("dark chocolate",    "like",     6.0,  "85% only"),
        ("rolled oats",       "like",     5.0,  "Fine, not exciting"),
        ("mushrooms",         "dislike",  7.0,  "Texture thing"),
        ("cilantro",          "dislike",  6.0,  "Tastes like soap"),
        ("cottage cheese",    "dislike",  5.0,  None),
        ("eggplant",          "dislike",  4.0,  "Okay if well hidden"),
        ("liver",             "dislike",  9.0,  "Strong aversion"),
    ]
    conn.executemany(
        """INSERT INTO preferences (item, sentiment, weight, notes)
           VALUES (?, ?, ?, ?)""",
        preferences,
    )

    # -- meal_history --------------------------------------------------------
    chicken_bowl_recipe = json.dumps({
        "ingredients": ["chicken breast 200g", "jasmine rice 150g", "broccoli 100g",
                        "olive oil 1 tbsp", "garlic 2 cloves"],
        "steps": ["Cook rice", "Sear chicken", "Steam broccoli", "Assemble"],
        "servings": 1,
    })
    salmon_recipe = json.dumps({
        "ingredients": ["salmon fillet 180g", "broccoli 150g", "lemon 1/2",
                        "olive oil 1 tbsp"],
        "steps": ["Roast broccoli 200C 15min", "Pan-sear salmon 4min/side",
                  "Finish with lemon"],
        "servings": 1,
    })

    meals = [
        # suggested_at, decided_at, name, type, kcal, P, C, F, status, reason, recipe
        (stamp(-13), stamp(-13), "Oatmeal with banana and almond butter", "breakfast",
         430, 15, 62, 14, "accepted", "Uses pantry staples, quick", None),
        (stamp(-12), stamp(-12), "Scrambled eggs on sourdough", "breakfast",
         380, 22, 34, 17, "rejected", "Blocked by hard exclusion: eggs", None),
        (stamp(-12), stamp(-12), "Grilled chicken and rice bowl", "dinner",
         620, 48, 65, 14, "cooked", "User cooked it as suggested", chicken_bowl_recipe),
        (stamp(-10), stamp(-10), "Mushroom risotto", "dinner",
         610, 16, 78, 22, "rejected", "Contains mushrooms — dislike weight 7.0", None),
        (stamp(-9),  stamp(-9),  "Salmon with roasted broccoli", "dinner",
         540, 42, 18, 30, "cooked", "Salmon is a top-3 like", salmon_recipe),
        (stamp(-7),  stamp(-7),  "Shrimp tacos", "dinner",
         590, 34, 55, 24, "rejected", "Blocked by hard exclusion: shellfish", None),
        (stamp(-6),  stamp(-6),  "Greek yogurt parfait", "snack",
         260, 20, 28, 6, "accepted", "High protein, no cooking", None),
        (stamp(-4),  stamp(-4),  "Black bean burrito bowl", "lunch",
         520, 22, 74, 12, "accepted", "Cheap, uses canned beans", None),
        (stamp(-3),  stamp(-3),  "Tomato basil soup with sourdough", "lunch",
         340, 11, 52, 9, "cooked", "Used up the tomatoes before they turned", None),
        (stamp(-1),  stamp(-1),  "Chicken stir fry with rice", "dinner",
         580, 45, 60, 15, "suggested", None, None),
        (stamp(0, -2), None,     "Overnight oats with banana", "breakfast",
         410, 18, 60, 11, "suggested", None, None),
    ]
    conn.executemany(
        """INSERT INTO meal_history
               (suggested_at, decided_at, meal_name, meal_type, calories,
                protein_g, carbs_g, fat_g, status, reason, recipe_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        meals,
    )

    # -- shopping_list -------------------------------------------------------
    shopping = [
        # item,               qty, unit,  source,    status,      priority, est_price, notes
        ("Coffee beans",       500, "g",   "restock", "pending",   1, 14.50, "Below threshold (200g left)"),
        ("Black beans",          4, "can", "restock", "pending",   2,  6.00, "Below threshold (2 left)"),
        ("Salmon fillet",      600, "g",   "missing", "pending",   2, 18.20, "For Wednesday dinner"),
        ("Baby spinach",       200, "g",   "restock", "approved",  1,  3.50, None),
        ("Greek yogurt",      1000, "g",   "restock", "ordered",   2,  7.00, "Part of order #1"),
        ("Sourdough bread",      1, "loaf","restock", "purchased", 3,  5.50, None),
        ("Almond butter",      340, "g",   "restock", "pending",   4,  9.99, "Not urgent"),
        ("Cilantro",             1, "bunch","manual","cancelled",  5,  1.20, "Cancelled — dislike weight 6.0"),
    ]
    conn.executemany(
        """INSERT INTO shopping_list
               (item, quantity, unit, source, status, priority, estimated_price, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        shopping,
    )

    # -- orders --------------------------------------------------------------
    order_1_items = [
        {"item": "Greek yogurt",  "quantity": 1000, "unit": "g",    "price": 7.00},
        {"item": "Baby spinach",  "quantity": 200,  "unit": "g",    "price": 3.50},
        {"item": "Sourdough bread", "quantity": 1,  "unit": "loaf", "price": 5.50},
        {"item": "Salmon fillet", "quantity": 600,  "unit": "g",    "price": 18.20},
    ]
    order_2_items = [
        {"item": "Coffee beans",  "quantity": 500,  "unit": "g",    "price": 14.50},
        {"item": "Black beans",   "quantity": 4,    "unit": "can",  "price": 1.50},
        {"item": "Rolled oats",   "quantity": 1000, "unit": "g",    "price": 4.25},
        {"item": "Almond butter", "quantity": 340,  "unit": "g",    "price": 9.99},
        {"item": "Bananas",       "quantity": 6,    "unit": "count","price": 2.10},
    ]
    order_3_items = [
        {"item": "Ribeye steak",  "quantity": 2,    "unit": "count","price": 24.00},
        {"item": "Truffle oil",   "quantity": 1,    "unit": "bottle","price": 13.75},
    ]

    orders = [
        # created_at, provider, items_json, subtotal, delivery, total, currency,
        # status, approval, approved_by, approved_at, notes
        (stamp(-6), "instacart", json.dumps(order_1_items), 34.20, 0.00, 34.20, "USD",
         "delivered", "approved", "user", stamp(-6, 1), "Weekly restock"),
        (stamp(-1), "instacart", json.dumps(order_2_items), 36.84, 4.99, 41.83, "USD",
         "pending_approval", "pending", None, None, "Awaiting user approval"),
        (stamp(-9), "amazon_fresh", json.dumps(order_3_items), 61.75, 0.00, 61.75, "USD",
         "cancelled", "rejected", "user", stamp(-9, 3), "Rejected — over budget"),
    ]
    conn.executemany(
        """INSERT INTO orders
               (created_at, provider, items_json, subtotal, delivery_fee, total,
                currency, status, approval, approved_by, approved_at, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        orders,
    )

    # -- payments (test mode, opaque provider ids only) ----------------------
    payments = [
        # order_id, provider, provider_payment_id,      amount, currency, status,     mode
        (1, "stripe", "pi_3TEST_a1b2c3d4e5f6", 34.20, "USD", "succeeded", "test"),
        (3, "stripe", "pi_3TEST_f9e8d7c6b5a4", 61.75, "USD", "failed",    "test"),
    ]
    conn.executemany(
        """INSERT INTO payments
               (order_id, provider, provider_payment_id, amount, currency, status, mode)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        payments,
    )

    conn.commit()

    # A couple of narrative events so the log isn't purely mechanical inserts.
    log_event(conn, "agent_run", None, "insert", "agent", None,
              {"run_id": "run_2024_demo", "note": "Seeded demo run"})
    log_event(conn, "orders", 2, "update", "agent", None,
              {"note": "Agent drafted restock order, awaiting user approval"})
    log_event(conn, "meal_history", 11, "insert", "agent", None,
              {"note": "Suggested overnight oats from inventory"})
    conn.commit()


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_database(db_path: Path | str = DB_PATH, reset: bool = False,
                   seed: bool = True) -> sqlite3.Connection:
    """Create the schema (and optionally test data). Returns an open connection."""
    db_path = Path(db_path)

    if reset and db_path.exists():
        # Remove the DB plus any WAL/SHM sidecars.
        for suffix in ("", "-wal", "-shm"):
            sidecar = Path(str(db_path) + suffix)
            if sidecar.exists():
                sidecar.unlink()

    conn = get_connection(db_path)
    conn.executescript(SCHEMA)
    install_event_triggers(conn)

    if seed:
        already = conn.execute("SELECT COUNT(*) FROM profile").fetchone()[0]
        if already:
            print("Database already contains data — skipping seed. "
                  "Use --reset to rebuild.")
        else:
            seed_test_data(conn)
            print("Seeded test data.")

    return conn


def print_summary(conn: sqlite3.Connection) -> None:
    print(f"\nDatabase: {DB_PATH}")
    print("-" * 42)
    for table in reversed(ALL_TABLES):          # logical reading order
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table:<16} {count:>4} rows")
    print("-" * 42)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Create and seed kitchen.db")
    parser.add_argument("--db", default=str(DB_PATH), help="Path to the SQLite file")
    parser.add_argument("--reset", action="store_true",
                        help="Delete the existing database and rebuild from scratch")
    parser.add_argument("--no-seed", action="store_true",
                        help="Create the schema only, without test data")
    parser.add_argument("--backup", metavar="DEST",
                        help="Also write a backup copy to DEST")
    args = parser.parse_args(argv)

    conn = build_database(args.db, reset=args.reset, seed=not args.no_seed)

    if args.backup:
        backup_database(conn, args.backup)
        print(f"Backup written to {args.backup}")

    print_summary(conn)

    print("\nNext meal suggestion on the table:")
    row = conn.execute(
        """SELECT meal_name, meal_type, calories, protein_g
           FROM meal_history WHERE status = 'suggested'
           ORDER BY suggested_at DESC LIMIT 1"""
    ).fetchone()
    if row:
        print(f"  {row['meal_name']} ({row['meal_type']}) — "
              f"{row['calories']:.0f} kcal, {row['protein_g']:.0f}g protein")

    low = conn.execute(
        """SELECT item, quantity, unit, low_stock_threshold
           FROM inventory WHERE quantity <= low_stock_threshold
           ORDER BY item"""
    ).fetchall()
    if low:
        print("\nLow stock:")
        for r in low:
            print(f"  {r['item']}: {r['quantity']:g}{r['unit']} "
                  f"(threshold {r['low_stock_threshold']:g})")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())