"""Behavioural tests for the recipes card's detail overlay and cookbook view.

Covers three shipped bugs:

1. The detail sheet showed no photo. That one is in the Python model (see
   ``test_recipe_detail_image_uses_the_working_host_not_the_payloads_own_link``); what is
   guarded here is that the card actually renders an <img> from ``image_url``.
2. The detail sheet's ✕ did not close it — the same pair of faults as the video lightbox: the
   overlay lives outside ``<ha-card>`` (so the delegated listener never saw it) and a
   propagation guard on ``.detailbox`` swallowed clicks on the ✕ inside it. A marker attribute
   on the overlay is equally wrong, since ``closest()`` walks up and matches every inner click.
3. The cookbook (favorites) list had no way to be shown at all: the card could add and remove
   favorites but never listed them.

The overlay tests run the real listener body against a DOM stub. Skipped without Node.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

WWW = Path(__file__).resolve().parents[1] / "custom_components" / "hellofresh" / "www"
RECIPES_CARD = WWW / "hellofresh-recipes-card.js"
SOURCE = RECIPES_CARD.read_text(encoding="utf-8")
# The detail sheet now lives in a module shared by the Recipes, Meal planner and Market cards,
# so the overlay guards below read it there rather than from any one card.
DETAIL_MODULE = WWW / "hellofresh-recipe-detail.js"
DETAIL_SOURCE = DETAIL_MODULE.read_text(encoding="utf-8")
NODE = shutil.which("node")

# Detail overlay structure as built by _renderDetail/_renderDetailBody: (name, class, parent).
DOM = [
    ("overlay", "detailwrap", None),
    ("box", "detailbox", "overlay"),
    ("head", "detailhead", "box"),
    ("closeBtn", "detailclose", "head"),
    ("title", "detailtitle", "head"),
    ("scroll", "detailscroll", "box"),
    ("img", "detailimg", "scroll"),
    ("steps", "steps", "scroll"),
]


def _overlay_listener_body() -> str:
    match = re.search(
        r'overlay\.addEventListener\("click", \(ev\) => \{(.*?)\n      \}\);',
        DETAIL_SOURCE,
        re.S,
    )
    assert match, "detail overlay click listener not found"
    # The module calls `this.open(...)` / `this.close()`; the stub below supplies both.
    return match.group(1)


def _click_outcomes() -> dict[str, dict]:
    """Fire a click at each overlay node; report whether it closed / changed servings."""
    script = f"""
    function mkEl(cls, attrs) {{
      const el = {{ className: cls, attrs: attrs || {{}}, parent: null }};
      el.getAttribute = (k) => el.attrs[k];
      el.closest = (sel) => {{
        const isAttr = sel.startsWith("[");
        const want = isAttr ? sel.slice(1, -1) : sel.slice(1);
        for (let n = el; n; n = n.parent) {{
          const hit = isAttr
            ? Object.prototype.hasOwnProperty.call(n.attrs, want)
            : String(n.className).split(/\\s+/).includes(want);
          if (hit) return n;
        }}
        return null;
      }};
      return el;
    }}
    const spec = {json.dumps(DOM)};
    const nodes = {{}};
    for (const [name, cls, parent] of spec) {{
      nodes[name] = mkEl(cls);
      if (parent) nodes[name].parent = nodes[parent];
    }}
    nodes.sbtn = mkEl("sbtn", {{ "data-servings": "4" }});
    nodes.sbtn.parent = nodes.scroll;

    const overlay = nodes.overlay;
    let closed = 0, servings = null;
    const self = {{
      _id: "r1",
      close: () => {{ closed++; }},
      open: (id, s) => {{ servings = s; }},
    }};
    const handler = (ev) => {{ {_overlay_listener_body().replace("this.", "self.")} }};
    const out = {{}};
    for (const name of Object.keys(nodes)) {{
      closed = 0; servings = null;
      handler({{ target: nodes[name] }});
      out[name] = {{ closed: closed > 0, servings }};
    }}
    console.log(JSON.stringify(out));
    """
    result = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(result.stdout)


nodejs = pytest.mark.skipif(NODE is None, reason="node is not installed")


@nodejs
def test_detail_close_button_dismisses_the_sheet() -> None:
    """The reported bug: tapping ✕ on a recipe did nothing."""
    assert _click_outcomes()["closeBtn"]["closed"] is True


@nodejs
def test_detail_backdrop_dismisses_the_sheet() -> None:
    assert _click_outcomes()["overlay"]["closed"] is True


@nodejs
@pytest.mark.parametrize("node", ["img", "title", "steps", "box", "head", "scroll"])
def test_clicks_inside_the_detail_sheet_do_not_dismiss(node: str) -> None:
    """Reading the recipe must not close it — the over-correction guard."""
    assert _click_outcomes()[node]["closed"] is False


@nodejs
def test_servings_switcher_still_works_and_does_not_dismiss() -> None:
    """The servings buttons live inside the sheet; they must refetch, not close."""
    outcome = _click_outcomes()["sbtn"]
    assert outcome["servings"] == 4
    assert outcome["closed"] is False


def test_detail_overlay_binds_its_own_listener() -> None:
    """Structural backstop: the overlay is outside <ha-card>, so it needs its own listener."""
    render = re.search(r"^  _render\(\) \{.*?^  \}", DETAIL_SOURCE, re.S | re.M)
    assert render, "_render not found in the detail module"
    assert 'overlay.addEventListener("click"' in render.group(0)


def test_no_blanket_stop_propagation_in_the_detail_sheet() -> None:
    """Swallowing every click in .detailbox also swallowed the ✕ inside it."""
    assert not re.search(r"detailbox[\s\S]{0,160}stopPropagation", DETAIL_SOURCE)


def test_detail_sheet_renders_the_recipe_image() -> None:
    """Bug 1's card half: the sheet must actually emit an <img> from image_url."""
    body = re.search(r"^  _body\(\) \{.*?^  \}", DETAIL_SOURCE, re.S | re.M)
    assert body, "_body not found in the detail module"
    assert "resizedImage(r.image_url" in body.group(0)
    assert 'class="detailimg"' in body.group(0)


def test_cookbook_is_reachable_from_the_card() -> None:
    """Bug 3: favorites could be added and removed but never listed.

    Asserts the chip is actually EMITTED, not merely that the slug is mentioned somewhere in
    the function: a first version of this test only checked for the constant, and still passed
    when the chip was deleted from the returned markup, because the slug also appears in the
    neighbouring "is this chip active?" comparison.
    """
    collections = re.search(r"^  _renderCollections\(\) \{.*?^  \}", SOURCE, re.S | re.M)
    assert collections, "_renderCollections not found"
    body = collections.group(0)

    chip_markup = re.search(
        r"const cookbook = `(.*?)`;", body, re.S
    )
    assert chip_markup, "no cookbook chip element is built"
    assert "data-collection" in chip_markup.group(1), "cookbook chip is not clickable"

    # ...and that the built chip is interpolated into the returned chips bar.
    returned = body[body.index("return `"):]
    assert "${cookbook}" in returned, "cookbook chip is built but never rendered"


def test_every_collection_is_rendered_as_a_chip() -> None:
    """All ~60 categories must render, not a slice.

    HelloFresh publishes around sixty categories and the chips are the card's only route into
    one, so a `.slice(0, n)` here would quietly hide whole cuisines (Indian, Korean, Thai …)
    while the bar still looked populated.
    """
    collections = re.search(r"^  _renderCollections\(\) \{.*?^  \}", SOURCE, re.S | re.M)
    assert collections, "_renderCollections not found"
    body = collections.group(0)

    assert "this._collections.map(" in body, "collections are not mapped to chips"
    assert ".slice(" not in body, "the chip list is truncated"


def test_collections_are_fetched_without_a_cap() -> None:
    """`get_recipe_collections` takes no limit; the card must not invent one."""
    fetch = re.search(r"^  async _fetch\(\) \{.*?^  \}", SOURCE, re.S | re.M)
    assert fetch, "_fetch not found"
    call = re.search(r'_call\("get_recipe_collections"(.*?)\)', fetch.group(0), re.S)
    assert call, "get_recipe_collections is not called"
    assert call.group(1).strip() in ("", ","), "collections are fetched with unexpected arguments"


def test_cookbook_view_calls_the_favorites_service() -> None:
    """The cookbook comes from get_favorites, not the browse catalog endpoint."""
    fetch = re.search(r"^  async _fetch\(\) \{.*?^  \}", SOURCE, re.S | re.M)
    assert fetch, "_fetch not found"
    assert 'this._call("get_favorites")' in fetch.group(0)
    assert "COOKBOOK_SLUG" in fetch.group(0)


def test_unfavoriting_in_the_cookbook_view_removes_the_row() -> None:
    """The cookbook list IS the favorites, so an un-favorited row must not linger in it."""
    toggle = re.search(r"^  async _toggleFavorite\(recipe\) \{.*?^  \}", SOURCE, re.S | re.M)
    assert toggle, "_toggleFavorite not found"
    assert "COOKBOOK_SLUG" in toggle.group(0)
    assert "filter" in toggle.group(0)
