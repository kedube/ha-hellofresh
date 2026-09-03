"""Guards on the shared recipe-detail sheet and its three consumers.

The sheet was extracted from the Recipes card so the Meal planner and Market cards could offer
the same tap-through view. Three things are easy to get wrong when a module is shared this way,
and each is checked here:

* **Shipping.** The module is a dependency, not a Lovelace resource, so it is deliberately NOT
  registered as a card (see tests/test_frontend.py). Nothing else would notice it missing.
* **Cache-busting.** Lovelace stamps ``?v=`` only onto the card URLs it registers. A static
  ``./x.js`` import resolves to the bare filename, so a browser could keep serving an old copy
  of the shared module after an upgrade while the cards themselves refreshed. Every consumer
  therefore imports it dynamically with its own version appended.
* **Teardown.** The overlay registers a document-level Escape handler, so a card that forgets
  to close it on disconnect leaks a listener that keeps the detached card alive.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

WWW = Path(__file__).resolve().parents[1] / "custom_components" / "hellofresh" / "www"
DETAIL_MODULE = WWW / "hellofresh-recipe-detail.js"

# Cards that offer the tap-through recipe sheet.
CONSUMERS = {
    "recipes": WWW / "hellofresh-recipes-card.js",
    "planner": WWW / "hellofresh-meal-planner-card.js",
    "market": WWW / "hellofresh-market-card.js",
}


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_shared_module_exists_and_exports_what_consumers_use() -> None:
    source = _source(DETAIL_MODULE)
    assert "export class RecipeDetailOverlay" in source
    assert "export const DETAIL_STYLES" in source


def test_overlay_is_fixed_positioned_not_absolute() -> None:
    """The sheet must not depend on its host card creating a positioned ancestor.

    With `position: absolute` the overlay resolved against the nearest positioned ancestor —
    which the Meal planner and Market cards do not create (neither sets any `:host` rule) — so
    the sheet escaped its card and the planner's `ha-card { overflow: hidden }` clipped the
    panel away. The backdrop painted, the content did not: a grey screen with no popup.
    `position: fixed` needs no positioned ancestor and is not clipped by ancestor overflow.
    """
    source = _source(DETAIL_MODULE)
    wrap = re.search(r"\.detailwrap \{(.*?)\}", source, re.S)
    assert wrap, ".detailwrap rule not found"
    body = wrap.group(1)
    assert "position: fixed" in body, "the overlay must be fixed-positioned"
    assert "position: absolute" not in body


def test_no_consumer_clips_a_fixed_overlay() -> None:
    """A `transform`/`filter`/`perspective` on the CARD ITSELF would trap `position: fixed`.

    Those properties create a containing block for fixed descendants, which would reintroduce
    the clipping this fix removes. Hover states and inner elements are fine — only a rule on
    the host or the card root matters.
    """
    for name, path in CONSUMERS.items():
        source = _source(path)
        for selector in ("ha-card", ":host"):
            rule = re.search(rf"^\s*{re.escape(selector)} \{{(.*?)\}}", source, re.S | re.M)
            if not rule:
                continue
            body = rule.group(1)
            for prop in ("transform:", "filter:", "perspective:", "backdrop-filter:"):
                assert prop not in body, (
                    f"{name}: `{prop}` on {selector} creates a containing block that would "
                    "clip the fixed-positioned recipe sheet"
                )


def test_every_consumer_imports_the_shared_module_with_a_cache_bust() -> None:
    """A bare "./hellofresh-recipe-detail.js" would go stale across upgrades."""
    for name, path in CONSUMERS.items():
        source = _source(path)
        assert "hellofresh-recipe-detail.js" in source, f"{name} does not import the module"
        assert re.search(r"hellofresh-recipe-detail\.js\?v=\$\{encodeURIComponent\(", source), (
            f"{name} imports the shared module without a ?v= cache-bust"
        )


def test_no_consumer_keeps_its_own_copy_of_the_sheet() -> None:
    """The point of the extraction: one implementation, not three that can drift apart."""
    for name, path in CONSUMERS.items():
        source = _source(path)
        assert "_renderDetailBody" not in source, f"{name} still has its own detail renderer"
        assert ".detailwrap {" not in source, f"{name} still carries its own overlay CSS"


def test_every_consumer_closes_the_sheet_on_disconnect() -> None:
    """The overlay holds a document-level Escape handler; a detached card must drop it."""
    for name, path in CONSUMERS.items():
        source = _source(path)
        disconnect = re.search(r"  disconnectedCallback\(\) \{.*?\n  \}", source, re.S)
        assert disconnect, f"{name} has no disconnectedCallback"
        body = disconnect.group(0)
        assert "_detailOverlay.close()" in body or "_closeDetail()" in body, (
            f"{name} does not close the recipe sheet on disconnect"
        )


def test_planner_opens_the_sheet_from_every_tile() -> None:
    """The market card's model, mirrored: the tile tap opens the recipe on EVERY week —
    editable or not — because selection lives on the + Add pill and the ± steppers, which
    stop propagation before the tile handler runs."""
    source = _source(CONSUMERS["planner"])
    assert 'ev.target.closest(".recipe")' in source, "no universal tile → recipe handler"
    assert "_openRecipeDetail" in source
    # The old split model must be gone: no editable-tap-toggles path, no ⓘ fallback.
    assert "_toggleRecipe" not in source
    assert "infobtn" not in source


def test_planner_selects_with_the_add_pill() -> None:
    """Unselected editable tiles carry a + Add pill that selects at one serving; its handler
    must stop propagation so adding a meal doesn't also open the recipe sheet."""
    source = _source(CONSUMERS["planner"])
    assert 'class="addbtn"' in source
    assert "data-add=" in source
    add_handler = re.search(
        r'closest\("\.addbtn"\).*?ev\.stopPropagation\(\).*?_addRecipe\(week, recipe\)',
        source,
        re.S,
    )
    assert add_handler, "the + Add pill must stopPropagation and route to _addRecipe"
    # Removal goes through the stepper: reaching 0 servings deselects.
    assert "if (next === 0)" in source


def test_market_opens_the_sheet_from_a_tile() -> None:
    """Market quantities are changed with the ± steppers, so the tile itself is free."""
    source = _source(CONSUMERS["market"])
    assert "_openRecipeDetail" in source
    assert 'ev.target.closest(".item")' in source


def test_tiles_are_keyboard_accessible() -> None:
    """Every clickable-div surface must be reachable and activatable by keyboard: focusable
    with role=button, an Enter/Space keydown handler, and a guard so keystrokes on the real
    inner <button>s/<a>s (whose Enter/Space already fires natively) don't double-fire.
    Covers the recipe tiles (the ONLY way to open a recipe) in the planner, market and
    recipes cards, and the schedule card's week-select timeline rows."""
    surfaces = (
        ("planner", CONSUMERS["planner"], '.recipe"'),
        ("market", CONSUMERS["market"], '.item"'),
        ("recipes", WWW / "hellofresh-recipes-card.js", '[data-detail]"'),
        ("schedule", WWW / "hellofresh-schedule-card.js", "cal-week\"]'"),
    )
    for name, path, tile_selector in surfaces:
        source = _source(path)
        assert 'role="button" tabindex="0"' in source, f"{name} tiles are not focusable"
        keydown = re.search(r'addEventListener\("keydown".*?\}\);', source, re.S)
        assert keydown, f"{name} has no keydown handler"
        body = keydown.group(0)
        assert tile_selector in body, f"{name} keydown does not target its tile selector"
        assert re.search(r'ev\.target\.closest\("button', body), (
            f"{name} would double-fire inner buttons"
        )
        assert "ev.preventDefault()" in body, f"{name}: Space would scroll instead of open"


# ---- selection footer ("read it, then decide") ---------------------------------------------
#
# On editable planner weeks the sheet carries an Add/servings footer, fed by an optional
# `getSelection` hook so the shared module stays selection-agnostic: hosts that don't supply
# it (recipes, market) render exactly the read-only sheet they always did.

NODE = shutil.which("node")


def _footer(selection: dict | None, with_hook: bool = True) -> str:
    """Render the real _selectionBar under Node against a stubbed host."""
    hook = f"() => ({json.dumps(selection)})" if with_hook else "undefined"
    script = f"""
    import {{ RecipeDetailOverlay }} from {json.dumps(DETAIL_MODULE.as_uri())};
    const o = new RecipeDetailOverlay({{
      getRoot: () => null,
      callService: async () => ({{}}),
      getSelection: {hook},
    }});
    console.log(JSON.stringify(o._selectionBar()));
    """
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_sheet_footer_renders_only_when_the_host_supplies_selection_state() -> None:
    assert _footer(None, with_hook=False) == ""  # recipes/market: no hook, no footer
    assert _footer(None) == ""  # planner on a read-only week: hook returns null


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_sheet_footer_offers_add_then_a_servings_stepper() -> None:
    unselected = _footer({"qty": 0, "maxQty": 4})
    assert 'data-sel="add"' in unselected and "+ Add" in unselected
    assert 'data-sel="inc"' not in unselected

    one = _footer({"qty": 1, "maxQty": 4})
    assert 'data-sel="dec"' in one and 'data-sel="inc"' in one
    assert "Remove meal" in one  # − at one serving removes, and says so
    assert "disabled" not in one

    maxed = _footer({"qty": 4, "maxQty": 4})
    assert 'data-sel="inc" disabled' in maxed


def test_planner_feeds_the_sheet_its_selection_mutators() -> None:
    """The footer must drive the SAME pending-selection methods as the + Add pill and the
    tile steppers, so the grid underneath stays in step with the sheet."""
    source = _source(CONSUMERS["planner"])
    assert "getSelection: () => this._detailSelection()" in source
    sel = re.search(r"  _detailSelection\(\) \{.*?\n  \}", source, re.S)
    assert sel, "_detailSelection not found"
    body = sel.group(0)
    assert "_addRecipe(week, recipe)" in body
    assert "_changeQuantity(week, recipe, 1)" in body
    assert "_changeQuantity(week, recipe, -1)" in body
    assert "_canEdit(week)" in body, "a locked week must render the sheet read-only"


def test_market_items_expose_a_recipe_id() -> None:
    """Market add-ons carry a real recipe id, but `item_id` falls back to SKU/index.

    Handing `item_id` to the recipe-detail API would 404 for any add-on identified only by its
    SKU, so the model exposes the recipe id separately and the card reads that.
    """
    models = (
        Path(__file__).resolve().parents[1] / "custom_components" / "hellofresh" / "models.py"
    ).read_text(encoding="utf-8")
    market = re.search(r"class HelloFreshMarketItem:.*?def as_dict", models, re.S)
    assert market, "HelloFreshMarketItem not found"
    assert "recipe_id: str | None = None" in market.group(0)
    assert '"recipe_id": self.recipe_id,' in models

    source = _source(CONSUMERS["market"])
    assert "item.recipe_id" in source, "market card does not use the recipe id"
