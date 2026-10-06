"""Seed or import the user's initial inventory into the DomainStore SQLite DB.

Goes through DomainStore.upsert_inventory, so every row gets normalized names,
gram estimates, confidence, and an audit entry in inventory_events.

Usage (run from the project root):

  # 1) Load sample inventory (names match the knowledge-graph seed data)
  python seed_inventory.py --user-id demo-user

  # 2) Wipe that user's inventory first, then reload
  python seed_inventory.py --user-id demo-user --reset

  # 3) Import from a table in ANOTHER SQLite file (target=source_column pairs)
  python seed_inventory.py --user-id demo-user \
      --source old_inventory.db --table inventory \
      --map name=item_name quantity=qty unit=unit grams=grams expiry=expiry_date

  # 4) Use a specific target DB instead of settings.workflow_database_path
  python seed_inventory.py --user-id demo-user --db ./data/workflow.db

IMPORTANT: --user-id must be the SAME id your MCP server passes to the agent
(the account-linked user id, or whatever your local test harness uses).
Inventory is user-scoped; a different id means the agent sees an empty pantry.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

from meal_agent.config import settings
from meal_agent.storage.domain_store import DomainStore
from meal_agent.tools.units import to_grams


def _in_days(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def sample_items() -> list[dict]:
    """Names are lowercase display names so they resolve against KG ingredient names/aliases."""
    return [
        {"name": "chicken breast", "quantity": 800, "unit": "g", "grams_estimate": 800, "expiry": _in_days(3)},
        {"name": "white rice", "quantity": 2000, "unit": "g", "grams_estimate": 2000},
        {"name": "egg", "quantity": 12, "unit": "items", "grams_estimate": 600, "expiry": _in_days(14)},
        {"name": "spinach", "quantity": 200, "unit": "g", "grams_estimate": 200, "expiry": _in_days(2)},
        {"name": "olive oil", "quantity": 460, "unit": "g", "grams_estimate": 460},
        {"name": "greek yogurt", "quantity": 500, "unit": "g", "grams_estimate": 500, "expiry": _in_days(6)},
        {"name": "rolled oats", "quantity": 500, "unit": "g", "grams_estimate": 500},
        {"name": "lentils", "quantity": 1000, "unit": "g", "grams_estimate": 1000},
        {"name": "peanut butter", "quantity": 340, "unit": "g", "grams_estimate": 340},
        {"name": "broccoli", "quantity": 300, "unit": "g", "grams_estimate": 300, "expiry": _in_days(4)},
        {"name": "banana", "quantity": 4, "unit": "items", "grams_estimate": 480, "expiry": _in_days(3)},
    ]


def reset_inventory(store: DomainStore, user_id: str) -> None:
    """Remove the user's items and reservations. inventory_events is append-only and is kept."""
    with store._connection() as connection:  # private helper; acceptable in an ops script
        connection.execute("DELETE FROM inventory_reservations WHERE user_id=?", (user_id,))
        connection.execute("DELETE FROM inventory_items WHERE user_id=?", (user_id,))


def load_items(store: DomainStore, user_id: str, items: list[dict], source: str) -> tuple[int, int]:
    """Insert items, skipping ones already present so re-running does not double quantities."""
    existing = {
        (row["name"], row["unit"], row["expiry"]) for row in store.list_inventory(user_id)
    }
    added = skipped = 0
    for item in items:
        key = (str(item["name"]).strip().lower(), str(item.get("unit", "g")).strip().lower(), item.get("expiry"))
        if key in existing:
            skipped += 1
            continue
        try:
            store.upsert_inventory(user_id, {"confidence": 1.0, "source": source, **item})
            added += 1
        except ValueError as exc:
            print(f"  ! skipped {item.get('name')!r}: {exc}", file=sys.stderr)
            skipped += 1
    return added, skipped


def read_source_table(source_db: str, table: str, mapping: dict[str, str]) -> list[dict]:
    if not re.fullmatch(r"\w+", table):
        raise SystemExit(f"Invalid table name: {table!r}")
    if "name" not in mapping or "quantity" not in mapping:
        raise SystemExit("--map must include at least name=<col> and quantity=<col>")

    connection = sqlite3.connect(source_db)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(f"SELECT * FROM {table}").fetchall()  # table validated above
    finally:
        connection.close()

    items: list[dict] = []
    for row in rows:
        record = dict(row)
        name = record.get(mapping["name"])
        quantity = record.get(mapping["quantity"])
        if not name or quantity is None:
            continue
        unit = str(record.get(mapping.get("unit", ""), "g") or "g").strip().lower()
        item: dict = {
            "name": str(name).strip().lower(),
            "quantity": float(quantity),
            "unit": unit,
        }
        if "expiry" in mapping and record.get(mapping["expiry"]):
            item["expiry"] = str(record[mapping["expiry"]])[:10]  # keep YYYY-MM-DD

        grams = record.get(mapping["grams"]) if "grams" in mapping else None
        if grams is None:
            try:
                grams = to_grams(item["quantity"], unit)
            except ValueError:
                grams = None  # cups/pieces etc. need a density; row stays unreservable
                print(f"  ! no gram estimate for {item['name']!r} ({unit}); "
                      "it cannot be reserved/consumed until grams are set", file=sys.stderr)
        if grams is not None:
            item["grams_estimate"] = float(grams)
        items.append(item)
    return items


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--db", help="Target SQLite path (default: settings.workflow_database_path)")
    parser.add_argument("--reset", action="store_true", help="Delete this user's inventory before loading")
    parser.add_argument("--source", help="Import from this SQLite file instead of loading sample data")
    parser.add_argument("--table", default="inventory", help="Source table name (with --source)")
    parser.add_argument("--map", nargs="*", default=[], metavar="TARGET=COLUMN",
                        help="Column mapping: name, quantity, unit, grams, expiry")
    args = parser.parse_args()

    db_path = Path(args.db or settings.workflow_database_path).expanduser()
    store = DomainStore(db_path)  # creates tables if missing
    print(f"Target DB: {db_path.resolve()}  |  user_id: {args.user_id}")

    if args.reset:
        reset_inventory(store, args.user_id)
        print("Existing inventory cleared.")

    if args.source:
        mapping = dict(pair.split("=", 1) for pair in args.map)
        items = read_source_table(args.source, args.table, mapping)
        added, skipped = load_items(store, args.user_id, items, source="import")
    else:
        added, skipped = load_items(store, args.user_id, sample_items(), source="seed")

    print(f"Added {added}, skipped {skipped}.\n")
    print(f"{'name':<18}{'qty':>8} {'unit':<6}{'grams':>8}{'avail':>8}  expiry")
    for row in store.list_inventory(args.user_id):
        print(f"{row['name']:<18}{row['quantity']:>8g} {row['unit']:<6}"
              f"{(row['grams_estimate'] or 0):>8g}{row['available_grams']:>8g}  {row['expiry'] or '-'}")


if __name__ == "__main__":
    main()