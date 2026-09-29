/*
 * HelloFresh card — Menu & Market (the week views)
 * ------------------------------------------------
 * Both views share one week strip and one pending box per week (BoxStore), so meals picked on
 * the Menu tab and add-ons picked on the Market tab are the same unsaved box. The sticky box bar
 * (rendered by the card under either view) summarises it, estimates its price while you edit,
 * and saves it:
 *
 *   * meals only   -> hellofresh.select_meals
 *   * extras only  -> hellofresh.select_market_items
 *   * both         -> hellofresh.select_meals with `market_quantities`, ONE cart write.
 *
 * The combined write matters: each service rebuilds the whole cart from the integration's last
 * poll, so two back-to-back writes could let the second one put the first one's old selection
 * back. Carrying both halves in one request makes that impossible.
 *
 * Grid behaviour (filters, dedupe, variant grouping, sold-out, videos, favourites, the recipe
 * sheet's Add/servings footer) carries over from the classic meal planner and Market cards.
 */

const MENU_VERSION = new URL(import.meta.url).searchParams.get("v") || "unknown";
const stamp = encodeURIComponent(MENU_VERSION);
const [L, UI] = await Promise.all([
  import(new URL(`./hellofresh-card-logic.js?v=${stamp}`, import.meta.url).href),
  import(new URL(`./hellofresh-card-ui.js?v=${stamp}`, import.meta.url).href),
]);

const { esc } = L;
const { icon, pill } = UI;

// Tiles rendered before a "Show more" button: a planning-catalog week carries ~350 meals, and
// painting them all at once is what made the classic planner slow to open.
const PAGE_SIZE = 60;

const nextFrame = () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));

// ---- the pending box -----------------------------------------------------------------------

export class BoxStore {
  constructor(card) {
    this.card = card;
    this.meals = {}; // week_id -> Map(selKey -> servings)
    this.market = {}; // week_id -> Map(item_id -> quantity)
    // What a successful save just wrote, per week, shown as the saved state until a fetch that
    // STARTED after the save lands. A fetch already in flight when the save finished carries the
    // old box; without this, its response would briefly show the pre-save selection.
    this.committed = {}; // week_id -> {meals, market, seq}
    this._savedMeals = new WeakMap();
    this._savedMarket = new WeakMap();
    this._deduped = new WeakMap();
    this._preview = new Map();
    this._previewBroken = new Set();
    this._previewTimer = null;
    this.downgradedWeek = null;
  }

  // Saved state and dedupe depend only on the week object, which is replaced on every fetch —
  // so a WeakMap keyed on it invalidates itself.
  savedMeals(week) {
    const committed = this.committed[week.week_id];
    if (committed) return committed.meals;
    let map = this._savedMeals.get(week);
    if (!map) {
      map = L.savedMealSelection(week);
      this._savedMeals.set(week, map);
    }
    return map;
  }

  savedMarket(week) {
    const committed = this.committed[week.week_id];
    if (committed) return committed.market;
    let map = this._savedMarket.get(week);
    if (!map) {
      map = L.savedMarketSelection(week);
      this._savedMarket.set(week, map);
    }
    return map;
  }

  deduped(week) {
    let entry = this._deduped.get(week);
    if (!entry) {
      entry = L.dedupeRecipes(week.recipes || []);
      this._deduped.set(week, entry);
    }
    return entry;
  }

  displayMeals(week) {
    return this.meals[week.week_id] || this.savedMeals(week);
  }

  displayMarket(week) {
    return this.market[week.week_id] || this.savedMarket(week);
  }

  mealsDirty(week) {
    const pending = this.meals[week.week_id];
    return Boolean(pending) && !L.selectionsEqual(pending, this.savedMeals(week));
  }

  marketDirty(week) {
    const pending = this.market[week.week_id];
    return Boolean(pending) && !L.selectionsEqual(pending, this.savedMarket(week));
  }

  dirty(week) {
    return this.mealsDirty(week) || this.marketDirty(week);
  }

  dirtyWeekId(weekId) {
    const week = this.card.weekById(weekId);
    return Boolean(week) && this.dirty(week);
  }

  canEdit(week) {
    return !this.card.busy && L.isWeekEditable(week);
  }

  _pendingMeals(week) {
    let pending = this.meals[week.week_id];
    if (!pending) {
      pending = new Map(this.savedMeals(week));
      this.meals[week.week_id] = pending;
    }
    return pending;
  }

  _pendingMarket(week) {
    let pending = this.market[week.week_id];
    if (!pending) {
      pending = new Map(this.savedMarket(week));
      this.market[week.week_id] = pending;
    }
    return pending;
  }

  // "+ Add": into the box at one serving. Returns whether anything changed.
  addMeal(week, recipe) {
    if (!this.canEdit(week)) return false;
    const pending = this._pendingMeals(week);
    const idx = L.activeIndex(pending, recipe);
    if (idx == null || pending.has(idx)) return false;
    pending.set(idx, 1);
    return true;
  }

  // Servings stepper, clamped to [0, MAX]; zero removes the meal.
  changeMeal(week, recipe, delta) {
    if (!this.canEdit(week)) return false;
    const pending = this._pendingMeals(week);
    const idx = L.activeIndex(pending, recipe);
    if (idx == null) return false;
    const current = pending.get(idx) || 0;
    const next = Math.max(0, Math.min(L.MAX_MEAL_SERVINGS, current + delta));
    if (next === current) return false;
    if (next === 0) pending.delete(idx);
    else pending.set(idx, next);
    return true;
  }

  // Customization: a chosen dish switches to another of its options, keeping its servings (the
  // cart holds the option's own meal in place of the old one, as the website does).
  swapMeal(week, fromRecipe, toRecipe) {
    if (!this.canEdit(week)) return false;
    const pending = this._pendingMeals(week);
    const from = L.activeIndex(pending, fromRecipe);
    const to = L.activeIndex(pending, toRecipe);
    if (from == null || to == null || from === to || !pending.has(from)) return false;
    const servings = pending.get(from);
    pending.delete(from);
    pending.set(to, Math.min(L.MAX_MEAL_SERVINGS, (pending.get(to) || 0) + servings));
    return true;
  }

  // Market stepper, clamped to the item's cap; a sold-out item can only go down.
  changeMarket(week, item, delta) {
    if (!this.canEdit(week)) return false;
    if (delta > 0 && item.is_sold_out === true) return false;
    const pending = this._pendingMarket(week);
    const current = pending.get(item.item_id) || 0;
    const next = Math.max(0, Math.min(L.marketCap(item), current + delta));
    if (next === current) return false;
    if (next === 0) pending.delete(item.item_id);
    else pending.set(item.item_id, next);
    return true;
  }

  discard(week) {
    delete this.meals[week.week_id];
    delete this.market[week.week_id];
  }

  clearDowngrade() {
    this.downgradedWeek = null;
  }

  // After a fetch: drop edits for weeks that vanished, keys the refreshed menu no longer has,
  // and edits that now match what is saved. Unlike the classic cards, a background refresh does
  // NOT throw away a half-built box. `fetchSeq` is the landing fetch's start number: a committed
  // save gives way to the server only once a fetch begun after it arrives.
  reconcile(weeks, fetchSeq = Infinity) {
    for (const [weekId, entry] of Object.entries(this.committed)) {
      if (fetchSeq > entry.seq) delete this.committed[weekId];
    }
    const byId = new Map((weeks || []).map((w) => [w.week_id, w]));
    for (const [store, validKeys, saved] of [
      [this.meals, (w) => new Set((w.recipes || []).map((r) => L.selKey(r))), (w) => this.savedMeals(w)],
      [this.market, (w) => new Set((w.market_items || []).map((i) => i.item_id)), (w) => this.savedMarket(w)],
    ]) {
      for (const weekId of Object.keys(store)) {
        const week = byId.get(weekId);
        if (!week || !L.isWeekEditable(week)) {
          delete store[weekId];
          continue;
        }
        const keys = validKeys(week);
        for (const key of [...store[weekId].keys()]) if (!keys.has(key)) store[weekId].delete(key);
        if (L.selectionsEqual(store[weekId], saved(week))) delete store[weekId];
      }
    }
    this._preview.clear();
  }

  // ---- live price estimate (hellofresh.preview_meal_price) ------------------------------------

  _previewKey(week) {
    const entries = [...this.displayMeals(week).entries()].sort((a, b) => String(a[0]).localeCompare(String(b[0])));
    return `${week.week_id}|${JSON.stringify(entries)}`;
  }

  preview(week) {
    if (!this.mealsDirty(week)) return null;
    return this._preview.get(this._previewKey(week)) || null;
  }

  // Price the pending meals once the user pauses (debounced, cached per selection). A week whose
  // preview fails once is not asked again — the estimate is a nicety, never worth an error loop.
  schedulePreview(week) {
    clearTimeout(this._previewTimer);
    if (!this.mealsDirty(week) || !L.isWeekEditable(week) || this._previewBroken.has(week.week_id)) return;
    if (L.mealsCount(this.displayMeals(week)) < L.MIN_MEALS) return;
    const key = this._previewKey(week);
    if (this._preview.has(key)) return;
    this._previewTimer = setTimeout(() => this._fetchPreview(week, key), 650);
  }

  async _fetchPreview(week, key) {
    const write = L.buildMealWrite(week, this.displayMeals(week));
    if (!write.recipe_ids.length) return;
    this._preview.set(key, { status: "loading" });
    this.card.renderBoxBar();
    try {
      const data = { week_id: week.week_id, recipe_ids: write.recipe_ids };
      if (Object.keys(write.quantities).length) data.quantities = write.quantities;
      this._preview.set(key, { status: "ok", data: await this.card.call("preview_meal_price", data) });
    } catch (err) {
      this._preview.set(key, { status: "error" });
      this._previewBroken.add(week.week_id);
      // eslint-disable-next-line no-console
      console.warn("hellofresh: price preview unavailable", err);
    }
    this.card.renderBoxBar();
    if (this.card.sheetKind === "review") this.card.renderSheet();
  }

  // ---- save -----------------------------------------------------------------------------------

  async save(week) {
    const card = this.card;
    if (card.busy || !this.dirty(week)) return;
    if (!L.isWeekEditable(week)) {
      // The deadline passed while the box was being edited; HelloFresh would reject the write.
      card.toast("Changes are closed for this week — its box ships as it was saved.", true);
      return;
    }
    const mealsDirty = this.mealsDirty(week);
    const marketDirty = this.marketDirty(week);
    if (mealsDirty) {
      const count = L.mealsCount(this.displayMeals(week));
      if (count < L.MIN_MEALS) {
        card.toast(`Choose at least ${L.MIN_MEALS} meals before saving (${count} selected).`, true);
        return;
      }
    }
    let service;
    let data;
    if (mealsDirty) {
      const write = L.buildMealWrite(week, this.displayMeals(week));
      service = "select_meals";
      data = { week_id: week.week_id, recipe_ids: write.recipe_ids };
      if (Object.keys(write.quantities).length) data.quantities = write.quantities;
      if (marketDirty) data.market_quantities = L.buildMarketWrite(this.displayMarket(week));
    } else {
      service = "select_market_items";
      data = { week_id: week.week_id, quantities: L.buildMarketWrite(this.displayMarket(week)) };
    }
    if (card.sheetKind === "review") card.closeSheet();
    card.setSaving("Saving your box…");
    card.renderView();
    // Let the banner paint before a fast service call and reload can pre-empt it.
    await nextFrame();
    let failed = null;
    let downgraded = false;
    try {
      const response = await card.call(service, data);
      downgraded = Boolean(response && response.downgraded);
    } catch (err) {
      failed = (err && err.message) || String(err);
    }
    if (!failed) {
      this.committed[week.week_id] = {
        meals: new Map(this.displayMeals(week)),
        market: new Map(this.displayMarket(week)),
        seq: card.fetchSeq,
      };
      this.discard(week);
      card.broadcastDataChanged();
    }
    card.setSaving(null);
    await card.reloadWeeks();
    if (failed) card.toast(`Couldn't save your box: ${failed}`, true);
    else if (downgraded) {
      this.downgradedWeek = week.week_id;
      card.renderView();
    } else card.toast("Your box is saved.");
  }
}

// ---- week strip & header (shared by Menu and Market) --------------------------------------

function renderWeekStrip(card, selected) {
  const weeks = card.weeks;
  const next = L.nextBoxWeek(weeks);
  const index = weeks.indexOf(selected);
  const chips = weeks
    .map((w) => {
      const state = L.weekState(w);
      const meta = L.STATE_META[state];
      const label = state === "needs" && L.isAutoPicked(w) ? "Review" : meta.short;
      const isNext = next.upcoming && next.week === w;
      const dirty = card.box.dirty(w);
      return `<button class="hf-weekchip ${state}${L.isPastWeek(w) ? " past" : ""}" data-action="select-week"
          data-week-id="${esc(w.week_id)}" aria-pressed="${w === selected}"
          title="${esc(`${w.display_name || w.week_id} · ${L.stateLabel(w, state)}`)}">
          ${isNext ? `<span class="hf-wcnow">Next</span>` : ""}
          ${dirty ? `<span class="hf-wcdirty" title="Unsaved changes"></span>` : ""}
          <span class="hf-wcday">${esc(L.fmtWeekday(w.delivery_date))}</span>
          <span class="hf-wcdate">${esc(L.fmtDateShort(w.delivery_date))}</span>
          <span class="hf-wcstate"><span class="hf-wcdot ${state}"></span>${esc(label)}</span>
        </button>`;
    })
    .join("");
  return `<div class="hf-weekbar">
      <button class="hf-iconbtn" data-action="week-step" data-step="-1" aria-label="Previous week"
        ${index <= 0 ? "disabled" : ""}>${icon("mdi:chevron-left")}</button>
      <div class="hf-weekstrip" data-scroll-key="weekstrip">${chips}</div>
      <button class="hf-iconbtn" data-action="week-step" data-step="1" aria-label="Next week"
        ${index >= weeks.length - 1 ? "disabled" : ""}>${icon("mdi:chevron-right")}</button>
    </div>`;
}

// Keep the chosen week visible in the strip — but only when the chosen week changed, so a user
// scrolling the strip isn't yanked back on every re-render.
function centerStrip(root, view, week) {
  const strip = root.querySelector(".hf-weekstrip");
  const chip = strip && strip.querySelector('[aria-pressed="true"]');
  if (!chip || !week) return;
  const changed = view._stripWeek !== week.week_id;
  const first = view._stripWeek === null;
  view._stripWeek = week.week_id;
  // Measured against the strip itself: a chip's offsetLeft is relative to the nearest
  // positioned ancestor, which is the card, not the scroller.
  const s = strip.getBoundingClientRect();
  const c = chip.getBoundingClientRect();
  const visible = c.left >= s.left + 16 && c.right <= s.right - 16;
  if (!changed && visible) return;
  const delta = c.left - s.left - (strip.clientWidth - c.width) / 2;
  strip.scrollTo({ left: Math.max(0, strip.scrollLeft + delta), behavior: changed && !first ? "smooth" : "instant" });
}

function renderWeekHead(card, week, kind) {
  const state = L.weekState(week);
  const editable = L.isWeekEditable(week);
  const rel = L.relativeWeek(week);
  const deadline = week.selection_deadline ? new Date(week.selection_deadline) : null;
  const pills = [UI.statePill(week, state)];
  if (L.wasPreselected(week) && L.stateLabel(week, state) !== "Preselected") {
    pills.push(pill("Preselected", "warn", null, "HelloFresh picked this week's meals."));
  }
  if (L.isHolidayShifted(week)) {
    pills.push(pill("Holiday schedule", "info", "mdi:calendar-star", week.holiday_message || ""));
  }
  const benefit = L.weekBenefit(week);
  if (benefit) pills.push(pill(benefit.label, "ok", "mdi:ticket-percent-outline", benefit.voucher_code ? `Voucher ${benefit.voucher_code}` : ""));
  const sub = [rel, week.display_name, week.slot_label].filter(Boolean).map(esc).join(" · ");

  const deadlineLine =
    editable && deadline
      ? `<div class="hf-weekdeadline${L.deadlineTone(deadline) === "urgent" ? " urgent" : ""}">${icon("mdi:timer-outline")}Make changes by
          <strong>${esc(L.fmtDateTime(deadline))}</strong> · ${UI.countdownHtml(deadline)}</div>`
      : "";
  const actions = [];
  const pantryId = card.pantryEntityFor(week);
  if (pantryId && kind === "menu") {
    const s = card.entityState(pantryId);
    const count = s && Number(s.state);
    actions.push(`<button class="hf-btn sm" data-action="pantry" data-entity="${esc(pantryId)}">
      ${icon("mdi:basket-outline")}Pantry${Number.isFinite(count) && count > 0 ? ` · ${count}` : ""}</button>`);
  }
  if (L.canReschedule(week)) {
    actions.push(`<button class="hf-btn sm" data-action="reschedule" ${card.busy ? "disabled" : ""}>
      ${icon("mdi:calendar-edit")}Change day</button>`);
  }
  if (L.canSkip(week)) {
    const skipped = L.isSkipped(week);
    actions.push(`<button class="hf-btn sm${skipped ? " primary" : ""}" data-action="skip" ${card.busy ? "disabled" : ""}>
      ${icon(skipped ? "mdi:restore" : "mdi:debug-step-over")}${skipped ? "Unskip week" : "Skip week"}</button>`);
  }
  return `<div class="hf-weekhead">
      <div class="hf-weektitle">
        <div class="hf-weekdate">${esc(L.fmtLongDate(week.delivery_date))}</div>
        <div class="hf-weekpills">${pills.join("")}${sub ? `<span class="hf-weeksub">${sub}</span>` : ""}</div>
        ${deadlineLine}
      </div>
      <div class="hf-actions">${actions.join("")}</div>
    </div>`;
}

// Notices and delivery detail between the header and the grid.
function renderWeekNotices(card, week, kind, view) {
  const out = [];
  if (card.box.downgradedWeek === week.week_id) {
    out.push(`<div class="hf-notice tone-warn" role="alert">${icon("mdi:alert-outline")}
      <div class="hf-noticebody">HelloFresh <strong>downsized this box</strong> to fit your plan — fewer
        items were saved than you picked. Check the saved box below.</div>
      <button class="hf-iconbtn" data-action="dismiss-downgrade" aria-label="Dismiss">${icon("mdi:close")}</button></div>`);
  }
  if (L.isSkipped(week)) {
    out.push(`<div class="hf-notice tone-muted">${icon("mdi:calendar-remove-outline")}
      <div class="hf-noticebody"><strong>This week is skipped.</strong> Nothing ships and you won't be charged.
        ${L.canSkip(week) ? "Unskip it to choose meals." : ""}</div>
      ${L.canSkip(week) ? `<button class="hf-btn sm primary" data-action="skip" ${card.busy ? "disabled" : ""}>Unskip</button>` : ""}</div>`);
  } else if (kind === "menu" && L.isAutoPicked(week)) {
    const deadline = week.selection_deadline ? new Date(week.selection_deadline) : null;
    out.push(`<div class="hf-notice tone-warn">${icon("mdi:auto-fix")}
      <div class="hf-noticebody"><strong>HelloFresh picked these meals for you.</strong> Keep them or swap any
        ${deadline ? `before ${esc(L.fmtDateTime(deadline))}` : "before the deadline"}.</div></div>`);
  } else if (!L.isWeekEditable(week) && !L.isPastWeek(week) && !L.isDelivered(week) && !L.isShipping(week)) {
    out.push(`<div class="hf-notice tone-muted">${icon("mdi:lock-outline")}
      <div class="hf-noticebody">Changes are closed for this box — it ships as shown.</div></div>`);
  }
  const history = L.isHistoryWeek(week, card.menuGraceWeeks());
  if (L.trackingStep(week) >= 0 && !history) {
    const total = L.boxTotal(week, card.account);
    const extra = [];
    if (total) extra.push(`${esc(total.label)} <strong>${esc(L.fmtPrice(total.amount, total.currency))}</strong>`);
    if (week.order && week.order.order_id) extra.push(`Order ${esc(week.order.order_id)}`);
    out.push(`<div class="hf-panel flat">${UI.trackingBlock(week, { historyOpen: view._historyOpen === week.week_id })}
      ${extra.length ? `<div class="hf-trackline" style="margin-top:8px">${extra.join('<span aria-hidden="true">·</span>')}</div>` : ""}</div>`);
  } else if ((week.order || L.isPastWeek(week)) && !L.isWeekEditable(week) && !L.isSkipped(week)) {
    // An editable week's total already lives in the box bar; the strip is for boxes that are
    // settled (locked, shipped, delivered).
    out.push(UI.orderStrip(week, card.account));
  }
  return `<div class="hf-stack">${out.join("")}</div>`;
}

// The Meals | Extras switch at the start of each week view's toolbar.
function renderKindSwitch(card, week, kind) {
  if (!card.hasView("menu") || !card.hasView("market")) return "";
  const meals = card.box.displayMeals(week).size;
  const extras = card.box.displayMarket(week).size;
  const btn = (key, label, iconName, count) =>
    `<button data-action="goto-kind" data-view="${key}" aria-pressed="${kind === key}">${icon(iconName)}${label}${
      count ? ` <span class="hf-muted">${count}</span>` : ""
    }</button>`;
  return `<div class="hf-segment" role="group" aria-label="Meals or extras">${btn("menu", "Meals", "mdi:silverware-fork-knife", meals)}${btn(
    "market",
    "Extras",
    "mdi:storefront-outline",
    extras
  )}</div>`;
}

function searchBox(key, value, placeholder) {
  return `<label class="hf-search"><span class="sr-only">${esc(placeholder)}</span>${icon("mdi:magnify")}
      <input type="search" data-focus-key="${key}" data-input="${key}" value="${esc(value)}"
        placeholder="${esc(placeholder)}" spellcheck="false" autocomplete="off">
      ${value ? `<button class="hf-iconbtn hf-clear" data-action="clear-search" aria-label="Clear search">${icon("mdi:close")}</button>` : ""}
    </label>`;
}

function stepSelectedWeek(card, delta) {
  const weeks = card.weeks || [];
  const idx = weeks.findIndex((w) => w.week_id === card.selectedWeekId);
  const next = weeks[idx + delta];
  if (next) card.selectWeek(next.week_id);
}

// Actions both week views handle identically. Returns true when handled.
function handleWeekAction(card, view, week, action, el) {
  switch (action) {
    case "select-week":
      card.selectWeek(el.getAttribute("data-week-id"));
      return true;
    case "week-step":
      stepSelectedWeek(card, Number(el.getAttribute("data-step")) || 0);
      return true;
    case "goto-kind":
      card.navigate(el.getAttribute("data-view"));
      return true;
    case "skip":
      card.confirmSkip(week);
      return true;
    case "reschedule":
      card.openReschedule(week);
      return true;
    case "pantry":
      card.openPantry(el.getAttribute("data-entity"), week);
      return true;
    case "dismiss-downgrade":
      card.box.clearDowngrade();
      card.renderView();
      return true;
    case "toggle-history":
      view._historyOpen = view._historyOpen === week.week_id ? null : week.week_id;
      card.renderView();
      return true;
    default:
      return false;
  }
}

// ---- Menu (meals) --------------------------------------------------------------------------

function loadSet(key, allowed = null) {
  try {
    const raw = L.storageGet(key);
    const list = raw ? JSON.parse(raw) : [];
    return new Set((Array.isArray(list) ? list : []).filter((v) => typeof v === "string" && (!allowed || allowed.includes(v))));
  } catch (_e) {
    return new Set();
  }
}

function loadHighlight() {
  const raw = L.storageGet(L.STORAGE_KEYS.highlight, "") || "";
  const allowed = L.HIGHLIGHT_FILTERS.map((f) => f.key);
  if (allowed.includes(raw)) return raw;
  if (raw.startsWith("[")) {
    try {
      return JSON.parse(raw).find((k) => allowed.includes(k)) || "";
    } catch (_e) {
      return "";
    }
  }
  return "";
}

function loadServerFilter() {
  let stored = {};
  try {
    const raw = L.storageGet(L.STORAGE_KEYS.server);
    stored = raw ? JSON.parse(raw) : {};
  } catch (_e) {
    stored = {};
  }
  const out = {};
  for (const group of L.SERVER_FILTER_GROUPS) {
    const slugs = stored && Array.isArray(stored[group]) ? stored[group] : [];
    out[group] = new Set(slugs.filter((s) => typeof s === "string" && s));
  }
  return out;
}

export class MealsView {
  constructor(card) {
    this.card = card;
    this.query = "";
    this.limit = PAGE_SIZE;
    const K = L.STORAGE_KEYS;
    this.f = {
      selectedOnly: L.storageGet(K.showSelectedOnly) === "1",
      protein: loadSet(K.protein, L.PROTEIN_FILTERS),
      diet: loadSet(K.diet, L.DIET_FILTERS.map((f) => f.key)),
      time: L.TIME_FILTERS.some((f) => f.key === L.storageGet(K.time)) ? L.storageGet(K.time) : "",
      highlight: loadHighlight(),
      section: L.storageGet(K.section, "") || "",
      server: loadServerFilter(),
      expanded: L.storageGet(K.filtersExpanded) === "1",
    };
    this._serverCache = new Map();
    this._serverPending = new Set();
    this._rendered = new Map();
    this._stripWeek = null;
    this._historyOpen = null;
    this._searchTimer = null;
  }

  onData() {
    this._serverCache = new Map(); // a menu change can change what a filter returns
  }

  onWeekChange() {
    this.limit = PAGE_SIZE;
  }

  // ---- filter state ------------------------------------------------------------------------

  _persist() {
    const K = L.STORAGE_KEYS;
    const f = this.f;
    L.storageSet(K.showSelectedOnly, f.selectedOnly ? "1" : "0");
    L.storageSet(K.protein, JSON.stringify([...f.protein]));
    L.storageSet(K.diet, JSON.stringify([...f.diet]));
    L.storageSet(K.time, f.time);
    L.storageSet(K.highlight, f.highlight);
    L.storageSet(K.section, f.section);
    const server = {};
    for (const [group, set] of Object.entries(f.server)) server[group] = [...set];
    L.storageSet(K.server, JSON.stringify(server));
    L.storageSet(K.filtersExpanded, f.expanded ? "1" : "0");
  }

  // Every active narrowing, as removable {kind, value, label} chips. A section or server option
  // the viewed week doesn't carry filters nothing there, so it doesn't count.
  _activeFilters(week) {
    const f = this.f;
    const out = [];
    for (const p of L.PROTEIN_FILTERS) if (f.protein.has(p)) out.push({ kind: "protein", value: p, label: p });
    for (const d of L.DIET_FILTERS) if (f.diet.has(d.key)) out.push({ kind: "diet", value: d.key, label: d.label });
    const time = L.TIME_FILTERS.find((t) => t.key === f.time);
    if (time) out.push({ kind: "time", value: time.key, label: time.label });
    const highlight = L.HIGHLIGHT_FILTERS.find((h) => h.key === f.highlight);
    if (highlight) out.push({ kind: "highlight", value: highlight.key, label: highlight.label });
    if (L.menuSectionIds(week, f.section)) {
      const row = (week.menu_categories || []).find((c) => c.slug === f.section);
      if (row) out.push({ kind: "section", value: row.slug, label: row.name });
    }
    for (const group of L.serverFilterGroups(week)) {
      const set = f.server[group.slug];
      for (const o of group.options) {
        if (set && set.has(o.slug)) out.push({ kind: group.slug, value: o.slug, label: o.name });
      }
    }
    return out;
  }

  _removeFilter(kind, value) {
    const f = this.f;
    if (kind === "protein") f.protein.delete(value);
    else if (kind === "diet") f.diet.delete(value);
    else if (kind === "time") f.time = "";
    else if (kind === "highlight") f.highlight = "";
    else if (kind === "section") f.section = "";
    else if (f.server[kind]) f.server[kind].delete(value);
  }

  _clearFilters() {
    const f = this.f;
    f.protein.clear();
    f.diet.clear();
    f.time = "";
    f.highlight = "";
    f.section = "";
    for (const set of Object.values(f.server)) set.clear();
  }

  // HelloFresh's own filter service answers cuisine / dish type / ingredients to avoid (menu
  // recipes carry neither allergen data nor those slugs), and places the meals the menu leaves
  // without a protein for the protein chips. Cached per week + query; while a lookup runs the
  // grid stays as it is, and a failure falls back to the client-side filters.
  _lookup(week, filters) {
    if (!Object.keys(filters).length) return null;
    const key = L.serverFilterKey(week.week_id, filters);
    if (this._serverCache.has(key)) return this._serverCache.get(key);
    if (!this._serverPending.has(key)) this._fetchServerIds(week.week_id, filters, key);
    return null;
  }

  async _fetchServerIds(weekId, filters, key) {
    this._serverPending.add(key);
    try {
      const response = await this.card.call("get_menu_courses", { week_id: weekId, filters });
      this._serverCache.set(key, new Set((response.recipe_ids || []).map((id) => String(id).split("-")[0])));
    } catch (err) {
      // eslint-disable-next-line no-console
      console.warn("hellofresh: menu filter lookup failed; filtering on the menu data alone", err);
      this._serverCache.set(key, null);
    } finally {
      this._serverPending.delete(key);
      if (this.card.view === "menu") this.card.renderView();
    }
  }

  // ---- render ------------------------------------------------------------------------------

  render() {
    const card = this.card;
    const placeholder = card.weeksPlaceholder();
    if (placeholder) return placeholder;
    if (!card.weeks.length) {
      return `<div class="hf-empty">${icon("mdi:calendar-blank-outline")}No delivery weeks found.</div>`;
    }
    const week = card.selectedWeek() || card.weeks[0];
    return `${renderWeekStrip(card, week)}${renderWeekHead(card, week, "menu")}${renderWeekNotices(
      card,
      week,
      "menu",
      this
    )}${this._renderBody(week)}`;
  }

  afterRender(root) {
    centerStrip(root, this, this.card.selectedWeek());
  }

  _renderBody(week) {
    const card = this.card;
    const box = card.box;
    const history = L.isHistoryWeek(week, card.menuGraceWeeks());
    const { recipes } = box.deduped(week);
    this._rendered = new Map(recipes.map((r) => [String(L.selKey(r)), r]));
    this._units = new Map();
    if (!recipes.length) {
      const message = L.isSkipped(week)
        ? "No meals this week."
        : L.isPastWeek(week)
          ? "No meals were delivered this week."
          : "This week's menu isn't published yet.";
      return `<div class="hf-empty">${icon("mdi:silverware-clean")}${esc(message)}</div>`;
    }
    const selection = box.displayMeals(week);
    const sel = (r) => L.tileSelected(selection, r);
    const f = this.f;
    const applyFilters = !history && !f.selectedOnly;
    const sectionIds = applyFilters ? L.menuSectionIds(week, f.section) : null;
    const serverIds = applyFilters ? this._lookup(week, L.activeServerFilters(week, f.server)) : null;
    const proteinIds = applyFilters ? this._lookup(week, L.proteinServerFilters(week, f.protein)) : null;
    const visible = L.menuTiles(recipes, {
      sel,
      selectedOnly: f.selectedOnly && !history,
      applyFilters,
      protein: f.protein,
      proteinIds,
      diet: f.diet,
      time: f.time,
      highlight: f.highlight,
      sectionIds,
      serverIds,
      query: this.query,
    });
    for (const unit of visible) this._units.set(String(L.selKey(unit.recipe)), unit);
    const total = L.menuTiles(recipes, { sel }).length; // the grid before any narrowing
    const ctx = this._tileContext(week, { history, selection, sel });

    const active = applyFilters ? this._activeFilters(week) : [];
    const toolbar = `<div class="hf-toolbar">
        ${renderKindSwitch(card, week, "menu")}
        ${searchBox("meal-search", this.query, history ? "Search delivered meals" : "Search this week's menu")}
        ${history ? "" : `<button class="hf-chip${f.expanded ? " on" : ""}" data-action="toggle-filters" aria-expanded="${f.expanded}">
            ${icon("mdi:tune-variant")}Filters${active.length ? ` · ${active.length}` : ""}</button>
          <button class="hf-chip${f.selectedOnly ? " on" : ""}" data-action="toggle-selected-only" aria-pressed="${f.selectedOnly}">
            ${icon("mdi:package-variant-closed")}In my box</button>`}
        <span class="hf-spacer"></span>
        ${this._serverPending.size ? `<span class="hf-busynote" role="status">Filtering…</span>` : ""}
      </div>`;
    const chips =
      active.length && !f.expanded
        ? `<div class="hf-activefilters">${active
            .map(
              (a) => `<button class="hf-chip on" data-action="remove-filter" data-kind="${esc(a.kind)}" data-value="${esc(a.value)}"
                  title="Remove: ${esc(a.label)}">${esc(a.label)}${icon("mdi:close")}</button>`
            )
            .join("")}<button class="hf-link hf-small" data-action="clear-filters">Clear all</button></div>`
        : "";
    const panel = applyFilters && f.expanded ? this._renderFilterPanel(week, active) : "";

    let grid;
    if (!visible.length) {
      const why = this.query
        ? `No meals match “${esc(this.query.trim())}”.`
        : f.selectedOnly
          ? "Nothing in your box yet — turn off “In my box” to browse the menu."
          : "No meals match these filters.";
      grid = `<div class="hf-empty">${icon("mdi:food-off-outline")}${why}</div>`;
    } else {
      const shown = visible.slice(0, this.limit);
      const narrowed = visible.length !== total;
      const note = narrowed
        ? `<p class="hf-resultnote">${visible.length} of ${L.plural(total, "meal")}</p>`
        : history
          ? `<p class="hf-resultnote">Delivered this week</p>`
          : "";
      grid = `${note}<div class="hf-grid${card.busy ? " busy" : ""}">${shown.map((unit) => this._tile(unit, ctx)).join("")}</div>
        ${visible.length > shown.length ? `<div class="hf-more-row"><button class="hf-btn" data-action="more">
          Show ${Math.min(PAGE_SIZE, visible.length - shown.length)} more · ${visible.length - shown.length} left</button></div>` : ""}`;
    }
    return `${toolbar}${chips}${panel}${grid}`;
  }

  _filterRow(label, chips) {
    return `<div class="hf-frow"><span class="hf-flabel">${esc(label)}</span><div class="hf-chiprow">${chips}</div></div>`;
  }

  _chip(action, value, label, on, extra = "") {
    return `<button class="hf-chip${on ? " on" : ""}" data-action="${action}" data-value="${esc(value)}" aria-pressed="${on}"${extra}>${label}</button>`;
  }

  _renderFilterPanel(week, active) {
    const f = this.f;
    const rows = [];
    const sections = week.menu_categories || [];
    if (sections.length >= 2) {
      const all = !L.menuSectionIds(week, f.section);
      rows.push(
        this._filterRow(
          "Categories",
          this._chip("f-section", "", "All", all) +
            sections.map((s) => this._chip("f-section", s.slug, esc(s.name), f.section === s.slug)).join("")
        )
      );
    }
    rows.push(
      this._filterRow(
        "Main protein",
        this._chip("f-protein-all", "", "All", f.protein.size === 0) +
          L.PROTEIN_FILTERS.map((p) =>
            this._chip("f-protein", p, `<span class="hf-dot" style="background:${L.PREFERENCE_COLORS[p]}"></span>${esc(p)}`, f.protein.has(p))
          ).join("")
      )
    );
    rows.push(
      this._filterRow(
        "Dietary preference",
        this._chip("f-diet-all", "", "All", f.diet.size === 0) +
          L.DIET_FILTERS.map((d) => this._chip("f-diet", d.key, esc(d.label), f.diet.has(d.key))).join("")
      )
    );
    for (const group of L.serverFilterGroups(week)) {
      const set = f.server[group.slug] || new Set();
      rows.push(
        this._filterRow(
          group.name || group.slug,
          this._chip("f-server-all", group.slug, "All", set.size === 0) +
            group.options
              .map((o) => this._chip("f-server", o.slug, esc(o.name), set.has(o.slug), ` data-group="${esc(group.slug)}"`))
              .join("")
        )
      );
    }
    rows.push(
      this._filterRow(
        "Total cooking time",
        this._chip("f-time", "", "Any", !f.time) + L.TIME_FILTERS.map((t) => this._chip("f-time", t.key, esc(t.label), f.time === t.key)).join("")
      )
    );
    rows.push(
      this._filterRow(
        "Highlights",
        this._chip("f-highlight", "", "All", !f.highlight) +
          L.HIGHLIGHT_FILTERS.map((h) =>
            this._chip("f-highlight", h.key, `${h.key === "favorite" ? icon("mdi:heart-outline") : ""}${esc(h.label)}`, f.highlight === h.key)
          ).join("")
      )
    );
    return `<div class="hf-filterpanel">${rows.join("")}
      ${active.length ? `<div class="hf-actions" style="justify-content:flex-end;padding-top:8px"><button class="hf-btn sm ghost" data-action="clear-filters">Clear all filters</button></div>` : ""}</div>`;
  }

  _tileContext(week, { history = false, selection, sel }) {
    const card = this.card;
    return {
      editable: card.box.canEdit(week),
      history,
      selection,
      sel,
      showSoldOut: L.isWeekEditable(week),
      currency: card.account && card.account.selected_plan_total_price_currency,
      imageWidth: card.config.image_width,
    };
  }

  // The tile's customization line: on a week you can edit, a selector that opens the options
  // drawer — "Customize · 8 options" while the dish is as written, the chosen option's name once
  // it isn't; otherwise just the chosen option's name.
  _optionLine(r, group, ctx) {
    const options = group.members.length - 1;
    const custom = !L.isDefaultMeal(r);
    if (options > 0 && ctx.editable) {
      return `<button class="hf-optselect${custom ? " custom" : ""}" data-action="customize" data-key="${esc(String(L.selKey(r)))}"
          aria-haspopup="dialog" aria-label="${esc(`Customize ${group.base.name}: ${L.optionLabel(r)}, ${L.plural(options, "option")}`)}">
          ${icon("mdi:tune-variant")}<span class="hf-optlabel">${esc(custom ? L.optionLabel(r) : "Customize")}</span>
          ${custom ? "" : `<span class="hf-optcount">${esc(L.plural(options, "option"))}</span>`}${icon("mdi:chevron-down")}</button>`;
    }
    return r.variation_title ? `<div class="hf-tvariant">${esc(r.variation_title)}</div>` : "";
  }

  _tile(unit, ctx) {
    const { recipe: r, group } = unit;
    const key = esc(String(L.selKey(r)));
    const selected = ctx.sel(r);
    const qty = selected ? L.tileQuantity(ctx.selection, r) : 0;
    const soldOut = r.is_sold_out === true && ctx.showSoldOut;
    const img = L.resizedImage(r.image_url, ctx.imageWidth);
    const video = L.safeMediaUrl(r.video_url);
    const meta = [];
    const mins = r.prep_time_minutes != null ? r.prep_time_minutes : r.total_time_minutes;
    if (mins != null) meta.push(`<span>${icon("mdi:timer-outline")}${esc(mins)} min</span>`);
    if (r.calories_kcal != null) meta.push(`<span>${icon("mdi:fire")}${esc(Math.round(r.calories_kcal))} kcal</span>`);
    if (r.protein_g != null) meta.push(`<span>${esc(Math.round(r.protein_g))}g protein</span>`);
    const labels = L.tileChipLabels(r);
    const chips = [];
    if (r.preference === "Veggie") chips.push(`<span class="hf-tchip veggie">Veggie</span>`);
    for (const label of labels.slice(0, 2)) chips.push(`<span class="hf-tchip">${esc(label)}</span>`);
    if (labels.length > 2) chips.push(`<span class="hf-tchip" title="${esc(labels.slice(2).join(", "))}">+${labels.length - 2}</span>`);
    const hist = [];
    if (r.delivered_count) hist.push(`Ordered ${r.delivered_count}×${r.last_delivered_week ? ` · last ${esc(r.last_delivered_week)}` : ""}`);
    if (r.rating) hist.push(`You rated ${esc(r.rating)}/${esc(r.rating_scale || 5)}`);

    let control = "";
    if (ctx.editable && !selected) {
      control = `<button class="hf-add" data-action="add" data-key="${key}" aria-label="Add ${esc(r.name)} to your box">${icon("mdi:plus")}Add</button>`;
    } else if (ctx.editable && selected) {
      // In the box: the servings stepper takes the whole footer (the price is in the box now).
      control = `<span class="hf-stepper wide" role="group" aria-label="Servings of ${esc(r.name)}">
          <button data-action="qty" data-delta="-1" data-key="${key}" aria-label="${qty === 1 ? "Remove meal" : "Fewer servings"}">${icon(qty === 1 ? "mdi:trash-can-outline" : "mdi:minus")}</button>
          <span class="hf-qty">${qty} <span class="hf-unit">serving${qty === 1 ? "" : "s"}</span></span>
          <button data-action="qty" data-delta="1" data-key="${key}" aria-label="More servings" ${qty >= L.MAX_MEAL_SERVINGS ? "disabled" : ""}>${icon("mdi:plus")}</button>
        </span>`;
    } else if (selected && !ctx.history) {
      control = `<span class="hf-ordered">${icon("mdi:check")} In box${qty > 1 ? ` · ${qty}×` : ""}</span>`;
    }
    const price =
      r.price != null && !(ctx.editable && selected)
        ? `<span class="hf-tprice">${esc(L.fmtPerServing(r.price, r.currency || ctx.currency))}<span>/serving</span></span>`
        : "";
    const color = L.PREFERENCE_COLORS[r.preference] || "var(--hf-muted)";
    const topRight = [
      r.is_favorite === true ? `<span class="hf-favdot" title="In your cookbook">${icon("mdi:heart")}</span>` : "",
      selected && !ctx.history ? `<span class="hf-checkmark" title="In your box">${icon("mdi:check-bold")}</span>` : "",
      qty > 1 ? `<span class="hf-overlaypill">${qty}×</span>` : "",
    ].join("");
    return `<div class="hf-tile${selected && !ctx.history ? " selected" : ""}${soldOut ? " soldout" : ""}"
        role="button" tabindex="0" data-action="open-recipe" data-key="${key}" aria-label="Open recipe: ${esc(r.name)}">
        <div class="hf-media">
          ${img ? `<img loading="lazy" src="${esc(img)}" alt="">` : `<div class="hf-noimg"></div>`}
          ${soldOut ? `<div class="hf-soldout">Sold out</div>` : ""}
          <div class="hf-tl">${r.badge ? `<span class="hf-badge"${L.badgeStyle(r)}>${esc(r.badge)}</span>` : ""}</div>
          <div class="hf-tr">${topRight}</div>
          <div class="hf-bl">${video ? `<button class="hf-play" data-action="play" data-key="${key}" aria-label="Play video for ${esc(r.name)}">${icon("mdi:play")}</button>` : ""}</div>
          <div class="hf-br">${r.surcharge_label ? `<span class="hf-overlaypill" title="Premium surcharge per serving">${esc(L.fmtSurcharge(r.surcharge_label, ctx.currency))}</span>` : ""}</div>
        </div>
        <div class="hf-tbody">
          <div class="hf-tname"><span class="hf-pdot" style="background:${color}" title="${esc(r.preference || "")}"></span><span>${esc(r.name)}</span></div>
          ${this._optionLine(r, group, ctx)}
          ${r.description ? `<div class="hf-tdesc">${esc(r.description)}</div>` : ""}
          ${meta.length ? `<div class="hf-tmeta">${meta.join("")}</div>` : ""}
          ${chips.length ? `<div class="hf-tchips">${chips.join("")}</div>` : ""}
          ${hist.length ? `<div class="hf-thist">${hist.join(" · ")}</div>` : ""}
          <div class="hf-tfoot">${price}${control}</div>
        </div>
      </div>`;
  }

  // After a selection edit: patch the one tile, the strip's unsaved dot and the box bar when no
  // filter could change which tiles are visible; otherwise re-render the view.
  afterSelection(week, recipe) {
    const card = this.card;
    card.box.schedulePreview(week);
    const f = this.f;
    const root = card.shadowRoot && card.shadowRoot.querySelector(".js-main");
    const visibilityMayChange = f.selectedOnly || this.query.trim() || this._activeFilters(week).length;
    if (card.view !== "menu" || !root || visibilityMayChange) {
      if (card.view === "menu") card.renderView();
      else card.renderBoxBar();
      return;
    }
    const key = String(L.selKey(recipe));
    const tile = root.querySelector(`.hf-tile[data-key="${CSS.escape(key)}"]`);
    const unit = this._units && this._units.get(key);
    if (!tile || !unit) {
      // No tile shows this meal as itself — e.g. an option added from its recipe sheet while
      // its dish's tile shows the base — so the grid must be rebuilt.
      card.renderView();
      return;
    }
    const selection = card.box.displayMeals(week);
    const html = this._tile(unit, this._tileContext(week, { selection, sel: (r) => L.tileSelected(selection, r) }));
    const tpl = document.createElement("template");
    tpl.innerHTML = html.trim();
    const hadFocus = tile.contains(card.shadowRoot.activeElement);
    const replacement = tpl.content.firstElementChild;
    tile.replaceWith(replacement);
    if (hadFocus) {
      const target = replacement.querySelector("[data-action='qty'], [data-action='add']") || replacement;
      target.focus({ preventScroll: true });
    }
    const chip = root.querySelector(`.hf-weekchip[data-week-id="${CSS.escape(week.week_id)}"]`);
    if (chip) {
      const dot = chip.querySelector(".hf-wcdirty");
      const dirty = card.box.dirty(week);
      if (dirty && !dot) chip.insertAdjacentHTML("afterbegin", `<span class="hf-wcdirty" title="Unsaved changes"></span>`);
      else if (!dirty && dot) dot.remove();
    }
    const kind = root.querySelector(".hf-segment");
    if (kind) {
      const tpl = document.createElement("template");
      tpl.innerHTML = renderKindSwitch(card, week, "menu").trim();
      if (tpl.content.firstElementChild) kind.replaceWith(tpl.content.firstElementChild);
    }
    card.renderBoxBar();
  }

  // Selection state for the recipe sheet's pinned Add/servings footer (null = read-only sheet).
  _detailSelection(week, recipe) {
    const box = this.card.box;
    if (!box.canEdit(week)) return null;
    return {
      qty: L.tileQuantity(box.displayMeals(week), recipe),
      maxQty: L.MAX_MEAL_SERVINGS,
      add: () => box.addMeal(week, recipe) && this.afterSelection(week, recipe),
      inc: () => box.changeMeal(week, recipe, 1) && this.afterSelection(week, recipe),
      dec: () => box.changeMeal(week, recipe, -1) && this.afterSelection(week, recipe),
    };
  }

  // ---- customization drawer ----------------------------------------------------------------
  //
  // Like the website's: every way to have the dish — the unchanged version first, then each
  // option with its ingredient photo and surcharge. Picking one marks it; the button applies it:
  // a dish in the box switches over keeping its servings, one that isn't is added as chosen.

  _openCustomize(week, unit) {
    const state = { choice: String(L.selKey(unit.recipe)) };
    this.card.openSheet({
      kind: "customize",
      narrow: true,
      label: `Customize ${unit.group.base.name}`,
      render: () => this._renderCustomize(this.card.weekById(week.week_id) || week, unit, state),
      onClick: (_ev, el) => this._onCustomizeClick(week, unit, state, el),
    });
  }

  _renderCustomize(week, unit, state) {
    const card = this.card;
    const { recipe: current, group } = unit;
    const selection = card.box.displayMeals(week);
    const inBox = L.tileSelected(selection, current);
    const editable = card.box.canEdit(week);
    const currency = card.account && card.account.selected_plan_total_price_currency;
    const rows = group.members
      .map((m) => {
        const key = String(L.selKey(m));
        const on = key === state.choice;
        const soldOut = m.is_sold_out === true && L.isWeekEditable(week);
        // Another tile already holds this option (two versions of the dish in one box).
        const taken = m !== current && L.tileSelected(selection, m);
        const note = soldOut ? "Sold out" : taken ? "Already in your box" : "";
        const photo = L.isDefaultMeal(m) ? L.resizedImage(m.image_url, 160) : L.resizedImage(m.variation_image_url, 96);
        const price = m.surcharge_label
          ? `<span class="hf-optprice">${esc(L.fmtSurcharge(m.surcharge_label, currency))}<span>/serving</span></span>`
          : `<span class="hf-optprice included">Included</span>`;
        const sub = m.name && m.name !== group.base.name ? m.name : L.isDefaultMeal(m) ? "As the recipe is written" : "";
        return `<button class="hf-optrow${on ? " on" : ""}" aria-pressed="${on}" data-action="opt-pick" data-key="${esc(key)}"
            ${soldOut || taken || !editable ? "disabled" : ""}>
            <span class="hf-radio" aria-hidden="true"></span>
            ${photo ? `<img class="hf-optimg${L.isDefaultMeal(m) ? " dish" : ""}" src="${esc(photo)}" alt="" loading="lazy">` : `<span class="hf-optimg"></span>`}
            <span class="hf-opttext"><span class="hf-optname">${esc(L.optionLabel(m))}</span>
              ${sub || note ? `<span class="hf-optsub">${esc(note || sub)}</span>` : ""}</span>
            ${price}
          </button>`;
      })
      .join("");
    const chosen = group.members.find((m) => String(L.selKey(m)) === state.choice) || current;
    const changed = chosen !== current;
    const primary = !editable
      ? ""
      : inBox
        ? `<button class="hf-btn primary" data-action="opt-apply" ${changed ? "" : "disabled"}>Update box</button>`
        : `<button class="hf-btn primary" data-action="opt-apply">${icon("mdi:plus")}Add to box</button>`;
    return `
      <div class="hf-sheethead"><div class="hf-sheettitle"><h2>Customize</h2>
        <div class="hf-sheetsub">${esc(group.base.name)}${inBox ? " · in your box" : ""}</div></div>
        <button class="hf-iconbtn" data-close-sheet aria-label="Close">${icon("mdi:close")}</button></div>
      <div class="hf-sheetbody hf-optlist" role="group" aria-label="Ways to have this meal">${rows}</div>
      <div class="hf-sheetfoot"><button class="hf-btn ghost" data-action="opt-recipe">${icon("mdi:book-open-page-variant-outline")}View recipe</button>
        ${primary}</div>`;
  }

  _onCustomizeClick(week, unit, state, el) {
    if (!el) return;
    const card = this.card;
    const current = card.weekById(week.week_id) || week;
    const chosen = unit.group.members.find((m) => String(L.selKey(m)) === state.choice) || unit.recipe;
    switch (el.getAttribute("data-action")) {
      case "opt-pick": {
        state.choice = el.getAttribute("data-key");
        card.renderSheet();
        const row = card.shadowRoot.querySelector(`.hf-optrow[data-key="${CSS.escape(state.choice)}"]`);
        if (row) row.focus({ preventScroll: true });
        break;
      }
      case "opt-apply": {
        const inBox = L.tileSelected(card.box.displayMeals(current), unit.recipe);
        const changed = inBox ? card.box.swapMeal(current, unit.recipe, chosen) : card.box.addMeal(current, chosen);
        card.closeSheet();
        if (changed) {
          card.box.schedulePreview(current);
          card.renderView();
        }
        break;
      }
      case "opt-recipe":
        card.closeSheet();
        card.openRecipe(chosen.recipe_id, () => this._detailSelection(card.selectedWeek(), chosen));
        break;
      default:
        break;
    }
  }

  onInput(ev) {
    const input = ev.target.closest("[data-input='meal-search']");
    if (!input) return;
    this.query = input.value;
    this.limit = PAGE_SIZE;
    clearTimeout(this._searchTimer);
    this._searchTimer = setTimeout(() => this.card.renderView(), 160);
  }

  onClick(ev, el) {
    const card = this.card;
    const week = card.selectedWeek();
    if (!el || !week) return;
    const action = el.getAttribute("data-action");
    if (handleWeekAction(card, this, week, action, el)) return;
    const f = this.f;
    const recipe = this._rendered.get(el.getAttribute("data-key"));
    const refilter = () => {
      this.limit = PAGE_SIZE;
      this._persist();
      card.renderView();
    };
    switch (action) {
      case "add":
        if (recipe && card.box.addMeal(week, recipe)) this.afterSelection(week, recipe);
        break;
      case "qty":
        if (recipe && card.box.changeMeal(week, recipe, Number(el.getAttribute("data-delta")) || 0)) {
          this.afterSelection(week, recipe);
        }
        break;
      case "play":
        if (recipe) card.openVideo(recipe);
        break;
      case "customize": {
        const unit = this._units && this._units.get(el.getAttribute("data-key"));
        if (unit) this._openCustomize(week, unit);
        break;
      }
      case "open-recipe":
        if (recipe) card.openRecipe(recipe.recipe_id, () => this._detailSelection(card.selectedWeek(), recipe));
        break;
      case "more":
        this.limit += PAGE_SIZE;
        card.renderView();
        break;
      case "clear-search":
        this.query = "";
        this.limit = PAGE_SIZE;
        card.renderView();
        break;
      case "toggle-filters":
        f.expanded = !f.expanded;
        this._persist();
        card.renderView();
        break;
      case "toggle-selected-only":
        f.selectedOnly = !f.selectedOnly;
        refilter();
        break;
      case "clear-filters":
        this._clearFilters();
        refilter();
        break;
      case "remove-filter":
        this._removeFilter(el.getAttribute("data-kind"), el.getAttribute("data-value"));
        refilter();
        break;
      case "f-section": {
        const value = el.getAttribute("data-value");
        f.section = value && value !== f.section ? value : "";
        refilter();
        break;
      }
      case "f-protein": {
        const value = el.getAttribute("data-value");
        if (f.protein.has(value)) f.protein.delete(value);
        else f.protein.add(value);
        refilter();
        break;
      }
      case "f-protein-all":
        f.protein.clear();
        refilter();
        break;
      case "f-diet": {
        const value = el.getAttribute("data-value");
        if (f.diet.has(value)) f.diet.delete(value);
        else f.diet.add(value);
        refilter();
        break;
      }
      case "f-diet-all":
        f.diet.clear();
        refilter();
        break;
      case "f-server": {
        const set = f.server[el.getAttribute("data-group")];
        const value = el.getAttribute("data-value");
        if (set) {
          if (set.has(value)) set.delete(value);
          else set.add(value);
        }
        refilter();
        break;
      }
      case "f-server-all": {
        const set = f.server[el.getAttribute("data-value")];
        if (set) set.clear();
        refilter();
        break;
      }
      case "f-time": {
        const value = el.getAttribute("data-value");
        f.time = value && value !== f.time ? value : "";
        refilter();
        break;
      }
      case "f-highlight": {
        const value = el.getAttribute("data-value");
        f.highlight = value && value !== f.highlight ? value : "";
        refilter();
        break;
      }
      default:
        break;
    }
  }
}

// ---- Market (extras) ----------------------------------------------------------------------

export class MarketView {
  constructor(card) {
    this.card = card;
    this.query = "";
    this.selectedOnly = L.storageGet(L.STORAGE_KEYS.marketSelectedOnly) === "1";
    this.sections = loadSet(L.STORAGE_KEYS.marketSections);
    this._items = new Map();
    this._stripWeek = null;
    this._historyOpen = null;
    this._searchTimer = null;
  }

  onWeekChange() {}

  _persist() {
    L.storageSet(L.STORAGE_KEYS.marketSelectedOnly, this.selectedOnly ? "1" : "0");
    L.storageSet(L.STORAGE_KEYS.marketSections, JSON.stringify([...this.sections]));
  }

  render() {
    const card = this.card;
    const placeholder = card.weeksPlaceholder();
    if (placeholder) return placeholder;
    if (!card.weeks.length) {
      return `<div class="hf-empty">${icon("mdi:storefront-outline")}No delivery weeks found.</div>`;
    }
    const week = card.selectedWeek() || card.weeks[0];
    return `${renderWeekStrip(card, week)}${renderWeekHead(card, week, "market")}${renderWeekNotices(
      card,
      week,
      "market",
      this
    )}${this._renderBody(week)}`;
  }

  afterRender(root) {
    centerStrip(root, this, this.card.selectedWeek());
  }

  _renderBody(week) {
    const card = this.card;
    const box = card.box;
    const past = L.isPastWeek(week);
    const all = week.market_items || [];
    this._items = new Map(all.map((i) => [String(i.item_id), i]));
    const selection = box.displayMarket(week);
    const editable = box.canEdit(week);
    const sections = L.marketSections(week);
    const present = new Set(sections);
    const active = new Set([...this.sections].filter((s) => present.has(s)));
    const showSections = !past && !this.selectedOnly && sections.length >= 2;
    const toolbar = `<div class="hf-toolbar">
        ${renderKindSwitch(card, week, "market")}
        ${all.length ? searchBox("market-search", this.query, "Search extras") : ""}
        ${past || !all.length ? "" : `<button class="hf-chip${this.selectedOnly ? " on" : ""}" data-action="toggle-selected-only"
          aria-pressed="${this.selectedOnly}">${icon("mdi:package-variant-closed")}In my box</button>`}
      </div>`;
    const sectionBar = showSections
      ? `<div class="hf-rail" data-scroll-key="market-sections" style="margin-bottom:14px">${[
          `<button class="hf-chip${active.size === 0 ? " on" : ""}" data-action="section-all" aria-pressed="${active.size === 0}">All</button>`,
          ...sections.map(
            (slug) => `<button class="hf-chip${active.has(slug) ? " on" : ""}" data-action="section" data-value="${esc(slug)}"
                aria-pressed="${active.has(slug)}">${esc(L.marketGroupLabel(slug))}</button>`
          ),
        ].join("")}</div>`
      : "";
    if (!all.length) {
      const message = past ? "No extras were ordered this week." : "The Market for this week isn't open yet.";
      return `${toolbar}<div class="hf-empty">${icon("mdi:storefront-outline")}${esc(message)}</div>`;
    }
    const groups = L.marketGroups(week, selection, {
      selectedOnly: past || this.selectedOnly,
      sections: showSections ? active : null,
      query: this.query,
    });
    if (!groups.length) {
      const why = this.query
        ? `No extras match “${esc(this.query.trim())}”.`
        : past
          ? "No extras were ordered this week."
          : this.selectedOnly
            ? "No extras in your box yet."
            : "No extras match these categories.";
      return `${toolbar}${sectionBar}<div class="hf-empty">${icon("mdi:storefront-outline")}${why}</div>`;
    }
    const currency = L.marketCurrency(week);
    const body = groups
      .map(
        (g) => `<section class="hf-group">
          ${g.label ? `<div class="hf-grouphead"><h3 class="hf-h3">${esc(g.label)}</h3><span class="hf-groupcount">${g.items.length}</span></div>` : ""}
          <div class="hf-grid market${card.busy ? " busy" : ""}">${g.items
            .map((item) => this._tile(item, selection.get(item.item_id) || 0, editable, currency, past))
            .join("")}</div></section>`
      )
      .join("");
    return `${toolbar}${sectionBar}${body}`;
  }

  _tile(item, qty, editable, currency, past) {
    const id = esc(String(item.item_id));
    const soldOut = item.is_sold_out === true;
    const img = L.resizedImage(item.image_url, this.card.config.image_width);
    const cap = L.marketCap(item);
    let control = "";
    if (editable && qty === 0) {
      control = `<button class="hf-add" data-action="market-qty" data-delta="1" data-id="${id}" ${soldOut ? "disabled" : ""}
        aria-label="Add ${esc(item.name)}">${icon("mdi:plus")}Add</button>`;
    } else if (editable) {
      control = `<span class="hf-stepper wide" role="group" aria-label="Quantity of ${esc(item.name)}">
          <button data-action="market-qty" data-delta="-1" data-id="${id}" aria-label="${qty === 1 ? "Remove" : "Fewer"}">${icon(qty === 1 ? "mdi:trash-can-outline" : "mdi:minus")}</button>
          <span class="hf-qty">${qty} <span class="hf-unit">in box</span></span>
          <button data-action="market-qty" data-delta="1" data-id="${id}" aria-label="More" ${qty >= cap || soldOut ? "disabled" : ""}>${icon("mdi:plus")}</button>
        </span>`;
    } else if (qty > 0) {
      control = `<span class="hf-ordered">${icon("mdi:check")} ${past ? `${qty} ordered` : `In box${qty > 1 ? ` · ${qty}×` : ""}`}</span>`;
    }
    const price =
      item.price != null && !(editable && qty > 0)
        ? `<span class="hf-tprice">${esc(L.fmtPrice(item.price, item.currency || currency))}</span>`
        : "";
    return `<div class="hf-tile${qty > 0 && !past ? " selected" : ""}${soldOut ? " soldout" : ""}" role="button" tabindex="0"
        data-action="open-item" data-id="${id}" aria-label="Open: ${esc(item.name)}">
        <div class="hf-media">
          ${img ? `<img loading="lazy" src="${esc(img)}" alt="">` : `<div class="hf-noimg"></div>`}
          ${soldOut ? `<div class="hf-soldout">Sold out</div>` : ""}
          <div class="hf-tr">${qty > 0 && !past ? `<span class="hf-checkmark">${icon("mdi:check-bold")}</span>` : ""}${qty > 1 ? `<span class="hf-overlaypill">${qty}×</span>` : ""}</div>
        </div>
        <div class="hf-tbody">
          <div class="hf-tname"><span>${esc(item.name)}</span></div>
          ${item.description ? `<div class="hf-tdesc">${esc(item.description)}</div>` : ""}
          ${item.calories_kcal != null ? `<div class="hf-tmeta"><span>${icon("mdi:fire")}${esc(Math.round(item.calories_kcal))} kcal</span></div>` : ""}
          <div class="hf-tfoot">${price}${control}</div>
        </div>
      </div>`;
  }

  _afterQuantity(week) {
    const card = this.card;
    // Visibility can change only under a filter (a filtered-out item stays visible while it is
    // in the box); otherwise a re-render of this small grid is cheap enough.
    card.renderView();
    card.renderBoxBar();
    void week;
  }

  _detailSelection(week, item) {
    const box = this.card.box;
    if (!box.canEdit(week)) return null;
    return {
      qty: box.displayMarket(week).get(item.item_id) || 0,
      maxQty: L.marketCap(item),
      unit: "item",
      add: () => box.changeMarket(week, item, 1) && this._afterQuantity(week),
      inc: () => box.changeMarket(week, item, 1) && this._afterQuantity(week),
      dec: () => box.changeMarket(week, item, -1) && this._afterQuantity(week),
    };
  }

  onInput(ev) {
    const input = ev.target.closest("[data-input='market-search']");
    if (!input) return;
    this.query = input.value;
    clearTimeout(this._searchTimer);
    this._searchTimer = setTimeout(() => this.card.renderView(), 160);
  }

  onClick(ev, el) {
    const card = this.card;
    const week = card.selectedWeek();
    if (!el || !week) return;
    const action = el.getAttribute("data-action");
    if (handleWeekAction(card, this, week, action, el)) return;
    const item = this._items.get(el.getAttribute("data-id"));
    switch (action) {
      case "market-qty":
        if (item && card.box.changeMarket(week, item, Number(el.getAttribute("data-delta")) || 0)) {
          this._afterQuantity(week);
        }
        break;
      case "open-item":
        // Add-ons carry a normal recipe id; an item known only by SKU has nothing to show.
        if (item && item.recipe_id) {
          card.openRecipe(item.recipe_id, () => this._detailSelection(card.selectedWeek(), item));
        }
        break;
      case "toggle-selected-only":
        this.selectedOnly = !this.selectedOnly;
        this._persist();
        card.renderView();
        break;
      case "section": {
        const value = el.getAttribute("data-value");
        if (this.sections.has(value)) this.sections.delete(value);
        else this.sections.add(value);
        this._persist();
        card.renderView();
        break;
      }
      case "section-all":
        this.sections.clear();
        this._persist();
        card.renderView();
        break;
      case "clear-search":
        this.query = "";
        card.renderView();
        break;
      default:
        break;
    }
  }
}

// ---- box bar & review sheet ---------------------------------------------------------------

// The estimate while meals are being changed: the meal preview's total (it prices meals,
// shipping, tax and discounts, not add-ons) plus the pending extras.
function estimate(card, week) {
  const box = card.box;
  const currency = L.marketCurrency(week) || (card.account && card.account.selected_plan_total_price_currency);
  const extrasCents = L.marketTotalCents(week, box.displayMarket(week));
  if (box.mealsDirty(week)) {
    const preview = box.preview(week);
    if (!preview) return { status: "pending" };
    if (preview.status === "loading") return { status: "loading" };
    if (preview.status !== "ok" || preview.data.grand_total == null) return { status: "unavailable" };
    return {
      status: "ok",
      amount: Number(preview.data.grand_total) + extrasCents / 100,
      currency,
      preview: preview.data,
      extras: extrasCents / 100,
    };
  }
  if (box.marketDirty(week)) return { status: "extras", extras: extrasCents / 100, currency };
  const total = L.boxTotal(week, card.account);
  return total ? { status: "saved", amount: total.amount, currency: total.currency, label: total.label } : { status: "none" };
}

export function renderBoxBar(card, week) {
  const box = card.box;
  const editable = L.isWeekEditable(week);
  const dirty = box.dirty(week);
  if (!editable && !dirty) return "";
  const meals = box.displayMeals(week);
  const count = L.mealsCount(meals);
  const servings = L.servingsTotal(meals);
  const required = Number(week.meals_required) || 0;
  const extras = box.displayMarket(week);
  const extraCount = [...extras.values()].reduce((a, b) => a + b, 0);
  const slots = [];
  const slotCount = Math.max(required, count);
  for (let i = 0; i < Math.min(slotCount, 10); i += 1) {
    slots.push(`<span class="hf-slot${i < count ? (i >= required && required ? " extra" : " filled") : ""}"></span>`);
  }
  const line = [
    `<span>${required ? `${count} of ${required} meals` : L.plural(count, "meal")}</span>`,
    servings !== count ? `<span class="hf-muted">${servings} servings</span>` : "",
    extraCount ? `<span class="hf-muted">${L.plural(extraCount, "extra")}</span>` : "",
  ].filter(Boolean).join("");
  let note;
  let noteCls = "";
  const closed = dirty && !editable;
  if (closed) {
    note = "The deadline passed — these changes can't be saved";
    noteCls = " warn";
  } else if (count < L.MIN_MEALS && box.mealsDirty(week)) {
    note = `Choose at least ${L.MIN_MEALS} meals to save`;
    noteCls = " warn";
  } else if (required && count !== required && count > 0) {
    note = `Box resized to ${count} meals for this week (plan: ${required}) — HelloFresh reprices it`;
    noteCls = " warn";
  } else if (dirty) note = "Unsaved changes";
  else note = "Saved · edit until the deadline";
  const est = estimate(card, week);
  let total = "";
  if (est.status === "ok") {
    total = `<span class="hf-boxamount">${esc(L.fmtPrice(est.amount, est.currency))}</span><span class="hf-boxamountnote">Estimated total</span>`;
  } else if (est.status === "loading" || est.status === "pending") {
    total = `<span class="hf-boxamountnote">Updating price…</span>`;
  } else if (est.status === "extras") {
    total = `<span class="hf-boxamount">+${esc(L.fmtPrice(est.extras, est.currency))}</span><span class="hf-boxamountnote">Extras</span>`;
  } else if (est.status === "saved") {
    total = `<span class="hf-boxamount">${esc(L.fmtPrice(est.amount, est.currency))}</span><span class="hf-boxamountnote">${esc(est.label === "Total" ? "Box total" : est.label)}</span>`;
  }
  const invalid = box.mealsDirty(week) && count < L.MIN_MEALS;
  const actions = closed
    ? `<button class="hf-btn" data-action="box-discard" ${card.busy ? "disabled" : ""}>Discard changes</button>`
    : dirty
      ? `<button class="hf-btn ghost" data-action="box-discard" ${card.busy ? "disabled" : ""}>Discard</button>
       <button class="hf-btn" data-action="box-review">Review</button>
       <button class="hf-btn primary" data-action="box-save" ${card.busy || invalid ? "disabled" : ""}>${icon("mdi:content-save-outline")}Save box</button>`
      : `<button class="hf-btn" data-action="box-review">${icon("mdi:package-variant-closed")}Your box</button>`;
  return `<div class="hf-boxbarinner" role="region" aria-label="Your box">
      <div class="hf-boxslots" aria-hidden="true">${slots.join("")}</div>
      <div class="hf-boxsummary"><div class="hf-boxline">${line}</div><div class="hf-boxnote${noteCls}">${esc(note)}</div></div>
      ${total ? `<div class="hf-boxtotal">${total}</div>` : ""}
      <div class="hf-actions">${actions}</div>
    </div>`;
}

export function onBoxBarClick(card, _ev, el) {
  const week = card.selectedWeek();
  if (!el || !week) return;
  const action = el.getAttribute("data-action");
  if (action === "box-save") card.box.save(week);
  else if (action === "box-discard") {
    card.box.discard(week);
    card.renderView();
  } else if (action === "box-review") openReview(card, week);
}

function lineItem(img, name, sub, tag = "", removed = false) {
  return `<div class="hf-lineitem${removed ? " removed" : ""}">
      ${img ? `<img src="${esc(img)}" alt="" loading="lazy">` : `<div class="hf-noimg"></div>`}
      <div class="hf-lineitemtext"><div class="hf-lineitemname">${esc(name)}</div>${sub ? `<div class="hf-lineitemsub">${sub}</div>` : ""}</div>
      ${tag}
    </div>`;
}

function renderReview(card, week) {
  const box = card.box;
  const meals = box.displayMeals(week);
  const saved = box.savedMeals(week);
  const recipes = week.recipes || [];
  const byKey = new Map(recipes.map((r) => [L.selKey(r), r]));
  const mealRows = [];
  for (const [key, qty] of meals) {
    const r = byKey.get(key);
    if (!r) continue;
    const was = saved.get(key);
    const tag = was == null ? pill("New", "ok") : was !== qty ? pill(`Was ${was}`, "info") : "";
    const price = r.price != null ? ` · ${esc(L.fmtPerServing(r.price, r.currency))}/serving` : "";
    const option = r.variation_title ? `${esc(r.variation_title)} · ` : "";
    mealRows.push(lineItem(L.resizedImage(r.image_url, 160), r.name, `${option}${L.plural(qty, "serving")}${price}`, tag));
  }
  for (const [key] of saved) {
    if (meals.has(key)) continue;
    const r = byKey.get(key);
    if (r) {
      const option = r.variation_title ? `${esc(r.variation_title)} · ` : "";
      mealRows.push(lineItem(L.resizedImage(r.image_url, 160), r.name, `${option}Removed`, pill("Removed", "danger"), true));
    }
  }
  const market = box.displayMarket(week);
  const savedMarket = box.savedMarket(week);
  const items = new Map((week.market_items || []).map((i) => [i.item_id, i]));
  const currency = L.marketCurrency(week);
  const extraRows = [];
  for (const [id, qty] of market) {
    const item = items.get(id);
    if (!item) continue;
    const was = savedMarket.get(id);
    const tag = was == null ? pill("New", "ok") : was !== qty ? pill(`Was ${was}`, "info") : "";
    const price = item.price != null ? ` · ${esc(L.fmtPrice(item.price * qty, item.currency || currency))}` : "";
    extraRows.push(lineItem(L.resizedImage(item.image_url, 160), item.name, `${qty} ×${price}`, tag));
  }
  for (const [id] of savedMarket) {
    if (market.has(id)) continue;
    const item = items.get(id);
    if (item) extraRows.push(lineItem(L.resizedImage(item.image_url, 160), item.name, "Removed", pill("Removed", "danger"), true));
  }

  const est = estimate(card, week);
  const rows = [];
  if (est.status === "ok") {
    const p = est.preview;
    const cur = est.currency;
    if (p.sub_total != null) rows.push(["Meals", L.fmtPrice(p.sub_total, cur)]);
    if (p.shipping_amount != null) rows.push(["Shipping", L.fmtPrice(p.shipping_amount, cur)]);
    if (Number(p.discount_amount) > 0) rows.push([`Discount${p.coupon_code ? ` (${esc(p.coupon_code)})` : ""}`, `−${L.fmtPrice(p.discount_amount, cur)}`]);
    if (Number(p.tax_amount) > 0) rows.push(["Tax", L.fmtPrice(p.tax_amount, cur)]);
    if (est.extras > 0) rows.push(["Extras", L.fmtPrice(est.extras, cur)]);
    rows.push(["Estimated total", L.fmtPrice(est.amount, cur), true]);
  } else if (est.status === "saved") {
    rows.push([est.label === "Total" ? "Box total" : est.label, L.fmtPrice(est.amount, est.currency), true]);
  } else if (est.status === "extras") {
    rows.push(["Extras (this change)", L.fmtPrice(est.extras, est.currency), true]);
  } else if (est.status === "loading" || est.status === "pending") {
    rows.push(["Estimated total", "Updating…", true]);
  }
  const priceTable = rows.length
    ? `<div><div class="hf-eyebrow" style="margin-bottom:8px">Price</div><div class="hf-pricetable">${rows
        .map(([k, v, strong]) => `<div class="hf-prow${strong ? " total" : ""}"><span>${k}</span><span>${esc(v)}</span></div>`)
        .join("")}</div>${est.status === "ok" ? `<p class="hf-small hf-muted" style="margin:8px 0 0">An estimate — HelloFresh calculates the final price when it charges the box.</p>` : ""}</div>`
    : "";
  const count = L.mealsCount(meals);
  const required = Number(week.meals_required) || 0;
  const notes = [];
  if (box.mealsDirty(week) && count < L.MIN_MEALS) {
    notes.push(`<div class="hf-notice tone-warn">${icon("mdi:alert-outline")}<div class="hf-noticebody">Choose at least ${L.MIN_MEALS} meals — HelloFresh has no smaller box.</div></div>`);
  } else if (required && count !== required && count > 0) {
    notes.push(`<div class="hf-notice tone-info">${icon("mdi:resize")}<div class="hf-noticebody">This week's box is resized to <strong>${count} meals</strong> (your plan has ${required}). HelloFresh reprices it; your plan itself doesn't change.</div></div>`);
  }
  const dirty = box.dirty(week);
  const invalid = box.mealsDirty(week) && count < L.MIN_MEALS;
  return `
    <div class="hf-sheethead"><div class="hf-sheettitle"><h2>Your box</h2>
      <div class="hf-sheetsub">${esc(L.fmtLongDate(week.delivery_date))} · ${esc(L.stateLabel(week))}${dirty ? " · unsaved changes" : ""}</div></div>
      <button class="hf-iconbtn" data-close-sheet aria-label="Close">${icon("mdi:close")}</button></div>
    <div class="hf-sheetbody">
      ${notes.join("")}
      <div><div class="hf-eyebrow" style="margin-bottom:4px">Meals · ${count}</div>
        ${mealRows.length ? `<div class="hf-lineitems">${mealRows.join("")}</div>` : `<div class="hf-muted hf-small">No meals chosen yet.</div>`}</div>
      ${extraRows.length ? `<div><div class="hf-eyebrow" style="margin-bottom:4px">Extras · ${market.size}</div><div class="hf-lineitems">${extraRows.join("")}</div></div>` : ""}
      ${priceTable}
    </div>
    <div class="hf-sheetfoot">
      ${dirty ? `<button class="hf-btn ghost danger" data-action="review-discard">Discard changes</button><span class="hf-spacer"></span>` : ""}
      <button class="hf-btn" data-close-sheet>${dirty ? "Keep editing" : "Close"}</button>
      ${dirty ? `<button class="hf-btn primary" data-action="review-save" ${card.busy || invalid ? "disabled" : ""}>${icon("mdi:content-save-outline")}Save box</button>` : ""}
    </div>`;
}

export function openReview(card, week) {
  card.box.schedulePreview(week);
  card.openSheet({
    kind: "review",
    label: "Your box",
    render: () => renderReview(card, card.weekById(week.week_id) || week),
    onClick: (_ev, el) => {
      if (!el) return;
      const current = card.weekById(week.week_id) || week;
      const action = el.getAttribute("data-action");
      if (action === "review-save") card.box.save(current);
      else if (action === "review-discard") {
        card.box.discard(current);
        card.closeSheet();
        card.renderView();
      }
    },
  });
}
