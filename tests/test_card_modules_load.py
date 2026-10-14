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
CARDS = (("hellofresh-card.js", "hellofresh-card"),)

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


# WebKit (Safari, and every browser on an iPad) resolves a second import() of a module that is
# still in its top-level await at once, and the importer finds exports that aren't initialized
# yet: "Cannot access 'esc' before initialization" on iPadOS 26.7.1. Node waits as the spec says,
# so this loader hook rewrites the card's import() calls to __hfImport, which behaves like WebKit:
# a module's exports throw until its first import has finished.
_WEBKIT_LOADER = """
export async function load(url, context, nextLoad) {
  if (!url.startsWith(WWW_URL)) return nextLoad(url, context);
  const result = await nextLoad(url, { ...context, format: "module" });
  const source = String(result.source).replace(/\\bimport\\(/g, "globalThis.__hfImport(");
  return { format: "module", source, shortCircuit: true };
}
"""

_WEBKIT_IMPORT = """
const finished = new Map();
const started = new Set();
globalThis.__hfImport = (url) => {
  if (started.has(url) && !finished.has(url)) {
    return Promise.resolve(new Proxy({}, {
      get(_target, name) {
        if (finished.has(url)) return finished.get(url)[name];
        if (name === "then" || typeof name === "symbol") return undefined;
        throw new ReferenceError(`Cannot access '${String(name)}' before initialization.`);
      },
    }));
  }
  started.add(url);
  const loading = import(url);
  loading.then((namespace) => finished.set(url, namespace), () => {});
  return loading;
};
"""


def _load(filename: str, *, webkit: bool = False, www: Path = WWW) -> list[str]:
    """Import a card module under Node; return the custom elements it defined.

    ``webkit`` makes a second import of an unfinished module resolve early, as WebKit does.
    """
    url = (www / filename).as_uri() + "?v=9.99"
    setup = ""
    if webkit:
        loader = _WEBKIT_LOADER.replace("WWW_URL", json.dumps(www.as_uri() + "/"))
        setup = f"""
    import {{ register }} from "node:module";
    register("data:text/javascript," + encodeURIComponent({json.dumps(loader)}), import.meta.url);
    {_WEBKIT_IMPORT}
    """
    script = f"""
    {setup}
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


@pytest.mark.parametrize(("filename", "element"), CARDS)
def test_card_module_loads_in_webkit(filename: str, element: str) -> None:
    """It loads when a second import of an unfinished module resolves early, as on WebKit.

    On iPadOS 26.7.1 the card never appeared ("The HelloFresh card couldn't load"):
    hellofresh-card-ui.js imported the logic module while that was still in its top-level await
    and read `esc` before it was set. The card now loads its shared modules one at a time.
    """
    assert element in _load(filename, webkit=True)


def _run_panel(www: Path) -> dict:
    """Load the panel under WebKit's import behavior while a dashboard is loading the card.

    ``www`` holds the panel and a stand-in card that defines its element after a top-level
    await, so the panel's import of the card is a second import of an unfinished module.
    """
    loader = _WEBKIT_LOADER.replace("WWW_URL", json.dumps(www.as_uri() + "/"))
    card_url = (www / "hellofresh-card.js").as_uri() + "?v=9.99"
    panel_url = (www / "hellofresh-panel.js").as_uri() + "?v=9.99"
    script = f"""
    import {{ register }} from "node:module";
    register("data:text/javascript," + encodeURIComponent({json.dumps(loader)}), import.meta.url);
    {_WEBKIT_IMPORT}
    const defined = new Map();
    const waiting = new Map();
    globalThis.customElements = {{
      define(name, cls) {{ defined.set(name, cls); for (const resolve of waiting.get(name) || []) resolve(cls); }},
      get: (name) => defined.get(name),
      whenDefined: (name) => defined.has(name)
        ? Promise.resolve(defined.get(name))
        : new Promise((resolve) => waiting.set(name, [...(waiting.get(name) || []), resolve])),
    }};
    const page = {{ innerHTML: "", append() {{}} }};
    globalThis.HTMLElement = class {{
      attachShadow() {{ this.shadowRoot = {{ innerHTML: "", querySelector: () => page }}; return this.shadowRoot; }}
      toggleAttribute() {{}}
    }};
    globalThis.document = {{
      createElement(name) {{
        const cls = defined.get(name);
        return cls ? new cls() : {{ setAttribute() {{}}, addEventListener() {{}} }};
      }},
    }};
    const failures = [];
    process.on("unhandledRejection", (err) => failures.push(String(err)));
    const dashboard = globalThis.__hfImport({json.dumps(card_url)});
    await import({json.dumps(panel_url)});
    new (defined.get("hellofresh-panel"))();
    await dashboard;
    await new Promise((resolve) => setTimeout(resolve, 100));
    console.log(JSON.stringify({{ mounted: Boolean(globalThis.__configured), failures, page: page.innerHTML }}));
    """
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr.strip()
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_panel_waits_for_the_card_element_in_webkit(tmp_path: Path) -> None:
    """The panel mounts the card once its element exists, not when its import says so.

    On WebKit the panel's import of the card can finish while a dashboard is still loading the
    card; mounting then would create a plain element without ``setConfig``.
    """
    panel = (WWW / "hellofresh-panel.js").read_text(encoding="utf-8")
    (tmp_path / "hellofresh-panel.js").write_text(panel, encoding="utf-8")
    (tmp_path / "hellofresh-card.js").write_text(
        "await new Promise((resolve) => setTimeout(resolve, 50));\n"
        "customElements.define('hellofresh-card', class extends HTMLElement {\n"
        "  setAttribute() {}\n"
        "  addEventListener() {}\n"
        "  setConfig(config) { globalThis.__configured = config; }\n"
        "});\n",
        encoding="utf-8",
    )
    outcome = _run_panel(tmp_path)
    assert outcome == {"mounted": True, "failures": [], "page": ""}, outcome


@pytest.mark.parametrize("filename", [name for name, _ in CARDS])
def test_card_module_resolves_its_shared_imports(filename: str) -> None:
    """Top-level `await import(...)` must resolve before the class body runs.

    A card importing helpers it then calls synchronously during render would throw here if the
    specifier were wrong or the await were missing.
    """
    # _load asserts a clean exit, which is the whole check; this pins the intent separately.
    assert _load(filename)
