"""The unified card's pending box (``BoxStore``) and its box bar.

Menu and Market edit one pending box per week and save it with one button. These run the real
``hellofresh-card-menu.js`` under Node against a stand-in card (the store touches the card only
through a handful of methods), covering what a regression would silently break:

* which service a save calls — meals only, extras only, or both in ONE ``select_meals`` write;
* the two-meal floor, servings clamping, sold-out and max-quantity guards;
* that a background refresh keeps a half-built box, and drops only what can no longer apply;
* that a save racing an in-flight refresh keeps showing what was saved (the committed overlay)
  until a fetch begun after the save lands;
* that a draft whose saved box changed elsewhere is never saved as it stands (Keep mine /
  Use saved), for meals and for extras;
* what the box bar says in each state, including the live price estimate and what an
  extras-only change adds or takes off;
* that a save's Undo writes the old box back (one ``select_meals`` with its Market quantities, or
  ``select_market_items`` for extras alone) through the saving state, and is offered only when it
  can work: not for an old box below the minimum, past the deadline, after a failed save, or twice.
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
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const text = (html) => html.replace(/<[^>]+>/g, " ").replace(/\\s+/g, " ").trim();
// A data-action element as the click handlers read it.
const el = (attrs) => ({ getAttribute: (k) => (k in attrs ? attrs[k] : null) });
// Mark a meal or extra as saved in a week's data, as a refresh would bring it.
function choose(week, index, quantity = 1) {
  week.recipes[index].is_selected = quantity > 0;
  week.recipes[index].selected_quantity = quantity > 0 ? quantity : null;
}
function stock(week, index, quantity) {
  week.market_items[index].is_selected = quantity > 0;
  week.market_items[index].selected_quantity = quantity > 0 ? quantity : null;
}
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
    busy: false, calls: [], toasts: [], undos: [], reloads: [], saving: [], fetchSeq: 1, weeks, account: { selected_plan_total_price: 84.93 },
    config: { views: ["menu", "market"] }, sheetKind: null,
    weekById(id) { return this.weeks.find((w) => w.week_id === id) || null; },
    async call(service, data) {
      this.calls.push([service, JSON.parse(JSON.stringify(data))]);
      if (fail === service) throw new Error("upstream said no");
      return service === "preview_meal_price" ? { grand_total: 100 } : { downgraded };
    },
    setSaving(text) { this.busy = Boolean(text); this.saving.push(text); },
    renderView() {}, renderBoxBar() {}, renderSheet() {}, closeSheet() {}, broadcastDataChanged() {},
    // The card's toast: an `undo` offer ({weekId, run}) rides along, and is kept to press.
    toast(message, isError = false, { undo = null } = {}) { this.toasts.push([message, isError]); this.undos.push(undo); },
    // As the card's: an offer only while the week's deadline is ahead.
    weekUndo(weekId, run) {
      const week = this.weekById(weekId);
      return week && Date.parse(week.selection_deadline) > Date.now() ? { weekId, run } : null;
    },
    undoResult(failed) { this.toast(failed ? `Couldn't undo: ${failed}` : "Change undone.", Boolean(failed)); },
    async reloadWeeks(options = {}) { this.reloads.push(options.weekId || null); },
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
                "quantities": {"r1": 2, "r2": 1, "r3": 1, "r4": 1},
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
                "quantities": {"r1": 1, "r2": 1, "r3": 1, "r5": 1},
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


def test_a_sold_out_extra_in_the_saved_box_can_be_put_back_but_not_added_to() -> None:
    """HelloFresh keeps a sold-out add-on the saved box already holds for the customer.

    So taking it out without saving and putting it back works, but going above the saved
    quantity doesn't.
    """
    body = """
      const week = makeWeek();
      week.market_items[0].is_sold_out = true;  // Bread: 1 in the saved box, max 2
      const box = new M.BoxStore(makeCard([week]));
      const out = box.changeMarket(week, week.market_items[0], -1);
      const back = box.changeMarket(week, week.market_items[0], 1);
      const more = box.changeMarket(week, week.market_items[0], 1);
      return [out, back, more, box.displayMarket(week).get("m1")];
    """
    assert _run(body) == [True, True, False, 1]


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
            {
                "week_id": "2026-W41",
                "recipe_ids": ["r3", "r5"],
                "quantities": {"r3": 1, "r5": 3},
            },
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


def test_a_saved_box_offers_undo_which_writes_the_old_box_back_in_one_write() -> None:
    """The toast's Undo puts back the meals, servings and extras the box was saved with before,
    in one select_meals carrying market_quantities, through the card's saving state; it re-reads
    that week alone, shows the old box at once and offers no further undo."""
    body = """
      const week = makeWeek();
      week.recipes[0].selected_quantity = 2;
      const card = makeCard([week]);
      const box = new M.BoxStore(card);
      box.addMeal(week, week.recipes[3]);
      box.changeMarket(week, week.market_items[1], 1);
      await box.save(week);
      const offer = card.undos.at(-1);
      const before = card.calls.length;
      await offer.run();
      return {
        offer: offer.weekId, undo: card.calls.slice(before), toasts: card.toasts, saving: card.saving,
        reloads: card.reloads, lastOffer: card.undos.at(-1), shown: [...box.savedMeals(week).entries()],
        extras: [...box.savedMarket(week).entries()], busy: card.busy,
      };
    """
    got = _run(body)
    assert got["offer"] == "2026-W41"
    assert got["undo"] == [
        [
            "select_meals",
            {
                "week_id": "2026-W41",
                "recipe_ids": ["r1", "r2", "r3"],
                "quantities": {"r1": 2, "r2": 1, "r3": 1},
                "market_quantities": {"m1": 1},
            },
        ]
    ]
    assert got["toasts"] == [["Your box is saved.", False], ["Change undone.", False]]
    assert got["saving"] == ["Saving your box…", None, "Undoing…", None] and got["busy"] is False
    assert got["reloads"] == [None, "2026-W41"]
    assert got["lastOffer"] is None
    # Until a read begun after the undo lands, the box shows what the undo wrote.
    assert got["shown"] == [[1, 2], [2, 1], [3, 1]] and got["extras"] == [["m1", 1]]


def test_undo_follows_what_the_save_changed_and_is_offered_only_when_it_can_work() -> None:
    """Extras only: select_market_items. No Undo when the old box can't be written back (below
    the two-meal minimum), when the week's deadline passed meanwhile, or when the save failed;
    and an undo while another write runs waits rather than writing twice."""
    body = """
      const extras = makeWeek();
      const c1 = makeCard([extras]);
      const b1 = new M.BoxStore(c1);
      b1.changeMarket(extras, extras.market_items[0], -1);
      await b1.save(extras);
      await c1.undos.at(-1).run();

      const small = makeWeek();
      small.recipes.forEach((r, i) => { r.is_selected = i === 0; r.selected_quantity = i === 0 ? 1 : null; });
      const c2 = makeCard([small]);
      const b2 = new M.BoxStore(c2);
      b2.addMeal(small, small.recipes[1]);
      b2.addMeal(small, small.recipes[2]);
      await b2.save(small);

      const late = makeWeek();
      const c3 = makeCard([late]);
      c3.reloadWeeks = async () => { late.selection_deadline = new Date(Date.now() - 1000).toISOString(); };
      const b3 = new M.BoxStore(c3);
      b3.addMeal(late, late.recipes[3]);
      await b3.save(late);

      const failed = makeWeek();
      const c4 = makeCard([failed], { fail: "select_meals" });
      const b4 = new M.BoxStore(c4);
      b4.addMeal(failed, failed.recipes[3]);
      await b4.save(failed);

      const busy = makeWeek();
      const c5 = makeCard([busy]);
      const b5 = new M.BoxStore(c5);
      b5.addMeal(busy, busy.recipes[3]);
      await b5.save(busy);
      c5.busy = true;
      const calls = c5.calls.length;
      await c5.undos.at(-1).run();

      return { extras: c1.calls.at(-1), small: c2.undos, late: c3.undos, failed: c4.undos, busy: c5.calls.length - calls };
    """
    got = _run(body)
    assert got["extras"] == [
        "select_market_items",
        {"week_id": "2026-W41", "quantities": {"m1": 1}},
    ]
    assert got["small"] == [None]
    assert got["late"] == [None]
    assert got["failed"] == [None]
    assert got["busy"] == 0


def test_a_failed_undo_says_so() -> None:
    body = """
      const week = makeWeek();
      const card = makeCard([week]);
      const box = new M.BoxStore(card);
      box.changeMarket(week, week.market_items[1], 2);
      await box.save(week);
      card.call = async () => { throw new Error("HelloFresh is down"); };
      await card.undos.at(-1).run();
      return [card.toasts.at(-1), card.busy, [...box.savedMarket(week).entries()]];
    """
    toast, busy, market = _run(body)
    assert toast == ["Couldn't undo: HelloFresh is down", True]
    assert busy is False
    # Nothing was written, so the box still shows what the save wrote.
    assert market == [["m1", 1], ["m2", 2]]


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


# ---- a draft whose saved box changed somewhere else ----------------------------------------------


def test_a_draft_over_a_box_changed_elsewhere_is_not_saved_until_the_user_chooses() -> None:
    """A tablet adds Meal 4 and leaves the draft; the website swaps Meal 3 for Meal 5 meanwhile.

    The draft is a full snapshot, so saving it as it stands would bring Meal 3 back and drop
    Meal 5. The bar says the saved box changed and offers Keep mine / Use saved instead of Save,
    and a save attempted anyway (the review sheet, a stale button) writes nothing.
    """
    body = """
      const week = makeWeek();
      const card = makeCard([week]);
      const box = new M.BoxStore(card);
      card.box = box;
      box.addMeal(week, week.recipes[3]);                 // the tablet's draft: 1 2 3 4
      const fresh = makeWeek();                            // saved elsewhere: 1 2 5
      choose(fresh, 2, 0);
      choose(fresh, 4, 1);
      card.weeks = [fresh];
      box.reconcile([fresh], 2);
      const bar = M.renderBoxBar(card, fresh);
      await box.save(fresh);
      return {
        stale: box.stale(fresh), dirty: box.dirty(fresh), bar: text(bar),
        save: /data-action="box-save"/.test(bar), keep: /data-action="box-keep-mine"/.test(bar),
        useSaved: /data-action="box-use-saved"/.test(bar), alert: /role="alert"/.test(bar),
        calls: card.calls, toast: card.toasts.at(-1),
      };
    """
    got = _run(body)
    assert got["stale"] and got["dirty"]
    assert "Your saved box changed since you started" in got["bar"], got["bar"]
    assert got["keep"] and got["useSaved"] and not got["save"] and got["alert"]
    assert "Updating price" not in got["bar"]  # no price is asked for a draft nobody can save
    assert got["calls"] == []
    assert "changed since you started" in got["toast"][0] and got["toast"][1] is True


def test_keep_mine_puts_the_users_own_changes_on_top_of_the_box_saved_now() -> None:
    """Adds, removals and serving changes are the user's; everything else follows the saved box."""
    body = """
      const week = makeWeek();                             // saved: 1 2 3
      const card = makeCard([week]);
      const box = new M.BoxStore(card);
      box.addMeal(week, week.recipes[3]);                  // + Meal 4
      box.changeMeal(week, week.recipes[2], -1);           // - Meal 3
      box.changeMeal(week, week.recipes[0], 1);            // Meal 1 at 2 servings
      const fresh = makeWeek();                            // elsewhere: Meal 2 -> Meal 5
      choose(fresh, 1, 0);
      choose(fresh, 4, 1);
      card.weeks = [fresh];
      box.reconcile([fresh], 2);
      const stale = box.stale(fresh);
      box.rebase(fresh);
      const rebased = [...box.displayMeals(fresh).entries()].sort((a, b) => a[0] - b[0]);
      const after = [box.stale(fresh), box.dirty(fresh)];
      await box.save(fresh);
      return { stale, rebased, after, calls: card.calls };
    """
    got = _run(body)
    assert got["stale"] is True
    # Meal 1 at the user's 2 servings, Meal 3 still out, Meal 4 still in, Meal 5 from elsewhere
    # kept, Meal 2 gone as it was elsewhere.
    assert got["rebased"] == [[1, 2], [4, 1], [5, 1]]
    assert got["after"] == [False, True]
    [(service, data)] = got["calls"]
    assert service == "select_meals"
    assert sorted(data.pop("recipe_ids")) == ["r1", "r4", "r5"]  # a set; the order is the draft's
    assert data == {"week_id": "2026-W41", "quantities": {"r1": 2, "r4": 1, "r5": 1}}


def test_use_saved_drops_the_draft_and_an_identical_box_needs_no_choice() -> None:
    body = """
      const week = makeWeek();
      const card = makeCard([week]);
      card.box = new M.BoxStore(card);
      card.selectedWeek = () => card.weeks[0];
      card.box.addMeal(week, week.recipes[3]);
      const fresh = makeWeek();
      choose(fresh, 2, 0);
      choose(fresh, 4, 1);
      card.weeks = [fresh];
      card.box.reconcile([fresh], 2);
      M.onBoxBarClick(card, {}, el({ "data-action": "box-use-saved" }));
      const used = [card.box.dirty(fresh), [...card.box.displayMeals(fresh).keys()]];

      // The phone saved exactly what the tablet's draft holds: nothing is left to choose.
      const again = makeWeek();
      card.weeks = [again];
      card.box.addMeal(again, again.recipes[3]);
      const same = makeWeek();
      choose(same, 3, 1);
      card.weeks = [same];
      card.box.reconcile([same], 3);
      // A refresh where nothing moved keeps an ordinary draft.
      card.box.changeMeal(same, same.recipes[0], 1);
      card.box.reconcile([makeWeek({ recipes: same.recipes })], 4);
      return { used, identical: [card.box.stale(same), Boolean(card.box.meals[same.week_id])], unmoved: card.box.stale(same) };
    """
    got = _run(body)
    assert got["used"] == [False, [1, 2, 5]]
    assert got["identical"] == [False, True]  # the 2-serving edit after it is a fresh draft
    assert got["unmoved"] is False


def test_extras_drafts_are_protected_the_same_way() -> None:
    """A Market draft over extras changed elsewhere: no select_market_items until Keep mine."""
    body = """
      const week = makeWeek();                             // saved: Bread x1
      const card = makeCard([week]);
      const box = new M.BoxStore(card);
      box.changeMarket(week, week.market_items[1], 1);     // + Cake
      const fresh = makeWeek();
      stock(fresh, 0, 2);                                  // elsewhere: Bread x2
      card.weeks = [fresh];
      box.reconcile([fresh], 2);
      const stale = [box.stale(fresh), box.marketStale(fresh), box.mealsStale(fresh)];
      await box.save(fresh);
      const refused = card.calls.length;
      box.rebase(fresh);
      await box.save(fresh);
      return { stale, refused, calls: card.calls };
    """
    got = _run(body)
    assert got["stale"] == [True, True, False]
    assert got["refused"] == 0
    assert got["calls"] == [
        ["select_market_items", {"week_id": "2026-W41", "quantities": {"m1": 2, "m2": 1}}]
    ]


def test_the_review_sheet_offers_the_choice_and_keep_mine_stays_in_it() -> None:
    body = """
      const week = makeWeek();
      const card = makeCard([week]);
      card.box = new M.BoxStore(card);
      card.selectedWeek = () => card.weeks[0];
      card.openSheet = (sheet) => { card.sheet = sheet; card.sheetKind = sheet.kind; };
      card.closeSheet = () => { card.sheet = null; card.sheetKind = null; };
      card.box.addMeal(week, week.recipes[3]);
      const fresh = makeWeek();
      choose(fresh, 2, 0);
      choose(fresh, 4, 1);
      card.weeks = [fresh];
      card.box.reconcile([fresh], 2);
      M.onBoxBarClick(card, {}, el({ "data-action": "box-review" }));
      const stale = card.sheet.render();
      card.sheet.onClick({}, el({ "data-action": "review-keep-mine" }));
      const kept = card.sheet && card.sheet.render();
      return {
        staleText: text(stale), staleSave: /review-save/.test(stale),
        keepMine: /review-keep-mine/.test(stale), useSaved: /review-use-saved/.test(stale),
        open: Boolean(card.sheet), keptSave: /review-save/.test(kept), keptNotice: /changed since you started/.test(kept),
      };
    """
    got = _run(body)
    assert "Your saved box changed since you started" in got["staleText"]
    assert "another device or hellofresh.com" in got["staleText"]
    assert got["keepMine"] and got["useSaved"] and not got["staleSave"]
    assert got["open"] and got["keptSave"] and not got["keptNotice"]


# ---- the live price estimate ---------------------------------------------------------------


PRICED = """
  // HelloFresh's estimate: $10 a meal, after `delay` ms; `fail` makes it refuse.
  function priced(card, { delay = 0 } = {}) {
    card.previews = [];
    card.failPreview = false;
    card.call = async (service, data) => {
      card.calls.push([service, JSON.parse(JSON.stringify(data))]);
      if (service !== "preview_meal_price") return {};
      card.previews.push(data.recipe_ids.length);
      await sleep(delay);
      if (card.failPreview) throw new Error("preview unavailable");
      return { grand_total: 10 * data.recipe_ids.length };
    };
    return card;
  }
  const price = (card, week) => text((M.renderBoxBar(card, week).match(/hf-boxtotal">(.*?)<\\/div>/s) || [, ""])[1]);
"""


def test_a_background_refresh_prices_a_box_being_edited_again() -> None:
    """A refresh drops every price (they were asked against the old data); the box being edited
    gets a fresh one rather than reading "Updating price…" until the next edit."""
    body = (
        PRICED
        + """
      const week = makeWeek();
      const card = priced(makeCard([week]));
      card.box = new M.BoxStore(card);
      card.box.addMeal(week, week.recipes[3]);
      card.box.schedulePreview(week);
      await sleep(700);
      const first = price(card, week);
      const fresh = makeWeek();
      card.weeks = [fresh];
      card.box.reconcile([fresh], 2);
      const refreshed = price(card, fresh);
      await sleep(700);
      return { first, refreshed, after: price(card, fresh), previews: card.previews };
    """
    )
    got = _run(body)
    assert "$44.99" in got["first"] and "Estimated total" in got["first"]  # 4 meals + Bread
    assert "Updating price" in got["refreshed"]
    assert "$44.99" in got["after"] and got["previews"] == [4, 4]


def test_no_estimate_reads_unavailable_and_an_edit_asks_again() -> None:
    """A refused estimate, or a box below the minimum, is "unavailable", never a lasting
    "Updating price…". A refresh doesn't ask about a refused selection again; an edit does."""
    body = (
        PRICED
        + """
      const week = makeWeek();
      const card = priced(makeCard([week]));
      card.box = new M.BoxStore(card);
      const box = card.box;
      card.failPreview = true;
      box.addMeal(week, week.recipes[3]);
      box.schedulePreview(week);
      await sleep(700);
      const failed = price(card, week);
      box.reconcile([week], 2);
      await sleep(700);
      const afterRefresh = [price(card, week), card.previews.length];
      card.failPreview = false;
      box.changeMeal(week, week.recipes[3], -1);           // out and back in: the same selection
      box.addMeal(week, week.recipes[3]);
      box.schedulePreview(week);
      const retrying = price(card, week);
      await sleep(700);
      const retried = [price(card, week), card.previews.length];
      box.discard(week);
      box.changeMeal(week, week.recipes[0], -1);
      box.changeMeal(week, week.recipes[1], -1);
      box.schedulePreview(week);
      await sleep(700);
      const tooFew = text(M.renderBoxBar(card, week));
      return { failed, afterRefresh, retrying, retried, tooFew, previews: card.previews.length };
    """
    )
    got = _run(body)
    assert got["failed"] == "Estimate unavailable"
    assert got["afterRefresh"] == ["Estimate unavailable", 1]
    assert got["retrying"] == "Updating price…"
    assert "$44.99" in got["retried"][0] and got["retried"][1] == 2
    assert "Updating price" not in got["tooFew"] and "at least 2 meals" in got["tooFew"]
    assert got["previews"] == 2  # nothing asked for one meal


def test_a_price_for_a_selection_that_moved_on_is_dropped() -> None:
    """Discarding inside the debounce sends nothing; an answer for a box edited again while it
    was on its way is not shown for the new box."""
    body = (
        PRICED
        + """
      const week = makeWeek();
      const card = priced(makeCard([week]), { delay: 200 });
      card.box = new M.BoxStore(card);
      const box = card.box;
      box.addMeal(week, week.recipes[3]);
      box.schedulePreview(week);
      await sleep(100);
      box.discard(week);                                   // Discard, inside the 650 ms wait
      await sleep(700);
      const afterDiscard = card.previews.slice();
      box.addMeal(week, week.recipes[3]);                  // 4 meals: asked at 650 ms
      box.schedulePreview(week);
      await sleep(700);
      box.addMeal(week, week.recipes[4]);                  // 5 meals while the 4-meal ask is out
      box.schedulePreview(week);
      await sleep(250);                                    // the 4-meal answer has landed
      const meanwhile = price(card, week);
      await sleep(900);
      return { afterDiscard, meanwhile, final: price(card, week), previews: card.previews };
    """
    )
    got = _run(body)
    assert got["afterDiscard"] == []
    assert got["meanwhile"] == "Updating price…"  # not the 4-meal box's $44.99
    assert "$54.99" in got["final"] and got["previews"] == [4, 5]


# ---- extras: what a change costs, and how many there are -----------------------------------


def test_an_extras_change_shows_what_it_adds_or_takes_off() -> None:
    """Removing a $6.99 extra from a box that keeps a $4.99 one is −$6.99, not "+$4.99"; and
    "N extras" counts quantities (two loaves are two extras) in the bar and the review."""
    body = """
      const week = makeWeek();
      stock(week, 1, 1);                                   // saved: Bread x1, Cake x1
      const card = makeCard([week]);
      card.box = new M.BoxStore(card);
      card.selectedWeek = () => week;
      card.openSheet = (sheet) => { card.sheet = sheet; };
      card.box.changeMarket(week, week.market_items[1], -1);
      const removed = text(M.renderBoxBar(card, week));
      M.onBoxBarClick(card, {}, el({ "data-action": "box-review" }));
      const review = text(card.sheet.render());
      card.box.discard(week);
      card.box.changeMarket(week, week.market_items[0], 1);  // Bread x2 + Cake x1
      const added = text(M.renderBoxBar(card, week));
      const reviewAdded = text(card.sheet.render());
      return { removed, review, added, reviewAdded };
    """
    got = _run(body)
    assert "−$6.99 Extras" in got["removed"] and "+$4.99" not in got["removed"]
    assert "Extras (this change) −$6.99" in got["review"]
    assert "3 extras" in got["added"] and "+$4.99 Extras" in got["added"]
    assert "Extras · 3" in got["reviewAdded"]
