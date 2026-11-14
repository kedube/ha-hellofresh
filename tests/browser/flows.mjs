// Browser flows: drive the real HelloFresh card in headless Chrome against a fake Home Assistant
// (fixtures.js, made-up data) and assert on its service calls and DOM. `npm test`; exits non-zero
// on any failed check or page error.
import { launchBrowser, serveDeliveryPhotos } from "./chrome.mjs";
import { startServer } from "./server.mjs";

const { server, port: PORT } = await startServer(0);
const browser = await launchBrowser();
serveDeliveryPhotos(browser, PORT);
const results = [];
const errors = [];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// The fixtures build their weeks around the page's clock: the next box comes the next Monday, its
// meal deadline the Thursday before at 02:59. From that Thursday until Monday the next box is
// locked, as it would be for real, so the flows that edit it found nothing to click and only
// passed from Tuesday to early Thursday. They all run on a fixed Tuesday instead; a flow can still
// pass its own `now`.
const FLOW_CLOCK = "2026-09-29T12:00:00";

async function open(params = {}, viewport = [1400, 900], path = "/index.html", init = null) {
  const page = await browser.newPage();
  await page.setViewport({ width: viewport[0], height: viewport[1] });
  // `init` runs in the page before its own scripts: a flow's stand-in for a Home Assistant that
  // misbehaves (see flakyWeeks).
  if (init) await page.evaluateOnNewDocument(init);
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

// FLOWS=<regex> runs only the flows whose name matches (case-insensitive), to try one while
// working on it; CI runs them all.
const only = process.env.FLOWS ? new RegExp(process.env.FLOWS, "i") : null;

async function check(name, fn) {
  if (only && !only.test(name)) return;
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
  const week = await page.evaluate(() => window.__card.selectedWeekId);
  await click(page, '.hf-weekchip[aria-pressed="true"] + .hf-weekchip', 500);
  await click(page, `.hf-weekchip[data-week-id="${week}"]`, 500);
  assert(/4 of 3 meals/.test(await text(page, ".js-boxbar")), "still 4 meals");
  assert(await q(page, ".hf-weekchip .hf-wcdirty"), "unsaved dot on chip");
  await page.close();
});

// The week strip shows a page of weeks: the chosen one (the next box at first) with the week
// before it, older weeks a page or more away. Its chips keep one width at any window size, and a
// page is as many as fit. Choosing a week on screen keeps the strip still (each re-render used
// to glide back from the oldest week), and the arrows turn whole pages without changing the
// chosen week.
await check("week strip: a page of weeks; choosing one keeps it still; the arrows turn pages", async () => {
  const strip = (page) =>
    page.evaluate(() => {
      const root = window.__card.shadowRoot;
      const el = root.querySelector(".hf-weekstrip");
      const box = el.getBoundingClientRect();
      const chips = [...el.querySelectorAll(".hf-weekchip")];
      const shown = chips.filter((c) => {
        const r = c.getBoundingClientRect();
        return r.left >= box.left - 1 && r.right <= box.right + 1;
      });
      const widths = chips.map((c) => Math.round(c.getBoundingClientRect().width * 10) / 10);
      const style = getComputedStyle(el);
      const room = el.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
      const gap = parseFloat(getComputedStyle(chips[0].parentElement).columnGap);
      const [earlier, later] = root.querySelectorAll('[data-action="strip-page"]');
      return {
        left: Math.round(el.scrollLeft),
        first: chips.indexOf(shown[0]),
        shown: shown.length,
        fits: Math.floor((room + gap + 0.5) / (widths[0] + gap)),
        widths: [...new Set(widths)],
        selected: chips.findIndex((c) => c.getAttribute("aria-pressed") === "true"),
        count: chips.length,
        earlier: earlier.disabled,
        later: later.disabled,
      };
    });
  let chipWidth;
  for (const width of [1400, 390]) {
    const page = await open({ view: "menu" }, [width, 900]);
    const at = `${width}px`;
    let s = await strip(page);
    // One width for every chip, at least the 92px they had before the strip turned pages, and
    // the same in a wide window as in a narrow one: they used to stretch to fill the strip.
    assert(s.widths.length === 1 && s.widths[0] >= 92, `${at}: chip widths ${s.widths.join(", ")}`);
    chipWidth ??= s.widths[0];
    assert(s.widths[0] === chipWidth, `${at}: chips ${s.widths[0]}px wide, ${chipWidth}px in the first window`);
    const size = s.fits;
    assert(size >= 2 && s.shown === size, `${at}: ${s.shown} weeks in view, ${size} fit`);
    assert(s.first === Math.max(0, Math.min(s.selected - 1, s.count - size)), `${at}: page ${JSON.stringify(s)}`);

    // Choosing the other week in view: not a frame of movement.
    const lefts = await page.evaluate(async () => {
      const root = window.__card.shadowRoot;
      const el = root.querySelector(".hf-weekstrip");
      const box = el.getBoundingClientRect();
      const other = [...el.querySelectorAll('.hf-weekchip:not([aria-pressed="true"])')].find((c) => {
        const r = c.getBoundingClientRect();
        return r.left >= box.left - 1 && r.right <= box.right + 1;
      });
      other.click();
      const out = [];
      for (let i = 0; i < 30; i += 1) {
        await new Promise((r) => requestAnimationFrame(r));
        out.push(Math.round(root.querySelector(".hf-weekstrip").scrollLeft));
      }
      return out;
    });
    assert(new Set([s.left, ...lefts]).size === 1, `${at}: the strip moved: ${s.left} then ${lefts.join(" ")}`);
    const chosen = (await strip(page)).selected;
    assert(chosen !== s.selected, `${at}: the click chose the other week`);

    // Earlier pages, a page at a time, down to the first; the chosen week stays chosen.
    for (let first = s.first; first > 0; ) {
      await click(page, '[data-action="strip-page"][data-dir="-1"]', 800);
      s = await strip(page);
      first = Math.max(0, first - size);
      assert(s.first === first && s.shown === size, `${at}: earlier page ${JSON.stringify(s)}, expected ${first}`);
      assert(s.selected === chosen, `${at}: paging changed the chosen week`);
    }
    assert(s.earlier && !s.later, `${at}: on the first page only "later" is on`);
    await click(page, '[data-action="strip-page"][data-dir="1"]', 800);
    s = await strip(page);
    assert(s.first === Math.min(size, s.count - size), `${at}: later page ${JSON.stringify(s)}`);

    // A re-render keeps the page the user turned to.
    await page.evaluate(() => window.__card.renderView());
    assert((await strip(page)).first === s.first, `${at}: a re-render moved the strip`);

    // A week chosen elsewhere, out of view, brings the strip to it: the last page.
    await page.evaluate(() => {
      const chips = window.__card.shadowRoot.querySelectorAll(".hf-weekchip");
      window.__card.selectWeek(chips[chips.length - 1].getAttribute("data-week-id"));
    });
    await sleep(800);
    s = await strip(page);
    assert(s.selected === s.count - 1 && s.first === s.count - size && s.later, `${at}: ${JSON.stringify(s)}`);
    await page.close();
  }
});

// A meal already in the saved box stays the customer's once it sells out (HelloFresh holds it,
// and its website shows it as usual), so it gets no "Sold out" ribbon; a meal outside the box does.
await check("a meal in the box never shows as sold out", async () => {
  const page = await open({ view: "menu" });
  const state = await page.evaluate(() => {
    const card = window.__card;
    const root = card.shadowRoot;
    const week = card.selectedWeek();
    const keyOf = (r) => String(r.course_index != null ? r.course_index : r.recipe_id);
    const recipeOf = (tile) => week.recipes.find((r) => keyOf(r) === tile.getAttribute("data-key"));
    const inBox = recipeOf(root.querySelector('.hf-tile.selected[data-action="open-recipe"]'));
    const outside = recipeOf(root.querySelector('.hf-tile:not(.selected):not(.soldout)[data-action="open-recipe"]'));
    inBox.is_sold_out = true;
    outside.is_sold_out = true;
    card.renderView();
    const look = (r) => {
      const tile = root.querySelector(`.hf-tile[data-action="open-recipe"][data-key="${keyOf(r)}"]`);
      return { greyed: tile.classList.contains("soldout"), ribbon: Boolean(tile.querySelector(".hf-soldout")) };
    };
    return { saved: inBox.is_selected === true, inBox: look(inBox), outside: look(outside) };
  });
  assert(state.saved, "the meal picked is in the saved box");
  assert(!state.inBox.greyed && !state.inBox.ribbon, `meal in the box: ${JSON.stringify(state.inBox)}`);
  assert(state.outside.greyed && state.outside.ribbon, `meal outside the box: ${JSON.stringify(state.outside)}`);
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
    s.value = "4 meals for 2 people";
    s.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
  });
  await sleep(300);
  assert((await calls(page, "select.select_option")).length === 0, "nothing written before confirming");
  await page.evaluate(() => window.__card.shadowRoot.querySelector('.hf-sheet [data-action="confirm-plan"]').click());
  await sleep(600);
  const s = await calls(page, "select.select_option");
  assert(s.length === 1 && s[0].data.option === "4 meals for 2 people", JSON.stringify(s));
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

await check("the plan's size is in people, as HelloFresh words it", async () => {
  const page = await open({ view: "account" });
  const sub = await text(page, ".hf-apptitle .hf-sub");
  assert(/3 meals for 2 people/.test(sub), `subtitle: ${sub}`);
  const plan = await text(page, ".hf-kv");
  assert(/Number of people/.test(plan) && !/Servings/.test(plan), `plan: ${plan}`);
  const shown = await page.evaluate(() => [...window.__card.shadowRoot.querySelector('[data-plan-control="box_size"]').options].map((o) => o.textContent.trim()));
  assert(shown.includes("3 meals for 2 people") && shown.includes("3 meals for 4 people"), JSON.stringify(shown));
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
  // A tile is a plain container; its dish name is the button that opens it.
  await page.evaluate(() => window.__card.shadowRoot.querySelector(".hf-tile .hf-open").focus());
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
        soldOut: b.classList.contains("soldout"),
        note: b.querySelector(".hf-optsub")?.textContent.trim() || "",
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
  // Sold out is advisory, as on the grid: the option says so and can still be chosen.
  assert(drawer.rows[7].soldOut && drawer.rows[7].note === "Sold out" && !drawer.rows[7].disabled, JSON.stringify(drawer.rows[7]));
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

// #15: after an update Home Assistant loads the new release's panel (a new ?v=) into a page that
// already has the old one, and a page can load the card from a second address. Neither may throw:
// the copy that came first carries on until a reload.
await check("sidebar panel: a second copy of the panel or card loads without an error", async () => {
  const page = await open({ panel: "1" });
  const notes = [];
  page.on("console", (msg) => msg.type() === "info" && notes.push(msg.text()));
  const seen = errors.length;
  const outcome = await page.evaluate(async () => {
    const names = ["hellofresh-panel", "hellofresh-card", "hellofresh-card-editor"];
    const before = names.map((name) => customElements.get(name));
    // Home Assistant's loadModule(url), as for a panel's module_url or a dashboard resource.
    for (const src of ["/hellofresh/hellofresh-panel.js?v=next", "/hellofresh/hellofresh-card.js?v=next"]) {
      await new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.type = "module";
        script.onload = resolve;
        script.onerror = reject;
        script.src = src;
        document.body.appendChild(script);
      });
    }
    // Then the element Home Assistant creates for the updated panel.
    const panel = document.createElement("hellofresh-panel");
    panel.panel = window.__panel.panel;
    panel.hass = window.__hass;
    document.getElementById("main").replaceChildren(panel);
    await new Promise((resolve) => setTimeout(resolve, 300));
    return {
      kept: names.every((name, i) => before[i] && customElements.get(name) === before[i]),
      pickerEntries: window.customCards.filter((card) => card.type === "hellofresh-card").length,
      mounted: Boolean(panel.shadowRoot.querySelector("hellofresh-card")),
    };
  });
  await sleep(200);
  assert(errors.length === seen, errors.slice(seen).join("; "));
  assert(outcome.kept, "the panel, card and editor defined first stay");
  assert(outcome.pickerEntries === 1, `card picker entries: ${outcome.pickerEntries}`);
  assert(outcome.mounted, "the new panel element mounts the card");
  for (const what of ["panel", "card"]) {
    const noted = notes.some((note) => note.includes(`HelloFresh ${what}, so version next takes over`));
    assert(noted, `no reload note for the ${what}: ${JSON.stringify(notes)}`);
  }
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
  assert(JSON.stringify(tabs) === JSON.stringify(["Oversikt", "Meny", "Tillegg", "Oppskrifter", "Uke", "Konto"]), JSON.stringify(tabs));
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
  assert(/Retter per kasse/.test(plan) && /Antall personer/.test(plan), `plan: ${plan}`);
  const sizes = await page.evaluate(() => [...window.__card.shadowRoot.querySelector('[data-plan-control="box_size"]').options].map((o) => o.textContent.trim()));
  assert(sizes.includes("3 retter for 2 personer"), JSON.stringify(sizes));
  await page.close();
});

// ---- the Week tab -----------------------------------------------------------------------------
// A Wednesday afternoon in the week whose box arrived on Monday; mealplan=1 has that week half
// planned already (Mon a meal, Tue "Pizza night", Wed a meal with an appetizer and a dessert,
// Thu a Market breakfast), with one box meal still to place.
const WEEK_CLOCK = "2026-10-14T15:00:00";
const openWeek = (params = {}) => open({ view: "week", mealplan: "1", now: WEEK_CLOCK, ...params });
const line = (day, meal = "dinner") => `.hf-planline[data-day="${day}"][data-meal="${meal}"]`;

await check("week tab: a tray meal goes on the day picked, in one set_planned_meal", async () => {
  const page = await openWeek();
  const tray = await page.evaluate(() => window.__card.shadowRoot.querySelectorAll(".hf-trayitem").length);
  assert(tray === 1, `tray items: ${tray}`);
  await click(page, ".hf-trayitem", 300);
  await click(page, '[data-action="plan-day"][data-day="2026-10-16"]', 600);
  const sets = await calls(page, "hellofresh.set_planned_meal");
  assert(sets.length === 1, `set calls: ${sets.length}`);
  const data = sets[0].data;
  assert(data.date === "2026-10-16" && data.meal === "dinner" && data.course === "main" && data.recipe_id && data.week_id, JSON.stringify(data));
  assert(!(await q(page, ".hf-sheet")), "the sheet closes");
  assert(await q(page, ".hf-notice.tone-ok"), "the tray says everything has a day");
  assert(/Greek Diner/.test(await text(page, `${line("2026-10-16")} .hf-planname`)), "Friday shows the meal");
  await page.close();
});

await check("week tab: a note with its own start time saves in one call", async () => {
  const page = await openWeek();
  await click(page, line("2026-10-17"), 300);
  await click(page, '[data-action="pick-note"]', 100);
  await page.evaluate(() => {
    const root = window.__card.shadowRoot;
    root.querySelector("[data-note-input]").value = "Eating out";
    root.querySelector("[data-start-input]").value = "19:00";
  });
  await click(page, '[data-action="save-slot"]', 600);
  const sets = await calls(page, "hellofresh.set_planned_meal");
  assert(sets.length === 1, `set calls: ${sets.length}`);
  const data = sets[0].data;
  assert(data.text === "Eating out" && data.start_time === "19:00" && data.date === "2026-10-17", JSON.stringify(data));
  assert(/Eating out/.test(await text(page, line("2026-10-17"))), "Saturday shows the note");
  assert(/^7:00/.test(await text(page, `${line("2026-10-17")} .hf-plantime`)), "at 7 PM");
  await page.close();
});

await check("week tab: dropping a course clears only it; a used-up meal can't be picked", async () => {
  const page = await openWeek();
  await click(page, line("2026-10-14"), 300);
  const used = await page.evaluate(() =>
    [...window.__card.shadowRoot.querySelectorAll('.hf-optrow[data-kind="meal"]')].map((b) => ({ on: b.classList.contains("on"), disabled: b.disabled }))
  );
  // Monday's meal is used up; Wednesday's own pick stays selectable; the tray meal is free.
  assert(used.filter((b) => b.disabled).length === 1 && used.filter((b) => b.on).length === 1, JSON.stringify(used));
  await click(page, '[data-action="pick-none"][data-course="dessert"]', 100);
  await click(page, '[data-action="save-slot"]', 600);
  const clears = await calls(page, "hellofresh.clear_planned_meal");
  const sets = await calls(page, "hellofresh.set_planned_meal");
  assert(clears.length === 1 && clears[0].data.course === "dessert" && clears[0].data.date === "2026-10-14", JSON.stringify(clears));
  assert(sets.length === 0, "nothing else written");
  const sub = await text(page, `${line("2026-10-14")} .hf-plansub`);
  assert(/Gyoza/.test(sub) && !/Lava/.test(sub), `Wednesday: ${sub}`);
  await page.close();
});

await check("week tab: Clear removes a whole meal; the photo opens the recipe", async () => {
  const page = await openWeek();
  await click(page, line("2026-10-15", "breakfast"), 300);
  await click(page, '[data-action="clear-slot"]', 600);
  const clears = await calls(page, "hellofresh.clear_planned_meal");
  assert(clears.length === 1 && clears[0].data.meal === "breakfast" && !clears[0].data.course, JSON.stringify(clears));
  assert(await q(page, '.hf-planadds [data-day="2026-10-15"][data-meal="breakfast"]'), "breakfast is a + link again");
  await click(page, `${line("2026-10-14")} .hf-planthumb`, 600);
  assert((await calls(page, "hellofresh.get_recipe_detail")).length === 1, "the recipe sheet opened");
  assert(!(await q(page, ".hf-sheet .hf-planopts")), "not the plan sheet");
  await page.close();
});

await check("week tab: a plan changed elsewhere shows without a refresh", async () => {
  const page = await openWeek();
  await page.evaluate(async () => {
    await new Promise((r) => setTimeout(r, 1100)); // a later stamp than the first one
    window.__fx.mealPlan.set({ date: "2026-10-18", text: "Grandma's" });
    window.__fx.touchMealPlan();
    window.__card.hass = window.__hass;
  });
  await sleep(500);
  assert(/Grandma/.test(await text(page, line("2026-10-18"))), "Sunday shows the change");
  await page.close();
});

await check("week tab: always there; empty without a plan; the Overview shows tonight", async () => {
  let page = await open({ now: WEEK_CLOCK });
  const tabs = await page.evaluate(() => [...window.__card.shadowRoot.querySelectorAll(".hf-tab")].map((t) => t.dataset.view));
  assert(tabs.join() === "overview,menu,market,recipes,week,account", JSON.stringify(tabs));
  assert(!(await q(page, ".hf-todayplan")), "no Tonight line without a plan");
  await click(page, '.hf-tab[data-view="week"]', 300);
  const empty = await page.evaluate(() => window.__card.shadowRoot.querySelectorAll(".hf-planline.empty").length);
  assert(empty === 7, `empty dinners: ${empty}`);
  await page.close();

  page = await open({ mealplan: "1", now: WEEK_CLOCK });
  const tonight = await text(page, ".hf-todayplan");
  assert(/Tonight: Romesco/.test(tonight) && /5:30/.test(tonight), `tonight: ${tonight}`);
  await click(page, '.hf-todayplan [data-action="goto"]', 300);
  assert((await page.evaluate(() => window.__card.view)) === "week", "the line opens the Week tab");
  await page.close();
});

// ---- the card itself: loading, keyboard focus, sheets, accounts ---------------------------------

// Runs before the page's own scripts (open's `init`): get_weeks fails while window.__down is set,
// as it does while Home Assistant restarts, counting the tries in window.__attempts; and the
// card's retry delays (15 s, 1 min, 5 min) are cut to a moment, noted in window.__retries.
const flakyWeeks = () => {
  window.__down = true;
  window.__attempts = 0;
  window.__retries = [];
  const later = window.setTimeout.bind(window);
  window.setTimeout = (fn, ms, ...rest) => {
    if (ms === 15000 || ms === 60000 || ms === 300000) {
      window.__retries.push(ms);
      ms = 100;
    }
    return later(fn, ms, ...rest);
  };
  // The harness sets window.__hass before it hands the card hass: wrap its callService there.
  let hass;
  Object.defineProperty(window, "__hass", {
    configurable: true,
    get: () => hass,
    set(value) {
      hass = value;
      const real = value.callService.bind(value);
      value.callService = async (domain, service, ...rest) => {
        if (domain === "hellofresh" && service === "get_weeks" && window.__down) {
          window.__attempts += 1;
          await new Promise((r) => later(r, 30));
          throw new Error("Home Assistant is starting");
        }
        return real(domain, service, ...rest);
      };
    },
  });
};

await check("a failed load is retried soon, then again when Home Assistant or the integration comes back", async () => {
  const page = await open({ view: "overview" }, [1400, 900], "/index.html", flakyWeeks);
  await sleep(800); // the three quick retries run (and fail)
  let s = await page.evaluate(() => ({
    retries: window.__retries,
    attempts: window.__attempts,
    fetched: window.__card.lastFetched,
    text: window.__card.shadowRoot.querySelector(".js-main").textContent,
  }));
  assert(JSON.stringify(s.retries) === "[15000,60000,300000]" && s.attempts === 4, `retries: ${JSON.stringify(s)}`);
  assert(s.fetched === 0, "a failed load counted as a load");
  assert(/Home Assistant is starting/.test(s.text) && /Try again/.test(s.text), s.text.slice(0, 160));
  await sleep(500);
  assert((await page.evaluate(() => window.__attempts)) === 4, "after those it waits for the usual interval");

  // Home Assistant reconnects: the card reads again straight away.
  await page.evaluate(() => {
    window.__down = false;
    const hass = window.__hass;
    window.__card.hass = { ...hass, connected: false };
    window.__card.hass = { ...hass, connected: true };
  });
  await sleep(1600);
  s = await page.evaluate(() => ({
    fetched: window.__card.lastFetched,
    error: window.__card.error,
    hero: Boolean(window.__card.shadowRoot.querySelector(".js-main .hf-hero")),
  }));
  assert(s.fetched > 0 && !s.error && s.hero, `after reconnecting: ${JSON.stringify(s)}`);

  // A refresh fails over data the card already shows; the integration's entities going
  // unavailable (it reloads) and coming back is the sign to read again.
  const banners = () => page.evaluate(() => window.__card.shadowRoot.querySelector(".js-banners").textContent);
  await page.evaluate(() => {
    window.__down = true;
    window.__card.refresh({ quiet: true });
  });
  await sleep(900);
  assert(/Couldn't refresh/.test(await banners()), "the failure shows over the data");
  await page.evaluate(() => {
    const hass = window.__hass;
    const down = Object.fromEntries(
      Object.entries(hass.states).map(([id, st]) => [id, id.includes("hellofresh") ? { ...st, state: "unavailable" } : st])
    );
    window.__card.hass = { ...hass, states: down };
    window.__down = false;
    window.__card.hass = { ...hass };
  });
  await sleep(1600);
  assert(!/Couldn't refresh/.test(await banners()), "not read again once the integration was back");
  await page.close();
});

await check("keyboard focus stays on the control in use through a redraw, a tab switch and a refresh", async () => {
  const page = await open({ view: "overview" });
  const focused = () =>
    page.evaluate(() => {
      const el = window.__card.shadowRoot.activeElement;
      if (!el) return null;
      const attr = (name) => el.getAttribute(name);
      return {
        action: attr("data-action"),
        view: attr("data-view"),
        uid: attr("data-uid"),
        week: attr("data-week-id"),
        current: attr("aria-current"),
        pressed: attr("aria-pressed"),
      };
    });
  // A pantry tick redraws the whole Overview; the ticked item (no focus key of its own) keeps focus.
  const uid = await page.evaluate(() => {
    const item = window.__card.shadowRoot.querySelector(".js-main .hf-pantryitem:not(.done)");
    item.focus();
    return item.getAttribute("data-uid");
  });
  await page.keyboard.press("Space");
  await sleep(300);
  let f = await focused();
  assert(f && f.action === "pantry-toggle" && f.uid === uid && f.pressed === "true", `after a pantry tick: ${JSON.stringify(f)}`);

  // A tab switch redraws the tab bar (the current tab moves) and the section.
  await page.evaluate(() => window.__card.shadowRoot.querySelector('.hf-tab[data-view="menu"]').focus());
  await page.keyboard.press("Enter");
  await sleep(500);
  f = await focused();
  assert(f && f.action === "nav" && f.view === "menu" && f.current === "page", `after a tab switch: ${JSON.stringify(f)}`);

  // Refresh: the button spins while the load runs, a second press starts nothing, focus stays.
  await page.evaluate(() => window.__card.shadowRoot.querySelector('.js-appbar [data-action="refresh"]').focus());
  const before = (await calls(page, "hellofresh.get_weeks")).length;
  await page.keyboard.press("Enter");
  const busy = await page.evaluate(() => {
    const button = window.__card.shadowRoot.querySelector('.js-appbar [data-action="refresh"]');
    return [button.classList.contains("spin"), button.getAttribute("aria-disabled")];
  });
  await page.keyboard.press("Enter");
  await sleep(900);
  f = await focused();
  assert(busy[0] && busy[1] === "true", `while loading: ${JSON.stringify(busy)}`);
  assert((await calls(page, "hellofresh.get_weeks")).length === before + 1, "one load for two presses");
  assert(f && f.action === "refresh", `after the load: ${JSON.stringify(f)}`);

  // A background load landing redraws the Menu under a focused week chip.
  const week = await page.evaluate(() => {
    const chip = window.__card.shadowRoot.querySelector('.hf-weekchip[aria-pressed="false"]');
    chip.focus();
    return chip.getAttribute("data-week-id");
  });
  await page.evaluate(() => window.__card.refresh({ quiet: true }));
  await sleep(800);
  f = await focused();
  assert(f && f.action === "select-week" && f.week === week, `after a background load: ${JSON.stringify(f)}`);
  await page.close();
});

await check("a sheet is built once per open: no replayed entrance, focus kept, Tab stays inside, focus goes back", async () => {
  const page = await open({ view: "overview" });
  const state = () =>
    page.evaluate(() => {
      const root = window.__card.shadowRoot;
      const dialog = root.querySelector('.hf-sheet[role="dialog"]');
      const wrap = root.querySelector(".hf-sheetwrap");
      const el = root.activeElement;
      return {
        open: Boolean(dialog),
        modal: dialog && dialog.getAttribute("aria-modal"),
        same: Boolean(dialog && dialog.__mark && wrap && wrap.__mark),
        animating: dialog ? dialog.getAnimations().length + wrap.getAnimations().length : 0,
        inDialog: Boolean(dialog && el && dialog.contains(el)),
        action: el && el.getAttribute("data-action"),
        uid: el && el.getAttribute("data-uid"),
        pressed: el && el.getAttribute("aria-pressed"),
        inert: root.querySelector("ha-card").inert,
      };
    });
  await page.evaluate(() => window.__card.shadowRoot.querySelector('.js-main [data-action="pantry"]').focus());
  await page.keyboard.press("Enter");
  await sleep(700);
  let s = await state();
  assert(s.open && s.modal === "true" && s.inDialog && s.inert, `opened: ${JSON.stringify(s)}`);
  const uid = await page.evaluate(() => {
    const root = window.__card.shadowRoot;
    root.querySelector('.hf-sheet[role="dialog"]').__mark = true;
    root.querySelector(".hf-sheetwrap").__mark = true;
    const item = root.querySelector(".hf-sheet .hf-pantryitem:not(.done)");
    item.focus();
    return item.getAttribute("data-uid");
  });
  // A tick redraws the sheet at once (before the write answers): only what is inside it.
  await page.keyboard.press("Space");
  s = await state();
  assert(s.same && s.animating === 0, `the tick rebuilt the sheet: ${JSON.stringify(s)}`);
  assert(s.action === "pantry-toggle" && s.uid === uid && s.pressed === "true", `focus after the tick: ${JSON.stringify(s)}`);
  // So does the list being read again (the to-do entity moved).
  await page.evaluate(() => {
    const card = window.__card;
    const id = card.shadowRoot.querySelector(".hf-sheet .hf-pantryitem").getAttribute("data-entity");
    card.loadPantry(id, { force: true });
  });
  await sleep(400);
  s = await state();
  assert(s.same && s.uid === uid, `after the list was read again: ${JSON.stringify(s)}`);
  assert((await calls(page, "todo.update_item")).length === 1, "one write");

  // Tab from the last control goes to the first; Shift+Tab from the first to the last.
  const stops = () =>
    page.evaluate(() => {
      const root = window.__card.shadowRoot;
      const all = [...root.querySelectorAll(".hf-sheet button")].filter((b) => !b.disabled && b.getClientRects().length);
      return { count: all.length, at: all.indexOf(root.activeElement) };
    });
  await page.evaluate(() => {
    const all = [...window.__card.shadowRoot.querySelectorAll(".hf-sheet button")].filter((b) => !b.disabled);
    all[all.length - 1].focus();
  });
  await page.keyboard.press("Tab");
  const wrapped = await stops();
  await page.keyboard.down("Shift");
  await page.keyboard.press("Tab");
  await page.keyboard.up("Shift");
  const back = await stops();
  assert(wrapped.count > 2 && wrapped.at === 0 && back.at === back.count - 1, `Tab: ${JSON.stringify([wrapped, back])}`);

  // The view behind is redrawn while the sheet is open, so the button that opened it is a new
  // element by the time it closes; focus still goes back to it.
  await page.evaluate(() => window.__card.renderView());
  await page.keyboard.press("Escape");
  await sleep(300);
  s = await state();
  assert(!s.open && !s.inert && s.action === "pantry", `after closing: ${JSON.stringify(s)}`);
  await page.close();
});

await check("the box bar is rewritten only when it changes, and Save keeps focus when the price lands", async () => {
  const page = await open({ view: "menu" });
  await click(page, '.hf-tile:not(.selected) [data-action="add"]', 100);
  const kept = await page.evaluate(() => {
    const root = window.__card.shadowRoot;
    const save = root.querySelector('.js-boxbar [data-action="box-save"]');
    save.__mark = true;
    save.focus();
    window.__card.renderBoxBar();
    window.__card.renderBoxBar();
    const again = root.querySelector('.js-boxbar [data-action="box-save"]');
    return Boolean(again && again.__mark) && root.activeElement === again;
  });
  assert(kept, "an unchanged bar was rewritten");
  await sleep(1500); // the price estimate (asked 650 ms after the edit) lands and changes the bar
  const after = await page.evaluate(() => {
    const root = window.__card.shadowRoot;
    const el = root.activeElement;
    return { action: el && el.getAttribute("data-action"), bar: root.querySelector(".js-boxbar").textContent.replace(/\s+/g, " ") };
  });
  assert(/Estimated total/.test(after.bar), `the price landed: ${after.bar}`);
  assert(after.action === "box-save", `focus after the price landed: ${JSON.stringify(after)}`);
  await page.close();
});

await check("the recipe sheet is a dialog that keeps the reader's place; focus goes back to the dish", async () => {
  const page = await open({ view: "menu" }, [1000, 640]);
  const key = await page.evaluate(() => {
    const opener = window.__card.shadowRoot.querySelector('.hf-tile:not(.selected) button[data-action="open-recipe"]');
    opener.focus();
    return opener.getAttribute("data-key");
  });
  await page.keyboard.press("Enter");
  await sleep(800);
  const sheet = () =>
    page.evaluate(() => {
      const root = window.__card.shadowRoot;
      const dialog = root.querySelector(".detailwrap .detailbox");
      const scroll = dialog && dialog.querySelector(".detailscroll");
      const el = root.activeElement;
      const close = dialog && dialog.querySelector(".detailclose");
      return {
        role: dialog && dialog.getAttribute("role"),
        modal: dialog && dialog.getAttribute("aria-modal"),
        label: dialog && dialog.getAttribute("aria-label"),
        close: close && close.getAttribute("aria-label"),
        focusIn: Boolean(dialog && el && dialog.contains(el)) || el === dialog,
        sel: el && el.getAttribute("data-sel"),
        same: Boolean(dialog && dialog.__mark && scroll && scroll.__mark),
        top: scroll ? Math.round(scroll.scrollTop) : -1,
        busy: Boolean(scroll && scroll.classList.contains("busy")),
        inert: root.querySelector("ha-card").inert,
      };
    });
  let s = await sheet();
  assert(s.role === "dialog" && s.modal === "true" && s.label && s.close && s.focusIn && s.inert, `opened: ${JSON.stringify(s)}`);
  // Read down to the steps, then add the meal from the footer by keyboard.
  await page.evaluate(() => {
    const dialog = window.__card.shadowRoot.querySelector(".detailwrap .detailbox");
    const scroll = dialog.querySelector(".detailscroll");
    dialog.__mark = true;
    scroll.__mark = true;
    scroll.scrollTop = 300;
    dialog.querySelector('[data-sel="add"]').focus();
  });
  const top = (await sheet()).top;
  assert(top > 0, "the recipe doesn't scroll");
  await page.keyboard.press("Enter");
  await sleep(300);
  s = await sheet();
  assert(s.same && s.top === top && s.focusIn && (s.sel === "inc" || s.sel === "dec"), `after Add: ${JSON.stringify(s)}`);
  assert(/4 of 3 meals/.test(await text(page, ".js-boxbar")), "added to the box");
  // Another serving size: the recipe stays on screen, dimmed and in place, while it reloads.
  await page.evaluate(() => window.__card.shadowRoot.querySelector('.detailwrap [data-servings="4"]').click());
  const loading = await sheet();
  await sleep(600);
  s = await sheet();
  assert(loading.busy && loading.top === top, `while the new amounts load: ${JSON.stringify(loading)}`);
  assert(!s.busy && s.top === top, `with the new amounts: ${JSON.stringify(s)}`);
  // Escape: focus goes back to the dish that opened the sheet, though its tile was redrawn.
  await page.keyboard.press("Escape");
  await sleep(300);
  const back = await page.evaluate(() => {
    const root = window.__card.shadowRoot;
    const el = root.activeElement;
    return {
      open: Boolean(root.querySelector(".detailwrap")),
      inert: root.querySelector("ha-card").inert,
      key: el && el.getAttribute("data-key"),
      action: el && el.getAttribute("data-action"),
    };
  });
  assert(!back.open && !back.inert && back.key === key && back.action === "open-recipe", `after closing: ${JSON.stringify(back)}, opened from ${key}`);
  await page.close();
});

await check("given another account, a live card drops the old one's weeks and unsaved box and loads the new one", async () => {
  const page = await open({ view: "menu" });
  await click(page, '.hf-tile:not(.selected) [data-action="add"]');
  const start = await page.evaluate(() => [window.__card.box.dirty(window.__card.selectedWeek()), window.__calls.length]);
  assert(start[0], "no pending edit to start with");
  const right = await page.evaluate(() => {
    const card = window.__card;
    card.setConfig({ type: "custom:hellofresh-card", config_entry_id: "entry2" });
    return { weeks: card.weeks, week: card.selectedWeekId, bar: card.shadowRoot.querySelector(".js-boxbar").textContent.trim() };
  });
  assert(right.weeks === null && right.week === null && !right.bar, `right after: ${JSON.stringify(right)}`);
  await sleep(900);
  const after = await page.evaluate((from) => {
    const card = window.__card;
    const week = card.selectedWeek();
    return {
      reads: window.__calls.slice(from).filter((c) => c.service === "get_weeks").map((c) => c.data.config_entry_id),
      dirty: Boolean(week) && card.box.dirty(week),
      meals: week ? card.box.displayMeals(week).size : -1,
      bar: card.shadowRoot.querySelector(".js-boxbar").textContent.replace(/\s+/g, " "),
    };
  }, start[1]);
  assert(after.reads.length >= 1 && after.reads.every((id) => id === "entry2"), `reads: ${JSON.stringify(after.reads)}`);
  assert(!after.dirty && after.meals === 3 && !/4 of 3/.test(after.bar), `the old account's edit stayed: ${JSON.stringify(after)}`);
  assert((await calls(page, "hellofresh.select_meals")).length === 0, "something was written");
  await page.close();
});

await check("a card reads only what its sections show; a save re-reads its week; two cards share one read", async () => {
  let page = await open({ config: JSON.stringify({ views: ["recipes"] }) });
  const services = (await calls(page)).filter((c) => c.domain === "hellofresh").map((c) => c.service);
  assert(
    !services.includes("get_weeks") && !services.includes("get_meal_plan") && services.includes("get_account_summary"),
    `a Recipes card read: ${services.join(", ")}`
  );
  await page.close();

  page = await open({ view: "menu" });
  await click(page, '.hf-tile:not(.selected) [data-action="add"]');
  const week = await page.evaluate(() => window.__card.selectedWeekId);
  const from = (await calls(page)).length;
  await click(page, '.js-boxbar [data-action="box-save"]', 1500);
  const reads = (await calls(page)).slice(from).filter((c) => c.service === "get_weeks");
  assert(reads.length === 1 && reads[0].data.week_id === week, `after the save: ${JSON.stringify(reads.map((c) => c.data))}`);

  // A second card for the same account on the page: one read serves both.
  const shared = await page.evaluate(async () => {
    const second = document.createElement("hellofresh-card");
    second.setConfig({ type: "custom:hellofresh-card", views: ["overview"] });
    second.hass = window.__hass;
    document.getElementById("wrap").appendChild(second);
    await new Promise((r) => setTimeout(r, 700));
    const start = window.__calls.length;
    window.__card.refresh();
    second.refresh();
    await new Promise((r) => setTimeout(r, 800));
    const services = window.__calls.slice(start).map((c) => c.service);
    const count = (name) => services.filter((s) => s === name).length;
    const shown = Boolean(second.shadowRoot.querySelector(".js-main .hf-hero"));
    second.remove();
    return { weeks: count("get_weeks"), summary: count("get_account_summary"), shown };
  });
  assert(shared.weeks === 1 && shared.summary === 1 && shared.shown, `two cards refreshing: ${JSON.stringify(shared)}`);
  await page.close();
});

await check("change day: day names are asked for again after a failed read; a new day re-reads that week and the summary", async () => {
  // The first get_delivery_options fails; every call's notifyOnError argument is noted.
  const failOnce = () => {
    let hass;
    window.__notify = {};
    Object.defineProperty(window, "__hass", {
      configurable: true,
      get: () => hass,
      set(value) {
        hass = value;
        const real = value.callService.bind(value);
        let failures = 1;
        value.callService = async (domain, service, data, target, notify, ...rest) => {
          window.__notify[`${domain}.${service}`] = notify;
          if (service === "get_delivery_options" && failures > 0) {
            failures -= 1;
            throw new Error("HelloFresh is down");
          }
          return real(domain, service, data, target, notify, ...rest);
        };
      },
    });
  };
  const page = await open({ view: "overview" }, [1400, 900], "/index.html", failOnce);
  const names = () =>
    page.evaluate(() =>
      [...window.__card.shadowRoot.querySelectorAll('.hf-sheet [data-action="pick-day"]')].map((b) => b.textContent.replace(/\s+/g, " ").trim())
    );
  await click(page, '.hf-hero [data-action="reschedule"]', 600);
  const first = await names();
  assert(first.length === 3 && !first.some((n) => /8AM/.test(n)), `after the failed read, dates alone: ${JSON.stringify(first)}`);
  await page.keyboard.press("Escape");
  await sleep(200);
  await click(page, '.hf-hero [data-action="reschedule"]', 600);
  const second = await names();
  assert(second.some((n) => /Tuesday 8AM - 8PM/.test(n)), `opened again, the names: ${JSON.stringify(second)}`);
  const from = (await calls(page)).length;
  await page.evaluate(() => window.__card.shadowRoot.querySelectorAll('.hf-sheet [data-action="pick-day"]')[1].click());
  await sleep(900);
  const after = (await calls(page)).slice(from).filter((c) => c.domain === "hellofresh");
  const moved = after.find((c) => c.service === "reschedule_week");
  const reads = after.filter((c) => c.service === "get_weeks");
  assert(moved && reads.length === 1 && reads[0].data.week_id === moved.data.week_id, `week re-read: ${JSON.stringify(after.map((c) => [c.service, c.data.week_id]))}`);
  assert(after.some((c) => c.service === "get_account_summary"), "the summary (header, next delivery) wasn't re-read");
  // Each write shows its own failure, so Home Assistant is asked not to add a toast of its own.
  await click(page, ".js-main .hf-pantryitem:not(.done)", 400);
  const notify = await page.evaluate(() => window.__notify);
  for (const service of ["hellofresh.reschedule_week", "todo.update_item", "hellofresh.get_weeks"]) {
    assert(notify[service] === false, `${service} notifyOnError: ${notify[service]}`);
  }
  await page.close();
});

await check("a plan re-read asked for while one is in flight runs after it", async () => {
  const page = await open({ view: "week" });
  const reads = await page.evaluate(async () => {
    const start = window.__calls.length;
    window.__card.loadMealPlan();
    window.__card.loadMealPlan(); // the plan changed on another device meanwhile
    await new Promise((r) => setTimeout(r, 600));
    return window.__calls.slice(start).filter((c) => c.service === "get_meal_plan").length;
  });
  assert(reads === 2, `get_meal_plan reads: ${reads}`);
  await page.close();
});

await check("a card taken out of the page and put back shows the chosen week in its strip again", async () => {
  const page = await open({ view: "menu" });
  const strip = () =>
    page.evaluate(() => {
      const el = window.__card.shadowRoot.querySelector(".hf-weekstrip");
      const box = el.getBoundingClientRect();
      const chips = [...el.querySelectorAll(".hf-weekchip")];
      const shown = chips.filter((c) => {
        const r = c.getBoundingClientRect();
        return r.left >= box.left - 1 && r.right <= box.right + 1;
      });
      const chosen = chips.find((c) => c.getAttribute("aria-pressed") === "true");
      return { first: chips.indexOf(shown[0]), chosenShown: shown.includes(chosen) };
    });
  const start = await strip();
  assert(start.first > 0 && start.chosenShown, `to start: ${JSON.stringify(start)}`);
  await page.evaluate(async () => {
    const card = window.__card;
    const parent = card.parentElement;
    card.remove(); // Home Assistant keeps a dashboard around; edit mode re-parents its cards
    card.renderView(); // a load landing meanwhile
    await new Promise((r) => setTimeout(r, 50));
    parent.appendChild(card);
  });
  await sleep(400);
  const back = await strip();
  assert(back.first === start.first && back.chosenShown, `put back: ${JSON.stringify(back)}, was ${JSON.stringify(start)}`);
  await page.close();
});

await check("a section that fails to draw says so, and the rest of the card carries on", async () => {
  const page = await open({ view: "overview" });
  page.removeAllListeners("console"); // the card reports the failure on the console, as it should
  const shown = await page.evaluate(() => {
    const card = window.__card;
    // A payload shape nobody has seen: reading the weeks' meals throws.
    for (const week of card.weeks) {
      Object.defineProperty(week, "recipes", {
        get() {
          throw new Error("an unexpected payload");
        },
      });
    }
    card.renderView();
    return card.shadowRoot.querySelector(".js-main").textContent;
  });
  assert(/couldn't be shown/.test(shown) && /an unexpected payload/.test(shown) && /Try again/.test(shown), shown.slice(0, 160));
  await click(page, '.hf-tab[data-view="recipes"]', 600);
  assert(await q(page, ".js-main .hf-tile"), "another section doesn't draw");
  await click(page, '.hf-tab[data-view="overview"]', 300);
  await click(page, '.js-main [data-action="refresh"]', 900); // Try again: the weeks are read afresh
  assert(await q(page, ".js-main .hf-hero"), "Try again doesn't bring the Overview back");
  await page.close();
});

await check("the visual editor can clear the title", async () => {
  const page = await open({});
  const titles = await page.evaluate(() => {
    const editor = document.createElement("hellofresh-card-editor");
    editor.setConfig({ type: "custom:hellofresh-card", title: "Kitchen" });
    document.body.appendChild(editor);
    editor.hass = window.__hass;
    const form = editor.querySelector("ha-form");
    const sent = [];
    editor.addEventListener("config-changed", (ev) => sent.push(ev.detail.config));
    // Home Assistant's text field reports an emptied optional field as undefined.
    const change = (title) => form.dispatchEvent(new CustomEvent("value-changed", { detail: { value: { ...form.data, title } } }));
    change(undefined);
    change("HelloFresh");
    change("Dinner");
    editor.remove();
    return sent.map((config) => ("title" in config ? config.title : "(default)"));
  });
  assert(JSON.stringify(titles) === JSON.stringify(["", "(default)", "Dinner"]), JSON.stringify(titles));
  await page.close();
});

// ---- the views: filters that can't be checked, boxes changed elsewhere, writes in flight ----------

await check("ingredients to avoid: unchecked meals stay hidden, a failed check says so, Retry asks again", async () => {
  const page = await open({ view: "menu" });
  // HelloFresh's filter service answers, then fails, while window.__filtersDown is set.
  await page.evaluate(() => {
    window.__filtersDown = true;
    const real = window.__hass.callService.bind(window.__hass);
    window.__hass.callService = async (domain, service, ...rest) => {
      const result = await real(domain, service, ...rest);
      if (service === "get_menu_courses" && window.__filtersDown) throw new Error("HelloFresh is down");
      return result;
    };
  });
  const grid = () =>
    page.evaluate(() => {
      const root = window.__card.shadowRoot;
      const tiles = [...root.querySelectorAll(".hf-grid .hf-tile")];
      const notice = root.querySelector(".hf-filternotice");
      return {
        tiles: tiles.length,
        outside: tiles.filter((t) => !t.classList.contains("selected")).length,
        notice: notice ? notice.textContent.replace(/\s+/g, " ").trim() : "",
        role: notice ? notice.getAttribute("role") : null,
        retry: Boolean(notice && notice.querySelector('[data-action="retry-filters"]')),
        filters: (root.querySelector('[data-action="toggle-filters"]')?.textContent || "").replace(/\s+/g, " ").trim(),
      };
    });
  await click(page, '[data-action="toggle-filters"]', 300);
  await click(page, '[data-action="f-server"][data-group="exclude-allergens"][data-value="nuts"]', 700);
  const failed = await grid();
  assert(failed.tiles >= 1 && failed.outside === 0, `only the box's meals while unchecked: ${JSON.stringify(failed)}`);
  assert(/couldn't check your ingredients to avoid/.test(failed.notice) && failed.role === "alert" && failed.retry, JSON.stringify(failed));
  assert(/Filters · 1/.test(failed.filters), `the filter still reads as on: ${failed.filters}`);
  const asked = (await calls(page, "hellofresh.get_menu_courses")).length;
  await page.evaluate(() => {
    window.__filtersDown = false;
  });
  await click(page, '[data-action="retry-filters"]', 700);
  const answered = await grid();
  assert((await calls(page, "hellofresh.get_menu_courses")).length === asked + 1, "Retry asked HelloFresh again");
  assert(answered.outside > 0 && !answered.notice, `checked meals are back: ${JSON.stringify(answered)}`);
  await page.close();
});

await check("a box changed elsewhere mid-edit: the bar says so, Save waits, Keep mine keeps both changes", async () => {
  const page = await open({ view: "menu" });
  const added = await page.evaluate(() => {
    const card = window.__card;
    const tile = card.shadowRoot.querySelector('.hf-tile:not(.selected) [data-action="add"]').closest(".hf-tile");
    const recipe = card.selectedWeek().recipes.find((r) => String(r.course_index) === tile.getAttribute("data-key"));
    return recipe.recipe_id;
  });
  await click(page, '.hf-tile:not(.selected) [data-action="add"]');
  // Meanwhile hellofresh.com swaps one of the box's meals for another, and the card refreshes.
  const elsewhere = await page.evaluate(async () => {
    const card = window.__card;
    const week = window.__fx.weeks.find((w) => w.week_id === card.selectedWeekId);
    const draft = card.box.displayMeals(card.selectedWeek());
    const out = week.recipes.find((r) => r.is_selected && r.variation_group == null);
    const into = [...week.recipes].reverse().find((r) => !r.is_selected && r.variation_group == null && !draft.has(r.course_index));
    out.is_selected = false;
    out.selected_quantity = null;
    into.is_selected = true;
    into.selected_quantity = 1;
    await card.refresh({ quiet: true });
    return { out: out.recipe_id, into: into.recipe_id };
  });
  await sleep(500);
  const bar = await text(page, ".js-boxbar");
  assert(/Your saved box changed since you started/.test(bar), `box bar: ${bar}`);
  assert(!(await q(page, '[data-action="box-save"]')), "no Save over a box that moved");
  assert((await q(page, '[data-action="box-keep-mine"]')) && (await q(page, '[data-action="box-use-saved"]')), "Keep mine and Use saved offered");
  await click(page, '[data-action="box-keep-mine"]', 400);
  assert(await q(page, '[data-action="box-save"]'), "Save is back once the user chose");
  await click(page, '[data-action="box-save"]', 1500);
  const saves = await calls(page, "hellofresh.select_meals");
  assert(saves.length === 1, `select_meals calls: ${saves.length}`);
  const ids = saves[0].data.recipe_ids;
  assert(ids.includes(added) && ids.includes(elsewhere.into) && !ids.includes(elsewhere.out), JSON.stringify({ ids, added, elsewhere }));
  await page.close();
});

await check("a plan change: the select shows the new option and both wait until HelloFresh answers", async () => {
  const page = await open({ view: "account" });
  const controls = () =>
    page.evaluate(() => {
      const root = window.__card.shadowRoot;
      const read = (key) => {
        const select = root.querySelector(`[data-plan-control="${key}"]`);
        return [select.value, select.disabled];
      };
      return { box: read("box_size"), day: read("delivery_day"), toast: root.querySelector(".hf-toast")?.textContent.trim() || "", sheet: Boolean(root.querySelector(".hf-sheet .hf-sheethead")) };
    });
  const choose = (key, value) =>
    page.evaluate(
      ([k, v]) => {
        const select = window.__card.shadowRoot.querySelector(`[data-plan-control="${k}"]`);
        select.value = v;
        select.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
      },
      [key, value]
    );
  // HelloFresh takes its time: the write is held until the flow lets it through.
  await page.evaluate(() => {
    const real = window.__hass.callService.bind(window.__hass);
    window.__hass.callService = async (domain, ...rest) => {
      if (domain === "select") await new Promise((r) => (window.__releasePlan = r));
      return real(domain, ...rest);
    };
  });
  await choose("box_size", "4 meals for 2 people");
  await sleep(300);
  await page.evaluate(() => window.__card.shadowRoot.querySelector('.hf-sheet [data-action="confirm-plan"]').click());
  await sleep(300);
  const during = await controls();
  assert(during.box[0] === "4 meals for 2 people" && during.box[1] && during.day[1], `while writing: ${JSON.stringify(during)}`);
  assert(/Updating your plan/.test(during.toast) && !during.sheet, `while writing: ${JSON.stringify(during)}`);
  await choose("delivery_day", "Tuesday 8AM - 8PM");
  await sleep(300);
  const meanwhile = await controls();
  assert(!meanwhile.sheet && meanwhile.day[0] === "Monday 8AM - 8PM", `a second change is not taken: ${JSON.stringify(meanwhile)}`);
  await page.evaluate(() => window.__releasePlan());
  await sleep(800);
  const after = await controls();
  assert(after.box[0] === "4 meals for 2 people" && !after.box[1] && !after.day[1], `after: ${JSON.stringify(after)}`);
  assert(/Box size changed/.test(after.toast), `toast: ${after.toast}`);
  const writes = await calls(page, "select.select_option");
  assert(writes.length === 1 && writes[0].data.option === "4 meals for 2 people", JSON.stringify(writes));
  await page.close();
});

await check("week tab: a note typed from the keyboard is chosen and saved", async () => {
  const page = await openWeek();
  await click(page, line("2026-10-17"), 300);
  await page.evaluate(() => window.__card.shadowRoot.querySelector("[data-note-input]").focus());
  await page.keyboard.type("Pizza night", { delay: 20 });
  const typed = await page.evaluate(() => {
    const root = window.__card.shadowRoot;
    return {
      pressed: root.querySelector(".hf-planotepick").getAttribute("aria-pressed"),
      on: root.querySelector(".hf-planote").classList.contains("on"),
      focused: Boolean(root.activeElement && root.activeElement.hasAttribute("data-note-input")),
      value: root.querySelector("[data-note-input]").value,
    };
  });
  assert(typed.pressed === "true" && typed.on && typed.focused && typed.value === "Pizza night", JSON.stringify(typed));
  // On to Save with the keyboard, never clicking the note's radio.
  await page.evaluate(() => window.__card.shadowRoot.querySelector('[data-action="save-slot"]').focus());
  await page.keyboard.press("Enter");
  await sleep(700);
  const sets = await calls(page, "hellofresh.set_planned_meal");
  assert(sets.length === 1 && sets[0].data.text === "Pizza night" && sets[0].data.date === "2026-10-17", JSON.stringify(sets));
  assert(/Pizza night/.test(await text(page, line("2026-10-17"))), "Saturday shows the note");
  await page.close();
});

await check("keyboard: + Add, + and − keep focus on the tile's controls, in Menu and Market", async () => {
  const page = await open({ view: "menu" });
  const focused = () => page.evaluate(() => window.__card.shadowRoot.activeElement?.getAttribute("data-focus-key") || null);
  const press = async () => {
    await page.keyboard.press("Enter");
    await sleep(300);
  };
  const key = await page.evaluate(() => {
    const add = window.__card.shadowRoot.querySelector('.hf-tile:not(.selected) [data-action="add"]');
    add.focus();
    return add.getAttribute("data-key");
  });
  await press();
  assert((await focused()) === `meal:${key}:inc`, `after + Add: ${await focused()}`);
  await press();
  assert((await focused()) === `meal:${key}:inc`, `after +: ${await focused()}`);
  const servings = await page.evaluate((k) => window.__card.box.displayMeals(window.__card.selectedWeek()).get(Number(k)), key);
  assert(servings === 2, `servings ${servings}`);
  await page.evaluate((k) => window.__card.shadowRoot.querySelector(`[data-focus-key="meal:${k}:dec"]`).focus(), key);
  await press();
  assert((await focused()) === `meal:${key}:dec`, `after −: ${await focused()}`);
  await press(); // the last serving: out of the box, and the tile is "+ Add" again
  assert((await focused()) === `meal:${key}:add`, `after the last −: ${await focused()}`);

  await click(page, '[data-action="goto-kind"][data-view="market"]', 600);
  const id = await page.evaluate(() => {
    const root = window.__card.shadowRoot;
    const add = root.querySelector('.hf-tile:not(.soldout) [data-action="market-qty"][data-delta="1"]:not([disabled])');
    // Another tile, to see that a quantity change redraws its own tile and leaves the rest.
    window.__otherTile = [...root.querySelectorAll(".hf-grid.market .hf-tile")].find((t) => !t.contains(add));
    add.focus();
    return add.getAttribute("data-id");
  });
  await press();
  assert((await focused()) === `extra:${id}:inc`, `after Add in Market: ${await focused()}`);
  await press();
  assert((await focused()) === `extra:${id}:inc`, `after + in Market: ${await focused()}`);
  assert(await page.evaluate(() => window.__otherTile.isConnected), "the other tiles were left in place");
  await page.close();
});

await check("typing in a search keeps its field in place while the results follow (Menu, Market, Recipes)", async () => {
  for (const [view, key, typed] of [["menu", "meal-search", "tacos"], ["market", "market-search", "bread"], ["recipes", "recipe-search", "chicken"]]) {
    const page = await open({ view });
    await sleep(view === "recipes" ? 600 : 0);
    await page.evaluate((k) => {
      window.__field = window.__card.shadowRoot.querySelector(`[data-input="${k}"]`);
      window.__field.focus();
    }, key);
    await page.keyboard.type(typed, { delay: 30 });
    await sleep(1000);
    const state = await page.evaluate((k) => {
      const root = window.__card.shadowRoot;
      const field = root.querySelector(`[data-input="${k}"]`);
      return {
        same: field === window.__field,
        focused: root.activeElement === field,
        value: field.value,
        clear: !field.parentElement.querySelector(".hf-clear").hidden,
        results: (root.querySelector(".js-results")?.textContent || "").replace(/\s+/g, " ").trim().slice(0, 60),
      };
    }, key);
    assert(state.same && state.focused && state.value === typed && state.clear, `${view}: ${JSON.stringify(state)}`);
    assert(state.results, `${view}: results shown`);
    await page.close();
  }
});

// ---- undo, Load more, one week's people, payments, credit, moved boxes --------------------------

// The toast as it stands: its role, words, whether it offers Undo, and whether it has focus.
const toastState = (page) =>
  page.evaluate(() => {
    const root = window.__card.shadowRoot;
    const toast = root.querySelector(".hf-toast");
    return {
      role: toast ? toast.getAttribute("role") : null,
      text: toast ? toast.textContent.replace(/\s+/g, " ").trim() : "",
      undo: Boolean(toast && toast.querySelector('[data-action="undo"]')),
      focused: Boolean(root.activeElement && root.activeElement.closest(".hf-toast")),
    };
  });
const pressUndo = (page) => page.evaluate(() => window.__card.shadowRoot.querySelector('.hf-toast [data-action="undo"]').click());

await check("undo after a box save puts the old box back in one write and re-reads only that week", async () => {
  const page = await open({ view: "menu" });
  const before = await page.evaluate(() => {
    const week = window.__card.selectedWeek();
    return { week: week.week_id, ids: week.recipes.filter((r) => r.is_selected).map((r) => r.recipe_id) };
  });
  await click(page, '.hf-tile:not(.selected) [data-action="add"]');
  await click(page, '[data-action="goto-kind"][data-view="market"]', 600);
  await click(page, '.hf-tile:not(.selected):not(.soldout) [data-action="market-qty"][data-delta="1"]');
  await click(page, '[data-action="box-save"]', 1500);
  const saved = await toastState(page);
  assert(saved.role === "status" && /Your box is saved/.test(saved.text) && saved.undo && !saved.focused, JSON.stringify(saved));
  const from = (await calls(page)).length;
  await pressUndo(page);
  await sleep(1500);
  const after = (await calls(page)).slice(from).filter((c) => c.domain === "hellofresh");
  const writes = after.filter((c) => c.service.startsWith("select_"));
  assert(writes.length === 1 && writes[0].service === "select_meals", `writes: ${JSON.stringify(writes.map((c) => c.service))}`);
  const data = writes[0].data;
  assert(JSON.stringify([...data.recipe_ids].sort()) === JSON.stringify([...before.ids].sort()), `recipe ids ${JSON.stringify(data)}`);
  assert(data.week_id === before.week && JSON.stringify(data.market_quantities) === "{}", JSON.stringify(data));
  const reads = after.filter((c) => c.service === "get_weeks");
  assert(reads.length === 1 && reads[0].data.week_id === before.week, `re-reads: ${JSON.stringify(reads.map((c) => c.data))}`);
  const undone = await toastState(page);
  assert(/Change undone/.test(undone.text) && !undone.undo, JSON.stringify(undone));
  const box = await page.evaluate(() => {
    const card = window.__card;
    const week = card.selectedWeek();
    return { meals: card.box.displayMeals(week).size, extras: card.box.displayMarket(week).size, dirty: card.box.dirty(week) };
  });
  assert(box.meals === before.ids.length && box.extras === 0 && !box.dirty, JSON.stringify(box));
  await page.close();
});

await check("undo after a skip: a status toast, its Undo next in the Tab order without taking focus, Escape dismisses", async () => {
  const page = await open({ view: "overview" });
  const skip = async () => {
    await click(page, '.hf-hero [data-action="skip"]', 300);
    await page.evaluate(() => window.__card.shadowRoot.querySelector('.hf-sheet [data-action="confirm-skip"]').click());
    await sleep(1200);
  };
  await skip();
  const week = (await calls(page, "hellofresh.skip_week"))[0].data.week_id;
  const skipped = await toastState(page);
  assert(skipped.role === "status" && /Week skipped/.test(skipped.text) && skipped.undo && !skipped.focused, JSON.stringify(skipped));
  // The card's last control, then Tab: the toast's Undo, and Enter takes it.
  const last = await page.evaluate(() => {
    const card = window.__card.shadowRoot.querySelector("ha-card");
    const controls = [...card.querySelectorAll("button, a[href], input, select")].filter((el) => !el.disabled && el.getClientRects().length);
    const el = controls[controls.length - 1];
    el.focus();
    return el.getAttribute("data-action");
  });
  await page.keyboard.press("Tab");
  const onUndo = await page.evaluate(() => window.__card.shadowRoot.activeElement?.getAttribute("data-action"));
  assert(onUndo === "undo", `Tab after the card's last control reached ${onUndo}`);
  await page.keyboard.press("Enter");
  await sleep(1500);
  const unskips = await calls(page, "hellofresh.unskip_week");
  assert(unskips.length === 1 && unskips[0].data.week_id === week, JSON.stringify(unskips));
  const back = await page.evaluate(() => ({
    action: window.__card.shadowRoot.activeElement?.getAttribute("data-action"),
    skipped: window.__card.weekById(window.__calls.find((c) => c.service === "skip_week").data.week_id).is_skipped,
  }));
  assert(back.action === last && back.skipped === false, `after Undo: ${JSON.stringify(back)}, came from ${last}`);
  const undone = await toastState(page);
  assert(/Change undone/.test(undone.text) && !undone.undo, JSON.stringify(undone));
  // Skipped again, then Escape: the toast goes and nothing is written.
  await skip();
  assert((await toastState(page)).undo, "the second skip offers Undo");
  await page.keyboard.press("Escape");
  await sleep(200);
  assert(!(await q(page, ".hf-toast")), "Escape dismisses the toast");
  assert((await calls(page, "hellofresh.unskip_week")).length === 1, "Escape wrote nothing");
  await page.close();
});

await check("undo after a new delivery day moves the box back to the day it had", async () => {
  const page = await open({ view: "overview" });
  await click(page, '.hf-hero [data-action="reschedule"]', 600);
  await page.evaluate(() => window.__card.shadowRoot.querySelectorAll('.hf-sheet [data-action="pick-day"]')[1].click());
  await sleep(1000);
  assert((await toastState(page)).undo, "the new day offers Undo");
  await pressUndo(page);
  await sleep(1200);
  const moves = (await calls(page, "hellofresh.reschedule_week")).map((c) => c.data.delivery_option);
  assert(JSON.stringify(moves) === JSON.stringify(["tue-8-20", "mon-8-20"]), JSON.stringify(moves));
  await page.close();
});

await check("undo of a new delivery day finds the old day among the moved week's other days", async () => {
  // HelloFresh lists a week's OTHER days as its one-off options, never the day the box is on.
  // Make the fake do the same, so the old day appears only once the box has moved.
  const init = () => {
    const others = (w) => {
      const all = [0, 1, 2].map((k) => {
        const [y, m, d] = w.base_date.split("-").map(Number);
        const o = new Date(y, m - 1, d + k);
        const pad = (n) => String(n).padStart(2, "0");
        return { handle: ["mon-8-20", "tue-8-20", "wed-8-20"][k], delivery_date: `${o.getFullYear()}-${pad(o.getMonth() + 1)}-${pad(o.getDate())}` };
      });
      w.available_one_off_options = all.filter((o) => o.delivery_date !== w.delivery_date);
    };
    window.__otherDaysOnly = () => {
      for (const w of window.__fx.weeks) {
        if (!(w.available_one_off_options || []).length) continue;
        w.base_date = w.available_one_off_options[0].delivery_date;
        others(w);
      }
      const real = window.__hass.callService.bind(window.__hass);
      window.__hass.callService = async (domain, service, data, ...rest) => {
        const result = await real(domain, service, data, ...rest);
        if (service === "reschedule_week") others(window.__fx.weeks.find((w) => w.week_id === data.week_id));
        return result;
      };
    };
  };
  const page = await open({ view: "overview" }, undefined, undefined, init);
  await page.evaluate(() => {
    window.__otherDaysOnly();
    return window.__card.refresh({ quiet: true });
  });
  await sleep(600);
  await click(page, '.hf-hero [data-action="reschedule"]', 600);
  await page.evaluate(() => window.__card.shadowRoot.querySelectorAll('.hf-sheet [data-action="pick-day"]')[0].click());
  await sleep(1000);
  assert((await toastState(page)).undo, "the new day offers Undo although its old day was not an option before");
  await pressUndo(page);
  await sleep(1200);
  const moves = (await calls(page, "hellofresh.reschedule_week")).map((c) => c.data.delivery_option);
  assert(JSON.stringify(moves) === JSON.stringify(["tue-8-20", "mon-8-20"]), JSON.stringify(moves));
  await page.close();
});

await check("undo after a recurring plan change selects the option the plan had", async () => {
  const page = await open({ view: "account" });
  await page.evaluate(() => {
    const s = window.__card.shadowRoot.querySelector('[data-plan-control="box_size"]');
    s.value = "4 meals for 2 people";
    s.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
  });
  await sleep(300);
  await page.evaluate(() => window.__card.shadowRoot.querySelector('.hf-sheet [data-action="confirm-plan"]').click());
  await sleep(900);
  const changed = await toastState(page);
  assert(/Box size changed/.test(changed.text) && changed.undo, JSON.stringify(changed));
  await pressUndo(page);
  await sleep(900);
  const options = (await calls(page, "select.select_option")).map((c) => c.data.option);
  assert(JSON.stringify(options) === JSON.stringify(["4 meals for 2 people", "3 meals for 2 people"]), JSON.stringify(options));
  const shown = await page.evaluate(() => {
    const s = window.__card.shadowRoot.querySelector('[data-plan-control="box_size"]');
    return [s.value, s.disabled];
  });
  assert(shown[0] === "3 meals for 2 people" && !shown[1], JSON.stringify(shown));
  assert(/Change undone/.test((await toastState(page)).text), "the undo's toast");
  await page.close();
});

const recipesState = (page) =>
  page.evaluate(() => {
    const root = window.__card.shadowRoot;
    const ids = [...root.querySelectorAll(".js-results .hf-tile .hf-open")].map((b) => b.getAttribute("data-id"));
    return {
      tiles: ids.length,
      unique: new Set(ids).size,
      note: root.querySelector(".js-results .hf-resultnote")?.textContent.trim() || "",
      more: root.querySelector('.js-results [data-action="load-more"]')?.textContent.replace(/\s+/g, " ").trim() || "",
      row: root.querySelector(".js-results .hf-more-row")?.textContent.replace(/\s+/g, " ").trim() || "",
      ids,
    };
  });

await check("recipes: Load more adds the next page, says how many came, and the keyboard moves to the first new recipe", async () => {
  const page = await open({ view: "recipes", config: JSON.stringify({ recipes_limit: 12 }) });
  await sleep(600);
  let s = await recipesState(page);
  assert(s.tiles === 12 && s.note === "Showing 12 of 30 recipes" && s.more === "Load more", JSON.stringify(s));
  await page.evaluate(() => window.__card.shadowRoot.querySelector('[data-action="load-more"]').focus());
  await page.keyboard.press("Enter");
  await sleep(700);
  s = await recipesState(page);
  assert(s.tiles === 24 && s.unique === 24 && s.note === "Showing 24 of 30 recipes" && s.more === "Load more", JSON.stringify(s));
  const focused = await page.evaluate(() => window.__card.shadowRoot.activeElement?.getAttribute("data-id"));
  assert(focused === s.ids[12], `focus on the first new recipe: ${focused}, expected ${s.ids[12]}`);
  const said = await page.evaluate(() => {
    const live = [...window.__card.shadowRoot.querySelectorAll('.sr-only[role="status"]')].map((n) => n.textContent);
    return live.join("|");
  });
  assert(/12 more recipes loaded/.test(said), `announced: ${said}`);
  const pages = (await calls(page, "hellofresh.get_catalog_recipes")).map((c) => [c.data.offset ?? null, c.data.limit]);
  assert(JSON.stringify(pages) === JSON.stringify([[null, 12], [12, 12]]), JSON.stringify(pages));
  await click(page, '[data-action="load-more"]', 700);
  s = await recipesState(page);
  assert(s.tiles === 30 && s.unique === 30 && !s.more && !s.note, `the last page: ${JSON.stringify(s)}`);
  await page.close();
});

await check("recipes: a failed page shows Retry in place of Load more; a page for a category left behind is dropped", async () => {
  const page = await open({ view: "recipes", config: JSON.stringify({ recipes_limit: 12 }) });
  await sleep(600);
  // Next pages (an offset) fail while window.__pagesDown is set, and take window.__pageDelay.
  await page.evaluate(() => {
    window.__pagesDown = true;
    const real = window.__hass.callService.bind(window.__hass);
    window.__hass.callService = async (domain, service, data, ...rest) => {
      if (service === "get_catalog_recipes" && data && data.offset) {
        await new Promise((r) => setTimeout(r, window.__pageDelay || 0));
        if (window.__pagesDown) throw new Error("HelloFresh is down");
      }
      return real(domain, service, data, ...rest);
    };
  });
  await click(page, '[data-action="load-more"]', 600);
  let s = await recipesState(page);
  assert(s.tiles === 12 && /Couldn't load more recipes: HelloFresh is down/.test(s.row) && s.more === "Retry", JSON.stringify(s));
  await page.evaluate(() => {
    window.__pagesDown = false;
  });
  await click(page, '[data-action="load-more"]', 700);
  s = await recipesState(page);
  assert(s.tiles === 24 && s.more === "Load more", `after Retry: ${JSON.stringify(s)}`);
  await page.evaluate(() => {
    window.__pageDelay = 900;
  });
  await click(page, '[data-action="load-more"]', 100);
  await click(page, '[data-action="collection"][data-slug="chicken-recipes"]', 1600);
  s = await recipesState(page);
  assert(s.tiles === 12 && s.note === "Showing 12 of 30 recipes" && s.more === "Load more", `the new category alone: ${JSON.stringify(s)}`);
  await page.close();
});

await check("servings: Change lists this box's sizes, asks first (this week only, with the price), re-reads the week, and Undo changes it back", async () => {
  const page = await open({ view: "menu" });
  const people = () => text(page, ".hf-weekpeople");
  assert(/For 2 people\s*Change/.test(await people()), `week head: ${await people()}`);
  await click(page, '.hf-weekpeople [data-action="servings"]', 700);
  const rows = await page.evaluate(() =>
    [...window.__card.shadowRoot.querySelectorAll('.hf-sheet [data-action="servings-pick"]')].map((b) => [
      Number(b.getAttribute("data-servings")),
      b.disabled,
      b.textContent.replace(/\s+/g, " ").trim(),
    ])
  );
  // This week's three meals in its own (Classic) family: not the Veggie boxes.
  assert(JSON.stringify(rows.map(([n, d]) => [n, d])) === "[[2,true],[4,false],[6,false]]", JSON.stringify(rows));
  assert(/Current/.test(rows[0][2]) && /\$131\.88/.test(rows[1][2]) && /\$179\.82/.test(rows[2][2]), JSON.stringify(rows));
  await page.evaluate(() => window.__card.shadowRoot.querySelector('.hf-sheet [data-servings="4"]').click());
  await sleep(300);
  const confirm = await text(page, ".hf-sheet");
  assert(/Change this box to 4 people\?/.test(confirm) && /Only your box for Monday, October 5 changes/.test(confirm), confirm.slice(0, 200));
  assert(/\$131\.88/.test(confirm), `the price: ${confirm}`);
  assert((await calls(page, "hellofresh.change_week_servings")).length === 0, "nothing written before confirming");
  const from = (await calls(page)).length;
  await page.evaluate(() => window.__card.shadowRoot.querySelector('.hf-sheet [data-action="confirm-servings"]').click());
  await sleep(1300);
  const after = (await calls(page)).slice(from).filter((c) => c.domain === "hellofresh");
  const writes = after.filter((c) => c.service === "change_week_servings");
  const week = await page.evaluate(() => window.__card.selectedWeekId);
  assert(writes.length === 1 && writes[0].data.servings === 4 && writes[0].data.week_id === week, JSON.stringify(writes));
  const reads = after.filter((c) => c.service === "get_weeks");
  assert(reads.length === 1 && reads[0].data.week_id === week, `re-reads: ${JSON.stringify(reads.map((c) => c.data))}`);
  assert(/For 4 people/.test(await people()), `after: ${await people()}`);
  const done = await toastState(page);
  assert(/This box is now for 4 people/.test(done.text) && done.undo, JSON.stringify(done));
  await pressUndo(page);
  await sleep(1300);
  const servings = (await calls(page, "hellofresh.change_week_servings")).map((c) => c.data.servings);
  assert(JSON.stringify(servings) === "[4,2]" && /For 2 people/.test(await people()), JSON.stringify(servings));
  assert(/Change undone/.test((await toastState(page)).text), "the undo's toast");
  // HelloFresh refuses: its own message is what the card says. The sizes were read once.
  await page.evaluate(() => {
    const real = window.__hass.callService.bind(window.__hass);
    window.__hass.callService = async (domain, service, ...rest) => {
      if (service === "change_week_servings") throw new Error("HelloFresh doesn't offer changing the number of people for one week");
      return real(domain, service, ...rest);
    };
  });
  await click(page, '.hf-weekpeople [data-action="servings"]', 400);
  await page.evaluate(() => window.__card.shadowRoot.querySelector('.hf-sheet [data-servings="4"]').click());
  await sleep(200);
  await page.evaluate(() => window.__card.shadowRoot.querySelector('.hf-sheet [data-action="confirm-servings"]').click());
  await sleep(1200);
  const refused = await toastState(page);
  assert(refused.role === "alert" && /Couldn't change this box: HelloFresh doesn't offer changing the number of people for one week/.test(refused.text), JSON.stringify(refused));
  assert((await calls(page, "hellofresh.get_plan_options")).length === 1, "the sizes were read again");
  await page.close();
});

await check("a payment problem: the Overview and Account name the boxes and link to HelloFresh; other sections don't", async () => {
  const page = await open({ view: "overview", scenario: "payment" });
  const banner = () =>
    page.evaluate(() => {
      const n = window.__card.shadowRoot.querySelector(".js-banners .hf-paymentnotice");
      const a = n && n.querySelector("a");
      return n && { text: n.textContent.replace(/\s+/g, " ").trim(), href: a?.getAttribute("href"), target: a?.getAttribute("target"), rel: a?.getAttribute("rel") };
    });
  let b = await banner();
  assert(b, "no payment banner on the Overview");
  assert(/payments for 2 boxes: Monday, September 28 \(\$84\.93\) and Monday, September 21/.test(b.text), b.text);
  assert(b.href === "https://www.hellofresh.com" && b.target === "_blank" && b.rel === "noopener noreferrer", JSON.stringify(b));
  await click(page, '[data-action="nav"][data-view="menu"]', 500);
  assert(!(await banner()), "the Menu shows the payment banner");
  await click(page, '[data-action="nav"][data-view="account"]', 500);
  b = await banner();
  assert(b && /Update your payment details on HelloFresh/.test(b.text), `Account: ${JSON.stringify(b)}`);
  await page.close();
  const clean = await open({ view: "overview" });
  assert(!(await clean.evaluate(() => Boolean(window.__card.shadowRoot.querySelector(".hf-paymentnotice")))), "a banner without a problem");
  await clean.close();
});

await check("account: HelloFresh Cash, bonus credit and the boxes until a free one sit under the credit", async () => {
  const page = await open({ view: "account" });
  const rows = await page.evaluate(() =>
    [...window.__card.shadowRoot.querySelectorAll(".hf-kv > div")].map((d) => [d.querySelector(".hf-k").textContent, d.querySelector(".hf-v").textContent])
  );
  const at = rows.findIndex(([k]) => k === "Credit");
  const got = JSON.stringify(rows.slice(at, at + 4));
  assert(got === JSON.stringify([["Credit", "$12.50"], ["HelloFresh Cash", "$10.00"], ["Bonus credit", "$2.50"], ["Rewards", "3 boxes until a free box"]]), got);
  await page.close();
});

await check("a box HelloFresh moved: the next box, its week's card and row, and the Menu say when it now arrives", async () => {
  const page = await open({ view: "overview", scenario: "delayed" });
  const hero = await text(page, ".hf-hero .hf-revisednotice");
  assert(/Delayed — now arriving Tuesday, October 6/.test(hero), `hero: ${hero}`);
  const cards = await page.evaluate(() => [...window.__card.shadowRoot.querySelectorAll(".hf-weekcard .hf-revised")].map((e) => e.textContent.trim()));
  assert(JSON.stringify(cards) === JSON.stringify(["Early — now arriving Sun, Oct 11"]), `week cards: ${JSON.stringify(cards)}`);
  await click(page, '[data-action="mode"][data-mode="calendar"]', 400);
  await click(page, '[data-action="cal-shift"][data-delta="1"]', 400);
  const rows = await page.evaluate(() => [...window.__card.shadowRoot.querySelectorAll(".hf-row .hf-revised")].map((e) => e.textContent.trim()));
  assert(JSON.stringify(rows) === JSON.stringify(["Delayed — now arriving Tue, Oct 6", "Early — now arriving Sun, Oct 11"]), `rows: ${JSON.stringify(rows)}`);
  await click(page, '[data-action="nav"][data-view="menu"]', 600);
  assert(/Delayed — now arriving Tuesday, October 6/.test(await text(page, ".hf-stack .hf-revisednotice")), "the Menu's week header");
  await page.close();
});

await check("a favourite toggled on Recipes changes its heart without redrawing the grid", async () => {
  const page = await open({ view: "recipes" });
  await sleep(600);
  const from = await page.evaluate(() => {
    for (const tile of window.__card.shadowRoot.querySelectorAll(".js-results .hf-tile")) tile.__mark = true;
    return window.__calls.length;
  });
  await click(page, ".hf-favbtn:not(.on)", 1500);
  const state = await page.evaluate((start) => {
    const tiles = [...window.__card.shadowRoot.querySelectorAll(".js-results .hf-tile")];
    return {
      kept: tiles.length > 0 && tiles.every((t) => t.__mark),
      hearts: tiles.filter((t) => t.querySelector(".hf-favbtn.on")).length,
      reads: window.__calls.slice(start).filter((c) => c.service === "get_weeks").length,
    };
  }, from);
  assert(state.reads >= 1, "the Menu's hearts weren't re-read");
  assert(state.kept, "the weeks read redrew the Recipes grid");
  await page.close();
});

await check("the video sheet has the shared header, named for the clip it closes", async () => {
  const page = await open({ view: "menu" });
  await click(page, ".hf-tile .hf-play", 500);
  const head = await page.evaluate(() => {
    const root = window.__card.shadowRoot;
    return {
      kind: window.__card.sheetKind,
      title: root.querySelector(".hf-sheet .hf-sheethead h2")?.textContent || "",
      close: root.querySelector(".hf-sheet .hf-sheethead [data-close-sheet]")?.getAttribute("aria-label"),
    };
  });
  assert(head.kind === "video" && head.title && head.close === "Close video", JSON.stringify(head));
  await page.keyboard.press("Escape");
  await sleep(200);
  assert(!(await q(page, ".hf-sheet")), "Escape closes it");
  await page.close();
});

await browser.close();
server.close();
const failed = results.filter((r) => r.startsWith("FAIL"));
console.log(`\n${results.length - failed.length} of ${results.length} flows passed`);
if (failed.length) console.log(failed.join("\n"));
if (errors.length) console.log("ERRORS:\n" + [...new Set(errors)].join("\n"));
process.exit(failed.length || errors.length ? 1 : 0);
