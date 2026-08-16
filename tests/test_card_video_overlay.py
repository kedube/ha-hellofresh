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


def test_no_blanket_stop_propagation_inside_the_player() -> None:
    """Guards fault 2: swallowing every click in .videobox also swallowed the ✕."""
    source = MEAL_PLANNER.read_text(encoding="utf-8")
    open_video = re.search(r"^  _openVideo\(recipe\) \{.*?^  \}", source, re.S | re.M)
    assert not re.search(r"videobox[\s\S]{0,160}stopPropagation", open_video.group(0))
