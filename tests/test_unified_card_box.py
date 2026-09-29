"""The unified card's pending box (``BoxStore``) and its box bar.

Menu and Market edit one pending box per week and save it with one button. These run the real
``hellofresh-card-menu.js`` under Node against a stand-in card (the store touches the card only
through a handful of methods), covering what a regression would silently break:

* which service a save calls — meals only, extras only, or both in ONE ``select_meals`` write;
* the two-meal floor, servings clamping, sold-out and max-quantity guards;
* that a background refresh keeps a half-built box, and drops only what can no longer apply;
* that a save racing an in-flight refresh keeps showing what was saved (the committed overlay)
  until a fetch begun after the save lands;
* what the box bar says in each state.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

WWW = Path(__file__).resolve().parents[1] / "custom_components" / "hellofresh" / "www"
MENU = WWW / "hellofresh-card-menu.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

PREAMBLE = """
globalThis.window = { localStorage: { getItem: () => null, setItem() {} }, dispatchEvent() { return true; } };
globalThis.CustomEvent = class { constructor(t, i) { this.type = t; this.detail = (i || {}).detail; } };
globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);
const future = new Date(Date.now() + 5 * 86400000).toISOString();
function makeWeek(extra = {}) {
  const d = new Date(Date.now() + 6 * 86400000);
  const iso = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  return {
    week_id: "2026-W41", delivery_date: iso, selection_deadline: future, meals_required: 3,
    allowed_actions: { mealSwap: true },
    recipes: [1, 2, 3, 4, 5].map((i) => ({
      recipe_id: `r${i}`, course_index: i, name: `Meal ${i}`,
      is_selected: i <= 3, selected_quantity: i <= 3 ? 1 : null,
    })),
    market_items: [
      { item_id: "m1", name: "Bread", price_cents: 499, price: 4.99, max_quantity: 2, is_selected: true, selected_quantity: 1 },
      { item_id: "m2", name: "Cake", price_cents: 699, price: 6.99, max_quantity: 6 },
      { item_id: "m3", name: "Gone", price_cents: 199, price: 1.99, is_sold_out: true },
    ],
    ...extra,
  };
}
function makeCard(weeks, { fail = null, downgraded = false } = {}) {
  const card = {
    busy: false, calls: [], toasts: [], fetchSeq: 1, weeks, account: { selected_plan_total_price: 84.93 },
    config: { views: ["menu", "market"] }, sheetKind: null,
    weekById(id) { return this.weeks.find((w) => w.week_id === id) || null; },
    async call(service, data) {
      this.calls.push([service, JSON.parse(JSON.stringify(data))]);
      if (fail === service) throw new Error("upstream said no");
      return service === "preview_meal_price" ? { grand_total: 100 } : { downgraded };
    },
    setSaving(text) { this.busy = Boolean(text); },
    renderView() {}, renderBoxBar() {}, renderSheet() {}, closeSheet() {}, broadcastDataChanged() {},
    toast(message, isError = false) { this.toasts.push([message, isError]); },
    async reloadWeeks() {},
    hasView(v) { return this.config.views.includes(v); },
  };
  return card;
}
"""


def _run(body: str) -> object:
    script = f"""
    {PREAMBLE}
    const M = await import({json.dumps(MENU.as_uri() + "?v=test")});
    const result = await (async () => {{ {body} }})();
    console.log(JSON.stringify(result));
    """
    out = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "TZ": "America/New_York"},
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_meals_only_save_calls_select_meals_without_market() -> None:
    body = """
      const week = makeWeek();
      const card = makeCard([week]);
      const box = new M.BoxStore(card);
      box.addMeal(week, week.recipes[3]);
      box.changeMeal(week, week.recipes[0], 1);
      await box.save(week);
      return card.calls;
    """
    assert _run(body) == [
        [
            "select_meals",
            {
                "week_id": "2026-W41",
                "recipe_ids": ["r1", "r2", "r3", "r4"],
                "quantities": {"r1": 2},
            },
        ]
    ]


def test_extras_only_save_calls_select_market_items_with_the_full_state() -> None:
    body = """
      const week = makeWeek();
      const card = makeCard([week]);
      const box = new M.BoxStore(card);
      box.changeMarket(week, week.market_items[1], 1);
      await box.save(week);
      return card.calls;
    """
    assert _run(body) == [
        ["select_market_items", {"week_id": "2026-W41", "quantities": {"m1": 1, "m2": 1}}]
    ]


def test_meals_and_extras_save_in_one_write() -> None:
    body = """
      const week = makeWeek();
      const card = makeCard([week]);
      const box = new M.BoxStore(card);
      box.addMeal(week, week.recipes[4]);
      box.changeMarket(week, week.market_items[0], -1);
      await box.save(week);
      return card.calls;
    """
    assert _run(body) == [
        [
            "select_meals",
            {
                "week_id": "2026-W41",
                "recipe_ids": ["r1", "r2", "r3", "r5"],
                "market_quantities": {},
            },
        ]
    ]


def test_below_two_meals_is_refused_before_any_write() -> None:
    body = """
      const week = makeWeek();
      const card = makeCard([week]);
      const box = new M.BoxStore(card);
      box.changeMeal(week, week.recipes[0], -1);
      box.changeMeal(week, week.recipes[1], -1);
      await box.save(week);
      return [card.calls.length, card.toasts, box.dirty(week)];
    """
    calls, toasts, dirty = _run(body)
    assert calls == 0 and dirty
    assert toasts and "at least 2 meals" in toasts[0][0] and toasts[0][1] is True


def test_steppers_clamp_and_sold_out_items_only_go_down() -> None:
    body = """
      const week = makeWeek();
      const box = new M.BoxStore(makeCard([week]));
      for (let i = 0; i < 9; i++) box.changeMeal(week, week.recipes[0], 1);
      for (let i = 0; i < 9; i++) box.changeMarket(week, week.market_items[0], 1);
      const soldOutUp = box.changeMarket(week, week.market_items[2], 1);
      return [box.displayMeals(week).get(1), box.displayMarket(week).get("m1"), soldOutUp];
    """
    # 4 servings is HelloFresh's per-meal cap; Bread's own max is 2.
    assert _run(body) == [4, 2, False]


def test_a_locked_week_or_a_busy_card_takes_no_edits() -> None:
    body = """
      const locked = makeWeek({ allowed_actions: { mealSwap: false } });
      const card = makeCard([locked]);
      const box = new M.BoxStore(card);
      const a = box.addMeal(locked, locked.recipes[4]);
      const open = makeWeek();
      card.weeks = [open];
      card.busy = true;
      const b = box.addMeal(open, open.recipes[4]);
      return [a, b];
    """
    assert _run(body) == [False, False]


def test_customizing_a_dish_swaps_in_the_option_keeping_its_servings() -> None:
    """The drawer's "Update box": the cart gets the option's own meal in place of the old one."""
    body = """
      const week = makeWeek();
      const card = makeCard([week]);
      const box = new M.BoxStore(card);
      box.changeMeal(week, week.recipes[0], 1);                              // r1 at 2 servings
      const swapped = box.swapMeal(week, week.recipes[0], week.recipes[4]);  // r1 -> r5
      const stale = box.swapMeal(week, week.recipes[0], week.recipes[4]);    // r1 is gone now
      const merged = box.swapMeal(week, week.recipes[1], week.recipes[4]);   // r2 (1) joins r5 (2)
      const after = [...box.displayMeals(week).entries()];
      await box.save(week);
      const locked = makeWeek({ allowed_actions: { mealSwap: false } });
      const lockedSwap = new M.BoxStore(makeCard([locked])).swapMeal(locked, locked.recipes[0], locked.recipes[4]);
      return { swapped, stale, merged, after, calls: card.calls, lockedSwap };
    """
    got = _run(body)
    assert [got["swapped"], got["stale"], got["merged"], got["lockedSwap"]] == [
        True,
        False,
        True,
        False,
    ]
    assert got["after"] == [[3, 1], [5, 3]]
    assert got["calls"] == [
        [
            "select_meals",
            {"week_id": "2026-W41", "recipe_ids": ["r3", "r5"], "quantities": {"r5": 3}},
        ]
    ]


def test_a_failed_save_keeps_the_edit_for_a_retry() -> None:
    body = """
      const week = makeWeek();
      const card = makeCard([week], { fail: "select_meals" });
      const box = new M.BoxStore(card);
      box.addMeal(week, week.recipes[3]);
      await box.save(week);
      return [box.dirty(week), card.toasts.at(-1)];
    """
    dirty, toast = _run(body)
    assert dirty is True
    assert "upstream said no" in toast[0] and toast[1] is True


def test_a_downgrade_is_remembered_for_that_week() -> None:
    body = """
      const week = makeWeek();
      const card = makeCard([week], { downgraded: true });
      const box = new M.BoxStore(card);
      box.addMeal(week, week.recipes[3]);
      await box.save(week);
      return box.downgradedWeek;
    """
    assert _run(body) == "2026-W41"


def test_background_refresh_keeps_a_half_built_box() -> None:
    body = """
      const week = makeWeek();
      const box = new M.BoxStore(makeCard([week]));
      box.addMeal(week, week.recipes[3]);
      const refreshed = makeWeek();
      box.reconcile([refreshed], 2);
      const kept = box.dirty(refreshed) && box.displayMeals(refreshed).size === 4;
      // A refresh where the menu lost that meal drops just that meal; a now-locked week drops all.
      const shrunk = makeWeek({ recipes: makeWeek().recipes.slice(0, 3) });
      box.reconcile([shrunk], 3);
      const dropped = !box.dirty(shrunk);
      box.addMeal(shrunk, shrunk.recipes[0]);
      box.changeMeal(shrunk, shrunk.recipes[0], 1);
      const locked = makeWeek({ allowed_actions: { mealSwap: false } });
      box.reconcile([locked], 4);
      return [kept, dropped, box.dirty(locked)];
    """
    assert _run(body) == [True, True, False]


def test_committed_overlay_outlasts_a_stale_fetch() -> None:
    """A save lands while a fetch (begun before it) is in flight: that fetch's old data must not
    replace what was just saved; only a fetch begun AFTER the save may."""
    body = """
      const week = makeWeek();
      const card = makeCard([week]);
      card.fetchSeq = 7; // a fetch numbered 7 is in flight while we save
      const box = new M.BoxStore(card);
      box.addMeal(week, week.recipes[3]);
      await box.save(week);
      const stale = makeWeek(); // fetch 7 lands with the pre-save selection (3 meals)
      box.reconcile([stale], 7);
      const afterStale = [box.displayMeals(stale).size, box.dirty(stale)];
      const fresh = makeWeek(); // fetch 8, begun after the save, is authoritative
      fresh.recipes[3].is_selected = true;
      fresh.recipes[3].selected_quantity = 1;
      box.reconcile([fresh], 8);
      return [afterStale, [box.displayMeals(fresh).size, Boolean(box.committed[fresh.week_id])]];
    """
    assert _run(body) == [[4, False], [4, False]]


def test_box_bar_states() -> None:
    body = """
      const week = makeWeek();
      const card = makeCard([week]);
      card.box = new M.BoxStore(card);
      const strip = (html) => html.replace(/<[^>]+>/g, " ").replace(/\\s+/g, " ").trim();
      const saved = strip(M.renderBoxBar(card, week));
      card.box.addMeal(week, week.recipes[3]);
      const resized = strip(M.renderBoxBar(card, week));
      card.box.discard(week);
      card.box.changeMeal(week, week.recipes[0], -1);
      card.box.changeMeal(week, week.recipes[1], -1);
      const tooFew = M.renderBoxBar(card, week);
      const locked = M.renderBoxBar(card, makeWeek({ week_id: "2026-W42", allowed_actions: { mealSwap: false } }));
      // The deadline passes mid-edit: the same week, now locked, still holds the pending edit.
      week.selection_deadline = new Date(Date.now() - 60000).toISOString();
      const closed = M.renderBoxBar(card, week);
      await card.box.save(week);
      return [saved, resized, strip(tooFew), /data-action="box-save"[^>]*disabled/.test(tooFew), locked,
        strip(closed), /box-save/.test(closed), card.calls.length, card.toasts.at(-1)[0]];
    """
    saved, resized, too_few, save_disabled, locked, closed, closed_has_save, calls, toast = _run(
        body
    )
    assert "3 of 3 meals" in saved and "1 extra" in saved and "$84.93" in saved
    assert "4 of 3 meals" in resized and "resized to 4 meals" in resized and "Save box" in resized
    assert "at least 2 meals" in too_few and save_disabled
    assert locked == ""  # nothing to act on for a locked week with no pending edits
    assert "deadline passed" in closed and "Discard changes" in closed and not closed_has_save
    assert calls == 0 and "Changes are closed" in toast
