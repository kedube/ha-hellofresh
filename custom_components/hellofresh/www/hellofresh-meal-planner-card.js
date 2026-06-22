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

const CARD_VERSION = "0.18.2";

// HelloFresh recipe images are Cloudinary URLs containing a `/q_auto/` transform segment.
// Inserting a width transform keeps grid thumbnails small/fast instead of loading full-size
// hero JPEGs. Unknown URL shapes are returned unchanged.
function resizedImage(url, width) {
  if (!url || !width) return url;
  return url.replace("/q_auto/", `/q_auto,w_${width}/`);
}

// Preference -> accent color for the little protein dot. Mirrors HelloFresh's own grouping.
const PREFERENCE_COLORS = {
  Poultry: "#f0a202",
  Beef: "#c1432f",
  Pork: "#e6789b",
  Seafood: "#2f8fc1",
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
    this._loading = false;
    this._error = null;
    this._busy = false; // a write (select/skip) is in flight
    this._fetched = false; // guard so we only auto-fetch once per hass attach
    this._hass = null;
    // Pending (unsaved) selection while the user edits a week, keyed by week_id -> Set of
    // course_index. Null entry / absent key means "show the saved selection". The set is
    // seeded from the server's is_selected on first edit, then mutated freely until the user
    // saves (submitting once) or it is discarded on refresh.
    this._pending = {};
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

  async _fetchWeeks() {
    if (!this._hass) return;
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
      this._cursor = this._defaultCursor(this._weeks);
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
        ? Date.parse(week.delivery_date) >= today.getTime()
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

  // Chronological sort key for a week (undated weeks sink to the end so they don't gate earlier
  // future weeks in _browsableWeeks).
  _weekSortKey(week) {
    const ms = week && week.delivery_date ? Date.parse(week.delivery_date) : NaN;
    return Number.isNaN(ms) ? Number.POSITIVE_INFINITY : ms;
  }

  // Land on the first week that still needs/allows a selection, else the first week.
  _defaultCursor(weeks) {
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

  _step(delta) {
    if (!this._weeks || this._weeks.length === 0) return;
    const count = this._weeks.length;
    this._cursor = (this._cursor + delta + count) % count;
    this._render();
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
      const d = new Date(w.delivery_date);
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
    if (target >= 0 && target !== this._cursor) {
      this._cursor = target;
      this._render();
    }
  }

  // Jump the cursor to the first week that still needs a meal selection (banner tap).
  _gotoNeedsChoice() {
    const needing = this._weeksNeedingChoice();
    if (needing.length === 0) return;
    const target = this._weeks.indexOf(needing[0]);
    if (target >= 0) {
      this._cursor = target;
      this._render();
    }
  }

  // Max servings the +/- stepper allows for one meal (HelloFresh's typical per-recipe cap).
  static get MAX_QUANTITY() {
    return 4;
  }

  // localStorage key for the "show selected only" view preference.
  static get FILTER_STORAGE_KEY() {
    return "hellofresh-meal-planner:show-selected-only";
  }

  _loadShowSelectedOnly() {
    try {
      return window.localStorage.getItem(HelloFreshMealPlannerCard.FILTER_STORAGE_KEY) === "1";
    } catch (_e) {
      return false; // private mode / storage disabled: default to showing all meals
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
  _savedSelection(week) {
    if (!this._savedSelCache) this._savedSelCache = new Map();
    const cached = this._savedSelCache.get(week.week_id);
    if (cached) return cached;
    const map = new Map();
    for (const r of week.recipes || []) {
      if (this._isSelected(r)) map.set(r.course_index, this._recipeQuantity(r));
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

  // Total servings in a selection map (sum of quantities) — this is what counts toward the
  // box's required-meal minimum, matching HelloFresh's box math.
  _servingsTotal(selectionMap) {
    let total = 0;
    for (const q of selectionMap.values()) total += q;
    return total;
  }

  // The quantity currently shown for a tile, accounting for collapsed-duplicate aliases.
  _displayedQuantity(week, recipe) {
    const display = this._displaySelection(week);
    const idx = (recipe._aliasIndexes || [recipe.course_index]).find((i) => display.has(i));
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
      this._nudgeIfOver(week, pending, 1);
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
      if (delta > 0) this._nudgeIfOver(week, pending, next - current);
      pending.set(idx, next);
    }
    this._renderSelectionChange(week, recipe);
  }

  // Selection edits are the hot path. When the "selected only" filter is OFF, a tap/stepper
  // can't change which tiles are visible, so update just the affected tile and the header
  // chips in place — no grid rebuild, no image teardown. With the filter ON, visibility can
  // change, so fall back to a full render.
  _renderSelectionChange(week, recipe) {
    if (this._showSelectedOnly || !this._shell) {
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

  // meals_required is the box's included serving count, not a hard cap — extras are allowed
  // (HelloFresh bills them as add-ons). Nudge once when an increase pushes total over the box.
  _nudgeIfOver(week, pending, added) {
    const required = week.meals_required;
    const total = this._servingsTotal(pending);
    if (required && total >= required) {
      this._flash(
        `Adding ${added} serving${added > 1 ? "s" : ""} past the included ${required} — extras may be charged as add-ons.`
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
    const required = week.meals_required;
    const servings = this._servingsTotal(pending);
    if (required && servings < required) {
      this._flash(`Choose at least ${required} servings before saving (${servings} selected).`);
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
    this._render();
    try {
      const data = { week_id: week.week_id, recipe_ids: recipeIds };
      if (Object.keys(quantities).length) data.quantities = quantities;
      if (this._config.config_entry_id) data.config_entry_id = this._config.config_entry_id;
      await this._hass.callService("hellofresh", "select_meals", data);
      delete this._pending[week.week_id]; // saved — drop the pending overlay
      this._flash("Meal selection updated.");
    } catch (err) {
      this._flash(`Selection failed: ${(err && err.message) || err}`, true);
    } finally {
      this._busy = false;
      await this._fetchWeeks(); // resync from the source of truth
    }
  }

  async _toggleSkip(week) {
    if (this._busy) return;
    this._busy = true;
    this._render();
    const service = week.is_skipped ? "unskip_week" : "skip_week";
    try {
      const data = { week_id: week.week_id };
      if (this._config.config_entry_id) data.config_entry_id = this._config.config_entry_id;
      await this._hass.callService("hellofresh", service, data);
    } catch (err) {
      this._flash(`${service} failed: ${(err && err.message) || err}`, true);
    } finally {
      this._busy = false;
      await this._fetchWeeks();
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
  //   • fewer meals are saved than the box requires (under-filled), or
  //   • HelloFresh has only *preselected* the meals (auto-picked), so the box is "full" of
  //     auto-picks the customer hasn't confirmed/reviewed yet.
  // A preselected week reports meals_selected == meals_required, so a pure count check misses it —
  // the meals_preselected/auto_picked flag is what surfaces those. Returns the list (cursor order).
  _weeksNeedingChoice() {
    return (this._weeks || []).filter((w) => {
      if (!this._isEditable(w)) return false;
      if (this._isPreselected(w)) return true;
      const required = w.meals_required;
      if (!required) return false;
      return this._savedSelection(w).size < required;
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
    this._shell.toast.innerHTML = this._toast
      ? `<div class="toast ${this._toast.isError ? "error" : ""}">${this._esc(this._toast.message)}</div>`
      : "";
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
    return `${this._renderHeader(week)}${this._renderOrder(week)}${this._renderGrid(week)}`;
  }

  // Per-week order/shipment summary shown above the recipe grid: order id, delivery status,
  // carrier, tracking number (linked when a tracking URL is present) and box total price.
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
        const value = order.tracking_url
          ? `<a href="${this._esc(order.tracking_url)}" target="_blank" rel="noopener">${num}</a>`
          : num;
        items.push(this._orderItem("Tracking", value, true));
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
    const required = week.meals_required;
    const display = this._displaySelection(week);
    // Count total servings (a 2× meal fills two slots), matching HelloFresh's box math.
    const selected = this._servingsTotal(display);
    const dirty = this._isDirty(week);
    const deadline = week.selection_deadline ? new Date(week.selection_deadline) : null;
    const canSave = !this._busy && (!required || selected >= required);
    const extra = required && selected > required ? selected - required : 0;
    return `
      <div class="statusrow">
        <span class="chip ${!required || selected >= required ? "ok" : "warn"}">
          ${selected}${required ? `/${required}` : ""} servings${extra ? ` (+${extra} extra)` : ""}${dirty ? " · unsaved" : ""}
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
        ${week.allowed_actions && week.allowed_actions.mealSwap !== undefined
          ? `<button class="skipbtn" data-action="skip" ${this._busy ? "disabled" : ""}>${week.is_skipped ? "Unskip week" : "Skip week"}</button>`
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
      // A tile is selected if its own index OR any collapsed-duplicate index is chosen.
      sel: (r) =>
        display.has(r.course_index) || (r._aliasIndexes || []).some((i) => display.has(i)),
    };
  }

  _renderGrid(week) {
    const { recipes } = this._dedupedFor(week);
    if (recipes.length === 0) {
      return `<div class="state">No menu available for this week yet.</div>`;
    }
    // Cache the deduped tiles (which carry `_aliasIndexes`) so click handling resolves the same
    // objects the grid rendered, not the raw recipe list.
    this._renderedRecipes = recipes;
    const ctx = this._tileContext(week);

    // When "show selected only" is on, hide unselected tiles. Uses the display selection, so a
    // meal stays visible while the user is toggling it off mid-edit (it disappears on the next
    // full render only if still deselected). Variant aliases are respected via sel().
    const visible = this._showSelectedOnly ? recipes.filter((r) => ctx.sel(r)) : recipes;
    if (this._showSelectedOnly && visible.length === 0) {
      return `<div class="state">No meals selected for this week yet.</div>`;
    }

    const ordered = visible.slice().sort((a, b) => (ctx.sel(b) ? 1 : 0) - (ctx.sel(a) ? 1 : 0));
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
    // Meaningful variant/diet tags surfaced as chips (skip internal cuisine slugs).
    const VARIANT_TAGS = ["double-protein", "GLP-1 Friendly", "High Protein", "Under 650 Calories", "Carb Conscious", "Veggie", "Vegan"];
    const tags = (r.tags || []).filter((t) => VARIANT_TAGS.includes(t));
    // Serving quantity for this tile (0 when not chosen). Drives the qty badge + stepper.
    const qty = isSelected ? this._displayedQuantity(week, r) : 0;
    const maxQty = HelloFreshMealPlannerCard.MAX_QUANTITY;
    const idxAttr = this._esc(String(r.course_index));
    return `
      <div class="recipe ${isSelected ? "selected" : ""} ${ctx.editable ? "editable" : ""} ${isVariant ? "variant" : ""}"
           data-index="${idxAttr}">
        <div class="imgwrap">
          ${r.image_url
            ? `<img loading="lazy" src="${this._esc(resizedImage(r.image_url, this._config.image_width))}" alt="${this._esc(r.name)}">`
            : `<div class="noimg"></div>`}
          ${isSelected ? `<div class="check">✓</div>` : ""}
          ${qty > 1 ? `<div class="qtybadge">${qty}×</div>` : ""}
          ${r.surcharge_label ? `<div class="surcharge">${this._esc(this._fmtSurcharge(r.surcharge_label))}</div>` : ""}
          ${variantTitle
            ? `<div class="variant-flag">${this._esc(variantTitle)}</div>`
            : isVariant ? `<div class="variant-flag">Variant</div>` : ""}
        </div>
        <div class="meta">
          <div class="name"><span class="dot" style="background:${color}"></span>${this._esc(r.name)}</div>
          ${variantTitle ? `<div class="variation">${this._esc(variantTitle)}</div>` : ""}
          ${r.description ? `<div class="desc">${this._esc(r.description)}</div>` : ""}
          ${r.badge || tags.length
            ? `<div class="chips">
                 ${r.badge ? `<span class="rchip badge">${this._esc(r.badge)}</span>` : ""}
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
    });
  }

  _findRenderedRecipe(index) {
    const week = this._weeks ? this._weeks[this._cursor] : null;
    const list = this._renderedRecipes || (week && week.recipes) || [];
    return list.find((r) => String(r.course_index) === index);
  }

  // ---- small helpers -------------------------------------------------------

  // Normalize HelloFresh's surcharge label ("+7.99/serving") to a compact "+$7.99". Leaves
  // unrecognized formats unchanged.
  _fmtSurcharge(label) {
    const m = String(label).match(/\+?\s*([\d.]+)/);
    return m ? `+$${m[1]}` : String(label);
  }

  _relativeWeek(week) {
    if (!week.delivery_date) return "";
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const d = new Date(week.delivery_date);
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
      return new Date(iso).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
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
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // Lazily build and cache the shared CSSStyleSheet (parsed once, reused by every instance).
  static _sheet() {
    if (!this.__sheet) {
      this.__sheet = new CSSStyleSheet();
      this.__sheet.replaceSync(this._styles());
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
      .cals { font-size: 0.75em; color: var(--secondary-text-color); margin-top: 4px; font-weight: 600; }
      .toast {
        margin: 4px 16px 12px; padding: 8px 12px; border-radius: 8px;
        background: var(--secondary-background-color); font-size: 0.85em; text-align: center;
      }
      .actions { text-align: center; margin-top: 8px; }
      .actions button { padding: 6px 16px; border-radius: 8px; cursor: pointer; }
    `;
  }
}

customElements.define("hellofresh-meal-planner-card", HelloFreshMealPlannerCard);

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
