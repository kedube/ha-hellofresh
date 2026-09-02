"""Tests for the Market card's section filter bar (Appetizers / Breakfast / Desserts / …).

Mirrors the meal-planner card's protein filter, with one structural difference worth
guarding: the planner's protein list is fixed, but Market sections are whatever the week's
catalog carries, so the chips must be derived from the viewed week's items and a slug
persisted from another week must not invisibly blank a week that lacks that section.

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
SOURCE = (WWW / "hellofresh-market-card.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

nodejs = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _method(name: str) -> str:
    match = re.search(rf"^  {name}\((?:\w+(?:, \w+)*)?\) \{{.*?^  \}}", SOURCE, re.S | re.M)
    assert match, f"{name} not found"
    return match.group(0)


def _active_sections(stored: list[str], week_sections: list[str]) -> list[str]:
    """Run the real _sectionsFor + _activeSectionFilter bodies; return the active slugs."""
    items = [{"item_id": f"i{n}", "group_type": s} for n, s in enumerate(week_sections)]
    script = f"""
    const self = {{
      _sectionFilter: new Set({json.dumps(stored)}),
      {_method("_sectionsFor").replace("_sectionsFor(", "sectionsFor(").replace("this.", "self.")},
      {_method("_activeSectionFilter").replace("_activeSectionFilter(", "activeSectionFilter(").replace("this._sectionsFor", "self.sectionsFor").replace("this.", "self.")},
    }};
    const week = {{ market_items: {json.dumps(items)} }};
    console.log(JSON.stringify([...self.activeSectionFilter(week)]));
    """
    result = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(result.stdout)


@nodejs
def test_active_filter_is_the_stored_set_intersected_with_the_week() -> None:
    active = _active_sections(["dessert", "sides"], ["appetizer", "dessert", "protein"])
    assert active == ["dessert"]


@nodejs
def test_a_stale_slug_never_blanks_a_week_without_that_section() -> None:
    """A filter persisted from another week must deactivate, not hide everything."""
    assert _active_sections(["sides"], ["appetizer", "dessert"]) == []


@nodejs
def test_history_weeks_have_no_sections_so_no_active_filter() -> None:
    """History-sourced items carry no group_type; the stored filter must not apply."""
    items = json.dumps([{"item_id": "i0"}])
    script = f"""
    const self = {{
      _sectionFilter: new Set(["dessert"]),
      {_method("_sectionsFor").replace("_sectionsFor(", "sectionsFor(").replace("this.", "self.")},
    }};
    console.log(JSON.stringify(self.sectionsFor({{ market_items: {items} }})));
    """
    result = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True
    )
    assert json.loads(result.stdout) == []


def test_chips_are_derived_from_the_weeks_own_sections() -> None:
    """No hardcoded section list: the bar must map _sectionsFor(week), like the catalog."""
    bar = _method("_renderSectionBar")
    assert "this._sectionsFor(week)" in bar
    assert 'data-action="filter-section"' in bar
    assert 'data-action="filter-section-all"' in bar, "the All chip is missing"


def test_filter_bar_respects_filters_apply() -> None:
    """Past weeks and 'show selected only' render no bar (nothing browsable to narrow)."""
    assert "this._filtersApply(week)" in _method("_renderSectionBar")
    apply = _method("_filtersApply")
    assert "_isPast" in apply
    assert "_showSelectedOnly" in apply


def test_selected_items_bypass_the_section_filter() -> None:
    """An item in your cart must never vanish because its section is filtered out."""
    groups = _method("_renderGroups")
    match = re.search(r"^[^\n]*activeSections\.has[^\n]*", groups, re.M)
    assert match, "section filter is never applied to the item list"
    assert "qtyOf(i) > 0 ||" in match.group(0), "selected items do not bypass the filter"


def test_quantity_fast_path_defers_to_full_render_under_a_section_filter() -> None:
    """Stepping a bypassed item to 0 changes visibility; the in-place tile swap can't."""
    change = _method("_renderQuantityChange")
    assert "_activeSectionFilter(week)" in change


def test_section_filter_persists_and_the_delegated_listener_routes_it() -> None:
    assert "hellofresh-market:section-filter" in SOURCE
    bind = re.search(r"^  _bindDelegated\(card\) \{.*?^  \}", SOURCE, re.S | re.M)
    assert bind, "_bindDelegated not found"
    assert "filter-section-all" in bind.group(0)
    assert "_toggleSectionFilter" in bind.group(0)
