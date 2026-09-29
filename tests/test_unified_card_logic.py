"""The unified card's decision logic (``hellofresh-card-logic.js``).

Two kinds of test:

* **Parity** — the unified card replaces seven classic cards, and "retain every capability"
  means it must make the SAME call they do: which dietary tags a filter matches, what a
  surcharge reads, which weeks count as past, what a week's state is, how a month's cost
  rolls up. These extract the classic cards' real method bodies from the shipped sources (as
  the other card tests do) and run them side by side with the new module on the same inputs.
  Deliberate differences are asserted explicitly, not left implicit.
* **Behaviour** — what is new: the combined week list, the shipping state and tracker steps,
  the box write payloads, search, entity discovery.

The module is real ESM and imported directly under Node; the classic methods are lifted with a
regex, so renaming or reindenting one fails loudly here rather than silently passing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

WWW = Path(__file__).resolve().parents[1] / "custom_components" / "hellofresh" / "www"
LOGIC = WWW / "hellofresh-card-logic.js"
SHARED = WWW / "hellofresh-shared.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

# Weeks built relative to "today" so date gating is exercised for real, never against a
# fixture that silently ages out.
WEEK_HELPERS = """
const DAY = 86400000;
function iso(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function week(id, weeksFromToday, extra = {}) {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  d.setDate(d.getDate() + weeksFromToday * 7);
  return { week_id: id, delivery_date: iso(d), recipes: [{ recipe_id: "r" }], market_items: [], ...extra };
}
const future = new Date(Date.now() + 5 * DAY).toISOString();
const passed = new Date(Date.now() - DAY).toISOString();
"""


def _methods(card: str, names: tuple[str, ...]) -> str:
    source = (WWW / card).read_text(encoding="utf-8")
    bodies = []
    for name in names:
        match = re.search(rf"^  {re.escape(name)}\(([^)]*)\) \{{.*?^  \}}", source, re.S | re.M)
        assert match, f"{name} not found in {card}"
        bodies.append(match.group(0).strip())
    return "\n".join(bodies)


def _static_getter(card: str, name: str) -> str:
    """The expression a classic card's `static get NAME()` returns."""
    source = (WWW / card).read_text(encoding="utf-8")
    match = re.search(
        rf"^  static get {name}\(\) \{{\n    return (.*?);\n  \}}", source, re.S | re.M
    )
    assert match, f"static {name} not found in {card}"
    return match.group(1)


def _run(body: str) -> object:
    script = f"""
    globalThis.window = {{ localStorage: {{ getItem: () => null, setItem() {{}} }}, dispatchEvent() {{ return true; }} }};
    globalThis.CustomEvent = class {{ constructor(t, i) {{ this.type = t; this.detail = (i || {{}}).detail; }} }};
    const L = await import({json.dumps(LOGIC.as_uri() + "?v=test")});
    const S = await import({json.dumps(SHARED.as_uri() + "?v=test")});
    // The classic cards call their shared imports by bare name inside the lifted methods.
    const {{ fmtDate, fmtPrice, titleCase, parseLocalDate, isEditable, relativeWeek, esc }} = S;
    {WEEK_HELPERS}
    const result = await (async () => {{ {body} }})();
    console.log(JSON.stringify(result));
    """
    env = {**os.environ, "TZ": "America/New_York"}
    out = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


# ---- parity with the classic cards ----------------------------------------------------------

PLANNER = "hellofresh-meal-planner-card.js"
SCHEDULE = "hellofresh-schedule-card.js"
MARKET = "hellofresh-market-card.js"
COST = "hellofresh-cost-card.js"


@pytest.mark.parametrize("table", ["DIET_FILTERS", "TIME_FILTERS", "PROTEIN_FILTERS"])
def test_filter_tables_match_the_classic_planner(table: str) -> None:
    """Same filters, same names, same tag aliases — so the chips behave identically."""
    classic = _static_getter(PLANNER, table)
    assert _run(f"return [{classic}, L.{table}];")[0] == _run(f"return L.{table};")


def test_highlights_are_the_classic_set_plus_favorites() -> None:
    classic = _run(f"return {_static_getter(PLANNER, 'HIGHLIGHT_FILTERS')};")
    unified = _run("return L.HIGHLIGHT_FILTERS;")
    assert unified[: len(classic)] == classic
    assert [h["key"] for h in unified[len(classic) :]] == ["favorite"]


RECIPES = [
    {
        "name": "A",
        "tags": ["Calorie Smart", "High Protein"],
        "calories_kcal": 900,
        "prep_time_minutes": 35,
    },
    {
        "name": "B",
        "tags": [],
        "calories_kcal": 600,
        "prep_time_minutes": 25,
        "total_time_minutes": 5,
    },
    {
        "name": "C",
        "tags": ["GLP-1 Balance", "double-protein"],
        "badge": "High Protein",
        "calories_kcal": None,
    },
    {"name": "D", "tags": ["Contains Gluten"], "prep_time_minutes": None, "total_time_minutes": 12},
    {"name": "E", "tags": ["gluten-free", "Under 30 Minutes"], "variation_title": "2x Beef"},
]


def test_diet_and_time_matching_agree_with_the_planner() -> None:
    body = f"""
      class Card {{ {_methods(PLANNER, ("_matchesDietFilter",))} }}
      const card = new Card();
      const recipes = {json.dumps(RECIPES)};
      const out = [];
      for (const r of recipes) for (const f of [...L.DIET_FILTERS, ...L.TIME_FILTERS]) {{
        out.push([card._matchesDietFilter(r, f), L.matchesDietFilter(r, f)]);
      }}
      return out;
    """
    pairs = _run(body)
    assert pairs and all(classic == unified for classic, unified in pairs)


def test_tile_chips_signature_and_surcharge_agree_with_the_planner() -> None:
    body = f"""
      const HelloFreshMealPlannerCard = {{ DIET_FILTERS: L.DIET_FILTERS }};
      class Card {{ {_methods(PLANNER, ("_tileChipLabels", "_recipeSignature", "_fmtSurcharge"))} }}
      const card = new Card();
      const recipes = {json.dumps(RECIPES)};
      return {{
        chips: recipes.map((r) => [card._tileChipLabels(r), L.tileChipLabels(r)]),
        sigs: recipes.map((r) => [card._recipeSignature(r), L.recipeSignature(r)]),
        surcharges: ["+7.99/serving", "+7,99/portion", "Premium"].map((s) => [
          card._fmtSurcharge(s, "EUR"), L.fmtSurcharge(s, "EUR"),
        ]),
      }};
    """
    result = _run(body)
    for key in ("chips", "sigs", "surcharges"):
        assert all(a == b for a, b in result[key]), key


def test_past_weeks_agree_with_the_planner_and_market() -> None:
    """The load-bearing week-list invariant: every card exposes the same history."""
    body = f"""
      const helpers = {{ _parseLocalDate: S.parseLocalDate, _weekSortKey: L.weekSortKey }};
      class Planner {{ {_methods(PLANNER, ("_browsableWeeks",))} }}
      class Market {{ {_methods(MARKET, ("_browsableWeeks",))} }}
      for (const C of [Planner, Market]) Object.assign(C.prototype, helpers);
      const weeks = [];
      for (let i = 12; i >= 1; i--) weeks.push(week(`W-${{i}}`, -i, {{ recipes: i % 3 ? [{{}}] : [] }}));
      const past = (list) => list.filter((w) => S.isPast(w)).map((w) => w.week_id);
      return [past(new Planner()._browsableWeeks(weeks)), past(new Market()._browsableWeeks(weeks)), past(L.browsableWeeks(weeks))];
    """
    planner, market, unified = _run(body)
    assert planner == market == unified
    assert len(unified) == 12


def test_week_state_agrees_with_the_schedule_card() -> None:
    """Same badge for every state the schedule card knows; "shipping" is the one addition."""
    body = f"""
      class Schedule {{ {_methods(SCHEDULE, ("_isSkipped", "_isEditable", "_needsSelection", "_weekState"))} }}
      const card = new Schedule();
      const weeks = [
        week("skipped", 1, {{ is_skipped: true, allowed_actions: {{ mealSwap: true }} }}),
        week("paused", 1, {{ status: "PAUSED", allowed_actions: {{ mealSwap: true }} }}),
        week("delivered", -1, {{ status: "DELIVERED" }}),
        week("needs", 1, {{ allowed_actions: {{ mealSwap: true }}, selection_deadline: future, needs_selection: true }}),
        week("ready", 1, {{ allowed_actions: {{ mealSwap: true }}, selection_deadline: future, needs_selection: false }}),
        week("locked-deadline", 1, {{ allowed_actions: {{ mealSwap: true }}, selection_deadline: passed }}),
        week("locked-swap", 1, {{ allowed_actions: {{ mealSwap: false }} }}),
        week("no-actions", 1, {{}}),
      ];
      return weeks.map((w) => [w.week_id, card._weekState(w), L.weekState(w)]);
    """
    for week_id, classic, unified in _run(body):
        assert classic == unified, week_id


def test_shipping_is_a_state_the_schedule_card_called_locked() -> None:
    body = f"""
      class Schedule {{ {_methods(SCHEDULE, ("_isSkipped", "_isEditable", "_needsSelection", "_weekState"))} }}
      const w = week("road", 0, {{ status: "SHIPPED", order: {{ tracking_status: "in_transit" }} }});
      return [new Schedule()._weekState(w), L.weekState(w), L.trackingStep(w)];
    """
    assert _run(body) == ["locked", "shipping", 1]


def test_skip_eligibility_agrees_with_the_planner() -> None:
    body = f"""
      class Planner {{ {_methods(PLANNER, ("_isEditable", "_isPaused", "_canSkip", "_parseLocalDate"))} }}
      Planner.prototype._isEditable = function (w) {{ return S.isEditable(w); }};
      Planner.prototype._parseLocalDate = S.parseLocalDate;
      const card = new Planner();
      const weeks = [
        week("editable", 1, {{ allowed_actions: {{ mealSwap: true }}, selection_deadline: future }}),
        week("skipped-open", 1, {{ is_skipped: true, selection_deadline: future }}),
        week("skipped-closed", 1, {{ is_skipped: true, selection_deadline: passed }}),
        week("skipped-no-deadline-future", 1, {{ is_skipped: true }}),
        week("skipped-no-deadline-past", -1, {{ is_skipped: true }}),
        week("locked", 1, {{ allowed_actions: {{ mealSwap: false }} }}),
        week("delivered", -1, {{ status: "DELIVERED" }}),
        week("paused-open", 1, {{ status: "PAUSED", selection_deadline: future }}),
      ];
      return weeks.map((w) => [w.week_id, card._canSkip(w), L.canSkip(w)]);
    """
    for week_id, classic, unified in _run(body):
        assert classic == unified, week_id


def test_prices_counts_and_vouchers_agree_with_the_schedule_card() -> None:
    body = f"""
      class Schedule {{
        {_methods(SCHEDULE, ("_isSkipped", "_weekPriceParts", "_marketCount", "_weekBenefit", "_nextBoxVoucher", "_fmtDate"))}
      }}
      const card = new Schedule();
      const weeks = [
        week("billed", -1, {{ order: {{ billed_total_price: 88.91, billed_total_currency: "USD", total_price: 90 }} }}),
        week("cart", 1, {{ order: {{ total_price: "104.91", currency: "CAD" }} }}),
        week("skipped", 1, {{ is_skipped: true, order: {{ total_price: 50 }} }}),
        week("none", 1, {{ order: null }}),
        week("market", 1, {{ market_items: [{{ selected_quantity: 2 }}, {{ selected_quantity: 0, is_selected: true }}, {{ is_selected: true }}] }}),
        week("voucher", 1, {{ benefits: [{{ status: "available", label: "$10 off", one_time: true, expires_at: "2099-01-05T12:00:00Z" }}] }}),
      ];
      const account = {{ next_box_discount: {{ label: "$5 off", week_id: "none" }} }};
      card._account = account;
      return weeks.map((w) => {{
        const c = card._nextBoxVoucher(w);
        const u = L.nextBoxVoucher(w, account);
        return [
          w.week_id,
          [card._weekPriceParts(w), L.weekPriceParts(w)],
          [card._marketCount(w), L.marketCount(w)],
          [c && c.label, u && u.label],
          [c && c.note, u && u.note],
        ];
      }});
    """
    for week_id, *pairs in _run(body):
        for classic, unified in pairs:
            assert classic == unified, week_id


def test_dense_months_agree_with_the_cost_card() -> None:
    body = f"""
      class Cost {{ {_methods(COST, ("_denseMonths",))} }}
      const months = [
        {{ month: "2026-08", amount: 476.01, currency: "USD", box_count: 5 }},
        {{ month: "2026-05", amount: "334.67", currency: "USD", box_count: 3 }},
        {{ month: "2025-12", amount: 497, currency: "USD", box_count: 4 }},
        {{ month: "bogus", amount: 1 }},
      ];
      return [new Cost()._denseMonths(months, 12), L.denseMonths(months, 12)];
    """
    classic, unified = _run(body)
    assert classic == unified
    assert [m["month"] for m in unified][-1] == "2026-08"


# ---- new behaviour ---------------------------------------------------------------------------


def test_skipped_future_weeks_stay_listed_so_they_can_be_unskipped() -> None:
    body = """
      const weeks = [
        week("next", 1),
        week("skipped", 2, { is_skipped: true, recipes: [] }),
        week("market-only", 3, { recipes: [], market_items: [{ item_id: "m" }] }),
        week("gap", 4, { recipes: [] }),
        week("behind-gap", 5),
      ];
      return L.browsableWeeks(weeks).map((w) => w.week_id);
    """
    assert _run(body) == ["next", "skipped", "market-only"]


def test_next_box_skips_skipped_weeks_and_falls_back_to_the_last_box() -> None:
    body = """
      const a = L.nextBoxWeek([week("past", -1), week("skip", 1, { is_skipped: true }), week("ship", 2)]);
      const b = L.nextBoxWeek([week("old", -2), week("recent", -1)]);
      const c = L.nextBoxWeek([week("skip", 1, { is_skipped: true })]);
      return [[a.week.week_id, a.upcoming], [b.week.week_id, b.upcoming], [c.week.week_id, c.upcoming]];
    """
    assert _run(body) == [["ship", True], ["recent", False], ["skip", True]]


def test_a_paused_week_is_not_editable_until_unskipped() -> None:
    body = """
      const w = week("paused", 1, { status: "PAUSED", allowed_actions: { mealSwap: true }, selection_deadline: future });
      return [S.isEditable(w), L.isWeekEditable(w), L.weekState(w), L.canSkip(w)];
    """
    # The classic shared rule still says editable; the unified card treats paused as skipped.
    assert _run(body) == [True, False, "skipped", True]


def test_tracking_steps() -> None:
    body = """
      const mk = (status, tracking, extra = {}) => week("w", 0, { status, order: { tracking_status: tracking, ...extra } });
      return [
        L.trackingStep(week("none", 1, { order: null })),
        L.trackingStep(mk("RUNNING", "pre_transit")),
        L.trackingStep(mk("RUNNING", null, { tracking_number: "1Z" })),
        L.trackingStep(mk("ON_THE_WAY", null)),
        L.trackingStep(mk("RUNNING", "out_for_delivery")),
        L.trackingStep(week("d", -1, { delivered_at: "2026-09-28T21:42:00+00:00" })),
        L.trackingStep(mk("SKIPPED", "in_transit", {})),
      ];
    """
    # A skipped week never tracks, whatever its stale order says.
    result = _run(body)
    assert result[:6] == [-1, 0, 0, 1, 2, 3]


def test_meal_write_translates_course_indexes_and_skips_default_quantities() -> None:
    body = """
      const w = { recipes: [
        { recipe_id: "a", course_index: 1 }, { recipe_id: "b", course_index: 2 },
        { recipe_id: "c", course_index: 3 }, { recipe_id: "a", course_index: 7 },
      ] };
      return L.buildMealWrite(w, new Map([[1, 1], [2, 3], [7, 1], [99, 1]]));
    """
    # Unknown indexes are dropped; a recipe id is written once even if two indexes share it.
    assert _run(body) == {"recipe_ids": ["a", "b"], "quantities": {"b": 3}}


def test_market_write_is_the_full_desired_state() -> None:
    assert _run('return L.buildMarketWrite(new Map([["m1", 2], ["m2", 0], ["m3", 1]]));') == {
        "m1": 2,
        "m3": 1,
    }


def test_filters_keep_chosen_meals_but_search_does_not() -> None:
    body = """
      const recipes = [
        { recipe_id: "1", course_index: 1, name: "Salmon Bowl", preference: "Seafood", tags: [] },
        { recipe_id: "2", course_index: 2, name: "Beef Tacos", preference: "Beef", tags: [] },
        { recipe_id: "3", course_index: 3, name: "Chicken Tacos", preference: "Poultry", tags: [] },
      ];
      const chosen = new Map([[2, 1]]);
      const sel = (r) => L.tileSelected(chosen, r);
      const ids = (list) => list.map((r) => r.recipe_id);
      return [
        ids(L.visibleRecipes(recipes, { sel, applyFilters: true, protein: new Set(["Seafood"]) })),
        ids(L.visibleRecipes(recipes, { sel, query: "chicken" })),
        ids(L.visibleRecipes(recipes, { sel, query: "tacos" })),
        ids(L.visibleRecipes(recipes, { sel, selectedOnly: true })),
      ];
    """
    by_protein, by_query, multi, box = _run(body)
    assert by_protein == ["2", "1"]  # the chosen beef meal leads, despite the seafood filter
    assert by_query == ["3"]
    assert multi == ["2", "3"]
    assert box == ["2"]


def test_variant_hiding_and_grouping() -> None:
    body = """
      const recipes = [
        { recipe_id: "x", course_index: 5, name: "Zucchini", tags: [] },
        { recipe_id: "b", course_index: 11, name: "Base", variation_group: 10, tags: [] },
        { recipe_id: "a", course_index: 10, name: "Base", variation_group: 10, tags: [] },
      ];
      const sel = () => false;
      const ids = (list) => list.map((r) => r.recipe_id);
      return [
        ids(L.visibleRecipes(recipes, { sel, applyFilters: true })),
        ids(L.visibleRecipes(recipes, { sel, applyFilters: true, showVariants: false })),
      ];
    """
    grouped, hidden = _run(body)
    assert grouped == ["a", "b", "x"]  # the base leads its variant; groups cluster
    assert hidden == ["a", "x"]


def test_market_groups_keep_boxed_items_under_a_section_filter() -> None:
    body = """
      const w = { market_items: [
        { item_id: "1", name: "Garlic Bread", group_type: "appetizer" },
        { item_id: "2", name: "Lava Cake", group_type: "dessert" },
        { item_id: "3", name: "Churros", group_type: "dessert" },
      ] };
      const chosen = new Map([["1", 1]]);
      return L.marketGroups(w, chosen, { sections: new Set(["dessert"]) })
        .map((g) => [g.label, g.items.map((i) => i.item_id)]);
    """
    assert _run(body) == [["Appetizers", ["1"]], ["Desserts", ["2", "3"]]]


def test_box_total_falls_back_to_the_plan_price_as_an_estimate() -> None:
    body = """
      const account = { selected_plan_total_price: 84.93, selected_plan_total_price_currency: "USD" };
      return [
        L.boxTotal(week("billed", -1, { order: { billed_total_price: 90, billed_total_currency: "USD" } }), account),
        L.boxTotal(week("plan", 2, { order: null }), account),
        L.boxTotal(week("skipped", 2, { is_skipped: true }), account),
      ];
    """
    billed, plan, skipped = _run(body)
    assert billed == {"amount": 90, "currency": "USD", "estimate": False, "label": "Total"}
    assert plan["estimate"] is True and plan["label"] == "Plan price"
    assert skipped is None


def test_entities_are_found_by_translation_key_and_account() -> None:
    """Entity ids follow the (renameable) account title, so they are looked up, never guessed."""
    body = """
      const hass = {
        entities: {
          "todo.home_prep_list": { entity_id: "todo.home_prep_list", platform: "hellofresh", translation_key: "prep_list", device_id: "d1" },
          "todo.cabin_prep_list": { entity_id: "todo.cabin_prep_list", platform: "hellofresh", translation_key: "prep_list", device_id: "d2" },
          "todo.other": { entity_id: "todo.other", platform: "other", translation_key: "prep_list", device_id: "d1" },
          "select.home_box_size": { entity_id: "select.home_box_size", platform: "hellofresh", translation_key: "box_size", device_id: "d1" },
        },
        devices: { d1: { config_entries: ["entry-home"] }, d2: { config_entries: ["entry-cabin"] } },
      };
      return [
        L.findEntity(hass, "prep_list", { domain: "todo" }),
        L.findEntity(hass, "prep_list", { configEntryId: "entry-cabin" }),
        L.findEntity(hass, "box_size", { domain: "todo" }),
        L.findEntity(hass, "delivery_day"),
        L.findEntity({}, "prep_list"),
      ];
    """
    assert _run(body) == ["todo.home_prep_list", "todo.cabin_prep_list", None, None, None]


def test_countdown_and_deadline_tone() -> None:
    body = """
      const now = Date.now();
      const at = (ms) => new Date(now + ms);
      return [
        L.countdown(at(2 * 86400000 + 5 * 3600000 + 60000), now), L.deadlineTone(at(2 * 86400000), now),
        L.countdown(at(5 * 3600000 + 60000), now), L.deadlineTone(at(5 * 3600000), now),
        L.countdown(at(-1000), now), L.deadlineTone(at(-1000), now),
      ];
    """
    assert _run(body) == ["2d 5h left", "soon", "5h left", "urgent", "passed", "passed"]


def test_month_rollup_counts_boxes_skips_and_own_prices_only() -> None:
    body = """
      const rows = [
        week("a", -3, { order: { billed_total_price: 90, billed_total_currency: "USD" } }),
        week("b", -2, { order: { total_price: 80, currency: "USD" } }),
        week("c", -1, { is_skipped: true, order: { total_price: 70 } }),
        week("d", 0, { order: null }),
      ];
      return L.monthRollup(rows);
    """
    assert _run(body) == {"boxes": 3, "skipped": 1, "total": 170, "currency": "USD", "priced": 2}
