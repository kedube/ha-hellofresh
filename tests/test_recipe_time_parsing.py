"""Regression tests for menu-recipe time parsing (the broken Total Cooking Time filter).

The authenticated weekly menu carries times as ISO-8601 durations (`prepTime: "PT35M"`,
2026-W35 HAR), but the week normalizer coerced them with `int()` only — so every menu
recipe had None for all three time fields, and the meal-planner card's Total Cooking Time
filter matched nothing.

Also pinned: the payload's swapped naming. `prepTime` is the headline time the website
shows on the tile ("35 min") and `totalTime` the smaller hands-on number — both map
verbatim, so consumers wanting "the displayed time" must read prep_time_minutes.
"""

from __future__ import annotations

from custom_components.hellofresh.normalizers import HelloFreshPayloadNormalizer, _coerce_minutes


def _recipe(recipe_fields: dict) -> object:
    norm = HelloFreshPayloadNormalizer.__new__(HelloFreshPayloadNormalizer)
    return norm._recipe_from_raw_meal({"recipe": {"id": "r1", "name": "Meal", **recipe_fields}})


def test_iso_duration_times_parse_to_minutes() -> None:
    """The live menu shape: ISO strings, prepTime the big number, totalTime the small one."""
    recipe = _recipe({"prepTime": "PT35M", "totalTime": "PT5M"})
    assert recipe.prep_time_minutes == 35
    assert recipe.total_time_minutes == 5


def test_integer_minutes_still_parse() -> None:
    """Other payload shapes carry plain ints; int-first coercion must keep working."""
    recipe = _recipe({"prepTime": 25, "cookTime": 10})
    assert recipe.prep_time_minutes == 25
    assert recipe.cook_time_minutes == 10
    assert recipe.total_time_minutes == 35  # cook + prep fallback when totalTime is absent


def test_hours_and_garbage() -> None:
    assert _coerce_minutes("PT1H30M") == 90
    assert _coerce_minutes("45") == 45
    assert _coerce_minutes("soon") is None
    assert _coerce_minutes(None) is None
