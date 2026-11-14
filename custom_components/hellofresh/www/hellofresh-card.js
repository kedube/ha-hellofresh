/*
 * HelloFresh Card (unified)
 * -------------------------
 * One Lovelace card for the whole HelloFresh experience, organised the way hellofresh.com is:
 *
 *   Overview  — the next box front and centre (date, deadline, picks, tracking, pantry list),
 *               what's coming up (as cards or a month calendar) and recent deliveries.
 *   Menu      — a week strip shared with Market; browse, filter and search the week's menu and
 *               build the box with + Add / servings steppers.
 *   Market    — the same week's add-ons.
 *   Recipes   — the public catalog and your cookbook.
 *   Week      — which day each meal in the box gets eaten: a meal plan the integration turns
 *               into a calendar and sensors.
 *   Account   — plan & billing (with the recurring plan controls), food preferences, spending.
 *
 * Menu and Market edit ONE pending box per week. The sticky box bar at the bottom shows what is
 * in it, a live price estimate, and saves meals and extras together in a single write.
 *
 * It replaced the seven classic single-purpose cards (since removed) and kept their filter
 * preferences and week selection. Everything here reads the integration's services. The card
 * reads again on the integration's own refresh interval (and its delivery-day watch) — sooner
 * would only return the same cached data — and sooner than that only after a load that failed,
 * or when Home Assistant comes back from a restart.
 *
 * Config:
 *   type: custom:hellofresh-card
 *   config_entry_id: <optional>   # only with more than one HelloFresh account
 *   title: HelloFresh             # header title ("" hides it)
 *   logo: true                    # bundled logo (false hides it, or a URL)
 *   views: [overview, menu, market, recipes, week, account]   # which sections, in order
 *   default_view: overview        # where the card opens (else the last section used)
 *   accent: brand                 # brand (HelloFresh green) | theme (HA primary colour)
 *   image_width: 400              # recipe image resize width
 *   recipes_limit: 50             # recipes per category in Recipes (1-200)
 *   recipes_collection: <slug>    # category Recipes opens on (default: Top rated)
 *   spending_months: 12           # months in the spending chart (1-24)
 *
 * A single entry in `views` gives a focused "breakout" card (no tab bar) — e.g. just Recipes on
 * a kitchen tablet.
 *
 * The integration also shows this card full screen as a sidebar panel (hellofresh-panel.js).
 * There it carries a `panel` attribute and a `narrow` property from Home Assistant: on phones it
 * runs edge to edge and its header gains the button that opens Home Assistant's sidebar.
 *
 * No build step: hand-written ES modules served from the integration's www/ directory. Every
 * module is imported with this file's own ?v= cache-bust so an upgrade never mixes versions.
 */

const CARD_VERSION = new URL(import.meta.url).searchParams.get("v") || "unknown";
const moduleUrl = (name) =>
  new URL(`./${name}?v=${encodeURIComponent(CARD_VERSION)}`, import.meta.url).href;

// AWAITED at module top level: every view renders synchronously from these on first paint.
//
// The shared modules load one at a time, each before anything else imports it. They all end
// with a top-level await, and WebKit (Safari, and every browser on an iPad) hands a module that
// is still in that await straight to a second importer, which then reads exports that aren't
// set yet: "Cannot access 'esc' before initialization" in hellofresh-card-ui.js, seen on
// iPadOS 26.7.1. Loaded in order, each has finished before its next importer asks for it. The
// views share nothing else (only the Account view imports the profile editor), so they can
// load together.
await import(moduleUrl("hellofresh-i18n.js"));
await import(moduleUrl("hellofresh-shared.js"));
const L = await import(moduleUrl("hellofresh-card-logic.js"));
const UI = await import(moduleUrl("hellofresh-card-ui.js"));
const [{ CARD_STYLES }, { OverviewView }, Menu, { RecipesView }, { WeekView }, { AccountView }] = await Promise.all([
  import(moduleUrl("hellofresh-card-styles.js")),
  import(moduleUrl("hellofresh-card-deliveries.js")),
  import(moduleUrl("hellofresh-card-menu.js")),
  import(moduleUrl("hellofresh-card-recipes.js")),
  import(moduleUrl("hellofresh-card-week.js")),
  import(moduleUrl("hellofresh-card-account.js")),
]);

// The recipe sheet. Only needed once a recipe is opened, so it loads in the background rather
// than blocking first paint.
const detailModule = import(moduleUrl("hellofresh-recipe-detail.js"));

const { esc, t, ht, html } = L;
const { icon } = UI;
const LOGO_URL = "/hellofresh/hellofresh-logo.png";

// Each section's tab; `label` reads the card's text (views.<key>) when shown.
export const VIEWS = [
  { key: "overview", icon: "mdi:home-variant-outline" },
  { key: "menu", icon: "mdi:silverware-fork-knife" },
  { key: "market", icon: "mdi:storefront-outline" },
  { key: "recipes", icon: "mdi:book-open-page-variant-outline" },
  { key: "week", icon: "mdi:calendar-week" },
  { key: "account", icon: "mdi:account-circle-outline" },
].map((view) => Object.defineProperty(view, "label", { enumerable: true, get: () => t(`views.${view.key}`) }));
const VIEW_KEYS = VIEWS.map((v) => v.key);

// Views that edit a week's box, and so carry the sticky box bar.
const BOX_VIEWS = new Set(["menu", "market"]);

// A control's name for putting focus back after a render (L.focusKey; a data-focus-key the
// view gave it wins), with a selector that finds the controls that could share it.
function focusName(el) {
  const keyed = el.getAttribute("data-focus-key");
  if (keyed) return { name: `key|${keyed}`, selector: `[data-focus-key="${CSS.escape(keyed)}"]` };
  const attributes = [...el.attributes].map((a) => [a.name, a.value]);
  const name = L.focusKey(el.localName, attributes);
  if (!name) return null;
  const selector = el.localName + L.focusNaming(attributes).map(([n, v]) => `[${n}="${CSS.escape(v)}"]`).join("");
  return { name, selector };
}

// Keep only known section keys, in the order given; nothing valid means every section.
export function normalizeViews(value) {
  const list = Array.isArray(value) ? value : typeof value === "string" ? [value] : [];
  const out = [];
  for (const raw of list) {
    const key = String(raw || "").trim().toLowerCase();
    if (VIEW_KEYS.includes(key) && !out.includes(key)) out.push(key);
  }
  return out.length ? out : VIEW_KEYS.slice();
}

class HelloFreshCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.shadowRoot.adoptedStyleSheets = [HelloFreshCard._sheet()];
    this._hass = null;
    this._config = null;
    // Data. `_weeks` is the browsable list (see logic.browsableWeeks); `_account` the get_weeks
    // account payload; `_summary` get_account_summary (header, banners, Account view).
    this._weeks = null;
    this._rawWeeks = null; // get_weeks as it came; a re-read week is merged in here
    this._account = null;
    this._summary = null;
    // The Week tab's meal plan (get_meal_plan).
    this._mealPlan = null;
    this._mealPlanError = null;
    this._mealPlanLoading = false;
    this._mealPlanQueued = false;
    this._mealPlanStamp = 0; // moves when a write hands the plan over (setMealPlan)
    this._loading = false;
    this._summaryLoading = false;
    this._refreshing = 0; // refresh() calls under way
    this._error = null;
    this._fetched = false;
    this._lastFetched = 0; // the last load that worked
    this._lastAttempt = 0; // the last load that started
    this._failCount = 0; // loads failed in a row (see L.loadDelayMs)
    this._retryTimer = null;
    this._recoverTimer = null;
    this._queuedWeek = undefined; // what the queued get_weeks reads: one week's id, or null for all
    this._fetchSeq = 0; // start number of the latest get_weeks (see BoxStore.committed)
    this._dataGen = 0; // moves when the card is given another account; older answers are dropped
    this._written = undefined; // the week written since the last read (see L.noteWrite)
    this._connected = true;
    this._unavailable = 0;
    this._busy = false;
    // UI state.
    this._view = "overview";
    this._weekId = null;
    this._views = {};
    this._sheetState = null;
    this._toastState = null;
    this._toastHolds = new Set(); // what keeps the toast up: "pointer" over it, "focus" in it
    this._toastReturn = null; // where the keyboard came from to reach the toast's Undo
    this._saving = null;
    this._deliveryOptions = null;
    this._deliveryLoading = false;
    this._planOptions = null; // get_plan_options: {status, options, error} once asked for
    this._pantry = {};
    // Rendering: the markup last written to each region (compared before writing again), the
    // parts waiting for the next frame, the open sheet's dialog and where focus goes back to.
    this._html = {};
    this._wanted = new Set();
    this._frame = 0;
    this._sheetDialog = null;
    this._sheetReturn = null;
    this._entityIds = null;
    this._entityIdsFor = null;
    this._entitySig = {};
    this._instanceId = Math.random().toString(36).slice(2);
    this._narrow = false; // set by the sidebar panel: Home Assistant hides its sidebar
    this._menuShown = false;
    // The card's words come from Home Assistant in the user's language (hellofresh-i18n.js);
    // until they are here the card shows its loading skeleton rather than flash English.
    this._textLoaded = false;
    this.box = new Menu.BoxStore(this);
    this._onSyncWeek = (ev) => this._receiveSyncedWeek(ev);
    this._onDataChanged = (ev) => this._receiveDataChanged(ev);
    this._onVisibility = () => this._onBecameVisible();
    this._onDocKeydown = (ev) => {
      if (ev.key !== "Escape") return;
      // The recipe sheet handles its own Escape; don't close what is underneath it too.
      if (this._detail && this._detail.isOpen) return;
      if (this._sheetState) this.closeSheet();
      else if (this._toastState && !this._saving) this._clearToast();
    };
  }

  // ---- Lovelace lifecycle ---------------------------------------------------------------------

  setConfig(config) {
    if (!config || typeof config !== "object") throw new Error("Invalid configuration");
    const before = this._config;
    const views = normalizeViews(config.views);
    this._config = {
      title: "HelloFresh",
      logo: true,
      image_width: 400,
      accent: "brand",
      ...config,
      views,
    };
    if (this._config.accent === "theme") this.setAttribute("accent", "theme");
    else this.removeAttribute("accent");
    const configured = String(config.default_view || "").toLowerCase();
    const stored = L.storageGet(L.viewStorageKey(this._config));
    if (views.includes(configured)) this._view = configured;
    else if (views.includes(stored)) this._view = stored;
    else this._view = views[0];
    this._entityIds = null;
    // The sidebar panel (and Lovelace) hand a live card its new config. For another account,
    // nothing of the old one may stay.
    if (before && L.accountKey(before) !== L.accountKey(this._config)) this._resetAccount();
    this._render();
    if (!this._hass) return;
    if (!this._fetched) {
      this._fetched = true;
      this.refresh({ quiet: true });
    } else if (this._needs().weeks && !this._weeks && !this._loading && !this._error) {
      // A section that reads the weeks was added to a card that had none.
      this.refresh({ quiet: true });
    }
  }

  // The card now shows another account: drop the old one's weeks and summary, the box edits
  // pending against its weeks (Save would send them to the new account), every view's caches,
  // the selected week and whatever was open, then load the new account from scratch. Answers
  // still on their way for the old account are ignored (_dataGen).
  _resetAccount() {
    this._dataGen += 1;
    clearTimeout(this._retryTimer);
    clearTimeout(this._recoverTimer);
    this._retryTimer = null;
    this._recoverTimer = null;
    if (this._sheetState) this.closeSheet();
    if (this._detail) this._detail.close();
    clearTimeout(this.box._previewTimer);
    this.box = new Menu.BoxStore(this);
    this._views = {};
    this._renderedView = null;
    this._weeks = null;
    this._rawWeeks = null;
    this._account = null;
    this._summary = null;
    this._mealPlan = null;
    this._mealPlanError = null;
    this._pantry = {};
    this._deliveryOptions = null;
    this._deliveryLoading = false;
    this._planOptions = null;
    // An Undo still on offer would write the old account's change into the new one.
    if (this._toastState) this._clearToast();
    this._error = null;
    this._weekId = null;
    this._loading = false;
    this._summaryLoading = false;
    this._summaryQueued = false;
    this._mealPlanLoading = false;
    this._mealPlanQueued = false;
    const waiting = this._resolveQueuedFetch;
    this._queuedFetch = null;
    this._resolveQueuedFetch = null;
    this._queuedWeek = undefined;
    if (waiting) waiting(false);
    this._busy = false;
    this._saving = null;
    this._written = undefined;
    this._lastFetched = 0;
    this._lastAttempt = 0;
    this._failCount = 0;
    this._entitySig = {};
    this._unavailable = 0;
    this._fetched = false;
  }

  set hass(hass) {
    const wasConnected = this._connected;
    this._hass = hass;
    this._connected = !hass || hass.connected !== false;
    const textLoaded = L.I18n.useHass(hass);
    if (textLoaded !== this._textLoaded) {
      this._textLoaded = textLoaded;
      if (textLoaded) this._render();
    }
    this.toggleAttribute("dark", Boolean(hass && hass.themes && hass.themes.darkMode));
    if (this._shell && this._wantsMenuButton() !== this._menuShown) this._renderChrome();
    if (hass && !this._fetched && this._config) {
      this._fetched = true;
      this.refresh({ quiet: true });
    } else if (hass && this._fetched && this._connected && !wasConnected) {
      // Home Assistant is back after a restart or a dropped connection.
      this._recover();
    }
    // hass is reassigned on EVERY state change in Home Assistant. Only the handful of this
    // integration's entities the card shows (prep lists, plan selects, health sensors) may
    // trigger work, only when their state actually moved, and only for the view showing them.
    const ids = this.entities();
    const stateOf = (id) => this.entityState(id);
    const sig = L.entitySignature(ids, stateOf);
    const changed = L.changedEntityGroups(this._entitySig, sig);
    this._entitySig = sig;
    // Every entity is unavailable while the integration sets up or reloads; fewer of them
    // unavailable than a moment ago means it is ready, and the load that failed meanwhile
    // can be tried again now.
    const unavailable = L.unavailableEntities(ids, stateOf);
    if (unavailable < this._unavailable && this._fetched) this._recover();
    this._unavailable = unavailable;
    if (changed.length) this._onEntitiesChanged(new Set(changed));
  }

  get hass() {
    return this._hass;
  }

  // The sidebar panel passes Home Assistant's `narrow` on (phones: the sidebar is hidden).
  set narrow(narrow) {
    this._narrow = Boolean(narrow);
    this.toggleAttribute("narrow", this._narrow);
    if (this._shell && this._wantsMenuButton() !== this._menuShown) this._renderChrome();
  }

  get narrow() {
    return this._narrow;
  }

  // In the sidebar panel, wherever Home Assistant isn't showing its sidebar (the same test its
  // own menu button uses), the header offers the way back to it.
  _wantsMenuButton() {
    if (!this.hasAttribute("panel")) return false;
    return this._narrow || Boolean(this._hass && this._hass.dockedSidebar === "always_hidden");
  }

  connectedCallback() {
    this._offText = L.I18n.onChange(() => this._onTextChanged());
    window.addEventListener(L.WEEK_SYNC_EVENT, this._onSyncWeek);
    window.addEventListener(L.DATA_CHANGED_EVENT, this._onDataChanged);
    document.addEventListener("visibilitychange", this._onVisibility);
    document.addEventListener("keydown", this._onDocKeydown);
    this._startTick();
    // A card taken out of the page and put back (Home Assistant keeps a dashboard around; edit
    // mode re-parents its cards) has lost its scroll offsets, so the next render treats its
    // view as just opened and the week strip goes back to the chosen week (see syncStrip).
    this._renderedView = null;
    if (this._fetched && !this._refreshIfStale()) this._render();
  }

  disconnectedCallback() {
    if (this._offText) this._offText();
    this._offText = null;
    window.removeEventListener(L.WEEK_SYNC_EVENT, this._onSyncWeek);
    window.removeEventListener(L.DATA_CHANGED_EVENT, this._onDataChanged);
    document.removeEventListener("visibilitychange", this._onVisibility);
    document.removeEventListener("keydown", this._onDocKeydown);
    this._stopTick();
    clearTimeout(this._toastTimer);
    clearTimeout(this._retryTimer);
    clearTimeout(this._recoverTimer);
    this._retryTimer = null;
    this._recoverTimer = null;
    if (this._frame) cancelAnimationFrame(this._frame);
    this._frame = 0;
    this._wanted.clear();
    this._toastState = null;
    this._toastHolds.clear();
    this._toastReturn = null;
    this._sheetState = null;
    this._sheetDialog = null;
    this._sheetReturn = null;
    if (this._shell) {
      this._shell.sheet.replaceChildren();
      this._write(this._shell.toast, "toast", "");
    }
    if (this._detail) this._detail.close();
    this._syncModal();
  }

  // The text arrived (or changed language): draw everything again in it.
  _onTextChanged() {
    if (!this._hass) return;
    this._textLoaded = L.I18n.useHass(this._hass);
    this._render();
    if (this._sheetState) this.renderSheet();
  }

  getCardSize() {
    return 12;
  }

  getGridOptions() {
    return { columns: "full", min_columns: 6, rows: "auto" };
  }

  getLayoutOptions() {
    return { grid_columns: "full" };
  }

  static getConfigElement() {
    return document.createElement("hellofresh-card-editor");
  }

  static getStubConfig() {
    return { type: "custom:hellofresh-card" };
  }

  // ---- accessors the views use -------------------------------------------------------------

  get config() {
    return this._config;
  }

  get weeks() {
    return this._weeks;
  }

  get account() {
    return this._account;
  }

  get summary() {
    return this._summary;
  }

  get busy() {
    return this._busy;
  }

  get loading() {
    return this._loading;
  }

  get error() {
    return this._error;
  }

  get view() {
    return this._view;
  }

  get lastFetched() {
    return this._lastFetched;
  }

  get fetchSeq() {
    return this._fetchSeq;
  }

  get selectedWeekId() {
    return this._weekId;
  }

  get planOptions() {
    return this._planOptions;
  }

  weekById(weekId) {
    return (this._weeks || []).find((w) => w.week_id === weekId) || null;
  }

  selectedWeek() {
    return this.weekById(this._weekId);
  }

  menuGraceWeeks() {
    const weeks = this._account && this._account.menu_grace_weeks;
    return Number.isFinite(weeks) ? weeks : 2;
  }

  hasView(key) {
    return Boolean(this._config && this._config.views.includes(key));
  }

  // ---- meal plan (the Week tab) ----------------------------------------------------------

  get mealPlan() {
    return this._mealPlan;
  }

  get mealPlanError() {
    return this._mealPlanError;
  }

  // Re-read the plan. A second request while one is in flight is queued behind it rather than
  // dropped: the one in flight began before whatever prompted the second (the plan changed on
  // another device), so its answer may not have it.
  async loadMealPlan(fresh = false) {
    if (!this._hass) return;
    if (this._mealPlanLoading) {
      this._mealPlanQueued = true;
      return;
    }
    const gen = this._dataGen;
    const stamp = this._mealPlanStamp;
    this._mealPlanLoading = true;
    let plan = null;
    let failed = null;
    try {
      plan = await this._load("meal-plan", fresh, () => this.call("get_meal_plan"));
    } catch (err) {
      failed = (err && err.message) || String(err);
    }
    if (gen !== this._dataGen) return;
    this._mealPlanLoading = false;
    if (stamp !== this._mealPlanStamp) {
      // A write handed over a newer plan while this read was in flight: keep that one.
      this._mealPlanQueued = true;
    } else if (failed) {
      this._mealPlanError = failed;
    } else {
      this._mealPlan = plan;
      this._mealPlanError = null;
    }
    if (this._view === "week" || this._view === "overview") this._invalidate("view");
    if (this._mealPlanQueued) {
      this._mealPlanQueued = false;
      this.loadMealPlan(true);
    }
  }

  // A write answered with the updated plan: show it without another round trip.
  setMealPlan(plan) {
    this._mealPlanStamp += 1;
    this._mealPlan = plan;
    this._mealPlanError = null;
    this.renderView();
  }

  // ---- services --------------------------------------------------------------------------

  _serviceData(data) {
    const payload = { ...(data || {}) };
    if (this._config && this._config.config_entry_id) {
      payload.config_entry_id = this._config.config_entry_id;
    }
    return payload;
  }

  // A response-returning hellofresh.* service. Home Assistant's own error toast is turned off
  // (the 5th argument): every caller shows the failure itself, once.
  async call(service, data) {
    try {
      const result = await this._hass.callService(
        "hellofresh",
        service,
        this._serviceData(data),
        undefined,
        false,
        true
      );
      return (result && result.response) || {};
    } finally {
      if (!L.isReadService(service)) this._noteWrite(service, data);
    }
  }

  // A hellofresh.* action without a response (skip, unskip, reschedule, refresh).
  async callAction(service, data) {
    try {
      await this._hass.callService("hellofresh", service, this._serviceData(data), undefined, false);
    } finally {
      this._noteWrite(service, data);
    }
  }

  // A write went out. Even one that failed may have gone through in part, so either way: no
  // card joins a read that began before it, and the next reload knows what to re-read.
  _noteWrite(service, data) {
    L.bumpDataEpoch(this._config);
    this._written = L.noteWrite(this._written, service, data);
  }

  // Tell sibling cards a write changed the account. When the writes since the last read were
  // all to one week's box (or `weekId` names it), they re-read that week alone.
  broadcastDataChanged({ weekId = null } = {}) {
    const noted = typeof this._written === "string" && this._written !== "all" ? this._written : null;
    L.bumpDataEpoch(this._config);
    L.broadcastDataChanged(this._config, this._instanceId, weekId || noted);
  }

  // ---- data --------------------------------------------------------------------------------

  _needs() {
    return L.dataNeeds(this._config ? this._config.views : []);
  }

  // A read shared with the other HelloFresh cards on the page (see L.sharedLoad).
  _load(what, fresh, start) {
    return L.sharedLoad(this._config, what, start, { fresh });
  }

  // Read everything the configured sections show.
  async refresh({ quiet = false } = {}) {
    if (!this._hass || !this._config) return;
    const gen = this._dataGen;
    // The day names and prices in "Change day" follow the plan: read them again when it next
    // opens, or now if it is open (it would otherwise lose its names the next time it is drawn).
    this._deliveryOptions = null;
    if (this.sheetKind === "reschedule") this._ensureDeliveryOptions();
    this._refreshing += 1;
    this._syncBusy();
    try {
      await Promise.all([this._fetchWeeks({ quiet }), this._fetchSummary()]);
      if (gen === this._dataGen && this._needs().mealPlan) await this.loadMealPlan();
    } finally {
      this._refreshing -= 1;
      this._syncBusy();
    }
    if (gen !== this._dataGen) return;
    const view = this._views[this._view];
    if (view && view.onRefresh) view.onRefresh();
  }

  // Resolves once the card holds data at least as new as this call: when a fetch is already in
  // flight (whose response may predate a write that just happened), one more fetch is queued
  // and the caller waits for THAT — so "save, then show what's saved" never shows the old box.
  // `weekId` re-reads that week alone.
  _fetchWeeks({ quiet = false, weekId = null } = {}) {
    if (!this._hass || !this._needs().weeks) return Promise.resolve(true);
    if (this._loading) {
      // Two callers wanting different weeks get every week.
      this._queuedWeek = this._queuedFetch && this._queuedWeek !== weekId ? null : weekId;
      if (!this._queuedFetch) {
        this._queuedFetch = new Promise((resolve) => {
          this._resolveQueuedFetch = resolve;
        });
      }
      return this._queuedFetch;
    }
    return this._runFetchWeeks(quiet, weekId);
  }

  async _runFetchWeeks(quiet, weekId = null, fresh = false) {
    const gen = this._dataGen;
    const seq = ++this._fetchSeq;
    // One week can only be merged into a list that is already here.
    const only = weekId && this._rawWeeks ? weekId : null;
    if (!only || this._written === only) this._written = undefined;
    this._loading = true;
    this._quietLoad = quiet;
    this._lastAttempt = Date.now();
    this._syncBusy();
    let ok = false;
    let failed = null;
    try {
      let response = await this._load(only ? `week:${only}` : "weeks", fresh, () =>
        this.call("get_weeks", only ? { week_id: only } : undefined)
      );
      const week = only ? (response.weeks || []).find((w) => w && w.week_id === only) : null;
      if (only && !week && gen === this._dataGen) {
        // The week is no longer on the account; only the whole list says what is.
        response = await this._load("weeks", true, () => this.call("get_weeks"));
      }
      if (gen !== this._dataGen) return false;
      this._rawWeeks = week ? L.mergeWeek(this._rawWeeks, week) : response.weeks || [];
      this._weeks = L.browsableWeeks(this._rawWeeks);
      this._account = response.account || null;
      this._error = null;
      this.box.reconcile(this._weeks, seq);
      for (const view of Object.values(this._views)) if (view.onData) view.onData();
      this._ensureWeek();
      ok = true;
    } catch (err) {
      failed = (err && err.message) || String(err);
    }
    if (gen !== this._dataGen) return false;
    if (failed) this._error = failed;
    this._loading = false;
    this._noteLoad(ok);
    this._syncBusy();
    this._invalidate("chrome");
    // Only a section drawn from the weeks has anything to redraw (L.viewShowsWeeks).
    if (L.viewShowsWeeks(this._view)) this._invalidate("view");
    if (this._queuedFetch) {
      const next = this._queuedWeek;
      const resolve = this._resolveQueuedFetch;
      this._queuedFetch = null;
      this._resolveQueuedFetch = null;
      this._queuedWeek = undefined;
      this._runFetchWeeks(true, next, true).then(resolve, resolve);
    }
    return ok;
  }

  // The outcome of the load the card stands on (its weeks; the summary for a card that shows no
  // weeks). A failure is not stamped as a load: it is tried again soon (L.RETRY_DELAYS_MS), and
  // once those tries are used up, at the usual interval by the tick.
  _noteLoad(ok) {
    clearTimeout(this._retryTimer);
    this._retryTimer = null;
    if (ok) {
      this._failCount = 0;
      this._lastFetched = Date.now();
      return;
    }
    this._failCount += 1;
    if (this._failCount > L.RETRY_DELAYS_MS.length) return;
    this._retryTimer = setTimeout(() => {
      this._retryTimer = null;
      // A hidden page, or a card no longer on it, waits: becoming visible or being put back runs
      // _refreshIfStale, which this failure makes due.
      if (document.visibilityState === "hidden" || !this.isConnected || this._loading || this._refreshing) return;
      this.refresh({ quiet: true });
    }, L.loadDelayMs(this._failCount, this._refetchIntervalMs()));
  }

  // Home Assistant reconnected, or the integration's entities came back: read now instead of at
  // the next interval. Entities return one at a time, so this waits a moment for the last.
  _recover() {
    clearTimeout(this._recoverTimer);
    this._recoverTimer = setTimeout(() => {
      this._recoverTimer = null;
      if (this.isConnected && this._hass && this._connected) this.refresh({ quiet: true });
    }, 1000);
  }

  // A quiet re-read after a write (views call this; the refresh icon spins, nothing dims). After
  // a write to one week's box, that week alone (`weekId`, or the week the card saw written).
  async reloadWeeks({ weekId = null } = {}) {
    const noted = typeof this._written === "string" && this._written !== "all" ? this._written : null;
    await this._fetchWeeks({ quiet: true, weekId: weekId || noted });
    this._fetchSummary();
  }

  async _fetchSummary(fresh = false) {
    if (!this._hass) return;
    if (this._summaryLoading) {
      this._summaryQueued = true; // same reasoning as the weeks queue: the in-flight one is older
      return;
    }
    const gen = this._dataGen;
    // A card without weeks (just Recipes, just Account) stands on its summary instead.
    const primary = !this._needs().weeks;
    this._summaryLoading = true;
    if (primary) this._lastAttempt = Date.now();
    let ok = false;
    const before = this._summary;
    try {
      const summary = await this._load("summary", fresh, () => this.call("get_account_summary"));
      if (gen !== this._dataGen) return;
      this._summary = summary;
      ok = true;
    } catch (err) {
      if (gen !== this._dataGen) return;
      // The summary only decorates (header, banners, Account); the weeks error covers outages.
      // eslint-disable-next-line no-console
      console.warn("hellofresh: account summary unavailable", err);
    }
    this._summaryLoading = false;
    if (primary) this._noteLoad(ok);
    this._invalidate("chrome");
    // Account shows the summary. The Menu's week head falls back on its number of people for a
    // week that doesn't say its own, so it is redrawn when that number arrives or changes.
    const people = L.weekServings(null, this._summary) !== L.weekServings(null, before);
    if (this._view === "account" || (this._view === "menu" && people)) this._invalidate("view");
    if (this._summaryQueued) {
      this._summaryQueued = false;
      this._fetchSummary(true);
    }
  }

  // Land on a sensible week: keep the current one, else the week another HelloFresh card last
  // selected when it is still ahead of us, else the next box — the one you'd edit — rather than
  // the nearest date, which the day after a delivery is the box already in your kitchen.
  _ensureWeek() {
    if (this._weekId && this.weekById(this._weekId)) return;
    const synced = L.loadSyncedWeekId(this._config);
    const syncedWeek = synced ? this.weekById(synced) : null;
    if (syncedWeek && !L.isPastWeek(syncedWeek)) {
      this._weekId = synced;
      return;
    }
    const weeks = this._weeks || [];
    const next = L.nextBoxWeek(weeks);
    if (next.week && next.upcoming) {
      this._weekId = next.week.week_id;
      return;
    }
    const idx = L.currentWeekIndex(weeks);
    this._weekId = idx >= 0 ? weeks[idx].week_id : weeks.length ? weeks[0].week_id : null;
  }

  _refetchIntervalMs() {
    return L.refetchIntervalMs(this._account || this._summary);
  }

  // Read again when the data is due: a refresh interval after the last load that worked, or the
  // retry delay after the last attempt when that one failed.
  _refreshIfStale() {
    if (!this._hass || !this._fetched || this._loading || this._refreshing) return false;
    const since = this._failCount ? this._lastAttempt : this._lastFetched;
    if (Date.now() - since < L.loadDelayMs(this._failCount, this._refetchIntervalMs())) return false;
    this.refresh({ quiet: true });
    return true;
  }

  _startTick() {
    if (this._tickTimer) return;
    this._tickTimer = setInterval(() => {
      if (document.visibilityState === "hidden") return;
      if (this._refreshIfStale()) return;
      this._updateCountdowns();
      // A deadline passing is the one change a tick can cause on its own: the week's Add
      // buttons, steppers, Save and Skip must go away without waiting for the next poll.
      let actions = this._renderedActions;
      try {
        actions = L.actionsKey(this._weeks);
      } catch (_err) {
        /* weeks it can't read: renderView shows that, once */
      }
      if (this._renderedActions !== actions) this.renderView();
    }, 60000);
  }

  _stopTick() {
    clearInterval(this._tickTimer);
    this._tickTimer = null;
  }

  _onBecameVisible() {
    if (document.visibilityState !== "visible") return;
    if (!this._refreshIfStale()) this._updateCountdowns();
  }

  // Deadline countdowns tick in place — no re-render, so scroll, focus and edits are untouched.
  _updateCountdowns() {
    if (!this.shadowRoot) return;
    for (const el of this.shadowRoot.querySelectorAll("[data-countdown]")) {
      const when = new Date(el.getAttribute("data-countdown"));
      if (Number.isNaN(when.getTime())) continue;
      el.textContent = L.countdown(when);
      const tone = L.deadlineTone(when);
      el.classList.toggle("hf-urgent", tone === "urgent");
      el.classList.toggle("hf-muted", tone !== "urgent");
    }
  }

  // ---- cross-card sync -------------------------------------------------------------------

  _receiveSyncedWeek(ev) {
    const detail = (ev && ev.detail) || {};
    if (!detail.weekId || !L.eventMatchesAccount(detail, this._config)) return;
    if (detail.weekId === this._weekId || !this.weekById(detail.weekId)) return;
    this._weekId = detail.weekId;
    if (BOX_VIEWS.has(this._view)) this._render();
  }

  _receiveDataChanged(ev) {
    const detail = (ev && ev.detail) || {};
    if (detail.source === this._instanceId) return;
    if (!L.eventMatchesAccount(detail, this._config) || !this._fetched) return;
    if (detail.weekId && this._rawWeeks) {
      // A sibling changed one week's box: that week and the figures around it, not every
      // week's whole menu again.
      this.reloadWeeks({ weekId: String(detail.weekId) });
      return;
    }
    this.refresh({ quiet: true });
  }

  // ---- Home Assistant entities -----------------------------------------------------------

  entities() {
    const hass = this._hass;
    if (!hass || !hass.entities || !this._config) return {};
    if (this._entityIds && this._entityIdsFor === hass.entities) return this._entityIds;
    const opts = { configEntryId: this._config.config_entry_id || null };
    const find = (key, domain) => L.findEntity(hass, key, { ...opts, domain });
    this._entityIds = {
      prep: [find("prep_list", "todo"), find("prep_list_week_2", "todo")].filter(Boolean),
      boxSize: find("box_size", "select"),
      deliveryDay: find("delivery_day", "select"),
      accessToken: find("access_token_minutes_remaining", "sensor"),
      refreshToken: find("refresh_token_days_remaining", "sensor"),
      writeActions: find("write_actions_available", "binary_sensor"),
      payloadShape: find("payload_shape_changed", "binary_sensor"),
      // Live tracking, only for accounts where HelloFresh drives its own vans (the Netherlands, Germany).
      trackingPhase: find("delivery_tracking_phase", "sensor"),
      trackingEta: find("delivery_tracking_eta", "sensor"),
      // The Week tab's Meals calendar: its plan_updated attribute moves with every plan change.
      meals: find("meals", "calendar"),
      // The HelloFresh site the account is on (its API base URL is the website's address).
      site: find("api_base_url", "sensor"),
    };
    this._entityIdsFor = hass.entities;
    return this._entityIds;
  }

  entityState(entityId) {
    return (entityId && this._hass && this._hass.states && this._hass.states[entityId]) || null;
  }

  // The account's HelloFresh website ("https://www.hellofresh.de"), for "do this on HelloFresh"
  // links; "" when Home Assistant doesn't have it.
  siteUrl() {
    const state = this.entityState(this.entities().site);
    return state ? L.safeUrl(state.state, { httpsOnly: true }) : "";
  }

  // What a change in the card's entities means for what is on screen (the groups are
  // L.entitySignature's). Drawn in the next frame: several entities often move together.
  _onEntitiesChanged(groups) {
    if (groups.has("pantry")) {
      // A prep list changed underneath us (a meal swap, a tick from another device): re-read it.
      for (const id of this.entities().prep || []) {
        const st = this._pantry[id];
        const s = this.entityState(id);
        if (st && st.items && s && st.sig !== s.last_updated) this.loadPantry(id, { force: true });
      }
      if (this._view === "overview" || this._view === "menu") this._invalidate("view");
    }
    if (this._view === "account" && (groups.has("plan") || groups.has("health"))) this._invalidate("view");
    if (groups.has("mealplan")) {
      // The plan changed somewhere else (another device, an automation): re-read it, and not by
      // joining a read that began before the change. Every card sees this same change; the
      // signature lets them still share the one read it starts.
      L.bumpDataEpoch(this._config, "mealplan", this._entitySig.mealplan);
      if (this._needs().mealPlan) this.loadMealPlan();
    }
    if (groups.has("tracking")) {
      if (this._view === "overview") this._invalidate("view");
      if (this.sheetKind === "delivery") this._invalidate("sheet");
    }
  }

  // The live delivery (Netherlands, Germany), from the tracking sensors; null when nothing is on the road.
  liveTracking() {
    const ids = this.entities();
    return L.liveTracking(this.entityState(ids.trackingPhase), this.entityState(ids.trackingEta));
  }

  // The prep-list entity covering a week, when the integration has one for it.
  pantryEntityFor(week) {
    if (!week) return null;
    for (const id of this.entities().prep || []) {
      const s = this.entityState(id);
      if (s && s.attributes && s.attributes.week_id === week.week_id) return id;
    }
    return null;
  }

  pantry(entityId) {
    return this._pantry[entityId] || null;
  }

  async loadPantry(entityId, { force = false } = {}) {
    if (!entityId || !this._hass) return;
    const st = this._pantry[entityId] || (this._pantry[entityId] = { items: null, loading: false, error: null, sig: null });
    const s = this.entityState(entityId);
    const sig = s ? s.last_updated : null;
    if (st.loading) {
      // The read in flight began before whatever asked for this one: read once more after it.
      if (force) st.queued = true;
      return;
    }
    if (!force && st.items && st.sig === sig) return;
    st.loading = true;
    try {
      const result = await this._hass.callService(
        "todo",
        "get_items",
        {},
        { entity_id: entityId },
        false,
        true
      );
      const response = (result && result.response) || {};
      st.items = (response[entityId] && response[entityId].items) || [];
      st.sig = sig;
      st.error = null;
    } catch (err) {
      st.error = (err && err.message) || String(err);
      st.items = st.items || [];
    } finally {
      st.loading = false;
      // Only what shows the list: the Overview's and the Menu's pantry button, and the sheet.
      if (this._view === "overview" || this._view === "menu") this._invalidate("view");
      if (this.sheetKind === "pantry") this._invalidate("sheet");
      if (st.queued) {
        st.queued = false;
        this.loadPantry(entityId, { force: true });
      }
    }
  }

  async togglePantryItem(entityId, uid) {
    const st = this._pantry[entityId];
    const item = st && st.items && st.items.find((i) => i.uid === uid);
    if (!item) return;
    const previous = item.status;
    item.status = previous === "completed" ? "needs_action" : "completed";
    this.renderView();
    if (this._sheetState && this._sheetState.kind === "pantry") this.renderSheet();
    try {
      // No Home Assistant error toast on top of the card's own (the 5th argument).
      await this._hass.callService(
        "todo",
        "update_item",
        { item: uid, status: item.status },
        { entity_id: entityId },
        false
      );
    } catch (err) {
      item.status = previous;
      this.toast(t("pantry.update_failed", { error: (err && err.message) || err }), true);
      this.renderView();
      if (this._sheetState && this._sheetState.kind === "pantry") this.renderSheet();
    }
  }

  // ---- week actions (shared by Overview and the week views) ------------------------------

  selectWeek(weekId, { render = true, broadcast = true } = {}) {
    if (!weekId) return;
    if (!this._weeks) {
      // Asked for before the weeks load (a link to a week): hold it; _ensureWeek keeps it once
      // they arrive, if the week is there.
      this._weekId = weekId;
      return;
    }
    if (!this.weekById(weekId)) return;
    if (weekId !== this._weekId) {
      this._weekId = weekId;
      this.box.clearDowngrade();
      const menu = this._views.menu;
      if (menu && menu.onWeekChange) menu.onWeekChange();
      const market = this._views.market;
      if (market && market.onWeekChange) market.onWeekChange();
    }
    if (broadcast) L.broadcastWeek(this._config, weekId);
    if (render) this._render();
  }

  navigate(view, { weekId = null } = {}) {
    if (!this.hasView(view)) return;
    if (weekId) this.selectWeek(weekId, { render: false });
    const changed = view !== this._view;
    this._view = view;
    L.storageSet(L.viewStorageKey(this._config), view);
    this._render();
    // The sidebar panel mirrors the section in its address (/hellofresh-app/menu).
    this.dispatchEvent(new CustomEvent("hellofresh-navigated", { detail: { view }, bubbles: true, composed: true }));
    if (changed && this._shell) {
      // Jumping from a hero button deep in a long page: bring the card's top back into view.
      const rect = this._shell.app.getBoundingClientRect();
      if (rect.top < 0) this._shell.app.scrollIntoView({ block: "start", behavior: "smooth" });
    }
  }

  confirmSkip(week) {
    if (!week) return;
    if (L.isSkipped(week)) {
      this.toggleSkip(week);
      return;
    }
    const date = L.fmtLongDate(week.delivery_date);
    this.openSheet({
      kind: "confirm",
      narrow: true,
      label: t("week.skip_this_week"),
      render: () => `
        ${UI.sheetHead(t("skip.title", { date }))}
        <div class="hf-sheetbody"><p class="hf-confirmtext" style="margin:0">${ht("skip.body")}</p></div>
        <div class="hf-sheetfoot"><button class="hf-btn" data-close-sheet>${ht("skip.keep")}</button>
          <button class="hf-btn primary" data-action="confirm-skip">${ht("week.skip_week")}</button></div>`,
      onClick: (_ev, el) => {
        if (el && el.dataset.action === "confirm-skip") {
          this.closeSheet();
          this.toggleSkip(week);
        }
      },
    });
  }

  toggleSkip(week) {
    if (week) return this._setSkipped(week, !L.isSkipped(week));
    return undefined;
  }

  // Skip a week, or unskip it. Its toast offers Undo, which writes the other one back; the undo
  // itself (`undoing`) shows "Undoing…" while it runs and offers nothing further.
  async _setSkipped(week, skip, { undoing = false } = {}) {
    if (this._busy || !week) return;
    const service = skip ? "skip_week" : "unskip_week";
    this._startWrite(undoing);
    this.renderView();
    let failed = null;
    try {
      await this.callAction(service, { week_id: week.week_id });
    } catch (err) {
      failed = (err && err.message) || String(err);
    }
    this._endWrite(undoing);
    // Siblings first, so their re-read of this week and ours are one request.
    if (!failed) this.broadcastDataChanged({ weekId: week.week_id });
    await this.reloadWeeks({ weekId: week.week_id });
    if (undoing) this.undoResult(failed);
    else if (failed) this.toast(t(skip ? "skip.skip_failed" : "skip.unskip_failed", { error: failed }), true);
    else {
      const weekId = week.week_id;
      this.toast(t(skip ? "skip.done" : "skip.restored"), false, {
        undo: this.weekUndo(weekId, () => this._setSkipped(this.weekById(weekId) || week, !skip, { undoing: true })),
      });
    }
  }

  // The names and prices of the delivery days ("Tuesday 8AM - 8PM", +$2.99), by handle. null
  // until read. A read that fails leaves it null, so the sheet shows dates alone this time and
  // the next time it opens asks again; refresh() forgets it, since both follow the plan.
  async _ensureDeliveryOptions() {
    if (this._deliveryOptions !== null || this._deliveryLoading) return;
    const gen = this._dataGen;
    this._deliveryLoading = true;
    let byHandle = null;
    try {
      const response = await this._load("delivery-options", false, () => this.call("get_delivery_options"));
      byHandle = {};
      for (const o of response.delivery_options || []) if (o && o.handle) byHandle[o.handle] = o;
    } catch (err) {
      // eslint-disable-next-line no-console
      console.warn("hellofresh: delivery options unavailable", err);
    }
    if (gen !== this._dataGen) return;
    this._deliveryLoading = false;
    this._deliveryOptions = byHandle;
    if (byHandle && this.sheetKind === "reschedule") this.renderSheet();
  }

  // The box sizes on offer (hellofresh.get_plan_options), for changing one week's number of
  // people. Read once and kept for as long as the card shows this account: they follow the plan,
  // not the week. A read that failed is made again the next time they are asked for.
  async loadPlanOptions() {
    const current = this._planOptions;
    if (!this._hass || (current && current.status !== "failed")) return;
    const gen = this._dataGen;
    this._planOptions = { status: "loading", options: null, error: null };
    if (this.sheetKind === "servings") this.renderSheet();
    let next;
    try {
      const response = await this._load("plan-options", false, () => this.call("get_plan_options"));
      next = { status: "ok", options: Array.isArray(response.options) ? response.options : [], error: null };
    } catch (err) {
      next = { status: "failed", options: null, error: (err && err.message) || String(err) };
    }
    if (gen !== this._dataGen) return;
    this._planOptions = next;
    if (this.sheetKind === "servings") this.renderSheet();
  }

  openReschedule(week) {
    if (!week || !L.canReschedule(week)) return;
    this.openSheet({
      kind: "reschedule",
      narrow: true,
      label: t("reschedule.title"),
      render: () => {
        const currency = this._account && this._account.selected_plan_total_price_currency;
        const buttons = L.rescheduleOptions(week, this._deliveryOptions, currency)
          .map(
            (o) => `<button class="hf-controlrow hf-btn ghost" style="height:auto;border-radius:12px;justify-content:space-between;padding:12px 14px;width:100%"
                data-action="pick-day" data-handle="${esc(o.handle)}" ${o.current || this._busy ? "disabled" : ""}>
                <span style="text-align:left"><span class="hf-controllabel">${esc(o.name)}</span>
                  ${o.date ? `<span class="hf-controlhint">${esc(o.date)}</span>` : ""}</span>
                <span>${o.current ? UI.pill(t("reschedule.current"), "ok") : o.price ? `<span class="hf-muted hf-small">${esc(o.price)}</span>` : ""}</span>
              </button>`
          )
          .join("");
        return `
          ${UI.sheetHead(t("reschedule.title"), t("reschedule.only_this_box", { date: L.fmtLongDate(week.delivery_date) }))}
          <div class="hf-sheetbody" style="gap:4px">${buttons || `<div class="hf-empty">${ht("reschedule.none")}</div>`}</div>`;
      },
      onClick: (_ev, el) => {
        if (el && el.dataset.action === "pick-day") {
          const handle = el.dataset.handle;
          this.closeSheet();
          this.reschedule(week, handle);
        }
      },
    });
    this._ensureDeliveryOptions();
  }

  // Move one week's box to another of its delivery options. Undo moves it back to the option it
  // was on (the one for its date before the change).
  async reschedule(week, handle, { undoing = false } = {}) {
    if (this._busy || !week || !handle) return;
    const backDate = undoing ? null : week.delivery_date;
    const back = undoing ? null : L.currentDeliveryOption(week);
    this._startWrite(undoing);
    this.renderView();
    let failed = null;
    try {
      await this.callAction("reschedule_week", { week_id: week.week_id, delivery_option: handle });
    } catch (err) {
      failed = (err && err.message) || String(err);
    }
    this._endWrite(undoing);
    if (!failed) this.broadcastDataChanged({ weekId: week.week_id });
    // The week, and the summary with it: the header and the next delivery follow the new day.
    await this.reloadWeeks({ weekId: week.week_id });
    if (undoing) this.undoResult(failed);
    else if (failed) this.toast(t("reschedule.failed", { error: failed }), true);
    else {
      const weekId = week.week_id;
      // HelloFresh offers a week's other days, so the old day is normally among the re-read
      // week's options rather than among those from before the move.
      const target = L.deliveryOptionForDate(this.weekById(weekId), backDate) || back;
      const undo = target && target !== handle
        ? this.weekUndo(weekId, () => this.reschedule(this.weekById(weekId) || week, target, { undoing: true }))
        : null;
      this.toast(t("reschedule.done"), false, { undo });
    }
  }

  openPantry(entityId, week) {
    if (!entityId) return;
    this.openSheet({
      kind: "pantry",
      narrow: true,
      label: t("pantry.list"),
      render: () => {
        const st = this.pantry(entityId);
        const items = (st && st.items) || [];
        const open = items.filter((i) => i.status !== "completed").length;
        const body = !st || (st.loading && !st.items)
          ? `<div class="hf-empty">${ht("common.loading")}</div>`
          : items.length
            ? UI.pantryList(entityId, items)
            : `<div class="hf-empty">${icon("mdi:check-all")}${ht("pantry.nothing")}</div>`;
        return `
          ${UI.sheetHead(t("pantry.title"), t("pantry.sub", { date: L.fmtLongDate(week && week.delivery_date), count: open }))}
          <div class="hf-sheetbody">${body}</div>`;
      },
      onClick: (_ev, el) => {
        if (el && el.dataset.action === "pantry-toggle") {
          this.togglePantryItem(el.dataset.entity, el.dataset.uid);
        }
      },
    });
    this.loadPantry(entityId);
  }

  // ---- recipe sheet & video ------------------------------------------------------------------

  async openRecipe(recipeId, selectionProvider = null) {
    if (!recipeId || !this._hass) return;
    this._detailSelection = selectionProvider;
    try {
      if (!this._detail) {
        const { RecipeDetailOverlay } = await detailModule;
        this._detail = new RecipeDetailOverlay({
          getRoot: () => this.shadowRoot,
          callService: (service, data) => this.call(service, data),
          getSelection: () => (this._detailSelection ? this._detailSelection() : null),
          // The card knows which control opened the sheet and can find it again after its own
          // re-renders; it also puts everything behind the sheet out of reach while it is open.
          captureFocus: () => this._captureFocus(),
          returnFocus: (state) => this._returnFocus(state),
          onToggle: () => this._syncModal(),
        });
      }
      await this._detail.open(recipeId);
    } catch (err) {
      // eslint-disable-next-line no-console
      console.warn("hellofresh: recipe detail unavailable", err);
    }
  }

  openVideo(recipe) {
    const url = L.safeMediaUrl(recipe && recipe.video_url);
    if (!url) return;
    // HelloFresh serves both .mp4 and .mov as video/mp4, so the type is declared rather than
    // guessed from the suffix (which made browsers refuse playable .mov clips).
    this.openSheet({
      kind: "video",
      label: recipe.name,
      render: () => `
        ${UI.sheetHead(recipe.name, "", "", { close: t("video.close") })}
        <div class="hf-videobox">
          <video controls autoplay playsinline><source src="${esc(url)}" type="video/mp4"></video>
          <div class="hf-videoerr" hidden>${ht("video.error")}</div>
          <a class="hf-videofallback" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${ht("video.fallback")}</a>
        </div>`,
    });
    const video = this._shell.sheet.querySelector("video");
    const err = this._shell.sheet.querySelector(".hf-videoerr");
    const show = () => {
      if (err) err.hidden = false;
    };
    if (video) {
      video.addEventListener("error", show);
      const source = video.querySelector("source");
      if (source) source.addEventListener("error", show);
    }
  }

  // ---- sheets & toast ------------------------------------------------------------------------
  //
  // A sheet is { kind, label, narrow, render(), onClick(ev, el), onChange(ev), onInput(ev),
  // onClose() }. Its dialog is built once when it opens; renderSheet() replaces what is inside.

  openSheet(sheet) {
    this._ensureShell();
    // Where focus goes back to: the control that opened the first sheet (one can open another).
    if (!this._sheetState) this._sheetReturn = this._captureFocus();
    this._sheetState = sheet;
    this._sheetDialog = null;
    this.renderSheet();
    if (this._sheetDialog) this._sheetDialog.focus({ preventScroll: true });
  }

  renderSheet() {
    this._ensureShell();
    this._wanted.delete("sheet");
    const host = this._shell.sheet;
    const sheet = this._sheetState;
    if (!sheet) {
      host.replaceChildren();
      this._sheetDialog = null;
      this._syncModal();
      return;
    }
    let dialog = this._sheetDialog;
    if (!dialog || !dialog.isConnected) {
      // Once per open. Rebuilding the backdrop and dialog on every update replayed their
      // entrance (they flashed with each pantry tick and each price that arrived) and threw
      // away whatever had focus inside.
      const wrap = document.createElement("div");
      wrap.className = "hf-sheetwrap";
      dialog = document.createElement("div");
      dialog.setAttribute("role", "dialog");
      dialog.setAttribute("aria-modal", "true");
      dialog.tabIndex = -1;
      wrap.append(dialog);
      host.replaceChildren(wrap);
      this._sheetDialog = dialog;
      this._syncModal();
    }
    dialog.className = `hf-sheet${sheet.narrow ? " narrow" : ""}${sheet.kind === "video" ? " hf-videobox" : ""}`;
    dialog.setAttribute("aria-label", sheet.label || "");
    const hadFocus = dialog.contains(this.shadowRoot.activeElement);
    const focus = this._captureFocus();
    const scroll = this._captureScroll(dialog);
    const body = dialog.querySelector(".hf-sheetbody");
    const top = body ? body.scrollTop : 0;
    dialog.innerHTML = sheet.render();
    const next = dialog.querySelector(".hf-sheetbody");
    if (next) next.scrollTop = top;
    this._restoreScroll(dialog, scroll);
    this._restoreFocus(focus);
    // The control that had focus is gone (a button that only shows while there is something to
    // pick): focus stays in the dialog rather than falling to the page behind it.
    if (hadFocus && !this.shadowRoot.activeElement) dialog.focus({ preventScroll: true });
  }

  closeSheet() {
    if (!this._sheetState) return;
    const sheet = this._sheetState;
    const back = this._sheetReturn;
    this._sheetState = null;
    this._sheetReturn = null;
    this._sheetDialog = null;
    if (this._shell) this._shell.sheet.replaceChildren();
    this._syncModal();
    if (sheet.onClose) sheet.onClose();
    this._returnFocus(back);
  }

  get sheetKind() {
    return this._sheetState ? this._sheetState.kind : null;
  }

  // While a sheet or the recipe sheet is open, everything behind it is inert: out of reach of
  // Tab, clicks and screen readers, which is what the dialogs' aria-modal says.
  _syncModal() {
    if (!this._shell) return;
    const recipe = Boolean(this._detail && this._detail.isOpen);
    this._shell.card.inert = recipe || Boolean(this._sheetState);
    this._shell.sheet.inert = recipe;
  }

  // A message at the bottom of the card. A change that can be taken back passes `undo`
  // ({weekId, run}, from weekUndo): the toast then offers Undo for L.UNDO_MS, and pressing it
  // runs `run`, the reverse write. The toast is a status region; its Undo is an ordinary button
  // in the Tab order (after the card's own controls) that never takes focus by itself, and
  // Escape dismisses it.
  toast(message, isError = false, { undo = null } = {}) {
    const offer = undo && !isError ? { ...undo, gen: this._dataGen } : null;
    this._toastState = {
      message,
      isError,
      undo: offer,
      ends: Date.now() + (offer ? L.UNDO_MS : isError ? 6000 : 3500),
    };
    this._renderToast();
    this._armToast();
  }

  // An Undo for a change to one week, or null once that week's deadline has passed: HelloFresh
  // would refuse the reverse write.
  weekUndo(weekId, run) {
    return L.undoWindowOpen(this.weekById(weekId)) ? { weekId, run } : null;
  }

  // How an undo went, said in its own toast (which offers no further undo).
  undoResult(failed) {
    if (failed) this.toast(t("undo.failed", { error: failed }), true);
    else this.toast(t("undo.done"));
  }

  // (Re)start the toast's clock for the time it has left, `least` at the least. It stands still
  // while the pointer is on the toast or keyboard focus is in it: its Undo must not vanish under
  // the hand about to press it.
  _armToast(least = 0) {
    clearTimeout(this._toastTimer);
    this._toastTimer = null;
    const toast = this._toastState;
    if (!toast || this._toastHolds.size) return;
    this._toastTimer = setTimeout(() => this._clearToast(), Math.max(least, toast.ends - Date.now()));
  }

  // Take the toast away. Keyboard focus that was on it goes back to where it came from.
  _clearToast() {
    clearTimeout(this._toastTimer);
    this._toastTimer = null;
    this._toastState = null;
    this._toastHolds.clear();
    this._renderToast();
  }

  // A write is starting: an Undo still on offer goes, since it would no longer take back the
  // latest change.
  _dropUndo() {
    if (this._toastState && this._toastState.undo) this._clearToast();
  }

  // The toast's Undo. One press takes the offer (it can't run twice), and only while the card is
  // free to write, for the account it was made in, and while the week can still be changed.
  _takeUndo() {
    const offer = this._toastState && this._toastState.undo;
    if (!offer || this._busy) return;
    this._clearToast();
    if (offer.gen !== this._dataGen) return;
    if (offer.weekId && !L.undoWindowOpen(this.weekById(offer.weekId))) {
      this.toast(t("undo.closed"), true);
      return;
    }
    offer.run();
  }

  // Skip, unskip and a new delivery day run without the saving banner (their buttons just go
  // quiet while the card is busy); undoing one shows "Undoing…" like any other undo.
  _startWrite(undoing) {
    if (undoing) this.setSaving(t("undo.undoing"));
    else {
      this._dropUndo();
      this._busy = true;
    }
  }

  _endWrite(undoing) {
    if (undoing) this.setSaving(null);
    else this._busy = false;
  }

  setSaving(text) {
    if (text) this._dropUndo();
    this._saving = text;
    this._busy = Boolean(text);
    this._renderToast();
  }

  // Written only when it changed, so a render while the toast is up (a load landing) leaves its
  // Undo, and the keyboard on it, alone.
  _renderToast() {
    if (!this._shell) return;
    const host = this._shell.toast;
    let markup = "";
    const toast = this._toastState;
    if (this._saving) {
      markup = `<div class="hf-toast saving" role="status"><span class="hf-spinner"></span>${esc(this._saving)}</div>`;
    } else if (toast) {
      const undo = toast.undo
        ? `<button class="hf-toastbtn" data-action="undo" data-focus-key="toast-undo">${ht("undo.action")}</button>`
        : "";
      markup = `<div class="hf-toast${toast.isError ? " error" : ""}" role="${toast.isError ? "alert" : "status"}">${icon(
        toast.isError ? "mdi:alert-circle-outline" : "mdi:check-circle-outline"
      )}<span class="hf-toasttext">${esc(toast.message)}</span>${undo}</div>`;
    }
    const focus = host.contains(this.shadowRoot.activeElement) ? this._captureFocus() : null;
    if (!this._write(host, "toast", markup)) return;
    if (focus) {
      // The toast that had the keyboard was replaced: the same control in the new one, else
      // back to where the keyboard came from.
      this._restoreFocus(focus);
      if (!this.shadowRoot.activeElement) this._returnFocus(this._toastReturn);
    }
    // A focused control taken out of the page sends no focusout, so the hold goes here.
    if (!host.contains(this.shadowRoot.activeElement)) this._toastHolds.delete("focus");
  }

  // Say something to a screen reader without showing it ("12 more recipes"). One status region
  // that stays in the card: a live region added along with its words is often not read out.
  announce(message) {
    this._ensureShell();
    const node = this._shell.live;
    node.textContent = "";
    // Emptied first and filled a moment later, so the same words twice are read twice.
    setTimeout(() => {
      node.textContent = String(message || "");
    }, 50);
  }

  // ---- rendering ----------------------------------------------------------------------------

  _ensureShell() {
    if (this._shell) return;
    const card = document.createElement("ha-card");
    card.innerHTML = `
      <div class="hf-app">
        <header class="hf-appbar js-appbar"></header>
        <nav class="hf-tabs js-tabs"></nav>
        <div class="hf-banners js-banners"></div>
        <main class="hf-main js-main"></main>
        <div class="hf-boxbar js-boxbar"></div>
      </div>`;
    const sheet = document.createElement("div");
    const toast = document.createElement("div");
    const live = document.createElement("div");
    live.className = "sr-only";
    live.setAttribute("role", "status");
    live.setAttribute("aria-live", "polite");
    // After the card and its sheet: Tab from the card's last control (Save, say) reaches the
    // toast's Undo next.
    this.shadowRoot.append(card, sheet, toast, live);
    this._shell = {
      card,
      sheet,
      toast,
      live,
      app: card.querySelector(".hf-app"),
      appbar: card.querySelector(".js-appbar"),
      tabs: card.querySelector(".js-tabs"),
      banners: card.querySelector(".js-banners"),
      main: card.querySelector(".js-main"),
      boxbar: card.querySelector(".js-boxbar"),
    };
    toast.addEventListener("click", (ev) => {
      if (ev.target.closest('[data-action="undo"]')) this._takeUndo();
    });
    // The toast holds still while it has the pointer or the keyboard (_armToast), and notes where
    // the keyboard came from, to send it back there when the toast goes.
    const hold = (what) => {
      this._toastHolds.add(what);
      this._armToast();
    };
    const release = (what) => {
      if (!this._toastHolds.delete(what)) return;
      if (!this._toastHolds.size) this._armToast(3000);
    };
    toast.addEventListener("pointerenter", () => hold("pointer"));
    toast.addEventListener("pointerleave", () => release("pointer"));
    toast.addEventListener("focusin", (ev) => {
      if (!toast.contains(ev.relatedTarget)) this._toastReturn = this._captureFocus(ev.relatedTarget);
      hold("focus");
    });
    toast.addEventListener("focusout", (ev) => {
      if (!toast.contains(ev.relatedTarget)) release("focus");
    });
    card.addEventListener("click", (ev) => this._onClick(ev));
    card.addEventListener("keydown", (ev) => this._onKeydown(ev));
    card.addEventListener("input", (ev) => this._activeView().onInput && this._activeView().onInput(ev));
    card.addEventListener("change", (ev) => this._activeView().onChange && this._activeView().onChange(ev));
    sheet.addEventListener("click", (ev) => {
      const wrap = sheet.querySelector(".hf-sheetwrap");
      if (ev.target === wrap || ev.target.closest("[data-close-sheet]")) {
        this.closeSheet();
        return;
      }
      const current = this._sheetState;
      if (current && current.onClick) current.onClick(ev, ev.target.closest("[data-action]"));
    });
    sheet.addEventListener("change", (ev) => {
      const current = this._sheetState;
      if (current && current.onChange) current.onChange(ev);
    });
    sheet.addEventListener("input", (ev) => {
      const current = this._sheetState;
      if (current && current.onInput) current.onInput(ev);
    });
    sheet.addEventListener("keydown", (ev) => {
      // Tab stays inside the open sheet; rows that are role="button" work with Enter and Space
      // here as they do in the card.
      L.keepTabInside(ev, this._sheetDialog, this.shadowRoot.activeElement);
      this._onKeydown(ev);
    });
  }

  _activeView() {
    const key = this._view;
    if (!this._views[key]) {
      const factories = {
        overview: () => new OverviewView(this),
        menu: () => new Menu.MealsView(this),
        market: () => new Menu.MarketView(this),
        recipes: () => new RecipesView(this),
        week: () => new WeekView(this),
        account: () => new AccountView(this),
      };
      this._views[key] = (factories[key] || factories.overview)();
    }
    return this._views[key];
  }

  _render() {
    if (!this.shadowRoot || !this._config) return;
    this._ensureShell();
    if (!this._textLoaded) {
      this._renderWaiting();
      return;
    }
    this._renderChrome();
    this.renderView();
    this._renderToast();
  }

  // Draw a part of the card ("chrome", "view", "sheet") in the next frame. Loads that land
  // together — a summary on the heels of the weeks, a to-do list re-read while its sheet is open
  // — then cost one render between them, and a render a click does itself meanwhile covers the
  // part it draws.
  _invalidate(part) {
    this._wanted.add(part);
    if (this._frame) return;
    this._frame = requestAnimationFrame(() => {
      this._frame = 0;
      const wanted = this._wanted;
      this._wanted = new Set();
      if (wanted.has("chrome")) this._renderChrome();
      if (wanted.has("view")) this.renderView();
      if (wanted.has("sheet") && this._sheetState) this.renderSheet();
    });
  }

  // Before hass (and its language's text) arrives: the loading skeleton alone, no words.
  _renderWaiting() {
    const shell = this._shell;
    this._html = {};
    shell.appbar.hidden = true;
    shell.tabs.hidden = true;
    shell.appbar.innerHTML = "";
    shell.tabs.innerHTML = "";
    shell.banners.innerHTML = "";
    shell.boxbar.innerHTML = "";
    shell.main.innerHTML = `<div class="hf-skeletons" aria-busy="true"><div class="hf-skeleton"></div>
      <div class="hf-skeleton"></div><div class="hf-skeleton"></div></div>`;
  }

  // Write a region's markup, unless it is what was written there last. Compared against that
  // string and not the DOM: innerHTML reads back re-serialised (`disabled` as `disabled=""`, a
  // non-breaking space as `&nbsp;`), so it never equals the markup that produced it and a
  // comparison with it rewrote the region every time. Returns whether it wrote.
  _write(el, name, markup) {
    if (this._html[name] === markup) return false;
    el.innerHTML = markup;
    this._html[name] = markup;
    return true;
  }

  // The header, tabs and banners. Each is only rewritten when its markup changed, so a fetch
  // or a summary landing leaves a focused tab alone; when one is rewritten (the current tab
  // moved), focus goes back to the same control.
  _renderChrome() {
    if (!this._shell || !this._config || !this._textLoaded) return;
    this._wanted.delete("chrome");
    const shell = this._shell;
    shell.tabs.setAttribute("aria-label", t("card.sections"));
    const focus = this._captureFocus();
    const appbar = this._renderAppbar();
    const tabs = this._renderTabs();
    this._write(shell.appbar, "appbar", appbar);
    shell.appbar.hidden = !appbar.trim();
    this._write(shell.tabs, "tabs", tabs);
    shell.tabs.hidden = !tabs.trim();
    this._write(shell.banners, "banners", this._renderBanners());
    this._syncBusy();
    this._restoreFocus(focus);
  }

  // A load running: the refresh button spins and stops taking clicks, and the view dims unless
  // the load is a quiet one. Patched in place, so a load starting or ending rebuilds nothing.
  // The button is aria-disabled rather than disabled, which would drop keyboard focus from it.
  _syncBusy() {
    if (!this._shell) return;
    const busy = this._loading || this._refreshing > 0;
    for (const button of this._shell.appbar.querySelectorAll('[data-action="refresh"]')) {
      button.classList.toggle("spin", busy);
      if (busy) button.setAttribute("aria-disabled", "true");
      else button.removeAttribute("aria-disabled");
    }
    this._shell.main.classList.toggle("reloading", this._loading && !this._quietLoad);
  }

  // Re-render the active view (and the box bar), keeping focus, caret and rail scroll positions.
  renderView() {
    if (!this._shell || !this._config || !this._textLoaded) return;
    this._wanted.delete("view");
    const main = this._shell.main;
    const view = this._activeView();
    const focus = this._captureFocus();
    const scroll = this._captureScroll(main);
    // A view rendered right after another one was showing (a tab switch, the first render).
    const entered = this._renderedView !== view;
    this._renderedView = view;
    // One section failing to draw (a payload shape nobody has seen) must not take the card down
    // with it, nor leave whoever awaits the load that triggered the render waiting.
    let failed = null;
    try {
      this._renderedActions = L.actionsKey(this._weeks);
      main.innerHTML = view.render();
    } catch (err) {
      failed = err;
      main.innerHTML = this._viewError(err);
      // eslint-disable-next-line no-console
      console.error("hellofresh: this section could not be shown", err);
    }
    main.classList.toggle("reloading", this._loading && !this._quietLoad);
    this._restoreScroll(main, scroll);
    this._restoreFocus(focus);
    if (!failed && view.afterRender) {
      try {
        view.afterRender(main, { entered });
      } catch (err) {
        // eslint-disable-next-line no-console
        console.error("hellofresh: this section could not finish drawing", err);
      }
    }
    this._renderBoxBar();
  }

  _viewError(err) {
    return `<div class="hf-empty" role="alert">${icon("mdi:alert-circle-outline")}${ht("card.view_failed", {
      error: (err && err.message) || String(err),
    })}
      <div class="hf-actions" style="justify-content:center;margin-top:12px">
      <button class="hf-btn" data-action="refresh">${ht("common.try_again")}</button></div></div>`;
  }

  _renderBoxBar() {
    if (!this._shell) return;
    const week = this.selectedWeek();
    const markup = BOX_VIEWS.has(this._view) && week ? Menu.renderBoxBar(this, week) : "";
    if (this._html.boxbar === markup) return;
    // The bar is rewritten while it is in use (the price estimate lands a moment after an edit):
    // Save or Discard keeps the keyboard's focus.
    const focus = this._captureFocus();
    this._write(this._shell.boxbar, "boxbar", markup);
    this._restoreFocus(focus);
  }

  // Public so views can refresh just the bar during a selection edit.
  renderBoxBar() {
    this._renderBoxBar();
  }

  // ---- focus and scroll across a re-render ----------------------------------------------------
  //
  // Rendering replaces markup, and with it the focused control. Before a render the card notes
  // which control has focus by a name its replacement will share (focusName above); after it,
  // if focus was lost, it goes back to the control of that name. This covers every button a
  // view draws, not just the ones a view gave a data-focus-key.

  // The parts of the card that are re-rendered separately.
  _regions() {
    const shell = this._shell;
    return [shell.appbar, shell.tabs, shell.banners, shell.main, shell.boxbar, this._sheetDialog, shell.toast].filter(Boolean);
  }

  // Every control in a region going by a name (two can: the header's refresh and a banner's
  // Retry), in page order.
  _named(region, target) {
    return [...region.querySelectorAll(target.selector)].filter((el) => {
      const other = focusName(el);
      return Boolean(other) && other.name === target.name;
    });
  }

  // `active` is the focused control unless given (where the keyboard was before it moved on).
  _captureFocus(active = this._shell ? this.shadowRoot.activeElement : null) {
    if (!active || !this._shell) return null;
    const region = this._regions().find((el) => el.contains(active));
    if (!region) return null;
    // The control itself, then the focusable things around it (the tile around its Add button):
    // when a render takes the control away, focus lands on the nearest one still there instead
    // of dropping to the top of the page.
    const names = [];
    for (let el = active; el && el !== region; el = el.parentElement) {
      if (el !== active && el.tabIndex < 0 && !el.hasAttribute("data-focus-key")) continue;
      const target = focusName(el);
      if (target) names.push({ ...target, index: this._named(region, target).indexOf(el) });
    }
    let selection = null;
    try {
      if (typeof active.selectionStart === "number") selection = [active.selectionStart, active.selectionEnd];
    } catch (_e) {
      /* input types without a caret */
    }
    return { active, region, names, selection };
  }

  // After a render: put focus back if the render took it away. Focus that is still somewhere in
  // the card — the control survived, or the view moved it — is left where it is.
  _restoreFocus(state) {
    if (!state || this.shadowRoot.activeElement) return;
    this._focusNamed(state);
  }

  _focusNamed(state) {
    for (const [i, target] of (state.names || []).entries()) {
      const peers = this._named(state.region, target);
      const el = peers[target.index] || peers[0];
      if (!el || el.disabled) continue;
      el.focus({ preventScroll: true });
      if (this.shadowRoot.activeElement !== el) continue;
      if (i === 0 && state.selection && typeof el.setSelectionRange === "function") {
        try {
          el.setSelectionRange(state.selection[0], state.selection[1]);
        } catch (_e) {
          /* input types without a caret */
        }
      }
      return true;
    }
    return false;
  }

  // Closing a sheet: back to the control that opened it. A render while the sheet was open has
  // usually replaced that element, so it is found again by name.
  _returnFocus(state) {
    if (!state) return;
    if (state.active && state.active.isConnected && !state.active.disabled) {
      state.active.focus({ preventScroll: true });
      if (this.shadowRoot.activeElement === state.active) return;
    }
    this._focusNamed(state);
  }

  _captureScroll(root) {
    const scroll = {};
    // A card out of the page has no layout: every offset reads 0, and putting that back would
    // count as a position the user chose (see syncStrip).
    if (!this.isConnected) return scroll;
    for (const el of root.querySelectorAll("[data-scroll-key]")) {
      scroll[el.getAttribute("data-scroll-key")] = [el.scrollLeft, el.scrollTop];
    }
    return scroll;
  }

  _restoreScroll(root, scroll) {
    for (const el of root.querySelectorAll("[data-scroll-key]")) {
      const saved = scroll[el.getAttribute("data-scroll-key")];
      if (saved) {
        // At once: the element is new and starts at its beginning, so a smooth scroll would
        // glide back from there after every re-render.
        el.scrollTo({ left: saved[0], top: saved[1], behavior: "instant" });
        el.setAttribute("data-scroll-restored", "");
      }
    }
  }

  _renderAppbar() {
    const cfg = this._config;
    const logo = cfg.logo === false || cfg.logo === "" ? "" : cfg.logo === true || cfg.logo == null ? LOGO_URL : String(cfg.logo);
    const title = cfg.title;
    const sub = L.cardSubtitle(this._summary, this._weeks);
    const menu = this._wantsMenuButton();
    this._menuShown = menu;
    if (!menu && !logo && !title && cfg.views.length < 2) return "";
    return `
      ${menu ? `<button class="hf-iconbtn hf-menubtn" data-action="sidebar" title="${ht("card.sidebar")}" aria-label="${ht("card.open_sidebar")}">${icon("mdi:menu")}</button>` : ""}
      ${logo ? `<img class="hf-logo" src="${esc(logo)}" alt="">` : ""}
      <div class="hf-apptitle">${title ? `<h1>${esc(title)}</h1>` : ""}${sub ? `<div class="hf-sub">${esc(sub)}</div>` : ""}</div>
      <div class="hf-appactions">
        <button class="hf-iconbtn" data-action="refresh" title="${ht("common.refresh")}"
          aria-label="${ht("common.refresh")}">${icon("mdi:refresh")}</button>
      </div>`;
  }

  _renderTabs() {
    const views = this._config.views;
    if (views.length < 2) return "";
    const needs = L.weeksNeedingAttention(this._weeks || []).length;
    return VIEWS.filter((v) => views.includes(v.key))
      .sort((a, b) => views.indexOf(a.key) - views.indexOf(b.key))
      .map((v) => {
        const current = v.key === this._view;
        const badge =
          v.key === "menu" && needs
            ? `<span class="hf-count" title="${ht("card.weeks_need_picks", { count: needs })}">${needs}</span>`
            : "";
        return `<button class="hf-tab" data-action="nav" data-view="${v.key}" ${
          current ? 'aria-current="page"' : ""
        }>${icon(v.icon)}<span>${esc(v.label)}</span>${badge}</button>`;
      })
      .join("");
  }

  _renderBanners() {
    const out = [];
    if (this._error && this._weeks) {
      out.push(`<div class="hf-notice tone-danger" role="alert">${icon("mdi:cloud-alert-outline")}
        <div class="hf-noticebody">${ht("banner.refresh_failed", { error: this._error })}</div>
        <button class="hf-btn sm" data-action="refresh">${ht("common.retry")}</button></div>`);
    }
    const s = this._summary;
    if (s && (s.payment_method_expiring || s.payment_method_expired)) {
      const card = html(`<strong>${esc(L.cardOnFile(s) || t("banner.payment_card"))}</strong>`);
      const date = L.fmtCardExpiry(s.payment_card_expiry);
      const key = `banner.card_${s.payment_method_expired ? "expired" : "expiring"}${date ? "_date" : ""}`;
      const text = ht(key, { card, date });
      out.push(`<div class="hf-notice ${s.payment_method_expired ? "tone-danger" : "tone-warn"}" role="alert">
        ${icon(s.payment_method_expired ? "mdi:credit-card-off-outline" : "mdi:credit-card-clock-outline")}<div class="hf-noticebody">${text}</div></div>`);
    }
    if (s && s.next_holiday_message) {
      const date = s.next_holiday_delivery_date
        ? ` ${ht("banner.holiday_date", { date: html(`<strong>${esc(L.fmtLongDate(s.next_holiday_delivery_date))}</strong>`) })}`
        : "";
      out.push(`<div class="hf-notice tone-info">${icon("mdi:calendar-star")}
        <div class="hf-noticebody">${esc(s.next_holiday_message)}${date}</div></div>`);
    }
    // A payment HelloFresh couldn't collect heads the two sections about the account's state of
    // play: the Overview and Account.
    if (this._view === "overview" || this._view === "account") {
      out.push(UI.paymentBanner(L.paymentProblem(s, this._weeks), this.siteUrl()));
    }
    return out.join("");
  }

  // The full-view loading / error / empty state for views that need weeks.
  weeksPlaceholder() {
    if (this._weeks) return "";
    if (this._error) {
      return `<div class="hf-empty">${icon("mdi:cloud-alert-outline")}${ht("card.load_failed", { error: this._error })}
        <div class="hf-actions" style="justify-content:center;margin-top:12px">
        <button class="hf-btn primary" data-action="refresh">${ht("common.try_again")}</button></div></div>`;
    }
    return `<div class="hf-skeletons" aria-label="${ht("common.loading_label")}"><div class="hf-skeleton"></div>
      <div class="hf-skeleton"></div><div class="hf-skeleton"></div></div>`;
  }

  // ---- events ---------------------------------------------------------------------------------

  _onClick(ev) {
    const el = ev.target.closest("[data-action]");
    const action = el ? el.getAttribute("data-action") : null;
    if (el && el.disabled) return;
    if (action === "nav") return this.navigate(el.getAttribute("data-view"));
    // One load at a time: the button shows the one already running (_syncBusy).
    if (action === "refresh") return this._loading || this._refreshing ? undefined : this.refresh();
    if (action === "sidebar") {
      // Home Assistant's own menu button fires this; its main view opens the sidebar drawer.
      return this.dispatchEvent(new CustomEvent("hass-toggle-menu", { bubbles: true, composed: true }));
    }
    if (action === "goto") {
      return this.navigate(el.getAttribute("data-view") || "menu", {
        weekId: el.getAttribute("data-week-id"),
      });
    }
    if (action === "pantry-toggle") {
      return this.togglePantryItem(el.getAttribute("data-entity"), el.getAttribute("data-uid"));
    }
    if (ev.target.closest(".js-boxbar")) return Menu.onBoxBarClick(this, ev, el);
    const view = this._activeView();
    if (view.onClick) view.onClick(ev, el);
  }

  // Tiles, rows and cards are focusable role="button" divs; Enter/Space on one (not on a real
  // control inside it) behaves like a click.
  _onKeydown(ev) {
    if (ev.key !== "Enter" && ev.key !== " ") return;
    const target = ev.target;
    if (!target || !target.matches || !target.matches('[role="button"]')) return;
    ev.preventDefault(); // Space must activate, not scroll the page
    target.click();
  }

  // ---- styles -------------------------------------------------------------------------------

  static _sheet() {
    if (!HelloFreshCard.__sheet) {
      const sheet = new CSSStyleSheet();
      sheet.replaceSync(CARD_STYLES);
      // The recipe sheet's own CSS ships with its module; the card's restyling of it comes after.
      detailModule
        .then(({ DETAIL_STYLES }) => sheet.replaceSync(DETAIL_STYLES + CARD_STYLES))
        .catch(() => {
          /* the recipe sheet stays unavailable; the rest of the card is unaffected */
        });
      HelloFreshCard.__sheet = sheet;
    }
    return HelloFreshCard.__sheet;
  }
}

// A page can load the card from two addresses (a dashboard resource that isn't this release's, and
// the sidebar panel's import), and an element can only be defined once (#15): the copy that came
// first is kept until the page is reloaded.
const DEFINED_BEFORE = Boolean(customElements.get("hellofresh-card"));
if (!DEFINED_BEFORE) customElements.define("hellofresh-card", HelloFreshCard);

// ---- visual config editor --------------------------------------------------------------------

// Built when shown, so its words are in the card's language (editor.*).
function editorSchema() {
  const views = VIEWS.map((v) => ({ value: v.key, label: v.label }));
  return [
    { name: "title", selector: { text: {} } },
    { name: "logo", selector: { boolean: {} } },
    { name: "views", selector: { select: { multiple: true, mode: "list", options: views } } },
    { name: "default_view", selector: { select: { mode: "dropdown", options: views } } },
    {
      name: "accent",
      selector: {
        select: {
          mode: "dropdown",
          options: [
            { value: "brand", label: t("editor.accent_brand") },
            { value: "theme", label: t("editor.accent_theme") },
          ],
        },
      },
    },
    { name: "image_width", selector: { number: { min: 100, max: 1200, step: 50, mode: "box" } } },
    { name: "config_entry_id", selector: { config_entry: { integration: "hellofresh" } } },
  ];
}

const EDITOR_HELPERS = new Set(["views", "default_view", "config_entry_id"]);

class HelloFreshCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = { ...(config || {}) };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    L.I18n.useHass(hass);
    if (this._form) this._form.hass = hass;
  }

  connectedCallback() {
    this._offText = L.I18n.onChange(() => this._render());
  }

  disconnectedCallback() {
    if (this._offText) this._offText();
    this._offText = null;
  }

  _render() {
    if (!this._config) return;
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.computeLabel = (s) => t(`editor.${s.name}`);
      this._form.computeHelper = (s) => (EDITOR_HELPERS.has(s.name) ? t(`editor.help.${s.name}`) : "");
      this._form.addEventListener("value-changed", (ev) => this._onFormChanged(ev));
      this.appendChild(this._form);
    }
    this._form.schema = editorSchema();
    if (this._hass) this._form.hass = this._hass;
    this._form.data = {
      title: this._config.title != null ? this._config.title : "HelloFresh",
      logo: this._config.logo !== false,
      views: normalizeViews(this._config.views),
      default_view: this._config.default_view || "",
      accent: this._config.accent === "theme" ? "theme" : "brand",
      image_width: Number(this._config.image_width) || 400,
      config_entry_id: this._config.config_entry_id || "",
    };
  }

  _onFormChanged(ev) {
    ev.stopPropagation();
    const value = (ev.detail && ev.detail.value) || {};
    const config = { ...this._config };
    // Home Assistant's text field reports an emptied optional field as undefined. The form always
    // carries a title, so a missing one is the user clearing it ("" hides the title), not a
    // request for the default.
    const title = value.title == null ? "" : String(value.title);
    if (title === "HelloFresh") delete config.title;
    else config.title = title;
    if (value.logo === false) config.logo = false;
    else if (typeof this._config.logo === "string") config.logo = this._config.logo;
    else delete config.logo;
    const views = normalizeViews(value.views);
    if (views.length === VIEW_KEYS.length && views.every((v, i) => v === VIEW_KEYS[i])) delete config.views;
    else config.views = views;
    if (value.default_view) config.default_view = value.default_view;
    else delete config.default_view;
    if (value.accent === "theme") config.accent = "theme";
    else delete config.accent;
    if (Number(value.image_width) > 0 && Number(value.image_width) !== 400) config.image_width = Number(value.image_width);
    else delete config.image_width;
    if (value.config_entry_id) config.config_entry_id = value.config_entry_id;
    else delete config.config_entry_id;
    this._config = config;
    this.dispatchEvent(new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true }));
  }
}

if (!customElements.get("hellofresh-card-editor")) {
  customElements.define("hellofresh-card-editor", HelloFreshCardEditor);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === "hellofresh-card")) {
  window.customCards.push({
    type: "hellofresh-card",
    name: "HelloFresh",
    description:
      "Your whole HelloFresh account in one card: next box, menu and Market with one save, recipes, plan and spending.",
    preview: false,
    documentationURL: "https://github.com/kedube/ha-hellofresh",
  });
}

if (DEFINED_BEFORE) {
  // eslint-disable-next-line no-console -- says why a new version isn't running yet
  console.info("hellofresh: this page already has the HelloFresh card, so version", CARD_VERSION, "takes over when the page is reloaded");
} else {
  // eslint-disable-next-line no-console -- the version banner, for bug reports
  console.info(
    `%c HELLOFRESH-CARD %c v${CARD_VERSION} `,
    "color:#fff;background:#91c11e;font-weight:700",
    "color:#91c11e;background:#fff"
  );
}
