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

const { esc, t, ht } = L;
const { icon, pill, strong } = UI;

// Tiles rendered before a "Show more" button: a planning-catalog week carries ~350 meals, and
// painting them all at once is what made the classic planner slow to open.
const PAGE_SIZE = 100;

// How long the box waits after an edit before asking HelloFresh for a price.
const PREVIEW_DELAY_MS = 650;

const nextFrame = () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));

// The main area of the card in the page, or null (a card not rendered yet, or a test's stand-in).
const mainOf = (card) => (card.shadowRoot && card.shadowRoot.querySelector(".js-main")) || null;

// A meal priced above (or below) the dish as written. HelloFresh gives the difference in cents,
// in a display label, or both; L.fmtSurcharge takes the meal and prefers the cents.
const hasSurcharge = (r) => Boolean(r.surcharge_label) || (r.surcharge_cents != null && Number(r.surcharge_cents) !== 0);

// A draft's edits (what `pending` changed about `base`) put on top of a saved selection that
// moved underneath it: whatever the user added or set to another quantity is kept as they set
// it, whatever they removed stays out, and everything they left alone follows `saved`.
function rebaseSelection(base, pending, saved) {
  const out = new Map(saved);
  for (const [key, qty] of pending) if (base.get(key) !== qty) out.set(key, qty);
  for (const key of base.keys()) if (!pending.has(key)) out.delete(key);
  return out;
}

// ---- the pending box -----------------------------------------------------------------------

export class BoxStore {
  constructor(card) {
    this.card = card;
    this.meals = {}; // week_id -> Map(selKey -> servings)
    this.market = {}; // week_id -> Map(item_id -> quantity)
    // The saved selection each draft was started from. A draft is a full snapshot (that is what
    // the cart write takes), so when the saved box changes somewhere else in the meantime the
    // draft no longer says what the user meant: see stale() and rebase().
    this.mealsBase = {}; // week_id -> Map(selKey -> servings)
    this.marketBase = {}; // week_id -> Map(item_id -> quantity)
    // What a successful save just wrote, per week, shown as the saved state until a fetch that
    // STARTED after the save lands. A fetch already in flight when the save finished carries the
    // old box; without this, its response would briefly show the pre-save selection.
    this.committed = {}; // week_id -> {meals, market, seq}
    this._savedMeals = new WeakMap();
    this._savedMarket = new WeakMap();
    this._deduped = new WeakMap();
    this._preview = new Map(); // selection key -> {status: "loading" | "ok" | "error", data}
    this._previewTimers = new Map(); // week_id -> the debounce timer of its next price request
    this._previewEpoch = 0; // bumped by every refresh: a price asked before it is not kept
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

  // The saved box changed (another device, hellofresh.com, HelloFresh itself) after this week's
  // draft was started. Saving the draft as it is would put the old box back over that change,
  // so nothing is saved until the user picks: rebase() ("Keep mine") or discard() ("Use saved").
  mealsStale(week) {
    const base = this.mealsBase[week.week_id];
    return Boolean(this.meals[week.week_id] && base) && !L.selectionsEqual(base, this.savedMeals(week));
  }

  marketStale(week) {
    const base = this.marketBase[week.week_id];
    return Boolean(this.market[week.week_id] && base) && !L.selectionsEqual(base, this.savedMarket(week));
  }

  stale(week) {
    return this.dirty(week) && (this.mealsStale(week) || this.marketStale(week));
  }

  // "Keep mine": the user's own adds, removals and quantity changes, re-applied to the box as it
  // is saved now. The draft that comes out is an ordinary one, to review and save as usual.
  rebase(week) {
    for (const [store, bases, saved] of [
      [this.meals, this.mealsBase, this.savedMeals(week)],
      [this.market, this.marketBase, this.savedMarket(week)],
    ]) {
      const pending = store[week.week_id];
      if (!pending) continue;
      const rebased = rebaseSelection(bases[week.week_id] || saved, pending, saved);
      if (L.selectionsEqual(rebased, saved)) {
        delete store[week.week_id];
        delete bases[week.week_id];
      } else {
        store[week.week_id] = rebased;
        bases[week.week_id] = new Map(saved);
      }
    }
    this.schedulePreview(week);
  }

  _pendingMeals(week) {
    let pending = this.meals[week.week_id];
    if (!pending) {
      const saved = this.savedMeals(week);
      pending = new Map(saved);
      this.meals[week.week_id] = pending;
      this.mealsBase[week.week_id] = new Map(saved);
    }
    return pending;
  }

  _pendingMarket(week) {
    let pending = this.market[week.week_id];
    if (!pending) {
      const saved = this.savedMarket(week);
      pending = new Map(saved);
      this.market[week.week_id] = pending;
      this.marketBase[week.week_id] = new Map(saved);
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

  // Market stepper, clamped to the item's cap. A sold-out item can't go above what the saved box
  // holds (HelloFresh keeps that for the customer): an unsaved removal can be put back, no more.
  changeMarket(week, item, delta) {
    if (!this.canEdit(week)) return false;
    const pending = this._pendingMarket(week);
    const current = pending.get(item.item_id) || 0;
    if (delta > 0 && item.is_sold_out === true && current >= L.marketItemQuantity(item)) return false;
    const next = Math.max(0, Math.min(L.marketCap(item), current + delta));
    if (next === current) return false;
    if (next === 0) pending.delete(item.item_id);
    else pending.set(item.item_id, next);
    return true;
  }

  discard(week) {
    delete this.meals[week.week_id];
    delete this.market[week.week_id];
    delete this.mealsBase[week.week_id];
    delete this.marketBase[week.week_id];
    // A price asked for the dropped draft would otherwise still go out.
    this._cancelPreview(week.week_id);
  }

  clearDowngrade() {
    this.downgradedWeek = null;
  }

  // After a fetch: drop edits for weeks that vanished, keys the refreshed menu no longer has,
  // and edits that now match what is saved. Unlike the classic cards, a background refresh does
  // NOT throw away a half-built box. `fetchSeq` is the landing fetch's start number: a committed
  // save gives way to the server only once a fetch begun after it arrives.
  //
  // A draft whose saved box moved is kept as it is and reported by stale(); it is never rebased
  // or dropped behind the user's back.
  reconcile(weeks, fetchSeq = Infinity) {
    for (const [weekId, entry] of Object.entries(this.committed)) {
      if (fetchSeq > entry.seq) delete this.committed[weekId];
    }
    const byId = new Map((weeks || []).map((w) => [w.week_id, w]));
    for (const [store, bases, validKeys, saved] of [
      [this.meals, this.mealsBase, (w) => new Set((w.recipes || []).map((r) => L.selKey(r))), (w) => this.savedMeals(w)],
      [this.market, this.marketBase, (w) => new Set((w.market_items || []).map((i) => i.item_id)), (w) => this.savedMarket(w)],
    ]) {
      for (const weekId of Object.keys(store)) {
        const week = byId.get(weekId);
        if (!week || !L.isWeekEditable(week)) {
          delete store[weekId];
          delete bases[weekId];
          continue;
        }
        const keys = validKeys(week);
        for (const key of [...store[weekId].keys()]) if (!keys.has(key)) store[weekId].delete(key);
        if (L.selectionsEqual(store[weekId], saved(week))) {
          delete store[weekId];
          delete bases[weekId];
        }
      }
    }
    // Prices are only good for the data they were asked against. A selection that could not be
    // priced stays marked so (the refresh is no reason to ask again; the next edit is), and
    // every box still being edited gets a fresh price.
    this._previewEpoch += 1;
    for (const [key, entry] of [...this._preview]) if (entry.status !== "error") this._preview.delete(key);
    for (const weekId of [...this._previewTimers.keys()]) if (!byId.has(weekId)) this._cancelPreview(weekId);
    for (const week of weeks || []) this.schedulePreview(week, { retry: false });
    // An open review was drawn against the box as it was; show it against the refreshed one
    // (a draft gone stale must not keep offering its Save).
    if (this.card.sheetKind === "review") this.card.renderSheet();
  }

  // ---- live price estimate (hellofresh.preview_meal_price) ------------------------------------

  _previewKey(week) {
    const entries = [...this.displayMeals(week).entries()].sort((a, b) => String(a[0]).localeCompare(String(b[0])));
    return `${week.week_id}|${JSON.stringify(entries)}`;
  }

  // Whether HelloFresh can be asked to price this week's pending meals at all: an open week,
  // meals changed, at least the smallest box, and a draft that is the user's to save.
  _previewable(week) {
    return (
      this.mealsDirty(week) &&
      L.isWeekEditable(week) &&
      !this.stale(week) &&
      L.mealsCount(this.displayMeals(week)) >= L.MIN_MEALS &&
      L.buildMealWrite(week, this.displayMeals(week)).recipe_ids.length > 0
    );
  }

  // Where the price of the pending meals stands: "ok" (with `data`), "loading", "pending" (a
  // request is about to go out), or "unavailable" when there is none and none is coming — the
  // box is below the minimum, waiting on a stale-draft choice, or HelloFresh failed to price
  // this very selection.
  previewState(week) {
    if (!this.mealsDirty(week)) return { status: "none" };
    const cached = this._preview.get(this._previewKey(week));
    if (cached) return cached.status === "error" ? { status: "unavailable" } : cached;
    return { status: this._previewTimers.has(week.week_id) ? "pending" : "unavailable" };
  }

  _cancelPreview(weekId) {
    clearTimeout(this._previewTimers.get(weekId));
    this._previewTimers.delete(weekId);
  }

  // Price the pending meals once the user pauses (debounced, cached per selection). A selection
  // HelloFresh failed to price is asked about again only when the user does something (`retry`:
  // an edit, opening the review), never by a background refresh — so one failure neither leaves
  // the week without prices nor turns into an error loop.
  schedulePreview(week, { retry = true } = {}) {
    this._cancelPreview(week.week_id);
    if (!this._previewable(week)) return;
    const key = this._previewKey(week);
    const cached = this._preview.get(key);
    if (cached && !(retry && cached.status === "error")) return;
    this._preview.delete(key);
    const weekId = week.week_id;
    this._previewTimers.set(
      weekId,
      setTimeout(() => {
        this._previewTimers.delete(weekId);
        this._fetchPreview(weekId);
      }, PREVIEW_DELAY_MS)
    );
  }

  // The selection is read when the request goes out, and its key with it: by then the draft can
  // have been edited further or discarded since the timer was set, and a price filed under
  // another selection's key would be shown for that other selection.
  async _fetchPreview(weekId) {
    const card = this.card;
    const week = card.weekById(weekId);
    if (!week || !this._previewable(week)) {
      card.renderBoxBar();
      return;
    }
    const key = this._previewKey(week);
    if (this._preview.has(key)) return;
    const write = L.buildMealWrite(week, this.displayMeals(week));
    const epoch = this._previewEpoch;
    const marker = { status: "loading" };
    this._preview.set(key, marker);
    card.renderBoxBar();
    let entry;
    try {
      const data = { week_id: weekId, recipe_ids: write.recipe_ids };
      if (Object.keys(write.quantities).length) data.quantities = write.quantities;
      entry = { status: "ok", data: await card.call("preview_meal_price", data) };
    } catch (err) {
      entry = { status: "error" };
      // eslint-disable-next-line no-console
      console.warn("hellofresh: price preview unavailable", err);
    }
    const current = card.weekById(weekId);
    if (epoch !== this._previewEpoch || !current || this._previewKey(current) !== key) {
      // A refresh landed or the selection moved on while HelloFresh was pricing: this answer
      // is for a box nobody is looking at any more. Whatever replaced it scheduled its own.
      if (this._preview.get(key) === marker) this._preview.delete(key);
    } else {
      this._preview.set(key, entry);
    }
    card.renderBoxBar();
    if (card.sheetKind === "review") card.renderSheet();
  }

  // ---- save -----------------------------------------------------------------------------------

  async save(week) {
    const card = this.card;
    if (card.busy || !this.dirty(week)) return;
    if (!L.isWeekEditable(week)) {
      // The deadline passed while the box was being edited; HelloFresh would reject the write.
      card.toast(t("box.closed_toast"), true);
      return;
    }
    if (this.stale(week)) {
      // Never the stale snapshot: it would undo whatever changed the saved box.
      card.toast(t("box.changed_toast", { keep_mine: t("box.keep_mine"), use_saved: t("box.use_saved") }), true);
      return;
    }
    const mealsDirty = this.mealsDirty(week);
    const marketDirty = this.marketDirty(week);
    if (mealsDirty) {
      const count = L.mealsCount(this.displayMeals(week));
      if (count < L.MIN_MEALS) {
        card.toast(t("box.min_toast", { min: L.MIN_MEALS, selected: count }), true);
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
    // The box as it was saved before this save, for its Undo.
    const before = { meals: new Map(this.savedMeals(week)), market: new Map(this.savedMarket(week)) };
    if (card.sheetKind === "review") card.closeSheet();
    card.setSaving(t("box.saving"));
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
      this._commit(week.week_id, this.displayMeals(week), this.displayMarket(week));
      this.discard(week);
      card.broadcastDataChanged();
    }
    card.setSaving(null);
    await card.reloadWeeks();
    if (failed) card.toast(t("box.save_failed", { error: failed }), true);
    else if (downgraded) {
      this.downgradedWeek = week.week_id;
      card.renderView();
    } else {
      const write = L.boxUndoWrite(week, before, { meals: mealsDirty, market: marketDirty });
      const weekId = week.week_id;
      card.toast(t("box.saved"), false, {
        undo: write && card.weekUndo(weekId, () => this.restore(weekId, write, before)),
      });
    }
  }

  // What a save (or an undo) just wrote, shown as the saved box until a fetch that STARTED after
  // it lands (see `committed`).
  _commit(weekId, meals, market) {
    this.committed[weekId] = { meals: new Map(meals), market: new Map(market), seq: this.card.fetchSeq };
  }

  // Undo a save: write the box back as it was saved before it (`write` from L.boxUndoWrite), in
  // the card's saving state like the save itself. A draft started since is left as it is: the
  // saved box moving under it makes it a stale draft, which asks before it is saved.
  async restore(weekId, write, before) {
    const card = this.card;
    if (card.busy || !write) return;
    card.setSaving(t("undo.undoing"));
    card.renderView();
    await nextFrame();
    let failed = null;
    try {
      await card.call(write.service, write.data);
    } catch (err) {
      failed = (err && err.message) || String(err);
    }
    if (!failed) {
      this._commit(weekId, before.meals, before.market);
      card.broadcastDataChanged({ weekId });
    }
    card.setSaving(null);
    await card.reloadWeeks({ weekId });
    card.undoResult(failed);
  }
}

// ---- week strip & header (shared by Menu and Market) --------------------------------------

// The amber dot on a week chip with unsaved changes. Colour alone says nothing to a screen
// reader, so it carries its meaning as an image label too.
function dirtyDot() {
  return `<span class="hf-wcdirty" role="img" aria-label="${ht("box.unsaved_changes")}" title="${ht("box.unsaved_changes")}"></span>`;
}

function renderWeekStrip(card, selected) {
  const weeks = card.weeks;
  const next = L.nextBoxWeek(weeks);
  const chips = weeks
    .map((w) => {
      const state = L.weekState(w);
      const meta = L.STATE_META[state];
      const label = state === "needs" && L.isAutoPicked(w) ? t("week.review") : meta.short;
      const isNext = next.upcoming && next.week === w;
      const dirty = card.box.dirty(w);
      return `<button class="hf-weekchip ${state}${L.isPastWeek(w) ? " past" : ""}" data-action="select-week"
          data-week-id="${esc(w.week_id)}" aria-pressed="${w === selected}"
          title="${esc(`${w.display_name || w.week_id} · ${L.stateLabel(w, state)}`)}">
          ${isNext ? `<span class="hf-wcnow">${ht("week.next")}</span>` : ""}
          ${dirty ? dirtyDot() : ""}
          <span class="hf-wcday">${esc(L.fmtWeekday(w.delivery_date))}</span>
          <span class="hf-wcdate">${esc(L.fmtDateShort(w.delivery_date))}</span>
          <span class="hf-wcstate"><span class="hf-wcdot ${state}"></span><span class="hf-wclabel">${esc(label)}</span></span>
        </button>`;
    })
    .join("");
  // The arrows turn the strip a page at a time; UI.syncStrip disables one at its end.
  return `<div class="hf-weekbar">
      <button class="hf-iconbtn" data-action="strip-page" data-dir="-1"
        aria-label="${ht("week.earlier_weeks")}">${icon("mdi:chevron-left")}</button>
      <div class="hf-weekstrip" data-scroll-key="weekstrip"><div class="hf-weektrack">${chips}</div></div>
      <button class="hf-iconbtn" data-action="strip-page" data-dir="1"
        aria-label="${ht("week.later_weeks")}">${icon("mdi:chevron-right")}</button>
    </div>`;
}

function renderWeekHead(card, week, kind) {
  const state = L.weekState(week);
  const editable = L.isWeekEditable(week);
  const rel = L.relativeWeek(week);
  const deadline = week.selection_deadline ? new Date(week.selection_deadline) : null;
  const pills = [UI.statePill(week, state)];
  if (L.wasPreselected(week) && L.stateKey(week, state) !== "preselected") {
    pills.push(pill(t("state.preselected.label"), "warn", null, t("week.preselected_title")));
  }
  if (L.isHolidayShifted(week)) {
    pills.push(pill(t("week.holiday_schedule"), "info", "mdi:calendar-star", week.holiday_message || ""));
  }
  const benefit = L.weekBenefit(week);
  if (benefit) pills.push(pill(benefit.label, "ok", "mdi:ticket-percent-outline", benefit.voucher_code ? t("voucher.code", { code: benefit.voucher_code }) : ""));
  const sub = [rel, week.display_name, week.slot_label].filter(Boolean).map(esc).join(" · ");

  const deadlineLine = editable && deadline ? UI.deadlineLine(deadline) : "";
  const people = kind === "menu" ? renderPeopleLine(card, week) : "";
  const actions = [];
  if (kind === "menu") actions.push(UI.pantryButton(card, week, { small: true }));
  if (L.canReschedule(week)) {
    actions.push(`<button class="hf-btn sm" data-action="reschedule" ${card.busy ? "disabled" : ""}>
      ${icon("mdi:calendar-edit")}${ht("week.change_day")}</button>`);
  }
  if (L.canSkip(week)) {
    const skipped = L.isSkipped(week);
    actions.push(`<button class="hf-btn sm${skipped ? " primary" : ""}" data-action="skip" ${card.busy ? "disabled" : ""}>
      ${icon(skipped ? "mdi:restore" : "mdi:debug-step-over")}${ht(skipped ? "week.unskip_week" : "week.skip_week")}</button>`);
  }
  return `<div class="hf-weekhead">
      <div class="hf-weektitle">
        <div class="hf-weekdate">${esc(L.fmtLongDate(week.delivery_date))}</div>
        <div class="hf-weekpills">${pills.join("")}${sub ? `<span class="hf-weeksub">${sub}</span>` : ""}</div>
        ${deadlineLine}${people}
      </div>
      <div class="hf-actions">${actions.join("")}</div>
    </div>`;
}

// "For 2 people · Change": how many people the week's box is for (its own count, else the
// plan's), with the way to change that for this week alone, while the box can still change.
function renderPeopleLine(card, week) {
  if (!L.isWeekEditable(week)) return "";
  const people = L.weekServings(week, card.summary);
  if (!people) return "";
  return `<div class="hf-weekpeople">${icon("mdi:account-multiple-outline")}<span>${ht("servings.for", {
    people: t("card.people", { count: people }),
  })}</span><button class="hf-link" data-action="servings" data-focus-key="servings-change"
      aria-label="${ht("servings.change_aria")}" ${card.busy ? "disabled" : ""}>${ht("servings.change")}</button></div>`;
}

// ---- one week's number of people ----------------------------------------------------------
//
// hellofresh.change_week_servings changes the people for ONE week's box (the plan, and every
// other box, stay as they are). The sheet lists the box's sizes from get_plan_options with their
// prices; picking one asks first, then writes, re-reads that week and offers Undo.

// The plan's currency, which the plan options' prices are in (they carry none of their own).
function planCurrency(card) {
  return (
    (card.account && card.account.selected_plan_total_price_currency) ||
    (card.summary && card.summary.selected_plan_total_price_currency) ||
    null
  );
}

export function openServings(card, week) {
  if (!week || !L.isWeekEditable(week)) return;
  const weekId = week.week_id;
  const current = () => card.weekById(weekId) || week;
  card.openSheet({
    kind: "servings",
    narrow: true,
    label: t("servings.title"),
    render: () => renderServingsSheet(card, current()),
    onClick: (_ev, el) => {
      const action = el && el.getAttribute("data-action");
      if (action === "servings-retry") card.loadPlanOptions();
      else if (action === "servings-pick") confirmServings(card, current(), Number(el.getAttribute("data-servings")));
    },
  });
  card.loadPlanOptions();
}

function renderServingsSheet(card, week) {
  const head = UI.sheetHead(t("servings.title"), t("servings.only_this_box", { date: L.fmtLongDate(week.delivery_date) }));
  const state = card.planOptions;
  let body;
  if (!state || state.status === "loading") {
    body = `<div class="hf-empty">${ht("servings.loading")}</div>`;
  } else if (state.status === "failed") {
    body = `<div class="hf-empty">${icon("mdi:cloud-alert-outline")}${ht("servings.load_failed", { error: state.error })}
        <div class="hf-actions" style="justify-content:center;margin-top:12px">
        <button class="hf-btn" data-action="servings-retry" data-focus-key="servings-retry">${ht("common.retry")}</button></div></div>`;
  } else {
    const choices = L.servingsChoices(week, state.options, card.summary);
    const currency = planCurrency(card);
    body = choices.some((c) => !c.current)
      ? choices
          .map((c) => {
            const people = t("card.people", { count: c.servings });
            const side = c.current
              ? pill(t("reschedule.current"), "ok")
              : c.price !== null
                ? `<span class="hf-muted hf-small">${esc(L.fmtPrice(c.price, currency))}</span>`
                : "";
            return `<button class="hf-controlrow hf-btn ghost hf-choice" data-action="servings-pick" data-servings="${c.servings}"
                data-focus-key="servings:${c.servings}" ${c.current || card.busy ? "disabled" : ""}>
                <span class="hf-choicetext"><span class="hf-controllabel">${esc(people)}</span>
                  <span class="hf-controlhint">${esc(t("card.plan_size", { count: c.meals, people }))}</span></span>
                <span>${side}</span></button>`;
          })
          .join("")
      : `<div class="hf-empty">${ht("servings.none")}</div>`;
  }
  return `${head}<div class="hf-sheetbody" style="gap:4px">${body}</div>`;
}

// "Change this box to 4 people?": for this week only, at the price HelloFresh lists for it.
function confirmServings(card, week, servings) {
  const state = card.planOptions;
  const choice =
    state && state.status === "ok" ? L.servingsChoices(week, state.options, card.summary).find((c) => c.servings === servings) : null;
  if (!choice || choice.current) return;
  const weekId = week.week_id;
  const people = t("card.people", { count: servings });
  const date = L.fmtLongDate(week.delivery_date);
  const price =
    choice.price !== null
      ? ` ${ht("servings.confirm_price", {
          size: t("card.plan_size", { count: choice.meals, people }),
          price: strong(L.fmtPrice(choice.price, planCurrency(card))),
        })}`
      : "";
  card.openSheet({
    kind: "confirm",
    narrow: true,
    label: t("servings.confirm_title", { people }),
    render: () => `
      ${UI.sheetHead(t("servings.confirm_title", { people }), t("servings.only_this_box", { date }))}
      <div class="hf-sheetbody"><p class="hf-confirmtext" style="margin:0">${ht("servings.confirm_body", { date: strong(date) })}${price}</p></div>
      <div class="hf-sheetfoot"><button class="hf-btn" data-close-sheet>${ht("common.cancel")}</button>
        <button class="hf-btn primary" data-action="confirm-servings" ${card.busy ? "disabled" : ""}>${ht("servings.confirm")}</button></div>`,
    onClick: (_ev, el) => {
      if (!el || el.getAttribute("data-action") !== "confirm-servings") return;
      card.closeSheet();
      changeServings(card, card.weekById(weekId) || week, servings);
    },
  });
}

// Write one week's number of people, in the card's saving state, then re-read that week. Its
// toast offers Undo, which writes the old number back (`undoing`: that undo itself).
export async function changeServings(card, week, servings, { undoing = false } = {}) {
  if (card.busy || !week) return;
  if (!undoing && !L.isWeekEditable(week)) {
    // The deadline went by while the sheet was open: say why rather than send HelloFresh a
    // change it will refuse. (An undo is checked against the deadline when it is pressed.)
    card.renderView();
    card.toast(t("week.skip_closed"), true);
    return;
  }
  const weekId = week.week_id;
  const before = L.weekServings(week, card.summary);
  card.setSaving(t(undoing ? "undo.undoing" : "servings.saving"));
  card.renderView();
  let failed = null;
  try {
    await card.call("change_week_servings", { week_id: weekId, servings });
  } catch (err) {
    // HelloFresh may not offer this at all; its own words say so.
    failed = (err && err.message) || String(err);
  }
  card.setSaving(null);
  if (!failed) card.broadcastDataChanged({ weekId });
  await card.reloadWeeks({ weekId });
  if (undoing) card.undoResult(failed);
  else if (failed) card.toast(t("servings.failed", { error: failed }), true);
  else {
    const undo =
      before && before !== servings
        ? card.weekUndo(weekId, () => changeServings(card, card.weekById(weekId) || week, before, { undoing: true }))
        : null;
    card.toast(t("servings.done", { people: t("card.people", { count: servings }) }), false, { undo });
  }
}

// Notices and delivery detail between the header and the grid.
function renderWeekNotices(card, week, kind, view) {
  const out = [];
  if (card.box.downgradedWeek === week.week_id) {
    out.push(`<div class="hf-notice tone-warn" role="alert">${icon("mdi:alert-outline")}
      <div class="hf-noticebody"><strong>${ht("box.downsized_title")}</strong> ${ht("box.downsized_body")}</div>
      <button class="hf-iconbtn" data-action="dismiss-downgrade" aria-label="${ht("common.dismiss")}">${icon("mdi:close")}</button></div>`);
  }
  if (L.isSkipped(week)) {
    out.push(`<div class="hf-notice tone-muted">${icon("mdi:calendar-remove-outline")}
      <div class="hf-noticebody"><strong>${ht("week.skipped_title")}</strong> ${ht("overview.skipped_body")}
        ${L.canSkip(week) ? ht("week.unskip_to_choose") : ""}</div>
      ${L.canSkip(week) ? `<button class="hf-btn sm primary" data-action="skip" ${card.busy ? "disabled" : ""}>${ht("week.unskip")}</button>` : ""}</div>`);
  } else if (kind === "menu" && L.isAutoPicked(week)) {
    const deadline = week.selection_deadline ? new Date(week.selection_deadline) : null;
    out.push(`<div class="hf-notice tone-warn">${icon("mdi:auto-fix")}
      <div class="hf-noticebody"><strong>${ht("week.autopicked_title")}</strong> ${
        deadline ? ht("week.autopicked_before", { date: L.fmtDateTime(deadline) }) : ht("overview.autopicked_body")
      }</div></div>`);
  } else if (!L.isWeekEditable(week) && !L.isPastWeek(week) && !L.isDelivered(week) && !L.isShipping(week)) {
    out.push(`<div class="hf-notice tone-muted">${icon("mdi:lock-outline")}
      <div class="hf-noticebody">${ht("week.closed")}</div></div>`);
  }
  out.push(UI.revisedNotice(week));
  const history = L.isHistoryWeek(week, card.menuGraceWeeks());
  if (L.trackingStep(week) >= 0 && !history) {
    const total = L.boxTotal(week, card.account);
    const extra = [];
    if (total) extra.push(`${esc(total.label)} <strong>${esc(L.fmtPrice(total.amount, total.currency))}</strong>`);
    if (week.order && week.order.order_id) extra.push(ht("delivery.order", { id: week.order.order_id }));
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
  const meals = L.mealsCount(card.box.displayMeals(week));
  const extras = UI.extrasCount(card.box.displayMarket(week));
  const btn = (key, label, iconName, count) =>
    `<button data-action="goto-kind" data-view="${key}" aria-pressed="${kind === key}">${icon(iconName)}${label}${
      count ? ` <span class="hf-muted">${count}</span>` : ""
    }</button>`;
  return `<div class="hf-segment" role="group" aria-label="${ht("box.kind_label")}">${btn("menu", ht("box.meals"), "mdi:silverware-fork-knife", meals)}${btn(
    "market",
    ht("box.extras"),
    "mdi:storefront-outline",
    extras
  )}</div>`;
}

// After an edit that left the rest of the view standing: bring the few things outside the grid
// that count the box up to date — the week chip's unsaved dot, the Meals | Extras switch and
// the box bar.
function patchBoxChrome(card, week, kind) {
  const root = mainOf(card);
  if (root) {
    const chip = root.querySelector(`.hf-weekchip[data-week-id="${CSS.escape(week.week_id)}"]`);
    if (chip) {
      const dot = chip.querySelector(".hf-wcdirty");
      const dirty = card.box.dirty(week);
      if (dirty && !dot) chip.insertAdjacentHTML("afterbegin", dirtyDot());
      else if (!dirty && dot) dot.remove();
    }
    const segment = root.querySelector(".hf-toolbar .hf-segment");
    if (segment) UI.replaceNode(segment, renderKindSwitch(card, week, kind));
  }
  card.renderBoxBar();
}

// Redraw only a week view's results (the grid and what is said above it), leaving the toolbar
// — and with it the search field the user may be typing in — in the page. False when the view
// isn't on screen in that shape, and the caller should render it whole.
function redrawResults(card, markup) {
  const root = mainOf(card);
  const host = root && root.querySelector(".js-results");
  if (!host) return false;
  host.innerHTML = markup;
  return true;
}

// The stepper or Add button a quantity change came from, so the keyboard can be put back on it
// (or on what took its place) once its tile has been redrawn. Null for a tap or click that
// left focus elsewhere.
function focusedControl(card, el) {
  const active = card.shadowRoot && card.shadowRoot.activeElement;
  return active && el && active === el ? el.getAttribute("data-focus-key") : null;
}

// After a quantity change: focus the same control, else its neighbours ("+ Add" becomes the
// stepper's +, the last serving's − becomes "+ Add").
function refocusControl(card, focusKey) {
  if (!focusKey) return;
  const base = focusKey.slice(0, focusKey.lastIndexOf(":"));
  const order = focusKey.endsWith(":dec") ? ["dec", "add", "inc"] : ["inc", "dec", "add"];
  UI.focusKeyed(mainOf(card), [focusKey, ...order.map((side) => `${base}:${side}`)]);
}

// Actions both week views handle identically. Returns true when handled.
function handleWeekAction(card, view, week, action, el) {
  switch (action) {
    case "select-week":
      card.selectWeek(el.getAttribute("data-week-id"));
      return true;
    case "strip-page":
      UI.pageStrip(el, Number(el.getAttribute("data-dir")) || 0);
      return true;
    case "goto-kind":
      card.navigate(el.getAttribute("data-view"));
      return true;
    case "skip":
      if (!L.canSkip(week)) {
        // The deadline went by while the button was on screen: take it away and say why,
        // rather than send HelloFresh a change it will refuse.
        card.renderView();
        card.toast(t("week.skip_closed"), true);
        return true;
      }
      card.confirmSkip(week);
      return true;
    case "reschedule":
      card.openReschedule(week);
      return true;
    case "servings":
      openServings(card, week);
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
    for (const p of L.PROTEIN_FILTERS) if (f.protein.has(p)) out.push({ kind: "protein", value: p, label: L.proteinLabel(p) });
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
  // without a protein for the protein chips. Cached per week + query.
  //
  // The answer is one of four things, and "no filter" must never be confused with "couldn't
  // find out": {state: "none"} (nothing to ask), "pending" (asked, not answered), "failed"
  // (kept until Retry or the next refresh, so a broken service isn't hammered) or "ok" with the
  // matching `ids`.
  _lookup(week, filters) {
    if (!Object.keys(filters).length) return { state: "none", ids: null };
    const key = L.serverFilterKey(week.week_id, filters);
    const cached = this._serverCache.get(key);
    if (cached) return cached;
    if (!this._serverPending.has(key)) this._fetchServerIds(week.week_id, filters, key);
    return { state: "pending", ids: null };
  }

  async _fetchServerIds(weekId, filters, key) {
    this._serverPending.add(key);
    // A refresh replaces the cache while this is on its way; the answer belongs to the old one.
    const cache = this._serverCache;
    try {
      const response = await this.card.call("get_menu_courses", { week_id: weekId, filters });
      cache.set(key, { state: "ok", ids: new Set((response.recipe_ids || []).map((id) => String(id).split("-")[0])) });
    } catch (err) {
      // eslint-disable-next-line no-console
      console.warn("hellofresh: menu filter lookup failed", err);
      cache.set(key, { state: "failed", ids: null });
    } finally {
      this._serverPending.delete(key);
      if (this.card.view === "menu") this.card.renderView();
    }
  }

  // "Retry" on a failed lookup: forget the failures, and the next render asks again.
  _retryLookups() {
    for (const [key, entry] of [...this._serverCache]) if (entry.state === "failed") this._serverCache.delete(key);
  }

  // ---- render ------------------------------------------------------------------------------

  render() {
    const card = this.card;
    const placeholder = card.weeksPlaceholder();
    if (placeholder) return placeholder;
    if (!card.weeks.length) {
      return `<div class="hf-empty">${icon("mdi:calendar-blank-outline")}${ht("menu.no_weeks")}</div>`;
    }
    const week = card.selectedWeek() || card.weeks[0];
    return `${renderWeekStrip(card, week)}${renderWeekHead(card, week, "menu")}${renderWeekNotices(
      card,
      week,
      "menu",
      this
    )}${this._renderBody(week)}`;
  }

  afterRender(root, context) {
    UI.syncStrip(root, this, this.card.selectedWeek(), context);
    UI.watchSkipCutoff(this.card, this);
  }

  // What the grid shows for a week under the current search and filters, and why. Also
  // refreshes the lookups the click handlers use (`_rendered`, `_units`).
  _model(week) {
    const card = this.card;
    const box = card.box;
    const history = L.isHistoryWeek(week, card.menuGraceWeeks());
    const { recipes } = box.deduped(week);
    this._rendered = new Map(recipes.map((r) => [String(L.selKey(r)), r]));
    this._units = new Map();
    const selection = box.displayMeals(week);
    const sel = (r) => L.tileSelected(selection, r);
    const f = this.f;
    const applyFilters = !history && !f.selectedOnly;
    const sectionIds = applyFilters ? L.menuSectionIds(week, f.section) : null;
    const serverFilters = applyFilters ? L.activeServerFilters(week, f.server) : {};
    const server = this._lookup(week, serverFilters);
    const protein = this._lookup(week, applyFilters ? L.proteinServerFilters(week, f.protein) : {});
    const unanswered = (lookup) => lookup.state === "pending" || lookup.state === "failed";
    // "Ingredients to avoid" has nothing in the menu data to fall back on. Until HelloFresh has
    // answered, showing the menu unfiltered would put the very meals the user asked to avoid in
    // front of them with the filter looking active — so only what is already in the box shows.
    const failClosed = unanswered(server) && Boolean(serverFilters["exclude-allergens"]);
    const visible = L.menuTiles(recipes, {
      sel,
      selectedOnly: f.selectedOnly && !history,
      applyFilters,
      protein: f.protein,
      proteinIds: protein.ids,
      diet: f.diet,
      time: f.time,
      highlight: f.highlight,
      sectionIds,
      serverIds: failClosed ? new Set() : server.ids,
      query: this.query,
    });
    for (const unit of visible) this._units.set(String(L.selKey(unit.recipe)), unit);
    return {
      recipes,
      history,
      selection,
      sel,
      applyFilters,
      visible,
      total: L.menuTiles(recipes, { sel }).length, // the grid before any narrowing
      failClosed,
      // "pending" | "failed" | "": how the lookups the grid depends on stand.
      lookups: [server, protein].some((l) => l.state === "failed")
        ? "failed"
        : [server, protein].some((l) => l.state === "pending")
          ? "pending"
          : "",
    };
  }

  _renderBody(week) {
    const card = this.card;
    const model = this._model(week);
    if (!model.recipes.length) {
      const message = L.isSkipped(week)
        ? ht("menu.no_meals_skipped")
        : L.isPastWeek(week)
          ? ht("menu.no_meals_delivered")
          : ht("menu.not_published");
      return `<div class="hf-empty">${icon("mdi:silverware-clean")}${message}</div>`;
    }
    const { history, applyFilters } = model;
    const f = this.f;
    const active = applyFilters ? this._activeFilters(week) : [];
    const toolbar = `<div class="hf-toolbar">
        ${renderKindSwitch(card, week, "menu")}
        ${UI.searchBox("meal-search", this.query, t(history ? "menu.search_delivered" : "menu.search"))}
        ${history ? "" : `<button class="hf-chip${f.expanded ? " on" : ""}" data-action="toggle-filters" data-focus-key="menu-filters" aria-expanded="${f.expanded}">
            ${icon("mdi:tune-variant")}${ht("menu.filters")}${active.length ? ` · ${active.length}` : ""}</button>
          <button class="hf-chip${f.selectedOnly ? " on" : ""}" data-action="toggle-selected-only" data-focus-key="menu-in-box" aria-pressed="${f.selectedOnly}">
            ${icon("mdi:package-variant-closed")}${ht("menu.in_my_box")}</button>`}
        <span class="hf-spacer"></span>
        ${model.lookups === "pending" && !model.failClosed ? `<span class="hf-busynote" role="status">${ht("menu.filtering")}</span>` : ""}
      </div>`;
    const chips =
      active.length && !f.expanded
        ? `<div class="hf-activefilters">${active
            .map(
              (a) => `<button class="hf-chip on" data-action="remove-filter" data-kind="${esc(a.kind)}" data-value="${esc(a.value)}"
                  title="${ht("menu.remove_filter", { label: a.label })}">${esc(a.label)}${icon("mdi:close")}</button>`
            )
            .join("")}<button class="hf-link hf-small" data-action="clear-filters">${ht("menu.clear_all")}</button></div>`
        : "";
    const panel = applyFilters && f.expanded ? this._renderFilterPanel(week, active) : "";
    return `${toolbar}${chips}${panel}<div class="js-results">${this._renderResults(week, model)}</div>`;
  }

  // What the filter service's silence means for the grid below, with the way out. Its answer
  // for "Ingredients to avoid" is the only source there is, so that one says what is hidden.
  _lookupNotice(model) {
    if (model.failClosed && model.lookups !== "failed") {
      return `<div class="hf-notice tone-info hf-filternotice" role="status">${icon("mdi:shield-search")}
        <div class="hf-noticebody">${ht("menu.allergen_checking")}</div></div>`;
    }
    if (model.lookups !== "failed") return "";
    return `<div class="hf-notice tone-warn hf-filternotice" role="alert">${icon("mdi:alert-outline")}
      <div class="hf-noticebody">${ht(model.failClosed ? "menu.allergen_failed" : "menu.filter_failed")}</div>
      <button class="hf-btn sm" data-action="retry-filters" data-focus-key="menu-retry-filters">${ht("common.retry")}</button></div>`;
  }

  // The grid with what is said above it: everything a keystroke in the search field changes.
  _renderResults(week, model = this._model(week)) {
    const card = this.card;
    const f = this.f;
    const { visible, total, history } = model;
    const notice = this._lookupNotice(model);
    if (!visible.length) {
      // With the menu held back for an unanswered "Ingredients to avoid", the notice already
      // says why nothing is listed.
      if (model.failClosed && !this.query.trim()) return notice;
      const why = this.query.trim()
        ? ht("menu.no_match_query", { query: this.query.trim() })
        : f.selectedOnly
          ? ht("menu.nothing_in_box", { in_my_box: t("menu.in_my_box") })
          : ht("menu.no_match_filters");
      return `${notice}<div class="hf-empty">${icon("mdi:food-off-outline")}${why}</div>`;
    }
    const ctx = this._tileContext(week, model);
    const shown = visible.slice(0, this.limit);
    const narrowed = visible.length !== total;
    const note = narrowed
      ? `<p class="hf-resultnote">${ht("menu.result_count", { shown: visible.length, total: t("meals.count", { count: total }) })}</p>`
      : history
        ? `<p class="hf-resultnote">${ht("menu.delivered_this_week")}</p>`
        : "";
    return `${notice}${note}<div class="hf-grid${card.busy ? " busy" : ""}">${shown.map((unit) => this._tile(unit, ctx)).join("")}</div>
      ${visible.length > shown.length ? `<div class="hf-more-row"><button class="hf-btn" data-action="more" data-focus-key="menu-more">
        ${ht("menu.show_more", { count: Math.min(PAGE_SIZE, visible.length - shown.length), left: visible.length - shown.length })}</button></div>` : ""}`;
  }

  // Redraw just the results (see redrawResults); the whole view when it isn't on screen.
  _redrawResults() {
    const card = this.card;
    const week = card.selectedWeek();
    if (card.view !== "menu") return;
    if (!week || !redrawResults(card, this._renderResults(week))) card.renderView();
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
          t("filters.categories"),
          this._chip("f-section", "", ht("filters.all"), all) +
            sections.map((s) => this._chip("f-section", s.slug, esc(s.name), f.section === s.slug)).join("")
        )
      );
    }
    rows.push(
      this._filterRow(
        t("filters.main_protein"),
        this._chip("f-protein-all", "", ht("filters.all"), f.protein.size === 0) +
          L.PROTEIN_FILTERS.map((p) =>
            this._chip("f-protein", p, `<span class="hf-dot" style="background:${L.PREFERENCE_COLORS[p]}"></span>${esc(L.proteinLabel(p))}`, f.protein.has(p))
          ).join("")
      )
    );
    rows.push(
      this._filterRow(
        t("filters.dietary"),
        this._chip("f-diet-all", "", ht("filters.all"), f.diet.size === 0) +
          L.DIET_FILTERS.map((d) => this._chip("f-diet", d.key, esc(d.label), f.diet.has(d.key))).join("")
      )
    );
    for (const group of L.serverFilterGroups(week)) {
      const set = f.server[group.slug] || new Set();
      rows.push(
        this._filterRow(
          group.name || group.slug,
          this._chip("f-server-all", group.slug, ht("filters.all"), set.size === 0) +
            group.options
              .map((o) => this._chip("f-server", o.slug, esc(o.name), set.has(o.slug), ` data-group="${esc(group.slug)}"`))
              .join("")
        )
      );
    }
    rows.push(
      this._filterRow(
        t("filters.cooking_time"),
        this._chip("f-time", "", ht("filters.any"), !f.time) +
          L.TIME_FILTERS.map((time) => this._chip("f-time", time.key, esc(time.label), f.time === time.key)).join("")
      )
    );
    rows.push(
      this._filterRow(
        t("filters.highlights"),
        this._chip("f-highlight", "", ht("filters.all"), !f.highlight) +
          L.HIGHLIGHT_FILTERS.map((h) =>
            this._chip("f-highlight", h.key, `${h.key === "favorite" ? icon("mdi:heart-outline") : ""}${esc(h.label)}`, f.highlight === h.key)
          ).join("")
      )
    );
    return `<div class="hf-filterpanel">${rows.join("")}
      ${active.length ? `<div class="hf-actions" style="justify-content:flex-end;padding-top:8px"><button class="hf-btn sm ghost" data-action="clear-filters">${ht("filters.clear_all")}</button></div>` : ""}</div>`;
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
          data-focus-key="meal:${esc(String(L.selKey(r)))}:customize" aria-haspopup="dialog" aria-label="${ht("customize.aria", { dish: group.base.name, option: L.optionLabel(r), options: t("customize.options", { count: options }) })}">
          ${icon("mdi:tune-variant")}<span class="hf-optlabel">${custom ? esc(L.optionLabel(r)) : ht("customize.title")}</span>
          ${custom ? "" : `<span class="hf-optcount">${ht("customize.options", { count: options })}</span>`}${icon("mdi:chevron-down")}</button>`;
    }
    return r.variation_title ? `<div class="hf-tvariant">${esc(r.variation_title)}</div>` : "";
  }

  _tile(unit, ctx) {
    const { recipe: r, group } = unit;
    const key = esc(String(L.selKey(r)));
    const selected = ctx.sel(r);
    const qty = selected ? L.tileQuantity(ctx.selection, r) : 0;
    // A meal already in the saved box stays the customer's once it sells out (HelloFresh holds
    // it, and its website shows it as usual), so it gets no ribbon.
    const soldOut = r.is_sold_out === true && ctx.showSoldOut && !L.isRecipeSelected(r);
    const img = L.resizedImage(r.image_url, ctx.imageWidth);
    const video = L.safeMediaUrl(r.video_url);
    const meta = [];
    const mins = r.prep_time_minutes != null ? r.prep_time_minutes : r.total_time_minutes;
    if (mins != null) meta.push(`<span>${icon("mdi:timer-outline")}${ht("time.duration.minutes", { minutes: mins })}</span>`);
    if (r.calories_kcal != null) meta.push(`<span>${icon("mdi:fire")}${esc(Math.round(r.calories_kcal))} kcal</span>`);
    if (r.protein_g != null) meta.push(`<span>${ht("menu.protein_g", { grams: Math.round(r.protein_g) })}</span>`);
    const labels = L.tileChipLabels(r);
    const chips = [];
    if (r.preference === "Veggie") chips.push(`<span class="hf-tchip veggie">${esc(L.proteinLabel("Veggie"))}</span>`);
    for (const label of labels.slice(0, 2)) chips.push(`<span class="hf-tchip">${esc(label)}</span>`);
    if (labels.length > 2) chips.push(`<span class="hf-tchip" title="${esc(labels.slice(2).join(", "))}">+${labels.length - 2}</span>`);
    const hist = [];
    if (r.delivered_count) {
      hist.push(
        r.last_delivered_week
          ? ht("menu.ordered_last", { count: r.delivered_count, week: r.last_delivered_week })
          : ht("menu.ordered", { count: r.delivered_count })
      );
    }
    if (r.rating) hist.push(ht("menu.you_rated", { rating: r.rating, scale: r.rating_scale || 5 }));

    let control = "";
    if (ctx.editable && !selected) {
      control = `<button class="hf-add" data-action="add" data-key="${key}" data-focus-key="meal:${key}:add" aria-label="${ht("menu.add_aria", { name: r.name })}">${icon("mdi:plus")}${ht("menu.add")}</button>`;
    } else if (ctx.editable && selected) {
      // In the box: the servings stepper takes the whole footer (the price is in the box now).
      control = UI.stepper({
        label: t("menu.servings_of", { name: r.name }),
        attrs: `data-action="qty" data-key="${key}"`,
        focusKey: `meal:${L.selKey(r)}`,
        qty,
        unit: t("menu.serving_unit", { count: qty }),
        dec: { label: t(qty === 1 ? "menu.remove_meal" : "menu.fewer_servings"), icon: qty === 1 ? "mdi:trash-can-outline" : "mdi:minus" },
        inc: { label: t("menu.more_servings"), disabled: qty >= L.MAX_MEAL_SERVINGS },
        wide: true,
      });
    } else if (selected && !ctx.history) {
      control = `<span class="hf-ordered">${icon("mdi:check")} ${ht("menu.in_box")}${qty > 1 ? ` · ${qty}×` : ""}</span>`;
    }
    const price =
      r.price != null && !(ctx.editable && selected)
        ? `<span class="hf-tprice">${esc(L.fmtPerServing(r.price, r.currency || ctx.currency))}<span>${ht("menu.per_serving")}</span></span>`
        : "";
    // Indexed with a payload string, so only a known protein's own colour may come back (a
    // name like "constructor" would otherwise fetch something off Object.prototype).
    const color = (Object.hasOwn(L.PREFERENCE_COLORS, r.preference) && L.PREFERENCE_COLORS[r.preference]) || "var(--hf-muted)";
    const mark = (cls, label, iconName) =>
      `<span class="${cls}" role="img" aria-label="${esc(label)}" title="${esc(label)}">${icon(iconName)}</span>`;
    const topRight = [
      r.is_favorite === true ? mark("hf-favdot", t("menu.in_cookbook"), "mdi:heart") : "",
      selected && !ctx.history ? mark("hf-checkmark", t("menu.in_your_box"), "mdi:check-bold") : "",
      qty > 1 ? `<span class="hf-overlaypill">${qty}×</span>` : "",
    ].join("");
    const protein = L.PROTEIN_FILTERS.includes(r.preference) ? L.proteinLabel(r.preference) : r.preference || "";
    // The protein dot is colour only; a named protein gets a text alternative with it.
    const dot = `<span class="hf-pdot" style="background:${color}"${
      protein ? ` role="img" aria-label="${esc(protein)}" title="${esc(protein)}"` : ' aria-hidden="true"'
    }></span>`;
    // The tile is a plain container. Its one "open" control is the dish's name, a real button;
    // Add, the stepper, the video and the customize selector are that button's siblings, not
    // controls nested inside another control. (A click anywhere else on the tile still opens
    // the recipe: the container keeps the action for the mouse.)
    return `<div class="hf-tile${selected && !ctx.history ? " selected" : ""}${soldOut ? " soldout" : ""}"
        data-action="open-recipe" data-key="${key}">
        <div class="hf-media">
          ${img ? `<img loading="lazy" src="${esc(img)}" alt="">` : `<div class="hf-noimg"></div>`}
          ${soldOut ? `<div class="hf-soldout">${ht("menu.sold_out")}</div>` : ""}
          <div class="hf-tl">${r.badge ? `<span class="hf-badge"${L.badgeStyle(r)}>${esc(r.badge)}</span>` : ""}</div>
          <div class="hf-tr">${topRight}</div>
          <div class="hf-bl">${video ? `<button class="hf-play" data-action="play" data-key="${key}" aria-label="${ht("menu.play_video", { name: r.name })}">${icon("mdi:play")}</button>` : ""}</div>
          <div class="hf-br">${hasSurcharge(r) ? `<span class="hf-overlaypill" title="${ht("menu.surcharge")}">${esc(L.fmtSurcharge(r, ctx.currency))}</span>` : ""}</div>
        </div>
        <div class="hf-tbody">
          <div class="hf-tname">${dot}<button class="hf-open" data-action="open-recipe" data-key="${key}" data-focus-key="meal:${key}:open"
            aria-label="${ht("recipe.open", { name: r.name })}"><span>${esc(r.name)}</span></button></div>
          ${this._optionLine(r, group, ctx)}
          ${r.description ? `<div class="hf-tdesc">${esc(r.description)}</div>` : ""}
          ${meta.length ? `<div class="hf-tmeta">${meta.join("")}</div>` : ""}
          ${chips.length ? `<div class="hf-tchips">${chips.join("")}</div>` : ""}
          ${hist.length ? `<div class="hf-thist">${hist.join(" · ")}</div>` : ""}
          <div class="hf-tfoot">${price}${control}</div>
        </div>
      </div>`;
  }

  // After a selection edit: redraw as little as shows it. With no search, filter or "In my box"
  // on, an edit cannot change WHICH tiles are visible, so only that tile is swapped; otherwise
  // the results are redrawn. Either way the toolbar stays, the few counters outside the grid
  // are patched, and `focusKey` (the control the edit came from) keeps the keyboard.
  afterSelection(week, recipe, focusKey = null) {
    const card = this.card;
    card.box.schedulePreview(week);
    if (card.view !== "menu") {
      card.renderBoxBar();
      return;
    }
    const root = mainOf(card);
    if (!root || !root.querySelector(".js-results")) {
      card.renderView();
      return;
    }
    const f = this.f;
    const visibilityMayChange = f.selectedOnly || this.query.trim() || this._activeFilters(week).length;
    const key = String(L.selKey(recipe));
    const tile = visibilityMayChange ? null : root.querySelector(`.hf-tile[data-key="${CSS.escape(key)}"]`);
    const unit = this._units && this._units.get(key);
    if (tile && unit) {
      const selection = card.box.displayMeals(week);
      UI.replaceNode(tile, this._tile(unit, this._tileContext(week, { selection, sel: (r) => L.tileSelected(selection, r) })));
    } else {
      // A filter may now show or hide tiles, or no tile shows this meal as itself (an option
      // added from its recipe sheet while its dish's tile shows the base): rebuild the grid.
      this._redrawResults();
    }
    patchBoxChrome(card, week, "menu");
    refocusControl(card, focusKey);
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
      label: t("customize.label", { dish: unit.group.base.name }),
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
        // Advisory, as on the grid's tiles: the flag comes from HelloFresh's regional catalog,
        // not from what this subscription can order, so the option stays choosable under a
        // "Sold out" note and HelloFresh decides on save.
        const soldOut = m.is_sold_out === true && L.isWeekEditable(week) && !L.isRecipeSelected(m);
        // Another tile already holds this option (two versions of the dish in one box).
        const taken = m !== current && L.tileSelected(selection, m);
        const note = taken ? t("customize.taken") : soldOut ? t("menu.sold_out") : "";
        const photo = L.isDefaultMeal(m) ? L.resizedImage(m.image_url, 160) : L.resizedImage(m.variation_image_url, 96);
        const price = hasSurcharge(m)
          ? `<span class="hf-optprice">${esc(L.fmtSurcharge(m, currency))}<span>${ht("menu.per_serving")}</span></span>`
          : `<span class="hf-optprice included">${ht("customize.included")}</span>`;
        const sub = m.name && m.name !== group.base.name ? m.name : L.isDefaultMeal(m) ? t("customize.as_written") : "";
        return `<button class="hf-optrow${on ? " on" : ""}${soldOut ? " soldout" : ""}" aria-pressed="${on}" data-action="opt-pick" data-key="${esc(key)}"
            data-focus-key="opt:${esc(key)}" ${taken || !editable ? "disabled" : ""}>
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
        ? `<button class="hf-btn primary" data-action="opt-apply" ${changed ? "" : "disabled"}>${ht("customize.update")}</button>`
        : `<button class="hf-btn primary" data-action="opt-apply">${icon("mdi:plus")}${ht("customize.add")}</button>`;
    return `
      ${UI.sheetHead(t("customize.title"), [group.base.name, inBox ? t("customize.in_box") : ""].filter(Boolean).join(" · "))}
      <div class="hf-sheetbody hf-optlist" role="group" aria-label="${ht("customize.ways")}">${rows}</div>
      <div class="hf-sheetfoot"><button class="hf-btn ghost" data-action="opt-recipe">${icon("mdi:book-open-page-variant-outline")}${ht("customize.view_recipe")}</button>
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

  // Typing in the search field redraws the results only: the field itself, and the toolbar
  // around it, stay in the page.
  onInput(ev) {
    if (UI.searchTyped(this, ev, "meal-search", () => this._redrawResults())) this.limit = PAGE_SIZE;
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
        if (recipe && card.box.addMeal(week, recipe)) this.afterSelection(week, recipe, focusedControl(card, el));
        break;
      case "qty":
        if (recipe && card.box.changeMeal(week, recipe, Number(el.getAttribute("data-delta")) || 0)) {
          this.afterSelection(week, recipe, focusedControl(card, el));
        }
        break;
      case "retry-filters":
        this._retryLookups();
        card.renderView();
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
      case "more": {
        const fromKeyboard = Boolean(focusedControl(card, el));
        const first = this.limit;
        this.limit += PAGE_SIZE;
        this._redrawResults();
        // The button was replaced (or is gone): carry on from the first tile it brought in.
        const root = mainOf(card);
        const next = fromKeyboard && root ? root.querySelectorAll(".js-results .hf-tile .hf-open")[first] : null;
        if (next) next.focus({ preventScroll: true });
        break;
      }
      case "clear-search": {
        clearTimeout(this._searchTimer);
        this.query = "";
        this.limit = PAGE_SIZE;
        // Emptied in place, so the caret stays in the field the button belongs to.
        const input = el.parentElement && el.parentElement.querySelector("input");
        UI.syncSearchBox(input, "");
        if (input) input.focus({ preventScroll: true });
        this._redrawResults();
        break;
      }
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
      return `<div class="hf-empty">${icon("mdi:storefront-outline")}${ht("menu.no_weeks")}</div>`;
    }
    const week = card.selectedWeek() || card.weeks[0];
    return `${renderWeekStrip(card, week)}${renderWeekHead(card, week, "market")}${renderWeekNotices(
      card,
      week,
      "market",
      this
    )}${this._renderBody(week)}`;
  }

  afterRender(root, context) {
    UI.syncStrip(root, this, this.card.selectedWeek(), context);
    UI.watchSkipCutoff(this.card, this);
  }

  // The shelves a week has, and which of them the shelf chips currently narrow to.
  _sections(week) {
    const all = L.marketSections(week);
    const present = new Set(all);
    const active = new Set([...this.sections].filter((s) => present.has(s)));
    const shown = !L.isPastWeek(week) && !this.selectedOnly && all.length >= 2;
    return { all, active, shown };
  }

  _renderBody(week) {
    const card = this.card;
    const past = L.isPastWeek(week);
    const all = week.market_items || [];
    this._items = new Map(all.map((i) => [String(i.item_id), i]));
    const { all: sections, active, shown: showSections } = this._sections(week);
    const toolbar = `<div class="hf-toolbar">
        ${renderKindSwitch(card, week, "market")}
        ${all.length ? UI.searchBox("market-search", this.query, t("market.search")) : ""}
        ${past || !all.length ? "" : `<button class="hf-chip${this.selectedOnly ? " on" : ""}" data-action="toggle-selected-only" data-focus-key="market-in-box"
          aria-pressed="${this.selectedOnly}">${icon("mdi:package-variant-closed")}${ht("menu.in_my_box")}</button>`}
      </div>`;
    const sectionBar = showSections
      ? `<div class="hf-rail" data-scroll-key="market-sections" style="margin-bottom:14px">${[
          `<button class="hf-chip${active.size === 0 ? " on" : ""}" data-action="section-all" data-focus-key="market-section:" aria-pressed="${active.size === 0}">${ht("filters.all")}</button>`,
          ...sections.map(
            (slug) => `<button class="hf-chip${active.has(slug) ? " on" : ""}" data-action="section" data-value="${esc(slug)}"
                data-focus-key="market-section:${esc(slug)}" aria-pressed="${active.has(slug)}">${esc(L.marketGroupLabel(slug))}</button>`
          ),
        ].join("")}</div>`
      : "";
    if (!all.length) {
      const message = ht(past ? "market.none_ordered" : "market.not_open");
      return `${toolbar}<div class="hf-empty">${icon("mdi:storefront-outline")}${message}</div>`;
    }
    return `${toolbar}${sectionBar}<div class="js-results">${this._renderResults(week)}</div>`;
  }

  // The shelves of tiles (or why there are none): everything a keystroke in the search field,
  // or a quantity change under a filter, can change.
  _renderResults(week) {
    const card = this.card;
    const past = L.isPastWeek(week);
    const selection = card.box.displayMarket(week);
    const { active, shown: showSections } = this._sections(week);
    const groups = L.marketGroups(week, selection, {
      selectedOnly: past || this.selectedOnly,
      sections: showSections ? active : null,
      query: this.query,
    });
    if (!groups.length) {
      const why = this.query.trim()
        ? ht("market.no_match_query", { query: this.query.trim() })
        : past
          ? ht("market.none_ordered")
          : this.selectedOnly
            ? ht("market.none_in_box")
            : ht("market.no_match_categories");
      return `<div class="hf-empty">${icon("mdi:storefront-outline")}${why}</div>`;
    }
    const ctx = this._tileContext(week);
    return groups
      .map(
        (g) => `<section class="hf-group">
          ${g.label ? `<div class="hf-grouphead"><h3 class="hf-h3">${esc(g.label)}</h3><span class="hf-groupcount">${g.items.length}</span></div>` : ""}
          <div class="hf-grid market${card.busy ? " busy" : ""}">${g.items
            .map((item) => this._tile(item, selection.get(item.item_id) || 0, ctx))
            .join("")}</div></section>`
      )
      .join("");
  }

  _redrawResults() {
    const card = this.card;
    const week = card.selectedWeek();
    if (card.view !== "market") return;
    if (!week || !redrawResults(card, this._renderResults(week))) card.renderView();
  }

  _tileContext(week) {
    return { editable: this.card.box.canEdit(week), currency: L.marketCurrency(week), past: L.isPastWeek(week) };
  }

  _tile(item, qty, { editable, currency, past }) {
    const id = esc(String(item.item_id));
    // Sold out stops adding, but what the saved box holds stays the customer's: it can be put
    // back after an unsaved removal (changeMarket), and it shows no ribbon or greyed photo.
    const saved = L.marketItemQuantity(item);
    const noMore = item.is_sold_out === true && qty >= saved;
    const soldOut = item.is_sold_out === true && saved === 0;
    const img = L.resizedImage(item.image_url, this.card.config.image_width);
    const cap = L.marketCap(item);
    let control = "";
    if (editable && qty === 0) {
      control = `<button class="hf-add" data-action="market-qty" data-delta="1" data-id="${id}" data-focus-key="extra:${id}:add" ${noMore ? "disabled" : ""}
        aria-label="${ht("market.add_aria", { name: item.name })}">${icon("mdi:plus")}${ht("menu.add")}</button>`;
    } else if (editable) {
      control = UI.stepper({
        label: t("market.quantity_of", { name: item.name }),
        attrs: `data-action="market-qty" data-id="${id}"`,
        focusKey: `extra:${item.item_id}`,
        qty,
        unit: t("market.in_box_unit"),
        dec: { label: t(qty === 1 ? "market.remove" : "common.fewer"), icon: qty === 1 ? "mdi:trash-can-outline" : "mdi:minus" },
        inc: { label: t("common.more"), disabled: qty >= cap || noMore },
        wide: true,
      });
    } else if (qty > 0) {
      control = `<span class="hf-ordered">${icon("mdi:check")} ${past ? ht("market.ordered", { count: qty }) : `${ht("menu.in_box")}${qty > 1 ? ` · ${qty}×` : ""}`}</span>`;
    }
    const price =
      item.price != null && !(editable && qty > 0)
        ? `<span class="hf-tprice">${esc(L.fmtPrice(item.price, item.currency || currency))}</span>`
        : "";
    // Add-ons carry a normal recipe id; an item known only by SKU has nothing to open. As on
    // the Menu, the tile is a plain container and its name is the one "open" button, beside
    // (not around) the Add button or stepper.
    const open = item.recipe_id ? `data-action="open-item" data-id="${id}"` : `data-id="${id}"`;
    const name = item.recipe_id
      ? `<button class="hf-open" data-action="open-item" data-id="${id}" data-focus-key="extra:${id}:open"
          aria-label="${ht("market.open", { name: item.name })}"><span>${esc(item.name)}</span></button>`
      : `<span>${esc(item.name)}</span>`;
    return `<div class="hf-tile${qty > 0 && !past ? " selected" : ""}${soldOut ? " soldout" : ""}${item.recipe_id ? "" : " plain"}" ${open}>
        <div class="hf-media">
          ${img ? `<img loading="lazy" src="${esc(img)}" alt="">` : `<div class="hf-noimg"></div>`}
          ${soldOut ? `<div class="hf-soldout">${ht("menu.sold_out")}</div>` : ""}
          <div class="hf-tr">${qty > 0 && !past ? `<span class="hf-checkmark" role="img" aria-label="${ht("menu.in_your_box")}">${icon("mdi:check-bold")}</span>` : ""}${qty > 1 ? `<span class="hf-overlaypill">${qty}×</span>` : ""}</div>
        </div>
        <div class="hf-tbody">
          <div class="hf-tname">${name}</div>
          ${item.description ? `<div class="hf-tdesc">${esc(item.description)}</div>` : ""}
          ${item.calories_kcal != null ? `<div class="hf-tmeta"><span>${icon("mdi:fire")}${esc(Math.round(item.calories_kcal))} kcal</span></div>` : ""}
          <div class="hf-tfoot">${price}${control}</div>
        </div>
      </div>`;
  }

  // After a quantity change: swap that one tile when nothing narrows the shelves (a narrowed
  // view keeps an item only while it is in the box, so there the shelves are redrawn), patch
  // the counters outside the grid, and put the keyboard back on the control it came from.
  _afterQuantity(week, item, focusKey = null) {
    const card = this.card;
    if (card.view !== "market") {
      card.renderBoxBar();
      return;
    }
    const root = mainOf(card);
    if (!root || !root.querySelector(".js-results")) {
      card.renderView();
      return;
    }
    const narrowed = L.isPastWeek(week) || this.selectedOnly || this._sections(week).active.size > 0;
    const tile = narrowed ? null : root.querySelector(`.hf-tile[data-id="${CSS.escape(String(item.item_id))}"]`);
    if (tile) UI.replaceNode(tile, this._tile(item, card.box.displayMarket(week).get(item.item_id) || 0, this._tileContext(week)));
    else this._redrawResults();
    patchBoxChrome(card, week, "market");
    refocusControl(card, focusKey);
  }

  _detailSelection(week, item) {
    const box = this.card.box;
    if (!box.canEdit(week)) return null;
    return {
      qty: box.displayMarket(week).get(item.item_id) || 0,
      maxQty: L.marketCap(item),
      unit: "item",
      add: () => box.changeMarket(week, item, 1) && this._afterQuantity(week, item),
      inc: () => box.changeMarket(week, item, 1) && this._afterQuantity(week, item),
      dec: () => box.changeMarket(week, item, -1) && this._afterQuantity(week, item),
    };
  }

  onInput(ev) {
    UI.searchTyped(this, ev, "market-search", () => this._redrawResults());
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
          this._afterQuantity(week, item, focusedControl(card, el));
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
      case "clear-search": {
        clearTimeout(this._searchTimer);
        this.query = "";
        const input = el.parentElement && el.parentElement.querySelector("input");
        UI.syncSearchBox(input, "");
        if (input) input.focus({ preventScroll: true });
        this._redrawResults();
        break;
      }
      default:
        break;
    }
  }
}

// ---- box bar & review sheet ---------------------------------------------------------------

// "+$5.99" / "−$5.99": what a change adds to the bill or takes off it.
function signedPrice(amount, currency) {
  return `${amount < 0 ? "−" : "+"}${L.fmtPrice(Math.abs(amount), currency)}`;
}

// The price beside the box, by what is pending:
//
//   * meals changed       -> "ok": HelloFresh's own estimate for exactly those meals (it prices
//                            meals, shipping, tax and discounts, not add-ons) plus the pending
//                            extras; "pending" / "loading" while that is on its way; and
//                            "unavailable" when there is no estimate and none is coming.
//   * only extras changed -> "extras": what the change adds or takes off (pending minus saved).
//   * nothing changed     -> "saved": the box's bill.
//   * a stale draft       -> "stale": no figure until the user has chosen what to keep.
function estimate(card, week) {
  const box = card.box;
  if (box.stale(week)) return { status: "stale" };
  const currency = L.marketCurrency(week) || (card.account && card.account.selected_plan_total_price_currency);
  const extrasCents = L.marketTotalCents(week, box.displayMarket(week));
  if (box.mealsDirty(week)) {
    const preview = box.previewState(week);
    if (preview.status !== "ok") return { status: preview.status };
    if (!preview.data || preview.data.grand_total == null) return { status: "unavailable" };
    return {
      status: "ok",
      amount: Number(preview.data.grand_total) + extrasCents / 100,
      currency,
      preview: preview.data,
      extras: extrasCents / 100,
    };
  }
  if (box.marketDirty(week)) {
    const savedCents = L.marketTotalCents(week, box.savedMarket(week));
    return { status: "extras", delta: (extrasCents - savedCents) / 100, currency };
  }
  const total = L.boxTotal(week, card.account);
  return total
    ? { status: "saved", amount: total.amount, currency: total.currency, label: total.estimate ? total.label : t("box.box_total") }
    : { status: "none" };
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
  const extraCount = UI.extrasCount(box.displayMarket(week));
  const slots = [];
  const slotCount = Math.max(required, count);
  for (let i = 0; i < Math.min(slotCount, 10); i += 1) {
    slots.push(`<span class="hf-slot${i < count ? (i >= required && required ? " extra" : " filled") : ""}"></span>`);
  }
  const line = [
    `<span>${required ? ht("box.selected_of_required", { selected: count, required }) : ht("meals.count", { count })}</span>`,
    servings !== count ? `<span class="hf-muted">${ht("meals.servings", { count: servings })}</span>` : "",
    extraCount ? `<span class="hf-muted">${ht("meals.extras", { count: extraCount })}</span>` : "",
  ].filter(Boolean).join("");
  const closed = dirty && !editable;
  const stale = !closed && box.stale(week);
  const invalid = box.mealsDirty(week) && count < L.MIN_MEALS;
  let note;
  let noteCls = "";
  if (closed) {
    note = t("box.deadline_passed");
    noteCls = " warn";
  } else if (stale) {
    note = t("box.changed_title");
    noteCls = " warn";
  } else if (invalid) {
    note = t("box.min_note", { min: L.MIN_MEALS });
    noteCls = " warn";
  } else if (required && count !== required && count > 0) {
    note = t("box.resized_note", { selected: count, required });
    noteCls = " warn";
  } else if (dirty) note = t("box.unsaved_changes");
  else note = t("box.saved_note");
  const est = estimate(card, week);
  let total = "";
  if (est.status === "ok") {
    total = `<span class="hf-boxamount">${esc(L.fmtPrice(est.amount, est.currency))}</span><span class="hf-boxamountnote">${ht("box.estimated_total")}</span>`;
  } else if (est.status === "loading" || est.status === "pending") {
    total = `<span class="hf-boxamountnote">${ht("box.updating_price")}</span>`;
  } else if (est.status === "unavailable" && !invalid && !closed) {
    // Below the minimum the note beside it already says why there is no price.
    total = `<span class="hf-boxamountnote">${ht("box.estimate_unavailable")}</span>`;
  } else if (est.status === "extras") {
    total = `<span class="hf-boxamount">${esc(signedPrice(est.delta, est.currency))}</span><span class="hf-boxamountnote">${ht("box.extras")}</span>`;
  } else if (est.status === "saved") {
    total = `<span class="hf-boxamount">${esc(L.fmtPrice(est.amount, est.currency))}</span><span class="hf-boxamountnote">${esc(est.label)}</span>`;
  }
  const busy = card.busy ? "disabled" : "";
  let actions;
  if (closed) {
    actions = `<button class="hf-btn" data-action="box-discard" data-focus-key="box-discard" ${busy}>${ht("box.discard_changes")}</button>`;
  } else if (stale) {
    // The saved box moved under this draft: the two ways on, instead of a Save that would
    // put the old box back.
    actions = `<button class="hf-btn" data-action="box-use-saved" data-focus-key="box-use-saved" ${busy}>${ht("box.use_saved")}</button>
       <button class="hf-btn primary" data-action="box-keep-mine" data-focus-key="box-keep-mine" ${busy}>${ht("box.keep_mine")}</button>`;
  } else if (dirty) {
    actions = `<button class="hf-btn ghost" data-action="box-discard" data-focus-key="box-discard" ${busy}>${ht("box.discard")}</button>
       <button class="hf-btn" data-action="box-review" data-focus-key="box-review">${ht("week.review")}</button>
       <button class="hf-btn primary" data-action="box-save" data-focus-key="box-save" ${card.busy || invalid ? "disabled" : ""}>${icon("mdi:content-save-outline")}${ht("box.save")}</button>`;
  } else {
    actions = `<button class="hf-btn" data-action="box-review" data-focus-key="box-review">${icon("mdi:package-variant-closed")}${ht("box.your_box")}</button>`;
  }
  return `<div class="hf-boxbarinner" role="region" aria-label="${ht("box.your_box")}">
      <div class="hf-boxslots" aria-hidden="true">${slots.join("")}</div>
      <div class="hf-boxsummary"><div class="hf-boxline">${line}</div><div class="hf-boxnote${noteCls}"${stale ? ' role="alert"' : ""}>${esc(note)}</div></div>
      ${total ? `<div class="hf-boxtotal">${total}</div>` : ""}
      <div class="hf-actions">${actions}</div>
    </div>`;
}

export function onBoxBarClick(card, _ev, el) {
  const week = card.selectedWeek();
  if (!el || !week) return;
  const action = el.getAttribute("data-action");
  const fromKeyboard = Boolean(card.shadowRoot && card.shadowRoot.activeElement === el);
  if (action === "box-save") card.box.save(week);
  else if (action === "box-review") openReview(card, week);
  else if (action === "box-discard" || action === "box-use-saved" || action === "box-keep-mine") {
    if (action === "box-keep-mine") card.box.rebase(week);
    else card.box.discard(week);
    card.renderView();
    // The button just used is gone from the bar; hand the keyboard to what replaced it.
    const bar = fromKeyboard && card.shadowRoot.querySelector(".js-boxbar");
    if (bar) UI.focusKeyed(bar, ["box-save", "box-review"]);
  }
}

function lineItem(img, name, sub, tag = "", removed = false) {
  return `<div class="hf-lineitem${removed ? " removed" : ""}">
      ${img ? `<img src="${esc(img)}" alt="" loading="lazy">` : `<div class="hf-noimg"></div>`}
      <div class="hf-lineitemtext"><div class="hf-lineitemname">${esc(name)}</div>${sub ? `<div class="hf-lineitemsub">${sub}</div>` : ""}</div>
      ${tag}
    </div>`;
}

// One half of the review (meals or extras): what is in the pending selection, tagged where it
// differs from the saved one, then what the pending selection drops. `row` builds a line from
// (thing, quantity, tag); a `null` quantity is a removal.
function reviewRows(pending, saved, lookup, row) {
  const out = [];
  for (const [key, qty] of pending) {
    const thing = lookup.get(key);
    if (!thing) continue;
    const was = saved.get(key);
    const tag = was == null ? pill(t("box.new"), "ok") : was !== qty ? pill(t("box.was", { count: was }), "info") : "";
    out.push(row(thing, qty, tag));
  }
  for (const key of saved.keys()) {
    const thing = pending.has(key) ? null : lookup.get(key);
    if (thing) out.push(row(thing, null, pill(t("box.removed"), "danger")));
  }
  return out;
}

// The review's price table: the estimate's breakdown, the saved bill, or what an extras-only
// change adds or takes off.
function reviewPrice(est, { invalid }) {
  const rows = [];
  if (est.status === "ok") {
    const p = est.preview;
    const cur = est.currency;
    if (p.sub_total != null) rows.push([ht("box.meals"), L.fmtPrice(p.sub_total, cur)]);
    if (p.shipping_amount != null) rows.push([ht("box.shipping"), L.fmtPrice(p.shipping_amount, cur)]);
    if (Number(p.discount_amount) > 0) rows.push([`${ht("money.discount")}${p.coupon_code ? ` (${esc(p.coupon_code)})` : ""}`, `−${L.fmtPrice(p.discount_amount, cur)}`]);
    if (Number(p.tax_amount) > 0) rows.push([ht("box.tax"), L.fmtPrice(p.tax_amount, cur)]);
    if (est.extras > 0) rows.push([ht("box.extras"), L.fmtPrice(est.extras, cur)]);
    rows.push([ht("box.estimated_total"), L.fmtPrice(est.amount, cur), true]);
  } else if (est.status === "saved") {
    rows.push([esc(est.label), L.fmtPrice(est.amount, est.currency), true]);
  } else if (est.status === "extras") {
    rows.push([ht("box.extras_change"), signedPrice(est.delta, est.currency), true]);
  } else if (est.status === "loading" || est.status === "pending") {
    rows.push([ht("box.estimated_total"), t("box.updating"), true]);
  } else if (est.status === "unavailable" && !invalid) {
    return `<div><div class="hf-eyebrow" style="margin-bottom:8px">${ht("box.price")}</div>
      <p class="hf-small hf-muted" style="margin:0">${ht("box.estimate_unavailable_note")}</p></div>`;
  }
  if (!rows.length) return "";
  return `<div><div class="hf-eyebrow" style="margin-bottom:8px">${ht("box.price")}</div><div class="hf-pricetable">${rows
    .map(([k, v, bold]) => `<div class="hf-prow${bold ? " total" : ""}"><span>${k}</span><span>${esc(v)}</span></div>`)
    .join("")}</div>${est.status === "ok" ? `<p class="hf-small hf-muted" style="margin:8px 0 0">${ht("box.estimate_note")}</p>` : ""}</div>`;
}

function renderReview(card, week) {
  const box = card.box;
  const meals = box.displayMeals(week);
  const mealRows = reviewRows(meals, box.savedMeals(week), new Map((week.recipes || []).map((r) => [L.selKey(r), r])), (r, qty, tag) => {
    const option = r.variation_title ? `${esc(r.variation_title)} · ` : "";
    const price = qty != null && r.price != null ? ` · ${esc(L.fmtPerServing(r.price, r.currency))}${ht("menu.per_serving")}` : "";
    const sub = qty == null ? `${option}${ht("box.removed")}` : `${option}${ht("meals.servings", { count: qty })}${price}`;
    return lineItem(L.resizedImage(r.image_url, 160), r.name, sub, tag, qty == null);
  });
  const market = box.displayMarket(week);
  const currency = L.marketCurrency(week);
  const extraRows = reviewRows(market, box.savedMarket(week), new Map((week.market_items || []).map((i) => [i.item_id, i])), (item, qty, tag) => {
    const price = qty != null && item.price != null ? ` · ${esc(L.fmtPrice(item.price * qty, item.currency || currency))}` : "";
    return lineItem(L.resizedImage(item.image_url, 160), item.name, qty == null ? ht("box.removed") : `${qty} ×${price}`, tag, qty == null);
  });

  const count = L.mealsCount(meals);
  const required = Number(week.meals_required) || 0;
  const dirty = box.dirty(week);
  const stale = dirty && L.isWeekEditable(week) && box.stale(week);
  const invalid = box.mealsDirty(week) && count < L.MIN_MEALS;
  const notes = [];
  if (stale) {
    notes.push(`<div class="hf-notice tone-warn" role="alert">${icon("mdi:sync-alert")}
      <div class="hf-noticebody"><strong>${ht("box.changed_title")}</strong> ${ht("box.changed_body", {
        keep_mine: strong(t("box.keep_mine")),
        use_saved: strong(t("box.use_saved")),
      })}</div></div>`);
  }
  if (invalid) {
    notes.push(`<div class="hf-notice tone-warn">${icon("mdi:alert-outline")}<div class="hf-noticebody">${ht("box.min_notice", { min: L.MIN_MEALS })}</div></div>`);
  } else if (required && count !== required && count > 0) {
    notes.push(`<div class="hf-notice tone-info">${icon("mdi:resize")}<div class="hf-noticebody">${ht("box.resized_notice", {
      meals: strong(t("meals.count", { count })),
      required,
    })}</div></div>`);
  }
  const busy = card.busy ? "disabled" : "";
  // A stale draft is settled first (Use saved / Keep mine); only then is there a box to save.
  const foot = !dirty
    ? `<button class="hf-btn" data-close-sheet>${ht("common.close")}</button>`
    : stale
      ? `<button class="hf-btn ghost danger" data-action="review-use-saved" data-focus-key="review-use-saved" ${busy}>${ht("box.use_saved")}</button><span class="hf-spacer"></span>
         <button class="hf-btn" data-close-sheet>${ht("common.close")}</button>
         <button class="hf-btn primary" data-action="review-keep-mine" data-focus-key="review-keep-mine" ${busy}>${ht("box.keep_mine")}</button>`
      : `<button class="hf-btn ghost danger" data-action="review-discard" data-focus-key="review-discard" ${busy}>${ht("box.discard_changes")}</button><span class="hf-spacer"></span>
         <button class="hf-btn" data-close-sheet>${ht("box.keep_editing")}</button>
         <button class="hf-btn primary" data-action="review-save" data-focus-key="review-save" ${card.busy || invalid ? "disabled" : ""}>${icon("mdi:content-save-outline")}${ht("box.save")}</button>`;
  const sub = [L.fmtLongDate(week.delivery_date), L.stateLabel(week), dirty ? t("box.unsaved_lower") : ""].filter(Boolean).join(" · ");
  return `
    ${UI.sheetHead(t("box.your_box"), sub)}
    <div class="hf-sheetbody">
      ${notes.join("")}
      <div><div class="hf-eyebrow" style="margin-bottom:4px">${ht("box.meals")} · ${count}</div>
        ${mealRows.length ? `<div class="hf-lineitems">${mealRows.join("")}</div>` : `<div class="hf-muted hf-small">${ht("overview.no_meals")}</div>`}</div>
      ${extraRows.length ? `<div><div class="hf-eyebrow" style="margin-bottom:4px">${ht("box.extras")} · ${UI.extrasCount(market)}</div><div class="hf-lineitems">${extraRows.join("")}</div></div>` : ""}
      ${reviewPrice(estimate(card, week), { invalid })}
    </div>
    <div class="hf-sheetfoot">${foot}</div>`;
}

function openReview(card, week) {
  card.box.schedulePreview(week);
  card.openSheet({
    kind: "review",
    label: t("box.your_box"),
    render: () => renderReview(card, card.weekById(week.week_id) || week),
    onClick: (_ev, el) => {
      if (!el) return;
      const current = card.weekById(week.week_id) || week;
      const action = el.getAttribute("data-action");
      if (action === "review-save") card.box.save(current);
      else if (action === "review-discard" || action === "review-use-saved") {
        card.box.discard(current);
        card.closeSheet();
        card.renderView();
      } else if (action === "review-keep-mine") {
        // Stay in the review: it now shows the box that would be saved.
        card.box.rebase(current);
        card.renderSheet();
        card.renderView();
        const sheet = card.shadowRoot && card.shadowRoot.querySelector(".hf-sheet");
        if (sheet) UI.focusKeyed(sheet, ["review-save", "review-discard"]);
      }
    },
  });
}
