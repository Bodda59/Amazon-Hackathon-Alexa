"""Small deterministic mass-unit conversions; unknown/density units are rejected."""

from __future__ import annotations

_MASS_TO_GRAMS = {
    "g": 1.0,
    "gram": 1.0,
    "grams": 1.0,
    "kg": 1000.0,
    "kilogram": 1000.0,
    "kilograms": 1000.0,
    "mg": 0.001,
    "oz": 28.349523125,
    "ounce": 28.349523125,
    "ounces": 28.349523125,
    "lb": 453.59237,
    "lbs": 453.59237,
    "pound": 453.59237,
    "pounds": 453.59237,
}


def to_grams(quantity: float, unit: str) -> float:
    """Convert a mass to grams; volume and count units need ingredient density."""
    normalized = unit.strip().lower()
    try:
        factor = _MASS_TO_GRAMS[normalized]
    except KeyError as exc:
        raise ValueError(f"Unsupported mass unit: {unit!r}") from exc
    if quantity < 0:
        raise ValueError("Quantity cannot be negative.")
    return quantity * factor
