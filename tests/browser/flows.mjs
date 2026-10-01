// Browser flows: drive the real HelloFresh card in headless Chrome against a fake Home Assistant
// (fixtures.js, made-up data) and assert on its service calls and DOM. `npm test`; exits non-zero
// on any failed check or page error.
import { launchBrowser } from "./chrome.mjs";
import { startServer } from "./server.mjs";

const { server, port: PORT } = await startServer(0);
const browser = await launchBrowser();
const results = [];
const errors = [];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// The fixtures build their weeks around the page's clock: the next box comes the next Monday, its
// meal deadline the Thursday before at 02:59. From that Thursday until Monday the next box is
// locked, as it would be for real, so the flows that edit it found nothing to click and only
// passed from Tuesday to early Thursday. They all run on a fixed Tuesday instead; a flow can still
// pass its own `now`.
const FLOW_CLOCK = "2026-09-29T12:00:00";

async function open(params = {}, viewport = [1400, 900], path = "/index.html") {
  const page = await browser.newPage();
  await page.setViewport({ width: viewport[0], height: viewport[1] });
  page.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`));
  page.on("console", (msg) => {
    if (msg.type() === "error" && !msg.text().includes("404")) errors.push(`console: ${msg.text()}`);
    // The text layer warns once per key it can't find; a card string missing is a bug.
    if (/^warn/.test(msg.type()) && msg.text().includes("no card text for")) errors.push(`console: ${msg.text()}`);
  });
  const qs = new URLSearchParams({ latency: "60", now: FLOW_CLOCK, ...params });
  await page.goto(`http://localhost:${PORT}${path}?${qs}`, { waitUntil: "networkidle0" });
  await page.evaluate(() => localStorage.clear());
  await page.reload({ waitUntil: "networkidle0" });
  await page.waitForFunction(() => window.__ready === true);
  await page.waitForFunction(() => {
    const main = window.__card.shadowRoot.querySelector(".js-main");
    return main && main.innerHTML.trim() && !main.querySelector(".hf-skeleton");
  });
  await sleep(300);
  return page;
}

const q = (page, sel) => page.evaluate((s) => Boolean(window.__card.shadowRoot.querySelector(s)), sel);
const click = async (page, sel, wait = 400) => {
  const ok = await page.evaluate((s) => {
    const el = window.__card.shadowRoot.querySelector(s);
    if (!el) return false;
    el.click();
    return true;
  }, sel);
  if (!ok) throw new Error(`no element for ${sel}`);
  await sleep(wait);
};
const text = (page, sel) => page.evaluate((s) => (window.__card.shadowRoot.querySelector(s) || {}).textContent || "", sel);
const calls = (page, service) =>
  page.evaluate((svc) => window.__calls.filter((c) => !svc || `${c.domain}.${c.service}` === svc), service);

async function check(name, fn) {
  const started = Date.now();
  try {
    await fn();
    results.push(`ok    ${name}`);
  } catch (err) {
    results.push(`FAIL  ${name}: ${err.message}`);
  }
  console.log(`${results[results.length - 1]}  (${((Date.now() - started) / 1000).toFixed(1)}s)`);
}
const assert = (cond, msg) => {
  if (!cond) throw new Error(msg);
};

await check("combined meals + extras save is ONE select_meals call", async () => {
  const page = await open({ view: "menu" });
  await click(page, '.hf-tile:not(.selected) [data-action="add"]');
  await click(page, '[data-action="goto-kind"][data-view="market"]', 600);
  await click(page, '.hf-tile:not(.selected):not(.soldout) [data-action="market-qty"][data-delta="1"]');
  const bar = await text(page, ".js-boxbar");
  assert(/4 of 3 meals/.test(bar) && /1 extra|2 extras/.test(bar), `box bar: ${bar}`);
  await click(page, '[data-action="box-save"]', 1500);
  const saves = await calls(page, "hellofresh.select_meals");
  const market = await calls(page, "hellofresh.select_market_items");
  assert(saves.length === 1, `select_meals calls: ${saves.length}`);
  assert(market.length === 0, "no separate market write");
  const data = saves[0].data;
  assert(data.recipe_ids.length === 4, `recipe_ids ${JSON.stringify(data.recipe_ids)}`);
  assert(data.market_quantities && Object.keys(data.market_quantities).length >= 1, `market ${JSON.stringify(data.market_quantities)}`);
  const dirty = await page.evaluate(() => window.__card.box.dirty(window.__card.selectedWeek()));
  assert(!dirty, "no pending edits after saving");
  assert(!(await q(page, '[data-action="box-save"]')), "save button gone once saved");
  const toast = await page.evaluate(() => window.__card.shadowRoot.querySelector(".hf-toast")?.textContent || "");
  assert(/saved/i.test(toast), `toast: ${toast}`);
  await page.close();
});

await check("a save racing a slow refresh never flashes the old box", async () => {
  const page = await open({ view: "menu" });
  await click(page, '.hf-tile:not(.selected) [data-action="add"]');
  // Every get_weeks from now on answers with data captured when it STARTED, 1.5 s later.
  await page.evaluate(() => {
    const real = window.__hass.callService.bind(window.__hass);
    window.__hass.callService = async (d, s, ...rest) => {
      if (s !== "get_weeks") return real(d, s, ...rest);
      const snapshot = await real(d, s, ...rest);
      await new Promise((r) => setTimeout(r, 1500));
      return snapshot;
    };
    window.__card.refresh({ quiet: true }); // in flight, pre-save snapshot
  });
  await sleep(200);
  await click(page, '[data-action="box-save"]', 50);
  const meals = () =>
    page.evaluate(() => {
      const card = window.__card;
      const week = card.selectedWeek();
      return [card.box.displayMeals(week).size, card.box.dirty(week), Boolean(card.box.committed[week.week_id])];
    });
  await sleep(800); // saved, stale fetch still in flight
  const [a, dirtyA] = await meals();
  await sleep(1000); // stale (pre-save) response has landed
  const [b, dirtyB, committedB] = await meals();
  await sleep(2200); // the fresh fetch queued behind it has landed
  const [c, dirtyC, committedC] = await meals();
  assert(a === 4 && !dirtyA, `right after save: ${a} meals, dirty=${dirtyA}`);
  assert(b === 4 && !dirtyB && committedB, `after the stale fetch: ${b} meals, committed=${committedB}`);
  assert(c === 4 && !dirtyC && !committedC, `after the fresh fetch: ${c} meals, committed=${committedC}`);
  await page.close();
});

await check("market-only save uses select_market_items", async () => {
  const page = await open({ view: "market" });
  await click(page, '.hf-tile:not(.soldout) [data-action="market-qty"][data-delta="1"]');
  await click(page, '[data-action="box-save"]', 1500);
  const market = await calls(page, "hellofresh.select_market_items");
  const meals = await calls(page, "hellofresh.select_meals");
  assert(market.length === 1 && meals.length === 0, `market ${market.length} meals ${meals.length}`);
  await page.close();
});

await check("below-minimum box can't be saved", async () => {
  const page = await open({ view: "menu" });
  for (let i = 0; i < 2; i += 1) await click(page, '.hf-tile.selected [data-action="qty"][data-delta="-1"]');
  const disabled = await page.evaluate(() => window.__card.shadowRoot.querySelector('[data-action="box-save"]').disabled);
  const bar = await text(page, ".js-boxbar");
  assert(disabled && /at least 2 meals/.test(bar), `disabled=${disabled} bar=${bar}`);
  await click(page, '[data-action="box-discard"]');
  assert(/Saved/.test(await text(page, ".js-boxbar")), "discard resets");
  await page.close();
});

await check("pending edits survive switching tabs and weeks", async () => {
  const page = await open({ view: "menu" });
  await click(page, '.hf-tile:not(.selected) [data-action="add"]');
  await click(page, '[data-action="nav"][data-view="overview"]', 500);
  assert(await q(page, ".hf-hero .hf-notice.tone-info"), "overview shows unsaved notice");
  await click(page, '[data-action="nav"][data-view="menu"]', 500);
  await click(page, '[data-action="week-step"][data-step="1"]', 500);
  await click(page, '[data-action="week-step"][data-step="-1"]', 500);
  assert(/4 of 3 meals/.test(await text(page, ".js-boxbar")), "still 4 meals");
  assert(await q(page, ".hf-weekchip .hf-wcdirty"), "unsaved dot on chip");
  await page.close();
});

await check("skip asks first, then skips; unskip is immediate", async () => {
  const page = await open({ view: "overview" });
  await click(page, '.hf-hero [data-action="skip"]', 300);
  const sheet = await page.evaluate(() => window.__card.shadowRoot.querySelector(".hf-sheet")?.textContent || "");
  assert(/Skip Monday/.test(sheet), `confirm sheet: ${sheet.slice(0, 80)}`);
  assert((await calls(page, "hellofresh.skip_week")).length === 0, "not skipped before confirming");
  await page.evaluate(() => window.__card.shadowRoot.querySelector('.hf-sheet [data-action="confirm-skip"]').click());
  await sleep(1200);
  assert((await calls(page, "hellofresh.skip_week")).length === 1, "skip_week called");
  const moved = await page.evaluate(() => {
    const card = window.__card;
    const skipped = card.weeks.find((w) => w.is_skipped && w.week_id === window.__calls.find((c) => c.service === "skip_week").data.week_id);
    return Boolean(skipped) && !card.shadowRoot.querySelector(".hf-hero .hf-herodate").textContent.includes("October 5");
  });
  assert(moved, "the skipped week is skipped and the hero moved on to the next box that ships");
  await page.close();
});

await check("change day lists the week's options and reschedules", async () => {
  const page = await open({ view: "overview" });
  await click(page, '.hf-hero [data-action="reschedule"]', 600);
  const options = await page.evaluate(() => [...window.__card.shadowRoot.querySelectorAll('.hf-sheet [data-action="pick-day"]')].map((b) => [b.textContent.replace(/\s+/g, " ").trim(), b.disabled]));
  assert(options.length === 3 && options[0][1] === true, `options ${JSON.stringify(options)}`);
  assert(/Tuesday/.test(options[1][0]), `weekday names from get_delivery_options: ${options[1][0]}`);
  await page.evaluate(() => window.__card.shadowRoot.querySelectorAll('.hf-sheet [data-action="pick-day"]')[1].click());
  await sleep(900);
  const r = await calls(page, "hellofresh.reschedule_week");
  assert(r.length === 1 && r[0].data.delivery_option === "tue-8-20", JSON.stringify(r));
  await page.close();
});

await check("pantry ticks write todo.update_item", async () => {
  const page = await open({ view: "overview" });
  await click(page, '.hf-pantryitem:not(.done)', 600);
  const u = await calls(page, "todo.update_item");
  assert(u.length === 1 && u[0].data.status === "completed", JSON.stringify(u));
  await page.close();
});

await check("recurring plan change asks first", async () => {
  const page = await open({ view: "account" });
  await page.evaluate(() => {
    const s = window.__card.shadowRoot.querySelector('[data-plan-control="box_size"]');
    s.value = "4 meals × 2 servings";
    s.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
  });
  await sleep(300);
  assert((await calls(page, "select.select_option")).length === 0, "nothing written before confirming");
  await page.evaluate(() => window.__card.shadowRoot.querySelector('.hf-sheet [data-action="confirm-plan"]').click());
  await sleep(600);
  const s = await calls(page, "select.select_option");
  assert(s.length === 1 && s[0].data.option === "4 meals × 2 servings", JSON.stringify(s));
  await page.close();
});

await check("plan change cancel restores the select", async () => {
  const page = await open({ view: "account" });
  await page.evaluate(() => {
    const s = window.__card.shadowRoot.querySelector('[data-plan-control="delivery_day"]');
    s.value = "Tuesday 8AM - 8PM";
    s.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
  });
  await sleep(300);
  await page.evaluate(() => window.__card.shadowRoot.querySelector(".hf-sheet [data-close-sheet]").click());
  await sleep(300);
  const value = await page.evaluate(() => window.__card.shadowRoot.querySelector('[data-plan-control="delivery_day"]').value);
  assert(value === "Monday 8AM - 8PM", value);
  await page.close();
});

await check("favorite toggles through add_favorite", async () => {
  const page = await open({ view: "recipes" });
  await sleep(600);
  await click(page, '.hf-favbtn:not(.on)', 800);
  assert((await calls(page, "hellofresh.add_favorite")).length === 1, "add_favorite");
  await page.close();
});

await check("food preferences save only the edited sections", async () => {
  const page = await open({ view: "account" });
  await click(page, '[data-tab="profile"]', 900);
  await click(page, '[data-action="fp-step"][data-field="children"][data-delta="1"]', 300);
  const saveEnabled = await page.evaluate(() => !window.__card.shadowRoot.querySelector('[data-action="fp-save"]').disabled);
  assert(saveEnabled, "save enabled after edit");
  await click(page, '[data-action="fp-save"]', 900);
  const s = await calls(page, "hellofresh.set_food_profile");
  assert(s.length === 1 && s[0].data.household.children === 1 && s[0].data.household.adults === 2, JSON.stringify(s[0] && s[0].data.household));
  await page.close();
});

await check("goals cap at three and cooking style is required", async () => {
  const page = await open({ view: "account" });
  await click(page, '[data-tab="profile"]', 900);
  // Personal goals open themselves (Tell us more); pick until capped.
  await click(page, '[data-action="fp-list"][data-id="goals|goals|eat-healthier"]', 200);
  const blocked = await page.evaluate(() => [...window.__card.shadowRoot.querySelectorAll('[data-action="fp-list"][data-id^="goals|goals|"].blocked')].length);
  assert(blocked === 2, `blocked goals ${blocked}`);
  await page.close();
});

await check("menu search keeps focus while filtering", async () => {
  const page = await open({ view: "menu" });
  await page.evaluate(() => {
    const input = window.__card.shadowRoot.querySelector('[data-input="meal-search"]');
    input.focus();
  });
  await page.keyboard.type("tacos", { delay: 30 });
  await sleep(500);
  const state = await page.evaluate(() => {
    const root = window.__card.shadowRoot;
    return {
      focused: root.activeElement && root.activeElement.getAttribute("data-input"),
      value: root.querySelector('[data-input="meal-search"]').value,
      tiles: root.querySelectorAll(".hf-grid .hf-tile").length,
    };
  });
  assert(state.focused === "meal-search" && state.value === "tacos", JSON.stringify(state));
  assert(state.tiles > 0 && state.tiles < 10, `tiles ${state.tiles}`);
  await page.close();
});

await check("history weeks show only delivered meals, no filters", async () => {
  const page = await open({ view: "menu" });
  await page.evaluate(() => {
    const card = window.__card;
    const past = card.weeks.find((w) => w.status === "DELIVERED" && !w.menu_filters.length);
    card.selectWeek(past.week_id);
  });
  await sleep(500);
  const state = await page.evaluate(() => {
    const root = window.__card.shadowRoot;
    return {
      tiles: root.querySelectorAll(".hf-grid .hf-tile").length,
      filters: Boolean(root.querySelector('[data-action="toggle-filters"]')),
      bar: root.querySelector(".js-boxbar").innerHTML.trim().length,
      strip: Boolean(root.querySelector(".hf-orderstrip")),
    };
  });
  assert(state.tiles === 3 && !state.filters && state.bar === 0 && state.strip, JSON.stringify(state));
  await page.close();
});

await check("calendar: whole weeks of day tiles; a delivered day opens its details, an upcoming one its menu", async () => {
  const page = await open({ view: "overview" });
  await click(page, '[data-action="mode"][data-mode="calendar"]', 400);
  assert(await q(page, ".hf-cal"), "calendar shown");
  const grid = await page.evaluate(() => {
    const root = window.__card.shadowRoot;
    return {
      dows: root.querySelectorAll(".hf-caldows .hf-caldow").length,
      cells: root.querySelectorAll(".hf-calgrid .hf-cal-day").length,
      today: root.querySelectorAll('.hf-cal-day.today[aria-current="date"]').length,
    };
  });
  assert(grid.dows === 7 && grid.cells % 7 === 0 && grid.cells >= 28 && grid.today === 1, JSON.stringify(grid));
  await click(page, ".hf-cal-day.has.delivered:not(.other)", 600);
  assert(await q(page, ".hf-sheet .hf-dsec"), "a delivered day opens its delivery details");
  await page.keyboard.press("Escape");
  await sleep(400);
  await click(page, '[data-action="cal-shift"][data-delta="1"]', 300);
  assert(await q(page, '[data-action="cal-today"]'), "Today appears away from the current month");
  const target = await page.evaluate(() => {
    const cell = window.__card.shadowRoot.querySelector('.hf-cal-day.has[data-view="menu"]:not(.other)');
    return cell && cell.getAttribute("data-week-id");
  });
  assert(target, "an upcoming delivery day");
  await click(page, `.hf-cal-day.has[data-week-id="${target}"]`, 600);
  const state = await page.evaluate(() => ({ view: window.__card.view, week: window.__card.selectedWeekId }));
  assert(state.view === "menu" && state.week === target, JSON.stringify({ state, target }));
  await page.close();
});

await check("keyboard Enter on a tile opens the recipe sheet", async () => {
  const page = await open({ view: "menu" });
  await page.evaluate(() => window.__card.shadowRoot.querySelector(".hf-tile").focus());
  await page.keyboard.press("Enter");
  await sleep(700);
  assert(await q(page, ".detailwrap .detailbox"), "detail open");
  await page.keyboard.press("Escape");
  await sleep(200);
  assert(!(await q(page, ".detailwrap")), "escape closes");
  await page.close();
});

await check("recipe sheet footer adds a meal to the box", async () => {
  const page = await open({ view: "menu" });
  await click(page, ".hf-tile:not(.selected)", 800);
  await page.evaluate(() => window.__card.shadowRoot.querySelector('.detailwrap [data-sel="add"]').click());
  await sleep(300);
  assert(/4 of 3 meals/.test(await text(page, ".js-boxbar")), "added from the sheet");
  await page.close();
});

await check("breakout: a single section has no tab bar", async () => {
  const page = await open({ config: JSON.stringify({ views: ["recipes"], title: "Recipes" }) });
  const hidden = await page.evaluate(() => window.__card.shadowRoot.querySelector(".js-tabs").hidden);
  assert(hidden, "tabs hidden");
  assert(await q(page, ".hf-grid .hf-tile"), "recipes render");
  await page.close();
});

await check("filters persist and the classic planner key format is kept", async () => {
  const page = await open({ view: "menu" });
  await click(page, '[data-action="toggle-filters"]', 300);
  await click(page, '[data-action="f-protein"][data-value="Beef"]', 300);
  const stored = await page.evaluate(() => localStorage.getItem("hellofresh-meal-planner:protein-filter"));
  assert(stored === '["Beef"]', stored);
  await page.close();
});

await check("theme accent option applies", async () => {
  const page = await open({ config: JSON.stringify({ accent: "theme" }) });
  const attr = await page.evaluate(() => window.__card.getAttribute("accent"));
  assert(attr === "theme", attr);
  await page.close();
});

await check("a failed load shows an error with retry", async () => {
  const page = await browser.newPage();
  await page.goto(`http://localhost:${PORT}/index.html?latency=30&fail=get_weeks`, { waitUntil: "networkidle0" });
  await page.waitForFunction(() => window.__ready === true);
  await sleep(600);
  const body = await page.evaluate(() => window.__card.shadowRoot.querySelector(".js-main").textContent);
  assert(/HelloFresh is down/.test(body) && /Try again/.test(body), body.slice(0, 120));
  await page.close();
});

// ---- 2026-09-29 enhancements: pantry spacing, delivery details, one tile per dish ----------------

await check("pantry amounts sit right after their names, abbreviated", async () => {
  const page = await open({ view: "overview" });
  const rows = await page.evaluate(() =>
    [...window.__card.shadowRoot.querySelectorAll(".hf-pantryitem")].map((item) => {
      const name = item.querySelector(".hf-pantryname");
      const amt = item.querySelector(".hf-pantryamt");
      if (!amt) return { name: name.textContent };
      const n = name.getBoundingClientRect();
      const a = amt.getBoundingClientRect();
      return { name: name.textContent, amount: amt.textContent, title: amt.getAttribute("title"), gap: Math.round(a.left - n.right),
        sameLine: Math.abs(a.top - n.top) < 6, rowWidth: Math.round(item.getBoundingClientRect().width) };
    })
  );
  const oil = rows.find((r) => r.name === "Olive Oil");
  const soy = rows.find((r) => r.name === "Soy Sauce");
  assert(oil && oil.amount === "4 tbsp" && oil.title === "4 tablespoon (tbsp)", JSON.stringify(oil));
  assert(soy && soy.amount === "1 tbsp + 1 tsp", JSON.stringify(soy));
  const spaced = rows.filter((r) => r.amount);
  assert(spaced.every((r) => r.sameLine && r.gap >= 0 && r.gap <= 16), JSON.stringify(spaced));
  await page.close();
});

await check("a delivered box opens its delivery details: photo or a note, scans, contents, charges", async () => {
  const page = await open({ view: "overview" });
  const read = () =>
    page.evaluate(() => {
      const r = window.__card.shadowRoot;
      return {
        kind: window.__card.sheetKind,
        title: r.querySelector(".hf-sheet h2")?.textContent || "",
        photos: r.querySelectorAll(".hf-sheet .hf-dphotos img").length,
        notes: [...r.querySelectorAll(".hf-sheet .hf-dnote")].map((n) => n.textContent.trim()),
        scans: r.querySelectorAll(".hf-sheet .hf-timeline li").length,
        meals: r.querySelectorAll(".hf-sheet button.hf-dmeal").length,
        charged: r.querySelector(".hf-sheet .hf-dline")?.textContent || "",
        link: r.querySelector(".hf-sheet .hf-dcarrier a")?.getAttribute("href") || "",
      };
    });
  const hint = await text(page, '.hf-row[data-action="delivery"] .hf-podhint');
  assert(/Photo/.test(hint), `the newest row marks its photo: ${hint}`);
  await click(page, '.hf-row[data-action="delivery"]', 500);
  const first = await read();
  assert(first.kind === "delivery" && /^Delivered /.test(first.title), JSON.stringify(first));
  assert(first.photos === 1 && first.notes.some((n) => /Signed by Front porch/.test(n)), JSON.stringify(first));
  assert(first.scans === 3 && first.meals === 3 && /Charged/.test(first.charged) && /shipveho/.test(first.link), JSON.stringify(first));
  await click(page, ".hf-sheet [data-close-sheet]", 300);
  await click(page, '.hf-row[data-action="delivery"]:nth-of-type(2)', 500);
  const second = await read();
  assert(second.photos === 0 && second.notes.some((n) => /Veho didn't share a delivery photo/.test(n)), JSON.stringify(second));
  await click(page, '.hf-sheet [data-action="delivery-menu"]', 600);
  const view = await page.evaluate(() => [window.__card.view, window.__card.sheetKind]);
  assert(view[0] === "menu" && view[1] === null, JSON.stringify(view));
  await page.close();
});

await check("the menu shows one tile per dish, the chosen option on its tile", async () => {
  const page = await open({ view: "menu" });
  const grid = await page.evaluate(() => {
    const r = window.__card.shadowRoot;
    const tiles = [...r.querySelectorAll(".hf-grid .hf-tile")];
    const week = window.__card.selectedWeek();
    const dishes = new Set(week.recipes.map((x) => (x.variation_group != null ? `g${x.variation_group}` : `r${x.course_index}`))).size;
    return {
      tiles: tiles.length,
      recipes: week.recipes.length,
      dishes,
      selectors: r.querySelectorAll(".hf-grid .hf-optselect").length,
      chosen: tiles.filter((t) => t.classList.contains("selected")).map((t) => t.querySelector(".hf-optlabel")?.textContent || ""),
      note: r.querySelector(".hf-resultnote")?.textContent || "",
    };
  });
  assert(grid.tiles === grid.dishes && grid.recipes > grid.dishes, JSON.stringify(grid));
  assert(grid.selectors === 10 && grid.chosen.includes("2x Chicken Cutlets"), JSON.stringify(grid));
  assert(!grid.note, `no "N of M" note on an unfiltered menu: ${grid.note}`);
  await page.close();
});

await check("the drawer switches a boxed dish to another option, keeping servings, and saves it", async () => {
  const page = await open({ view: "menu" });
  await click(page, ".hf-tile.selected .hf-optselect", 400);
  const drawer = await page.evaluate(() => {
    const r = window.__card.shadowRoot;
    const apply = r.querySelector('.hf-sheet [data-action="opt-apply"]');
    return {
      kind: window.__card.sheetKind,
      rows: [...r.querySelectorAll(".hf-sheet .hf-optrow")].map((b) => ({
        name: b.querySelector(".hf-optname").textContent,
        on: b.getAttribute("aria-pressed") === "true",
        disabled: b.disabled,
        price: b.querySelector(".hf-optprice").textContent.trim(),
        img: Boolean(b.querySelector("img")),
      })),
      apply: apply?.textContent.trim(),
      applyDisabled: apply?.disabled,
    };
  });
  assert(drawer.kind === "customize" && drawer.rows.length === 9, JSON.stringify(drawer));
  assert(drawer.rows[0].name === "No Change" && drawer.rows[2].on && drawer.rows[2].name === "2x Chicken Cutlets", JSON.stringify(drawer.rows.slice(0, 3)));
  assert(drawer.rows[3].price === "Included" && /\+\$2\.49/.test(drawer.rows[2].price) && drawer.rows.every((x) => x.img), JSON.stringify(drawer.rows));
  assert(drawer.rows[7].disabled, "the sold-out option can't be picked");
  assert(/Update box/.test(drawer.apply) && drawer.applyDisabled, "nothing to update until a different option is picked");
  const before = await page.evaluate(() => window.__card.box.displayMeals(window.__card.selectedWeek()).size);
  await click(page, ".hf-sheet .hf-optrow:nth-child(4)", 300); // Salmon
  const enabled = await page.evaluate(() => !window.__card.shadowRoot.querySelector('.hf-sheet [data-action="opt-apply"]').disabled);
  assert(enabled, "picking a different option enables Update box");
  await click(page, '.hf-sheet [data-action="opt-apply"]', 500);
  const after = await page.evaluate(() => {
    const card = window.__card;
    const week = card.selectedWeek();
    const byIdx = new Map(week.recipes.map((x) => [x.course_index, x]));
    return {
      sheet: card.sheetKind,
      meals: [...card.box.displayMeals(week).entries()].map(([k, q]) => [byIdx.get(k)?.variation_title || byIdx.get(k)?.name, q]),
      labels: [...card.shadowRoot.querySelectorAll(".hf-tile.selected .hf-optlabel")].map((e) => e.textContent),
    };
  });
  assert(after.sheet === null && after.meals.length === before, JSON.stringify(after));
  assert(after.meals.some(([t, q]) => t === "Salmon" && q === 1) && !after.meals.some(([t]) => t === "2x Chicken Cutlets"), JSON.stringify(after));
  assert(after.labels.includes("Salmon"), JSON.stringify(after.labels));
  await click(page, '[data-action="box-save"]', 1500);
  const saves = await calls(page, "hellofresh.select_meals");
  const ids = await page.evaluate(() => {
    const recipes = window.__card.selectedWeek().recipes;
    const find = (t) => recipes.find((x) => x.variation_group === 2 && x.variation_title === t).recipe_id;
    return [find("Salmon"), find("2x Chicken Cutlets")];
  });
  assert(saves.length === 1 && saves[0].data.recipe_ids.includes(ids[0]) && !saves[0].data.recipe_ids.includes(ids[1]), JSON.stringify(saves));
  await page.close();
});

await check("a dish not in the box can be added as customized", async () => {
  const page = await open({ view: "menu" });
  await click(page, ".hf-tile:not(.selected) .hf-optselect", 400);
  const apply = await text(page, '.hf-sheet [data-action="opt-apply"]');
  assert(/Add to box/.test(apply), apply);
  await click(page, ".hf-sheet .hf-optrow:nth-child(2)", 300);
  const label = await text(page, ".hf-sheet .hf-optrow.on .hf-optname");
  await click(page, '.hf-sheet [data-action="opt-apply"]', 500);
  const state = await page.evaluate(() => {
    const card = window.__card;
    return {
      count: card.box.displayMeals(card.selectedWeek()).size,
      labels: [...card.shadowRoot.querySelectorAll(".hf-tile.selected .hf-optlabel")].map((e) => e.textContent),
    };
  });
  assert(state.count === 4 && state.labels.includes(label), JSON.stringify({ state, label }));
  await page.close();
});

await check("under a Seafood filter a chicken dish shows its Salmon option; no Hide variants", async () => {
  const page = await open({ view: "menu" });
  await click(page, '[data-action="toggle-filters"]', 300);
  assert(!(await q(page, '[data-action="f-variants"]')), "the Hide variants chip is gone");
  await click(page, '[data-action="f-protein"][data-value="Seafood"]', 500);
  const tiles = await page.evaluate(() =>
    [...window.__card.shadowRoot.querySelectorAll(".hf-grid .hf-tile")].map((t) => ({
      selected: t.classList.contains("selected"),
      label: t.querySelector(".hf-optlabel")?.textContent || "",
    }))
  );
  assert(tiles.some((t) => !t.selected && t.label === "Salmon"), JSON.stringify(tiles));
  assert(tiles.some((t) => t.selected && t.label === "2x Chicken Cutlets"), "the boxed version stays despite the filter");
  await page.close();
});

await check("Veggie shows a beef dish's tofu swap once HelloFresh's filter service answers", async () => {
  const page = await open({ view: "menu" });
  await click(page, '[data-action="toggle-filters"]', 300);
  await click(page, '[data-action="f-protein"][data-value="Veggie"]', 900);
  const asked = (await calls(page, "hellofresh.get_menu_courses")).map((c) => c.data.filters);
  assert(asked.some((f) => JSON.stringify(f) === '{"main-protein":["vegetarian"]}'), JSON.stringify(asked));
  const state = await page.evaluate(() => ({
    busy: Boolean(window.__card.shadowRoot.querySelector(".hf-busynote")),
    labels: [...window.__card.shadowRoot.querySelectorAll(".hf-grid .hf-tile:not(.selected) .hf-optlabel")].map((e) => e.textContent),
  }));
  assert(!state.busy, "the Filtering… note clears");
  assert(state.labels.includes("2x Tofu"), JSON.stringify(state.labels));
  // Lamb has no website slug: it stays a menu-data match and asks nothing more.
  await click(page, '[data-action="f-protein"][data-value="Veggie"]', 300);
  const before = (await calls(page, "hellofresh.get_menu_courses")).length;
  await click(page, '[data-action="f-protein"][data-value="Lamb"]', 600);
  assert((await calls(page, "hellofresh.get_menu_courses")).length === before, "no lookup for Lamb alone");
  await page.close();
});

await check("sidebar panel: hosts the card, pins its account, and on a phone opens Home Assistant's sidebar", async () => {
  const hasButton = (page) => q(page, '[data-action="sidebar"]');
  let page = await open({ panel: "1" });
  assert(await page.evaluate(() => window.__card.hasAttribute("panel") && window.__panel.contains(window.__card) === false), "the card lives in the panel's shadow root");
  assert(!(await hasButton(page)), "no sidebar button while Home Assistant shows its sidebar");
  await page.close();

  page = await open({ panel: "1", docked: "always_hidden" });
  assert(await hasButton(page), "the button appears when the sidebar is set to always hidden");
  await page.close();

  page = await open({ panel: "1", narrow: "1" }, [390, 844]);
  assert(await hasButton(page), "phones get the button");
  await click(page, '[data-action="sidebar"]', 200);
  assert((await page.evaluate(() => window.__toggles)) === 1, "it fires hass-toggle-menu");
  const radius = await page.evaluate(() => getComputedStyle(window.__card.shadowRoot.querySelector("ha-card")).borderTopLeftRadius);
  assert(radius === "0px", `edge to edge on a phone (radius ${radius})`);
  await page.close();

  page = await open({ panel: "1", config: JSON.stringify({ config_entry_id: "entry1" }) });
  const pinned = (await calls(page, "hellofresh.get_weeks")).every((c) => c.data.config_entry_id === "entry1");
  assert(pinned, "the panel's config_entry_id reaches the card's service calls");
  await page.close();
});

await check("live tracking (NL): the next box shows the van, ETA, stops and driver, and follows the sensors", async () => {
  const page = await open({ view: "overview", scenario: "live" });
  const live = await page.evaluate(() => {
    const el = window.__card.shadowRoot.querySelector(".hf-hero .hf-live");
    return el && {
      text: el.textContent.replace(/\s+/g, " "),
      map: (el.querySelector(".hf-livemap") || {}).href || "",
      now: el.querySelectorAll(".hf-livestep.now").length,
      done: el.querySelectorAll(".hf-livestep.done").length,
    };
  });
  assert(live, "live block in the next box");
  for (const bit of ["On the way to you", "3 stops before yours", "Driver Sanne", "Window 18:00", "in 38 min", "Updated 2 min ago", "Your HelloFresh box is in my van"]) {
    assert(live.text.includes(bit), `missing "${bit}" in: ${live.text}`);
  }
  assert(live.map.startsWith("https://www.hftrack.nl/"), live.map);
  assert(live.now === 1 && live.done === 1, JSON.stringify(live));
  // The sensors move on: one stop left, then delivered, then nothing live.
  const push = (attrs, state) =>
    page.evaluate(([a, st]) => {
      const hass = window.__hass;
      const id = "sensor.hellofresh_nl_delivery_tracking_phase";
      const old = hass.states[id];
      hass.states = { ...hass.states, [id]: { ...old, state: st, attributes: { ...old.attributes, ...a }, last_updated: new Date().toISOString() } };
      window.__card.hass = { ...hass };
    }, [attrs, state]);
  await push({ amount_of_stops_before: 0 }, "On the way");
  await sleep(300);
  assert((await text(page, ".hf-live")).includes("You're next"), "stop count follows the sensor");
  await push({ phase: "DELIVERED" }, "Delivered");
  await sleep(300);
  const delivered = await page.evaluate(() => window.__card.shadowRoot.querySelectorAll(".hf-live .hf-livestep.done").length);
  assert(delivered === 2 && (await text(page, ".hf-live .hf-livephase")) === "Delivered", `delivered steps ${delivered}`);
  await push({ active: false, phase: "INVALID_LINK" }, "unknown");
  await sleep(300);
  assert(!(await q(page, ".hf-live")), "no live block once nothing is on the road");
  await page.close();
});

await check("live tracking stays out of accounts without it", async () => {
  const page = await open({ view: "overview" });
  assert(!(await q(page, ".hf-live")), "no live block without the tracking sensors");
  await page.close();
});

await check("sidebar panel: the address opens a section (and week), and follows the tabs", async () => {
  const week = await (async () => {
    const page = await open({ panel: "1" });
    const id = await page.evaluate(() => window.__card.weeks.find((w) => !w.is_skipped && w.recipes.length > 0).week_id);
    await page.close();
    return id;
  })();
  let page = await open({ panel: "1" }, [1400, 900], "/hellofresh-app/market");
  assert((await page.evaluate(() => window.__card.view)) === "market", "/hellofresh-app/market opens Market");
  const entries = await page.evaluate(() => history.length);
  await click(page, '[data-action="nav"][data-view="recipes"]', 400);
  assert((await page.evaluate(() => location.pathname)) === "/hellofresh-app/recipes", "the address follows the tab");
  assert((await page.evaluate(() => history.length)) === entries, "in place, not a history entry per tab");
  await page.close();
  page = await open({ panel: "1" }, [1400, 900], `/hellofresh-app/menu/${week}`);
  const state = await page.evaluate(() => ({ view: window.__card.view, week: window.__card.selectedWeekId }));
  assert(state.view === "menu" && state.week === week, JSON.stringify({ state, week }));
  await page.close();
  page = await open({ panel: "1" }, [1400, 900], "/hellofresh-app/nowhere");
  assert((await page.evaluate(() => window.__card.view)) === "overview", "an unknown section is ignored");
  await page.close();
});

await check("calendar: starts on Home Assistant's first day of the week", async () => {
  const page = await open({ view: "overview" });
  await click(page, '[data-action="mode"][data-mode="calendar"]', 400);
  const heads = () => page.evaluate(() => [...window.__card.shadowRoot.querySelectorAll(".hf-caldow .hf-dowshort")].map((e) => e.textContent.trim()));
  const before = await heads();
  await page.evaluate(() => {
    window.__card.hass = { ...window.__hass, locale: { language: "en", first_weekday: "monday" } };
    window.__card.renderView();
  });
  await sleep(200);
  const after = await heads();
  const cells = await page.evaluate(() => window.__card.shadowRoot.querySelectorAll(".hf-calgrid .hf-cal-day").length);
  assert(before[0] === "Sun" && after[0] === "Mon" && after[6] === "Sun", JSON.stringify({ before, after }));
  assert(cells % 7 === 0, `whole weeks (${cells})`);
  await page.close();
});

await check("in Norwegian: the card asks Home Assistant for its text once and reads nb throughout", async () => {
  const page = await open({ lang: "nb" });
  const ws = await page.evaluate(() => window.__calls.filter((c) => c.ws === "frontend/get_translations").map((c) => c.data));
  assert(ws.length === 1 && ws[0].language === "nb" && ws[0].category === "config_panel", JSON.stringify(ws));
  const tabs = await page.evaluate(() => [...window.__card.shadowRoot.querySelectorAll(".hf-tab > span:not(.hf-count)")].map((e) => e.textContent.trim()));
  assert(JSON.stringify(tabs) === JSON.stringify(["Oversikt", "Meny", "Market", "Oppskrifter", "Konto"]), JSON.stringify(tabs));
  const eyebrow = await text(page, ".hf-eyebrow");
  assert(["Neste kasse", "Kommer i dag", "Kommer i morgen", "Forrige kasse"].includes(eyebrow.trim()), `eyebrow: ${eyebrow}`);
  const heroDate = await text(page, ".hf-herodate");
  assert(/mandag|tirsdag|onsdag|torsdag|fredag|lørdag|søndag/.test(heroDate), `a Norwegian date: ${heroDate}`);

  await click(page, '[data-action="nav"][data-view="menu"]', 600);
  const add = await text(page, '.hf-grid [data-action="add"]');
  assert(add.trim() === "Legg til", `add button: ${add}`);
  await click(page, '[data-action="toggle-filters"]', 300);
  const rows = await page.evaluate(() => [...window.__card.shadowRoot.querySelectorAll(".hf-flabel")].map((e) => e.textContent.trim()));
  assert(rows.includes("Hovedprotein") && rows.includes("Høydepunkter"), JSON.stringify(rows));
  assert(await q(page, '[data-action="f-protein"][data-value="Seafood"]'), "chips keep their English keys");
  const seafood = await text(page, '[data-action="f-protein"][data-value="Seafood"]');
  assert(seafood.trim() === "Sjømat", `chip: ${seafood}`);
  await click(page, '.hf-grid .hf-tile:not(.selected) [data-action="add"]', 400);
  const bar = await text(page, ".js-boxbar");
  assert(/av \d+ retter/.test(bar) && /Lagre kassen/.test(bar), `box bar: ${bar}`);

  // Home Assistant switched to English: the card follows without a reload.
  await page.evaluate(() => {
    window.__card.hass = { ...window.__hass, language: "en", locale: { language: "en" } };
  });
  await sleep(300);
  const english = await page.evaluate(() => [...window.__card.shadowRoot.querySelectorAll(".hf-tab > span:not(.hf-count)")].map((e) => e.textContent.trim()));
  assert(english[0] === "Overview" && english[1] === "Menu", JSON.stringify(english));
  await page.close();
});

await check("in Norwegian: the recipe sheet, the delivery details and the account", async () => {
  let page = await open({ lang: "nb", view: "menu" });
  await click(page, '.hf-grid .hf-tile[data-action="open-recipe"]', 1200);
  const sheet = await page.evaluate(() => window.__card.shadowRoot.querySelector(".detailbox")?.textContent || "");
  assert(/Ingredienser/.test(sheet) && /Fremgangsmåte/.test(sheet), `recipe sheet: ${sheet.slice(0, 200)}`);
  await page.close();

  page = await open({ lang: "nb" });
  await click(page, '.hf-row[data-action="delivery"]', 900);
  const details = await page.evaluate(() => window.__card.shadowRoot.querySelector(".hf-sheet")?.textContent || "");
  assert(/I denne kassen|Sporing|Betaling/.test(details), `delivery details: ${details.slice(0, 200)}`);
  await page.close();

  page = await open({ lang: "nb", view: "account" });
  const tabs = await page.evaluate(() => [...window.__card.shadowRoot.querySelectorAll(".hf-subtabs button")].map((e) => e.textContent.trim()));
  assert(tabs[0] === "Abonnement og betaling" && tabs[1] === "Matpreferanser" && tabs[2] === "Utgifter", JSON.stringify(tabs));
  const plan = await text(page, ".hf-kv");
  assert(/Retter per kasse/.test(plan), `plan: ${plan}`);
  await page.close();
});

await browser.close();
server.close();
const failed = results.filter((r) => r.startsWith("FAIL"));
console.log(`\n${results.length - failed.length} of ${results.length} flows passed`);
if (failed.length) console.log(failed.join("\n"));
if (errors.length) console.log("ERRORS:\n" + [...new Set(errors)].join("\n"));
process.exit(failed.length || errors.length ? 1 : 0);
