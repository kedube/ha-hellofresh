/*
 * HelloFresh Market Card
 * ----------------------
 * A custom Lovelace card for browsing HelloFresh Market add-ons (appetizers, sides, desserts,
 * proteins, ...) for each delivery week and changing their selection/quantity.
 *
 * Like the meal-planner card, per-week market items are NOT exposed as entity attributes (they
 * would exceed Home Assistant's 16 KB recorder cap), so the data is read on demand from the
 * response-returning `hellofresh.get_weeks` service and rendered live. Selections are written
 * back via the `hellofresh.select_market_items` service.
 *
 * Config:
 *   type: custom:hellofresh-market-card
 *   config_entry_id: <optional>   # required only when multiple HelloFresh accounts exist
 *   title: HelloFresh Market      # optional card header
 *   image_width: 400              # optional Cloudinary resize width for item images
 *
 * No build step: hand-written ES2020 served from the integration's www/ directory.
 */

// The integration stamps its release version onto the resource URL as ?v= (cache-bust),
// so the banner reports exactly which build the browser actually loaded.
const MARKET_CARD_VERSION = new URL(import.meta.url).searchParams.get("v") || "unknown";

// Shared recipe-detail sheet, also used by the Meal planner and Recipes cards. Loaded via a
// dynamic import carrying this card's own ?v=: a static "./…js" specifier resolves to the bare
// filename, which Lovelace never stamps, so a browser holding an old copy would keep serving
// it after an upgrade while the card itself refreshed.
const detailModule = import(
  new URL(
    `./hellofresh-recipe-detail.js?v=${encodeURIComponent(MARKET_CARD_VERSION)}`,
    import.meta.url,
  ).href
);

// Shared card helpers (escaping, dates, money, week state, the cross-card sync protocol).
// AWAITED AT TOP LEVEL on purpose: these are called synchronously during the first render, and
// an un-awaited dynamic import is still a Promise at that point. The dynamic form (rather than a
// static specifier) is what carries this card's ?v= cache-bust onto the shared module.
const {
  esc,
  parseLocalDate,
  relativeWeek,
  fmtDate,
  titleCase,
  fmtPrice,
  isEditable,
  isPast,
  accountKey,
  syncStorageKey,
  loadSyncedWeekId,
  eventMatchesAccount,
  broadcastWeek,
  broadcastDataChanged,
  WEEK_SYNC_EVENT,
  resizedImage,
} = await import(
  new URL(
    `./hellofresh-shared.js?v=${encodeURIComponent(MARKET_CARD_VERSION)}`,
    import.meta.url,
  ).href
);


// Pretty labels for HelloFresh's internal group slugs.
const GROUP_LABELS = {
  appetizer: "Appetizers",
  breakfast: "Breakfast",
  dessert: "Desserts",
  lunch: "Lunch",
  protein: "Proteins",
  sides: "Sides",
  goodchop: "GoodChop",
  petstable: "The Pets Table",
  donation: "Donations",
  lowprices: "Low Prices",
  // Internal slug for the add-on pool the meal-modularity flows draw from (dips, sides,
  // dessert cups). Real purchasable items, but "Modularity" is meaningless to a person.
  modularity: "Extras",
};

class HelloFreshMarketCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    // Parse the stylesheet ONCE and share across instances instead of re-injecting <style> on
    // every render.
    this.shadowRoot.adoptedStyleSheets = [HelloFreshMarketCard._sheet()];
    this._weeks = null;
    this._cursor = 0;
    this._loading = false;
    this._error = null;
    this._busy = false;
    this._saving = null; // persistent "please wait" banner text while a save + reload is in flight
    this._fetched = false;
    this._hass = null;
    // Pending (unsaved) edit per week: week_id -> Map(item_id -> quantity). Absent = show saved.
    this._pending = {};
    // Week id whose last save triggered a HelloFresh seamless downgrade (box silently shrunk to
    // fit). Drives a persistent, dismissable banner — the "your box was downsized" warning
    // shouldn't vanish before it's read. Cleared on dismiss or when the user changes weeks.
    this._downgradeNotice = null;
    // Persisted "show selected only" view preference (shared across weeks, survives reloads).
    this._showSelectedOnly = this._loadShowSelectedOnly();
    // Persisted Market-section filter (Appetizers / Breakfast / Desserts / …), a view
    // preference like the planner card's protein filter: empty set = show every section.
    this._sectionFilter = this._loadSectionFilter();
    // Cross-card week sync: follow a sibling HelloFresh card (e.g. the Meal Planner) to the same
    // week. If the event arrives before this card has loaded, remember the id and apply on fetch.
    this._syncPendingWeekId = null;
    this._onSyncWeek = (ev) => this._receiveSyncedWeek(ev);
  }

  connectedCallback() {
    window.addEventListener(HelloFreshMarketCard.WEEK_SYNC_EVENT, this._onSyncWeek);
    // Switching back to this card's dashboard tab re-connects the (already-loaded) element
    // without re-fetching, so pick up any week another card selected while we were hidden.
    if (this._weeks) this._applySyncedWeekId(this._loadSyncedWeekId());
  }

  disconnectedCallback() {
    window.removeEventListener(HelloFreshMarketCard.WEEK_SYNC_EVENT, this._onSyncWeek);
    // Drop the pending toast timer so a detached card isn't kept alive (holding the whole
    // weeks payload) only to re-render into a shadow root nobody can see.
    clearTimeout(this._toastTimer);
    // Also drop the toast itself: with the timer cancelled, a toast shown just before
    // disconnect would otherwise repaint forever once the card reconnects.
    this._toast = null;
    // The recipe sheet lives beside the card in the shadow root and registers a document-level
    // Escape handler, so it must be torn down explicitly.
    if (this._detailOverlay) this._detailOverlay.close();
  }

  setConfig(config) {
    this._config = { title: "HelloFresh Market", image_width: 400, ...config };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (hass && !this._fetched && !this._loading) {
      this._fetched = true;
      this._fetchWeeks();
    } else if (this._weeks) {
      // hass updates fire when a dashboard view becomes active. Some HA versions keep hidden
      // views in the DOM (no re-connect/re-fetch), so this is the reliable moment to pick up a
      // week another HelloFresh card selected while this one was hidden. Cheap: no service call.
      this._applySyncedWeekId(this._loadSyncedWeekId());
    }
  }

  getCardSize() {
    return 8;
  }

  static getStubConfig() {
    return { type: "custom:hellofresh-market-card" };
  }

  static get MAX_QUANTITY() {
    return 12;
  }

  static get FILTER_STORAGE_KEY() {
    return "hellofresh-market:show-selected-only";
  }

  _loadShowSelectedOnly() {
    try {
      return window.localStorage.getItem(HelloFreshMarketCard.FILTER_STORAGE_KEY) === "1";
    } catch (_e) {
      return false;
    }
  }

  _toggleShowSelectedOnly() {
    this._showSelectedOnly = !this._showSelectedOnly;
    try {
      window.localStorage.setItem(
        HelloFreshMarketCard.FILTER_STORAGE_KEY,
        this._showSelectedOnly ? "1" : "0"
      );
    } catch (_e) {
      /* storage unavailable */
    }
    this._render();
  }

  static get SECTION_STORAGE_KEY() {
    return "hellofresh-market:section-filter";
  }

  // Load the persisted section filter as a Set of group_type slugs. Unlike the planner's
  // protein filter there is no fixed allowlist to validate against — the sections are whatever
  // HelloFresh's catalog serves (GROUP_LABELS is labels, not an exhaustive list) — so stored
  // slugs are kept as-is and reconciled against the viewed week in _activeSectionFilter.
  _loadSectionFilter() {
    try {
      const raw = window.localStorage.getItem(HelloFreshMarketCard.SECTION_STORAGE_KEY);
      return new Set((raw ? JSON.parse(raw) : []).filter((s) => typeof s === "string"));
    } catch (_e) {
      return new Set(); // empty = show all sections
    }
  }

  // Toggle one section in/out of the filter set, persist, and re-render.
  _toggleSectionFilter(slug) {
    if (!slug) return;
    if (this._sectionFilter.has(slug)) this._sectionFilter.delete(slug);
    else this._sectionFilter.add(slug);
    try {
      window.localStorage.setItem(
        HelloFreshMarketCard.SECTION_STORAGE_KEY,
        JSON.stringify([...this._sectionFilter])
      );
    } catch (_e) {
      /* storage unavailable: keep the in-memory setting for this session */
    }
    this._render();
  }

  // Clear all section filters (the "All" chip), persist, and re-render.
  _clearSectionFilter() {
    if (this._sectionFilter.size === 0) return;
    this._sectionFilter.clear();
    try {
      window.localStorage.setItem(HelloFreshMarketCard.SECTION_STORAGE_KEY, "[]");
    } catch (_e) {
      /* storage unavailable */
    }
    this._render();
  }

  // The section slugs present in a week's catalog, in order of first appearance. Only the
  // browsable catalog carries group_type (see _renderGroups); a history-sourced week returns
  // an empty list, which also suppresses the filter bar there.
  _sectionsFor(week) {
    const seen = new Set();
    const out = [];
    for (const item of (week && week.market_items) || []) {
      if (item.group_type && !seen.has(item.group_type)) {
        seen.add(item.group_type);
        out.push(item.group_type);
      }
    }
    return out;
  }

  // Whether the section filter applies to this week: only the browsable catalog of a
  // current/future week, and not while "show selected only" is on (mirrors the planner).
  _filtersApply(week) {
    return Boolean(week) && !this._isPast(week) && !this._showSelectedOnly;
  }

  // The stored filter reconciled against the viewed week: only slugs that week actually has.
  // A slug persisted from another week (or a season HelloFresh has since dropped) must not
  // invisibly blank a week that never carried that section — with no matching chip rendered,
  // there would be nothing to tap to understand why everything vanished.
  _activeSectionFilter(week) {
    const present = new Set(this._sectionsFor(week));
    return new Set([...this._sectionFilter].filter((s) => present.has(s)));
  }

  // `keepWeekId`: after a write (e.g. save), stay on the week the user was on rather than
  // snapping back to the first week. Falls back to the first week when that week is gone.
  async _fetchWeeks(keepWeekId = null) {
    // Reentry guard: a refresh tap racing the post-save resync would otherwise run two
    // concurrent get_weeks calls, and the slower response could clobber the newer state.
    if (!this._hass || this._loading) return;
    this._loading = true;
    this._error = null;
    this._render();
    try {
      const data = {};
      if (this._config.config_entry_id) data.config_entry_id = this._config.config_entry_id;
      const result = await this._hass.callService("hellofresh", "get_weeks", data, undefined, false, true);
      const response = (result && result.response) || {};
      // Only weeks that actually carry a market catalog are browsable here.
      this._weeks = this._browsableWeeks(response.weeks || []);
      this._pending = {};
      // Land on, in priority order: an explicit keep (post-save), else a week a sibling card
      // asked us to sync to before we'd loaded, else the week another HelloFresh card last
      // selected (shared storage — cross-tab sync), else the CURRENT week by date (so both cards
      // start on the same week), else the first week.
      const syncId = this._syncPendingWeekId ?? this._loadSyncedWeekId();
      this._syncPendingWeekId = null;
      let landing = keepWeekId != null
        ? this._weeks.findIndex((w) => w.week_id === keepWeekId)
        : -1;
      if (landing < 0 && syncId != null) {
        landing = this._weeks.findIndex((w) => w.week_id === syncId);
      }
      if (landing < 0) landing = this._currentWeekIndex(); // -1 if no week is dated
      this._cursor = landing >= 0 ? landing : 0;
    } catch (err) {
      this._error = (err && err.message) || String(err);
    } finally {
      this._loading = false;
      this._render();
    }
  }

  // Trim the week list to what's actually browsable in the Market.
  //
  // HelloFresh schedules deliveries further out than it publishes the Market catalog, so the
  // deliveries feed includes future weeks with no market items yet. Show exactly the weeks that
  // have a catalog and no further — if HelloFresh has published 5 future weeks, show 5; if 7, show
  // 7 — without any fixed cap. So we walk the weeks in chronological order and, once we reach a
  // FUTURE week with no market data, stop including everything from there on. Past and current
  // weeks are ALWAYS kept, exactly as the meal-planner card does. A past week where nothing was
  // ordered is a real answer ("no Market items this week"), not a reason to hide the week — and
  // hiding it made the strip skip unpredictably from one delivery to another, so the Market and
  // My Menu cards disagreed about which weeks exist.
  _browsableWeeks(weeks) {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const ordered = (weeks || [])
      .slice()
      .sort((a, b) => this._weekSortKey(a) - this._weekSortKey(b));
    const result = [];
    let futureMarketEnded = false;
    for (const week of ordered) {
      const isFuture = week.delivery_date
        ? this._parseLocalDate(week.delivery_date).getTime() >= today.getTime()
        : true; // undated weeks are treated as future (can't anchor them to the past)
      const hasMarket = (week.market_items || []).length > 0;
      if (!isFuture) {
        result.push(week); // past/current week: always browsable history
        continue;
      }
      // Future week: include only while the catalog is still being published contiguously.
      if (futureMarketEnded || !hasMarket) {
        futureMarketEnded = true;
        continue;
      }
      result.push(week);
    }
    return result;
  }

  // Parse a date anchored to LOCAL midnight. A bare "YYYY-MM-DD" (how the integration
  // serializes delivery dates) parses as UTC midnight per the JS spec, which reads as the
  // PREVIOUS day anywhere west of UTC — a Monday delivery rendered as Sunday. Full datetime
  // strings (selection deadlines) parse normally.
  _parseLocalDate(value) {
    return parseLocalDate(value);
  }

  // Chronological sort key for a week (undated weeks sink to the end so they don't gate earlier
  // future weeks in _browsableWeeks).
  _weekSortKey(week) {
    const ms = week && week.delivery_date ? this._parseLocalDate(week.delivery_date).getTime() : NaN;
    return Number.isNaN(ms) ? Number.POSITIVE_INFINITY : ms;
  }

  // Move the cursor to an index, render, and (unless syncing FROM another card) broadcast the
  // resulting week so a sibling HelloFresh card on the same dashboard follows to the same week.
  _setCursor(index, { broadcast = true } = {}) {
    if (!this._weeks || this._weeks.length === 0) return;
    const clamped = Math.max(0, Math.min(index, this._weeks.length - 1));
    if (clamped === this._cursor) return;
    this._cursor = clamped;
    this._downgradeNotice = null; // a downgrade warning is about the week you just saved
    this._render();
    if (broadcast) this._broadcastWeek();
  }

  _step(delta) {
    if (!this._weeks || this._weeks.length === 0) return;
    const count = this._weeks.length;
    this._setCursor((this._cursor + delta + count) % count);
  }

  // Index of the "current" week by date: the week whose delivery_date is nearest to today,
  // breaking ties toward the upcoming delivery rather than a past one. Returns -1 if no week
  // carries a delivery_date.
  _currentWeekIndex() {
    if (!this._weeks || this._weeks.length === 0) return -1;
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    let best = -1;
    let bestDays = Infinity;
    this._weeks.forEach((w, i) => {
      if (!w.delivery_date) return;
      const d = this._parseLocalDate(w.delivery_date);
      d.setHours(0, 0, 0, 0);
      const days = Math.round((d - today) / 86400000);
      const dist = Math.abs(days);
      // Closer week wins; on a tie, prefer the one in the future (days >= 0).
      if (dist < bestDays || (dist === bestDays && days >= 0)) {
        best = i;
        bestDays = dist;
      }
    });
    return best;
  }

  // Jump the cursor to the current week by date (the "Current Week" button).
  _gotoCurrentWeek() {
    const target = this._currentWeekIndex();
    if (target >= 0) this._setCursor(target);
  }

  // ---- cross-card week sync ------------------------------------------------
  //
  // Shared with the meal-planner card. The two cards can live on DIFFERENT dashboard views
  // (tabs), so a live event alone is not enough — the other card may be unmounted when you
  // navigate. The selected week is PERSISTED in localStorage (keyed by account) and restored on
  // load; the live event is an optimization for when both cards are mounted on the same view.
  // Event name, storage key and accountKey scheme are defined once in hellofresh-shared.js —
  // their whole correctness condition is that every card agrees, so none of them is spelled
  // literally here any more.

  static get WEEK_SYNC_EVENT() {
    return WEEK_SYNC_EVENT;
  }

  _accountKey() {
    return accountKey(this._config);
  }

  _syncStorageKey() {
    return syncStorageKey(this._config);
  }

  _loadSyncedWeekId() {
    return loadSyncedWeekId(this._config);
  }

  // Announce + persist the currently displayed week so a sibling card follows — now (live event)
  // and later (storage, when it next mounts / you switch to its tab).
  _broadcastWeek() {
    const week = this._weeks ? this._weeks[this._cursor] : null;
    if (!week) return;
    broadcastWeek(this._config, week.week_id);
  }

  // Tell read-only sibling cards (the schedule card) that a write changed account data, so
  // they re-pull immediately instead of waiting for their next interval refresh.
  _broadcastDataChanged() {
    broadcastDataChanged(this._config);
  }

  // Move to a synced week id if this card carries it. Returns true if it handled the id.
  _applySyncedWeekId(weekId) {
    if (!weekId || !this._weeks) return false;
    const current = this._weeks[this._cursor];
    if (current && current.week_id === weekId) return true; // already there
    const target = this._weeks.findIndex((w) => w.week_id === weekId);
    if (target >= 0) {
      this._setCursor(target, { broadcast: false }); // don't echo into a loop
      return true;
    }
    return false; // this week isn't browsable in this card
  }

  // Follow a sibling card's live week change. Ignore our own broadcasts and other accounts.
  _receiveSyncedWeek(ev) {
    const detail = (ev && ev.detail) || {};
    if (!detail.weekId) return;
    if ((detail.accountKey || "default") !== this._accountKey()) return;
    if (!this._weeks) {
      this._syncPendingWeekId = detail.weekId; // not loaded yet — apply on fetch
      return;
    }
    this._applySyncedWeekId(detail.weekId);
  }

  // Whether HelloFresh will still accept a change to this week. Must match the meal-planner and
  // schedule cards and `HelloFreshWeek.is_editable` in models.py — that docstring states all
  // three are kept in lockstep.
  //
  // This copy used to omit the `allowed_actions.mealSwap` check and `return true` by default, so
  // a week HelloFresh had LOCKED (mealSwap false) with no recorded deadline — and a delivered
  // week, for the same reason — was labelled "Editable" here, kept its ± steppers live, and let
  // the user build a cart that the server then rejected. Absent allowed_actions now reads as
  // locked (the safe direction) rather than editable.
  _isEditable(week) {
    return isEditable(week);
  }

  // A week whose delivery date is before today (undated weeks are not treated as past). Mirrors
  // the meal-planner card. For a past week the market catalog is history, so only the items that
  // were actually selected/ordered are shown — never the full browsable catalog.
  _isPast(week) {
    return isPast(week);
  }

  // Whether the user can change the cart right now: the week must be editable AND the grid must
  // be in "show all items" mode. In "show selected only" mode the unselected items are hidden,
  // so quantity edits there are disabled to avoid editing against a partial view.
  _canEdit(week) {
    return this._isEditable(week) && !this._showSelectedOnly;
  }

  // ---- selection state -----------------------------------------------------

  _itemQuantity(item) {
    const q = Number(item.selected_quantity);
    if (Number.isFinite(q) && q > 0) return q;
    return item.is_selected === true ? 1 : 0;
  }

  // Saved selection as Map(item_id -> quantity) for chosen items.
  _savedSelection(week) {
    const map = new Map();
    for (const item of week.market_items || []) {
      const q = this._itemQuantity(item);
      if (q > 0) map.set(item.item_id, q);
    }
    return map;
  }

  _displaySelection(week) {
    return this._pending[week.week_id] || this._savedSelection(week);
  }

  _ensurePending(week) {
    let pending = this._pending[week.week_id];
    if (!pending) {
      pending = new Map(this._savedSelection(week));
      this._pending[week.week_id] = pending;
    }
    return pending;
  }

  _isDirty(week) {
    const pending = this._pending[week.week_id];
    if (!pending) return false;
    const saved = this._savedSelection(week);
    if (pending.size !== saved.size) return true;
    for (const [id, qty] of pending) if (saved.get(id) !== qty) return true;
    return false;
  }

  _cartTotalCents(week) {
    const display = this._displaySelection(week);
    let cents = 0;
    for (const item of week.market_items || []) {
      const qty = display.get(item.item_id) || 0;
      if (qty > 0 && item.price_cents != null) cents += item.price_cents * qty;
    }
    return cents;
  }

  // +/- stepper: change an item's quantity, clamped to [0, min(maxQuantity, MAX_QUANTITY)].
  _changeQuantity(week, item, delta) {
    if (this._busy || !this._canEdit(week)) return;
    const pending = this._ensurePending(week);
    const cap = Math.min(
      item.max_quantity != null ? item.max_quantity : HelloFreshMarketCard.MAX_QUANTITY,
      HelloFreshMarketCard.MAX_QUANTITY
    );
    const current = pending.get(item.item_id) || 0;
    const next = Math.max(0, Math.min(cap, current + delta));
    if (next === current) return;
    if (next === 0) pending.delete(item.item_id);
    else pending.set(item.item_id, next);
    this._renderQuantityChange(week, item);
  }

  // Quantity edits are the hot path. With the "selected only" filter OFF and no section filter
  // active, a step can't change which tiles are visible, so update just the affected tile and
  // the status row in place. With either filter ON, visibility can change (a filtered-out
  // section's item is only visible via the selected-item bypass, which ends at qty 0), so fall
  // back to a full render.
  _renderQuantityChange(week, item) {
    if (this._showSelectedOnly || this._activeSectionFilter(week).size > 0 || !this._shell) {
      this._render();
      return;
    }
    const idAttr = item.item_id;
    const tile = this._shell.body.querySelector(`.item[data-id="${CSS.escape(idAttr)}"]`);
    if (tile) {
      const editable = this._canEdit(week);
      const qty = this._displaySelection(week).get(item.item_id) || 0;
      // Same fallback the full render passes — omitting it made an item without its own
      // currency lose its price the moment its quantity changed in place.
      const frag = this._fragment(
        this._renderTile(week, item, editable, qty, week?.order?.currency)
      );
      if (frag.firstElementChild) tile.replaceWith(frag.firstElementChild);
    }
    this._shell.body.querySelector(".statusrow")?.replaceWith(
      this._fragment(this._renderStatusRow(week))
    );
  }

  _fragment(html) {
    const tpl = document.createElement("template");
    tpl.innerHTML = html;
    return tpl.content;
  }

  _cancelEdit(week) {
    delete this._pending[week.week_id];
    this._render();
  }

  async _saveSelection(week) {
    const pending = this._pending[week.week_id];
    if (!pending) return;
    // Send the full desired state (item_id -> quantity); omitted/zero items are removed.
    const quantities = {};
    for (const [id, qty] of pending) if (qty > 0) quantities[id] = qty;

    this._busy = true;
    this._saving = "Please wait while saving selections…"; // persistent banner until reload completes
    this._render();
    // Yield a paint frame so the banner is actually drawn before the (possibly fast-resolving)
    // service call and reload run — otherwise the microtask after `await` can pre-empt the paint
    // and the banner appears not to show until the page refreshes.
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    try {
      const data = { week_id: week.week_id, quantities };
      if (this._config.config_entry_id) data.config_entry_id = this._config.config_entry_id;
      // return_response=true so we can see HelloFresh's seamless-downgrade flag: the write may
      // succeed but silently shrink the box to fit, saving fewer items than requested.
      const result = await this._hass.callService(
        "hellofresh", "select_market_items", data, undefined, false, true
      );
      const downgraded = !!((result && result.response) || {}).downgraded;
      delete this._pending[week.week_id];
      this._busy = false;
      this._broadcastDataChanged();
      await this._fetchWeeks(week.week_id); // resync, staying on the week we just saved
      if (downgraded) this._noteDowngrade(week.week_id);
      else this._flash("Market selection updated.");
    } catch (err) {
      this._busy = false;
      // Keep the unsaved quantities across the resync (which wipes _pending) so a failed
      // save can just be retried instead of forcing the user to re-pick everything.
      const unsaved = this._pending[week.week_id];
      await this._fetchWeeks(week.week_id); // resync from the source of truth
      if (unsaved) this._pending[week.week_id] = unsaved;
      this._flash(`Selection failed: ${(err && err.message) || err}`, true);
    } finally {
      this._saving = null;
      this._render();
    }
  }

  _flash(message, isError = false) {
    this._toast = { message, isError };
    this._render();
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => {
      this._toast = null;
      this._render();
    }, 4000);
  }

  // Record that HelloFresh downsized the box on the last save for `weekId`, showing a
  // persistent banner (rendered by `_renderDowngradeNotice`) until dismissed or the user
  // navigates to another week.
  _noteDowngrade(weekId) {
    this._downgradeNotice = weekId;
    this._render();
  }

  _dismissDowngrade() {
    if (this._downgradeNotice === null) return;
    this._downgradeNotice = null;
    this._render();
  }

  // ---- render --------------------------------------------------------------

  // Build the stable shell once (ha-card + a single delegated listener), then on each render
  // only replace the inner regions' HTML — no CSS re-parse, no listener re-attach.
  _ensureShell() {
    if (this._shell) return;
    const card = document.createElement("ha-card");
    card.innerHTML = `
      <div class="head"></div>
      <div class="js-body"></div>
      <div class="js-toast"></div>`;
    this.shadowRoot.appendChild(card);
    this._shell = {
      head: card.querySelector(".head"),
      body: card.querySelector(".js-body"),
      toast: card.querySelector(".js-toast"),
    };
    this._bindDelegated(card);
  }

  _render() {
    if (!this.shadowRoot) return;
    this._ensureShell();
    const week = this._weeks ? this._weeks[this._cursor] : null;
    this._shell.head.innerHTML = `
      ${this._renderLogo()}
      <span class="title-text">${this._esc(this._config.title)}</span>`;
    this._shell.body.innerHTML = this._renderBody(week);
    // A save in flight shows a persistent "please wait" banner (with a spinner) until the
    // reload completes; a transient toast otherwise. The saving banner takes precedence.
    this._shell.toast.innerHTML = this._saving
      ? `<div class="toast saving"><span class="spinner"></span>${this._esc(this._saving)}</div>`
      : this._toast
        ? `<div class="toast ${this._toast.isError ? "error" : ""}">${this._esc(this._toast.message)}</div>`
        : "";
  }

  // Optional header logo. `logo: true` uses the bundled HelloFresh logo served by the
  // integration; `logo: <url>` uses a custom image. Matches the meal-planner card convention.
  _renderLogo() {
    const logo = this._config.logo;
    if (!logo) return "";
    const logoUrl = logo === true ? "/hellofresh/hellofresh-logo.png" : logo;
    return `<img class="logo" src="${this._esc(logoUrl)}" alt="HelloFresh">`;
  }

  _renderBody(week) {
    if (this._loading) return `<div class="state">Loading market…</div>`;
    if (this._error) {
      return `<div class="state error">Could not load market: ${this._esc(this._error)}</div>
        <div class="actions"><button data-action="refresh">Retry</button></div>`;
    }
    if (!this._weeks || this._weeks.length === 0) {
      return `<div class="state">No market items available for any week yet.</div>
        <div class="actions"><button data-action="refresh">Refresh</button></div>`;
    }
    return `${this._renderHeader(week)}${this._renderDowngradeNotice(week)}${this._renderSectionBar(week)}${this._renderGroups(week)}`;
  }

  // The Market-section filter bar (Appetizers / Breakfast / Desserts / …), mirroring the
  // planner card's protein bar. Chips come from the sections the viewed week actually has —
  // there is no fixed section list, HelloFresh's catalog is the authority — so the bar adapts
  // per week and disappears on history-sourced weeks (no group_type) or single-section weeks
  // (nothing to narrow).
  _renderSectionBar(week) {
    if (!this._filtersApply(week)) return "";
    const sections = this._sectionsFor(week);
    if (sections.length < 2) return "";
    const allActive = this._activeSectionFilter(week).size === 0;
    const chips = sections.map((slug) => {
      const on = this._sectionFilter.has(slug);
      const label = GROUP_LABELS[slug] || this._titleCase(slug);
      return `<button
          class="fbtn ${on ? "on" : ""}"
          data-action="filter-section"
          data-section="${this._esc(slug)}"
          aria-pressed="${on ? "true" : "false"}"
        >${this._esc(label)}</button>`;
    }).join("");
    return `
      <div class="filterbar">
        <div class="fgroup">
          <span class="flabel">Categories</span>
          <button
            class="fbtn ${allActive ? "on" : ""}"
            data-action="filter-section-all"
            aria-pressed="${allActive ? "true" : "false"}"
          >All</button>
          ${chips}
        </div>
      </div>
    `;
  }

  // Inline banner shown on the week whose last save triggered a HelloFresh seamless downgrade:
  // the write succeeded but the box was silently shrunk to fit, so fewer items were saved than
  // chosen. Surfaced right where the edit happened (the persistent HA notification is easy to
  // miss). Dismissable; also cleared automatically when the user navigates to another week.
  _renderDowngradeNotice(week) {
    if (!week || this._downgradeNotice !== week.week_id) return "";
    return `
      <div class="downgrade-notice" role="alert">
        <span class="downgrade-icon" aria-hidden="true">⚠</span>
        <span class="downgrade-text">HelloFresh <strong>downsized this box</strong> to fit your
          plan — fewer items were saved than you selected. Review the saved selection below.</span>
        <button class="downgrade-dismiss" data-action="dismiss-downgrade" aria-label="Dismiss">✕</button>
      </div>`;
  }

  _renderHeader(week) {
    const rel = this._relativeWeek(week);
    return `
      <div class="header">
        <button class="nav" data-action="prev" aria-label="Previous week">‹</button>
        <div class="weekinfo">
          <div class="weektitle">${this._esc(week.display_name || week.week_id)}</div>
          <div class="weeksub">
            ${week.delivery_date ? this._esc(this._fmtDate(week.delivery_date)) : ""}
            ${rel ? ` · ${this._esc(rel)}` : ""}
            ${week.is_skipped ? ` · <span class="skipped">Skipped</span>` : ""}
          </div>
          ${this._renderCurrentWeek()}
        </div>
        <button class="nav" data-action="next" aria-label="Next week">›</button>
      </div>
      ${this._renderStatusRow(week)}
    `;
  }

  // A "Current Week" button that jumps the cursor to the week matching today's date. Lives
  // inside .weekinfo so the nav arrows stay centered on the full block. When the cursor is
  // already on the current week (or no week has a date) we render an empty placeholder of the
  // same height so the header doesn't change size and the layout doesn't bounce.
  _renderCurrentWeek() {
    const target = this._currentWeekIndex();
    if (target < 0 || target === this._cursor) {
      return `<div class="currentweekslot"></div>`;
    }
    return `
      <div class="currentweekslot">
        <button class="currentweekbtn" data-action="goto-current">Current Week</button>
      </div>`;
  }

  // Reflects live cart total / dirty state, so it's re-rendered on its own during an in-place
  // quantity edit (see _renderQuantityChange) without rebuilding the whole header.
  _renderStatusRow(week) {
    const editable = this._isEditable(week);
    const dirty = this._isDirty(week);
    const deadline = week.selection_deadline ? new Date(week.selection_deadline) : null;
    const totalCents = this._cartTotalCents(week);
    const currency = (week.market_items.find((i) => i.currency) || week.order || {}).currency;
    return `
      <div class="statusrow">
        <span class="chip ${totalCents > 0 ? "ok" : ""}">
          Market total ${this._esc(this._fmtPrice(totalCents / 100, currency))}${dirty ? " · unsaved" : ""}
        </span>
        ${this._isPast(week)
          ? "" // Past weeks always show only what was ordered, so the toggle is meaningless.
          : `<button
          class="chip filterchip"
          data-action="toggle-filter"
          title="${this._showSelectedOnly ? "Show all market items" : "Show only selected market items"}"
        >${this._showSelectedOnly ? "Show all items" : "Show selected only"}</button>`}
        ${deadline ? `<span class="chip">Deadline ${this._esc(this._fmtDateTime(deadline))}</span>` : ""}
        <span class="chip ${editable ? "editable" : "locked"}">${editable ? "Editable" : "Locked"}</span>
        ${editable && this._showSelectedOnly
          ? `<span class="hint">Switch to “Show all items” to change your cart.</span>`
          : ""}
        ${dirty
          ? `<button class="savebtn" data-action="save" ${this._busy ? "disabled" : ""}>Save selection</button>
             <button class="skipbtn" data-action="cancel" ${this._busy ? "disabled" : ""}>Cancel</button>`
          : ""}
        <button class="skipbtn" data-action="refresh" ${this._busy ? "disabled" : ""}>↻</button>
      </div>
    `;
  }

  _renderGroups(week) {
    const editable = this._canEdit(week);
    const display = this._displaySelection(week);
    const qtyOf = (item) => display.get(item.item_id) || 0;

    // A past week's catalog is history: show only what was selected/ordered, never the full
    // browsable catalog (mirrors the meal-planner card). Otherwise honor the "show selected
    // only" toggle for current/future weeks.
    const isPast = this._isPast(week);
    const selectedOnly = isPast || this._showSelectedOnly;

    let items = week.market_items || [];
    if (selectedOnly) items = items.filter((i) => qtyOf(i) > 0);
    if (items.length === 0) {
      // The "…yet" phrasing implies you can still add items, so it only fits an editable week.
      // A locked/past week is final — say so plainly instead.
      const noneSelected = this._isEditable(week)
        ? "No market items selected for this week yet."
        : "No market items selected.";
      return `<div class="state">${selectedOnly ? noneSelected : "No market items for this week."}</div>`;
    }

    // Section filter (current & future weeks only). Selected items always pass so an active
    // filter never hides something already in your cart — same rule as the planner's meals.
    const activeSections = this._filtersApply(week) ? this._activeSectionFilter(week) : new Set();
    if (activeSections.size > 0) {
      const filtered = items.filter((i) => qtyOf(i) > 0 || activeSections.has(i.group_type));
      if (filtered.length === 0) {
        return `<div class="state">No market items match the current filter. Adjust the filters above to see more.</div>`;
      }
      items = filtered;
    }

    // A market item's own `currency` is often null; fall back to the week's order currency so a
    // non-USD account doesn't get every tile silently priced with a "$" by _fmtPrice's default.
    const fallbackCurrency = week?.order?.currency;

    // Group by group_type, preserving the catalog order of first appearance.
    //
    // Only the browsable catalog carries group_type, and HelloFresh serves that for roughly the
    // menu-grace window. Older weeks are reconstructed from delivered history, which records
    // what was bought but not which Market shelf it came from. Rather than invent a heading (or
    // bucket everything under a meaningless "Other"), those weeks render as a single ungrouped
    // list in delivery order — matching how My Menu presents an old week's meals.
    const order = [];
    const byGroup = new Map();
    for (const item of items) {
      const key = item.group_type || "";
      if (!byGroup.has(key)) {
        byGroup.set(key, []);
        order.push(key);
      }
      byGroup.get(key).push(item);
    }

    return order
      .map((key) => {
        const tiles = byGroup
          .get(key)
          .map((item) => this._renderTile(week, item, editable, qtyOf(item), fallbackCurrency))
          .join("");
        // No group_type (history-sourced): plain list, no heading.
        if (!key) {
          return `<div class="group">
            <div class="grid ${this._busy ? "busy" : ""}">${tiles}</div>
          </div>`;
        }
        const label = GROUP_LABELS[key] || this._titleCase(key);
        return `<div class="group">
          <div class="grouptitle">${this._esc(label)}</div>
          <div class="grid ${this._busy ? "busy" : ""}">${tiles}</div>
        </div>`;
      })
      .join("");
  }

  _renderTile(week, item, editable, qty, fallbackCurrency) {
    const idAttr = this._esc(item.item_id);
    const cap = Math.min(
      item.max_quantity != null ? item.max_quantity : HelloFreshMarketCard.MAX_QUANTITY,
      HelloFreshMarketCard.MAX_QUANTITY
    );
    const stats = [];
    if (item.calories_kcal != null) stats.push(`${Math.round(item.calories_kcal)} kcal`);
    // Sold-out items can't be added (but a still-selected sold-out item can be reduced/removed).
    const soldOut = item.is_sold_out === true;
    return `
      <div class="item ${qty > 0 ? "selected" : ""} ${soldOut ? "soldout" : ""}" data-id="${idAttr}"
           role="button" tabindex="0" aria-label="Open recipe: ${this._esc(item.name)}">
        <div class="imgwrap">
          ${item.image_url
            ? `<img loading="lazy" src="${this._esc(resizedImage(item.image_url, this._config.image_width))}" alt="${this._esc(item.name)}">`
            : `<div class="noimg"></div>`}
          ${qty > 0 ? `<div class="qtybadge">${qty}×</div>` : ""}
          ${soldOut ? `<div class="soldoutflag">Sold out</div>` : ""}
          ${item.price != null ? `<div class="price">${this._esc(this._fmtPrice(item.price, item.currency || fallbackCurrency))}</div>` : ""}
        </div>
        <div class="meta">
          <div class="name">${this._esc(item.name)}</div>
          ${item.description ? `<div class="desc">${this._esc(item.description)}</div>` : ""}
          ${stats.length ? `<div class="cals">${this._esc(stats.join(" · "))}</div>` : ""}
          ${editable
            ? `<div class="metafoot"><div class="stepper">
                 <button class="qbtn" data-qty="dec" data-id="${idAttr}" ${qty <= 0 ? "disabled" : ""} title="Fewer">−</button>
                 <span class="qval">${qty}</span>
                 <button class="qbtn" data-qty="inc" data-id="${idAttr}" ${qty >= cap || soldOut ? "disabled" : ""} title="More">+</button>
               </div></div>`
            : qty > 0 ? `<div class="metafoot"><div class="cals">${qty} selected</div></div>` : ""}
        </div>
      </div>`;
  }

  // A SINGLE delegated click listener on the card, attached once.
  _bindDelegated(card) {
    // Tiles are focusable (role="button" tabindex="0") because the tile tap is the only way
    // to open an item's recipe. Enter/Space on a FOCUSED TILE opens it; the guard skips
    // events bubbling from the real ± <button>s, whose Enter/Space already fires a click.
    card.addEventListener("keydown", (ev) => {
      if (ev.key !== "Enter" && ev.key !== " ") return;
      const tile = ev.target.closest(".item");
      if (!tile || ev.target.closest("button")) return;
      ev.preventDefault(); // Space must open the recipe, not scroll the page
      const week = this._weeks ? this._weeks[this._cursor] : null;
      const item = week && (week.market_items || []).find(
        (i) => i.item_id === tile.getAttribute("data-id")
      );
      if (item) this._openRecipeDetail(item);
    });
    card.addEventListener("click", (ev) => {
      const week = this._weeks ? this._weeks[this._cursor] : null;

      const qbtn = ev.target.closest(".qbtn");
      if (qbtn && !qbtn.disabled) {
        ev.stopPropagation();
        const item = week && (week.market_items || []).find(
          (i) => i.item_id === qbtn.getAttribute("data-id")
        );
        if (item) this._changeQuantity(week, item, qbtn.getAttribute("data-qty") === "inc" ? 1 : -1);
        return;
      }

      // Tapping the tile opens the full recipe. Market items are chosen with the ± steppers
      // (handled above, which stop propagation), so the tile itself is free for this on every
      // week — editable or not.
      const tile = ev.target.closest(".item");
      if (tile) {
        const item = week && (week.market_items || []).find(
          (i) => i.item_id === tile.getAttribute("data-id")
        );
        if (item) this._openRecipeDetail(item);
        return;
      }

      const actionEl = ev.target.closest("[data-action]");
      if (!actionEl) return;
      const action = actionEl.getAttribute("data-action");
      if (action === "prev") this._step(-1);
      else if (action === "next") this._step(1);
      else if (action === "goto-current") this._gotoCurrentWeek();
      else if (action === "refresh") this._fetchWeeks();
      else if (action === "save" && week) this._saveSelection(week);
      else if (action === "cancel" && week) this._cancelEdit(week);
      else if (action === "toggle-filter") this._toggleShowSelectedOnly();
      else if (action === "filter-section")
        this._toggleSectionFilter(actionEl.getAttribute("data-section"));
      else if (action === "filter-section-all") this._clearSectionFilter();
      else if (action === "dismiss-downgrade") this._dismissDowngrade();
    });
  }

  // Open the shared recipe sheet for a market add-on. Add-ons carry a normal HelloFresh
  // recipe id, so the same detail API serves them; an item without one (identified only by
  // SKU) simply has nothing to show.
  async _openRecipeDetail(item) {
    const recipeId = item && item.recipe_id;
    if (!recipeId || !this._hass) return;
    try {
      if (!this._detailOverlay) {
        const { RecipeDetailOverlay } = await detailModule;
        this._detailOverlay = new RecipeDetailOverlay({
          getRoot: () => this.shadowRoot,
          callService: (service, data) => this._callResponseService(service, data),
        });
      }
      await this._detailOverlay.open(recipeId);
    } catch (err) {
      // eslint-disable-next-line no-console
      console.warn("hellofresh: recipe detail unavailable", err);
    }
  }

  async _callResponseService(service, data) {
    const payload = { ...(data || {}) };
    if (this._config.config_entry_id) payload.config_entry_id = this._config.config_entry_id;
    const result = await this._hass.callService(
      "hellofresh",
      service,
      payload,
      undefined,
      false,
      true,
    );
    return (result && result.response) || {};
  }

  // ---- helpers -------------------------------------------------------------

  _fmtPrice(amount, currency) {
    return fmtPrice(amount, currency);
  }

  _titleCase(value) {
    return titleCase(value);
  }

  // How far the week's delivery is from today, in words (e.g. "in 3 days", "2 weeks ago").
  // Mirrors the meal-planner card so both headers read identically.
  _relativeWeek(week) {
    return relativeWeek(week);
  }

  // Defensive only: `delivery_date` is a typed date serialized with isoformat(), so this
  // receives a valid ISO string or nothing. Matches the schedule card's guard anyway —
  // toLocaleDateString on an Invalid Date returns the literal string "Invalid Date" *without*
  // throwing, so a catch alone would never fire if that ever changed.
  _fmtDate(iso) {
    return fmtDate(iso);
  }

  _fmtDateTime(d) {
    try {
      return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
    } catch (_e) {
      return String(d);
    }
  }

  _esc(value) {
    return esc(value);
  }

  // Lazily build and cache the shared CSSStyleSheet (parsed once, reused by every instance).
  static _sheet() {
    if (!this.__sheet) {
      this.__sheet = new CSSStyleSheet();
      this.__sheet.replaceSync(this._styles());
      // The detail sheet's CSS ships with its module, which loads asynchronously. Append it on
      // arrival: this stylesheet object is already adopted by every instance, so mutating it
      // styles overlays opened later without re-adopting anything.
      detailModule
        .then(({ DETAIL_STYLES }) => {
          this.__sheet.replaceSync(this._styles() + DETAIL_STYLES);
        })
        .catch(() => {
          /* The overlay stays unavailable; the rest of the card is unaffected. */
        });
    }
    return this.__sheet;
  }

  static _styles() {
    return `
      ha-card { padding: 16px 16px 16px; }
      .head { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
      .head .logo { height: 40px; width: 40px; border-radius: 8px; object-fit: cover; flex: none; }
      .title-text { font-size: 1.5em; font-weight: 500; }
      .state { text-align: center; padding: 28px 8px; color: var(--secondary-text-color); }
      .state.error { color: var(--error-color, #db4437); }
      .header { display: flex; align-items: center; gap: 8px; }
      .header .weekinfo { flex: 1; text-align: center; }
      /* Match the meal-planner card's header sizing so "Classic Box" + week read identically. */
      .weektitle { font-size: 1.2em; font-weight: 600; }
      .weeksub { font-size: 0.9em; color: var(--secondary-text-color); }
      .skipped { color: var(--warning-color, #ff9800); font-weight: 700; }
      .nav {
        font-size: 1.6em; line-height: 1; border: none; background: none; cursor: pointer;
        color: var(--primary-text-color); padding: 4px 12px; border-radius: 50%;
      }
      .nav:hover { background: var(--secondary-background-color); }
      .currentweekslot {
        display: flex; justify-content: center; align-items: center;
        min-height: 36px; margin-top: 6px;
      }
      .currentweekbtn {
        font: inherit; font-size: 0.8em; font-weight: 600; cursor: pointer;
        padding: 5px 14px; border-radius: 14px; border: 1px solid var(--divider-color);
        background: var(--secondary-background-color); color: var(--primary-text-color);
      }
      .currentweekbtn:hover { filter: brightness(0.95); }
      .statusrow { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 12px 0; }
      .chip {
        font-size: 0.8em; padding: 4px 10px; border-radius: 14px;
        background: var(--secondary-background-color); color: var(--primary-text-color);
      }
      .chip.ok { background: var(--success-color, #4caf50); color: #fff; }
      .chip.locked { opacity: 0.7; }
      .chip.editable { background: var(--primary-color); color: var(--text-primary-color, #fff); }
      .hint { font-size: 0.78em; color: var(--secondary-text-color); }
      .filterchip { border: 1px solid var(--divider-color); cursor: pointer; font: inherit; font-size: 0.8em; }
      .filterchip:hover { filter: brightness(0.95); }
      /* Section filter bar: same classes and values as the meal-planner card's protein bar so
         the two cards read as one system. */
      .filterbar {
        display: flex; flex-wrap: wrap; align-items: center; gap: 8px 18px; margin: 0 0 12px;
        padding: 8px 12px; border-radius: 10px; background: var(--secondary-background-color);
      }
      .fgroup { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
      .flabel {
        font-size: 0.68em; text-transform: uppercase; letter-spacing: 0.04em;
        color: var(--secondary-text-color); margin-right: 2px;
      }
      .fbtn {
        display: inline-flex; align-items: center; gap: 6px;
        font: inherit; font-size: 0.8em; padding: 4px 10px; border-radius: 14px; cursor: pointer;
        border: 1px solid var(--divider-color); background: var(--card-background-color);
        color: var(--primary-text-color);
      }
      .fbtn:hover { filter: brightness(0.95); }
      .fbtn.on {
        background: var(--primary-color); color: var(--text-primary-color, #fff);
        border-color: var(--primary-color);
      }
      .skipbtn {
        margin-left: auto; font-size: 0.85em; padding: 5px 12px; border-radius: 14px;
        border: 1px solid var(--divider-color); background: var(--card-background-color);
        color: var(--primary-text-color); cursor: pointer;
      }
      .skipbtn + .skipbtn { margin-left: 4px; }
      .skipbtn:disabled { opacity: 0.5; cursor: default; }
      .savebtn {
        margin-left: auto; font-size: 0.85em; padding: 5px 14px; border-radius: 14px;
        border: none; background: var(--primary-color); color: var(--text-primary-color, #fff);
        cursor: pointer; font-weight: 600;
      }
      .savebtn + .skipbtn { margin-left: 4px; }
      .savebtn:disabled { opacity: 0.5; cursor: default; }
      .group { margin-top: 14px; }
      .grouptitle { font-size: 0.95em; font-weight: 700; margin: 6px 0; }
      .grid {
        display: grid; gap: 12px;
        grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
      }
      .grid.busy { opacity: 0.6; pointer-events: none; }
      /* Flex column so .metafoot can pin the ± stepper to the tile's bottom edge: grid
         rows stretch tiles to equal height, so steppers line up across a row no matter
         how long each item's description runs. */
      .item {
        border: 2px solid transparent; border-radius: 10px; overflow: hidden;
        background: var(--secondary-background-color);
        display: flex; flex-direction: column;
        /* Tapping the tile opens the full recipe (quantity is changed with the ± steppers). */
        cursor: pointer;
      }
      .item.selected { border-color: var(--primary-color); }
      .item:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
      .imgwrap { position: relative; aspect-ratio: 1 / 1; background: var(--divider-color); }
      .imgwrap img { width: 100%; height: 100%; object-fit: cover; display: block; }
      .noimg { width: 100%; height: 100%; }
      .qtybadge {
        position: absolute; top: 6px; right: 6px; min-width: 26px; height: 22px; padding: 0 6px;
        box-sizing: border-box; background: var(--primary-color); color: var(--text-primary-color, #fff);
        border-radius: 11px; display: flex; align-items: center; justify-content: center;
        font-size: 0.78em; font-weight: 700;
      }
      .price {
        position: absolute; bottom: 6px; right: 6px; padding: 2px 7px; border-radius: 10px;
        background: rgba(0,0,0,0.72); color: #fff; font-size: 0.72em; font-weight: 700;
      }
      .item.soldout .imgwrap img { opacity: 0.5; }
      .soldoutflag {
        position: absolute; top: 6px; left: 6px; padding: 2px 8px; border-radius: 10px;
        background: rgba(0,0,0,0.78); color: #fff; font-size: 0.68em; font-weight: 700;
        text-transform: uppercase; letter-spacing: 0.03em;
      }
      .meta { padding: 8px; flex: 1; display: flex; flex-direction: column; }
      .metafoot { margin-top: auto; padding-top: 8px; }
      .metafoot > .cals { margin-top: 0; }
      .name { font-size: 0.9em; font-weight: 600; }
      .desc { font-size: 0.8em; color: var(--secondary-text-color); margin-top: 2px; }
      .cals { font-size: 0.75em; color: var(--secondary-text-color); margin-top: 4px; font-weight: 600; }
      .stepper { display: flex; align-items: center; gap: 8px; }
      .qbtn {
        width: 28px; height: 28px; border-radius: 50%; border: 1px solid var(--divider-color);
        background: var(--card-background-color); color: var(--primary-text-color);
        font-size: 1.1em; font-weight: 700; line-height: 1; cursor: pointer; flex: none;
        display: flex; align-items: center; justify-content: center;
      }
      .qbtn:hover:not([disabled]) { border-color: var(--primary-color); color: var(--primary-color); }
      .qbtn[disabled] { opacity: 0.4; cursor: default; }
      .qval { font-weight: 700; min-width: 12px; text-align: center; }
      .toast {
        margin: 8px 0 0; padding: 8px 12px; border-radius: 8px;
        background: var(--secondary-background-color); font-size: 0.85em; text-align: center;
      }
      .toast.error { background: var(--error-color, #db4437); color: #fff; }
      .downgrade-notice {
        display: flex; align-items: flex-start; gap: 10px;
        margin: 8px 0 0; padding: 10px 14px; border-radius: 10px;
        background: var(--warning-color, #ff9800); color: #fff;
      }
      .downgrade-icon { font-size: 1.2em; flex: none; line-height: 1.3; }
      .downgrade-text { font-size: 0.9em; flex: 1; line-height: 1.35; }
      .downgrade-dismiss {
        flex: none; background: transparent; border: none; color: inherit;
        font-size: 1em; cursor: pointer; padding: 0 2px; opacity: 0.85;
      }
      .downgrade-dismiss:hover { opacity: 1; }
      /* Pin the "please wait while saving" banner to the top of the viewport so it's visible the
         instant Save is pressed, rather than sitting below a tall grid where it only scrolls into
         view once the reload shrinks the content (looking like it never appeared). */
      .toast.saving {
        position: fixed; top: 16px; left: 50%; transform: translateX(-50%); z-index: 9;
        margin: 0; width: max-content; max-width: calc(100vw - 32px);
        display: flex; align-items: center; justify-content: center; gap: 8px;
        background: var(--primary-color); color: var(--text-primary-color, #fff);
        box-shadow: 0 4px 16px rgba(0, 0, 0, 0.3);
      }
      .toast.saving .spinner {
        width: 14px; height: 14px; flex: none; border-radius: 50%;
        border: 2px solid currentColor; border-right-color: transparent;
        animation: hf-spin 0.7s linear infinite;
      }
      @keyframes hf-spin { to { transform: rotate(360deg); } }
      .actions { text-align: center; margin-top: 8px; }
      .actions button { padding: 6px 16px; border-radius: 8px; cursor: pointer; }
    `;
  }
}

customElements.define("hellofresh-market-card", HelloFreshMarketCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "hellofresh-market-card",
  name: "HelloFresh Market Card",
  description: "Browse and select HelloFresh Market add-ons per delivery week.",
});

console.info(`%c HELLOFRESH-MARKET-CARD %c v${MARKET_CARD_VERSION} `, "color:#fff;background:#91c11e;font-weight:700", "color:#91c11e;background:#fff");
