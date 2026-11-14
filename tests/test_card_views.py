"""The unified card's views, driven under Node against a stand-in card.

Each test runs the real view modules (``hellofresh-card-menu.js``, ``-deliveries.js``,
``-week.js``, ``-recipes.js``, ``-profile.js``, ``-account.js``) with a card object that has
only the handful of methods the views call, and checks what they draw and which services they
call. What a browser adds (focus, the DOM patched in place) is covered by tests/browser.

Covered here:

* "Ingredients to avoid" hides unchecked meals until HelloFresh answers, says so, and Retry asks
  again; another filter that fails falls back to the menu data with the same notice;
* a sold-out option in the customize drawer can be chosen (sold out is advisory);
* typing in a search redraws once the typing pauses, and Recipes never says "No recipes found"
  while a search is still on its way; the public catalog isn't re-read on account refreshes;
* ``recipes_limit`` is kept within what the service takes, a full page says it is the first N,
  and a category picked during a load is loaded next; Load more asks for the next page from where
  the list ends, adds it (each recipe once) and says how many came, a failed page offers Retry,
  and a page for a list replaced meanwhile is dropped;
* one week's number of people: the week head's line, the sizes on offer (read once, again after
  a failure), the confirmation that it is this week only, the write and its Undo;
* the account's HelloFresh Cash, bonus credit and boxes until a free one, the payment-problem
  banner, a box HelloFresh moved, and the Undo of a recurring plan change;
* the food profile sends only the sections that changed, never an unanswered household, re-reads
  its completion after a save and shows a failed reload;
* a recurring plan change shows its option and blocks another one until the entity reports it;
* the Week tab: a typed note is chosen and saved, a save goes out once, a partial save says what
  was kept, and times and dates are Home Assistant's whatever the browser's zone;
* the Overview hero, delivery photos (https only, no referrer), Skip after the deadline, and the
  accessibility markup (tablist, text for colour-only dots, no controls inside other controls).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

WWW = Path(__file__).resolve().parents[1] / "custom_components" / "hellofresh" / "www"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

MODULES = {
    "L": "hellofresh-card-logic.js",
    "U": "hellofresh-card-ui.js",
    "M": "hellofresh-card-menu.js",
    "O": "hellofresh-card-deliveries.js",
    "W": "hellofresh-card-week.js",
    "R": "hellofresh-card-recipes.js",
    "P": "hellofresh-card-profile.js",
    "A": "hellofresh-card-account.js",
}

PREAMBLE = """
globalThis.window = {
  localStorage: { getItem: () => null, setItem() {} },
  dispatchEvent() { return true; },
  confirm: () => true,
};
globalThis.CustomEvent = class { constructor(t, i) { this.type = t; this.detail = (i || {}).detail; } };
globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);
globalThis.CSS = { escape: (s) => String(s) };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// Markup as text: tags dropped, the escaped characters back, and the narrow and no-break spaces
// Intl puts in times read as spaces.
const ENTITIES = { "&#39;": "'", "&quot;": '"', "&lt;": "<", "&gt;": ">", "&amp;": "&" };
const text = (html) => String(html).replace(/<[^>]+>/g, " ").replace(/&(#39|quot|lt|gt|amp);/g, (e) => ENTITIES[e])
  .replace(/[\\s\\u00a0\\u202f]+/g, " ").trim();
const el = (attrs) => ({ getAttribute: (k) => (k in attrs ? attrs[k] : null) });
const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const inDays = (n) => new Date(Date.now() + n * 86400000);
// The names on the Menu's tiles (each tile's one "open" button).
const tileNames = (html) => [...html.matchAll(/class="hf-open"[^>]*><span>([^<]+)<\\/span>/g)].map((m) => m[1]);

function makeWeek(extra = {}) {
  return {
    week_id: "2026-W41", display_name: "Week 41", delivery_date: iso(inDays(6)),
    selection_deadline: inDays(3).toISOString(), meals_required: 3, allowed_actions: { mealSwap: true },
    recipes: [1, 2, 3, 4, 5].map((i) => ({
      recipe_id: `r${i}`, course_index: i, name: `Meal ${i}`, preference: "Beef",
      is_selected: i <= 3, selected_quantity: i <= 3 ? 1 : null,
    })),
    market_items: [
      { item_id: "m1", name: "Bread", price_cents: 499, price: 4.99, max_quantity: 2, is_selected: true, selected_quantity: 1 },
    ],
    order: { currency: "USD" },
    ...extra,
  };
}

// The card as the views see it. `view` is the view under test once set: renderView draws it.
function makeCard(weeks, extra = {}) {
  const card = {
    view: "menu", busy: false, saving: null, calls: [], toasts: [], renders: 0, fetchSeq: 1,
    weeks, account: {}, summary: null, error: null, mealPlan: null, config: { views: ["overview", "menu", "market", "recipes", "week", "account"] },
    hass: {}, sheet: null, sheetKind: null, closed: 0, html: "", selectedWeekId: null, lastFetched: null,
    shadowRoot: { querySelector: () => null, activeElement: null },
    weekById(id) { return this.weeks.find((w) => w.week_id === id) || null; },
    selectedWeek() { return this.weeks[0]; },
    weeksPlaceholder: () => "", pantryEntityFor: () => null, entityState: () => null, entities: () => ({}),
    liveTracking: () => null, menuGraceWeeks: () => 2,
    hasView(v) { return this.config.views.includes(v); },
    async call(service, data) { this.calls.push([service, JSON.parse(JSON.stringify(data || {}))]); return {}; },
    setSaving(text) { this.busy = Boolean(text); this.saving = text; if (text) this.savingSeen = text; },
    renderView() { this.renders += 1; if (this.viewObj) this.html = this.viewObj.render(); },
    renderBoxBar() {}, broadcastDataChanged() { this.broadcasts = (this.broadcasts || 0) + 1; },
    async reloadWeeks(options = {}) { (this.reloads ||= []).push(options.weekId || null); },
    openSheet(sheet) { this.sheet = sheet; this.sheetKind = sheet.kind; this.sheetHtml = sheet.render(); },
    renderSheet() { if (this.sheet) this.sheetHtml = this.sheet.render(); },
    closeSheet() {
      const sheet = this.sheet;
      if (!sheet) return;
      this.sheet = null; this.sheetKind = null; this.closed += 1;
      if (sheet.onClose) sheet.onClose();
    },
    // The card's toast: an `undo` offer ({weekId, run}) rides along, and is kept to press.
    toast(message, isError = false, { undo = null } = {}) { this.toasts.push([message, isError]); (this.undos ||= []).push(undo); },
    weekUndo(weekId, run) { return L.undoWindowOpen(this.weekById(weekId)) ? { weekId, run } : null; },
    undoResult(failed) { this.toast(failed ? `Couldn't undo: ${failed}` : "Change undone.", Boolean(failed)); },
    announce(message) { (this.announced ||= []).push(message); },
    // The plan options as the card keeps them (get_plan_options, read once).
    planOptions: null,
    async loadPlanOptions() {
      if (this.planOptions && this.planOptions.status !== "failed") return;
      (this.optionReads ||= []).push(1);
      this.planOptions = { status: "loading", options: null, error: null };
      try {
        const response = await this.call("get_plan_options");
        this.planOptions = { status: "ok", options: response.options || [], error: null };
      } catch (err) {
        this.planOptions = { status: "failed", options: null, error: err.message };
      }
      this.renderSheet();
    },
    confirmSkip(week) { this.skipAsked = week.week_id; },
    setMealPlan(plan) { this.mealPlan = plan; }, async loadMealPlan() {},
    ...extra,
  };
  card.box = new M.BoxStore(card);
  return card;
}
"""


def _run(body: str, tz: str = "America/New_York") -> object:
    imports = "\n".join(
        f"const {name} = await import({json.dumps((WWW / file).as_uri() + '?v=test')});"
        for name, file in MODULES.items()
    )
    script = f"""
    {imports}
    {PREAMBLE}
    const result = await (async () => {{ {body} }})();
    console.log(JSON.stringify(result));
    """
    out = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "TZ": tz},
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


# ---- Menu: HelloFresh's filter service --------------------------------------------------------

ALLERGENS = """
  const week = makeWeek({ menu_filters: [
    { slug: "exclude-allergens", name: "Ingredients to avoid", options: [{ slug: "peanuts", name: "Peanuts" }] },
    { slug: "cuisine", name: "Cuisine type", options: [{ slug: "thai", name: "Thai" }] },
  ] });
  const card = makeCard([week]);
  const view = new M.MealsView(card);
  card.viewObj = view;
  card.lookup = "fail";
  // The filter service: Meal 4 is the one meal outside the box it lets through.
  card.call = async (service, data) => {
    card.calls.push([service, data]);
    await sleep(10);
    if (card.lookup === "fail") throw new Error("HTTP 503");
    return { recipe_ids: ["r4"] };
  };
  const lookups = () => card.calls.filter(([s]) => s === "get_menu_courses").length;
  const notice = (html) => {
    const m = html.match(/<div class="hf-notice[^"]*hf-filternotice"[\\s\\S]*?<\\/div>\\s*(<button[^>]*retry-filters[^>]*>[\\s\\S]*?<\\/button>)?\\s*<\\/div>/);
    return m ? { text: text(m[0]), retry: /data-action="retry-filters"/.test(m[0]) } : null;
  };
"""


def test_ingredients_to_avoid_hides_unchecked_meals_until_hellofresh_answers() -> None:
    """With "Ingredients to avoid: Peanuts" on, a meal HelloFresh hasn't cleared is never shown:
    only the box's own meals while the check runs or after it fails, with a notice and Retry."""
    body = (
        ALLERGENS
        + """
      view.f.server["exclude-allergens"].add("peanuts");
      const pending = view.render();
      await sleep(40);
      const failed = view.render();
      const askedAfterFail = lookups();
      view.render();
      const askedAfterRender = lookups();
      card.lookup = "ok";
      view.onClick({}, el({ "data-action": "retry-filters" }));
      await sleep(40);
      const answered = view.render();
      return {
        pending: [tileNames(pending), notice(pending), /hf-busynote/.test(pending)],
        failed: [tileNames(failed), notice(failed), /Filters · 1/.test(text(failed))],
        asked: [askedAfterFail, askedAfterRender, lookups()],
        answered: [tileNames(answered), notice(answered)],
      };
    """
    )
    got = _run(body)
    names, note, busy = got["pending"]
    assert names == ["Meal 1", "Meal 2", "Meal 3"]
    assert "Checking with HelloFresh which meals avoid those ingredients" in note["text"]
    assert not note["retry"] and not busy  # the notice says it; no second "Filtering…"
    names, note, counted = got["failed"]
    assert names == ["Meal 1", "Meal 2", "Meal 3"] and counted
    assert "couldn't check your ingredients to avoid" in note["text"] and note["retry"]
    # A failure is kept (no retry loop on every render) until Retry asks again.
    assert got["asked"] == [1, 1, 2]
    assert got["answered"] == [["Meal 1", "Meal 2", "Meal 3", "Meal 4"], None]


def test_another_filter_that_fails_uses_the_menu_data_and_says_so() -> None:
    body = (
        ALLERGENS
        + """
      view.f.server.cuisine.add("thai");
      view.render();
      await sleep(40);
      const failed = view.render();
      return [tileNames(failed), notice(failed)];
    """
    )
    names, note = _run(body)
    assert names == ["Meal 1", "Meal 2", "Meal 3", "Meal 4", "Meal 5"]  # nothing hidden unchecked
    assert "filter service didn't answer" in note["text"] and note["retry"]


def test_a_sold_out_option_can_be_chosen_in_the_customize_drawer() -> None:
    """Sold out is advisory (HelloFresh's regional flag, not this subscription's): the drawer
    shows the note and lets the option be picked, as the grid's tiles do."""
    body = """
      const week = makeWeek();
      week.recipes[0].variation_group = 1;
      week.recipes.push({ recipe_id: "r6", course_index: 6, name: "Meal 1", variation_group: 1, variation_order: 1,
        variation_title: "Salmon", is_sold_out: true, preference: "Seafood" });
      const card = makeCard([week]);
      const view = new M.MealsView(card);
      view.render();
      view.onClick({}, el({ "data-action": "customize", "data-key": "1" }));
      const drawer = card.sheetHtml;
      const row = (drawer.match(/<button class="hf-optrow[^"]*"[^>]*data-key="6"[\\s\\S]*?<\\/button>/) || [""])[0];
      card.sheet.onClick({}, el({ "data-action": "opt-pick", "data-key": "6" }));
      card.sheet.onClick({}, el({ "data-action": "opt-apply" }));
      return { row: text(row), disabled: /^<button[^>]*\\sdisabled/.test(row), soldout: /hf-optrow[^"]*soldout/.test(row),
        meals: [...card.box.displayMeals(week).keys()].sort((a, b) => a - b) };
    """
    got = _run(body)
    assert "Salmon" in got["row"] and "Sold out" in got["row"]
    assert got["soldout"] and not got["disabled"]
    assert got["meals"] == [2, 3, 6]  # Meal 1 swapped for its Salmon option


def test_typing_in_a_search_redraws_once_the_typing_pauses() -> None:
    """Menu and Market: a keystroke redraws nothing (the field stays as typed); the results are
    redrawn once, after the pause."""
    body = """
      const week = makeWeek();
      const card = makeCard([week]);
      const meals = new M.MealsView(card);
      card.viewObj = meals;
      meals.render();
      const type = (view, key, value) => view.onInput({ target: { closest: (sel) => (sel.includes(key) ? { value } : null) } });
      for (const value of ["m", "me", "mea", "meal 4"]) type(meals, "meal-search", value);
      const duringMenu = card.renders;
      await sleep(250);
      const afterMenu = [card.renders, tileNames(card.html)];
      card.view = "market";
      card.renders = 0;
      const market = new M.MarketView(card);
      card.viewObj = market;
      for (const value of ["b", "br", "bre"]) type(market, "market-search", value);
      const duringMarket = card.renders;
      await sleep(250);
      return { duringMenu, afterMenu, duringMarket, afterMarket: card.renders };
    """
    got = _run(body)
    assert got["duringMenu"] == 0 and got["afterMenu"] == [1, ["Meal 4"]]
    assert got["duringMarket"] == 0 and got["afterMarket"] == 1


# ---- Recipes ---------------------------------------------------------------------------------

RECIPES = """
  const card = makeCard([], { view: "recipes" });
  card.call = async (service, data) => {
    card.calls.push([service, data]);
    if (service === "get_recipe_collections") return { collections: [{ slug: "a", path: "a", name: "A" }, { slug: "b", path: "b", name: "B" }] };
    if (service === "get_catalog_recipes" && data.search) {
      await sleep(data.search === "chi" ? 150 : 400);
      return { recipes: [{ recipe_id: "x", name: `Found ${data.search}` }] };
    }
    await sleep(card.latency || 0);
    const n = Math.min(data.limit, card.catalogSize || 3);
    return { recipes: Array.from({ length: n }, (_, i) => ({ recipe_id: `${data.collection || "top"}${i}`, name: `${data.collection || "top"} ${i}` })) };
  };
  const view = new R.RecipesView(card);
  card.viewObj = view;
  const type = (value) => view.onInput({ target: { closest: () => ({ value }) } });
"""


def test_recipes_search_never_says_nothing_found_while_a_search_is_on_its_way() -> None:
    """Search A ("chi") is answered after the user typed on: its answer is dropped, and so is
    its "done" — the tab keeps saying it is searching instead of "No recipes found"."""
    body = (
        RECIPES
        + """
      view.collection = "";
      view.fetched = true;
      const seen = [];
      const look = () => seen.push(text(view._body()) || "(skeleton)");
      type("chi");
      look();
      await sleep(360);                    // the debounce went off: search A is out
      type("chic");                        // typed on while A is out
      look();
      await sleep(200);                    // A answered (for text no longer there)
      look();
      await sleep(300);                    // search B is out
      look();
      await sleep(500);
      return { seen, last: text(view._body()), searches: card.calls.filter(([, d]) => d && d.search).map(([, d]) => d.search),
        renders: card.renders };
    """
    )
    got = _run(body)
    assert all("No recipes found" not in s for s in got["seen"]), got["seen"]
    assert got["searches"] == ["chi", "chic"]
    assert "Found chic" in got["last"]
    assert got["renders"] <= 2  # one when the search started, one when it answered


def test_a_failed_recipe_search_says_so_and_can_be_tried_again() -> None:
    """Not "No recipes found": the search didn't answer, and Try again asks again."""
    body = (
        RECIPES
        + """
      view.collection = "";
      view.fetched = true;
      const call = card.call;
      card.call = async (service, data) => {
        if (data && data.search && card.searchDown) {
          card.calls.push([service, data]);
          throw new Error("HTTP 504");
        }
        return call(service, data);
      };
      card.searchDown = true;
      type("chicken");
      await sleep(400);
      const failed = text(view._body());
      card.searchDown = false;
      view.onClick({ target: { closest: () => null } }, el({ "data-action": "retry-search" }));
      const retrying = text(view._body()) || "(skeleton)";
      await sleep(500);
      return { failed, retrying, found: text(view._body()), toasts: card.toasts,
        searches: card.calls.filter(([, d]) => d && d.search).length };
    """
    )
    got = _run(body)
    assert got["failed"] == "Search failed: HTTP 504 Try again"
    assert got["retrying"] == "(skeleton)"
    assert "Found chicken" in got["found"] and got["searches"] == 2
    assert got["toasts"] == []  # said in place, where the results would be


def test_recipes_limit_is_kept_within_what_the_service_takes() -> None:
    body = (
        RECIPES
        + """
      const limits = [500, 0, -3, 7.6, "abc", undefined].map((value) => {
        card.config.recipes_limit = value;
        return view._limit();
      });
      card.config.recipes_limit = 3;
      card.catalogSize = 10;
      view.collection = "a";
      await view._fetch();
      const capped = text(view._body());
      card.catalogSize = 2;
      view.collection = "b";
      await view._fetch();
      return { limits, capped, short: text(view._body()), asked: card.calls.filter(([s]) => s === "get_catalog_recipes").map(([, d]) => d.limit) };
    """
    )
    got = _run(body)
    assert got["limits"] == [200, 50, 1, 8, 50, 50]
    assert got["capped"].startswith("Showing the first 3 recipes")
    assert "Showing the first" not in got["short"]
    assert got["asked"] == [3, 3]


def test_a_category_picked_while_another_loads_is_loaded_next() -> None:
    """The click is queued, not dropped, and the earlier answer doesn't land on the new one."""
    body = (
        RECIPES
        + """
      card.latency = 50;
      view.collection = "a";
      const first = view._fetch();
      await sleep(10);
      view.onClick({ target: { closest: () => null } }, el({ "data-action": "collection", "data-slug": "b" }));
      await first;
      await sleep(120);
      return { collection: view.collection, recipes: view.recipes.map((r) => r.recipe_id), loading: view.loading,
        asked: card.calls.filter(([s]) => s === "get_catalog_recipes").map(([, d]) => d.collection) };
    """
    )
    got = _run(body)
    assert got["asked"] == ["a", "b"]
    assert got["collection"] == "b" and got["recipes"] == ["b0", "b1", "b2"] and not got["loading"]


def test_account_refreshes_leave_the_public_catalog_alone() -> None:
    """The card refreshes the account every few minutes on a delivery day and after every write:
    the catalog (public) isn't re-read for that; the cookbook (the account's) is."""
    body = (
        RECIPES
        + """
      view.collection = "a";
      await view._fetch();
      const before = card.calls.length;
      view.onRefresh();
      await sleep(20);
      const catalog = card.calls.length - before;
      view.collection = "@cookbook";
      view.onRefresh();
      await sleep(20);
      return { catalog, cookbook: card.calls.slice(before).map(([s]) => s) };
    """
    )
    got = _run(body)
    assert got["catalog"] == 0
    assert got["cookbook"] == ["get_favorites"]


# A catalog of seven recipes served three at a time, with the total and whether more follow.
PAGED = """
  const card = makeCard([], { view: "recipes" });
  const catalog = Array.from({ length: 7 }, (_, i) => ({ recipe_id: `c${i}`, name: `Recipe ${i}` }));
  card.call = async (service, data) => {
    card.calls.push([service, JSON.parse(JSON.stringify(data || {}))]);
    if (service === "get_recipe_collections") return { collections: [{ slug: "a", path: "a", name: "A" }] };
    if (service !== "get_catalog_recipes") return {};
    await sleep(card.latency || 0);
    if (card.pageDown && data.offset) throw new Error("HTTP 502");
    const all = data.search ? catalog.filter((r) => r.name.includes(data.search)) : catalog;
    const offset = data.offset || 0;
    // The catalog moved: a page repeats the last one's final recipe.
    const start = card.overlap && offset ? offset - 1 : offset;
    return { recipes: all.slice(start, offset + data.limit), offset, total: all.length, has_more: offset + data.limit < all.length };
  };
  card.config.recipes_limit = 3;
  const view = new R.RecipesView(card);
  card.viewObj = view;
  const more = () => view.onClick({ target: { closest: () => null } }, el({ "data-action": "load-more" }));
  const type = (value) => view.onInput({ target: { closest: () => ({ value }) } });
  const pages = () => card.calls.filter(([s]) => s === "get_catalog_recipes").map(([, d]) => [d.offset ?? null, d.limit, d.collection || d.search || ""]);
  const ids = (list) => list.map((r) => r.recipe_id);
"""


def test_load_more_adds_the_next_page_and_says_how_many_came() -> None:
    """The next page starts where the list ends, with the same limit; the line above counts
    against the total; a repeated recipe is added once; the last page ends Load more."""
    body = (
        PAGED
        + """
      view.collection = "a";
      await view._fetch();
      const first = text(view._body());
      more();
      const loading = view._body();
      await sleep(10);
      const second = text(view._body());
      card.overlap = true;
      more();
      await sleep(10);
      return { first, loading: [/aria-disabled="true"/.test(loading), text(loading).includes("Loading more")],
        second, third: text(view._body()), recipes: ids(view.recipes), pages: pages(), announced: card.announced };
    """
    )
    got = _run(body)
    assert got["first"].startswith("Showing 3 of 7 recipes") and got["first"].endswith("Load more")
    assert got["loading"] == [True, True]
    assert got["second"].startswith("Showing 6 of 7 recipes") and got["second"].endswith(
        "Load more"
    )
    # All seven shown: a whole category says nothing more, and offers nothing more.
    assert "Showing" not in got["third"] and "Load more" not in got["third"]
    assert got["recipes"] == ["c0", "c1", "c2", "c3", "c4", "c5", "c6"]
    assert got["pages"] == [[None, 3, "a"], [3, 3, "a"], [6, 3, "a"]]
    assert got["announced"] == ["3 more recipes loaded", "1 more recipe loaded"]


def test_a_failed_page_offers_retry_and_a_page_for_a_replaced_list_is_dropped() -> None:
    body = (
        PAGED
        + """
      view.collection = "a";
      await view._fetch();
      card.pageDown = true;
      more();
      await sleep(10);
      const failed = view._body();
      card.pageDown = false;
      more();                                   // Retry
      await sleep(10);
      const retried = ids(view.recipes);
      card.latency = 40;
      more();                                   // on its way when another category is picked
      await sleep(5);
      view.onClick({ target: { closest: () => null } }, el({ "data-action": "collection", "data-slug": "" }));
      await sleep(150);
      return { failed: text(failed), retry: /data-action="load-more"[^>]*>[\\s\\S]*?Retry/.test(failed), retried,
        after: ids(view.recipes), collection: view.collection, more: view.more, pages: pages() };
    """
    )
    got = _run(body)
    assert "Couldn't load more recipes: HTTP 502" in got["failed"] and got["retry"]
    assert "Load more" not in got["failed"]
    assert got["retried"] == ["c0", "c1", "c2", "c3", "c4", "c5"]
    # Top rated's own first page, nothing of category A's late one.
    assert got["after"] == ["c0", "c1", "c2"] and got["collection"] == "" and got["more"] is None
    assert got["pages"][-2:] == [[6, 3, "a"], [None, 3, ""]]


def test_search_results_load_more_and_a_new_search_starts_again() -> None:
    body = (
        PAGED
        + """
      view.collection = "";
      view.fetched = true;
      type("Recipe");
      await sleep(400);
      const first = text(view._body());
      more();
      await sleep(10);
      const second = ids(view.searchResults);
      type("Recipe 1");
      const typing = text(view._body());
      await sleep(400);
      return { first, second, typing, last: text(view._body()), pages: pages() };
    """
    )
    got = _run(body)
    assert got["first"].startswith("Showing 3 of 7 results across every category") and got[
        "first"
    ].endswith("Load more")
    assert got["second"] == ["c0", "c1", "c2", "c3", "c4", "c5"]
    # The results on screen while another search runs are an older search's: no more of those.
    assert "Load more" not in got["typing"]
    assert got["last"].startswith("1 result across every category")
    assert got["pages"] == [[None, 3, "Recipe"], [3, 3, "Recipe"], [None, 3, "Recipe 1"]]


# ---- Food preferences ------------------------------------------------------------------------

PROFILE = """
  const card = makeCard([], { view: "account" });
  const editor = new P.ProfileEditor(card);
  const response = () => ({
    options: { taste: { cuisines: ["italian", "thai"], mealTypes: ["quick"] }, household: { adults: [1, 2, 3, 4], children: [0, 1, 2, 3] }, meta: {} },
    profile: { taste: { mealTypes: ["quick"], exclusions: ["pork"] }, household: {}, goals: {} },
    completion: { total: 4, completed: card.saves ? 3 : 2, percent: card.saves ? 75 : 50, incomplete_fields: [] },
  });
  card.call = async (service, data) => {
    card.calls.push([service, JSON.parse(JSON.stringify(data || {}))]);
    if (service === "get_food_profile") {
      if (card.profileDown) throw new Error("HTTP 502");
      return response();
    }
    if (service === "set_food_profile") {
      card.saves = (card.saves || 0) + 1;
      return { profile: { ...response().profile, ...data } };
    }
    return {};
  };
"""


def test_food_profile_saves_only_what_changed_and_never_an_unanswered_household() -> None:
    """The household was never answered (the panel shows 1 adult, 0 kids as a starting point):
    liking a cuisine must not write "0 adults, 0 kids" or empty goals with it."""
    body = (
        PROFILE
        + """
      await editor.fetch();
      editor._cycleWeighted("cuisines", "italian", "like");
      const tasteOnly = editor.buildChanges();
      editor._stepHousehold("children", 1);
      const withKids = editor.buildChanges();
      await editor.save();
      return { tasteOnly, withKids, sent: card.calls.filter(([s]) => s === "set_food_profile").map(([, d]) => d),
        completion: editor.completion.completed, reads: card.calls.filter(([s]) => s === "get_food_profile").length };
    """
    )
    got = _run(body)
    assert got["tasteOnly"] == {
        "taste": {"exclusions": ["pork"], "mealTypes": ["quick"], "cuisines": {"italian": 100}}
    }
    # Only the count the user set; the adults were never answered and stay out.
    assert got["withKids"]["household"] == {"children": 1} and "goals" not in got["withKids"]
    assert got["sent"] == [got["withKids"]]
    # The completion figure is re-read after the save ("Tell us more" follows what was saved).
    assert got["completion"] == 3 and got["reads"] == 2


def test_a_failed_reload_of_the_food_profile_is_shown() -> None:
    body = (
        PROFILE
        + """
      await editor.fetch();
      card.profileDown = true;
      await editor.fetch();
      return text(editor.render());
    """
    )
    shown = _run(body)
    assert shown.startswith("Couldn't reload your food preferences: HTTP 502 Try again")
    assert "Adults" in shown  # the profile loaded before stays on screen under it


# ---- Account: the recurring plan controls ----------------------------------------------------

PLAN = """
  const states = {
    "select.box": { state: "3 meals for 2 people", attributes: { options: ["3 meals for 2 people", "4 meals for 2 people"] } },
    "select.day": { state: "Monday 8AM - 8PM", attributes: { options: ["Monday 8AM - 8PM", "Tuesday 8AM - 8PM"] } },
  };
  const card = makeCard([], {
    view: "account", summary: { selected_plan: "Classic", subscription_status: "active" },
    entities: () => ({ boxSize: "select.box", deliveryDay: "select.day" }),
    entityState: (id) => states[id] || null,
  });
  let release;
  card.gate = new Promise((r) => (release = r));
  card.hass = {
    async callService(domain, service, data, target) {
      card.calls.push([`${domain}.${service}`, data, target]);
      await card.gate;
      if (card.planFails) throw new Error("HelloFresh said no");
      if (!card.entityLags) states[target.entity_id] = { ...states[target.entity_id], state: data.option };
    },
  };
  const view = new A.AccountView(card);
  card.viewObj = view;
  const pick = (key, entity, value) =>
    view.onChange({ target: { closest: () => ({ value, getAttribute: (k) => ({ "data-entity": entity, "data-plan-control": key })[k] }) } });
  const control = (html, key) => {
    const select = (html.match(new RegExp(`<select[^>]*data-plan-control="${key}"[\\\\s\\\\S]*?</select>`)) || [""])[0];
    return { disabled: /^<select[^>]*\\sdisabled/.test(select), shown: (select.match(/<option value="([^"]*)" selected>/) || [])[1] };
  };
  const writes = () => card.calls.filter(([s]) => s === "select.select_option").length;
"""


def test_a_plan_change_shows_its_option_and_blocks_another_until_it_is_through() -> None:
    body = (
        PLAN
        + """
      pick("box_size", "select.box", "4 meals for 2 people");
      card.sheet.onClick({}, el({ "data-action": "confirm-plan" }));
      await sleep(0);
      const during = view.render();
      // Meanwhile: the other control, and a second write by any route, are refused.
      pick("delivery_day", "select.day", "Tuesday 8AM - 8PM");
      const sheetMeanwhile = card.sheet;
      await view._changePlan("select.day", "delivery_day", "Tuesday 8AM - 8PM");
      const writesMeanwhile = writes();
      release();
      await sleep(10);
      const after = view.render();
      return {
        saving: card.toasts.length ? null : null, busyText: card.savingSeen,
        during: [control(during, "box_size"), control(during, "delivery_day")],
        sheetMeanwhile, writesMeanwhile,
        after: [control(after, "box_size"), control(after, "delivery_day")],
        pending: view.planPending, toasts: card.toasts, busy: card.busy,
      };
    """
    )
    body = body.replace(
        "const view = new A.AccountView(card);",
        "const setSaving = card.setSaving.bind(card);\n"
        "  card.setSaving = (t) => { if (t) card.savingSeen = t; setSaving(t); };\n"
        "  const view = new A.AccountView(card);",
    )
    got = _run(body)
    assert got["busyText"] == "Updating your plan…"
    assert got["during"] == [
        {"disabled": True, "shown": "4 meals for 2 people"},
        {"disabled": True, "shown": "Monday 8AM - 8PM"},
    ]
    assert got["sheetMeanwhile"] is None and got["writesMeanwhile"] == 1
    assert got["after"] == [
        {"disabled": False, "shown": "4 meals for 2 people"},
        {"disabled": False, "shown": "Monday 8AM - 8PM"},
    ]
    assert got["pending"] is None and got["busy"] is False
    assert got["toasts"] == [["Box size changed.", False]]


def test_an_accepted_plan_change_waits_for_its_entity_and_a_refused_one_lets_go() -> None:
    body = (
        PLAN
        + """
      card.entityLags = true;                     // Home Assistant's state trails the answer
      pick("box_size", "select.box", "4 meals for 2 people");
      card.sheet.onClick({}, el({ "data-action": "confirm-plan" }));
      release();
      await sleep(10);
      const lagging = control(view.render(), "box_size");
      states["select.box"] = { ...states["select.box"], state: "4 meals for 2 people" };
      const caughtUp = control(view.render(), "box_size");

      card.planFails = true;
      pick("delivery_day", "select.day", "Tuesday 8AM - 8PM");
      card.sheet.onClick({}, el({ "data-action": "confirm-plan" }));
      await sleep(10);
      return { lagging, caughtUp, refused: control(view.render(), "delivery_day"), toast: card.toasts.at(-1), pending: view.planPending };
    """
    )
    got = _run(body)
    assert got["lagging"] == {"disabled": True, "shown": "4 meals for 2 people"}
    assert got["caughtUp"] == {"disabled": False, "shown": "4 meals for 2 people"}
    assert got["refused"] == {"disabled": False, "shown": "Monday 8AM - 8PM"}
    assert got["toast"] == ["Couldn't change the delivery day: HelloFresh said no", True]
    assert got["pending"] is None


def test_a_plan_change_is_undone_by_selecting_the_option_it_had() -> None:
    """Undo selects the plan's old option through the same path (the card busy, the select
    showing it), even before the entity has reported the change; it offers no further undo."""
    body = (
        PLAN
        + """
      card.entityLags = true;                     // the change hasn't reached the entity yet
      pick("box_size", "select.box", "4 meals for 2 people");
      card.sheet.onClick({}, el({ "data-action": "confirm-plan" }));
      release();
      await sleep(10);
      const offer = card.undos.at(-1);
      card.entityLags = false;
      const running = offer.run();
      const during = control(view.render(), "box_size");
      await running;
      return { offer: offer.weekId, during, saving: card.savingSeen, after: control(view.render(), "box_size"),
        writes: card.calls.filter(([s]) => s === "select.select_option").map(([, d]) => d.option),
        toasts: card.toasts, lastOffer: card.undos.at(-1), pending: view.planPending };
    """
    )
    got = _run(body)
    assert got["offer"] is None  # the plan is no one week's
    assert got["during"] == {"disabled": True, "shown": "3 meals for 2 people"}
    assert got["saving"] == "Undoing…"
    assert got["writes"] == ["4 meals for 2 people", "3 meals for 2 people"]
    assert got["after"] == {"disabled": False, "shown": "3 meals for 2 people"}
    assert got["toasts"] == [["Box size changed.", False], ["Change undone.", False]]
    assert got["lastOffer"] is None and got["pending"] is None


def test_account_lists_cash_bonus_credit_and_the_boxes_until_a_free_one() -> None:
    body = """
      const card = makeCard([], { view: "account", summary: {
        selected_plan: "Classic", account_credit: 12.5, account_credit_currency: "EUR",
        account_credit_cash: 10, account_credit_bonus: 2.5, loyalty_boxes_until_free_box: 1,
      } });
      const view = new A.AccountView(card);
      const kv = (html) => [...html.matchAll(/<div class="[^"]*"><div class="hf-k">([^<]*)<\\/div><div class="hf-v">([^<]*)<\\/div><\\/div>/g)]
        .map((m) => [m[1], m[2]]);
      const rows = kv(view.render());
      card.summary = { ...card.summary, account_credit_cash: 0, account_credit_bonus: null, loyalty_boxes_until_free_box: null };
      return { rows, without: kv(view.render()).map(([k]) => k) };
    """
    got = _run(body)
    at = [k for k, _ in got["rows"]].index("Credit")
    assert got["rows"][at : at + 4] == [
        ["Credit", "€12.50"],
        ["HelloFresh Cash", "€10.00"],
        ["Bonus credit", "€2.50"],
        ["Rewards", "1 box until a free box"],
    ]
    assert not {"HelloFresh Cash", "Bonus credit", "Rewards"} & set(got["without"])


# ---- Menu: one week's number of people -----------------------------------------------------------

SERVINGS = """
  const week = makeWeek({ servings: 2, display_name: "Classic Box" });
  const card = makeCard([week], { summary: { number_of_people: 2, selected_plan_total_price_currency: "USD" } });
  const options = [
    { handle: "US-CBU-3-2-0", family_handle: "cbu", family_name: "Classic", meals: 3, servings: 2, price: 71.94 },
    { handle: "US-CBU-3-4-0", family_handle: "cbu", family_name: "Classic", meals: 3, servings: 4, price: 131.88 },
    { handle: "US-CBU-4-4-0", family_handle: "cbu", family_name: "Classic", meals: 4, servings: 4, price: 167.84 },
    { handle: "US-VEG-3-2-0", family_handle: "veg", family_name: "Veggie", meals: 3, servings: 2, price: 68.94 },
    { handle: "US-VEG-3-6-0", family_handle: "veg", family_name: "Veggie", meals: 3, servings: 6, price: 160 },
  ];
  card.call = async (service, data) => {
    card.calls.push([service, JSON.parse(JSON.stringify(data || {}))]);
    if (service === "get_plan_options") {
      if (card.optionsDown) throw new Error("HTTP 500");
      return { options };
    }
    if (service === "change_week_servings") {
      if (card.refuse) throw new Error("HelloFresh doesn't offer a box for that many people this week");
      week.servings = data.servings;
      return { week_id: data.week_id, servings: data.servings, product_handle: "US-CBU-3-4-0", price: 131.88, currency: "USD" };
    }
    return {};
  };
  const menu = new M.MealsView(card);
  const people = (html) => text((html.match(/<div class="hf-weekpeople">[\\s\\S]*?<\\/div>/) || [""])[0]);
  const writes = () => card.calls.filter(([s]) => s === "change_week_servings").map(([, d]) => d);
"""


def test_a_weeks_people_change_lists_its_sizes_asks_first_and_offers_undo() -> None:
    body = (
        SERVINGS
        + """
      const line = people(menu.render());
      card.optionsDown = true;
      menu.onClick({}, el({ "data-action": "servings" }));
      const loading = text(card.sheetHtml);
      await sleep(10);
      const failed = text(card.sheetHtml);
      card.optionsDown = false;
      card.sheet.onClick({}, el({ "data-action": "servings-retry" }));
      await sleep(10);
      const rows = [...card.sheetHtml.matchAll(/<button class="hf-controlrow[^"]*"[^>]*data-servings="(\\d+)"[^>]*>[\\s\\S]*?<\\/button>/g)]
        .map((m) => [Number(m[1]), /\\sdisabled[\\s>]/.test(m[0].split(">")[0] + ">"), text(m[0])]);
      card.sheet.onClick({}, el({ "data-action": "servings-pick", "data-servings": "4" }));
      const confirm = text(card.sheetHtml);
      const writtenBeforeConfirming = writes().length;
      card.sheet.onClick({}, el({ "data-action": "confirm-servings" }));
      await sleep(10);
      const offer = card.undos.at(-1);
      const changed = people(menu.render());
      await offer.run();
      // HelloFresh refuses: its own words are what the toast says.
      card.refuse = true;
      await M.changeServings(card, week, 4);
      menu.onClick({}, el({ "data-action": "servings" }));
      return { line, loading, failed, rows, confirm, writtenBeforeConfirming, changed, offer: offer.weekId,
        writes: writes(), toasts: card.toasts, reloads: card.reloads, reads: card.calls.filter(([s]) => s === "get_plan_options").length,
        lastOffers: card.undos.slice(1) };
    """
    )
    got = _run(body)
    assert got["line"] == "For 2 people Change"
    assert (
        got["loading"].startswith("People in this box Only for the box scheduled")
        and "Loading the box sizes" in got["loading"]
    )
    assert "Couldn't load the box sizes: HTTP 500 Retry" in got["failed"]
    # This week's meal count, in its own family (not Veggie's 6), fewest people first.
    assert got["rows"] == [
        [2, True, "2 people 3 meals for 2 people Current"],
        [4, False, "4 people 3 meals for 4 people $131.88"],
    ]
    assert got["confirm"].startswith("Change this box to 4 people?")
    assert (
        "Only your box for" in got["confirm"]
        and "Your plan and your other boxes stay as they are." in got["confirm"]
    )
    assert "HelloFresh lists 3 meals for 4 people at $131.88" in got["confirm"]
    assert got["writtenBeforeConfirming"] == 0
    assert got["changed"] == "For 4 people Change"
    assert got["offer"] == "2026-W41"
    assert got["writes"] == [
        {"week_id": "2026-W41", "servings": 4},
        {"week_id": "2026-W41", "servings": 2},
        {"week_id": "2026-W41", "servings": 4},
    ]
    assert got["toasts"] == [
        ["This box is now for 4 people.", False],
        ["Change undone.", False],
        [
            "Couldn't change this box: HelloFresh doesn't offer a box for that many people this week",
            True,
        ],
    ]
    assert got["reloads"] == ["2026-W41"] * 3
    # Read once after the failure; kept from then on.
    assert got["reads"] == 2
    assert got["lastOffers"] == [None, None]


def test_the_people_line_shows_only_on_a_week_that_can_still_change() -> None:
    body = (
        SERVINGS
        + """
      const head = (w, summary = card.summary) => { card.weeks = [w]; card.summary = summary; return people(menu.render()); };
      return {
        own: head(makeWeek({ servings: 4 })),
        plan: head(makeWeek(), { number_of_people: 3 }),
        unknown: head(makeWeek(), null),
        locked: head(makeWeek({ servings: 2, selection_deadline: inDays(-1).toISOString() })),
        skipped: head(makeWeek({ servings: 2, is_skipped: true })),
        market: (() => { card.weeks = [makeWeek({ servings: 2 })]; return /hf-weekpeople/.test(new M.MarketView(card).render()); })(),
        // The deadline passed while the sheet was open: refused with the reason, nothing written.
        closed: await (async () => {
          await M.changeServings(card, makeWeek({ servings: 2, selection_deadline: inDays(-0.01).toISOString() }), 4);
          return [writes().length, card.toasts.at(-1)];
        })(),
      };
    """
    )
    assert _run(body) == {
        "own": "For 4 people Change",
        "plan": "For 3 people Change",
        "unknown": "",
        "locked": "",
        "skipped": "",
        "market": False,
        "closed": [
            0,
            ["The deadline for this week has passed, so it can no longer be changed.", True],
        ],
    }


# ---- Week tab --------------------------------------------------------------------------------

PLAN_WEEK = """
  // The box being eaten now: delivered today, Meals 1-3 in it, a Gyoza appetizer bought.
  const week = makeWeek({ delivery_date: iso(new Date()), selection_deadline: inDays(-4).toISOString(),
    allowed_actions: { mealSwap: false } });
  week.market_items = [{ item_id: "a1", name: "Gyoza", group_type: "appetizer", is_selected: true, selected_quantity: 1 }];
  const card = makeCard([week], { view: "week", mealPlan: { days: {}, meals: { dinner: { start: "17:30" } } } });
  card.call = async (service, data) => {
    card.calls.push([service, JSON.parse(JSON.stringify(data))]);
    await sleep(20);
    if (card.failCourse && data.course === card.failCourse) throw new Error("HTTP 500");
    return { days: {}, meals: { dinner: { start: "17:30" } } };
  };
  const view = new W.WeekView(card);
  card.viewObj = view;
  const day = L.planDays(week)[1];
"""


def test_a_note_typed_from_the_keyboard_is_chosen_and_saved() -> None:
    """Tab into "Something else", type, Tab to Save, Enter: nobody clicked the note's radio,
    and the note is still what gets saved (it used to be dropped without a word)."""
    body = (
        PLAN_WEEK
        + """
      view._openSlot(day, "dinner");
      const input = { value: "Pizza night", closest: (sel) => (sel === "[data-note-input]" ? input : null) };
      card.shadowRoot = { querySelector: (sel) => (sel === "[data-note-input]" ? input : null), activeElement: null };
      card.sheet.onInput({ target: input });
      const sheet = card.sheet.render();
      card.sheet.onClick({}, el({ "data-action": "save-slot" }));
      await sleep(60);
      const note = (sheet.match(/<div class="hf-optrow hf-planote[^"]*"[\\s\\S]*?<\\/div>/) || [""])[0];
      return {
        rowOn: /hf-planote on/.test(note),
        radio: (note.match(/<button class="hf-planotepick"[^>]*>/) || [""])[0],
        calls: card.calls, closed: card.closed, toasts: card.toasts,
      };
    """
    )
    got = _run(body)
    assert got["rowOn"] and 'aria-pressed="true"' in got["radio"]
    assert got["calls"][0][0] == "set_planned_meal" and len(got["calls"]) == 1
    data = got["calls"][0][1]
    assert data["text"] == "Pizza night" and data["meal"] == "dinner" and data["course"] == "main"
    assert got["closed"] == 1 and got["toasts"] == [["Plan saved", False]]


def test_a_plan_save_goes_out_once_and_a_partial_one_says_what_was_kept() -> None:
    body = (
        PLAN_WEEK
        + """
      view._openSlot(day, "dinner");
      card.sheet.onClick({}, el({ "data-action": "pick", "data-course": "main", "data-kind": "meal", "data-id": "r1" }));
      card.sheet.onClick({}, el({ "data-action": "save-slot" }));
      const whileSaving = card.sheetHtml;
      card.sheet.onClick({}, el({ "data-action": "save-slot" }));   // a double tap
      await sleep(60);
      const once = card.calls.map(([s, d]) => `${s}:${d.recipe_id || ""}`);

      card.calls = [];
      card.toasts = [];
      card.failCourse = "appetizer";
      view._openSlot(L.planDays(week)[2], "dinner");
      card.sheet.onClick({}, el({ "data-action": "pick", "data-course": "main", "data-kind": "meal", "data-id": "r2" }));
      card.sheet.onClick({}, el({ "data-action": "pick", "data-course": "appetizer", "data-kind": "market", "data-id": "a1" }));
      card.sheet.onClick({}, el({ "data-action": "save-slot" }));
      await sleep(80);
      return {
        disabled: /data-action="save-slot"[^>]*disabled/.test(whileSaving) && /data-action="pick"[^>]*disabled/.test(whileSaving),
        once, partial: card.calls.map(([, d]) => d.course), toast: card.toasts.at(-1), open: Boolean(card.sheet),
        reenabled: !/data-action="save-slot"[^>]*disabled/.test(card.sheetHtml),
      };
    """
    )
    got = _run(body)
    assert got["disabled"]
    assert got["once"] == ["set_planned_meal:r1"]
    assert got["partial"] == ["main", "appetizer"]
    assert got["toast"] == [
        "Only part of the plan was saved. Saved: Main. Not saved: Appetizer (HTTP 500).",
        True,
    ]
    assert got["open"] and got["reenabled"]  # the sheet stays, ready to try the rest again


def test_a_save_closes_only_the_sheet_it_was_made_in() -> None:
    body = (
        PLAN_WEEK
        + """
      view._openSlot(day, "dinner");
      card.sheet.onClick({}, el({ "data-action": "pick", "data-course": "main", "data-kind": "meal", "data-id": "r1" }));
      card.sheet.onClick({}, el({ "data-action": "save-slot" }));
      card.closeSheet();                                  // Cancel while it saves
      view._openSlot(L.planDays(week)[3], "dinner");      // and open another day
      const other = card.sheet;
      const drawnBusy = /data-action="save-slot"[^>]*disabled/.test(card.sheetHtml);
      await sleep(60);
      return { stillOpen: card.sheet === other && other !== null, toasts: card.toasts, drawnBusy,
        freed: !/data-action="save-slot"[^>]*disabled/.test(card.sheetHtml) };
    """
    )
    got = _run(body)
    assert got["stillOpen"] and got["toasts"] == [["Plan saved", False]]
    # Opened while the first save was out, it was drawn with its buttons off; they come back.
    assert got["drawnBusy"] and got["freed"]


def test_week_times_are_home_assistants_whatever_the_browsers_zone() -> None:
    """Home Assistant in New York, the browser in Los Angeles: a dinner at 6 PM reads 6 PM, and
    the time field holds 18:00 (what the integration reads back as New York time)."""
    body = (
        PLAN_WEEK
        + """
      L.I18n.useHass({ language: "en", config: { time_zone: "America/New_York" } });
      const offset = (date) => new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", timeZoneName: "longOffset" })
        .formatToParts(date).find((p) => p.type === "timeZoneName").value.replace("GMT", "") || "+00:00";
      const zone = offset(L.parseLocalDate(day));
      card.mealPlan = { meals: { dinner: { start: "17:30" } }, days: { [day]: { dinner: {
        main: { kind: "meal", id: "r1", week_id: week.week_id, name: "Meal 1" },
        start: `${day}T18:00:00${zone}`, end: `${day}T19:30:00${zone}` } } } };
      const line = text((view.render().match(new RegExp(`<div class="hf-planline"[^>]*data-day="${day}"[\\\\s\\\\S]*?</div>`)) || [""])[0]);
      view._openSlot(day, "dinner");
      const field = (card.sheetHtml.match(/<input class="hf-planinput" type="time"[^>]*value="([^"]*)"/) || [])[1];
      return { line, field };
    """
    )
    got = _run(body, tz="America/Los_Angeles")
    assert "6:00 PM–7:30 PM" in got["line"], got["line"]
    assert got["field"] == "18:00"


# ---- Overview ---------------------------------------------------------------------------------


def test_the_hero_never_calls_a_delivered_box_arriving() -> None:
    body = """
      const today = makeWeek({ delivery_date: iso(new Date()), delivered_at: new Date(Date.now() - 3600e3).toISOString(),
        status: "DELIVERED", selection_deadline: inDays(-4).toISOString(), allowed_actions: { mealSwap: false } });
      const early = makeWeek({ week_id: "2026-W42", delivery_date: iso(inDays(1)), delivered_at: new Date().toISOString(),
        status: "DELIVERED", selection_deadline: inDays(-3).toISOString(), allowed_actions: { mealSwap: false } });
      const card = makeCard([today], { view: "overview" });
      const view = new O.OverviewView(card);
      const eyebrow = (html) => text((html.match(/<span class="hf-eyebrow">([^<]*)<\\/span>/) || [, ""])[1]);
      return [eyebrow(view._hero(today, true)), eyebrow(view._hero(early, true))];
    """
    assert _run(body) == ["Last box", "Last box"]


def test_a_box_helloFresh_moved_says_when_it_now_arrives() -> None:
    """The next box (a notice), a week card and a row (a short line), and the Menu's week
    header; dates read as local days, never shifted by the browser's zone."""
    body = """
      const next = makeWeek({ week_id: "W1", sub_status: "DELAYED", revised_delivery_date: iso(inDays(7)) });
      const later = makeWeek({ week_id: "W2", delivery_date: iso(inDays(13)), selection_deadline: inDays(10).toISOString(),
        sub_status: "EARLY", revised_delivery_date: iso(inDays(12)) });
      const card = makeCard([next, later], { view: "overview" });
      const view = new O.OverviewView(card);
      const hero = view._hero(next, true);
      return {
        hero: text(hero), heroTone: /hf-notice tone-warn hf-revisednotice/.test(hero),
        card: text(view._weekCard(later)), row: text(view._row(later)), head: text(new M.MealsView(card).render()),
        plain: /now arriving/.test(view._weekCard(makeWeek({ week_id: "W3" })) + view._hero(makeWeek({ week_id: "W3" }), true)),
        long: L.fmtLongDate(iso(inDays(7))), short: L.fmtDate(iso(inDays(12))),
      };
    """
    got = _run(body, tz="Pacific/Honolulu")
    assert f"Delayed — now arriving {got['long']}" in got["hero"] and got["heroTone"]
    assert f"Early — now arriving {got['short']}" in got["card"]
    assert f"Early — now arriving {got['short']}" in got["row"]
    assert f"Delayed — now arriving {got['long']}" in got["head"]
    assert not got["plain"]


def test_the_payment_banner_names_the_boxes_and_links_to_helloFresh() -> None:
    body = """
      const problem = L.paymentProblem({ payment_problem: true, payment_problem_weeks: [
        { week_id: "W40", delivery_date: "2026-09-28", status: "payment_failed", amount: 84.93, currency: "USD" },
        { week_id: "W39", delivery_date: "2026-09-21", status: "chargeback", amount: null, currency: null },
      ] }, []);
      const two = U.paymentBanner(problem, "https://www.hellofresh.com");
      return {
        two: text(two), link: (two.match(/<a [^>]*>/) || [""])[0], live: /role=/.test(two),
        one: text(U.paymentBanner({ boxes: problem.boxes.slice(0, 1) }, "")),
        any: text(U.paymentBanner({ boxes: [] }, "")), anyLink: /<a /.test(U.paymentBanner({ boxes: [] }, "")),
        unsafe: ["javascript:alert(1)", "http://www.hellofresh.com"].map((site) => /<a /.test(U.paymentBanner({ boxes: [] }, site))),
        none: U.paymentBanner(null, "https://www.hellofresh.com"),
        hostile: U.paymentBanner({ boxes: [{ date: "<img src=x onerror=alert(1)>", amount: "" }] }, ""),
      };
    """
    got = _run(body)
    assert got["two"] == (
        "There's a problem with the payments for 2 boxes: Monday, September 28 ($84.93) and Monday, "
        "September 21. Update your payment details on HelloFresh"
    )
    assert 'href="https://www.hellofresh.com"' in got["link"]
    assert 'target="_blank"' in got["link"] and 'rel="noopener noreferrer"' in got["link"]
    assert not got["live"]  # part of the section, not news to read out on every visit
    assert got["one"] == (
        "There's a problem with the payment for your box of Monday, September 28 ($84.93). "
        "Update your payment details on HelloFresh"
    )
    assert (
        got["any"]
        == "There's a problem with a payment on your HelloFresh account. Update your payment details on HelloFresh"
    )
    assert not got["anyLink"]  # no site known: the words alone
    assert got["unsafe"] == [False, False]  # https only
    assert got["none"] == ""
    assert "<img" not in got["hostile"] and "&lt;img" in got["hostile"]


def test_delivery_photos_come_only_from_https_and_send_no_referrer() -> None:
    body = """
      const week = makeWeek({ delivery_date: iso(inDays(-2)), delivered_at: inDays(-2).toISOString(), status: "DELIVERED",
        selection_deadline: inDays(-6).toISOString(), allowed_actions: { mealSwap: false } });
      week.order = { currency: "USD", carrier: "Veho", delivery_photo_urls: ["http://carrier.example/plain.jpg", "https://carrier.example/door.jpg"] };
      const card = makeCard([week], { view: "overview" });
      const view = new O.OverviewView(card);
      const sheet = view._renderDelivery(week);
      return [...sheet.matchAll(/<img [^>]*>/g)].map((m) => m[0]).filter((img) => img.includes("carrier.example"));
    """
    imgs = _run(body)
    assert len(imgs) == 1
    assert (
        'src="https://carrier.example/door.jpg"' in imgs[0]
        and 'referrerpolicy="no-referrer"' in imgs[0]
    )


def test_skip_once_the_deadline_has_passed_is_refused_with_a_reason() -> None:
    """The minute tick doesn't re-render for a skipped week's deadline, so Unskip can still be on
    screen after it: a click then says why instead of sending HelloFresh a change it refuses."""
    body = """
      const week = makeWeek({ is_skipped: true, status: "SKIPPED", selection_deadline: inDays(-0.01).toISOString() });
      const card = makeCard([week], { view: "overview" });
      const overview = new O.OverviewView(card);
      overview.onClick({}, el({ "data-action": "skip", "data-week-id": week.week_id }));
      const menu = new M.MealsView(card);
      menu.onClick({}, el({ "data-action": "skip" }));
      return { asked: card.skipAsked || null, toasts: card.toasts, renders: card.renders };
    """
    got = _run(body)
    assert got["asked"] is None and got["renders"] == 2
    assert [t[1] for t in got["toasts"]] == [True, True]
    assert "deadline for this week has passed" in got["toasts"][0][0]


# ---- accessibility markup ---------------------------------------------------------------------


def test_controls_are_never_nested_and_colour_is_never_the_only_signal() -> None:
    """Tiles, week cards, rows and plan lines are plain containers whose one "open" control is
    a real button beside the others; the protein and unsaved-change dots say what they mean."""
    body = """
      const week = makeWeek();
      const card = makeCard([week]);
      const menu = new M.MealsView(card);
      card.box.addMeal(week, week.recipes[3]);
      const grid = menu.render();
      const later = makeWeek({ week_id: "2026-W42", delivery_date: iso(inDays(13)), selection_deadline: inDays(10).toISOString() });
      card.weeks = [week, later];
      const overview = new O.OverviewView(card);
      const weekCard = overview._weekCard(later);
      const row = overview._row(later);
      const roleButtons = [grid, weekCard, row].join("").match(/<[^>]*role="button"[^>]*>/g) || [];
      return {
        roleButtons,
        tile: (grid.match(/<div class="hf-tile[^>]*>/) || [""])[0],
        opens: (grid.match(/<button class="hf-open"/g) || []).length,
        dot: (grid.match(/<span class="hf-pdot"[^>]*>/) || [""])[0],
        dirty: (grid.match(/<span class="hf-wcdirty"[^>]*>/) || [""])[0],
        weekCard: (weekCard.match(/<div class="hf-weekcard[^>]*>/) || [""])[0], weekCardOpen: /<button class="hf-open"/.test(weekCard),
        row: (row.match(/<div class="hf-row[^>]*>/) || [""])[0], rowOpen: /<button class="hf-open"/.test(row),
      };
    """
    got = _run(body)
    assert got["roleButtons"] == []
    assert "role=" not in got["tile"] and "tabindex" not in got["tile"] and got["opens"] == 5
    assert 'role="img"' in got["dot"] and 'aria-label="Beef"' in got["dot"]
    assert 'role="img"' in got["dirty"] and 'aria-label="Unsaved changes"' in got["dirty"]
    assert "role=" not in got["weekCard"] and got["weekCardOpen"]
    assert "role=" not in got["row"] and got["rowOpen"]


def test_account_sections_are_a_tablist_moved_by_the_arrow_keys() -> None:
    body = """
      const card = makeCard([], { view: "account", summary: { selected_plan: "Classic" } });
      const view = new A.AccountView(card);
      card.viewObj = view;
      const tabs = (html) => [...html.matchAll(/<button role="tab"[^>]*>/g)].map((m) => m[0]);
      const before = tabs(view.render());
      const keys = [];
      for (const key of ["ArrowRight", "ArrowRight", "ArrowRight", "ArrowLeft", "End", "Home", "x"]) {
        let prevented = false;
        view._onTabKey({ key, preventDefault: () => { prevented = true; } });
        keys.push([view.tab, prevented]);
      }
      return { before, keys, panel: /role="tabpanel"[^>]*aria-labelledby="hf-acct-tab-plan"/.test(view.render()) };
    """
    got = _run(body)
    assert len(got["before"]) == 3
    assert all("aria-pressed" not in tab for tab in got["before"])
    assert 'aria-selected="true"' in got["before"][0] and 'tabindex="0"' in got["before"][0]
    assert all(
        'aria-selected="false"' in tab and 'tabindex="-1"' in tab for tab in got["before"][1:]
    )
    assert got["keys"] == [
        ["profile", True],
        ["spending", True],
        ["plan", True],  # wraps at the end
        ["spending", True],
        ["spending", True],
        ["plan", True],
        ["plan", False],  # any other key is left alone
    ]
    assert got["panel"]
