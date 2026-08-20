/**
 * Behavioural tests for the Lovelace cards' week-selection logic.
 *
 * `_browsableWeeks` decides which weeks a card lets you browse. It is pure (weeks in -> weeks
 * out) and therefore directly testable, which matters because every past-week browsing bug this
 * integration has had lived precisely here:
 *
 *   - the Market card dropped past weeks that carried no market catalog, so history collapsed to
 *     whatever the menu endpoint still served (~2 weeks) while My Menu spanned the full
 *     configured window;
 *   - a later attempt "fixed" it in a way that made the two cards disagree differently.
 *
 * The load-bearing invariant is that **the Market and My Menu cards agree on which past weeks
 * exist**. A user browsing history should see the same weeks in both. So rather than assert each
 * card's behaviour separately, the key test runs both real implementations over identical input
 * and requires identical output.
 *
 * The functions are extracted from the shipped card sources (not reimplemented) so these tests
 * cannot drift from what users actually run.
 *
 * Run: node .github/scripts/check_card_logic.mjs
 */

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const CARD_DIR = "custom_components/hellofresh/www";

/** Pull one method body out of a card source and turn it into a callable function. */
function extractMethod(file, name) {
  const src = fs.readFileSync(path.join(CARD_DIR, file), "utf8");
  // Match from `name(args) {` to the closing brace at method indentation (two spaces).
  const re = new RegExp(`\\n  ${name}\\(([^)]*)\\) \\{\\n([\\s\\S]*?)\\n  \\}`);
  const match = src.match(re);
  if (!match) throw new Error(`Could not extract ${name}() from ${file}`);
  const [, args, body] = match;
  // `this` inside the body resolves to the harness object supplied by the caller.
  return new Function(
    "helpers",
    `return function (${args}) {\n${body.replace(/\bthis\./g, "helpers.")}\n};`
  )(helpers);
}

// Minimal stand-ins for the card helpers `_browsableWeeks` calls. These mirror the real
// implementations in hellofresh-shared.js.
const helpers = {
  _parseLocalDate(value) {
    const [y, m, d] = String(value).split("-").map(Number);
    return new Date(y, m - 1, d);
  },
  _weekSortKey(week) {
    const ms = week && week.delivery_date ? helpers._parseLocalDate(week.delivery_date).getTime() : NaN;
    return Number.isNaN(ms) ? Number.POSITIVE_INFINITY : ms;
  },
};

const marketBrowsable = extractMethod("hellofresh-market-card.js", "_browsableWeeks");
const plannerBrowsable = extractMethod("hellofresh-meal-planner-card.js", "_browsableWeeks");

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

test("Market and My Menu agree on past weeks (the core regression)", () => {
  // 12 past weeks; only 3 had Market purchases, mirroring a real account where add-ons are
  // occasional. The Market card must NOT hide the other 9 -- that is the bug users reported as
  // "past weeks only shows 2 days, 9 days, 65 days ago".
  const weeks = [];
  for (let i = 12; i >= 1; i--) {
    weeks.push(week(`W-${i}`, -i, { market: [1, 2, 9].includes(i) ? 2 : 0, recipes: 3 }));
  }
  const marketIds = marketBrowsable(weeks).map((w) => w.week_id);
  const plannerIds = plannerBrowsable(weeks).map((w) => w.week_id);

  assert.deepEqual(
    marketIds,
    plannerIds,
    "Market and My Menu must expose the same past weeks; they disagreed:\n" +
      `  market : ${marketIds.join(" ")}\n  planner: ${plannerIds.join(" ")}`
  );
  assert.equal(marketIds.length, 12, "all 12 past weeks should be browsable");
});

test("a past week with no market items is still browsable", () => {
  const weeks = [week("past", -3, { market: 0, recipes: 3 })];
  assert.equal(marketBrowsable(weeks).length, 1, "empty past week must not vanish from the strip");
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
  assert.deepEqual(marketBrowsable(weeks).map((w) => w.week_id), ["f1", "f2"]);
  assert.deepEqual(plannerBrowsable(weeks).map((w) => w.week_id), ["f1", "f2"]);
});

test("weeks are returned in chronological order", () => {
  const weeks = [
    week("c", 1, { market: 2, recipes: 2 }),
    week("a", -2, { market: 2, recipes: 2 }),
    week("b", 0, { market: 2, recipes: 2 }),
  ];
  assert.deepEqual(marketBrowsable(weeks).map((w) => w.week_id), ["a", "b", "c"]);
});

test("today's week follows the future rule, and both cards agree", () => {
  // `isFuture` is `delivery_date >= today`, so the CURRENT week takes the future branch. That is
  // deliberate: today's box is still live and editable, and one with no data yet simply is not
  // published, so it is withheld rather than shown empty. What matters is that both cards make
  // the same call -- a disagreement here would desync the shared week cursor between them.
  const empty = [week("today", 0, { market: 0, recipes: 0 })];
  assert.equal(marketBrowsable(empty).length, 0, "unpublished current week is withheld");
  assert.equal(plannerBrowsable(empty).length, marketBrowsable(empty).length, "cards must agree");

  // Once it carries data it becomes browsable in both.
  const published = [week("today", 0, { market: 4, recipes: 4 })];
  assert.equal(marketBrowsable(published).length, 1);
  assert.equal(plannerBrowsable(published).length, 1);
});

test("empty and missing input never throws", () => {
  for (const value of [[], null, undefined]) {
    assert.deepEqual(marketBrowsable(value), []);
    assert.deepEqual(plannerBrowsable(value), []);
  }
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
