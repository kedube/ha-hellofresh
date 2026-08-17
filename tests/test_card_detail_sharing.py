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

from pathlib import Path
import re

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


def test_every_consumer_imports_the_shared_module_with_a_cache_bust() -> None:
    """A bare "./hellofresh-recipe-detail.js" would go stale across upgrades."""
    for name, path in CONSUMERS.items():
        source = _source(path)
        assert "hellofresh-recipe-detail.js" in source, f"{name} does not import the module"
        assert re.search(
            r"hellofresh-recipe-detail\.js\?v=\$\{encodeURIComponent\(", source
        ), f"{name} imports the shared module without a ?v= cache-bust"


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
        assert (
            "_detailOverlay.close()" in body or "_closeDetail()" in body
        ), f"{name} does not close the recipe sheet on disconnect"


def test_planner_opens_the_sheet_on_a_read_only_tile() -> None:
    """The requested behaviour: a week you can't edit opens the recipe instead of doing nothing.

    On an editable week the tap still toggles the selection — changing the meal is the point
    there — so that path keeps its own handler and the ⓘ button covers the read-only case.
    """
    source = _source(CONSUMERS["planner"])
    assert '.recipe:not(.editable)' in source, "no read-only tile handler"
    assert "_openRecipeDetail" in source
    # The editable path must still toggle rather than open.
    assert "_toggleRecipe(week, recipe)" in source


def test_planner_offers_an_info_button_on_editable_weeks() -> None:
    """An editable tile's tap is taken by selection, so the recipe needs its own affordance."""
    source = _source(CONSUMERS["planner"])
    assert 'class="infobtn"' in source
    assert 'data-info=' in source


def test_market_opens_the_sheet_from_a_tile() -> None:
    """Market quantities are changed with the ± steppers, so the tile itself is free."""
    source = _source(CONSUMERS["market"])
    assert "_openRecipeDetail" in source
    assert 'ev.target.closest(".item")' in source


def test_market_items_expose_a_recipe_id() -> None:
    """Market add-ons carry a real recipe id, but `item_id` falls back to SKU/index.

    Handing `item_id` to the recipe-detail API would 404 for any add-on identified only by its
    SKU, so the model exposes the recipe id separately and the card reads that.
    """
    models = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "hellofresh"
        / "models.py"
    ).read_text(encoding="utf-8")
    market = re.search(r"class HelloFreshMarketItem:.*?def as_dict", models, re.S)
    assert market, "HelloFreshMarketItem not found"
    assert "recipe_id: str | None = None" in market.group(0)
    assert '"recipe_id": self.recipe_id,' in models

    source = _source(CONSUMERS["market"])
    assert "item.recipe_id" in source, "market card does not use the recipe id"
