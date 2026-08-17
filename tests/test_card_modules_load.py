"""Smoke tests that every card module actually LOADS.

`node --check` only parses — it happily accepts a reference to an undefined variable, because
that is a runtime error, not a syntax error. Migrating the cards to the shared helper module
introduced exactly that: a card whose version constant is named `CARD_VERSION` was given an
import interpolating `MEAL_PLANNER_CARD_VERSION`, which parsed fine and then threw
`ReferenceError` the moment a browser loaded it — i.e. the card would silently fail to register
and simply not appear on the dashboard.

These tests import each card as a real ES module under Node with the browser globals it touches
at module scope stubbed out, and assert it registers its custom element. That catches
module-scope ReferenceErrors, bad import specifiers, and top-level-await failures.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

WWW = Path(__file__).resolve().parents[1] / "custom_components" / "hellofresh" / "www"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

# (module filename, custom element it must define)
CARDS = (
    ("hellofresh-meal-planner-card.js", "hellofresh-meal-planner-card"),
    ("hellofresh-market-card.js", "hellofresh-market-card"),
    ("hellofresh-schedule-card.js", "hellofresh-schedule-card"),
    ("hellofresh-subscription-card.js", "hellofresh-subscription-card"),
    ("hellofresh-cost-card.js", "hellofresh-cost-card"),
    ("hellofresh-food-profile-card.js", "hellofresh-food-profile-card"),
    ("hellofresh-recipes-card.js", "hellofresh-recipes-card"),
)

# Minimal stubs for the browser surface the cards touch while their module body runs.
_BROWSER_STUBS = """
globalThis.window = {
  addEventListener() {}, removeEventListener() {},
  dispatchEvent(ev) { (globalThis.__events ||= []).push(ev); return true; },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
};
globalThis.document = { addEventListener() {}, removeEventListener() {}, createElement: () => ({}) };
globalThis.HTMLElement = class { attachShadow() { return (this._sr = {}); } };
globalThis.CSSStyleSheet = class { replaceSync() {} };
globalThis.CustomEvent = class {
  constructor(type, init) { this.type = type; this.detail = (init || {}).detail; }
};
globalThis.__defined = [];
globalThis.customElements = {
  define(name) { globalThis.__defined.push(name); },
  get() { return undefined; },
};
"""


def _load(filename: str) -> list[str]:
    """Import a card module under Node; return the custom elements it defined."""
    url = (WWW / filename).as_uri() + "?v=9.99"
    script = f"""
    {_BROWSER_STUBS}
    await import({json.dumps(url)});
    console.log(JSON.stringify(globalThis.__defined));
    """
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"{filename} failed to load as a module:\n{result.stderr.strip()}"
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(("filename", "element"), CARDS)
def test_card_module_loads_and_registers(filename: str, element: str) -> None:
    """A card that throws at module scope never registers and silently vanishes."""
    assert element in _load(filename)


@pytest.mark.parametrize("filename", [name for name, _ in CARDS])
def test_card_module_resolves_its_shared_imports(filename: str) -> None:
    """Top-level `await import(...)` must resolve before the class body runs.

    A card importing helpers it then calls synchronously during render would throw here if the
    specifier were wrong or the await were missing.
    """
    # _load asserts a clean exit, which is the whole check; this pins the intent separately.
    assert _load(filename)
