"""The seven classic cards are deprecated in favour of the HelloFresh card.

Each must say so wherever someone meets it: in the card picker ("(deprecated)" in its name and
why in its description), in its own title (a "Deprecated" badge linking to the migration notes),
and once in the browser console. The HelloFresh card itself must not be marked, and the badge's
link must land on a real heading.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
WWW = ROOT / "custom_components" / "hellofresh" / "www"

CLASSIC = [
    "hellofresh-meal-planner-card",
    "hellofresh-market-card",
    "hellofresh-recipes-card",
    "hellofresh-food-profile-card",
    "hellofresh-schedule-card",
    "hellofresh-subscription-card",
    "hellofresh-cost-card",
]


def _picker_entry(source: str) -> str:
    start = source.index("window.customCards.push({")
    return source[start : source.index("});", start)]


@pytest.mark.parametrize("card", CLASSIC)
def test_a_classic_card_says_it_is_deprecated(card: str) -> None:
    source = (WWW / f"{card}.js").read_text(encoding="utf-8")
    picker = _picker_entry(source)
    assert re.search(r'name: "[^"]+ \(deprecated\)"', picker), "card picker name"
    assert "Deprecated: the HelloFresh card does all this and more" in picker, "picker description"
    assert f'warnDeprecated("{card}")' in source, "console warning when used"
    assert "${deprecatedBadge()}" in source, "badge in the card's title"


def test_the_hellofresh_card_is_not_deprecated() -> None:
    source = (WWW / "hellofresh-card.js").read_text(encoding="utf-8")
    assert 'name: "HelloFresh",' in _picker_entry(source)
    assert "deprecatedBadge" not in source and "warnDeprecated" not in source


def test_the_badge_links_to_the_migration_notes() -> None:
    shared = (WWW / "hellofresh-shared.js").read_text(encoding="utf-8")
    link = re.search(
        r'"https://github\.com/kedube/ha-hellofresh/blob/main/docs/dashboard\.md#([a-z0-9-]+)"',
        shared,
    )
    assert link, "the badge's link"
    assert link.group(1) == "moving-from-the-classic-cards"
    guide = (ROOT / "docs" / "dashboard.md").read_text(encoding="utf-8")
    assert re.search(r"^## Moving from the classic cards$", guide, re.M)
