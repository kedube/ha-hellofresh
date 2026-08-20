"""Behavioural tests for the meal-planner card's video lightbox dismissal.

The lightbox shipped unclosable, from two independent faults that a source-level read did not
make obvious:

1. The overlay is appended to the shadow root as a SIBLING of ``<ha-card>``, but dismissal was
   handled by the card's delegated click listener bound to ``<ha-card>``. Clicks on the overlay
   never reached it.
2. ``.videobox`` stopped propagation for every click inside it -- and the ✕ button lives inside
   ``.videobox``, so even a correctly-placed handler would not have seen it.

A third fault appeared in the first attempt at the fix: marking the overlay itself with a
``data-video-close`` attribute and matching via ``closest()``. ``closest()`` walks *up* from the
event target, so the marker matched every click inside the overlay -- clicking the video's own
controls would have dismissed it. The backdrop is therefore matched by identity instead.

These tests run the real listener body from the card source against a minimal DOM stub, so they
assert behaviour ("does clicking ✕ close it?") rather than the presence of particular source
text. Skipped when Node is unavailable.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

WWW = Path(__file__).resolve().parents[1] / "custom_components" / "hellofresh" / "www"
MEAL_PLANNER = WWW / "hellofresh-meal-planner-card.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

# Overlay structure as built by _openVideo, as (name, class, parent).
DOM = [
    ("overlay", "videowrap", None),
    ("box", "videobox", "overlay"),
    ("head", "videohead", "box"),
    ("closeBtn", "videoclose", "head"),
    ("title", "videotitle", "head"),
    ("video", "videoel", "box"),
    ("link", "videofallback", "box"),
]


def _overlay_listener_body() -> str:
    """Extract the real click handler registered on the overlay in ``_openVideo``."""
    source = MEAL_PLANNER.read_text(encoding="utf-8")
    match = re.search(
        r'overlay\.addEventListener\("click", \(ev\) => \{(.*?)\n    \}\);', source, re.S
    )
    assert match, "overlay click listener not found in _openVideo"
    return match.group(1)


def _clicks_that_close() -> dict[str, bool]:
    """Fire a click at each overlay node and report whether _closeVideo ran."""
    script = f"""
    function mkEl(cls) {{
      const el = {{ className: cls, parent: null }};
      el.closest = (sel) => {{
        const want = sel.slice(1);
        for (let n = el; n; n = n.parent) {{
          if (String(n.className).split(/\\s+/).includes(want)) return n;
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
    const overlay = nodes.overlay;
    let closed = 0;
    const self = {{ _closeVideo: () => {{ closed++; }} }};
    const handler = (ev) => {{ {_overlay_listener_body().replace("this.", "self.")} }};
    const out = {{}};
    for (const name of Object.keys(nodes)) {{
      closed = 0;
      handler({{ target: nodes[name] }});
      out[name] = closed > 0;
    }}
    console.log(JSON.stringify(out));
    """
    result = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(result.stdout)


def test_close_button_dismisses_the_lightbox() -> None:
    """The reported bug: tapping ✕ did nothing."""
    assert _clicks_that_close()["closeBtn"] is True


def test_backdrop_click_dismisses_the_lightbox() -> None:
    """Clicking outside the player is the other expected way to dismiss it."""
    assert _clicks_that_close()["overlay"] is True


@pytest.mark.parametrize("node", ["video", "box", "head", "title", "link"])
def test_clicks_inside_the_player_do_not_dismiss(node: str) -> None:
    """Using the player must not close it.

    Guards the over-correction: a dismiss marker on the overlay makes ``closest()`` match from
    any descendant, so scrubbing the video's timeline would close the lightbox.
    """
    assert _clicks_that_close()[node] is False


def test_overlay_binds_its_own_listener() -> None:
    """Structural backstop for fault 1.

    The behavioural tests above run the handler directly, so they would still pass if it were
    never attached. The overlay lives outside ``<ha-card>``, so it must bind its own listener.
    """
    source = MEAL_PLANNER.read_text(encoding="utf-8")
    open_video = re.search(r"^  _openVideo\(recipe\) \{.*?^  \}", source, re.S | re.M)
    assert open_video, "_openVideo not found"
    assert 'overlay.addEventListener("click"' in open_video.group(0)


def _tile_lookup_outcomes() -> list[dict]:
    """Run the card's real tile-key logic over a delivered week's meals.

    Past-delivery meals carry NO `index`, so every recipe in such a week has
    `course_index === null`. This exercises the key the tiles are rendered with and the lookup
    that resolves a tap back to a recipe.
    """
    source = MEAL_PLANNER.read_text(encoding="utf-8")
    sel_key = re.search(r"^  _selKey\(recipe\) \{.*?^  \}", source, re.S | re.M)
    assert sel_key, "_selKey not found"
    finder = re.search(r"^  _findRenderedRecipe\(key\) \{.*?^  \}", source, re.S | re.M)
    assert finder, "_findRenderedRecipe not found"

    script = f"""
    class Card {{
      constructor(list) {{ this._renderedRecipes = list; this._weeks = null; this._cursor = 0; }}
      {sel_key.group(0).strip()}
      {finder.group(0).strip()}
    }}
    // Real 2026-W21 meals from a live capture: three distinct dishes, no course indexes.
    const week = [
      {{ name: "Grass-Fed Rib-Eye", course_index: null, recipe_id: "69a5e5b5b83c0c3e7ed548aa", video_url: "f0700e02" }},
      {{ name: "Korean-Style Wing Feast", course_index: null, recipe_id: "68d655cd681e58ed6e5b3544", video_url: "fa84c70a" }},
      {{ name: "Cobb Salad", course_index: null, recipe_id: "68e5117a908d7cf716b7971b", video_url: null }},
    ];
    const card = new Card(week);
    const out = week.map((r) => {{
      const hit = card._findRenderedRecipe(String(card._selKey(r)));
      return {{ tapped: r.name, resolved: hit ? hit.name : null, video: hit ? hit.video_url : null }};
    }});
    console.log(JSON.stringify(out));
    """
    result = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(result.stdout)


def test_each_tile_resolves_to_its_own_meal_on_a_delivered_week() -> None:
    """The reported bug: tapping one meal played another's video and opened its recipe.

    Past-delivery meals have no `index`, so keying tiles on `course_index` alone gave every
    tile in a delivered week the same key (`"null"`) — and the lookup returned the FIRST match,
    i.e. the week's first meal. Tapping the Wing Feast opened the Rib-Eye. The tile key falls
    back to the recipe id, which is always present and unique.
    """
    for outcome in _tile_lookup_outcomes():
        assert outcome["resolved"] == outcome["tapped"], (
            f"tapping {outcome['tapped']!r} resolved to {outcome['resolved']!r}"
        )


def test_each_tile_keeps_its_own_video_on_a_delivered_week() -> None:
    """A meal with no clip must not inherit a sibling's, and vice versa."""
    outcomes = {o["tapped"]: o["video"] for o in _tile_lookup_outcomes()}
    assert outcomes["Grass-Fed Rib-Eye"] == "f0700e02"
    assert outcomes["Korean-Style Wing Feast"] == "fa84c70a"
    # This one genuinely has no video; resolving to a sibling would fabricate one.
    assert outcomes["Cobb Salad"] is None


def test_tile_keys_are_not_derived_from_course_index_alone() -> None:
    """Structural backstop for every place the tile key is emitted or read."""
    source = MEAL_PLANNER.read_text(encoding="utf-8")
    assert "String(recipe.course_index)" not in source
    assert "this._esc(String(r.course_index))" not in source
    assert "list.find((r) => String(r.course_index) === index)" not in source


def test_video_source_is_declared_as_mp4() -> None:
    """HelloFresh's `.mov` URLs are served as `video/mp4` and DO play.

    An earlier revision assumed `.mov` was unplayable in Chrome/Firefox and left the browser to
    guess from the suffix, which is why some clips appeared broken. Declaring the type
    explicitly is what makes them play.
    """
    source = MEAL_PLANNER.read_text(encoding="utf-8")
    open_video = re.search(r"^  _openVideo\(recipe\) \{.*?^  \}", source, re.S | re.M)
    assert open_video, "_openVideo not found"
    assert "<source src=" in open_video.group(0)
    assert 'type="video/mp4"' in open_video.group(0)


def test_video_failure_is_surfaced_not_silent() -> None:
    """A clip that genuinely cannot play must say so, not show a black box."""
    source = MEAL_PLANNER.read_text(encoding="utf-8")
    open_video = re.search(r"^  _openVideo\(recipe\) \{.*?^  \}", source, re.S | re.M)
    body = open_video.group(0)
    assert 'class="videoerr"' in body
    # <source> errors do not bubble to <video>, so BOTH need a listener.
    assert 'videoEl.addEventListener("error"' in body
    assert 'sourceEl.addEventListener("error"' in body


def test_sold_out_ribbon_is_suppressed_on_past_weeks() -> None:
    """A delivered week must not render a "Sold out" ribbon."""
    source = MEAL_PLANNER.read_text(encoding="utf-8")
    assert "showSoldOut: !this._isPast(week)" in source
    assert "ctx.showSoldOut !== false" in source


def test_no_blanket_stop_propagation_inside_the_player() -> None:
    """Guards fault 2: swallowing every click in .videobox also swallowed the ✕."""
    source = MEAL_PLANNER.read_text(encoding="utf-8")
    open_video = re.search(r"^  _openVideo\(recipe\) \{.*?^  \}", source, re.S | re.M)
    assert not re.search(r"videobox[\s\S]{0,160}stopPropagation", open_video.group(0))
