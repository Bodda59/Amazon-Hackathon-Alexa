"""Typed request and response contracts shared across tools and agents."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class NutritionTarget(BaseModel):
    """Requested exact or bounded meal macros; omitted values are unconstrained."""

    model_config = ConfigDict(extra="forbid")

    kcal: float | None = Field(default=None, gt=0, description="Exact calorie target.")
    protein_g: float | None = Field(default=None, gt=0, description="Exact protein target in grams.")
    carbs_g: float | None = Field(default=None, gt=0, description="Exact carbohydrate target in grams.")
    fat_g: float | None = Field(default=None, gt=0, description="Exact fat target in grams.")
    kcal_min: float | None = Field(default=None, gt=0, description="Minimum calories.")
    kcal_max: float | None = Field(default=None, gt=0, description="Maximum calories.")
    protein_g_min: float | None = Field(default=None, gt=0, description="Minimum protein in grams.")
    protein_g_max: float | None = Field(default=None, gt=0, description="Maximum protein in grams.")
    carbs_g_min: float | None = Field(default=None, gt=0, description="Minimum carbohydrates in grams.")
    carbs_g_max: float | None = Field(default=None, gt=0, description="Maximum carbohydrates in grams.")
    fat_g_min: float | None = Field(default=None, gt=0, description="Minimum fat in grams.")
    fat_g_max: float | None = Field(default=None, gt=0, description="Maximum fat in grams.")

    @model_validator(mode="after")
    def require_at_least_one_target(self) -> NutritionTarget:
        nutrient_names = ("kcal", "protein_g", "carbs_g", "fat_g")
        if all(
            getattr(self, name + suffix) is None
            for name in nutrient_names
            for suffix in ("", "_min", "_max")
        ):
            raise ValueError("Provide at least one calorie or macro target.")
        for name in nutrient_names:
            exact = getattr(self, name)
            minimum = getattr(self, f"{name}_min")
            maximum = getattr(self, f"{name}_max")
            if minimum is not None and maximum is not None and minimum > maximum:
                raise ValueError(f"{name}_min cannot be greater than {name}_max.")
            if exact is not None and minimum is not None and exact < minimum:
                raise ValueError(f"{name} cannot be below {name}_min.")
            if exact is not None and maximum is not None and exact > maximum:
                raise ValueError(f"{name} cannot be above {name}_max.")
        return self


class MacroTotals(BaseModel):
    kcal: float = Field(default=0, ge=0)
    protein_g: float = Field(default=0, ge=0)
    carbs_g: float = Field(default=0, ge=0)
    fat_g: float = Field(default=0, ge=0)


class IngredientPortion(BaseModel):
    name: str
    grams: float = Field(ge=0)
    per_100g: MacroTotals


class VerificationResult(BaseModel):
    passed: bool
    totals: MacroTotals
    deviation: dict[str, float] = Field(default_factory=dict)
    violations: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class MealPlanResult(BaseModel):
    status: str
    spoken_summary: str
    plan_id: str | None = None
    target: NutritionTarget | None = None
    ingredients: list[IngredientPortion] = Field(default_factory=list)
    verification: VerificationResult | None = None
    missing_items: list[str] = Field(default_factory=list)


class InventoryImage(BaseModel):
    """Image payload supplied as a data URL or base64 string plus media type."""

    data: str = Field(description="Base64-encoded image bytes; do not pass a remote URL.")
    mime_type: str = Field(description="Image MIME type, for example image/jpeg.")


class MCPToolResponse(BaseModel):
    """Consistent voice-first structured response for every public MCP tool."""

    model_config = ConfigDict(populate_by_name=True)

    status: str
    spoken_summary: str = Field(description="Concise one- or two-sentence voice response.")
    resource_uri: str = Field(alias="resourceUri", description="Related MCP App UI resource.")
    data: dict[str, Any] = Field(default_factory=dict)
