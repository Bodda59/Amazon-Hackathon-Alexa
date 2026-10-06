"""Tests for deterministic macro arithmetic and verification guards."""

import unittest

from meal_agent.graph.supervisor import finish
from meal_agent.schemas import IngredientPortion, MacroTotals, NutritionTarget
from meal_agent.tools.nutrition import compute_macros, verify_macros


class NutritionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.portions = [
            IngredientPortion(
                name="sample",
                grams=200,
                per_100g=MacroTotals(kcal=150, protein_g=20, carbs_g=10, fat_g=5),
            )
        ]

    def test_compute_macros_scales_from_100g(self) -> None:
        totals = compute_macros(self.portions)
        self.assertEqual(totals.kcal, 300)
        self.assertEqual(totals.protein_g, 40)

    def test_verify_macros_passes_within_tolerance(self) -> None:
        result = verify_macros(
            self.portions,
            NutritionTarget(kcal=300, protein_g=40),
        )
        self.assertTrue(result.passed)

    def test_verify_macros_fails_outside_tolerance(self) -> None:
        result = verify_macros(
            self.portions,
            NutritionTarget(kcal=500, protein_g=40),
        )
        self.assertFalse(result.passed)

    def test_verify_macros_enforces_minimum_and_maximum_bounds(self) -> None:
        result = verify_macros(
            self.portions,
            NutritionTarget(protein_g_min=40, carbs_g_max=20),
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.deviation["protein_g_min"], 0)
        self.assertEqual(result.deviation["carbs_g_max"], 0)

        failing_result = verify_macros(
            self.portions,
            NutritionTarget(protein_g_min=45),
        )
        self.assertFalse(failing_result.passed)

    def test_finish_rejects_unverified_results(self) -> None:
        with self.assertRaises(ValueError):
            finish(False, {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
