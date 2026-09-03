"""Tests for the meal-planner card's Highlights chips (New / Bestsellers / Cooked Before),
menu-section filter, alias-driven tile chips, and HelloFresh badge colors.

Semantics guarded: highlight chips are SINGLE-SELECT (mutually exclusive views — a meal
can't be new AND cooked before, and a new meal isn't a bestseller yet), with the old
multi-select stored-array format migrated; the tile chips share the filter bar's DIET_FILTERS alias
table so a HelloFresh tag renaming can't drop a chip the filter still matches; a stale menu
section slug deactivates rather than blanking a week that lacks it; and badge colors reach an
inline style only as strict hex.

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


def _method(name: str) -> str:
    match = re.search(rf"^  {name}\((?:\w+(?:, \w+)*)?\) \{{.*?^  \}}", SOURCE, re.S | re.M)
    assert match, f"{name} not found"
    return match.group(0)


def _run(script: str) -> object:
    result = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(result.stdout)


def _passes_highlight(selected: str, recipe: dict) -> bool:
    script = f"""
    const self = {{
      _highlightFilter: {json.dumps(selected)},
      {_method("_matchesHighlight").replace("_matchesHighlight(", "matchesHighlight(")},
      {_method("_passesHighlightFilter").replace("_passesHighlightFilter(", "passesHighlightFilter(").replace("this.", "self.")},
    }};
    self._matchesHighlight = self.matchesHighlight;
    console.log(JSON.stringify(self.passesHighlightFilter({json.dumps(recipe)})));
    """
    return _run(script)


@nodejs
def test_highlight_signals_come_from_tag_badge_or_delivered_count() -> None:
    assert _passes_highlight("new", {"tags": ["New"]}) is True
    assert _passes_highlight("new", {"tags": [], "badge": "NEW"}) is True
    assert _passes_highlight("bestseller", {"tags": ["Bestseller"]}) is True
    assert _passes_highlight("bestseller", {"tags": [], "badge": "BESTSELLER"}) is True
    assert _passes_highlight("cooked-before", {"tags": [], "delivered_count": 2}) is True
    assert _passes_highlight("cooked-before", {"tags": [], "delivered_count": 0}) is False
    assert _passes_highlight("", {"tags": ["Quick"]}) is True  # nothing selected: all pass


@nodejs
def test_highlights_are_single_select_and_reselecting_clears() -> None:
    """The highlights are mutually exclusive views (a meal can't be new AND cooked before),
    so the chips single-select like the cooking-time group: picking one replaces the other,
    and tapping the active chip (or All) clears."""
    select = (
        _method("_selectHighlightFilter")
        .replace("_selectHighlightFilter(", "selectHighlightFilter(")
        .replace("this.", "self.")
    )
    script = f"""
    const HelloFreshMealPlannerCard = {{
      HIGHLIGHT_FILTERS: [{{ key: "new" }}, {{ key: "bestseller" }}, {{ key: "cooked-before" }}],
      HIGHLIGHT_STORAGE_KEY: "k",
    }};
    const window = {{ localStorage: {{ setItem() {{}} }} }};
    const self = {{ _highlightFilter: "", _render() {{}}, {select} }};
    const out = [];
    self.selectHighlightFilter("new");
    out.push(self._highlightFilter);
    self.selectHighlightFilter("bestseller"); // replaces, never combines
    out.push(self._highlightFilter);
    self.selectHighlightFilter("bestseller"); // tap the active chip: clears
    out.push(self._highlightFilter);
    self.selectHighlightFilter("nonsense"); // unknown key: clears rather than sticking
    out.push(self._highlightFilter);
    console.log(JSON.stringify(out));
    """
    assert _run(script) == ["new", "bestseller", "", ""]


@nodejs
def test_stored_multi_select_arrays_migrate_to_one_key() -> None:
    """Earlier versions stored a JSON array; the first still-valid key survives the upgrade."""
    load = (
        _method("_loadHighlightFilter")
        .replace("_loadHighlightFilter(", "loadHighlightFilter(")
        .replace("this.", "self.")
    )
    script = f"""
    const HelloFreshMealPlannerCard = {{
      HIGHLIGHT_FILTERS: [{{ key: "new" }}, {{ key: "bestseller" }}, {{ key: "cooked-before" }}],
      HIGHLIGHT_STORAGE_KEY: "k",
    }};
    let stored;
    const window = {{ localStorage: {{ getItem: () => stored }} }};
    const self = {{ {load} }};
    const out = [];
    for (stored of ["bestseller", '["gone","new"]', "[]", "stale", null]) {{
      out.push(self.loadHighlightFilter());
    }}
    console.log(JSON.stringify(out));
    """
    assert _run(script) == ["bestseller", "new", "", "", ""]


def _active_section_ids(selected: str, categories: list[dict]) -> list[str] | None:
    script = f"""
    const self = {{
      _menuSection: {json.dumps(selected)},
      {_method("_activeMenuSectionIds").replace("_activeMenuSectionIds(", "activeMenuSectionIds(").replace("this.", "self.")},
    }};
    const out = self.activeMenuSectionIds({{ menu_categories: {json.dumps(categories)} }});
    console.log(JSON.stringify(out ? [...out] : null));
    """
    return _run(script)


@nodejs
def test_section_filter_resolves_against_the_weeks_own_sections() -> None:
    cats = [{"name": "Family Menu", "slug": "family", "recipe_ids": ["aaa", "bbb-suffix"]}]
    assert _active_section_ids("family", cats) == ["aaa", "bbb"]  # bare-id normalized


@nodejs
def test_a_stale_section_slug_deactivates_instead_of_blanking_the_week() -> None:
    cats = [{"name": "Family Menu", "slug": "family", "recipe_ids": ["aaa"]}]
    assert _active_section_ids("dinners-specials", cats) is None
    assert _active_section_ids("", cats) is None


def _tile_chips(recipe: dict) -> list[str]:
    diet = re.search(r"^  static get DIET_FILTERS\(\) \{.*?^  \}", SOURCE, re.S | re.M)
    assert diet, "DIET_FILTERS not found"
    body = diet.group(0).split("{", 1)[1].rsplit("}", 1)[0]
    script = f"""
    const HelloFreshMealPlannerCard = {{ DIET_FILTERS: (function() {{ {body} }})() }};
    const self = {{
      {_method("_tileChipLabels").replace("_tileChipLabels(", "tileChipLabels(")},
    }};
    console.log(JSON.stringify(self.tileChipLabels({json.dumps(recipe)})));
    """
    return _run(script)


@nodejs
def test_tile_chips_share_the_filter_bars_alias_table() -> None:
    """The shipped bug: tiles matched the exact string "GLP-1 Friendly", so meals tagged
    "GLP-1 Balance"/"GLP-1 Support" silently lost their chip while the filter still worked."""
    assert _tile_chips({"tags": ["GLP-1 Balance"]}) == ["GLP-1 Support"]
    assert _tile_chips({"tags": ["glp-1 friendly"]}) == ["GLP-1 Support"]
    assert _tile_chips({"tags": ["Carb Smart"]}) == ["Carb Conscious"]


@nodejs
def test_tile_chips_skip_time_categories_and_dedupe_against_the_badge() -> None:
    # Numeric-only matches never earn a chip (that's the FILTER widening, not a label),
    # and a chip that would repeat the badge text is dropped.
    assert _tile_chips({"tags": [], "calories_kcal": 500}) == []
    assert _tile_chips({"tags": ["GLP-1 Support"], "badge": "GLP-1 Support"}) == []
    assert _tile_chips({"tags": ["double-protein"]}) == ["2x Protein"]


@nodejs
def test_badge_style_emits_only_strict_hex() -> None:
    script = f"""
    const self = {{
      {_method("_badgeStyle").replace("_badgeStyle(", "badgeStyle(")},
    }};
    const out = [
      self.badgeStyle({{ badge_background: "#067A46", badge_foreground: "#FFFFFF" }}),
      self.badgeStyle({{ badge_background: 'red" onmouseover="x', badge_foreground: "white" }}),
      self.badgeStyle({{}}),
    ];
    console.log(JSON.stringify(out));
    """
    styled, injected, empty = _run(script)
    assert styled == ' style="background:#067A46;color:#FFFFFF"'
    assert injected == ""
    assert empty == ""


def test_grid_and_bar_are_wired_for_the_new_filters() -> None:
    grid = _method("_renderGrid")
    assert "_activeMenuSectionIds(week)" in grid, "section filter never reaches the grid"
    assert "ctx.sel(r) || sectionIds.has" in grid, "selected meals must bypass the section"
    assert "_passesHighlightFilter(r)" in _method("_passesFilters")
    bar = _method("_renderFilterBar")
    assert "_renderMenuSectionGroup(week)" in bar
    assert 'data-action="filter-highlight"' in bar
    section_group = _method("_renderMenuSectionGroup")
    assert "menu_categories" in section_group, "section chips must come from the week payload"
    has_active = _method("_hasActiveFilters")
    assert "_highlightFilter" in has_active
    assert "_menuSection" in has_active
    bind = re.search(r"^  _bindDelegated\(card\) \{.*?^  \}", SOURCE, re.S | re.M)
    assert bind, "_bindDelegated not found"
    for action in ("filter-highlight-all", "filter-section-all"):
        assert action in bind.group(0), f"{action} is not routed"


def test_badge_chip_renders_with_its_style() -> None:
    tile = _method("_renderRecipeTile")
    assert "_badgeStyle(r)" in tile, "badge colors are parsed but never rendered"
    assert "_tileChipLabels(r)" in tile, "tile chips no longer share the alias table"


def test_skip_resync_stays_on_the_toggled_week() -> None:
    """Skipping/unskipping refetches; without keepWeekId the landing logic jumps the view
    back to the current week, losing the user's place (saves already passed it)."""
    toggle = re.search(r"^  async _toggleSkip\(week\) \{.*?^  \}", SOURCE, re.S | re.M)
    assert toggle, "_toggleSkip not found"
    assert "_fetchWeeks(week.week_id)" in toggle.group(0)


def test_tile_stats_show_the_headline_cooking_time() -> None:
    """The stats line shows the same headline time the website's tile does. The payload's
    naming is swapped (prep_time_minutes IS the headline number), so the tile must prefer
    prep over total — the same choice the Total Cooking Time filter makes."""
    tile = _method("_renderRecipeTile")
    assert re.search(
        r"r\.prep_time_minutes != null \? r\.prep_time_minutes : r\.total_time_minutes", tile
    ), "tile time must prefer the headline prep_time_minutes"
    assert "min`" in tile, "the minutes stat is not rendered"


# ---- collapsible filter panel --------------------------------------------------------------
#
# Six chip groups drowned the menu grid, so the panel is collapsed by default: a single
# "Filters · N active" header plus the active selections as removable ✕ chips; expanding
# reveals each group on its own aligned row (_filterRow).


def _statics_literal(*names: str) -> str:
    out = []
    for name in names:
        match = re.search(rf"^  static get {name}\(\) \{{.*?^  \}}", SOURCE, re.S | re.M)
        assert match, f"{name} not found"
        body = match.group(0).split("{", 1)[1].rsplit("}", 1)[0]
        out.append(f"{name}: (function() {{ {body} }})()")
    return ", ".join(out)


def _summary(state: dict, week: dict) -> list[dict]:
    script = f"""
    const HelloFreshMealPlannerCard = {{ {
        _statics_literal("PROTEIN_FILTERS", "DIET_FILTERS", "TIME_FILTERS", "HIGHLIGHT_FILTERS")
    } }};
    const self = {{
      _proteinFilter: new Set({json.dumps(state.get("protein", []))}),
      _dietFilter: new Set({json.dumps(state.get("diet", []))}),
      _timeFilter: {json.dumps(state.get("time", ""))},
      _highlightFilter: {json.dumps(state.get("highlight", ""))},
      _menuSection: {json.dumps(state.get("section", ""))},
      _showVariants: {json.dumps(state.get("variants", True))},
      {
        _method("_activeMenuSectionIds")
        .replace("_activeMenuSectionIds(", "activeMenuSectionIds(")
        .replace("this.", "self.")
    },
      {
        _method("_activeFilterSummary")
        .replace("_activeFilterSummary(", "activeFilterSummary(")
        .replace("this.", "self.")
    },
    }};
    self._activeMenuSectionIds = self.activeMenuSectionIds;
    console.log(JSON.stringify(self.activeFilterSummary({json.dumps(week)})));
    """
    return _run(script)


@nodejs
def test_active_summary_lists_every_narrowing_filter() -> None:
    week = {"menu_categories": [{"name": "Family Menu", "slug": "family", "recipe_ids": ["a"]}]}
    rows = _summary(
        {
            "protein": ["Beef"],
            "diet": ["high-protein"],
            "time": "under-30-min",
            "highlight": "new",
            "section": "family",
            "variants": False,
        },
        week,
    )
    assert [r["label"] for r in rows] == [
        "Beef",
        "High Protein",
        "Under 30 Minutes",
        "New",
        "Family Menu",
        "Variants hidden",
    ]


@nodejs
def test_summary_omits_a_section_the_week_does_not_carry() -> None:
    """A stale slug filters nothing on this week, so it must not count as active."""
    week = {"menu_categories": [{"name": "Family Menu", "slug": "family", "recipe_ids": ["a"]}]}
    rows = _summary({"section": "dinners-specials"}, week)
    assert rows == []


def test_panel_is_collapsed_by_default_and_persisted() -> None:
    load = _method("_loadFiltersExpanded")
    assert re.search(r'===\s*"1"', load), "expansion must be opt-in (collapsed by default)"
    assert "return false" in load, "storage failure must fall back to collapsed"
    assert "hellofresh-meal-planner:filters-expanded" in SOURCE


def test_collapsed_bar_shows_removable_chips_and_no_group_rows() -> None:
    bar = _method("_renderFilterBar")
    collapsed = bar.split("if (!expanded)")[1].split("const allActive")[0]
    assert 'data-action="remove-filter"' in collapsed
    assert "_filterRow(" not in collapsed, "group rows must only render when expanded"
    assert 'data-action="toggle-filters"' in bar


def test_remove_filter_routes_each_kind_to_its_toggle() -> None:
    remove = _method("_removeFilter")
    for needle in (
        "_toggleProteinFilter",
        "_toggleDietFilter",
        '_selectTimeFilter("")',
        '_selectHighlightFilter("")',
        '_selectMenuSection("")',
        "_toggleShowVariants",
    ):
        assert needle in remove, f"{needle} not routed from the summary chips"
    bind = re.search(r"^  _bindDelegated\(card\) \{.*?^  \}", SOURCE, re.S | re.M)
    assert bind, "_bindDelegated not found"
    assert "toggle-filters" in bind.group(0)
    assert "remove-filter" in bind.group(0)
