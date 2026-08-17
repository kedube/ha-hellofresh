/*
 * HelloFresh Meal Planner Card
 * ----------------------------
 * A custom Lovelace card for browsing HelloFresh delivery weeks recipe-by-recipe and
 * (where the week is still editable) changing the meal selection.
 *
 * Why a custom card rather than YAML + built-in cards: per-week recipes are NOT exposed as
 * entity attributes (they would exceed Home Assistant's 16 KB recorder attribute cap), so
 * the data is read on demand from the response-returning `hellofresh.get_weeks` service.
 * Lovelace cards can't bind a response-service result, so this card calls the service
 * directly via hass.callService(..., return_response=true) and renders the result. That also
 * sidesteps the size limit entirely — full recipe detail and images flow through live.
 *
 * Config:
 *   type: custom:hellofresh-meal-planner-card
 *   config_entry_id: <optional>   # required only when multiple HelloFresh accounts exist
 *   title: HelloFresh Meal Planner # optional card header
 *   image_width: 400               # optional Cloudinary resize width for recipe images
 *
 * No build step: this is hand-written ES2020 served straight from the integration's www/
 * directory, registered as a Lovelace resource by the integration at startup.
 */

// The integration stamps its release version onto the resource URL as ?v= (cache-bust),
// so the banner reports exactly which build the browser actually loaded.
const CARD_VERSION = new URL(import.meta.url).searchParams.get("v") || "unknown";

// Shared recipe-detail sheet, also used by the Recipes and Market cards.
//
// Loaded via a DYNAMIC import carrying this card's own ?v=. A static `./…js` specifier would
// resolve to the bare filename, which Lovelace never stamps — so a browser holding an old copy
// would keep serving it after an upgrade while the card itself refreshed. Propagating the
// version keeps the two in lockstep.
const detailModule = import(
  new URL(
    `./hellofresh-recipe-detail.js?v=${encodeURIComponent(CARD_VERSION)}`,
    import.meta.url,
  ).href
);

// HelloFresh recipe images are Cloudinary URLs containing a `/q_auto/` transform segment.
// Inserting a width transform keeps grid thumbnails small/fast instead of loading full-size
// hero JPEGs. Unknown URL shapes are returned unchanged.
function resizedImage(url, width) {
  // Only plain web URLs may reach <img src> — same defense the anchor sinks get from
  // _safeUrl. javascript:/data: in img src is inert in modern browsers, but an
  // attacker-chosen scheme/host has no business in the DOM at all.
  if (!url || !/^https?:\/\//i.test(String(url))) return "";
  if (!width) return url;
  return url.replace("/q_auto/", `/q_auto,w_${width}/`);
}

// Recipe promo clips are served from media.hellofresh.com. Same gate as resizedImage: only
// plain http(s) URLs may reach a media sink, so a payload-supplied scheme can never become a
// javascript:/data: URL in the DOM. Returns "" when there is no usable video.
function safeMediaUrl(url) {
  if (!url || !/^https?:\/\//i.test(String(url))) return "";
  return String(url);
}

// Preference -> accent color for the little protein dot. Mirrors HelloFresh's own grouping.
const PREFERENCE_COLORS = {
  Poultry: "#f0a202",
  Beef: "#c1432f",
  Pork: "#e6789b",
  Seafood: "#2f8fc1",
  Lamb: "#8d5b3f",
  Veggie: "#4caf50",
};

class HelloFreshMealPlannerCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    // Parse the stylesheet ONCE and share it across all instances via adoptedStyleSheets,
    // instead of re-injecting <style> (and re-parsing ~150 lines of CSS) on every render.
    this.shadowRoot.adoptedStyleSheets = [HelloFreshMealPlannerCard._sheet()];
    this._weeks = null; // cached get_weeks response
    this._account = null; // account-level fields from get_weeks (e.g. plan total price fallback)
    this._cursor = 0; // index into this._weeks
    // "Show selected only" filter. Persisted in localStorage so it survives reloads/reboots and
    // is shared across weeks (a view preference, not per-week state).
    this._showSelectedOnly = this._loadShowSelectedOnly();
    // Meal filters for current/future weeks (a view preference, persisted like the above):
    //  * _proteinFilter: Set of allowed protein preferences (empty = allow all).
    //  * _showVariants: when false, hide variant meals (2x protein, protein swaps, veg swaps),
    //    showing only the default meal of each group plus meals that have no variants.
    this._proteinFilter = this._loadProteinFilter();
    this._showVariants = this._loadShowVariants();
    this._loading = false;
    this._error = null;
    this._busy = false; // a write (select/skip) is in flight
    this._saving = null; // persistent "please wait" banner text while a save + reload is in flight
    this._fetched = false; // guard so we only auto-fetch once per hass attach
    this._hass = null;
    // Pending (unsaved) selection while the user edits a week, keyed by week_id -> Set of
    // course_index. Null entry / absent key means "show the saved selection". The set is
    // seeded from the server's is_selected on first edit, then mutated freely until the user
    // saves (submitting once) or it is discarded on refresh.
    this._pending = {};
    // Week id whose last save triggered a HelloFresh seamless downgrade (box silently shrunk
    // to fit). Drives a persistent, dismissable banner — unlike the 4s toast, a "your box was
    // downsized" warning shouldn't vanish before it's read. Cleared on dismiss or week change.
    this._downgradeNotice = null;
    // Cross-card week sync: a sibling HelloFresh card (e.g. the Market card) broadcasts the week
    // it navigates to; we follow it to the same week. If the event arrives before this card has
    // loaded its weeks, remember the id and apply it once the fetch completes.
    this._syncPendingWeekId = null;
    this._onSyncWeek = (ev) => this._receiveSyncedWeek(ev);
  }

  connectedCallback() {
    window.addEventListener(HelloFreshMealPlannerCard.WEEK_SYNC_EVENT, this._onSyncWeek);
    // Switching back to this card's dashboard tab re-connects the (already-loaded) element
    // without re-fetching, so pick up any week another card selected while we were hidden.
    if (this._weeks) this._applySyncedWeekId(this._loadSyncedWeekId());
  }

  disconnectedCallback() {
    window.removeEventListener(HelloFreshMealPlannerCard.WEEK_SYNC_EVENT, this._onSyncWeek);
    // Drop the pending toast timer so a detached card isn't kept alive (holding the whole
    // weeks payload) only to re-render into a shadow root nobody can see.
    clearTimeout(this._toastTimer);
    // Also drop the toast itself: with the timer cancelled, a toast shown just before
    // disconnect would otherwise repaint forever once the card reconnects.
    this._toast = null;
    // The video overlay lives beside the card in the shadow root and holds a document-level
    // key listener, so it has to be torn down explicitly rather than with the card's markup.
    this._closeVideo();
    // Same for the recipe sheet — it also registers an Escape handler on `document`.
    if (this._detailOverlay) this._detailOverlay.close();
  }

  setConfig(config) {
    this._config = {
      title: "HelloFresh Meal Planner",
      image_width: 400,
      ...config,
    };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    // Fetch once when hass first becomes available. Re-fetching happens explicitly after a
    // write or via the refresh button — polling on every hass update would hammer the service.
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

  static getConfigElement() {
    return document.createElement("hellofresh-meal-planner-card-editor");
  }

  static getStubConfig() {
    return { type: "custom:hellofresh-meal-planner-card" };
  }

  // `keepWeekId`: after a write (e.g. save), stay on the week the user was on rather than
  // snapping back to the default cursor. Falls back to the default when that week is gone.
  async _fetchWeeks(keepWeekId = null) {
    // Reentry guard: a refresh tap racing the post-save/post-skip resync would otherwise run
    // two concurrent get_weeks calls, and the slower response could clobber the newer state.
    if (!this._hass || this._loading) return;
    this._loading = true;
    this._error = null;
    this._render();
    try {
      const data = {};
      if (this._config.config_entry_id) {
        data.config_entry_id = this._config.config_entry_id;
      }
      const result = await this._hass.callService("hellofresh", "get_weeks", data, undefined, false, true);
      const response = (result && result.response) || {};
      const weeks = response.weeks || [];
      this._account = response.account || null; // account-level fallbacks (e.g. plan price)
      this._weeks = this._browsableWeeks(weeks);
      this._pending = {}; // server is now the source of truth; drop any stale edits
      this._dedupeCache = null; // recipe data changed: drop cached dedupe/name-count results
      this._savedSelCache = null;
      // Resolve which week to land on, in priority order:
      //  1. an explicit keep (post-save — stay where the user was),
      //  2. a week a sibling card asked us to sync to before we'd loaded,
      //  3. the week another HelloFresh card last selected (shared storage — cross-tab sync),
      //  4. this card's own default.
      const syncId = this._syncPendingWeekId ?? this._loadSyncedWeekId();
      this._syncPendingWeekId = null;
      let landing = keepWeekId != null
        ? this._weeks.findIndex((w) => w.week_id === keepWeekId)
        : -1;
      if (landing < 0 && syncId != null) {
        landing = this._weeks.findIndex((w) => w.week_id === syncId);
      }
      this._cursor = landing >= 0 ? landing : this._defaultCursor(this._weeks);
    } catch (err) {
      this._error = (err && err.message) || String(err);
    } finally {
      this._loading = false;
      this._render();
    }
  }

  // Trim the week list to what's actually browsable in the planner.
  //
  // HelloFresh schedules deliveries further out than it publishes *menus*, so the deliveries feed
  // includes future weeks that have no recipes yet. The planner should show exactly the weeks that
  // have menu data and no further — if HelloFresh has published 5 future weeks, show 5; if 7, show
  // 7 — without any fixed cap. So we walk the weeks in chronological order and, once we reach a
  // FUTURE week with no menu data, stop including everything from there on. Past and current weeks
  // are always kept (they belong to the browsable history even if their recipe payload is absent).
  _browsableWeeks(weeks) {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const ordered = (weeks || [])
      .slice()
      .sort((a, b) => this._weekSortKey(a) - this._weekSortKey(b));
    const result = [];
    let futureMenuEnded = false;
    for (const week of ordered) {
      const isFuture = week.delivery_date
        ? this._parseLocalDate(week.delivery_date).getTime() >= today.getTime()
        : true; // undated weeks are treated as future (can't anchor them to the past)
      const hasMenu = (week.recipes || []).length > 0;
      if (!isFuture) {
        result.push(week); // past/current week: always browsable history
        continue;
      }
      // Future week: include only while menus are still being published contiguously.
      if (futureMenuEnded || !hasMenu) {
        futureMenuEnded = true;
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
    const m = typeof value === "string" && /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
    if (m) return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    return new Date(value);
  }

  // Chronological sort key for a week (undated weeks sink to the end so they don't gate earlier
  // future weeks in _browsableWeeks).
  _weekSortKey(week) {
    const ms = week && week.delivery_date ? this._parseLocalDate(week.delivery_date).getTime() : NaN;
    return Number.isNaN(ms) ? Number.POSITIVE_INFINITY : ms;
  }

  // Land on the first week that still needs/allows a selection, else the first week.
  // Where the card opens when there's no synced/kept week: the CURRENT week by date, so both
  // HelloFresh cards start on the same week. Falls back to the first editable week (then week 0)
  // when no week carries a delivery date.
  _defaultCursor(weeks) {
    const current = this._currentWeekIndex();
    if (current >= 0) return current;
    const idx = weeks.findIndex((w) => this._isEditable(w));
    return idx >= 0 ? idx : 0;
  }

  _isEditable(week) {
    if (!week) return false;
    const actions = week.allowed_actions || {};
    if (actions.mealSwap === false) return false;
    if (week.is_skipped) return false;
    const deadline = week.selection_deadline ? Date.parse(week.selection_deadline) : null;
    if (deadline && deadline < Date.now()) return false;
    return Boolean(actions.mealSwap);
  }

  // A paused week never shipped, so its preselected/auto-fill picks are not a real selection.
  _isPaused(week) {
    return Boolean(week) && String(week.status || "").toUpperCase() === "PAUSED";
  }

  // Same eligibility as the schedule card's Skip/Unskip pill: only weeks the action can
  // still change. allowed_actions.mealSwap merely being PRESENT isn't enough — locked and
  // past weeks carry it as false, which used to leave a dead Skip button on them.
  _canSkip(week) {
    if (this._isEditable(week)) return true; // editable ⇒ skippable
    // A skipped/paused week can be restored (Unskip) while its deadline hasn't passed;
    // once the deadline is gone — or the week is in the past — nothing brings the box back.
    if (!week.is_skipped && !this._isPaused(week)) return false;
    const deadline = week.selection_deadline ? Date.parse(week.selection_deadline) : null;
    if (deadline) return deadline > Date.now();
    if (!week.delivery_date) return false;
    // No deadline info: fall back to "not in the past" (real date, NOT the browse grace
    // window _isPast uses — a delivered-last-week box is browsable but not skippable).
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    return this._parseLocalDate(week.delivery_date).getTime() >= today.getTime();
  }

  // The menu grace window in whole weeks: how long after its delivery date a week keeps its
  // full browsable menu. The integration reports its configured value (the "Full menu after
  // delivery" option, DEFAULT_MENU_GRACE_WEEKS in const.py) on the get_weeks account payload;
  // fall back to the same default before the first fetch resolves.
  _menuGraceWeeks() {
    const weeks = this._account && this._account.menu_grace_weeks;
    return Number.isFinite(weeks) ? weeks : 2;
  }

  // A week whose delivery date is more than the menu grace window before today (undated weeks
  // are not treated as past). Mirrors the integration's gating: a just-delivered week still
  // carries its full browsable menu (with the delivered meals marked selected), so it keeps
  // the filter bar and browsable rendering; only older weeks render as delivered-only
  // history. Editability is unaffected — that's gated by allowed_actions/cutoff.
  _isPast(week) {
    if (!week || !week.delivery_date) return false;
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const graceMs = this._menuGraceWeeks() * 7 * 24 * 60 * 60 * 1000;
    return this._parseLocalDate(week.delivery_date).getTime() < today.getTime() - graceMs;
  }

  // Move the cursor to an index, render, and (unless syncing FROM another card) broadcast the
  // resulting week so a sibling HelloFresh card on the same dashboard follows to the same week.
  _setCursor(index, { broadcast = true } = {}) {
    if (!this._weeks || this._weeks.length === 0) return;
    const clamped = Math.max(0, Math.min(index, this._weeks.length - 1));
    if (clamped === this._cursor) return;
    this._cursor = clamped;
    this._downgradeNotice = null; // a downgrade warning is about the week you just saved
    // A clip belongs to the week it was opened from; leaving that week must not leave it playing.
    this._closeVideo();
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

  // Jump the cursor to the first week that still needs a meal selection (banner tap).
  _gotoNeedsChoice() {
    const needing = this._weeksNeedingChoice();
    if (needing.length === 0) return;
    const target = this._weeks.indexOf(needing[0]);
    if (target >= 0) this._setCursor(target);
  }

  // ---- cross-card week sync ------------------------------------------------
  //
  // The meal-planner and market cards share the selected week. The two cards can live on
  // DIFFERENT dashboard views (tabs), so a live event alone is not enough — the other card may
  // be unmounted when you navigate, and only mounts when you switch to its tab. So the selected
  // week is PERSISTED in localStorage (keyed by account) and restored on load; the live event is
  // an optimization for when both cards are mounted on the same view at once.

  // The window event both HelloFresh cards use to keep their selected week in step.
  static get WEEK_SYNC_EVENT() {
    return "hellofresh-week-selected";
  }

  // The config entry (account) this card is bound to, if pinned. Only cards for the SAME account
  // sync with each other, so a multi-account dashboard doesn't cross-drive unrelated cards.
  _accountKey() {
    return (this._config && this._config.config_entry_id) || "default";
  }

  _syncStorageKey() {
    return `hellofresh:selected-week:${this._accountKey()}`;
  }

  // The week id a sibling card last selected (from shared storage), or null.
  _loadSyncedWeekId() {
    try {
      return window.localStorage.getItem(this._syncStorageKey()) || null;
    } catch (_e) {
      return null;
    }
  }

  // Announce + persist the currently displayed week so a sibling card follows to it — now (live
  // event, if it's mounted) and later (storage, when it next mounts / you switch to its tab).
  _broadcastWeek() {
    const week = this._weeks ? this._weeks[this._cursor] : null;
    if (!week) return;
    try {
      window.localStorage.setItem(this._syncStorageKey(), week.week_id);
    } catch (_e) {
      /* storage unavailable: fall back to the live event only */
    }
    window.dispatchEvent(
      new CustomEvent(HelloFreshMealPlannerCard.WEEK_SYNC_EVENT, {
        detail: { weekId: week.week_id, accountKey: this._accountKey() },
      })
    );
  }

  // Tell read-only sibling cards (the schedule card) that a write changed account data, so
  // they re-pull immediately instead of waiting for their next interval refresh.
  _broadcastDataChanged() {
    window.dispatchEvent(
      new CustomEvent("hellofresh-data-changed", {
        detail: { accountKey: this._accountKey() },
      })
    );
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

  // Max servings the +/- stepper allows for one meal (HelloFresh's typical per-recipe cap).
  static get MAX_QUANTITY() {
    return 4;
  }

  // Smallest box HelloFresh accepts: a week must have at least this many DISTINCT meals. The
  // box resizes to match the number of distinct meals chosen (up or down from the base plan),
  // so this floor — not the base plan's meal count — is what gates saving. Mirrors the client's
  // MIN_MEALS_PER_WEEK.
  static get MIN_MEALS() {
    return 2;
  }

  // localStorage key for the "show selected only" view preference.
  static get FILTER_STORAGE_KEY() {
    return "hellofresh-meal-planner:show-selected-only";
  }

  // The protein preferences that can be filtered on. Mirrors HelloFresh's own grouping (the same
  // set that colors the protein dot); the value is the recipe `preference` field.
  static get PROTEIN_FILTERS() {
    return ["Beef", "Poultry", "Pork", "Seafood", "Lamb", "Veggie"];
  }

  static get PROTEIN_STORAGE_KEY() {
    return "hellofresh-meal-planner:protein-filter";
  }

  static get VARIANTS_STORAGE_KEY() {
    return "hellofresh-meal-planner:show-variants";
  }

  _loadShowSelectedOnly() {
    try {
      return window.localStorage.getItem(HelloFreshMealPlannerCard.FILTER_STORAGE_KEY) === "1";
    } catch (_e) {
      return false; // private mode / storage disabled: default to showing all meals
    }
  }

  // Load the persisted protein filter as a Set, dropping any stored value no longer valid.
  _loadProteinFilter() {
    try {
      const raw = window.localStorage.getItem(HelloFreshMealPlannerCard.PROTEIN_STORAGE_KEY);
      const allowed = HelloFreshMealPlannerCard.PROTEIN_FILTERS;
      return new Set((raw ? JSON.parse(raw) : []).filter((p) => allowed.includes(p)));
    } catch (_e) {
      return new Set(); // empty = show all proteins
    }
  }

  _loadShowVariants() {
    try {
      // Default to showing variants (only "0" turns them off) so first-run shows the full menu.
      return window.localStorage.getItem(HelloFreshMealPlannerCard.VARIANTS_STORAGE_KEY) !== "0";
    } catch (_e) {
      return true;
    }
  }

  // Flip the filter, persist it, and re-render.
  _toggleShowSelectedOnly() {
    this._showSelectedOnly = !this._showSelectedOnly;
    try {
      window.localStorage.setItem(
        HelloFreshMealPlannerCard.FILTER_STORAGE_KEY,
        this._showSelectedOnly ? "1" : "0"
      );
    } catch (_e) {
      /* storage unavailable: keep the in-memory setting for this session */
    }
    this._render();
  }

  // Toggle one protein in/out of the filter set, persist, and re-render.
  _toggleProteinFilter(protein) {
    if (!HelloFreshMealPlannerCard.PROTEIN_FILTERS.includes(protein)) return;
    if (this._proteinFilter.has(protein)) this._proteinFilter.delete(protein);
    else this._proteinFilter.add(protein);
    try {
      window.localStorage.setItem(
        HelloFreshMealPlannerCard.PROTEIN_STORAGE_KEY,
        JSON.stringify([...this._proteinFilter])
      );
    } catch (_e) {
      /* storage unavailable: keep the in-memory setting for this session */
    }
    this._render();
  }

  // Clear all protein filters (the "All" chip), persist, and re-render.
  _clearProteinFilter() {
    if (this._proteinFilter.size === 0) return;
    this._proteinFilter.clear();
    try {
      window.localStorage.setItem(HelloFreshMealPlannerCard.PROTEIN_STORAGE_KEY, "[]");
    } catch (_e) {
      /* storage unavailable */
    }
    this._render();
  }

  // Flip the "show variants" toggle, persist, and re-render.
  _toggleShowVariants() {
    this._showVariants = !this._showVariants;
    try {
      window.localStorage.setItem(
        HelloFreshMealPlannerCard.VARIANTS_STORAGE_KEY,
        this._showVariants ? "1" : "0"
      );
    } catch (_e) {
      /* storage unavailable */
    }
    this._render();
  }

  // Whether the meal filters (protein / variants) apply to this week. They only make sense for
  // the browsable menu of a current or upcoming week; a past week just shows what was delivered.
  _filtersApply(week) {
    return Boolean(week) && !this._isPast(week) && !this._showSelectedOnly;
  }

  // Whether any visibility-affecting filter is active (protein narrowed, or variants hidden).
  // Used to decide when an in-place tile update is safe vs when a full render is needed.
  _hasActiveFilters() {
    return this._proteinFilter.size > 0 || !this._showVariants;
  }

  // A "default" meal is one shown when variants are hidden: it has no variant group, or it IS
  // the base (default) meal of its group. Variant tiles (2x protein, protein/veg swaps) are the
  // group members whose own index differs from the group's base index.
  _isDefaultMeal(r) {
    return r.variation_group == null || r.course_index === r.variation_group;
  }

  // Whether a recipe passes the active protein/variant filters. Selected meals bypass the filter
  // so a chosen meal never vanishes from view while you're editing (it always leads the grid).
  _passesFilters(r, ctx) {
    if (ctx.sel(r)) return true;
    if (this._proteinFilter.size > 0 && !this._proteinFilter.has(r.preference)) return false;
    if (!this._showVariants && !this._isDefaultMeal(r)) return false;
    return true;
  }

  // The course_index -> quantity map currently shown for a week: the user's pending edit if one
  // is in progress, otherwise the server's saved selection.
  _displaySelection(week) {
    const pending = this._pending[week.week_id];
    if (pending) return pending;
    return this._savedSelection(week);
  }

  // Saved selection as a Map of course_index -> serving quantity (>=1 for chosen meals).
  // Cached per week_id (depends only on server data, invalidated on fetch) since it's consulted
  // repeatedly per render (display, dirty check, needs-choice, tile selected state).
  // Stable key for a recipe in the selection map. Menu recipes carry a numeric course_index
  // (used for editing/cart writes); delivered past-week recipes have no index (index=null), so
  // fall back to recipe_id — otherwise every null-index meal collapses onto one map entry and a
  // 3-meal past week miscounts as "1 meal".
  _selKey(recipe) {
    return recipe.course_index != null ? recipe.course_index : recipe.recipe_id;
  }

  _savedSelection(week) {
    if (!this._savedSelCache) this._savedSelCache = new Map();
    const cached = this._savedSelCache.get(week.week_id);
    if (cached) return cached;
    const map = new Map();
    for (const r of week.recipes || []) {
      if (this._isSelected(r)) map.set(this._selKey(r), this._recipeQuantity(r));
    }
    this._savedSelCache.set(week.week_id, map);
    return map;
  }

  _isSelected(recipe) {
    return recipe.is_selected === true || recipe.is_selected === "true";
  }

  // The recipe's currently-saved serving count (defaults to 1 when selected but unspecified).
  _recipeQuantity(recipe) {
    const q = Number(recipe.selected_quantity);
    return Number.isFinite(q) && q > 0 ? q : 1;
  }

  // Total servings in a selection map (sum of quantities) — reflects box size in servings.
  _servingsTotal(selectionMap) {
    let total = 0;
    for (const q of selectionMap.values()) total += q;
    return total;
  }

  // Number of DISTINCT meals in a selection (one entry per chosen recipe, regardless of its
  // serving quantity). This is what sizes the box and gates saving against MIN_MEALS.
  _mealsCount(selectionMap) {
    return selectionMap.size;
  }

  // The quantity currently shown for a tile, accounting for collapsed-duplicate aliases.
  _displayedQuantity(week, recipe) {
    const display = this._displaySelection(week);
    const idx = (recipe._aliasIndexes || [this._selKey(recipe)]).find((i) => display.has(i));
    return idx === undefined ? 0 : display.get(idx);
  }

  // True when the pending edit differs from what's saved (i.e. there's something to save).
  _isDirty(week) {
    const pending = this._pending[week.week_id];
    if (!pending) return false;
    const saved = this._savedSelection(week);
    if (pending.size !== saved.size) return true;
    for (const [idx, qty] of pending) if (saved.get(idx) !== qty) return true;
    return false;
  }

  // Seed (once) and return the mutable pending selection map for a week.
  _ensurePending(week) {
    let pending = this._pending[week.week_id];
    if (!pending) {
      pending = new Map(this._savedSelection(week));
      this._pending[week.week_id] = pending;
    }
    return pending;
  }

  // The course_index this tile writes to: the one already in the pending selection (for a
  // collapsed duplicate that may be saved under an alias), else the representative index.
  _activeIndex(pending, recipe) {
    const allIdx = recipe._aliasIndexes || [recipe.course_index];
    const chosen = allIdx.find((i) => pending.has(i));
    return chosen !== undefined ? chosen : recipe.course_index;
  }

  // Tap a recipe tile: toggle it in/out of the selection at quantity 1 (or remove if present).
  _toggleRecipe(week, recipe) {
    if (this._busy || !this._canEdit(week)) return;
    const pending = this._ensurePending(week);
    const idx = this._activeIndex(pending, recipe);
    if (pending.has(idx)) {
      pending.delete(idx);
    } else {
      this._nudgeIfOver(week, pending, true); // adding a new distinct meal
      pending.set(idx, 1);
    }
    this._renderSelectionChange(week, recipe);
  }

  // +/- stepper: change a meal's serving count by delta, clamped to [0, MAX_QUANTITY].
  // Reaching 0 removes the meal entirely.
  _changeQuantity(week, recipe, delta) {
    if (this._busy || !this._canEdit(week)) return;
    const pending = this._ensurePending(week);
    const idx = this._activeIndex(pending, recipe);
    const current = pending.get(idx) || 0;
    const next = Math.max(0, Math.min(HelloFreshMealPlannerCard.MAX_QUANTITY, current + delta));
    if (next === current) return;
    if (next === 0) {
      pending.delete(idx);
    } else {
      // A serving bump on an existing meal doesn't grow the box (no new distinct meal).
      pending.set(idx, next);
    }
    this._renderSelectionChange(week, recipe);
  }

  // Selection edits are the hot path. When no visibility-affecting filter is active, a
  // tap/stepper can't change which tiles are visible, so update just the affected tile and
  // the header chips in place — no grid rebuild, no image teardown. With "selected only"
  // ON, or a protein/variant filter active (a selected meal bypasses those filters, so
  // DE-selecting it can hide its tile), visibility can change — full render instead, or
  // the grid drifts out of sync with the filter until the next unrelated render.
  _renderSelectionChange(week, recipe) {
    if (this._showSelectedOnly || this._hasActiveFilters() || !this._shell) {
      this._render();
      return;
    }
    this._updateTile(week, recipe);
    // Header carries the live servings count / dirty + Save/Cancel state.
    this._shell.content.querySelector(".statusrow")?.replaceWith(
      this._fragment(this._renderStatusRow(week))
    );
  }

  // Re-render a single recipe tile in place (selected state, qty badge, stepper).
  _updateTile(week, recipe) {
    const idxAttr = String(recipe.course_index);
    const tile = this._shell.content.querySelector(`.recipe[data-index="${CSS.escape(idxAttr)}"]`);
    if (!tile) return;
    const replacement = this._fragment(this._renderRecipeTile(week, recipe, this._tileContext(week)));
    if (replacement.firstElementChild) tile.replaceWith(replacement.firstElementChild);
  }

  // Parse an HTML string into a detached fragment (for in-place node replacement).
  _fragment(html) {
    const tpl = document.createElement("template");
    tpl.innerHTML = html;
    return tpl.content;
  }

  // Choosing more DISTINCT meals than the base plan resizes the box up for this week (a larger
  // box, repriced by HelloFresh) — not a hard cap. Nudge once when a newly-added meal takes the
  // selection past the plan's meal count so the price change isn't a surprise.
  _nudgeIfOver(week, pending, addedMeal) {
    const required = week.meals_required;
    // Only a new distinct meal grows the box; bumping an existing meal's servings does not.
    if (addedMeal && required && this._mealsCount(pending) >= required) {
      this._flash(
        `Choosing more than your ${required}-meal plan resizes this week's box — the price will update to match.`
      );
    }
  }

  // Discard a week's pending edit (revert to the saved selection).
  _cancelEdit(week) {
    delete this._pending[week.week_id];
    this._render();
  }

  async _saveSelection(week) {
    const pending = this._pending[week.week_id];
    if (!pending) return;
    // The box resizes to the number of DISTINCT meals chosen, so the only floor is HelloFresh's
    // smallest box (MIN_MEALS) — not the base plan's meal count. Fewer has no valid box.
    const meals = this._mealsCount(pending);
    const min = HelloFreshMealPlannerCard.MIN_MEALS;
    if (meals < min) {
      this._flash(`Choose at least ${min} meals before saving (${meals} selected).`);
      return;
    }
    // Translate chosen course_index values back into recipe ids + per-recipe quantities.
    const byIndex = new Map((week.recipes || []).map((r) => [r.course_index, r.recipe_id]));
    const recipeIds = [];
    const quantities = {};
    for (const [idx, qty] of pending) {
      const recipeId = byIndex.get(idx);
      if (!recipeId) continue;
      recipeIds.push(recipeId);
      if (qty > 1) quantities[recipeId] = qty; // 1 is the default; only send overrides
    }

    this._busy = true;
    this._saving = "Please wait while saving selections…"; // persistent banner until reload completes
    this._render();
    // Yield a paint frame so the banner is actually drawn before the (possibly fast-resolving)
    // service call and reload run — otherwise the microtask after `await` can pre-empt the paint
    // and the banner appears not to show until the page refreshes.
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    try {
      const data = { week_id: week.week_id, recipe_ids: recipeIds };
      if (Object.keys(quantities).length) data.quantities = quantities;
      if (this._config.config_entry_id) data.config_entry_id = this._config.config_entry_id;
      // return_response=true so we can see HelloFresh's seamless-downgrade flag: the write
      // may succeed but silently shrink the box to fit, saving fewer meals than requested.
      const result = await this._hass.callService(
        "hellofresh", "select_meals", data, undefined, false, true
      );
      const downgraded = !!((result && result.response) || {}).downgraded;
      delete this._pending[week.week_id]; // saved — drop the pending overlay
      this._busy = false;
      this._broadcastDataChanged();
      await this._fetchWeeks(week.week_id); // resync, staying on the week we just saved
      if (downgraded) this._noteDowngrade(week.week_id);
      else this._flash("Meal selection updated.");
    } catch (err) {
      this._busy = false;
      // Preserve the user's unsaved picks across the resync: _fetchWeeks drops _pending
      // (server is normally the source of truth), but after a FAILED save those edits are
      // the user's only copy — wiping them forced re-picking the whole box after a
      // transient error. Restore them so Save can simply be retried.
      const unsaved = this._pending[week.week_id];
      await this._fetchWeeks(week.week_id); // resync from the source of truth
      if (unsaved) this._pending[week.week_id] = unsaved;
      this._flash(`Selection failed: ${(err && err.message) || err}`, true);
    } finally {
      this._saving = null;
      this._render();
    }
  }

  async _toggleSkip(week) {
    if (this._busy) return;
    this._busy = true;
    this._render();
    const service = week.is_skipped || this._isPaused(week) ? "unskip_week" : "skip_week";
    try {
      const data = { week_id: week.week_id };
      if (this._config.config_entry_id) data.config_entry_id = this._config.config_entry_id;
      await this._hass.callService("hellofresh", service, data);
      this._broadcastDataChanged();
    } catch (err) {
      this._flash(`${service} failed: ${(err && err.message) || err}`, true);
    } finally {
      this._busy = false;
      await this._fetchWeeks();
    }
  }

  _flash(message, isError = false) {
    this._toast = { message, isError };
    // Repaint only the toast region: a full _render() tears down and rebuilds the whole
    // recipe grid (hundreds of <img> tiles on a planning-catalog week) twice per toast —
    // once to show it, once 4s later to hide it — for a change confined to the toast box.
    this._renderToast();
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => {
      this._toast = null;
      this._renderToast();
    }, 4000);
  }

  // Update just the toast/saving-banner region; falls back to a full render pre-shell.
  _renderToast() {
    if (!this._shell) {
      this._render();
      return;
    }
    this._shell.toast.innerHTML = this._saving
      ? `<div class="toast saving"><span class="spinner"></span>${this._esc(this._saving)}</div>`
      : this._toast
        ? `<div class="toast ${this._toast.isError ? "error" : ""}">${this._esc(this._toast.message)}</div>`
        : "";
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

  // The card header: an optional logo image plus the title. `logo: true` uses the bundled
  // HelloFresh logo served by the integration; `logo: <url>` uses a custom image.
  _renderCardHeader() {
    const logo = this._config.logo;
    const title = this._config.title;
    if (!logo && !title) return "";
    const logoUrl = logo === true ? "/hellofresh/hellofresh-logo.png" : logo;
    return `
      <div class="card-header">
        ${logoUrl ? `<img class="logo" src="${this._esc(logoUrl)}" alt="HelloFresh">` : ""}
        ${title ? `<span class="title-text">${this._esc(title)}</span>` : ""}
      </div>`;
  }

  // A week still needs the customer's attention when it's editable AND either:
  //   • it has fewer than the minimum number of meals saved (an invalid, under-sized box), or
  //   • HelloFresh has only *preselected* the meals (auto-picked), so the box is "full" of
  //     auto-picks the customer hasn't confirmed/reviewed yet.
  // A preselected week reports meals_selected == meals_required, so a pure count check misses it —
  // the meals_preselected/auto_picked flag is what surfaces those. Saving fewer meals than the
  // base plan is a valid box resize, not a problem, so we no longer flag those. Returns the list
  // (cursor order).
  _weeksNeedingChoice() {
    return (this._weeks || []).filter((w) => {
      if (!this._isEditable(w)) return false;
      if (this._isPreselected(w)) return true;
      return this._savedSelection(w).size < HelloFreshMealPlannerCard.MIN_MEALS;
    });
  }

  // Whether HelloFresh auto-picked (preselected) this week's meals rather than the customer
  // choosing them. Prefers the server's auto_picked flag (delivery-date aware) and falls back to
  // the raw meals_preselected flag, ignoring paused weeks whose preselection never ships.
  _isPreselected(week) {
    if (!week || this._isPaused(week)) return false;
    if (week.auto_picked != null) return Boolean(week.auto_picked);
    return Boolean(week.meals_preselected);
  }

  // Banner summarizing weeks that still need meals chosen, with the soonest deadline. Moved
  // here from the dashboard's Overview view so the prompt lives with the planner that fixes
  // it; tapping the banner jumps the week cursor to the first week needing a choice.
  _renderNeedsChoosingBanner() {
    // The banner is a prompt to edit, so it only shows while the grid is in its editable
    // "show all meals" mode. In "show selected only" mode editing is disabled, so prompting to
    // pick meals there would be misleading.
    if (this._showSelectedOnly) return "";
    const pending = this._weeksNeedingChoice();
    if (pending.length === 0) return "";
    const plural = pending.length === 1 ? "week" : "weeks";
    return `
      <div class="banner" data-action="goto-needs" role="button" tabindex="0">
        <span class="banner-icon">🍽️</span>
        <span class="banner-text">
          <strong>${pending.length} ${plural}</strong> still need a meal selection
        </span>
      </div>`;
  }

  // Build the stable shell once (ha-card + delegated listeners), then on every render only
  // replace the inner regions' HTML. The shadow root and its single set of event listeners are
  // never torn down, so interactions don't re-parse CSS or re-attach handlers.
  _ensureShell() {
    if (this._shell) return;
    const card = document.createElement("ha-card");
    card.innerHTML = `
      <div class="js-card-header"></div>
      <div class="js-banner"></div>
      <div class="content"></div>
      <div class="js-toast"></div>`;
    this.shadowRoot.appendChild(card);
    this._shell = {
      header: card.querySelector(".js-card-header"),
      banner: card.querySelector(".js-banner"),
      content: card.querySelector(".content"),
      toast: card.querySelector(".js-toast"),
    };
    this._bindDelegated(card);
  }

  _render() {
    if (!this.shadowRoot) return;
    this._ensureShell();
    const week = this._weeks ? this._weeks[this._cursor] : null;
    this._shell.header.innerHTML = this._renderCardHeader();
    this._shell.banner.innerHTML = this._renderNeedsChoosingBanner();
    this._shell.content.innerHTML = this._renderBody(week);
    // A save in flight shows a persistent "please wait" banner (with a spinner) until the
    // reload completes; a transient toast otherwise. The saving banner takes precedence.
    this._renderToast();
  }

  _renderBody(week) {
    if (this._loading) return `<div class="state">Loading weeks…</div>`;
    if (this._error) {
      return `<div class="state error">Could not load weeks: ${this._esc(this._error)}</div>
        <div class="actions"><button data-action="refresh">Retry</button></div>`;
    }
    if (!this._weeks || this._weeks.length === 0) {
      return `<div class="state">No delivery weeks found.</div>
        <div class="actions"><button data-action="refresh">Refresh</button></div>`;
    }
    return `${this._renderHeader(week)}${this._renderDowngradeNotice(week)}${this._renderOrder(week)}${this._renderFilterBar(week)}${this._renderGrid(week)}`;
  }

  // Inline banner shown on the week whose last save triggered a HelloFresh seamless downgrade:
  // the write succeeded but the box was silently shrunk to fit, so fewer meals were saved than
  // chosen. Surfaced right where the edit happened (the persistent HA notification is easy to
  // miss). Dismissable; also cleared automatically when the user navigates to another week.
  _renderDowngradeNotice(week) {
    if (!week || this._downgradeNotice !== week.week_id) return "";
    return `
      <div class="downgrade-notice" role="alert">
        <span class="downgrade-icon" aria-hidden="true">⚠</span>
        <span class="downgrade-text">HelloFresh <strong>downsized this box</strong> to fit your
          plan — fewer meals were saved than you selected. Review the saved selection below.</span>
        <button class="downgrade-dismiss" data-action="dismiss-downgrade" aria-label="Dismiss">✕</button>
      </div>`;
  }

  // Per-week order/shipment summary shown above the recipe grid: order id, delivery status,
  // carrier, tracking number (linked when a tracking URL is present), the delivered date on
  // boxes that have arrived, and box total price.
  // Only renders fields that exist. When a week has no order, the Total still shows the
  // account's recurring plan price (matching the selected_plan_total_price sensor) so the
  // strip remains informative for weeks that aren't billed yet.
  _renderOrder(week) {
    const order = week.order;
    const items = [];
    if (order) {
      const status = order.tracking_status || order.status;
      if (status) items.push(this._orderItem("Status", this._titleCase(status)));
      if (order.carrier) items.push(this._orderItem("Carrier", order.carrier));
      if (order.tracking_number) {
        const num = this._esc(order.tracking_number);
        const href = this._safeUrl(order.tracking_url);
        const value = href
          ? `<a href="${href}" target="_blank" rel="noopener">${num}</a>`
          : num;
        items.push(this._orderItem("Tracking", value, true));
      }
      // For a box that has arrived, show WHEN: the order's delivery date (falling back to the
      // week's). Only on delivered boxes — for upcoming weeks the header already shows the
      // scheduled date, so repeating it here would be noise.
      // "DELIVERED" exact word — a bare "DELIVER" needle would also match OUT_FOR_DELIVERY.
      const isDelivered = String(status || week.status || "").toUpperCase().includes("DELIVERED");
      // Prefer the week's delivered_at — the ACTUAL carrier timestamp from the deliveries
      // payload's tracking node — over the scheduled dates. The order's delivery_date is a
      // billing-derived anchor and read a day early; delivered_at is when the box arrived,
      // rendered in the viewer's timezone.
      const deliveredDate = week.delivered_at || week.delivery_date;
      if (isDelivered && deliveredDate) {
        items.push(this._orderItem("Delivered", this._fmtDate(deliveredDate)));
      }
    }

    // Total resolution, in priority order:
    //  1. The order's authoritative summed billing total (matches next_box_total_price sensor).
    //  2. The order's per-week cart/estimate price (billing not computed yet).
    //  3. The account's recurring plan price (no order id for this week) — the requested
    //     fallback, matching the selected_plan_total_price sensor.
    let total = null;
    let totalCurrency = null;
    let totalLabel = "Total";
    if (order && order.billed_total_price != null) {
      total = order.billed_total_price;
      totalCurrency = order.billed_total_currency || order.currency;
    } else if (order && order.total_price != null) {
      total = order.total_price;
      totalCurrency = order.currency;
    } else if (this._account && this._account.selected_plan_total_price != null) {
      total = this._account.selected_plan_total_price;
      totalCurrency = this._account.selected_plan_total_price_currency;
      totalLabel = "Plan total"; // distinguish the estimate from a real billed box total
    }
    if (total != null) {
      items.push(this._orderItem(totalLabel, this._fmtPrice(total, totalCurrency)));
    }
    if (order && order.order_id) items.push(this._orderItem("Order ID", order.order_id));
    if (items.length === 0) return "";
    return `<div class="orderbar">${items.join("")}</div>`;
  }

  // One label/value cell of the order bar. `isHtml` lets a pre-escaped value (e.g. a link)
  // pass through without double-escaping.
  _orderItem(label, value, isHtml = false) {
    return `<div class="oitem">
      <span class="olabel">${this._esc(label)}</span>
      <span class="oval">${isHtml ? value : this._esc(value)}</span>
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
            ${week.delivery_date ? `${this._esc(this._fmtDate(week.delivery_date))}` : ""}
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

  // The status row reflects live selection state, so it's re-rendered on its own during an
  // in-place selection edit (see _renderSelectionChange) without rebuilding the whole header.
  _renderStatusRow(week) {
    const editable = this._isEditable(week);
    // Coerced to a number before interpolation into the chip's title attribute and body:
    // an integer under the server contract, but the card must not trust that (innerHTML).
    const required = Number(week.meals_required) || 0;
    const min = HelloFreshMealPlannerCard.MIN_MEALS;
    const display = this._displaySelection(week);
    // The box sizes to DISTINCT meals; servings (a 2× meal) portion within that box.
    const meals = this._mealsCount(display);
    const servings = this._servingsTotal(display);
    const dirty = this._isDirty(week);
    const deadline = week.selection_deadline ? new Date(week.selection_deadline) : null;
    // Saving is gated only by the smallest box HelloFresh sells, not the base plan.
    const canSave = !this._busy && meals >= min;
    // A skipped/paused week never ships, so it's neither "resized" nor under-filled — just show
    // a plain "0 meals" with no plan/resize hint (that framing only makes sense for a live box).
    const inactive = week.is_skipped || this._isPaused(week);
    // The box resizes when the meal count differs from the base plan (repriced by HelloFresh).
    const resized = !inactive && required && meals !== required;
    const mealLabel = `${meals} meal${meals === 1 ? "" : "s"}`;
    const servingsLabel = servings !== meals ? ` · ${servings} servings` : "";
    return `
      <div class="statusrow">
        <span class="chip ${inactive ? "" : meals >= min ? "ok" : "warn"}" title="${resized ? `Resizes your ${required}-meal plan to a ${meals}-meal box for this week` : ""}">
          ${mealLabel}${resized ? ` (plan: ${required})` : ""}${servingsLabel}${dirty ? " · unsaved" : ""}
        </span>
        ${week.meals_preselected && !this._isPaused(week)
          ? `<span class="chip preselected" title="HelloFresh auto-picked these meals — review and adjust before the deadline.">Preselected</span>`
          : ""}
        <button
          class="chip filterchip"
          data-action="toggle-filter"
          title="${this._showSelectedOnly ? "Show all meals" : "Show only selected meals"}"
        >${this._showSelectedOnly ? "Show all meals" : "Show selected only"}</button>
        ${deadline ? `<span class="chip">Deadline ${this._esc(this._fmtDateTime(deadline))}</span>` : ""}
        <span class="chip ${editable ? "editable" : "locked"}">${editable ? "Editable" : "Locked"}</span>
        ${editable && this._showSelectedOnly
          ? `<span class="hint">Switch to “Show all meals” to change your selection.</span>`
          : editable
            ? `<span class="hint">Tap meals to choose, then Save.</span>`
            : ""}
        ${dirty
          ? `<button class="savebtn" data-action="save" ${canSave ? "" : "disabled"}>Save selection</button>
             <button class="skipbtn" data-action="cancel" ${this._busy ? "disabled" : ""}>Cancel</button>`
          : ""}
        ${this._canSkip(week)
          ? `<button class="skipbtn" data-action="skip" ${this._busy ? "disabled" : ""}>${week.is_skipped || this._isPaused(week) ? "Unskip week" : "Skip week"}</button>`
          : ""}
        <button class="skipbtn" data-action="refresh" ${this._busy ? "disabled" : ""}>↻</button>
      </div>
    `;
  }

  // A signature of the fields that would visibly distinguish two same-named tiles. Two recipes
  // with the same signature are TRUE duplicates (HelloFresh lists one dish under multiple menu
  // categories), not variants — there is nothing to tell them apart, so they're collapsed.
  _recipeSignature(r) {
    return JSON.stringify([
      r.name || "",
      r.description || "",
      r.variation_title || "",
      r.surcharge_label || "",
      r.calories_kcal ?? "",
      r.protein_g ?? "",
      (r.tags || []).slice().sort(),
    ]);
  }

  // Collapse true-duplicate recipes (identical signature) into one representative, keeping every
  // collapsed copy's course_index under `aliasIndexes` so a saved selection on any copy still
  // shows as chosen and selection round-trips. Genuinely different variants are left untouched.
  //
  // Result (deduped list + name counts) is cached per week_id; it depends only on the week's
  // recipe data, not on selection/filter state, so it's computed once per fetch rather than on
  // every render (each entry previously cost a JSON.stringify per recipe across the catalog).
  _dedupedFor(week) {
    if (!this._dedupeCache) this._dedupeCache = new Map();
    const cached = this._dedupeCache.get(week.week_id);
    if (cached) return cached;

    const bySig = new Map();
    for (const r of week.recipes || []) {
      const sig = this._recipeSignature(r);
      const existing = bySig.get(sig);
      if (existing) existing.aliasIndexes.push(r.course_index);
      else bySig.set(sig, { recipe: r, aliasIndexes: [r.course_index] });
    }
    const recipes = [...bySig.values()].map(({ recipe, aliasIndexes }) =>
      aliasIndexes.length > 1 ? { ...recipe, _aliasIndexes: aliasIndexes } : recipe
    );
    // After dedup, a name only recurs when copies genuinely DIFFER (variant/surcharge/nutrition).
    const nameCounts = {};
    for (const r of recipes) nameCounts[r.name] = (nameCounts[r.name] || 0) + 1;

    const entry = { recipes, nameCounts };
    this._dedupeCache.set(week.week_id, entry);
    return entry;
  }

  // Whether the user can edit the selection right now: the week must be editable AND the grid
  // must be in "show all meals" mode. In "show selected only" mode the unselected meals are
  // hidden, so toggling/quantity edits there are disabled to avoid editing against a partial view.
  _canEdit(week) {
    return this._isEditable(week) && !this._showSelectedOnly;
  }

  // Per-render constants shared by every tile of a week (avoids recomputing per tile).
  _tileContext(week) {
    const { nameCounts } = this._dedupedFor(week);
    const display = this._displaySelection(week);
    return {
      editable: this._canEdit(week),
      display,
      nameCounts,
      // Sold-out is only meaningful for a box you can still change. A delivered week already
      // arrived, so a lingering flag on its payload must never surface as a "Sold out" ribbon.
      showSoldOut: !this._isPast(week),
      // A tile is selected if its own key OR any collapsed-duplicate index is chosen. Uses the
      // same key as _savedSelection (course_index, or recipe_id when the meal has no index).
      sel: (r) =>
        display.has(this._selKey(r)) || (r._aliasIndexes || []).some((i) => display.has(i)),
    };
  }

  // The protein/variant filter bar. Shown only for current/future weeks (a past week just shows
  // what was delivered) and hidden while "Show selected only" is active (nothing to filter).
  _renderFilterBar(week) {
    if (!this._filtersApply(week)) return "";
    const allActive = this._proteinFilter.size === 0;
    const proteinChips = HelloFreshMealPlannerCard.PROTEIN_FILTERS.map((p) => {
      const on = this._proteinFilter.has(p);
      const color = PREFERENCE_COLORS[p] || "var(--secondary-text-color)";
      return `<button
          class="fbtn protein ${on ? "on" : ""}"
          data-action="filter-protein"
          data-protein="${p}"
          aria-pressed="${on ? "true" : "false"}"
        ><span class="fdot" style="background:${color}"></span>${p}</button>`;
    }).join("");
    return `
      <div class="filterbar">
        <div class="fgroup">
          <span class="flabel">Protein</span>
          <button
            class="fbtn ${allActive ? "on" : ""}"
            data-action="filter-protein-all"
            aria-pressed="${allActive ? "true" : "false"}"
          >All</button>
          ${proteinChips}
        </div>
        <div class="fgroup">
          <button
            class="fbtn ${this._showVariants ? "on" : ""}"
            data-action="toggle-variants"
            aria-pressed="${this._showVariants ? "true" : "false"}"
            title="Variants are the 2× protein, protein-swap and veggie-swap versions of a meal"
          >${this._showVariants ? "Hide variants" : "Show variants"}</button>
        </div>
      </div>
    `;
  }

  _renderGrid(week) {
    const { recipes } = this._dedupedFor(week);
    if (recipes.length === 0) {
      // A skipped/paused week never shipped, and a past week with no meals simply had none —
      // "no menu yet" only makes sense for an upcoming week whose menu hasn't published.
      const emptyMessage =
        week.is_skipped || this._isPaused(week)
          ? "This week was skipped — no meals selected."
          : this._isPast(week)
            ? "No meals selected for this week."
            : "No menu available for this week yet.";
      return `<div class="state">${emptyMessage}</div>`;
    }
    // Cache the deduped tiles (which carry `_aliasIndexes`) so click handling resolves the same
    // objects the grid rendered, not the raw recipe list.
    this._renderedRecipes = recipes;
    const ctx = this._tileContext(week);

    // When "show selected only" is on, hide unselected tiles. Uses the display selection, so a
    // meal stays visible while the user is toggling it off mid-edit (it disappears on the next
    // full render only if still deselected). Variant aliases are respected via sel().
    let visible = this._showSelectedOnly ? recipes.filter((r) => ctx.sel(r)) : recipes;
    if (this._showSelectedOnly && visible.length === 0) {
      return `<div class="state">No meals selected for this week yet.</div>`;
    }
    // Protein / variant filters (current & future weeks only). Selected meals always pass so an
    // active filter never hides a meal you've chosen.
    if (this._filtersApply(week)) {
      const filtered = visible.filter((r) => this._passesFilters(r, ctx));
      if (filtered.length === 0) {
        return `<div class="state">No meals match the current filter. Adjust the filters above to see more.</div>`;
      }
      visible = filtered;
    }

    // Selected meals lead (in their existing order); the remaining menu is grouped so a dish's
    // variants (2x fish, broccoli, green beans, …) sit together instead of scattered. Grouping
    // uses the variant-group key (the base dish's index) when present, so protein swaps that
    // carry a DIFFERENT name (Salmon vs. Cod in the same group) still cluster; it falls back to
    // the meal name otherwise. Array.sort is stable, so ties keep their catalog order.
    const groupKey = (r) => (r.variation_group != null ? `g:${r.variation_group}` : `n:${r.name || ""}`);
    // Within a group, the base ("Default") meal is the one whose own index IS the group key
    // (the modularity defaultCourseIndex). Sort it ahead of its variants; 0 for everything else.
    const isBase = (r) => (r.variation_group != null && r.course_index === r.variation_group ? 0 : 1);
    const ordered = visible.slice().sort((a, b) => {
      const selDelta = (ctx.sel(b) ? 1 : 0) - (ctx.sel(a) ? 1 : 0);
      if (selDelta !== 0) return selDelta;
      if (ctx.sel(a)) return 0; // both selected: preserve their existing order
      const groupDelta = groupKey(a).localeCompare(groupKey(b)); // group variants together
      if (groupDelta !== 0) return groupDelta;
      return isBase(a) - isBase(b); // within a group, the Default meal leads its variants
    });
    const cards = ordered.map((r) => this._renderRecipeTile(week, r, ctx)).join("");
    return `<div class="grid ${this._busy ? "busy" : ""}">${cards}</div>`;
  }

  // Render one recipe tile. `ctx` carries the per-week shared values from _tileContext.
  _renderRecipeTile(week, r, ctx) {
    const color = PREFERENCE_COLORS[r.preference] || "var(--secondary-text-color)";
    const isSelected = ctx.sel(r);
    const isVariant = ctx.nameCounts[r.name] > 1;
    // The modifier that actually names the difference ("2x Bacon", "Ground Turkey", ...).
    const variantTitle = r.variation_title || null;
    // Numbers line: protein + calories together makes double-protein variants obvious.
    const stats = [];
    if (r.protein_g != null) stats.push(`${Math.round(r.protein_g)}g protein`);
    if (r.calories_kcal != null) stats.push(`${Math.round(r.calories_kcal)} kcal`);
    // HelloFresh's own "you've had this before" count, and your star rating when you gave one.
    // Both are optional and only ever present on a minority of meals.
    if (r.delivered_count) {
      const when = r.last_delivered_week ? `, last ${r.last_delivered_week}` : "";
      stats.push(`Ordered ${r.delivered_count}×${when}`);
    }
    if (r.rating) stats.push(`You rated ${r.rating}/${r.rating_scale || 5}`);
    // A meatless dish carries no protein category, so surface an explicit "Veggie" chip driven
    // by preference (reliably set to "Veggie" for Veggie/Vegan meals) — the protein dot alone is
    // an unlabeled color. Meat meals identify their protein via that dot instead.
    const isVeggie = r.preference === "Veggie";
    // Meaningful variant/diet tags surfaced as chips (skip internal cuisine slugs). Drop the
    // Veggie/Vegan tags here since the dedicated chip below already conveys "meatless".
    const VARIANT_TAGS = ["double-protein", "GLP-1 Friendly", "High Protein", "Under 650 Calories", "Carb Conscious"];
    const tags = (r.tags || []).filter((t) => VARIANT_TAGS.includes(t));
    // Serving quantity for this tile (0 when not chosen). Drives the qty badge + stepper.
    const qty = isSelected ? this._displayedQuantity(week, r) : 0;
    const maxQty = HelloFreshMealPlannerCard.MAX_QUANTITY;
    const idxAttr = this._esc(String(r.course_index));
    const currency = this._account?.selected_plan_total_price_currency;
    // Only a handful of meals per week carry a promo clip (past weeks included — delivered
    // meals keep theirs), so the still image is always the base layer and the play affordance
    // is purely additive. URLs end in both .mp4 and .mov, but HelloFresh's CDN serves both as
    // `video/mp4`, so the player declares that type explicitly rather than letting the browser
    // reject a ".mov" it would actually have played.
    //
    // NOTE: HelloFresh attaches ONE clip to a whole recipe family, so every protein variant of
    // a dish points at the same video — a Rib-Eye clip plays for the Sirloin and Bavette
    // versions too. That is their data, not a mismatch introduced here.
    const videoUrl = safeMediaUrl(r.video_url);
    // A sold-out meal is greyed with a ribbon but stays tappable. The flag comes from the
    // anonymous regional catalog, which has not been confirmed to track per-customer
    // availability — so it is shown as advice, not enforced. Blocking on it would make a
    // selection HelloFresh might well accept impossible to build, with no override.
    const soldOut = r.is_sold_out === true && ctx.showSoldOut !== false;
    return `
      <div class="recipe ${isSelected ? "selected" : ""} ${ctx.editable ? "editable" : ""} ${isVariant ? "variant" : ""} ${soldOut ? "soldout" : ""}"
           data-index="${idxAttr}">
        <div class="imgwrap">
          ${r.image_url
            ? `<img loading="lazy" src="${this._esc(resizedImage(r.image_url, this._config.image_width))}" alt="${this._esc(r.name)}">`
            : `<div class="noimg"></div>`}
          ${soldOut ? `<div class="soldoutflag">Sold out</div>` : ""}
          ${videoUrl
            ? `<button class="playbtn" data-video="${idxAttr}" title="Play recipe video" aria-label="Play video for ${this._esc(r.name)}">▶</button>`
            : ""}
          ${ctx.editable
            ? `<button class="infobtn" data-info="${idxAttr}" title="Full recipe" aria-label="Full recipe for ${this._esc(r.name)}">ⓘ</button>`
            : ""}
          ${r.is_favorite === true ? `<div class="fav" title="In your HelloFresh cookbook">♥</div>` : ""}
          ${isSelected ? `<div class="check">✓</div>` : ""}
          ${qty > 1 ? `<div class="qtybadge">${qty}×</div>` : ""}
          ${r.surcharge_label ? `<div class="surcharge">${this._esc(this._fmtSurcharge(r.surcharge_label, currency))}</div>` : ""}
          ${variantTitle ? `<div class="variant-flag">${this._esc(variantTitle)}</div>` : ""}
        </div>
        <div class="meta">
          <div class="name"><span class="dot" style="background:${color}"></span>${this._esc(r.name)}</div>
          ${variantTitle ? `<div class="variation">${this._esc(variantTitle)}</div>` : ""}
          ${r.description ? `<div class="desc">${this._esc(r.description)}</div>` : ""}
          ${r.price != null
            ? `<div class="price">${this._esc(this._fmtPrice(r.price, r.currency || currency))}<span class="perserving"> / serving</span></div>`
            : ""}
          ${r.badge || isVeggie || tags.length
            ? `<div class="chips">
                 ${r.badge ? `<span class="rchip badge">${this._esc(r.badge)}</span>` : ""}
                 ${isVeggie ? `<span class="rchip veggie">🌱 Veggie</span>` : ""}
                 ${tags.map((t) => `<span class="rchip">${this._esc(t)}</span>`).join("")}
               </div>`
            : ""}
          ${stats.length ? `<div class="cals">${this._esc(stats.join(" · "))}</div>` : ""}
          ${ctx.editable && isSelected
            ? `<div class="stepper" data-stepper="${idxAttr}">
                 <button class="qbtn" data-qty="dec" data-index="${idxAttr}" title="Fewer servings">−</button>
                 <span class="qval">${qty}</span>
                 <button class="qbtn" data-qty="inc" data-index="${idxAttr}" ${qty >= maxQty ? "disabled" : ""} title="More servings">+</button>
                 <span class="qlabel">serving${qty === 1 ? "" : "s"}</span>
               </div>`
            : ""}
        </div>
      </div>`;
  }

  // A SINGLE delegated click listener on the card, attached once. It reads data attributes off
  // the clicked element's ancestry, so re-rendering inner HTML never re-attaches handlers.
  _bindDelegated(card) {
    card.addEventListener("click", (ev) => {
      const week = this._weeks ? this._weeks[this._cursor] : null;

      // Quantity steppers take priority — a +/- tap must not also toggle the tile.
      const qbtn = ev.target.closest(".qbtn");
      if (qbtn && !qbtn.disabled) {
        ev.stopPropagation();
        const recipe = this._findRenderedRecipe(qbtn.getAttribute("data-index"));
        if (week && recipe) {
          this._changeQuantity(week, recipe, qbtn.getAttribute("data-qty") === "inc" ? 1 : -1);
        }
        return;
      }

      // Play button takes priority too — watching the clip must not toggle the selection.
      const playbtn = ev.target.closest(".playbtn");
      if (playbtn) {
        ev.stopPropagation();
        const recipe = this._findRenderedRecipe(playbtn.getAttribute("data-video"));
        if (recipe) this._openVideo(recipe);
        return;
      }

      // NOTE: the video overlay is dismissed by its own listener in _openVideo, not here — it
      // lives beside <ha-card> in the shadow root, so its clicks never reach this handler.

      // Tapping a tile on a week you CAN'T change opens the full recipe instead. On an
      // editable week the tap still toggles the selection (below) — changing the meal is the
      // point there, and the ⓘ button opens the recipe without touching the selection.
      const infoBtn = ev.target.closest(".infobtn");
      if (infoBtn) {
        ev.stopPropagation();
        const recipe = this._findRenderedRecipe(infoBtn.getAttribute("data-info"));
        if (recipe) this._openRecipeDetail(recipe);
        return;
      }
      const readOnlyTile = ev.target.closest(".recipe:not(.editable)");
      if (readOnlyTile) {
        const recipe = this._findRenderedRecipe(readOnlyTile.getAttribute("data-index"));
        if (recipe) this._openRecipeDetail(recipe);
        return;
      }

      // Recipe tile tap (only meaningful on editable weeks).
      const tile = ev.target.closest(".recipe.editable");
      if (tile) {
        const recipe = this._findRenderedRecipe(tile.getAttribute("data-index"));
        if (week && recipe) this._toggleRecipe(week, recipe);
        return;
      }

      // Everything else routes through data-action.
      const actionEl = ev.target.closest("[data-action]");
      if (!actionEl) return;
      const action = actionEl.getAttribute("data-action");
      if (action === "prev") this._step(-1);
      else if (action === "next") this._step(1);
      else if (action === "refresh") this._fetchWeeks();
      else if (action === "skip" && week) this._toggleSkip(week);
      else if (action === "save" && week) this._saveSelection(week);
      else if (action === "cancel" && week) this._cancelEdit(week);
      else if (action === "goto-current") this._gotoCurrentWeek();
      else if (action === "goto-needs") this._gotoNeedsChoice();
      else if (action === "toggle-filter") this._toggleShowSelectedOnly();
      else if (action === "filter-protein")
        this._toggleProteinFilter(actionEl.getAttribute("data-protein"));
      else if (action === "filter-protein-all") this._clearProteinFilter();
      else if (action === "toggle-variants") this._toggleShowVariants();
      else if (action === "dismiss-downgrade") this._dismissDowngrade();
    });
  }

  // Open the shared recipe sheet for one meal. The module is loaded on demand, so the first
  // open may wait a moment for it; failures are swallowed because this is additive detail on
  // top of a menu that already rendered.
  async _openRecipeDetail(recipe) {
    const recipeId = recipe && recipe.recipe_id;
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

  // Response-returning service call shaped for the detail module (which knows nothing about
  // this card's config or hass handle).
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

  _findRenderedRecipe(index) {
    const week = this._weeks ? this._weeks[this._cursor] : null;
    const list = this._renderedRecipes || (week && week.recipes) || [];
    return list.find((r) => String(r.course_index) === index);
  }

  // ---- recipe video overlay -------------------------------------------------

  // Shows the clip in a lightbox rather than inline: only a few meals per week have one, and
  // an inline <video> per tile would load media for a 400-tile catalog week. The overlay is
  // appended to the shadow root (not the tile) so it is never clipped by the grid's overflow.
  _openVideo(recipe) {
    const url = safeMediaUrl(recipe.video_url);
    if (!url || !this.shadowRoot) return;
    this._closeVideo();

    const overlay = document.createElement("div");
    overlay.className = "videowrap";
    // HelloFresh's URLs end in both .mp4 and .mov, but the CDN serves BOTH as `video/mp4`
    // (verified: a .mov URL responds `content-type: video/mp4`) — they are H.264 in a
    // QuickTime-named container, which browsers play fine. So the source is declared as
    // video/mp4 explicitly rather than letting the browser guess from the ".mov" suffix and
    // refuse it. An earlier revision assumed .mov was simply unplayable and only offered a
    // link; that was wrong, and it is why some clips appeared broken.
    overlay.innerHTML = `
      <div class="videobox">
        <div class="videohead">
          <span class="videotitle">${this._esc(recipe.name)}</span>
          <button class="videoclose" title="Close" aria-label="Close video">✕</button>
        </div>
        <video class="videoel" controls autoplay playsinline>
          <source src="${this._esc(url)}" type="video/mp4">
        </video>
        <div class="videoerr" hidden>
          This clip could not be played here.
        </div>
        <a class="videofallback" href="${this._esc(url)}" target="_blank" rel="noopener noreferrer">
          Video not playing? Open it directly
        </a>
      </div>`;
    // Surface a real failure instead of a silent black box: <source> errors do not bubble to
    // the <video>, so listen on the element itself and on the source node.
    const videoEl = overlay.querySelector("video");
    const errEl = overlay.querySelector(".videoerr");
    const showError = () => {
      if (errEl) errEl.hidden = false;
    };
    if (videoEl) {
      videoEl.addEventListener("error", showError);
      const sourceEl = videoEl.querySelector("source");
      if (sourceEl) sourceEl.addEventListener("error", showError);
    }
    // The overlay is a SIBLING of <ha-card> in the shadow root, so the card's delegated click
    // listener never sees these clicks — it must bind its own. (An earlier revision relied on
    // that delegated handler and additionally stopped propagation on .videobox, which contains
    // the ✕ button; between the two, neither the backdrop nor the close button could dismiss
    // the lightbox.)
    //
    // The backdrop is matched by IDENTITY (ev.target === overlay), not by a data attribute on
    // the overlay: closest() walks *up* from the target, so a dismiss marker on the overlay
    // would match every click inside it — including one on the video — and close the lightbox
    // the moment you tried to use the player controls.
    overlay.addEventListener("click", (ev) => {
      if (ev.target === overlay || ev.target.closest(".videoclose")) {
        this._closeVideo();
      }
    });

    this.shadowRoot.appendChild(overlay);
    this._videoOverlay = overlay;
    // Escape closes, matching what a lightbox is expected to do.
    this._videoKeyHandler = (ev) => {
      if (ev.key === "Escape") this._closeVideo();
    };
    document.addEventListener("keydown", this._videoKeyHandler);
  }

  _closeVideo() {
    if (this._videoKeyHandler) {
      document.removeEventListener("keydown", this._videoKeyHandler);
      this._videoKeyHandler = null;
    }
    if (!this._videoOverlay) return;
    // Pause before removal so audio stops immediately even if the node lingers.
    const el = this._videoOverlay.querySelector("video");
    if (el) el.pause();
    this._videoOverlay.remove();
    this._videoOverlay = null;
  }

  // ---- small helpers -------------------------------------------------------

  // Normalize HelloFresh's surcharge label ("+7.99/serving") to a compact "+$7.99" in the
  // account's own currency. Leaves unrecognized formats unchanged.
  //
  // The label is HelloFresh's raw localized string, so non-English markets send a decimal
  // COMMA ("+7,99/portion"). Matching digits-and-dots against that stops at the comma and
  // yields "7", so the old hardcoded `+$${m[1]}` rendered "+7,99" as "+$7" -- both the wrong
  // symbol and a silently truncated amount. Normalize the comma before parsing, then let
  // toLocaleString apply the right symbol.
  _fmtSurcharge(label, currency) {
    const m = String(label).replace(/,/g, ".").match(/\+?\s*([\d.]+)/);
    if (!m) return String(label);
    const amount = Number.parseFloat(m[1]);
    if (!Number.isFinite(amount)) return String(label);
    try {
      return amount.toLocaleString(undefined, {
        style: "currency",
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
        currency: currency || "USD",
        signDisplay: "always",
        // "narrowSymbol" keeps "$7.99" rather than "US$7.99"/"CA$7.99" in the tight tile badge.
        currencyDisplay: "narrowSymbol",
      });
    } catch (_e) {
      return String(label);
    }
  }

  // The meal's actual per-serving price (distinct from the surcharge badge, which shows only
  // the premium uplift). Prefers the price's own currency — HelloFresh sends it per item —
  // over the account default.
  _fmtPrice(amount, currency) {
    if (typeof amount !== "number" || !Number.isFinite(amount)) return "";
    try {
      return amount.toLocaleString(undefined, {
        style: "currency",
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
        currency: currency || "USD",
        currencyDisplay: "narrowSymbol",
      });
    } catch (_e) {
      return String(amount);
    }
  }

  _relativeWeek(week) {
    if (!week.delivery_date) return "";
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const d = this._parseLocalDate(week.delivery_date);
    d.setHours(0, 0, 0, 0);
    const days = Math.round((d - today) / 86400000);
    if (days === 0) return "today";
    if (days < 0) return days === -1 ? "yesterday" : `${-days} days ago`;
    if (days < 7) return `in ${days} days`;
    const weeks = Math.round(days / 7);
    return weeks === 1 ? "next week" : `in ${weeks} weeks`;
  }

  _fmtDate(iso) {
    try {
      return this._parseLocalDate(iso).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
    } catch (_e) {
      return iso;
    }
  }

  _fmtDateTime(d) {
    try {
      return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
    } catch (_e) {
      return String(d);
    }
  }

  // Format a numeric price with its currency (falls back to a bare number for unknown codes).
  _fmtPrice(amount, currency) {
    const num = Number(amount);
    if (!Number.isFinite(num)) return String(amount);
    try {
      return num.toLocaleString(undefined, { style: "currency", currency: currency || "USD" });
    } catch (_e) {
      return num.toFixed(2);
    }
  }

  // Normalize an API status like "ON_THE_WAY" / "on_the_way" to "On The Way".
  _titleCase(value) {
    return String(value)
      .replace(/[_-]+/g, " ")
      .toLowerCase()
      .replace(/\b\w/g, (c) => c.toUpperCase());
  }

  _esc(value) {
    if (value === null || value === undefined) return "";
    return String(value).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[c]));
  }

  // Escape a value for use in an href/src. HTML-escaping alone does NOT neutralize a
  // javascript:/data: scheme (a tracking_url from the API is untrusted), so allow only
  // http(s) and return "" otherwise — an empty href renders a dead link, never executes.
  _safeUrl(value) {
    const raw = String(value ?? "").trim();
    if (!/^https?:\/\//i.test(raw)) return "";
    return this._esc(raw);
  }

  // Lazily build and cache the shared CSSStyleSheet (parsed once, reused by every instance).
  static _sheet() {
    if (!this.__sheet) {
      this.__sheet = new CSSStyleSheet();
      this.__sheet.replaceSync(this._styles());
      // The detail sheet's CSS lives in its own module, which loads asynchronously. Append it
      // when it arrives: the stylesheet object is already adopted by every instance, so
      // mutating it here styles overlays opened later without re-adopting anything.
      detailModule
        .then(({ DETAIL_STYLES }) => {
          this.__sheet.replaceSync(this._styles() + DETAIL_STYLES);
        })
        .catch(() => {
          /* The overlay simply stays unavailable; the rest of the card is unaffected. */
        });
    }
    return this.__sheet;
  }

  static _styles() {
    return `
      ha-card { overflow: hidden; }
      .card-header {
        display: flex; align-items: center; gap: 12px; padding: 16px 16px 0;
        font-size: 1.5em; font-weight: 500;
      }
      .card-header .logo {
        height: 40px; width: 40px; border-radius: 8px; object-fit: cover; flex: none;
      }
      .banner {
        display: flex; align-items: center; gap: 10px; cursor: pointer;
        margin: 12px 16px 0; padding: 10px 14px; border-radius: 10px;
        background: var(--warning-color, #ff9800); color: #fff;
      }
      .banner:hover { filter: brightness(1.05); }
      .banner-icon { font-size: 1.2em; flex: none; }
      .banner-text { font-size: 0.92em; }
      .downgrade-notice {
        display: flex; align-items: flex-start; gap: 10px;
        margin: 12px 0 0; padding: 10px 14px; border-radius: 10px;
        background: var(--warning-color, #ff9800); color: #fff;
      }
      .downgrade-icon { font-size: 1.2em; flex: none; line-height: 1.3; }
      .downgrade-text { font-size: 0.9em; flex: 1; line-height: 1.35; }
      .downgrade-dismiss {
        flex: none; background: transparent; border: none; color: inherit;
        font-size: 1em; cursor: pointer; padding: 0 2px; opacity: 0.85;
      }
      .downgrade-dismiss:hover { opacity: 1; }
      .content { padding: 8px 16px 16px; }
      .state { padding: 24px 8px; text-align: center; color: var(--secondary-text-color); }
      .state.error, .toast.error { color: var(--error-color); }
      .header { display: flex; align-items: center; gap: 8px; }
      .header .weekinfo { flex: 1; text-align: center; }
      .weektitle { font-size: 1.2em; font-weight: 600; }
      .weeksub { color: var(--secondary-text-color); font-size: 0.9em; }
      .skipped { color: var(--error-color); font-weight: 600; }
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
      .chip.warn { background: var(--warning-color, #ff9800); color: #fff; }
      .chip.preselected { background: var(--warning-color, #ff9800); color: #fff; font-weight: 700; }
      .chip.locked { opacity: 0.7; }
      .chip.editable { background: var(--primary-color); color: var(--text-primary-color, #fff); }
      .filterchip {
        border: 1px solid var(--divider-color); cursor: pointer; font: inherit; font-size: 0.8em;
      }
      .filterchip:hover { filter: brightness(0.95); }
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
      .fdot { width: 9px; height: 9px; border-radius: 50%; flex: none; }
      .orderbar {
        display: flex; flex-wrap: wrap; gap: 8px 20px; margin: 0 0 12px;
        padding: 10px 12px; border-radius: 10px;
        background: var(--secondary-background-color);
      }
      .oitem { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
      .olabel {
        font-size: 0.68em; text-transform: uppercase; letter-spacing: 0.04em;
        color: var(--secondary-text-color);
      }
      .oval { font-size: 0.9em; font-weight: 600; word-break: break-word; }
      .oval a { color: var(--primary-color); text-decoration: none; }
      .oval a:hover { text-decoration: underline; }
      .skipbtn {
        margin-left: auto; font-size: 0.85em; padding: 5px 12px; border-radius: 14px;
        border: 1px solid var(--divider-color); background: var(--card-background-color);
        color: var(--primary-text-color); cursor: pointer;
      }
      .skipbtn + .skipbtn { margin-left: 4px; }
      .skipbtn:disabled { opacity: 0.5; cursor: default; }
      .hint { font-size: 0.78em; color: var(--secondary-text-color); }
      .savebtn {
        margin-left: auto; font-size: 0.85em; padding: 5px 14px; border-radius: 14px;
        border: none; background: var(--primary-color); color: var(--text-primary-color, #fff);
        cursor: pointer; font-weight: 600;
      }
      .savebtn + .skipbtn { margin-left: 4px; }
      .savebtn:disabled { opacity: 0.5; cursor: default; }
      .grid {
        display: grid; gap: 12px; margin-top: 4px;
        /* auto-fill spreads the catalog across as many ~180px columns as the card width
           allows. In a panel-mode view (full page width) this becomes a wide multi-column
           grid; in a normal column it falls back to one or two columns. */
        grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
      }
      .grid.busy { opacity: 0.6; pointer-events: none; }
      .recipe {
        border: 2px solid transparent; border-radius: 10px; overflow: hidden;
        background: var(--secondary-background-color); transition: border-color 0.15s, transform 0.1s;
      }
      .recipe.editable { cursor: pointer; }
      .recipe.editable:hover { transform: translateY(-2px); }
      .recipe.selected { border-color: var(--primary-color); }
      .recipe.variant { border-style: dashed; }
      .recipe.variant.selected { border-style: solid; }
      .imgwrap { position: relative; aspect-ratio: 1 / 1; background: var(--divider-color); }
      .imgwrap img { width: 100%; height: 100%; object-fit: cover; display: block; }
      .noimg { width: 100%; height: 100%; }
      .check {
        position: absolute; top: 6px; right: 6px; width: 26px; height: 26px;
        background: var(--primary-color); color: var(--text-primary-color, #fff);
        border-radius: 50%; display: flex; align-items: center; justify-content: center;
        font-weight: 700;
      }
      /* A sold-out meal is shown rather than hidden (HelloFresh drops it from its own grid),
         so it is obvious why a dish you were considering is no longer choosable. Dimmed and
         non-interactive: the tile also loses its .editable class, so taps do nothing. */
      .recipe.soldout { opacity: 0.55; }
      .recipe.soldout .imgwrap img { filter: grayscale(1); }
      .soldoutflag {
        position: absolute; top: 50%; left: 0; right: 0; transform: translateY(-50%);
        background: rgba(0, 0, 0, 0.62); color: #fff; text-align: center;
        font-size: 0.78em; font-weight: 700; letter-spacing: 0.04em;
        text-transform: uppercase; padding: 4px 0;
      }
      .price { font-size: 0.82em; font-weight: 600; margin-top: 2px; }
      .perserving { font-weight: 400; color: var(--secondary-text-color); font-size: 0.92em; }
      /* Favorite heart sits top-LEFT so it never collides with the selection check/qty badge
         stacked down the right edge. Only rendered when is_favorite is exactly true. */
      .fav {
        position: absolute; top: 6px; left: 6px; width: 24px; height: 24px;
        background: rgba(0, 0, 0, 0.45); color: #ff5a7a; border-radius: 50%;
        display: flex; align-items: center; justify-content: center;
        font-size: 0.9em; line-height: 1; pointer-events: none;
      }
      /* Play affordance is centered over the still image; the image stays the base layer so a
         missing/unplayable video never leaves an empty tile. */
      .playbtn {
        position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
        width: 40px; height: 40px; border: none; border-radius: 50%; cursor: pointer;
        background: rgba(0, 0, 0, 0.55); color: #fff; font-size: 1em; line-height: 40px;
        padding: 0 0 0 3px; /* optical centering of the ▶ glyph */
        display: flex; align-items: center; justify-content: center;
      }
      .playbtn:hover { background: rgba(0, 0, 0, 0.75); }
      .playbtn:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
      /* On an editable week the tile tap toggles the selection, so opening the full recipe
         needs its own affordance. Bottom-left keeps it clear of the ♥ (top-right), the
         sold-out ribbon (top-left) and the centred ▶. */
      .infobtn {
        position: absolute; left: 6px; bottom: 6px;
        width: 24px; height: 24px; border: none; border-radius: 50%; cursor: pointer;
        background: rgba(0, 0, 0, 0.55); color: #fff; font-size: 0.85em; line-height: 1;
        padding: 0; display: flex; align-items: center; justify-content: center;
      }
      .infobtn:hover { background: rgba(0, 0, 0, 0.75); }
      .infobtn:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
      /* A read-only tile is now tappable (it opens the recipe), so it should say so. */
      .recipe:not(.editable) { cursor: pointer; }
      .videowrap {
        position: fixed; inset: 0; z-index: 9; background: rgba(0, 0, 0, 0.75);
        display: flex; align-items: center; justify-content: center; padding: 16px;
      }
      .videobox {
        background: var(--card-background-color, #fff); border-radius: 12px; overflow: hidden;
        max-width: min(560px, 92vw); width: 100%; box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
      }
      .videohead {
        display: flex; align-items: center; justify-content: space-between; gap: 8px;
        padding: 8px 8px 8px 12px;
      }
      .videotitle { font-weight: 600; font-size: 0.95em; }
      .videoclose {
        border: none; background: transparent; cursor: pointer; font-size: 1.1em;
        color: var(--secondary-text-color); padding: 4px 8px; border-radius: 6px;
      }
      .videoclose:hover { background: var(--divider-color); }
      .videoel { display: block; width: 100%; max-height: 70vh; background: #000; }
      .videoerr {
        padding: 8px 12px 0; font-size: 0.85em; color: var(--error-color, #db4437);
      }
      .videofallback {
        display: block; padding: 8px 12px; font-size: 0.82em;
        color: var(--secondary-text-color); text-decoration: none;
      }
      .videofallback:hover { text-decoration: underline; }
      .qtybadge {
        position: absolute; top: 38px; right: 6px; min-width: 26px; height: 22px; padding: 0 6px;
        box-sizing: border-box; background: var(--primary-color); color: var(--text-primary-color, #fff);
        border-radius: 11px; display: flex; align-items: center; justify-content: center;
        font-size: 0.78em; font-weight: 700;
      }
      .surcharge {
        position: absolute; bottom: 6px; right: 6px; padding: 2px 7px; border-radius: 10px;
        background: rgba(0,0,0,0.72); color: #fff; font-size: 0.72em; font-weight: 700;
      }
      .stepper {
        display: flex; align-items: center; gap: 8px; margin-top: 8px;
      }
      .qbtn {
        width: 28px; height: 28px; border-radius: 50%; border: 1px solid var(--divider-color);
        background: var(--card-background-color); color: var(--primary-text-color);
        font-size: 1.1em; font-weight: 700; line-height: 1; cursor: pointer; flex: none;
        display: flex; align-items: center; justify-content: center;
      }
      .qbtn:hover:not([disabled]) { border-color: var(--primary-color); color: var(--primary-color); }
      .qbtn[disabled] { opacity: 0.4; cursor: default; }
      .qval { font-weight: 700; min-width: 12px; text-align: center; }
      .qlabel { font-size: 0.78em; color: var(--secondary-text-color); }
      .variant-flag {
        position: absolute; top: 6px; left: 6px; max-width: calc(100% - 12px);
        padding: 2px 7px; border-radius: 10px;
        background: var(--warning-color, #ff9800); color: #fff; font-size: 0.68em; font-weight: 700;
        letter-spacing: 0.02em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      }
      .meta { padding: 8px; }
      .name { font-size: 0.9em; font-weight: 600; display: flex; align-items: baseline; gap: 6px; }
      .dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
      .variation {
        font-size: 0.8em; font-weight: 700; color: var(--warning-color, #ff9800); margin-top: 2px;
      }
      .desc { font-size: 0.8em; color: var(--secondary-text-color); margin-top: 2px; }
      .chips { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
      .rchip {
        font-size: 0.68em; padding: 1px 7px; border-radius: 10px;
        background: var(--secondary-background-color); color: var(--primary-text-color);
      }
      .rchip.badge { background: #333332; color: #fff; }
      .rchip.veggie { background: #4caf50; color: #fff; font-weight: 600; }
      .cals { font-size: 0.75em; color: var(--secondary-text-color); margin-top: 4px; font-weight: 600; }
      .toast {
        margin: 4px 16px 12px; padding: 8px 12px; border-radius: 8px;
        background: var(--secondary-background-color); font-size: 0.85em; text-align: center;
      }
      /* The "please wait while saving" banner must be seen the instant Save is pressed. The grid
         can be tall, so an inline toast at the bottom of the card sits below the fold and only
         scrolls into view once the reload shrinks the content — looking like it never appeared.
         Pin it to the top of the viewport instead so it's visible regardless of scroll position. */
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

customElements.define("hellofresh-meal-planner-card", HelloFreshMealPlannerCard);

// ---- visual config editor ---------------------------------------------------
// ha-form-based editor backing getConfigElement() above (previously referenced but never
// defined, which left the UI editor blank). `logo` is a boolean toggle here; a custom logo
// path set via YAML is preserved as long as the toggle stays on.

const EDITOR_SCHEMA = [
  { name: "title", selector: { text: {} } },
  { name: "logo", selector: { boolean: {} } },
  { name: "image_width", selector: { number: { min: 100, max: 1200, step: 50, mode: "box" } } },
  { name: "config_entry_id", selector: { text: {} } },
];
const EDITOR_LABELS = {
  title: "Title",
  logo: "Show HelloFresh logo",
  image_width: "Recipe image width (px)",
  config_entry_id: "Config entry ID",
};
const EDITOR_HELPERS = {
  image_width: "Cloudinary resize width for recipe images.",
  config_entry_id: "Only needed when multiple HelloFresh accounts are configured.",
};

class HelloFreshMealPlannerCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = { ...(config || {}) };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (this._form) this._form.hass = hass;
  }

  _render() {
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.schema = EDITOR_SCHEMA;
      this._form.computeLabel = (s) => EDITOR_LABELS[s.name] || s.name;
      this._form.computeHelper = (s) => EDITOR_HELPERS[s.name] || "";
      this._form.addEventListener("value-changed", (ev) => this._onFormChanged(ev));
      this.appendChild(this._form);
    }
    if (this._hass) this._form.hass = this._hass;
    this._form.data = {
      title: this._config.title != null ? this._config.title : "HelloFresh Meal Planner",
      logo: Boolean(this._config.logo),
      image_width: Number(this._config.image_width) || 400,
      config_entry_id: this._config.config_entry_id || "",
    };
  }

  _onFormChanged(ev) {
    ev.stopPropagation();
    const value = (ev.detail && ev.detail.value) || {};
    const config = { ...this._config, title: value.title || "HelloFresh Meal Planner" };
    if (value.logo) config.logo = typeof this._config.logo === "string" ? this._config.logo : true;
    else delete config.logo;
    if (Number(value.image_width) > 0) config.image_width = Number(value.image_width);
    else delete config.image_width;
    if (value.config_entry_id) config.config_entry_id = value.config_entry_id;
    else delete config.config_entry_id;
    this._config = config;
    this.dispatchEvent(
      new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true })
    );
  }
}

customElements.define("hellofresh-meal-planner-card-editor", HelloFreshMealPlannerCardEditor);

// Register with Lovelace's card picker.
window.customCards = window.customCards || [];
window.customCards.push({
  type: "hellofresh-meal-planner-card",
  name: "HelloFresh Meal Planner",
  description: "Browse HelloFresh weeks and select meals with images.",
  preview: false,
  documentationURL: "https://github.com/kedube/ha-hellofresh",
});

console.info(`%c HELLOFRESH-MEAL-PLANNER-CARD %c v${CARD_VERSION} `, "color:#fff;background:#91c11e;font-weight:700", "color:#91c11e;background:#fff");
