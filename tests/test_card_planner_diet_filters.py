"""Tests for the meal-planner card's dietary filter chips (GLP-1 Support, Carb Conscious,
High Protein, Under 650 Calories, Gluten-Free Friendly, Sodium Smart, Low Added Sugar,
Under 20/30 Minutes).

The semantics worth guarding: alias tags must match case-insensitively (HelloFresh renames
these — recipes carry "GLP-1 Friendly" beside the site's "GLP-1 Support" category, and
"Calorie Smart" became "Under 650 Calories"); the numeric fallbacks must catch untagged
meals the recipe's own numbers can answer; and selected dietary chips AND together
(constraints), unlike the protein chips (which OR).

The behavioural tests run the real method bodies against stubs under Node.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

WWW = Path(__file__).resolve().parents[1] / "custom_components" / "hellofresh" / "www"
SOURCE = (WWW / "hellofresh-meal-planner-card.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

nodejs = pytest.mark.skipif(NODE is None, reason="node is not installed")

USER_REQUESTED_LABELS = [
    "GLP-1 Support",
    "Carb Conscious",
    "High Protein",
    "Under 650 Calories",
    "Gluten-Free Friendly",
    "Sodium Smart",
    "Low Added Sugar",
    "Under 20 Minutes",
    "Under 30 Minutes",
    # Rounding out the site's Health Conscious section and cooking-time filter (HAR .46):
    "High Fiber",
    "Mediterranean",
    "Under 15 Minutes",
]


def _method(name: str) -> str:
    match = re.search(rf"^  {name}\((?:\w+(?:, \w+)*)?\) \{{.*?^  \}}", SOURCE, re.S | re.M)
    assert match, f"{name} not found"
    return match.group(0)


def _diet_filters_literal() -> str:
    match = re.search(r"^  static get DIET_FILTERS\(\) \{.*?^  \}", SOURCE, re.S | re.M)
    assert match, "DIET_FILTERS not found"
    body = match.group(0).split("{", 1)[1].rsplit("}", 1)[0]
    return f"(function() {{ {body} }})()"


def _passes(active: list[str], recipe: dict) -> bool:
    """Run the real _passesDietFilters/_matchesDietFilter bodies against one recipe."""
    script = f"""
    const HelloFreshMealPlannerCard = {{ DIET_FILTERS: {_diet_filters_literal()} }};
    const self = {{
      _dietFilter: new Set({json.dumps(active)}),
      {_method("_matchesDietFilter").replace("_matchesDietFilter(", "matchesDietFilter(")},
      {_method("_passesDietFilters").replace("_passesDietFilters(", "passesDietFilters(").replace("this.", "self.").replace("self.matchesDietFilter", "self.matchesDietFilter")},
    }};
    self._matchesDietFilter = self.matchesDietFilter;
    console.log(JSON.stringify(self.passesDietFilters({json.dumps(recipe)})));
    """
    result = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(result.stdout)


@nodejs
def test_alias_tags_match_case_insensitively() -> None:
    """API spelling drift must not break a category: one HAR capture carries "GLP-1 Support",
    "GLP-1 Friendly" AND "GLP-1 Balance" as tags in the same season's menus."""
    assert _passes(["glp1"], {"tags": ["GLP-1 Friendly"]}) is True
    assert _passes(["glp1"], {"tags": ["GLP-1 Balance"]}) is True
    assert _passes(["glp1"], {"tags": ["glp-1 support"]}) is True
    assert _passes(["carb-conscious"], {"tags": ["Carb Smart"]}) is True
    assert _passes(["carb-conscious"], {"tags": ["Family Friendly"]}) is False


@nodejs
def test_fiber_category_matches_both_site_spellings() -> None:
    """The filter panel says "Fiber Powered", the menu section "High Fiber", and recipes have
    carried "High Fiber" and "Fiber Filled" tags — all must land in one chip."""
    assert _passes(["high-fiber"], {"tags": ["High Fiber"]}) is True
    assert _passes(["high-fiber"], {"tags": ["Fiber Filled"]}) is True
    assert _passes(["high-fiber"], {"tags": ["Fiber Powered"]}) is True


@nodejs
def test_under_15_uses_the_time_fallback_like_its_siblings() -> None:
    assert _passes(["under-15-min"], {"tags": [], "total_time_minutes": 15}) is True
    assert _passes(["under-15-min"], {"tags": [], "total_time_minutes": 18}) is False


@nodejs
def test_contains_gluten_does_not_match_gluten_free() -> None:
    """Both tags exist in real payloads; substring matching would pass the wrong meals."""
    assert _passes(["gluten-free"], {"tags": ["Contains Gluten"]}) is False
    assert _passes(["gluten-free"], {"tags": ["Gluten-Free Friendly"]}) is True
    assert _passes(["gluten-free"], {"tags": ["gluten free"]}) is True


@nodejs
def test_selected_diet_chips_and_together() -> None:
    """Two chips = both constraints; a meal passing only one must be hidden."""
    both = {"tags": ["High Protein"], "total_time_minutes": 25}
    slow = {"tags": ["High Protein"], "total_time_minutes": 45}
    assert _passes(["high-protein", "under-30-min"], both) is True
    assert _passes(["high-protein", "under-30-min"], slow) is False


@nodejs
def test_calorie_category_falls_back_to_the_recipes_own_number() -> None:
    """An untagged 600 kcal meal still belongs under "Under 650 Calories"."""
    assert _passes(["under-650-cal"], {"tags": [], "calories_kcal": 600}) is True
    assert _passes(["under-650-cal"], {"tags": [], "calories_kcal": 700}) is False
    assert _passes(["under-650-cal"], {"tags": ["Calorie Smart"], "calories_kcal": None}) is True


@nodejs
def test_time_categories_use_total_time_with_prep_fallback() -> None:
    assert _passes(["under-20-min"], {"tags": [], "total_time_minutes": 20}) is True
    assert _passes(["under-20-min"], {"tags": [], "total_time_minutes": 25}) is False
    # total missing: prep time answers instead of the meal silently failing the filter.
    assert _passes(["under-30-min"], {"tags": [], "prep_time_minutes": 30}) is True
    # neither time known and no tag: the meal cannot claim the category.
    assert _passes(["under-30-min"], {"tags": []}) is False


def test_every_requested_category_is_defined() -> None:
    for label in USER_REQUESTED_LABELS:
        assert f'label: "{label}"' in SOURCE, f"missing dietary filter: {label}"


def test_filter_bar_renders_the_diet_group() -> None:
    bar = _method("_renderFilterBar")
    assert "DIET_FILTERS.map(" in bar, "diet chips are not rendered from DIET_FILTERS"
    assert 'data-action="filter-diet"' in bar
    assert 'data-action="filter-diet-all"' in bar, "the All chip is missing"


def test_grid_filter_consults_the_diet_filter() -> None:
    """_passesFilters is what the grid applies; the diet check must be wired into it."""
    assert "_passesDietFilters(r)" in _method("_passesFilters")
    assert "_dietFilter.size" in _method("_hasActiveFilters"), (
        "in-place tile updates would desync the grid while a diet filter is active"
    )


def test_diet_filter_persists_and_the_delegated_listener_routes_it() -> None:
    assert "hellofresh-meal-planner:diet-filter" in SOURCE
    bind = re.search(r"^  _bindDelegated\(card\) \{.*?^  \}", SOURCE, re.S | re.M)
    assert bind, "_bindDelegated not found"
    assert "filter-diet-all" in bind.group(0)
    assert "_toggleDietFilter" in bind.group(0)


def test_stored_diet_keys_are_validated_on_load() -> None:
    """A renamed/removed category key in localStorage must be dropped, not kept forever."""
    load = _method("_loadDietFilter")
    assert "DIET_FILTERS" in load
    assert "filter" in load
