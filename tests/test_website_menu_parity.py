"""The Menu grid shows what hellofresh.com shows, on a real week.

``fixtures/website_menu_2026_w42.json`` is a US 2026-W42 menu trimmed from a website capture
(HAR 57), with the tiles the site showed: unfiltered, each of the 86 dishes as written (463 meals
in all — every protein swap, 2× portion and add-on is a meal of its own); under each protein
filter, per dish the dish itself when it matches, else its first matching option. It also holds
what HelloFresh's filter service answered for each of those filters.

This runs the integration's real normalizer on the menu, then the card's real tile logic under
Node, and requires the same tiles as the website — the end-to-end check behind "one tile per
dish, like the website".
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from custom_components.hellofresh.models import HelloFreshSubscription
from custom_components.hellofresh.normalizers import HelloFreshPayloadNormalizer

ROOT = Path(__file__).resolve().parents[1]
LOGIC = ROOT / "custom_components" / "hellofresh" / "www" / "hellofresh-card-logic.js"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "website_menu_2026_w42.json"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

# The website's main-protein slugs and the card's chips for them.
CHIPS = {
    "fish-seafood": "Seafood",
    "beef": "Beef",
    "poultry": "Poultry",
    "pork": "Pork",
    "vegetarian": "Veggie",
}
# "Chinese-Style Speedy Ramen Noodles with 2x Tofu": a swap on a beef dish whose protein the menu
# leaves blank (as a meal and as an option); only the filter service puts it under Veggie.
TOFU_SWAP = 453


def _normalized_week() -> tuple[dict, dict]:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    normalizer = HelloFreshPayloadNormalizer()
    weeks = normalizer._normalize_menu_weeks(
        [fixture["menu"]],
        HelloFreshSubscription(subscription_id="sub", account_id="acct", meals_required=3),
    )
    normalizer._apply_variation_titles(weeks)
    normalizer._apply_menu_filters(weeks)
    week = weeks[0]
    return fixture, {
        "week_id": week.week_id,
        "recipes": [recipe.as_dict() for recipe in week.recipes],
        "menu_filters": week.menu_filters,
    }


def test_the_menu_shows_the_websites_tiles(tmp_path: Path) -> None:
    fixture, week = _normalized_week()
    data = tmp_path / "week.json"
    data.write_text(json.dumps({"week": week, "courses": fixture["courses"], "chips": CHIPS}))
    script = f"""
      globalThis.window = {{ localStorage: {{ getItem: () => null, setItem() {{}} }} }};
      const {{ readFileSync }} = await import("node:fs");
      const L = await import({json.dumps(LOGIC.as_uri() + "?v=test")});
      const {{ week, courses, chips }} = JSON.parse(readFileSync({json.dumps(str(data))}, "utf8"));
      const none = () => false;
      const tiles = (opts) =>
        L.menuTiles(week.recipes, {{ sel: none, applyFilters: true, ...opts }})
          .map((t) => t.recipe.course_index)
          .sort((a, b) => a - b);
      const out = {{ none: tiles({{}}), queries: {{}}, service: {{}}, menuOnly: {{}} }};
      for (const [slug, chip] of Object.entries(chips)) {{
        const protein = new Set([chip]);
        out.queries[slug] = L.proteinServerFilters(week, protein);
        out.service[slug] = tiles({{ protein, proteinIds: new Set(courses[slug]) }});
        out.menuOnly[slug] = tiles({{ protein }});
      }}
      console.log(JSON.stringify(out));
    """
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "TZ": "America/New_York"},
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    website = fixture["website_tiles"]

    assert len(week["recipes"]) == 463
    assert out["none"] == website["none"] and len(out["none"]) == 86
    for slug in CHIPS:
        # The card asks the filter service exactly what the site asked it...
        assert out["queries"][slug] == {"main-protein": [slug]}
        # ...and, with the answer, shows the site's tiles, tile for tile.
        assert out["service"][slug] == website[slug], slug

    # From the menu data alone every protein already matches the site, except the tofu swap.
    for slug in CHIPS:
        expected = [i for i in website[slug] if i != TOFU_SWAP]
        assert out["menuOnly"][slug] == expected, slug
    assert TOFU_SWAP in website["vegetarian"]
