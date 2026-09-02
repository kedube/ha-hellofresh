"""Tests for menu-section parsing (`_build_menu_categories`) and badge colors.

The `categories` block is the source of the website's sectioned menu (This Week's Menu,
Health Conscious Menu, Family Menu, …). Shape guarded here, per the 2026-W35 HAR:
a section's meals live in `items[]` of `{id}` AND in `subcategories[].items[]` — a section
like "Featured" lists ONLY subcategories, so ignoring them would render it empty — and the
`market` pseudo-section holds add-ons, not meals, so it must not become a meal filter.

Badge colors (`label.foregroundColor`/`backgroundColor`) land in card inline styles, so the
normalizer must forward only strict hex literals.
"""

from __future__ import annotations

from custom_components.hellofresh.normalizers import (
    HelloFreshPayloadNormalizer,
    _safe_css_color,
)


def _norm() -> HelloFreshPayloadNormalizer:
    return HelloFreshPayloadNormalizer.__new__(HelloFreshPayloadNormalizer)


def _raw_week() -> dict:
    return {
        "categories": {
            "mainCategory": "dinners",
            "categories": [
                {
                    "name": "This Week's Menu",
                    "slug": "dinners",
                    "items": [{"id": "aaa"}, {"id": "bbb"}],
                    "subcategories": [
                        # Subcategory ids merge in, duplicates dropped.
                        {
                            "name": "High Protein",
                            "slug": "hp",
                            "items": [{"id": "bbb"}, {"id": "ccc"}],
                        },
                    ],
                },
                {
                    # The "Featured" shape: no items of its own, subcategories only.
                    "name": "Featured",
                    "slug": "featured",
                    "items": [],
                    "subcategories": [
                        {"name": "Prep & Bake", "slug": "pb", "items": [{"id": "ddd"}]},
                    ],
                },
                {
                    # Market members are add-ons, not meals — must be skipped.
                    "name": "HelloFresh Market",
                    "slug": "market",
                    "items": [{"id": "eee"}],
                },
                {"name": "Empty", "slug": "empty", "items": []},  # nothing to filter on
                "not-a-dict",
            ],
        }
    }


def test_sections_merge_their_subcategory_ids() -> None:
    rows = _norm()._build_menu_categories(_raw_week())
    dinners = next(r for r in rows if r["slug"] == "dinners")
    assert dinners["name"] == "This Week's Menu"
    assert dinners["recipe_ids"] == ["aaa", "bbb", "ccc"]  # deduped, order preserved


def test_subcategory_only_sections_are_not_empty() -> None:
    """ "Featured" lists only subcategories; their ids must carry the section."""
    rows = _norm()._build_menu_categories(_raw_week())
    featured = next(r for r in rows if r["slug"] == "featured")
    assert featured["recipe_ids"] == ["ddd"]


def test_market_and_id_less_sections_are_dropped() -> None:
    slugs = [r["slug"] for r in _norm()._build_menu_categories(_raw_week())]
    assert "market" not in slugs, "Market members are add-ons, not meals"
    assert "empty" not in slugs, "a section with no ids can never match anything"


def test_categories_are_found_under_the_nested_menu_payload() -> None:
    """Merged weeks keep the menu payload under raw._menu_payload, like addOns."""
    nested = {"_menu_payload": _raw_week()}
    rows = _norm()._build_menu_categories(nested)
    assert [r["slug"] for r in rows] == ["dinners", "featured"]


def test_missing_or_malformed_categories_yield_no_sections() -> None:
    assert _norm()._build_menu_categories({}) == []
    assert _norm()._build_menu_categories({"categories": "nope"}) == []
    assert _norm()._build_menu_categories({"categories": {"categories": None}}) == []


def test_badge_colors_pass_only_strict_hex() -> None:
    assert _safe_css_color("#FFFFFF") == "#FFFFFF"
    assert _safe_css_color("#fff") == "#fff"
    assert _safe_css_color("#00FF0080") == "#00FF0080"  # RRGGBBAA
    # Anything else is dropped: these end up inside a style attribute.
    for bad in ("red", "#GGGGGG", "#ff", 'red" onmouseover="x', "url(evil)", None, 7):
        assert _safe_css_color(bad) is None


def test_recipe_carries_its_badge_colors() -> None:
    recipe = _norm()._recipe_from_raw_meal(
        {
            "recipe": {
                "id": "r1",
                "name": "Test Meal",
                "label": {
                    "text": "BESTSELLER",
                    "handle": "bestseller",
                    "foregroundColor": "#FFFFFF",
                    "backgroundColor": "#067A46",
                },
            }
        }
    )
    assert recipe.badge == "BESTSELLER"
    assert recipe.badge_foreground == "#FFFFFF"
    assert recipe.badge_background == "#067A46"


def test_bad_badge_colors_are_dropped_but_text_kept() -> None:
    recipe = _norm()._recipe_from_raw_meal(
        {
            "recipe": {
                "id": "r1",
                "name": "Test Meal",
                "label": {"text": "NEW", "foregroundColor": "white", "backgroundColor": 'x"y'},
            }
        }
    )
    assert recipe.badge == "NEW"
    assert recipe.badge_foreground is None
    assert recipe.badge_background is None
