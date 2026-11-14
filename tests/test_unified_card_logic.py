"""The unified card's decision logic (``hellofresh-card-logic.js``).

Two kinds of test:

* **Carried over** — the unified card replaced seven classic cards (since removed), and
  "retain every capability" meant making the SAME call they did: which dietary tags a filter
  matches, what a surcharge reads, which weeks count as past, what a week's state is, how a
  month's cost rolls up. Parity tests once ran both side by side; the expected values here are
  what both produced. Deliberate differences are asserted explicitly, not left implicit.
* **Behaviour** — what is new: the combined week list, the shipping state and tracker steps,
  the box write payloads, search, entity discovery, and the decisions behind Undo, a week's
  number of people, Recipes' Load more, payment problems, credit and boxes HelloFresh moved.

The module is real ESM and imported directly under Node.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

WWW = Path(__file__).resolve().parents[1] / "custom_components" / "hellofresh" / "www"
LOGIC = WWW / "hellofresh-card-logic.js"
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


def _run(body: str, tz: str = "America/New_York") -> object:
    """Run `body` under Node with the logic module as `L`; `tz` is the "browser's" time zone."""
    script = f"""
    globalThis.window = {{ localStorage: {{ getItem: () => null, setItem() {{}} }}, dispatchEvent() {{ return true; }} }};
    globalThis.CustomEvent = class {{ constructor(t, i) {{ this.type = t; this.detail = (i || {{}}).detail; }} }};
    const L = await import({json.dumps(LOGIC.as_uri() + "?v=test")});
    {WEEK_HELPERS}
    const result = await (async () => {{ {body} }})();
    console.log(JSON.stringify(result));
    """
    env = {**os.environ, "TZ": tz}
    out = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


# ---- carried over from the classic cards ----------------------------------------------------


def test_filter_groups_use_the_websites_words() -> None:
    """The filter panel's groups, in the website's order and with its labels."""
    diet, time, protein, highlights = _run("""
      return [
        L.DIET_FILTERS.map((f) => [f.key, f.label]),
        L.TIME_FILTERS.map((f) => [f.key, f.label, f.maxMinutes]),
        L.PROTEIN_FILTERS,
        L.HIGHLIGHT_FILTERS.map((f) => [f.key, f.label]),
      ];
    """)
    assert diet == [
        ["vegetarian", "Vegetarian"],
        ["under-650-cal", "Under 650 Calories"],
        ["high-protein", "High Protein"],
        ["carb-conscious", "Carb Conscious"],
        ["high-fiber", "Fiber Powered"],
        ["gluten-free", "Gluten-Free Friendly"],
        ["sodium-smart", "Sodium Smart"],
        ["low-sugar", "Low Added Sugar"],
        ["organic-protein", "Organic Protein"],
        ["glp1", "GLP-1 Support"],
    ]
    assert time == [
        ["under-15-min", "Under 15 Minutes", 15],
        ["under-20-min", "Under 20 Minutes", 20],
        ["under-30-min", "Under 30 Minutes", 30],
    ]
    assert protein == ["Beef", "Poultry", "Pork", "Seafood", "Lamb", "Veggie"]
    # The classic planner's three highlights, plus Favorites.
    assert highlights == [
        ["new", "New"],
        ["bestseller", "Bestsellers"],
        ["cooked-before", "Cooked Before"],
        ["favorite", "Favorites"],
    ]


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


def test_diet_and_time_matching() -> None:
    body = f"""
      const recipes = {json.dumps(RECIPES)};
      return recipes.map((r) =>
        [...L.DIET_FILTERS, ...L.TIME_FILTERS].filter((f) => L.matchesDietFilter(r, f)).map((f) => f.key)
      );
    """
    assert _run(body) == [
        # Tagged "Calorie Smart" (the category's old name): it counts despite its 900 kcal.
        ["under-650-cal", "high-protein"],
        # Untagged: its own 600 kcal, and its 25-minute prep time rather than the 5-minute total.
        ["under-650-cal", "under-30-min"],
        # "GLP-1 Balance" is an alias; a High Protein badge or "double-protein" tag is not one.
        ["glp1"],
        # "Contains Gluten" is not gluten-free; with no prep time, the 12-minute total counts.
        ["under-15-min", "under-20-min", "under-30-min"],
        # Aliases match case-insensitively.
        ["gluten-free", "under-30-min"],
    ]


def test_tile_chips_and_surcharges() -> None:
    body = f"""
      const recipes = {json.dumps(RECIPES)};
      return {{
        chips: recipes.map((r) => L.tileChipLabels(r)),
        surcharges: ["+7.99/serving", "+7,99/portion", "Premium"].map((s) => L.fmtSurcharge(s, "EUR")),
      }};
    """
    result = _run(body)
    assert result["chips"] == [
        ["Under 650 Calories", "High Protein"],
        [],
        ["2x Protein", "GLP-1 Support"],
        [],
        ["Gluten-Free Friendly"],
    ]
    # In the account's currency, a decimal comma read as one; a label with no amount kept.
    assert result["surcharges"] == ["+€7.99", "+€7.99", "Premium"]


def test_a_surcharge_is_its_cents_and_the_label_only_without_them() -> None:
    """The payload carries the amount as `surcharge_cents`; re-reading it out of the display
    label turned "-2.00" into "+$2.00", "2x +3.99" into "+$2.00" and "+1,299.00" into "+$1.30".
    The cents win; without them the label's signed number is read, minus and all."""
    body = """
      return {
        cents: [
          L.fmtSurcharge("+7.99/serving", "USD", 499),
          L.fmtSurcharge({ surcharge_label: "2x +3.99", surcharge_cents: 399 }, "USD"),
          L.fmtSurcharge({ surcharge_label: "discount", surcharge_cents: -250 }, "USD"),
          L.fmtSurcharge({ surcharge_label: "+7.99/serving", surcharge_cents: null }, "USD"),
          L.fmtSurcharge("+7.99/serving", "USD", "799"),
        ],
        labels: ["-2.00", "2x +3.99", "+1,299.00", "+1.299,00", "\u22121,50/portion", "Premium", ""].map(
          (label) => L.fmtSurcharge(label, "USD")
        ),
        missing: [L.fmtSurcharge(null, "USD"), L.fmtSurcharge(undefined, "USD")],
      };
    """
    result = _run(body)
    assert result["cents"] == ["+$4.99", "+$3.99", "-$2.50", "+$7.99", "+$7.99"]
    assert result["labels"] == [
        "-$2.00",
        "+$3.99",
        "+$1,299.00",
        "+$1,299.00",
        "-$1.50",
        "Premium",
        "",
    ]
    assert result["missing"] == ["", ""]


def test_a_dish_listed_twice_has_one_signature() -> None:
    """One dish under two menu categories collapses to one tile; a variant does not."""
    body = f"""
      const [, , , , e] = {json.dumps(RECIPES)};
      const again = {{ ...e, course_index: 9, tags: e.tags.slice().reverse() }};
      const variant = {{ ...e, variation_title: "2x Chicken" }};
      return [L.recipeSignature(e) === L.recipeSignature(again), L.recipeSignature(e) === L.recipeSignature(variant)];
    """
    assert _run(body) == [True, False]


def test_every_past_week_stays_listed() -> None:
    """The load-bearing week-list invariant: the whole history, menu data or not."""
    body = """
      const weeks = [];
      for (let i = 12; i >= 1; i--) weeks.push(week(`W-${i}`, -i, { recipes: i % 3 ? [{}] : [] }));
      return L.browsableWeeks(weeks).filter((w) => L.isPastWeek(w)).map((w) => w.week_id);
    """
    assert _run(body) == [f"W-{i}" for i in range(12, 0, -1)]


def test_week_state() -> None:
    """The classic schedule card's badge for every state it knew; "shipping" is new (below)."""
    body = """
      const weeks = [
        week("skipped", 1, { is_skipped: true, allowed_actions: { mealSwap: true } }),
        week("paused", 1, { status: "PAUSED", allowed_actions: { mealSwap: true } }),
        week("delivered", -1, { status: "DELIVERED" }),
        week("needs", 1, { allowed_actions: { mealSwap: true }, selection_deadline: future, needs_selection: true }),
        week("ready", 1, { allowed_actions: { mealSwap: true }, selection_deadline: future, needs_selection: false }),
        week("locked-deadline", 1, { allowed_actions: { mealSwap: true }, selection_deadline: passed }),
        week("locked-swap", 1, { allowed_actions: { mealSwap: false } }),
        week("no-actions", 1, {}),
      ];
      return Object.fromEntries(weeks.map((w) => [w.week_id, L.weekState(w)]));
    """
    assert _run(body) == {
        "skipped": "skipped",
        "paused": "skipped",
        "delivered": "delivered",
        "needs": "needs",
        "ready": "ready",
        "locked-deadline": "locked",
        "locked-swap": "locked",
        "no-actions": "locked",
    }


def test_shipping_is_a_state_of_its_own() -> None:
    """The classic schedule card read a box on the road as "locked"."""
    body = """
      const w = week("road", 0, { status: "SHIPPED", order: { tracking_status: "in_transit" } });
      return [L.weekState(w), L.trackingStep(w)];
    """
    assert _run(body) == ["shipping", 1]


def test_a_past_week_without_delivery_information_is_delivered() -> None:
    """HelloFresh keeps a box's progress for a few weeks only; the website still shows it delivered.

    "Locked" is for a box past its deadline that hasn't come yet. A past week reads delivered
    unless it was skipped, never sent, late, or is still on its way.
    """
    body = """
      const weeks = [
        week("old", -10, { status: "UNKNOWN", order: { tracking_status: null } }),
        week("old-running", -6, { status: "RUNNING" }),
        week("skipped", -3, { status: "PAUSED" }),
        week("cancelled", -2, { status: "CANCELLED" }),
        week("late", -1, { status: "RUNNING", sub_status: "DELAYED" }),
        week("on-the-road", -1, { status: "RUNNING", order: { tracking_status: "in_transit" } }),
        week("coming", 1, { status: "RUNNING", allowed_actions: { mealSwap: false } }),
      ];
      const states = Object.fromEntries(weeks.map((w) => [w.week_id, L.weekState(w)]));
      return { states, oldStep: L.trackingStep(weeks[0]) };
    """
    assert _run(body) == {
        "states": {
            "old": "delivered",
            "old-running": "delivered",
            "skipped": "skipped",
            "cancelled": "locked",
            "late": "locked",
            "on-the-road": "shipping",
            "coming": "locked",
        },
        "oldStep": 3,
    }


def test_the_carrier_decides_once_the_delivery_day_has_passed() -> None:
    """A Veho box that never came (2026-11-03): the day after delivery day it read delivered.

    HelloFresh still had the week PREPARING and Veho had it delayed in transit (it was later
    discarded as damaged), but the card counted the past date alone. A past date only stands
    in for delivery when nothing live is left to say otherwise.
    """
    body = """
      const yesterday = iso(new Date(Date.now() - DAY));
      const box = (status, tracking, extra = {}) => week("w", 0, {
        delivery_date: yesterday,
        status,
        sub_status: "COOK_IT",
        allowed_actions: { mealSwap: false },
        order: { tracking_status: tracking, tracking_number: "HF00000000000000", ...extra },
      });
      const cases = {
        delayed: box("PREPARING", "delayed", { tracking_status_detail: "delayed_in_transit" }),
        exception: box("RUNNING", "exception", { tracking_status_detail: "damaged" }),
        label_only: box("RUNNING", "pre_transit"),
        preparing: week("p", 0, { delivery_date: yesterday, status: "PREPARING" }),
        carrier_delivered: box("PREPARING", "delivered"),
      };
      return Object.fromEntries(
        Object.entries(cases).map(([k, w]) => [k, [L.isDelivered(w), L.weekState(w), L.trackingStep(w)]])
      );
    """
    assert _run(body) == {
        "delayed": [False, "shipping", 1],
        "exception": [False, "locked", 0],
        "label_only": [False, "locked", 0],
        "preparing": [False, "locked", -1],
        "carrier_delivered": [True, "delivered", 3],
    }


def test_skip_eligibility() -> None:
    body = """
      const weeks = [
        week("editable", 1, { allowed_actions: { mealSwap: true }, selection_deadline: future }),
        week("skipped-open", 1, { is_skipped: true, selection_deadline: future }),
        week("skipped-closed", 1, { is_skipped: true, selection_deadline: passed }),
        week("skipped-no-deadline-future", 1, { is_skipped: true }),
        week("skipped-no-deadline-past", -1, { is_skipped: true }),
        week("locked", 1, { allowed_actions: { mealSwap: false } }),
        week("delivered", -1, { status: "DELIVERED" }),
        week("paused-open", 1, { status: "PAUSED", selection_deadline: future }),
      ];
      return Object.fromEntries(weeks.map((w) => [w.week_id, L.canSkip(w)]));
    """
    assert _run(body) == {
        "editable": True,
        # A skipped week can be unskipped until its deadline (or, undated, its delivery day).
        "skipped-open": True,
        "skipped-closed": False,
        "skipped-no-deadline-future": True,
        "skipped-no-deadline-past": False,
        "locked": False,
        "delivered": False,
        "paused-open": True,
    }


def test_prices_counts_and_vouchers() -> None:
    body = """
      const weeks = [
        week("billed", -1, { order: { billed_total_price: 88.91, billed_total_currency: "USD", total_price: 90 } }),
        week("cart", 1, { order: { total_price: "104.91", currency: "CAD" } }),
        week("skipped", 1, { is_skipped: true, order: { total_price: 50 } }),
        week("none", 1, { order: null }),
        week("market", 1, { market_items: [{ selected_quantity: 2 }, { selected_quantity: 0, is_selected: true }, { is_selected: true }] }),
        week("voucher", 1, { benefits: [{ status: "available", label: "$10 off", one_time: true, expires_at: "2099-01-05T12:00:00Z" }] }),
      ];
      const account = { next_box_discount: { label: "$5 off", week_id: "none" } };
      return Object.fromEntries(weeks.map((w) => {
        const v = L.nextBoxVoucher(w, account);
        return [w.week_id, [L.weekPriceParts(w), L.marketCount(w), v && v.label, v && v.note]];
      }));
    """
    assert _run(body) == {
        # What was billed beats the cart total; a string amount is read as a number.
        "billed": [{"amount": 88.91, "currency": "USD"}, 0, None, None],
        "cart": [{"amount": 104.91, "currency": "CAD"}, 0, None, None],
        "skipped": [None, 0, None, None],
        # The account's next-box discount lands on the week it names.
        "none": [None, 0, "$5 off", ""],
        # Extras counted by quantity: a stale is_selected with quantity 0 is not one.
        "market": [None, 2, None, None],
        "voucher": [None, 0, "$10 off", "expires Mon, Jan 5 · one-time"],
    }


def test_next_box_discount_uses_account_currency_when_delivery_total_has_none() -> None:
    amounts = _run("""
      const account = {
        next_delivery_price_breakdown: { discount_amount: 239 },
        next_delivery_total_currency: null,
        selected_plan_total_price_currency: "DKK",
        next_box_discount: { currency: "DKK" },
      };
      return [
        L.nextBoxDiscount(account),
        L.nextBoxDiscount({ ...account, next_delivery_total_currency: "EUR" }),
        L.nextBoxDiscount({ ...account, selected_plan_total_price_currency: null }),
        L.nextBoxDiscount({ ...account, next_delivery_price_breakdown: { discount_amount: 0 } }),
      ];
    """)
    assert "DKK" in amounts[0]
    assert "€" in amounts[1]
    assert "DKK" in amounts[2]
    assert amounts[3] is None


def test_dense_months() -> None:
    """Twelve months ending at the latest, gaps filled with empty months, junk dropped."""
    body = """
      const months = [
        { month: "2026-08", amount: 476.01, currency: "USD", box_count: 5 },
        { month: "2026-05", amount: "334.67", currency: "USD", box_count: 3 },
        { month: "2025-12", amount: 497, currency: "USD", box_count: 4 },
        { month: "bogus", amount: 1 },
      ];
      return L.denseMonths(months, 12);
    """
    months = _run(body)
    assert [m["month"] for m in months] == [
        "2025-09", "2025-10", "2025-11", "2025-12", "2026-01", "2026-02",
        "2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08",
    ]  # fmt: skip
    filled = {m["month"]: (m["amount"], m["currency"], m["box_count"]) for m in months}
    assert filled["2025-12"] == (497, "USD", 4)
    assert filled["2026-05"] == (334.67, "USD", 3)
    assert filled["2026-08"] == (476.01, "USD", 5)
    assert filled["2026-07"] == (0, None, 0)


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
      return [L.isWeekEditable(w), L.weekState(w), L.canSkip(w)];
    """
    # The classic cards still offered meal edits on one; the unified card treats paused as skipped.
    assert _run(body) == [False, "skipped", True]


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
      const ids = (tiles) => tiles.map((t) => t.recipe.recipe_id);
      return [
        ids(L.menuTiles(recipes, { sel, applyFilters: true, protein: new Set(["Seafood"]) })),
        ids(L.menuTiles(recipes, { sel, query: "chicken" })),
        ids(L.menuTiles(recipes, { sel, query: "tacos" })),
        ids(L.menuTiles(recipes, { sel, selectedOnly: true })),
      ];
    """
    by_protein, by_query, multi, box = _run(body)
    assert by_protein == ["2", "1"]  # the chosen beef meal leads, despite the seafood filter
    assert by_query == ["3"]
    assert multi == ["2", "3"]
    assert box == ["2"]


# A W42-shaped dish: base 10 with options listed Salmon, 2x Chicken, Organic (sold out) — in the
# site's order, which is not course-index order — and a standalone dish.
DISH = """
  const recipes = [
    { recipe_id: "org", course_index: 13, name: "Honey Organic Chicken", variation_group: 10,
      variation_title: "Organic Chicken", variation_order: 2, preference: "Poultry", tags: [] },
    { recipe_id: "base", course_index: 10, name: "Honey Chicken", variation_group: 10,
      variation_default_title: "No Change", preference: "Poultry", tags: [] },
    { recipe_id: "2x", course_index: 11, name: "Honey 2x Chicken", variation_group: 10,
      variation_title: "2x Chicken Cutlets", variation_order: 1, preference: "Poultry", tags: [] },
    { recipe_id: "salmon", course_index: 12, name: "Honey Salmon", variation_group: 10,
      variation_title: "Salmon", variation_order: 0, preference: "Seafood", tags: [] },
    { recipe_id: "tacos", course_index: 5, name: "Beef Tacos", preference: "Beef", tags: [] },
  ];
  const ids = (tiles) => tiles.map((t) => t.recipe.recipe_id);
  const tiles = (chosen, opts = {}) =>
    ids(L.menuTiles(recipes, { sel: (r) => L.tileSelected(chosen, r), ...opts }));
"""


def test_a_dish_and_its_options_are_one_tile() -> None:
    body = (
        DISH
        + """
      const [dish] = L.dishGroups(recipes);
      return {
        members: dish.members.map((r) => r.recipe_id),
        base: dish.base.recipe_id,
        none: tiles(new Map()),
        option: tiles(new Map([[11, 2]])),
        both: tiles(new Map([[11, 1], [12, 1]])),
      };
    """
    )
    got = _run(body)
    # One tile for the dish, one for the standalone meal; the base leads its options, which
    # follow the website's list order rather than their course indexes.
    assert got["members"] == ["base", "salmon", "2x", "org"]
    assert got["base"] == "base"
    assert got["none"] == ["base", "tacos"]
    # A chosen option stands in for the dish (and leads, being in the box)…
    assert got["option"] == ["2x", "tacos"]
    # …and two versions of one dish in the box each get a tile (in the dish's own option order),
    # so neither is hidden.
    assert got["both"] == ["salmon", "2x", "tacos"]


def test_a_filter_or_search_can_show_a_dishs_option() -> None:
    body = (
        DISH
        + """
      return [
        tiles(new Map(), { applyFilters: true, protein: new Set(["Seafood"]) }),
        tiles(new Map(), { query: "salmon" }),
        tiles(new Map(), { query: "organic" }),
        tiles(new Map([[11, 1]]), { applyFilters: true, protein: new Set(["Seafood"]) }),
      ];
    """
    )
    seafood, salmon, organic, chosen = _run(body)
    assert seafood == ["salmon"]  # the chicken dish shows its Salmon version; tacos drop out
    assert salmon == ["salmon"]
    assert organic == ["org"]
    assert chosen == ["2x"]  # a chosen version still stays, whatever the filter


def test_protein_chips_also_take_hellofreshs_own_answer() -> None:
    """The menu leaves a few meals without a protein — W42's "Chinese-Style Speedy Ramen Noodles
    with 2x Tofu", a swap on a beef dish, has a blank category in the menu AND in its option
    list — yet the website shows it under Veggie, because its filter service says so. The chips
    ask that service for the proteins the week declares (the site has no Lamb) and add what it
    returns to what the menu data already matches."""
    body = """
      const week = { week_id: "2026-W42", menu_filters: [{ slug: "main-protein", options: [
        { name: "Beef", slug: "beef" }, { name: "Seafood", slug: "fish-seafood" },
        { name: "Veggie", slug: "vegetarian" } ] }] };
      const recipes = [
        { recipe_id: "b1-a", course_index: 57, variation_group: 57, name: "Beef Ramen", preference: "Beef" },
        { recipe_id: "v1-a", course_index: 8, name: "Chickpea Wraps", preference: "Veggie" },
        { recipe_id: "t1-a", course_index: 453, variation_group: 57, variation_order: 5,
          variation_title: "2x Tofu", name: "Ramen with 2x Tofu", preference: null },
      ];
      const veggie = (proteinIds) => L.menuTiles(recipes, {
        sel: () => false, applyFilters: true, protein: new Set(["Veggie"]), proteinIds,
      }).map((t) => t.recipe.name);
      return [
        L.proteinServerFilters(week, new Set(["Veggie", "Lamb", "Seafood", "Pork"])),
        L.proteinServerFilters(week, new Set(["Lamb"])),
        L.proteinServerFilters({ week_id: "2026-W41" }, new Set(["Veggie"])),
        veggie(null),
        veggie(new Set(["t1", "v1"])),
      ];
    """
    query, lamb, undeclared, menu_only, with_service = _run(body)
    assert query == {"main-protein": ["fish-seafood", "vegetarian"]}  # no Lamb; Pork not offered
    assert lamb == {} and undeclared == {}  # nothing to ask
    assert menu_only == ["Chickpea Wraps"]  # before the answer (or if the lookup fails)
    assert with_service == ["Ramen with 2x Tofu", "Chickpea Wraps"]  # the beef dish's tofu swap


def test_dishes_sit_where_their_base_does() -> None:
    """The website orders dishes by their base's place in the menu (HAR 57); an option listed
    ahead of its base must not pull the dish forward. Your box's meals still lead."""
    body = """
      const recipes = [
        { recipe_id: "x", course_index: 33, name: "Fajitas" },
        { recipe_id: "o", course_index: 513, variation_group: 68, variation_order: 0, name: "Salmon 2x" },
        { recipe_id: "p", course_index: 32, name: "Meatballs" },
        { recipe_id: "s", course_index: 68, variation_group: 68, name: "Salmon" },
        { recipe_id: "q", course_index: 12, name: "Stew" },
      ];
      const order = (chosen) => L.menuTiles(recipes, { sel: (r) => chosen.includes(r.recipe_id) })
        .map((t) => t.recipe.recipe_id);
      return [order([]), order(["s", "p"])];
    """
    plain, boxed = _run(body)
    assert plain == ["x", "p", "s", "q"]
    assert boxed == ["p", "s", "x", "q"]


def test_option_labels_use_the_websites_words() -> None:
    body = (
        DISH
        + """
      const by = Object.fromEntries(recipes.map((r) => [r.recipe_id, L.optionLabel(r)]));
      return [by, L.optionLabel({ course_index: 7, variation_group: 7, name: "X" })];
    """
    )
    labels, unnamed = _run(body)
    assert labels == {
        "base": "No Change",
        "salmon": "Salmon",
        "2x": "2x Chicken Cutlets",
        "org": "Organic Chicken",
        "tacos": "Original recipe",
    }
    assert unnamed == "Original recipe"


def test_pantry_amounts_use_the_abbreviation() -> None:
    body = """
      return [
        "3 teaspoon (tsp)", "2 tablespoon (tbsp) + 1 cup (c)", "1 unit", "1-2 clove", "", "¼ teaspoon (tsp)",
      ].map((a) => L.shortAmount(a));
    """
    assert _run(body) == ["3 tsp", "2 tbsp + 1 c", "1 unit", "1-2 clove", "", "¼ tsp"]


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


def test_entities_are_found_by_the_device_config_entry_id() -> None:
    """Home Assistant 2026.8 gave devices a single config_entry_id and deprecated the
    config_entries list, which a later release drops; either shape finds the account."""
    body = """
      const entities = {
        "todo.home_prep_list": { entity_id: "todo.home_prep_list", platform: "hellofresh", translation_key: "prep_list", device_id: "d1" },
        "todo.cabin_prep_list": { entity_id: "todo.cabin_prep_list", platform: "hellofresh", translation_key: "prep_list", device_id: "d2" },
      };
      const current = { entities, devices: {
        d1: { config_entry_id: "entry-home", config_entries: ["entry-home"] },
        d2: { config_entry_id: "entry-cabin", config_entries: ["entry-cabin"] },
      } };
      const future = { entities, devices: { d1: { config_entry_id: "entry-home" }, d2: { config_entry_id: "entry-cabin" } } };
      return [
        L.findEntity(current, "prep_list", { configEntryId: "entry-cabin" }),
        L.findEntity(future, "prep_list", { configEntryId: "entry-cabin" }),
        L.findEntity(future, "prep_list", { configEntryId: "entry-gone" }),
      ];
    """
    assert _run(body) == ["todo.cabin_prep_list", "todo.cabin_prep_list", None]


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


def test_live_tracking_reads_the_netherlands_tracker_sensors() -> None:
    """Where HelloFresh drives its own vans (the Netherlands), the integration's tracking sensors
    carry its live tracker: the phase sensor the whole snapshot as attributes, the ETA sensor the
    arrival. The card turns them into one live view, tied to the week whose order carries the
    tracked link, and shows nothing while no delivery is live."""
    body = """
      const url = "https://www.hftrack.nl/track/abc";
      const phase = (attrs, state = "On the way") => ({
        state, attributes: { active: true, phase: "ON_THE_WAY", tracking_url: url, ...attrs },
      });
      const eta = { state: "2026-10-05T18:18:00+02:00" };
      const live = L.liveTracking(phase({
        amount_of_stops_before: 0, driver_name: "Sanne", delivery_time: "18:00 – 22:00",
        personal_customer_message: "Hoi!", last_fetched: "2026-10-05T17:38:00+02:00",
      }), eta);
      const week = (link) => ({ week_id: "2026-W41", order: { tracking_url: link } });
      const now = new Date("2026-10-05T17:40:00+02:00");
      return {
        live: { ...live, eta: live.eta.toISOString(), updated: live.updated.toISOString() },
        idle: L.liveTracking({ state: "unknown", attributes: { active: false, phase: "INVALID_LINK" } }, eta),
        missing: L.liveTracking(null, null),
        unknownEta: L.liveTracking(phase({}), { state: "unknown" }).eta,
        newPhase: L.liveTracking(phase({ phase: "SORTING" }, "Sorting"), null).label,
        delayed: L.liveTracking(phase({ phase: "DELAYED" }), null).tone,
        cancelled: L.liveTracking(phase({ phase: "CANCELLED" }), null).step,
        mine: L.liveTrackingFor(live, week(url)),
        other: L.liveTrackingFor(live, week("https://track.shipveho.com/x")),
        until: L.untilText(live.eta, now),
        soon: L.untilText(new Date(now.getTime() + 30000), now),
        late: L.untilText(new Date(now.getTime() - 60000), now),
        ago: L.agoText(live.updated, now),
      };
    """
    out = _run(body)
    assert out["live"] == {
        "phase": "ON_THE_WAY",
        "label": "On the way to you",
        "step": 1,
        "tone": "",
        "eta": "2026-10-05T16:18:00.000Z",
        "window": "18:00 – 22:00",
        "stops": 0,
        "driver": "Sanne",
        "message": "Hoi!",
        "trackingUrl": "https://www.hftrack.nl/track/abc",
        "mapUrl": "https://www.hftrack.nl/track/abc",
        "updated": "2026-10-05T15:38:00.000Z",
    }
    assert out["idle"] is None and out["missing"] is None
    assert out["unknownEta"] is None
    assert out["newPhase"] == "Sorting"  # a phase HelloFresh adds later still reads sensibly
    assert out["delayed"] == "warn" and out["cancelled"] == -1
    assert out["mine"] is True and out["other"] is False
    assert (out["until"], out["soon"], out["late"], out["ago"]) == (
        "in 38 min",
        "any minute now",
        "",
        "2 min ago",
    )


def test_the_calendar_starts_on_home_assistants_first_weekday() -> None:
    """Home Assistant's profile setting wins; left on "language", the language decides — Monday
    for Dutch or British English, Sunday for US English."""
    body = """
      const first = (locale, language) => L.firstWeekday({ locale, language });
      return [
        first({ first_weekday: "monday", language: "en" }),
        first({ first_weekday: "saturday", language: "nl" }),
        first({ first_weekday: "language", language: "nl" }),
        first({ first_weekday: "language", language: "en-GB" }),
        first({ first_weekday: "language", language: "en" }),
        first(undefined, "de"),
        L.firstWeekday(null),
      ];
    """
    assert _run(body) == [1, 6, 1, 1, 0, 1, 0]


# ---- the Week tab's plan ---------------------------------------------------------------------

PLAN_WEEK = """
const box = {
  week_id: "2026-W42", delivery_date: "2026-10-12",
  recipes: [
    { recipe_id: "a", name: "A", is_selected: true, selected_quantity: 1, prep_time_minutes: 30, total_time_minutes: 10 },
    { recipe_id: "b", name: "B", is_selected: true, selected_quantity: 2 },
    { recipe_id: "b", name: "B, customized", is_selected: true },
    { recipe_id: "c", name: "C" },
  ],
  market_items: [
    { item_id: "gy", name: "Gyoza", group_type: "appetizer", selected_quantity: 1 },
    { item_id: "pk", name: "Pancakes", group_type: "breakfast", selected_quantity: 1 },
    { item_id: "ck", name: "Chicken", group_type: "protein", is_selected: true },
    { item_id: "pet", name: "Treats", group_type: "petstable", selected_quantity: 1 },
    { item_id: "don", name: "Donation", group_type: "donation", selected_quantity: 1 },
    { item_id: "no", name: "Unbought", group_type: "dessert", selected_quantity: 0 },
  ],
};
const meal = (id) => ({ kind: "meal", id, week_id: "2026-W42", name: id });
const wednesday = new Date(2026, 9, 14, 15);
"""


def test_plan_covers_seven_days_from_delivery_and_the_weeks_still_being_eaten() -> None:
    days, weeks, opens, ahead = _run(
        PLAN_WEEK
        + """
      const older = { week_id: "2026-W41", delivery_date: "2026-10-05" };
      const later = { week_id: "2026-W43", delivery_date: "2026-10-19" };
      const weeks = L.planWeeks([older, box, later, { week_id: "undated" }], wednesday);
      return [
        L.planDays(box),
        weeks.map((w) => w.week_id),
        L.defaultPlanWeek(weeks, wednesday).week_id,
        L.defaultPlanWeek([later], wednesday).week_id,
      ];
    """
    )
    assert days == [f"2026-10-{d}" for d in range(12, 19)]
    # Last week's box ended on Sunday the 11th; an undated week has no days to plan.
    assert weeks == ["2026-W42", "2026-W43"]
    assert opens == "2026-W42"  # the week whose days include today
    assert ahead == "2026-W43"  # else the next one


def test_plan_offers_the_box_and_the_market_items_bought_never_pets_or_donations() -> None:
    items, slots = _run(
        PLAN_WEEK
        + """
      const items = L.planItems(box);
      return [
        items.map((i) => [i.kind, i.id, i.quantity, i.minutes, i.group || null]),
        items.map((i) => [i.id, L.suggestedSlot(i).meal, L.suggestedSlot(i).course]),
      ];
    """
    )
    # A meal listed twice (a customized variant shares the id) is offered once; the tile's
    # headline time is prep_time_minutes.
    assert items == [
        ["meal", "a", 1, 30, None],
        ["meal", "b", 2, None, None],
        ["market", "gy", 1, None, "appetizer"],
        ["market", "pk", 1, None, "breakfast"],
        ["market", "ck", 1, None, "protein"],
    ]
    assert slots == [
        ["a", "dinner", "main"],
        ["b", "dinner", "main"],
        ["gy", "dinner", "appetizer"],
        ["pk", "breakfast", "main"],
        ["ck", "dinner", "main"],
    ]


def test_plan_counts_uses_fills_the_tray_and_shows_breakfast_only_when_bought() -> None:
    uses, skipped, tray, shown, bare, always, titles, notes = _run(
        PLAN_WEEK
        + """
      const gyoza = { kind: "market", id: "gy", week_id: "2026-W42", name: "Gyoza" };
      const plan = { always_show: { breakfast: false, lunch: false }, days: {
        "2026-10-12": { dinner: { main: meal("a") } },
        "2026-10-13": { dinner: { main: meal("b"), appetizer: gyoza } },
        "2026-10-14": { lunch: { main: { kind: "note", name: "Leftovers" } } },
      } };
      const noMarket = { ...box, market_items: [] };
      return [
        L.itemUses(plan, meal("b")),
        L.itemUses(plan, meal("b"), { day: "2026-10-13", meal: "dinner", course: "main" }).count,
        L.trayItems(box, plan).map((i) => [i.id, i.left]),
        L.planMealsShown(box, plan),
        L.planMealsShown(noMarket, { days: {} }),
        L.planMealsShown(noMarket, { always_show: { lunch: true }, days: {} }),
        [L.slotTitle(plan.days["2026-10-13"].dinner), L.slotTitle({ appetizer: { name: "Gyoza" }, dessert: { name: "Cake" } })],
        L.sameItem({ kind: "note", name: "x" }, { kind: "note", name: "x" }),
      ];
    """
    )
    assert uses == {"count": 1, "days": ["2026-10-13"]}
    assert skipped == 0  # the slot being edited never counts against its own pick
    # A is planned as often as it was ordered; B once of twice; the appetizer isn't a tray item.
    assert tray == [["b", 1], ["pk", 1]]
    # Breakfast because it was bought, lunch because something is planned for it.
    assert shown == ["breakfast", "lunch", "dinner"]
    assert bare == ["dinner"]
    assert always == ["lunch", "dinner"]
    assert titles == ["b", "Gyoza, Cake"]
    assert notes is False  # two notes are never "the same item" (no limit on notes)


def test_plan_flags_a_swapped_meal_and_shows_what_is_still_to_come_today() -> None:
    inbox, swapped, unknown, note, today, start = _run(
        PLAN_WEEK
        + """
      // As the integration writes them: Home Assistant's wall clock with its offset.
      const pad = (n) => String(n).padStart(2, "0");
      const at = (h, m = 0) => `2026-10-14T${pad(h)}:${pad(m)}:00-04:00`;
      const plan = { days: { "2026-10-14": {
        breakfast: { main: { kind: "note", name: "Eggs" }, start: at(7, 30), end: at(8) },
        dinner: { main: meal("a"), start: at(17, 30), end: at(19) },
      } } };
      return [
        L.stillInBox(meal("a"), [box]),
        L.stillInBox(meal("c"), [box]),
        L.stillInBox(meal("a"), []),
        L.stillInBox({ kind: "note", name: "x" }, [box]),
        L.todayPlan(plan, wednesday).map((t) => t.meal),
        L.hhmm(plan.days["2026-10-14"].dinner.start),
      ];
    """
    )
    assert inbox is True
    assert swapped is False  # C is on the menu but no longer chosen
    assert unknown is True  # a week no longer loaded can't be checked
    assert note is True
    assert today == ["dinner"]  # breakfast is over by 3 PM
    assert start == "17:30"


def test_payment_brands_use_their_names_not_their_codes() -> None:
    """HelloFresh sends Adyen-style codes; "mc" must read Mastercard, not "Mc"."""
    result = _run(
        """
        return ["mc", "visa", "amex", "bcmc", "i_deal", "paypal", "credit_card", "jcb"].map(
          (brand) => L.cardOnFile({ payment_card_brand: brand })
        );
        """
    )
    assert result == [
        "Mastercard",
        "Visa",
        "American Express",
        "Bancontact",
        "iDEAL",
        "PayPal",
        "Credit card",
        "Jcb",
    ]


# ---- Home Assistant's clock ------------------------------------------------------------------


def test_plan_times_and_today_follow_home_assistants_clock_not_the_browsers() -> None:
    """The integration writes a planned meal's start as Home Assistant's wall clock and reads an
    edited time back the same way. A browser three hours west used to prefill 15:00 for an 18:00
    dinner; changing it to 15:30 then moved dinner to 15:30 in Home Assistant's zone. "Today"
    drifted the same way around midnight."""
    body = """
      const start = "2026-11-13T18:00:00-05:00";
      // 01:30 on the 14th in New York (Home Assistant), 22:30 on the 13th in Los Angeles (browser).
      const night = new Date("2026-11-14T06:30:00Z");
      const plan = { days: {
        "2026-11-13": { dinner: { main: { kind: "note", name: "Friday" }, end: "2026-11-13T19:30:00-05:00" } },
        "2026-11-14": { dinner: { main: { kind: "note", name: "Saturday" }, start: "2026-11-14T18:00:00-05:00",
          end: "2026-11-14T19:30:00-05:00" } },
      } };
      // Delivered Saturday the 7th: its seven days end on Friday the 13th.
      const over = { week_id: "over", delivery_date: "2026-11-07" };
      const next = { week_id: "next", delivery_date: "2026-11-14" };
      const read = () => ({
        zone: L.haTimeZone(),
        input: L.hhmm(start),
        shown: L.fmtHaTime(start),
        fromDate: L.fmtHaTime(new Date(start)),
        today: L.haToday(night),
        tonight: L.todayPlan(plan, night).map((entry) => entry.slot.main.name),
        weeks: L.planWeeks([over, next], night).map((w) => w.week_id),
        opens: L.defaultPlanWeek([over, next], night).week_id,
      });
      const before = read();
      L.I18n.useHass({ language: "en", config: { time_zone: "America/New_York" } });
      const after = read();
      L.I18n.useHass({ language: "en", config: { time_zone: "Not/AZone" } });
      const unknown = read();
      return { before, after, unknown, bad: [L.hhmm(null), L.hhmm("2026-11-13"), L.fmtHaTime("nonsense")] };
    """
    out = _run(body, tz="America/Los_Angeles")
    # The time field reads the string's own wall clock, whatever zone anything is in.
    assert {out[k]["input"] for k in ("before", "after", "unknown")} == {"18:00"}
    # With Home Assistant's zone known, its day and its clock are used...
    assert out["after"] == {
        "zone": "America/New_York",
        "input": "18:00",
        "shown": "6:00 PM",
        "fromDate": "6:00 PM",
        "today": "2026-11-14",
        "tonight": ["Saturday"],
        "weeks": ["next"],
        "opens": "next",
    }
    # ...and until it is known (or when the browser doesn't know it), the browser's stand in.
    for key in ("before", "unknown"):
        assert out[key]["shown"] == "3:00 PM"
        assert out[key]["today"] == "2026-11-13"
        assert out[key]["weeks"] == ["over", "next"]
    assert out["before"]["zone"] is None
    assert out["bad"] == ["", "", ""]


def test_week_state_honours_the_clock_it_is_given() -> None:
    """weekState(week, now) passed `now` to its deadline checks but not to "has this box been
    delivered", so a state asked for another moment mixed two clocks."""
    body = """
      const delivered = week("past", -1, { allowed_actions: {} });
      const before = Date.now() - 14 * DAY; // a moment before that box's delivery day
      return [
        L.weekState(delivered),
        L.weekState(delivered, before),
        L.isShipping({ ...delivered, status: "SHIPPED" }),
        L.isShipping({ ...delivered, status: "SHIPPED", order: { tracking_status: "in_transit" } }, before),
      ];
    """
    # Past its day with nothing live left: delivered. Seen from before its day: not yet.
    assert _run(body) == ["delivered", "locked", True, True]


# ---- the card's own bookkeeping ----------------------------------------------------------------


def test_a_card_fetches_only_what_its_sections_show() -> None:
    """A Recipes-only or Account-only card reads neither every week's menu nor the meal plan;
    the Overview reads the plan only beside the Week tab (its "Tonight" line)."""
    body = """
      const needs = (views) => { const n = L.dataNeeds(views); return [n.weeks, n.summary, n.mealPlan]; };
      return {
        all: needs(["overview", "menu", "market", "recipes", "week", "account"]),
        recipes: needs(["recipes"]),
        account: needs(["account"]),
        overview: needs(["overview"]),
        overviewAndWeek: needs(["overview", "week"]),
        market: needs(["market", "recipes"]),
        none: needs(undefined),
      };
    """
    assert _run(body) == {
        "all": [True, True, True],
        "recipes": [False, True, False],
        "account": [False, True, False],
        "overview": [True, True, False],
        "overviewAndWeek": [True, True, True],
        "market": [True, True, False],
        "none": [False, True, False],
    }


def test_a_weeks_read_redraws_only_a_section_drawn_from_the_weeks() -> None:
    """Toggling a favourite on Recipes re-reads the weeks for the Menu's hearts; the Recipes grid
    shows no week, so that read must not redraw it (up to 200 photos). Account does show weeks
    (its counters open them)."""
    body = """
      return ["overview", "menu", "market", "week", "account", "recipes", "", null].map(L.viewShowsWeeks);
    """
    assert _run(body) == [True, True, True, True, True, False, False, False]


def test_a_failed_load_is_retried_soon_then_at_the_usual_pace() -> None:
    """15 seconds, a minute, five minutes, then the refresh interval; never longer than the
    interval itself (the delivery-day watch can be shorter than a retry delay)."""
    body = """
      const hours3 = 180 * 60000;
      return {
        delays: [0, 1, 2, 3, 4, 9].map((failures) => L.loadDelayMs(failures, hours3)),
        capped: [1, 2, 3].map((failures) => L.loadDelayMs(failures, 2 * 60000)),
        steps: L.RETRY_DELAYS_MS,
      };
    """
    out = _run(body)
    hours3 = 180 * 60000
    assert out["steps"] == [15000, 60000, 300000]
    assert out["delays"] == [hours3, 15000, 60000, 300000, hours3, hours3]
    assert out["capped"] == [15000, 60000, 120000]


def test_after_a_write_only_the_week_written_is_reread() -> None:
    """A save, skip or reschedule changes one week's box; anything else, or two different weeks,
    means every week. The Week tab's plan writes leave the weeks alone."""
    body = """
      const run = (...writes) => writes.reduce((noted, [service, data]) => L.noteWrite(noted, service, data), undefined);
      const w = (week_id) => ({ week_id });
      return {
        save: run(["select_meals", w("2026-W41")]),
        twice: run(["select_meals", w("2026-W41")], ["select_market_items", w("2026-W41")]),
        two: run(["select_meals", w("2026-W41")], ["skip_week", w("2026-W42")]),
        favourite: run(["add_favorite", { recipe_id: "r" }]),
        saveThenFavourite: run(["select_meals", w("2026-W41")], ["add_favorite", {}]),
        plan: run(["set_planned_meal", { date: "2026-10-14", week_id: "2026-W41" }]) === undefined,
        planThenSave: run(["set_planned_meal", w("2026-W40")], ["reschedule_week", w("2026-W41")]),
        noWeek: run(["skip_week", {}]),
        servings: run(["change_week_servings", { week_id: "2026-W41", servings: 4 }]),
        servingsThenUndo: run(["change_week_servings", w("2026-W41")], ["change_week_servings", w("2026-W41")]),
        reads: ["get_weeks", "get_meal_plan", "preview_meal_price", "select_meals", "refresh_data", ""].map(L.isReadService),
        planOptions: L.isReadService("get_plan_options"),
      };
    """
    assert _run(body) == {
        "save": "2026-W41",
        "twice": "2026-W41",
        "two": "all",
        "favourite": "all",
        "saveThenFavourite": "all",
        "plan": True,
        "planThenSave": "2026-W41",
        "noWeek": "all",
        # One week's number of people is that week's box alone.
        "servings": "2026-W41",
        "servingsThenUndo": "2026-W41",
        "reads": [True, True, True, False, False, False],
        "planOptions": True,
    }


def test_a_reread_week_goes_back_into_the_list() -> None:
    body = """
      const weeks = [week("a", -1), week("b", 1), week("c", 2)];
      const skipped = { ...week("b", 1), is_skipped: true, recipes: [] };
      const merged = L.mergeWeek(weeks, skipped);
      const added = L.mergeWeek(weeks, week("d", 3));
      return {
        ids: merged.map((w) => w.week_id),
        replaced: merged[1] === skipped && weeks[1] !== skipped,
        browsable: L.browsableWeeks(merged).map((w) => [w.week_id, L.isSkipped(w)]),
        added: L.browsableWeeks(added).map((w) => w.week_id),
        empty: L.mergeWeek(null, skipped).length,
      };
    """
    assert _run(body) == {
        "ids": ["a", "b", "c"],
        "replaced": True,  # a new list; the old one is the sibling cards' too
        "browsable": [["a", False], ["b", True], ["c", False]],
        "added": ["a", "b", "c", "d"],
        "empty": 1,
    }


def test_cards_share_one_request_unless_something_changed_since_it_began() -> None:
    """Three cards loading the same account at the same moment are one request. A read that
    began before a write (or before a change seen in Home Assistant) is never joined: its answer
    may predate the change. The same change seen by several cards moves the epoch once."""
    body = """
      const account = { config_entry_id: "a" };
      let started = 0;
      const gates = [];
      const start = () => { started += 1; return new Promise((resolve, reject) => gates.push({ resolve, reject })); };
      const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
      const out = {};

      const one = L.sharedLoad(account, "weeks", start);
      const two = L.sharedLoad(account, "weeks", start);
      const other = L.sharedLoad({ config_entry_id: "b" }, "weeks", start);
      const summary = L.sharedLoad(account, "summary", start);
      await tick();
      out.shared = [one === two, one === other, one === summary, started];

      // A write: the read in flight is older than it.
      L.bumpDataEpoch(account);
      const afterWrite = L.sharedLoad(account, "weeks", start);
      await tick();
      out.afterWrite = [afterWrite === one, started];

      // The same entity change seen by two cards.
      const before = L.dataEpoch(account);
      L.bumpDataEpoch(account, "mealplan", "calendar=off@t1");
      L.bumpDataEpoch(account, "mealplan", "calendar=off@t1");
      const once = L.dataEpoch(account) - before;
      L.bumpDataEpoch(account, "mealplan", "calendar=off@t2");
      out.tokens = [once, L.dataEpoch(account) - before, L.dataEpoch({ config_entry_id: "b" })];

      // A load queued behind another never joins one.
      const a = L.sharedLoad(account, "plan", start);
      const fresh = L.sharedLoad(account, "plan", start, { fresh: true });
      const joinsFresh = L.sharedLoad(account, "plan", start);
      await tick();
      out.fresh = [a === fresh, fresh === joinsFresh];

      // Finished (or failed) loads are forgotten; the answer reaches everyone who joined.
      for (const gate of gates.slice(0, -1)) gate.resolve({ ok: true });
      gates[gates.length - 1].reject(new Error("down"));
      out.answers = [(await one).ok, (await two).ok, await fresh.then(() => "ok", (err) => err.message)];
      const count = started;
      const again = L.sharedLoad(account, "plan", start);
      await tick();
      out.again = [again === fresh, started - count];
      return out;
    """
    assert _run(body) == {
        "shared": [True, False, False, 3],
        "afterWrite": [False, 4],
        "tokens": [1, 2, 0],
        "fresh": [False, True],
        "answers": [True, True, "down"],
        "again": [False, 1],
    }


def test_the_header_line_names_the_plan() -> None:
    body = """
      const weeks = [{ ...week("w", 1), display_name: "Oct 5 - Oct 11" }];
      return [
        L.cardSubtitle({ selected_plan: "Classic Plan", required_meal_count: 3, number_of_people: 2,
          subscription_status: "active" }, weeks),
        L.cardSubtitle({ selected_plan: "Classic Plan", required_meal_count: "1", number_of_people: "1",
          subscription_status: "PAUSED" }, weeks),
        L.cardSubtitle({}, weeks),
        L.cardSubtitle(null, weeks),
        L.cardSubtitle(null, null),
      ];
    """
    assert _run(body) == [
        "Classic Plan · 3 meals for 2 people",
        "Classic Plan · 1 meal for 1 person · Paused",
        "Oct 5 - Oct 11",
        "Oct 5 - Oct 11",
        "",
    ]


def test_entity_changes_are_told_apart_by_group() -> None:
    """Home Assistant hands the card `hass` on every state change anywhere. Only a change in one
    of the card's own entity groups causes work; the first reading is not a change, whichever
    entities exist (a setup with only the Meals calendar never noticed a plan change)."""
    body = """
      const ids = { prep: ["todo.a", "todo.b"], boxSize: "select.size", deliveryDay: null,
        accessToken: "sensor.token", meals: "calendar.meals", trackingPhase: null, trackingEta: null };
      const states = {
        "todo.a": { state: "3", last_updated: "t1" },
        "select.size": { state: "3 meals", last_updated: "t1" },
        "sensor.token": { state: "42", last_updated: "t1" },
        "calendar.meals": { state: "off", last_updated: "t1" },
      };
      const sign = (s = states, of = ids) => L.entitySignature(of, (id) => s[id] || null);
      const first = sign();
      const token = sign({ ...states, "sensor.token": { state: "41", last_updated: "t2" } });
      const plan = sign({ ...states, "calendar.meals": { state: "off", last_updated: "t2" } });
      const onlyMeals = { meals: "calendar.meals" };
      const mealsFirst = sign(states, onlyMeals);
      const mealsNext = sign({ "calendar.meals": { state: "off", last_updated: "t2" } }, onlyMeals);
      const down = (s) => L.unavailableEntities(ids, (id) => s[id] || null);
      return {
        first,
        firstReading: [L.changedEntityGroups({}, first), L.changedEntityGroups(L.entitySignature({}, () => null), first)],
        token: L.changedEntityGroups(first, token),
        plan: L.changedEntityGroups(first, plan),
        same: L.changedEntityGroups(first, sign()),
        mealsOnly: [L.changedEntityGroups({}, mealsFirst), L.changedEntityGroups(mealsFirst, mealsNext)],
        unavailable: [
          down(states),
          down({ ...states, "todo.a": { state: "unavailable" }, "calendar.meals": { state: "unavailable" } }),
          L.unavailableEntities(null, () => null),
        ],
      };
    """
    out = _run(body)
    assert out["first"] == {
        "pantry": "todo.a=3@t1|todo.b=-",
        "plan": "select.size=3 meals@t1",
        "health": "sensor.token=42@t1",
        "tracking": "",
        "mealplan": "calendar.meals=off@t1",
    }
    assert out["firstReading"] == [[], []]
    assert out["token"] == ["health"]
    assert out["plan"] == ["mealplan"]
    assert out["same"] == []
    assert out["mealsOnly"] == [[], ["mealplan"]]
    assert out["unavailable"] == [0, 2, 0]


def test_the_tick_notices_a_deadline_passing() -> None:
    """Time alone changes one thing: a deadline goes by, and the Add buttons, Save and
    Skip/Unskip must go with it."""
    body = """
      const soon = new Date(Date.now() + 60000).toISOString();
      const weeks = [
        week("edit", 1, { allowed_actions: { mealSwap: true }, selection_deadline: soon }),
        week("skipped", 2, { is_skipped: true, selection_deadline: soon }),
        week("locked", 0, { allowed_actions: { mealSwap: false } }),
      ];
      return [L.actionsKey(weeks), L.actionsKey(weeks, Date.now() + 120000), L.actionsKey(null)];
    """
    assert _run(body) == ["es-s--", "------", ""]


def test_change_day_rows_use_the_delivery_options_when_they_are_known() -> None:
    body = """
      const w = { delivery_date: "2026-10-05", available_one_off_options: [
        { handle: "mon", delivery_date: "2026-10-05" },
        { handle: "tue", delivery_date: "2026-10-06" },
        { handle: "wed", delivery_date: "2026-10-07" },
        { handle: "odd" },
        { delivery_date: "2026-10-09" },
        null,
      ] };
      const catalog = {
        mon: { delivery_name: "Monday 8AM - 8PM", price: 0 },
        wed: { delivery_name: "Wednesday 8AM - 8PM", price: "2.99" },
      };
      return {
        named: L.rescheduleOptions(w, catalog, "USD"),
        bare: L.rescheduleOptions(w, null, "USD").map((o) => [o.handle, o.name, o.date, o.price]),
        none: L.rescheduleOptions({}, catalog, "USD"),
      };
    """
    out = _run(body)
    assert out["named"] == [
        {
            "handle": "mon",
            "current": True,
            "name": "Monday 8AM - 8PM",
            "date": "Mon, Oct 5",
            "price": "",
        },
        # No entry for it in the catalog: the date alone.
        {"handle": "tue", "current": False, "name": "Tuesday, October 6", "date": "", "price": ""},
        {
            "handle": "wed",
            "current": False,
            "name": "Wednesday 8AM - 8PM",
            "date": "Wed, Oct 7",
            "price": "+$2.99",
        },
        {"handle": "odd", "current": False, "name": "odd", "date": "", "price": ""},
    ]
    # The options couldn't be read: every row still has its date.
    assert out["bare"] == [
        ["mon", "Monday, October 5", "", ""],
        ["tue", "Tuesday, October 6", "", ""],
        ["wed", "Wednesday, October 7", "", ""],
        ["odd", "odd", "", ""],
    ]
    assert out["none"] == []


def test_a_pantry_summary_splits_into_name_and_amount() -> None:
    body = """
      return ["Butter — 2 tablespoon (tbsp)", "Salt", "Oil — 1 tablespoon (tbsp) + 1 teaspoon (tsp)", null]
        .map((summary) => L.pantryParts(summary));
    """
    assert _run(body) == [
        {"name": "Butter", "amount": "2 tablespoon (tbsp)", "short": "2 tbsp"},
        {"name": "Salt", "amount": "", "short": ""},
        {
            "name": "Oil",
            "amount": "1 tablespoon (tbsp) + 1 teaspoon (tsp)",
            "short": "1 tbsp + 1 tsp",
        },
        {"name": "", "amount": "", "short": ""},
    ]


def test_a_control_is_named_by_what_it_does() -> None:
    """Focus goes back to "the same" control after a re-render by a name both share: its data
    attributes (the action and its arguments), in any order; without any, what identifies it."""
    body = """
      const key = (tag, attrs) => L.focusKey(tag, Object.entries(attrs));
      return [
        key("BUTTON", { class: "hf-tab", "data-action": "nav", "data-view": "menu", "aria-current": "page" }),
        key("button", { "data-view": "menu", "data-action": "nav", class: "hf-tab" }),
        key("button", { "data-action": "qty", "data-key": "12", "data-delta": "-1", disabled: "" }),
        key("div", { "data-scroll-key": "weekstrip", "data-scroll-restored": "" }),
        key("button", { "data-focus-key": "x", "data-action": "fp-step" }),
        key("select", { id: "fp-diet", class: "hf-select" }),
        key("a", { href: "https://example.com/a?b=1", target: "_blank" }),
        key("button", { class: "hf-btn" }),
        L.focusNaming(Object.entries({ class: "x", "data-action": "nav", "data-view": "menu" })),
      ];
    """
    assert _run(body) == [
        "button|data-action=nav|data-view=menu",
        "button|data-action=nav|data-view=menu",
        "button|data-action=qty|data-delta=-1|data-key=12",
        "div|data-scroll-key=weekstrip",
        "button|data-action=fp-step",
        "select|id=fp-diet",
        "a|href=https://example.com/a?b=1",
        "",
        [["data-action", "nav"], ["data-view", "menu"]],
    ]


def test_payload_urls_come_back_as_urls_https_only_for_delivery_photos() -> None:
    """One URL check everywhere: plain http(s) or nothing, returned as the URL (escaped where it
    enters markup). Delivery photos load from whatever host a carrier names, so those are https
    only."""
    body = """
      const w = { order: { delivery_photo_urls: [
        "https://cdn.carrier.example/p1.jpg?a=1&b=2",
        "http://cdn.carrier.example/p2.jpg",
        "javascript:alert(1)",
        "https://cdn.carrier.example/p3.jpg",
        "https://cdn.carrier.example/p4.jpg",
        "https://cdn.carrier.example/p5.jpg",
      ] } };
      const live = L.liveTracking({ state: "On the way", attributes: { active: true, phase: "ON_THE_WAY",
        tracking_url: "https://www.hftrack.nl/track/a?b=1&c=2" } }, null);
      return {
        photos: L.deliveryPhotos(w),
        none: L.deliveryPhotos(null),
        map: live.mapUrl,
        media: [L.safeMediaUrl("https://media.hellofresh.com/v.mp4?x=1&y=2"), L.safeMediaUrl("data:video/mp4,AAAA")],
        same: L.safeMediaUrl === L.safeUrl,
      };
    """
    assert _run(body) == {
        "photos": [
            "https://cdn.carrier.example/p1.jpg?a=1&b=2",
            "https://cdn.carrier.example/p3.jpg",
            "https://cdn.carrier.example/p4.jpg",
        ],
        "none": [],
        "map": "https://www.hftrack.nl/track/a?b=1&c=2",
        "media": ["https://media.hellofresh.com/v.mp4?x=1&y=2", ""],
        "same": True,
    }


# ---- undo --------------------------------------------------------------------------------------


def test_undo_is_offered_until_the_weeks_selection_deadline() -> None:
    """HelloFresh takes a change to a week only until its selection deadline, so the toast's
    Undo stands only until then; a week without one counts while it is still to come."""
    body = """
      return [
        L.undoWindowOpen(week("open", 1, { selection_deadline: future })),
        L.undoWindowOpen(week("shut", 1, { selection_deadline: passed })),
        L.undoWindowOpen(week("undated-deadline", 1)),
        L.undoWindowOpen(week("past", -1)),
        L.undoWindowOpen(null),
        L.UNDO_MS,
      ];
    """
    assert _run(body) == [True, False, True, False, False, 10000]


def test_undoing_a_save_writes_the_old_box_back_with_the_same_service() -> None:
    """Meals changed: ONE select_meals with the old recipe ids, servings and Market quantities,
    the whole old box in one cart write. Only extras changed: select_market_items. An old box
    smaller than HelloFresh's smallest can't be written back."""
    body = """
      const w = { week_id: "W", recipes: [1, 2, 3, 4].map((i) => ({ recipe_id: `r${i}`, course_index: i })) };
      const before = { meals: new Map([[1, 1], [2, 2], [3, 1]]), market: new Map([["m1", 2]]) };
      return {
        meals: L.boxUndoWrite(w, before, { meals: true, market: false }),
        both: L.boxUndoWrite(w, before, { meals: true, market: true }),
        extras: L.boxUndoWrite(w, before, { meals: false, market: true }),
        noExtrasBefore: L.boxUndoWrite(w, { meals: before.meals, market: new Map() }, { meals: false, market: true }),
        tooSmall: L.boxUndoWrite(w, { meals: new Map([[1, 1]]), market: new Map() }, { meals: true, market: false }),
        nothingChanged: L.boxUndoWrite(w, before, { meals: false, market: false }),
      };
    """
    assert _run(body) == {
        "meals": {
            "service": "select_meals",
            "data": {
                "week_id": "W",
                "recipe_ids": ["r1", "r2", "r3"],
                "quantities": {"r2": 2},
                "market_quantities": {"m1": 2},
            },
        },
        "both": {
            "service": "select_meals",
            "data": {
                "week_id": "W",
                "recipe_ids": ["r1", "r2", "r3"],
                "quantities": {"r2": 2},
                "market_quantities": {"m1": 2},
            },
        },
        "extras": {
            "service": "select_market_items",
            "data": {"week_id": "W", "quantities": {"m1": 2}},
        },
        # Every extra removed is the full state too: nothing.
        "noExtrasBefore": {
            "service": "select_market_items",
            "data": {"week_id": "W", "quantities": {}},
        },
        "tooSmall": None,
        "nothingChanged": None,
    }


def test_a_new_delivery_day_is_undone_back_to_the_option_for_the_old_date() -> None:
    body = """
      const w = { week_id: "W", delivery_date: "2026-10-05", available_one_off_options: [
        { handle: null, delivery_date: "2026-10-05" },
        { handle: "mon", delivery_date: "2026-10-05" },
        { handle: "tue", delivery_date: "2026-10-06" },
      ] };
      return [
        L.currentDeliveryOption(w),
        L.currentDeliveryOption({ ...w, delivery_date: "2026-10-09" }),
        L.currentDeliveryOption({ week_id: "W", delivery_date: "2026-10-05" }),
        L.currentDeliveryOption(null),
      ];
    """
    assert _run(body) == ["mon", None, None, None]


def test_the_old_delivery_day_is_found_among_the_moved_weeks_other_days() -> None:
    """HelloFresh lists a week's other days, so the day a box left appears only after the move.

    Undo of a new delivery day looked for the old day in the options from before the move,
    where HelloFresh never lists it, so Undo was hardly ever offered.
    """
    body = """
      const moved = { week_id: "W", delivery_date: "2026-10-06", available_one_off_options: [
        { handle: "mon", delivery_date: "2026-10-05" },
        { handle: "wed", delivery_date: "2026-10-07" },
      ] };
      return [
        L.deliveryOptionForDate(moved, "2026-10-05"),
        L.deliveryOptionForDate(moved, "2026-10-09"),
        L.deliveryOptionForDate(moved, null),
        L.deliveryOptionForDate(null, "2026-10-05"),
      ];
    """
    assert _run(body) == ["mon", None, None, None]


# ---- one week's number of people ----------------------------------------------------------------

PLAN_OPTIONS = [
    {
        "handle": "US-CBU-3-2-0",
        "family_handle": "cbu",
        "family_name": "Classic",
        "meals": 3,
        "servings": 2,
        "price": 71.94,
    },
    {
        "handle": "US-CBU-3-4-0",
        "family_handle": "cbu",
        "family_name": "Classic",
        "meals": 3,
        "servings": 4,
        "price": 131.88,
    },
    {
        "handle": "US-CBU-4-4-0",
        "family_handle": "cbu",
        "family_name": "Classic",
        "meals": 4,
        "servings": 4,
        "price": 167.84,
    },
    {
        "handle": "US-CBU-3-6-0",
        "family_handle": "cbu",
        "family_name": "Classic",
        "meals": "3",
        "servings": "6",
        "price": None,
    },
    {
        "handle": "US-VEG-3-2-0",
        "family_handle": "veg",
        "family_name": "Veggie",
        "meals": 3,
        "servings": 2,
        "price": 68.94,
    },
    {
        "handle": "US-VEG-3-1-0",
        "family_handle": "veg",
        "family_name": "Veggie",
        "meals": 3,
        "servings": 1,
        "price": 40,
    },
    {
        "handle": "US-ODD-0-0-0",
        "family_handle": "odd",
        "family_name": "Odd",
        "meals": None,
        "servings": 2,
        "price": 1,
    },
]


def test_a_weeks_box_changes_size_within_its_meal_count_and_family() -> None:
    """The sizes a week's box can change to: its meal count, its family (told by the one family
    selling its size, else by the name the box or the plan carries), each number of people once,
    fewest first, with the whole box's price. Without a family: every family's sizes."""
    body = f"""
      const options = {json.dumps(PLAN_OPTIONS)};
      const summary = {{ number_of_people: 2, required_meal_count: 3, selected_plan: "Classic Plan" }};
      const classic = {{ week_id: "W", display_name: "Classic Box", meals_required: 3, servings: 2 }};
      const sizes = (w, s = summary) => L.servingsChoices(w, options, s).map((c) => [c.servings, c.meals, c.price, c.current]);
      return {{
        people: [
          L.weekServings(classic, summary),
          L.weekServings({{ servings: null }}, {{ number_of_people: "4" }}),
          L.weekServings({{ servings: 0 }}, {{}}),
          L.weekServings(null, null),
        ],
        classic: sizes(classic),
        namedByThePlan: sizes({{ ...classic, display_name: "" }}),
        oneFamilySellsIt: sizes({{ ...classic, servings: 1, display_name: "Mystery" }}, {{ ...summary, selected_plan: "" }}),
        familyUnknown: sizes({{ ...classic, display_name: "Mystery" }}, {{ ...summary, selected_plan: "" }}),
        fromThePlan: sizes({{ week_id: "W", display_name: "Classic Box" }}),
        noMealCount: sizes({{ ...classic, meals_required: null }}, {{}}),
        handle: L.servingsChoices(classic, options, summary).find((c) => c.servings === 4).handle,
        noOptions: L.servingsChoices(classic, null, summary),
      }};
    """
    got = _run(body)
    assert got["people"] == [2, 4, None, None]
    classic = [[2, 3, 71.94, True], [4, 3, 131.88, False], [6, 3, None, False]]
    assert got["classic"] == classic
    assert got["namedByThePlan"] == classic
    assert got["oneFamilySellsIt"] == [[1, 3, 40, True], [2, 3, 68.94, False]]
    # Each size once, from the first family listing it.
    assert got["familyUnknown"] == [
        [1, 3, 40, False],
        [2, 3, 71.94, True],
        [4, 3, 131.88, False],
        [6, 3, None, False],
    ]
    assert got["fromThePlan"] == classic
    assert got["noMealCount"] == []
    assert got["handle"] == "US-CBU-3-4-0"
    assert got["noOptions"] == []


# ---- recipes, a page at a time --------------------------------------------------------------------


def test_catalog_pages_say_whether_more_follow_and_add_each_recipe_once() -> None:
    """has_more decides Load more; an answer without it (an integration from before paging)
    can't be paged, though a full page of it is still "the first N". A page repeating a recipe
    the list has adds it once."""
    body = """
      const page = (extra) => L.catalogPage({ recipes: [{ recipe_id: "a" }, { recipe_id: "b" }], ...extra }, 2);
      const keep = ({ total, more, capped, recipes }) => ({ total, more, capped, recipes: recipes.length });
      const added = L.addRecipes([{ recipe_id: "a" }, { recipe_id: "b" }], [{ recipe_id: "b" }, { recipe_id: "c" }, null, { recipe_id: "c" }]);
      return {
        paged: keep(page({ total: 5, has_more: true, offset: 0 })),
        last: keep(page({ total: 5, has_more: false })),
        noTotal: keep(page({ total: null, has_more: true })),
        badTotal: keep(page({ total: "5", has_more: true })),
        beforePaging: keep(page({})),
        short: keep(L.catalogPage({ recipes: [{ recipe_id: "a" }] }, 2)),
        nothing: keep(L.catalogPage(null, 2)),
        added: [added.list.map((r) => r.recipe_id), added.added.map((r) => r.recipe_id)],
      };
    """
    assert _run(body) == {
        "paged": {"total": 5, "more": True, "capped": True, "recipes": 2},
        "last": {"total": 5, "more": False, "capped": False, "recipes": 2},
        "noTotal": {"total": None, "more": True, "capped": True, "recipes": 2},
        "badTotal": {"total": None, "more": True, "capped": True, "recipes": 2},
        "beforePaging": {"total": None, "more": False, "capped": True, "recipes": 2},
        "short": {"total": None, "more": False, "capped": False, "recipes": 1},
        "nothing": {"total": None, "more": False, "capped": False, "recipes": 0},
        "added": [["a", "b", "c"], ["c"]],
    }


def test_the_line_above_the_recipes_counts_what_is_shown_against_the_total() -> None:
    body = """
      const note = (args) => { const n = L.catalogNote(args); return n ? L.t(n.key, n.params) : null; };
      return [
        note({ count: 24, total: 1234, capped: true }),
        note({ count: 24, total: 130, capped: true, search: true }),
        note({ count: 24, total: null, capped: true }),
        note({ count: 24, total: null, capped: true, search: true }),
        note({ count: 30, total: 30 }),
        note({ count: 30, total: 30, search: true }),
        note({ count: 7, total: null, capped: false }),
        note({ count: 1, total: null, capped: false, search: true }),
      ];
    """
    assert _run(body) == [
        "Showing 24 of 1,234 recipes",
        "Showing 24 of 130 results across every category",
        "Showing the first 24 recipes",
        "Showing the first 24 results across every category",
        None,  # a whole category says nothing
        "30 results across every category",
        None,
        "1 result across every category",
    ]


# ---- the account: payments, credit, rewards -------------------------------------------------------


def test_a_payment_problem_names_its_boxes_by_date_and_amount() -> None:
    """Each box by the summary's delivery date, else the card's own week's, else its week id;
    each amount in its own currency (the account's when it has none)."""
    body = """
      const weeks = [week("2026-W39", -2)];
      const summary = { payment_problem: true, account_credit_currency: "EUR", payment_problem_weeks: [
        { week_id: "2026-W40", delivery_date: "2026-09-28", status: "payment_failed", amount: 84.93, currency: "USD" },
        { week_id: "2026-W39", delivery_date: null, status: "CHARGEBACK", amount: null, currency: null },
        { week_id: "2026-W01", delivery_date: null, status: "failed", amount: "12.5", currency: null },
        null,
        { week_id: null, delivery_date: null, status: "failed", amount: 1 },
      ] };
      return {
        problem: L.paymentProblem(summary, weeks),
        w39: L.fmtLongDate(weeks[0].delivery_date),
        none: [L.paymentProblem({ payment_problem: false, payment_problem_weeks: summary.payment_problem_weeks }, weeks), L.paymentProblem(null, weeks)],
        bare: L.paymentProblem({ payment_problem: true }, weeks),
        list: [L.listText(["A"]), L.listText(["A", "B"]), L.listText(["A", "B", "C"])],
      };
    """
    got = _run(body)
    assert got["problem"] == {
        "boxes": [
            {
                "weekId": "2026-W40",
                "date": "Monday, September 28",
                "amount": "$84.93",
                "status": "payment_failed",
            },
            {"weekId": "2026-W39", "date": got["w39"], "amount": "", "status": "chargeback"},
            {"weekId": "2026-W01", "date": "2026-W01", "amount": "€12.50", "status": "failed"},
        ]
    }
    assert got["none"] == [None, None]
    assert got["bare"] == {"boxes": []}
    assert got["list"] == ["A", "A and B", "A, B, and C"]


def test_credit_lists_cash_bonus_and_the_boxes_left_until_a_free_one() -> None:
    body = """
      const base = { account_credit_currency: "EUR" };
      return [
        L.creditDetails({ ...base, account_credit_cash: 10, account_credit_bonus: 2.5, loyalty_boxes_until_free_box: 3 }),
        L.creditDetails({ ...base, account_credit_cash: 0, account_credit_bonus: null, loyalty_boxes_until_free_box: 1 }),
        L.creditDetails({ ...base, loyalty_boxes_until_free_box: 0 }),
        L.creditDetails({ ...base, account_credit_cash: "x", loyalty_boxes_until_free_box: null }),
        L.creditDetails({ selected_plan_total_price_currency: "USD", account_credit_bonus: 5 }),
        L.creditDetails(null),
      ];
    """
    assert _run(body) == [
        [
            ["HelloFresh Cash", "€10.00"],
            ["Bonus credit", "€2.50"],
            ["Rewards", "3 boxes until a free box"],
        ],
        [["Rewards", "1 box until a free box"]],
        [["Rewards", "You've earned a free box"]],
        [],
        [["Bonus credit", "$5.00"]],
        [],
    ]


# ---- a box HelloFresh moved ---------------------------------------------------------------------


def test_a_moved_box_says_when_it_now_arrives() -> None:
    """DELAYED or EARLY with the new date (any case), read as a local date; nothing for another
    sub-status, a box without the new date, or one that came or was skipped."""
    body = """
      const late = week("a", 1, { sub_status: "DELAYED", revised_delivery_date: "2026-10-06" });
      const early = week("b", 1, { sub_status: "early", revised_delivery_date: "2026-10-04" });
      return {
        late: [L.revisedDelivery(late), L.revisedDeliveryText(late), L.revisedDeliveryText(late, { long: false }),
          L.revisedDeliveryText(late, { markup: true })],
        early: L.revisedDeliveryText(early),
        others: [
          L.revisedDelivery(week("c", 1, { sub_status: "DELAYED" })),
          L.revisedDelivery(week("d", 1, { sub_status: "COOK_IT", revised_delivery_date: "2026-10-06" })),
          L.revisedDelivery(week("e", -1, { sub_status: "DELAYED", revised_delivery_date: "2026-10-06", delivered_at: new Date().toISOString() })),
          L.revisedDelivery(week("f", 1, { sub_status: "DELAYED", revised_delivery_date: "2026-10-06", is_skipped: true })),
          L.revisedDelivery(week("g", 1, { sub_status: "DELAYED", revised_delivery_date: "soon" })),
          L.revisedDelivery(null),
        ],
        plain: L.revisedDeliveryText(week("h", 1)),
      };
    """
    got = _run(body, tz="America/Los_Angeles")
    assert got["late"] == [
        {"kind": "delayed", "date": "2026-10-06"},
        "Delayed — now arriving Tuesday, October 6",
        "Delayed — now arriving Tue, Oct 6",
        "Delayed — now arriving <strong>Tuesday, October 6</strong>",
    ]
    assert got["early"] == "Early — now arriving Sunday, October 4"
    assert got["others"] == [None] * 6
    assert got["plain"] == ""


# ---- the text layer ----------------------------------------------------------------------------


def test_english_words_follow_english_plural_rules_in_any_language() -> None:
    """Home Assistant fills a key a language lacks with its English text, and English words need
    English plural rules: under French rules 0 is "one" ("0 meal"), under Russian so is 21 ("21
    meal"). A language's own words keep its own rules. A count that arrives as a string ("3",
    read from a data attribute) still picks a form, where it used to show the key."""
    body = """
      const I18n = L.I18n;
      const count = (values) => values.map((value) => L.t("meals.count", { count: value }));
      const out = {};
      I18n.setStrings({ "meta.language": "fr" }); // no French "meals.count": the English one
      out.french = count([0, 1, 2]);
      I18n.setStrings({ "meta.language": "ru" });
      out.russian = count([1, 21, 22]);
      I18n.setStrings({ "meta.language": "fr", "meals.count.one": "{count} plat", "meals.count.other": "{count} plats" });
      out.ownWords = count([0, 1, 2]);
      I18n.setStrings(null);
      out.strings = [...count(["3", "1"]), L.ht("meals.count", { count: " 2 " })];
      out.noCount = [L.t("meals.count"), ...count([undefined, null, "many"])];
      return out;
    """
    out = _run(body)
    assert out["french"] == ["0 meals", "1 meal", "2 meals"]
    assert out["russian"] == ["1 meal", "21 meals", "22 meals"]
    assert out["ownWords"] == ["0 plat", "1 plat", "2 plats"]
    assert out["strings"] == ["3 meals", "1 meal", "2 meals"]
    # Without a usable count it still reads as words, never as its key.
    assert all("meals.count" not in text and text.endswith("meals") for text in out["noCount"])
