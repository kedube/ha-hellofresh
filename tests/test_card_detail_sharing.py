"""Guards on the recipe-detail sheet (``hellofresh-recipe-detail.js``) and how the card uses it.

The sheet is its own module, loaded by the HelloFresh card when a recipe is opened. Three things
are easy to get wrong with a module like this, and each is checked here:

* **Shipping.** The module is a dependency, not a Lovelace resource, so it is deliberately NOT
  registered as a card (see tests/test_frontend.py). Nothing else would notice it missing.
* **Cache-busting.** Lovelace stamps ``?v=`` only onto the card URLs it registers. A static
  ``./x.js`` import resolves to the bare filename, so a browser could keep serving an old copy
  of the sheet after an upgrade while the card itself refreshed. The card therefore imports it
  dynamically with its own version appended.
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

CARD = WWW / "hellofresh-card.js"
CARD_STYLES = WWW / "hellofresh-card-styles.js"
# The card and the view/logic modules it imports.
CARD_MODULES = sorted(WWW.glob("hellofresh-card*.js"))


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_shared_module_exists_and_exports_what_consumers_use() -> None:
    source = _source(DETAIL_MODULE)
    assert "export class RecipeDetailOverlay" in source
    assert "export const DETAIL_STYLES" in source


def test_overlay_is_fixed_positioned_not_absolute() -> None:
    """The sheet must not depend on its host card creating a positioned ancestor.

    With `position: absolute` the overlay resolved against the nearest positioned ancestor —
    which the classic Meal planner and Market cards did not create (neither set any `:host`
    rule) — so the sheet escaped its card and the planner's `ha-card { overflow: hidden }`
    clipped the panel away. The backdrop painted, the content did not: a grey screen with no popup.
    `position: fixed` needs no positioned ancestor and is not clipped by ancestor overflow.
    """
    source = _source(DETAIL_MODULE)
    wrap = re.search(r"\.detailwrap \{(.*?)\}", source, re.S)
    assert wrap, ".detailwrap rule not found"
    body = wrap.group(1)
    assert "position: fixed" in body, "the overlay must be fixed-positioned"
    assert "position: absolute" not in body


def test_the_card_does_not_clip_a_fixed_overlay() -> None:
    """A `transform`/`filter`/`perspective` on the CARD ITSELF would trap `position: fixed`.

    Those properties create a containing block for fixed descendants, which would reintroduce
    the clipping this fix removes. Hover states and inner elements are fine — only a rule on
    the host or the card root matters.
    """
    source = _source(CARD_STYLES)
    for selector in ("ha-card", ":host"):
        rule = re.search(rf"^\s*{re.escape(selector)} \{{(.*?)\}}", source, re.S | re.M)
        assert rule, f"no {selector} rule in the card's styles"
        body = rule.group(1)
        for prop in ("transform:", "filter:", "perspective:", "backdrop-filter:"):
            assert prop not in body, (
                f"`{prop}` on {selector} creates a containing block that would clip the "
                "fixed-positioned recipe sheet"
            )


def test_the_card_imports_the_sheet_with_a_cache_bust() -> None:
    """A bare "./hellofresh-recipe-detail.js" would go stale across upgrades."""
    source = _source(CARD)
    assert 'import(moduleUrl("hellofresh-recipe-detail.js"))' in source
    assert "new URL(`./${name}?v=${encodeURIComponent(CARD_VERSION)}`" in source, (
        "moduleUrl no longer appends the card's ?v= cache-bust"
    )


def test_no_card_module_keeps_its_own_copy_of_the_sheet() -> None:
    """One implementation of the sheet, not copies that can drift apart. (The card may theme
    the sheet, e.g. its text colour, but not lay out an overlay of its own.)"""
    for path in CARD_MODULES:
        source = _source(path)
        assert "_renderDetailBody" not in source, f"{path.name} has its own detail renderer"
        assert not re.search(r"\.detailwrap \{[^}]*position:", source), (
            f"{path.name} carries its own overlay CSS"
        )


def test_the_card_closes_the_sheet_on_disconnect() -> None:
    """The overlay holds a document-level Escape handler; a detached card must drop it."""
    disconnect = re.search(r"  disconnectedCallback\(\) \{.*?\n  \}", _source(CARD), re.S)
    assert disconnect, "the card has no disconnectedCallback"
    assert "this._detail.close()" in disconnect.group(0), (
        "the card does not close the recipe sheet on disconnect"
    )


# ---- selection footer ("read it, then decide") ---------------------------------------------
#
# On an editable week the sheet carries an Add/servings footer, fed by an optional
# `getSelection` hook so the module stays selection-agnostic: with no hook, or when the hook
# returns nothing (Recipes, Market, a locked week), it renders the read-only sheet.

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
    assert _footer(None, with_hook=False) == ""  # no hook, no footer
    assert _footer(None) == ""  # a read-only week: the hook returns null


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


# ---- step photos and timers -------------------------------------------------------------------


def _sheet(detail: dict) -> str:
    """Render the real sheet body under Node for one recipe detail payload."""
    script = f"""
    import {{ RecipeDetailOverlay }} from {json.dumps(DETAIL_MODULE.as_uri())};
    const o = new RecipeDetailOverlay({{ getRoot: () => null, callService: async () => ({{}}) }});
    o._id = "r1";
    o._detail = {json.dumps(detail)};
    console.log(JSON.stringify(o._body()));
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
def test_sheet_shows_step_photos_captions_timers_and_bold_text() -> None:
    html = _sheet(
        {
            "name": "Hand Pies",
            "steps": [
                {
                    "index": 1,
                    "instructions": "Make the dough.",
                    "paragraphs": [
                        [{"text": "Make the ", "bold": False}, {"text": "<dough>", "bold": True}],
                        [{"text": "Rest it.", "bold": False}],
                    ],
                    "image_url": (
                        "https://img.hellofresh.com/f_auto,fl_lossy,q_auto,w_640"
                        "/hellofresh_s3/r1/step-1.jpeg"
                    ),
                    "caption": "Bake & Rest",
                    "timers": [
                        # Named after the step, as HelloFresh does: the name would repeat the
                        # caption beside it, so only the time shows.
                        {"name": "bake & rest", "seconds": 900},
                        {"name": None, "seconds": 90},
                        {"name": "Cool", "seconds": 30},
                    ],
                },
                # No rich runs: the plain text renders. An unsafe URL never reaches an <img>.
                {
                    "index": 2,
                    "instructions": "Serve.\nEnjoy <3",
                    "paragraphs": [],
                    "image_url": "javascript:alert(1)",
                },
            ],
        }
    )
    # Thumbnails request the width they are shown at, not the hero photo's 640.
    assert "w_360/hellofresh_s3/r1/step-1.jpeg" in html
    assert "w_640" not in html
    assert 'loading="lazy"' in html
    assert "<figcaption>Bake &amp; Rest</figcaption>" in html
    # Bold runs become <strong>, and their text is escaped like any other string.
    assert '<p class="steppara">Make the <strong>&lt;dough&gt;</strong></p>' in html
    assert "Serve.<br>Enjoy &lt;3" in html
    assert "⏱ 15 min<" in html
    assert "bake &amp; rest ·" not in html
    assert "⏱ 1 min 30 sec" in html
    assert "⏱ Cool · 30 sec" in html
    assert html.count("<figure") == 1
    assert "javascript:" not in html
    assert html.count('class="steptimers"') == 1
    # Text before photo: the list number aligns to the first flex item's baseline, which for
    # a photo is its bottom edge.
    assert html.index('class="stepbody"') < html.index("<figure")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_sheet_shows_labels_description_ingredients_nutrition_and_video() -> None:
    html = _sheet(
        {
            "name": "Meatballs",
            "labels": ["Protein Smart"],
            "description": "Cozy <and> quick.",
            "ingredients": [
                {
                    "name": "Ciabatta",
                    "amount": 1,
                    "unit": "unit",
                    "image_url": (
                        "https://img.hellofresh.com/f_auto,fl_lossy,q_auto,w_640"
                        "/hellofresh_s3/ingredient/ciabatta.png"
                    ),
                    "shipped": True,
                    "allergens": ["Wheat", "Soy"],
                },
                {"name": "Salt", "amount": None, "shipped": False, "allergens": []},
            ],
            "nutrition": {"Calories": "1000kcal", "Protein": "52g", "Sodium": ""},
            "video_url": "https://media.hellofresh.com/video.mp4",
            "card_url": "https://www.hellofresh.com/recipecards/card/x.pdf",
        }
    )
    assert '<span class="detaillabel">Protein Smart</span>' in html
    assert '<p class="detaildesc">Cozy &lt;and&gt; quick.</p>' in html
    # Ingredient cut-outs are requested at thumbnail size and are decorative (the name is
    # right beside them); a row without a photo keeps an empty slot so names stay aligned.
    assert "w_96/hellofresh_s3/ingredient/ciabatta.png" in html
    assert 'alt=""' in html
    assert '<span class="ingimg"></span>' in html
    assert "Contains Wheat, Soy" in html
    assert "(not in box)" in html
    assert "<dt>Calories</dt><dd>1000kcal</dd>" in html
    assert "<dt>Protein</dt><dd>52g</dd>" in html
    assert "Sodium" not in html  # an empty value is not a row
    assert "per serving" in html
    assert "Watch the recipe video" in html
    assert "Printable recipe card (PDF)" in html


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_sheet_omits_sections_the_recipe_does_not_have() -> None:
    html = _sheet({"name": "Bare", "video_url": "javascript:alert(1)"})
    for absent in ("detaillabels", "detaildesc", "Nutrition", "Watch the recipe video", "<figure"):
        assert absent not in html


def test_market_items_expose_a_recipe_id() -> None:
    """Market add-ons carry a real recipe id, but `item_id` falls back to SKU/index.

    Handing `item_id` to the recipe-detail API would 404 for any add-on identified only by its
    SKU, so the model exposes the recipe id separately and the card's Market reads that.
    """
    models = (
        Path(__file__).resolve().parents[1] / "custom_components" / "hellofresh" / "models.py"
    ).read_text(encoding="utf-8")
    market = re.search(r"class HelloFreshMarketItem:.*?def as_dict", models, re.S)
    assert market, "HelloFreshMarketItem not found"
    assert "recipe_id: str | None = None" in market.group(0)
    assert '"recipe_id": self.recipe_id,' in models

    source = _source(WWW / "hellofresh-card-menu.js")
    assert "card.openRecipe(item.recipe_id" in source, "the Market does not use the recipe id"
