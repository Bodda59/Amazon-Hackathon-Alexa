"""Hard completion checks shared by the supervisor and its tool boundary."""

from __future__ import annotations

from typing import Any


def finish(verification_passed: bool, result: dict[str, Any]) -> dict[str, Any]:
    """Return a final meal only when its deterministic verification passed."""
    verification = result.get("verification")
    if hasattr(verification, "model_dump"):
        verification = verification.model_dump(mode="json")
    passed = (
        isinstance(verification, dict)
        and verification.get("passed") is True
        and isinstance(verification.get("totals"), dict)
        and not verification.get("violations")
    )
    if not verification_passed or not passed:
        raise ValueError("Cannot finish with a meal whose deterministic verification did not pass.")
    result["status"] = "verified"
    return result
