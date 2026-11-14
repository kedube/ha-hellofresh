"""Behavioural tests for the recipe sheet's overlay (``hellofresh-recipe-detail.js``).

Covers three shipped bugs:

1. The sheet showed no photo. That one is in the Python model (see
   ``test_recipe_detail_image_uses_the_working_host_not_the_payloads_own_link``); what is
   guarded here is that the sheet actually renders an <img> from ``image_url``.
2. The sheet's ✕ did not close it: the overlay lives outside ``<ha-card>`` (so the card's
   delegated listener never saw it) and a propagation guard on ``.detailbox`` swallowed clicks
   on the ✕ inside it. A marker attribute on the overlay is equally wrong, since ``closest()``
   walks up and matches every inner click.
3. A tap on Add / + / − redrew the whole sheet, which sent a reader at step 6 back to the top
   and dropped keyboard focus. Those taps redraw the footer alone; the sheet's dialog is built
   once per open.

The overlay tests run the real listener body against a DOM stub. Skipped without Node. What
needs a real DOM (focus going into the sheet and back, the scroll position kept, Tab staying
inside) is in the browser flows.
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
    ("footer", "selfooter", "box"),
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
    nodes.addBtn = mkEl("sadd", {{ "data-sel": "add" }});
    nodes.addBtn.parent = nodes.footer;

    const overlay = nodes.overlay;
    let closed = 0, servings = null, added = 0, footers = [], redraws = 0;
    const self = {{
      _id: "r1",
      close: () => {{ closed++; }},
      open: (id, s) => {{ servings = s; }},
      _getSelection: () => ({{ add: () => {{ added++; }}, inc() {{}}, dec() {{}} }}),
      _renderFooter: (pressed) => {{ footers.push(pressed); }},
      _render: () => {{ redraws++; }},
    }};
    const handler = (ev) => {{ {_overlay_listener_body().replace("this.", "self.")} }};
    const out = {{}};
    for (const name of Object.keys(nodes)) {{
      closed = 0; servings = null; added = 0; footers = []; redraws = 0;
      handler({{ target: nodes[name] }});
      out[name] = {{ closed: closed > 0, servings, added, footers, redraws }};
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


@nodejs
def test_a_tap_on_add_redraws_the_footer_and_nothing_else() -> None:
    """Add / + / − change the box and redraw the footer; the recipe above keeps its scroll
    position, its loaded photos and the reader's place."""
    outcome = _click_outcomes()["addBtn"]
    assert outcome["added"] == 1
    assert outcome["footers"] == ["add"]
    assert outcome["redraws"] == 0
    assert outcome["closed"] is False


def test_the_sheet_is_a_modal_dialog_built_once() -> None:
    """Its dialog carries the dialog role and is created where the overlay is, once per open —
    not rewritten with each redraw, which is what dropped focus and scroll."""
    render = re.search(r"^  _render\(\) \{.*?^  \}", DETAIL_SOURCE, re.S | re.M)
    assert render, "_render not found in the detail module"
    build = re.search(r"if \(!this\._overlay\) \{(.*?)\n    \}\n", render.group(0), re.S)
    assert build, "the once-per-open block was not found"
    for needle in ('"role", "dialog"', '"aria-modal", "true"', "box.focus("):
        assert needle in build.group(1), needle
    # Each redraw names the dialog after the recipe and fills it; it does not rebuild it.
    after = render.group(0)[build.end() :]
    assert 'setAttribute("aria-label"' in after
    assert "box.innerHTML = this._body()" in after
    assert "createElement" not in after
    # The close button has a name, and its glyph is not read out as one.
    assert 'aria-label="${ht("recipe.close")}"><span aria-hidden="true">✕</span>' in DETAIL_SOURCE


@nodejs
def test_changing_servings_keeps_the_recipe_on_screen() -> None:
    """Another serving size for the SAME recipe keeps what is on screen (dimmed, the size just
    picked marked) until the new amounts arrive; a failed re-read keeps it too, with the error
    above it. Only another recipe starts from the loading message."""
    script = f"""
    import {{ RecipeDetailOverlay }} from {json.dumps(DETAIL_MODULE.as_uri())};
    let answer;
    const o = new RecipeDetailOverlay({{
      getRoot: () => null,
      callService: () => new Promise((resolve, reject) => (answer = {{ resolve, reject }})),
    }});
    const recipe = (servings) => ({{ recipe: {{ name: "Tacos", servings, available_yields: [2, 4] }} }});
    const look = () => {{
      const body = o._body();
      return {{
        name: o._detail && o._detail.name,
        dimmed: body.includes('class="detailscroll busy"'),
        marked: (/class="sbtn active"[^>]*data-servings="(\\d)"/s.exec(body) || [])[1] || null,
        loadingOnly: body.includes("Loading recipe"),
        error: body.includes("offline"),
      }};
    }};
    let opening = o.open("r1");
    const first = look();
    answer.resolve(recipe(2));
    await opening;
    const shown = look();
    opening = o.open("r1", 4);
    const switching = look();
    answer.resolve(recipe(4));
    await opening;
    const switched = look();
    opening = o.open("r1", 2);
    answer.reject(new Error("offline"));
    await opening;
    const failed = look();
    o.open("r2");
    const other = look();
    console.log(JSON.stringify({{ first, shown, switching, switched, failed, other }}));
    """
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    out = json.loads(result.stdout)
    assert out["first"]["loadingOnly"] and out["first"]["name"] is None
    assert out["shown"] == {
        "name": "Tacos",
        "dimmed": False,
        "marked": "2",
        "loadingOnly": False,
        "error": False,
    }
    # Still the recipe, dimmed, with the size just picked marked — not the loading message.
    assert out["switching"] == {**out["shown"], "dimmed": True, "marked": "4"}
    assert out["switched"] == {**out["shown"], "marked": "4"}
    # The re-read failed: the amounts on screen stay, with the error above them.
    assert out["failed"] == {**out["shown"], "marked": "4", "error": True}
    assert out["other"]["loadingOnly"] and out["other"]["name"] is None


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
