/**
 * Behavioural tests for the HelloFresh card's week-selection logic.
 *
 * `browsableWeeks` decides which weeks the card lets you browse. It is pure (weeks in -> weeks
 * out) and therefore directly testable, which matters because every past-week browsing bug this
 * integration has had lived precisely here:
 *
 *   - the classic Market card dropped past weeks that carried no market catalog, so history
 *     collapsed to whatever the menu endpoint still served (~2 weeks) while My Menu spanned the
 *     full configured window;
 *   - a later attempt "fixed" it in a way that made the two classic cards disagree differently.
 *
 * The load-bearing invariant is that **every past week in the configured window is browsable**,
 * whatever data it carries: one week list serves the Menu, the Market and the Overview at once.
 *
 * The function is imported from the shipped module (not reimplemented), so these tests cannot
 * drift from what users actually run.
 *
 * Run: node .github/scripts/check_card_logic.mjs
 */

import assert from "node:assert/strict";
import path from "node:path";
import { pathToFileURL } from "node:url";

const CARD_DIR = "custom_components/hellofresh/www";

const logic = await import(
  `${pathToFileURL(path.resolve(CARD_DIR, "hellofresh-card-logic.js")).href}?v=ci`
);
const browsable = (weeks) => logic.browsableWeeks(weeks).map((w) => w.week_id);

/** Build a week `weeksFromToday` in the past (negative) or future (positive). */
function week(id, weeksFromToday, { market = 0, recipes = 0 } = {}) {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  d.setDate(d.getDate() + weeksFromToday * 7);
  const iso = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  return {
    week_id: id,
    delivery_date: iso,
    market_items: Array.from({ length: market }, (_, i) => ({ item_id: `m${i}` })),
    recipes: Array.from({ length: recipes }, (_, i) => ({ recipe_id: `r${i}` })),
  };
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("every past week is browsable, Market data or not (the core regression)", () => {
  // 12 past weeks; only 3 had Market purchases, mirroring a real account where add-ons are
  // occasional, and some no longer carry a menu. Hiding any of them is the bug users reported as
  // "past weeks only shows 2 days, 9 days, 65 days ago".
  const weeks = [];
  for (let i = 12; i >= 1; i--) {
    weeks.push(week(`W-${i}`, -i, { market: [1, 2, 9].includes(i) ? 2 : 0, recipes: i % 4 ? 3 : 0 }));
  }
  assert.deepEqual(browsable(weeks), weeks.map((w) => w.week_id), "all 12 past weeks should be browsable");
});

test("a past week with no data at all is still browsable", () => {
  assert.deepEqual(browsable([week("past", -3)]), ["past"], "an empty past week must not vanish from the strip");
});

test("future weeks stop at the first gap in published data", () => {
  // HelloFresh publishes further out for deliveries than for menus/catalogs, so a future week
  // with no data means "not published yet" -- everything past it must be hidden, not skipped.
  const weeks = [
    week("f1", 1, { market: 5, recipes: 5 }),
    week("f2", 2, { market: 5, recipes: 5 }),
    week("f3", 3, { market: 0, recipes: 0 }), // gap
    week("f4", 4, { market: 5, recipes: 5 }), // published but behind the gap
  ];
  assert.deepEqual(browsable(weeks), ["f1", "f2"]);
});

test("weeks are returned in chronological order", () => {
  const weeks = [
    week("c", 1, { market: 2, recipes: 2 }),
    week("a", -2, { market: 2, recipes: 2 }),
    week("b", 0, { market: 2, recipes: 2 }),
  ];
  assert.deepEqual(browsable(weeks), ["a", "b", "c"]);
});

test("today's week follows the future rule", () => {
  // `isFuture` is `delivery_date >= today`, so the CURRENT week takes the future branch. That is
  // deliberate: today's box is still live and editable, and one with no data yet simply is not
  // published, so it is withheld rather than shown empty.
  assert.deepEqual(browsable([week("today", 0)]), [], "unpublished current week is withheld");
  // Once it carries data it becomes browsable.
  assert.deepEqual(browsable([week("today", 0, { market: 4, recipes: 4 })]), ["today"]);
});

test("empty and missing input never throws", () => {
  for (const value of [[], null, undefined]) {
    assert.deepEqual(browsable(value), []);
  }
});

test("skipped future weeks stay listed, and the list still stops at the first gap", () => {
  // A skipped week has no menu by design — hiding it would leave nothing to tap Unskip on (the
  // classic Schedule card's rule). Market data alone also makes a future week published.
  const skipped = { ...week("f2", 2), is_skipped: true };
  const weeks = [
    week("f1", 1, { market: 2, recipes: 2 }),
    skipped,
    week("f3", 3, { market: 3, recipes: 0 }), // Market published, menu not yet: still a week
    week("f4", 4), // gap
    week("f5", 5, { market: 2, recipes: 2 }), // behind the gap
  ];
  assert.deepEqual(browsable(weeks), ["f1", "f2", "f3"]);
});

let failed = 0;
for (const [name, fn] of tests) {
  try {
    fn();
    console.log(`ok    ${name}`);
  } catch (err) {
    failed++;
    console.log(`FAIL  ${name}`);
    console.error(`      ${err.message.split("\n").join("\n      ")}`);
  }
}

console.log(`\n${tests.length - failed}/${tests.length} card logic tests passed.`);
process.exit(failed ? 1 : 0);
