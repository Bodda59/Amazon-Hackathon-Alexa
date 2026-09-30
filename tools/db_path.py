"""Single source of truth for where kitchen.db lives."""
from pathlib import Path


def find_kitchen_db() -> Path:
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        if (parent / "run.py").exists() or (parent / "pyproject.toml").exists():
            cand = parent / "database" / "kitchen.db"
            if cand.exists():
                return cand
            cand = parent / "kitchen.db"
            if cand.exists():
                return cand
            return parent / "database" / "kitchen.db"
    return here.parent.parent / "database" / "kitchen.db"


KITCHEN_DB = find_kitchen_db()


def assert_kitchen_db() -> Path:
    """Raise a loud error instead of silently creating an empty DB."""
    if not KITCHEN_DB.exists():
        raise FileNotFoundError(
            f"kitchen.db not found at {KITCHEN_DB}. "
            f"Run:  uv run python database/kitchen.py --reset"
        )
    return KITCHEN_DB