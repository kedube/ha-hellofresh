"""Tests for the meal-planner card's server-resolved filter groups: Cuisine type, Dish type
and Ingredients to avoid.

Unlike the tag-matched groups (protein, dietary, cooking time, highlights), these three are
answered by HelloFresh's own filter service through `hellofresh.get_menu_courses`: menu
recipes carry no allergen data and their tag slugs don't match the site's cuisine/dish-type
option slugs. Semantics guarded here: the groups are built from each week's `menu_filters`
(and absent without it); ONE request carries every active group, shaped `{week_id, filters}`
with arrays of option slugs; the returned id-set is intersected with the client-side filters
while selected meals stay visible; responses are cached per week + filters and a stale
response can never clear or re-render over a newer request; a stored slug the viewed week
doesn't offer is ignored; and a failed lookup falls back to client-side filtering rather
than blanking the grid.

The behavioural tests run the real method bodies against stubs under Node.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

WWW = Path(__file__).resolve().parents[1] / "custom_components" / "hellofresh" / "www"
SOURCE = (WWW / "hellofresh-meal-planner-card.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

nodejs = pytest.mark.skipif(NODE is None, reason="node is not installed")

GROUPS = ["cuisine", "dish-type", "exclude-allergens"]

# A week's menu_filters as the integration serializes them from the menu payload: the
# server-resolved groups deliberately out of panel order, beside groups the card matches
# client-side (main-protein, total-cooking-time) which must NOT become server chips.
WEEK = {
    "week_id": "2026-W37",
    "menu_filters": [
        {
            "name": "Main protein",
            "slug": "main-protein",
            "choice": "MULTI-OR",
            "options": [{"name": "Beef", "slug": "beef", "default": False}],
        },
        {
            "name": "Dish type",
            "slug": "dish-type",
            "choice": "MULTI-OR",
            "options": [
                {"name": "Bowls", "slug": "salad-and-bowls", "default": False},
                {"name": "Handhelds", "slug": "burgers-and-sandwiches", "default": False},
            ],
        },
        {
            "name": "Cuisine type",
            "slug": "cuisine",
            "choice": "MULTI-OR",
            "options": [
                {"name": "Italian", "slug": "classic-euro-dishes", "default": False},
                {"name": "Mediterranean", "slug": "mediterranean", "default": False},
                {"name": "Global", "slug": "world-flavors", "default": False},
            ],
        },
        {
            "name": "Ingredients to avoid",
            "slug": "exclude-allergens",
            "choice": "MULTI-AND",
            "options": [
                {"name": "Milk", "slug": "milk", "default": False},
                {"name": "Nuts", "slug": "nuts", "default": False},
            ],
        },
        {
            "name": "Total cooking time",
            "slug": "total-cooking-time",
            "choice": "SINGLE",
            "options": [{"name": "Under 15 Minutes", "slug": "under-15-minutes"}],
        },
    ],
}


def _method(name: str) -> str:
    match = re.search(rf"^  {name}\((?:\w+(?:, \w+)*)?\) \{{.*?^  \}}", SOURCE, re.S | re.M)
    assert match, f"{name} not found"
    return match.group(0)


def _async_method(name: str) -> str:
    match = re.search(rf"^  async {name}\((?:\w+(?:, \w+)*)?\) \{{.*?^  \}}", SOURCE, re.S | re.M)
    assert match, f"async {name} not found"
    return match.group(0)


def _statics_literal(*names: str) -> str:
    out = []
    for name in names:
        match = re.search(rf"^  static get {name}\(\) \{{.*?^  \}}", SOURCE, re.S | re.M)
        assert match, f"{name} not found"
        body = match.group(0).split("{", 1)[1].rsplit("}", 1)[0]
        out.append(f"{name}: (function() {{ {body} }})()")
    return ", ".join(out)


SERVER_METHODS = (
    "_loadServerFilter",
    "_persistServerFilter",
    "_toggleServerFilter",
    "_clearServerFilter",
    "_serverFilterGroups",
    "_activeServerFilters",
    "_serverFilterKey",
    "_serverFilterIds",
    "_renderServerFilterGroups",
    "_filterRow",
)

# The harness: real statics, an in-memory localStorage, a service stub whose promises the
# test body settles by hand (so response ORDER is under test control), and counters for
# renders and console.warn calls. `__BODY__` runs inside an async function with `out`
# collected and printed as JSON.
HARNESS = """
const HelloFreshMealPlannerCard = { __STATICS__ };
const store = __STORE__;
const window = {
  localStorage: {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
  },
};
const calls = [];
const deferred = [];
const warnings = [];
console.warn = (...args) => warnings.push(String(args[0]));
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
const self = {
  _serverFilterCache: new Map(),
  _serverFilterPending: null,
  _serverFilterSeq: 0,
  _renders: 0,
  _render() { this._renders += 1; },
  _esc: (v) => String(v),
  _callResponseService(service, data) {
    calls.push({ service, data: JSON.parse(JSON.stringify(data)) });
    return new Promise((resolve, reject) => deferred.push({ resolve, reject }));
  },
  __METHODS__
};
self._serverFilter = Object.fromEntries(
  HelloFreshMealPlannerCard.SERVER_FILTER_GROUPS.map((g) => [g, new Set((__STATE__)[g] || [])])
);
const WEEK = __WEEK__;
const out = {};
(async () => {
  __BODY__
  console.log(JSON.stringify(out));
})().catch((err) => { console.error(err && err.stack || err); process.exit(1); });
"""


def _run(body: str, state: dict | None = None, store: dict | None = None) -> dict:
    methods = ",\n  ".join(_method(m) for m in SERVER_METHODS)
    methods += ",\n  " + _async_method("_fetchMenuCourses")
    script = (
        HARNESS.replace(
            "__STATICS__",
            _statics_literal("SERVER_FILTER_GROUPS", "SERVER_FILTER_STORAGE_KEY"),
        )
        .replace("__STORE__", json.dumps(store or {}))
        .replace("__METHODS__", methods)
        .replace("__STATE__", json.dumps(state or {}))
        .replace("__WEEK__", json.dumps(WEEK))
        .replace("__BODY__", body)
    )
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# ---- group construction from menu_filters -------------------------------------------------


@nodejs
def test_groups_come_from_menu_filters_in_panel_order_with_the_apis_option_order() -> None:
    """Panel order is the card's (cuisine, dish type, ingredients to avoid) regardless of the
    payload's order; the options inside each group keep the API's order and names."""
    out = _run(
        "out.groups = self._serverFilterGroups(WEEK)"
        ".map((g) => [g.name, g.options.map((o) => o.name)]);"
    )
    assert out["groups"] == [
        ["Cuisine type", ["Italian", "Mediterranean", "Global"]],
        ["Dish type", ["Bowls", "Handhelds"]],
        ["Ingredients to avoid", ["Milk", "Nuts"]],
    ]


@nodejs
def test_groups_are_hidden_on_weeks_without_menu_filters() -> None:
    """History weeks carry no menu payload — `menu_filters` missing or [] — and a week that
    declares only client-side groups yields none of the three either."""
    out = _run(
        """
        const weeks = [
          { week_id: "w" },
          { week_id: "w", menu_filters: [] },
          { week_id: "w", menu_filters: null },
          { week_id: "w", menu_filters: [WEEK.menu_filters[0], WEEK.menu_filters[4]] },
          { week_id: "w", menu_filters: [{ name: "Cuisine type", slug: "cuisine", options: [] }] },
        ];
        out.groups = weeks.map((w) => self._serverFilterGroups(w).length);
        out.html = weeks.map((w) => self._renderServerFilterGroups(w));
        """
    )
    assert out["groups"] == [0, 0, 0, 0, 0]
    assert out["html"] == ["", "", "", "", ""]


@nodejs
def test_group_rows_render_slug_chips_with_an_all_chip() -> None:
    out = _run(
        "out.html = self._renderServerFilterGroups(WEEK);",
        state={"cuisine": ["mediterranean"]},
    )
    html = out["html"]
    assert html.count('data-action="filter-server-all"') == 3
    for label in ("Cuisine type", "Dish type", "Ingredients to avoid"):
        assert f'<span class="flabel">{label}</span>' in html
    assert 'data-slug="classic-euro-dishes"' in html
    assert ">Italian</button>" in html
    assert 'data-slug="beef"' not in html, "main protein is a client-side group"
    assert re.search(r'data-slug="mediterranean"\s+aria-pressed="true"', html)
    assert re.search(r'data-slug="world-flavors"\s+aria-pressed="false"', html)
    # The All chip is on only for a group with nothing selected.
    assert re.search(r'filter-server-all"\s+data-group="cuisine"\s+aria-pressed="false"', html)
    assert re.search(r'filter-server-all"\s+data-group="dish-type"\s+aria-pressed="true"', html)


# ---- the service call -----------------------------------------------------------------------


@nodejs
def test_one_request_carries_week_id_and_only_the_active_groups() -> None:
    """Two active groups → ONE get_menu_courses call, `filters` holding only those groups as
    arrays of option slugs in the week's option order; the empty group is omitted. A second
    lookup while it is in flight issues nothing, and the answer is cached afterwards."""
    out = _run(
        """
        out.first = self._serverFilterIds(WEEK);
        out.again = self._serverFilterIds(WEEK);
        out.calls = calls.slice();
        out.pending = self._serverFilterPending;
        deferred[0].resolve({ week_id: "2026-W37", recipe_ids: ["aaa", "bbb-suffix"], count: 2 });
        await tick();
        out.ids = [...self._serverFilterIds(WEEK)];
        out.callsAfter = calls.length;
        out.renders = self._renders;
        out.pendingAfter = self._serverFilterPending;
        """,
        state={
            "cuisine": ["world-flavors", "mediterranean"],  # stored out of the API's order
            "dish-type": ["salad-and-bowls"],
            "exclude-allergens": [],
        },
    )
    assert out["first"] is None and out["again"] is None, "grid must not blank while in flight"
    assert out["calls"] == [
        {
            "service": "get_menu_courses",
            "data": {
                "week_id": "2026-W37",
                "filters": {
                    "cuisine": ["mediterranean", "world-flavors"],
                    "dish-type": ["salad-and-bowls"],
                },
            },
        }
    ]
    assert list(out["calls"][0]["data"]["filters"]) == ["cuisine", "dish-type"]
    assert out["pending"] == (
        '2026-W37|{"cuisine":["mediterranean","world-flavors"],"dish-type":["salad-and-bowls"]}'
    )
    assert out["ids"] == ["aaa", "bbb"]  # bare ids, as the section filter normalizes them
    assert out["callsAfter"] == 1
    assert out["renders"] == 1
    assert out["pendingAfter"] is None


@nodejs
def test_nothing_active_means_no_request_and_no_constraint() -> None:
    out = _run(
        "out.ids = self._serverFilterIds(WEEK); out.calls = calls.length;"
        " out.active = self._activeServerFilters(WEEK);"
    )
    assert out == {"ids": None, "calls": 0, "active": {}}


# ---- intersection with the client-side filters --------------------------------------------


def _grid_ids(server_ids: list[str] | None) -> list[str]:
    """Run the real _renderGrid + _passesFilters with a Beef protein filter, meal `d`
    selected, and the server set stubbed to `server_ids` (null = no server constraint)."""
    recipes = [
        {"recipe_id": "a", "name": "A", "preference": "Beef", "course_index": 0},
        {"recipe_id": "b", "name": "B", "preference": "Beef", "course_index": 1},
        {"recipe_id": "c", "name": "C", "preference": "Pork", "course_index": 2},
        {"recipe_id": "d", "name": "D", "preference": "Pork", "course_index": 3},
    ]
    server = "null" if server_ids is None else f"new Set({json.dumps(server_ids)})"
    body = f"""
    Object.assign(self, {{
      _showSelectedOnly: false,
      _proteinFilter: new Set(["Beef"]),
      _showVariants: true,
      _busy: false,
      _dedupedFor: (w) => ({{ recipes: w.recipes }}),
      _tileContext: () => ({{ sel: (r) => r.recipe_id === "d" }}),
      _filtersApply: () => true,
      _activeMenuSectionIds: () => null,
      _serverFilterIds: () => {server},
      _passesDietFilters: () => true,
      _passesTimeFilter: () => true,
      _passesHighlightFilter: () => true,
      _isDefaultMeal: () => true,
      _isPast: () => false,
      _isPaused: () => false,
      {_method("_renderGrid")},
      {_method("_passesFilters")},
      _renderRecipeTile: (w, r) => `<i>${{r.recipe_id}}</i>`,
    }});
    const html = self._renderGrid({{ week_id: "w", recipes: {json.dumps(recipes)} }});
    out.ids = [...html.matchAll(/<i>(\\w+)<\\/i>/g)].map((m) => m[1]);
    out.html = html;
    """
    return _run(body)["ids"]


@nodejs
def test_grid_intersects_the_server_set_with_client_side_filters() -> None:
    """`a` is Beef but not in the server set; `c` is in the server set but Pork: both must
    go. `b` passes both. `d` is selected, so it stays regardless of either filter."""
    assert _grid_ids(["b", "c", "d"]) == ["d", "b"]


@nodejs
def test_grid_without_a_server_constraint_is_the_client_side_result() -> None:
    """null (nothing active, in flight, or failed) means the existing grid, unchanged."""
    assert _grid_ids(None) == ["d", "a", "b"]


@nodejs
def test_grid_keeps_selected_meals_when_the_server_matches_nothing() -> None:
    assert _grid_ids([]) == ["d"]


# ---- stale-response guard ----------------------------------------------------------------


@nodejs
def test_an_older_response_landing_last_never_clears_or_rerenders_the_newer_one() -> None:
    out = _run(
        """
        self._serverFilter.cuisine = new Set(["mediterranean"]);
        self._serverFilterIds(WEEK); // request 1
        self._serverFilter.cuisine = new Set(["world-flavors"]);
        self._serverFilterIds(WEEK); // request 2 (newer)
        out.calls = calls.length;
        deferred[1].resolve({ recipe_ids: ["new1"] });
        await tick();
        const snap = () => ({
          pending: self._serverFilterPending,
          renders: self._renders,
          ids: [...(self._serverFilterIds(WEEK) || ["<none>"])],
        });
        out.afterNewer = snap();
        deferred[0].resolve({ recipe_ids: ["old1"] }); // the slow older response
        await tick();
        out.afterOlder = snap();
        """
    )
    assert out["calls"] == 2
    assert out["afterNewer"] == {"pending": None, "renders": 1, "ids": ["new1"]}
    assert out["afterOlder"] == {"pending": None, "renders": 1, "ids": ["new1"]}


@nodejs
def test_an_older_response_landing_first_keeps_the_newer_request_pending() -> None:
    out = _run(
        """
        self._serverFilter.cuisine = new Set(["mediterranean"]);
        self._serverFilterIds(WEEK); // request 1
        self._serverFilter.cuisine = new Set(["world-flavors"]);
        self._serverFilterIds(WEEK); // request 2 (newer)
        const newerKey = self._serverFilterPending;
        deferred[0].resolve({ recipe_ids: ["old1"] });
        await tick();
        out.stillPending = self._serverFilterPending === newerKey;
        out.renders = self._renders;
        out.ids = self._serverFilterIds(WEEK);
        out.calls = calls.length; // no duplicate request for the still-pending key
        deferred[1].resolve({ recipe_ids: ["new1"] });
        await tick();
        out.final = { pending: self._serverFilterPending, renders: self._renders,
                      ids: [...self._serverFilterIds(WEEK)] };
        """
    )
    assert out["stillPending"] is True
    assert out["renders"] == 0
    assert out["ids"] is None
    assert out["calls"] == 2
    assert out["final"] == {"pending": None, "renders": 1, "ids": ["new1"]}


# ---- persistence ----------------------------------------------------------------------------


@nodejs
def test_a_stored_slug_the_week_does_not_offer_is_ignored() -> None:
    """A slug persisted from another week (or renamed by HelloFresh) must neither reach the
    service nor count as active; a stored group the card doesn't know is dropped on load."""
    out = _run(
        """
        self._serverFilter = self._loadServerFilter();
        out.loaded = Object.fromEntries(
          Object.entries(self._serverFilter).map(([g, s]) => [g, [...s]])
        );
        out.active = self._activeServerFilters(WEEK);
        out.ids = self._serverFilterIds(WEEK);
        out.payload = calls[0].data.filters;
        const bare = { week_id: "w", menu_filters: [
          { name: "Cuisine type", slug: "cuisine",
            options: [{ name: "Italian", slug: "classic-euro-dishes" }] },
        ] };
        out.activeElsewhere = self._activeServerFilters(bare);
        out.idsElsewhere = self._serverFilterIds(bare);
        out.calls = calls.length;
        out.pressed = (self._renderServerFilterGroups(WEEK).match(/aria-pressed="true"/g) || []).length;
        """,
        store={
            "hellofresh-meal-planner:server-filter": json.dumps(
                {
                    "cuisine": ["mediterranean", "gone-cuisine"],
                    "dish-type": ["salad-and-bowls"],
                    "exclude-allergens": "not-a-list",
                    "bogus-group": ["x"],
                }
            )
        },
    )
    assert out["loaded"] == {
        "cuisine": ["mediterranean", "gone-cuisine"],
        "dish-type": ["salad-and-bowls"],
        "exclude-allergens": [],
    }
    assert out["active"] == {"cuisine": ["mediterranean"], "dish-type": ["salad-and-bowls"]}
    assert out["payload"] == {"cuisine": ["mediterranean"], "dish-type": ["salad-and-bowls"]}
    assert out["activeElsewhere"] == {}
    assert out["idsElsewhere"] is None
    assert out["calls"] == 1, "a week the stored slugs don't apply to must not be queried"
    # Two selected chips render pressed (mediterranean, salad-and-bowls) and the All chip of
    # the untouched allergens group: the stale slug has no chip to press.
    assert out["pressed"] == 3


@nodejs
def test_a_corrupt_store_loads_as_nothing_selected() -> None:
    out = _run(
        "self._serverFilter = self._loadServerFilter();"
        " out.sizes = Object.values(self._serverFilter).map((s) => s.size);",
        store={"hellofresh-meal-planner:server-filter": "{not json"},
    )
    assert out["sizes"] == [0, 0, 0]


@nodejs
def test_toggle_and_clear_persist_one_json_object_and_rerender() -> None:
    out = _run(
        """
        const key = HelloFreshMealPlannerCard.SERVER_FILTER_STORAGE_KEY;
        self._toggleServerFilter("cuisine", "mediterranean");
        self._toggleServerFilter("exclude-allergens", "nuts");
        self._toggleServerFilter("bogus", "x"); // unknown group: ignored, no render
        self._toggleServerFilter("cuisine", ""); // empty slug: ignored
        out.stored = JSON.parse(store[key]);
        out.renders = self._renders;
        self._toggleServerFilter("exclude-allergens", "nuts"); // second tap removes
        self._clearServerFilter("cuisine");
        self._clearServerFilter("cuisine"); // already empty: no render
        out.cleared = JSON.parse(store[key]);
        out.rendersAfter = self._renders;
        """
    )
    assert out["stored"] == {
        "cuisine": ["mediterranean"],
        "dish-type": [],
        "exclude-allergens": ["nuts"],
    }
    assert out["renders"] == 2
    assert out["cleared"] == {"cuisine": [], "dish-type": [], "exclude-allergens": []}
    assert out["rendersAfter"] == 4


# ---- error fallback ------------------------------------------------------------------------


@nodejs
def test_a_failed_lookup_warns_once_and_falls_back_without_refetching() -> None:
    """The fallback is `null` — no server constraint — so the grid shows the client-side
    result instead of blanking; it is cached so re-rendering doesn't hammer a failing
    service (a refresh clears the cache and retries)."""
    out = _run(
        """
        self._serverFilterIds(WEEK);
        deferred[0].reject(new Error("boom"));
        await tick();
        out.warnings = warnings;
        out.ids = self._serverFilterIds(WEEK);
        self._serverFilterIds(WEEK);
        out.calls = calls.length;
        out.pending = self._serverFilterPending;
        out.renders = self._renders;
        """,
        state={"exclude-allergens": ["nuts"]},
    )
    assert len(out["warnings"]) == 1
    assert "hellofresh" in out["warnings"][0]
    assert out["ids"] is None
    assert out["calls"] == 1
    assert out["pending"] is None
    assert out["renders"] == 1


# ---- summary chips + wiring ------------------------------------------------------------------


@nodejs
def test_active_summary_lists_server_chips_under_their_group_and_remove_routes_back() -> None:
    body = f"""
    Object.assign(HelloFreshMealPlannerCard, {{ {
        _statics_literal("PROTEIN_FILTERS", "DIET_FILTERS", "TIME_FILTERS", "HIGHLIGHT_FILTERS")
    } }});
    const removed = [];
    Object.assign(self, {{
      _proteinFilter: new Set(), _dietFilter: new Set(), _timeFilter: "", _highlightFilter: "",
      _menuSection: "", _showVariants: true,
      _activeMenuSectionIds: () => null,
      _toggleServerFilter: (g, s) => removed.push([g, s]),
      {_method("_activeFilterSummary")},
      {_method("_removeFilter")},
    }});
    out.rows = self._activeFilterSummary(WEEK);
    out.rowsElsewhere = self._activeFilterSummary({{ week_id: "w" }});
    for (const r of out.rows) self._removeFilter(r.kind, r.value);
    out.removed = removed;
    """
    out = _run(body, state={"cuisine": ["mediterranean", "gone"], "exclude-allergens": ["nuts"]})
    assert out["rows"] == [
        {"kind": "cuisine", "value": "mediterranean", "label": "Mediterranean"},
        {"kind": "exclude-allergens", "value": "nuts", "label": "Nuts"},
    ]
    assert out["rowsElsewhere"] == [], "a week without the groups has nothing to count"
    assert out["removed"] == [["cuisine", "mediterranean"], ["exclude-allergens", "nuts"]]


def test_bar_grid_listener_and_cache_are_wired() -> None:
    bar = _method("_renderFilterBar")
    assert "_renderServerFilterGroups(week)" in bar
    assert "_serverFilterIds(week)" in bar, "the bar primes the lookup so it can say Filtering…"
    assert "Filtering…" in bar
    grid = _method("_renderGrid")
    assert "_serverFilterIds(week)" in grid
    assert "ctx.sel(r) || serverIds.has(" in grid, "selected meals must bypass the server set"
    assert "_serverFilter" in _method("_hasActiveFilters")
    bind = re.search(r"^  _bindDelegated\(card\) \{.*?^  \}", SOURCE, re.S | re.M)
    assert bind, "_bindDelegated not found"
    for needle in (
        "filter-server",
        "filter-server-all",
        "_toggleServerFilter",
        "_clearServerFilter",
    ):
        assert needle in bind.group(0), f"{needle} is not routed"
    assert "SERVER_FILTER_GROUPS.includes(kind)" in _method("_removeFilter")
    fetch_weeks = re.search(r"^  async _fetchWeeks\(.*?^  \}", SOURCE, re.S | re.M)
    assert fetch_weeks, "_fetchWeeks not found"
    assert "_serverFilterCache = new Map()" in fetch_weeks.group(0), (
        "a refetch must drop cached filter answers"
    )
    assert 'this._callResponseService("get_menu_courses"' in _async_method("_fetchMenuCourses")
    assert "config_entry_id" in _async_method("_callResponseService")
    assert "hellofresh-meal-planner:server-filter" in SOURCE
