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
 *   Account   — plan & billing (with the recurring plan controls), food preferences, spending.
 *
 * Menu and Market edit ONE pending box per week. The sticky box bar at the bottom shows what is
 * in it, a live price estimate, and saves meals and extras together in a single write.
 *
 * It replaces the seven classic cards, which still ship (deprecated, to be removed in a future
 * release) and share this card's filter preferences and week selection. Everything here reads
 * the same services they do; no new polling.
 *
 * Config:
 *   type: custom:hellofresh-card
 *   config_entry_id: <optional>   # only with more than one HelloFresh account
 *   title: HelloFresh             # header title ("" hides it)
 *   logo: true                    # bundled logo (false hides it, or a URL)
 *   views: [overview, menu, market, recipes, account]   # which sections, in order
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
const [L, { CARD_STYLES }, UI, { OverviewView }, Menu, { RecipesView }, { AccountView }] =
  await Promise.all([
    import(moduleUrl("hellofresh-card-logic.js")),
    import(moduleUrl("hellofresh-card-styles.js")),
    import(moduleUrl("hellofresh-card-ui.js")),
    import(moduleUrl("hellofresh-card-deliveries.js")),
    import(moduleUrl("hellofresh-card-menu.js")),
    import(moduleUrl("hellofresh-card-recipes.js")),
    import(moduleUrl("hellofresh-card-account.js")),
  ]);

// The shared recipe sheet (also used by the classic cards). Only needed once a recipe is
// opened, so it loads in the background rather than blocking first paint.
const detailModule = import(moduleUrl("hellofresh-recipe-detail.js"));

const { esc } = L;
const { icon } = UI;
const LOGO_URL = "/hellofresh/hellofresh-logo.png";

export const VIEWS = [
  { key: "overview", label: "Overview", icon: "mdi:home-variant-outline" },
  { key: "menu", label: "Menu", icon: "mdi:silverware-fork-knife" },
  { key: "market", label: "Market", icon: "mdi:storefront-outline" },
  { key: "recipes", label: "Recipes", icon: "mdi:book-open-page-variant-outline" },
  { key: "account", label: "Account", icon: "mdi:account-circle-outline" },
];
const VIEW_KEYS = VIEWS.map((v) => v.key);

// Views that edit a week's box, and so carry the sticky box bar.
const BOX_VIEWS = new Set(["menu", "market"]);

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
    this._account = null;
    this._summary = null;
    this._loading = false;
    this._summaryLoading = false;
    this._error = null;
    this._fetched = false;
    this._lastFetched = 0;
    this._refetchQueued = false;
    this._fetchSeq = 0; // start number of the latest get_weeks (see BoxStore.committed)
    this._busy = false;
    // UI state.
    this._view = "overview";
    this._weekId = null;
    this._views = {};
    this._sheetState = null;
    this._toastState = null;
    this._saving = null;
    this._deliveryOptions = null;
    this._pantry = {};
    this._entityIds = null;
    this._entityIdsFor = null;
    this._entitySig = {};
    this._instanceId = Math.random().toString(36).slice(2);
    this._narrow = false; // set by the sidebar panel: Home Assistant hides its sidebar
    this._menuShown = false;
    this.box = new Menu.BoxStore(this);
    this._onSyncWeek = (ev) => this._receiveSyncedWeek(ev);
    this._onDataChanged = (ev) => this._receiveDataChanged(ev);
    this._onVisibility = () => this._onBecameVisible();
    this._onDocKeydown = (ev) => {
      if (ev.key !== "Escape" || !this._sheetState) return;
      // The recipe sheet handles its own Escape; don't close the sheet underneath it too.
      if (this._detail && this._detail.isOpen) return;
      this.closeSheet();
    };
  }

  // ---- Lovelace lifecycle ---------------------------------------------------------------------

  setConfig(config) {
    if (!config || typeof config !== "object") throw new Error("Invalid configuration");
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
    this._render();
    if (this._hass && !this._fetched) {
      this._fetched = true;
      this.refresh({ quiet: true });
    }
  }

  set hass(hass) {
    this._hass = hass;
    this.toggleAttribute("dark", Boolean(hass && hass.themes && hass.themes.darkMode));
    if (this._shell && this._wantsMenuButton() !== this._menuShown) this._renderChrome();
    if (hass && !this._fetched && this._config) {
      this._fetched = true;
      this.refresh({ quiet: true });
    }
    // hass is reassigned on EVERY state change in Home Assistant. Only the handful of this
    // integration's entities the card shows (prep lists, plan selects, health sensors) may
    // trigger work, only when their state actually moved, and only for the view showing them.
    const sig = this._entitySignature();
    const changed = Object.keys(sig).filter((group) => sig[group] !== this._entitySig[group]);
    const first = !this._entitySig.pantry && !this._entitySig.plan && !this._entitySig.health && !this._entitySig.tracking;
    this._entitySig = sig;
    if (changed.length && !first) this._onEntitiesChanged(new Set(changed));
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
    window.addEventListener(L.WEEK_SYNC_EVENT, this._onSyncWeek);
    window.addEventListener(L.DATA_CHANGED_EVENT, this._onDataChanged);
    document.addEventListener("visibilitychange", this._onVisibility);
    document.addEventListener("keydown", this._onDocKeydown);
    this._startTick();
    if (this._weeks && !this._refreshIfStale()) this._render();
  }

  disconnectedCallback() {
    window.removeEventListener(L.WEEK_SYNC_EVENT, this._onSyncWeek);
    window.removeEventListener(L.DATA_CHANGED_EVENT, this._onDataChanged);
    document.removeEventListener("visibilitychange", this._onVisibility);
    document.removeEventListener("keydown", this._onDocKeydown);
    this._stopTick();
    clearTimeout(this._toastTimer);
    this._toastState = null;
    this._sheetState = null;
    if (this._shell) {
      this._shell.sheet.innerHTML = "";
      this._shell.toast.innerHTML = "";
    }
    if (this._detail) this._detail.close();
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

  // ---- services --------------------------------------------------------------------------

  _serviceData(data) {
    const payload = { ...(data || {}) };
    if (this._config && this._config.config_entry_id) {
      payload.config_entry_id = this._config.config_entry_id;
    }
    return payload;
  }

  // A response-returning hellofresh.* service.
  async call(service, data) {
    const result = await this._hass.callService(
      "hellofresh",
      service,
      this._serviceData(data),
      undefined,
      false,
      true
    );
    return (result && result.response) || {};
  }

  // A hellofresh.* action without a response (skip, unskip, reschedule, refresh).
  async callAction(service, data) {
    await this._hass.callService("hellofresh", service, this._serviceData(data));
  }

  broadcastDataChanged() {
    L.broadcastDataChanged(this._config, this._instanceId);
  }

  // ---- data --------------------------------------------------------------------------------

  async refresh({ quiet = false } = {}) {
    await Promise.all([this._fetchWeeks({ quiet }), this._fetchSummary()]);
    const view = this._views[this._view];
    if (view && view.onRefresh) view.onRefresh();
  }

  // Resolves once the card holds data at least as new as this call: when a fetch is already in
  // flight (whose response may predate a write that just happened), one more fetch is queued
  // and the caller waits for THAT — so "save, then show what's saved" never shows the old box.
  _fetchWeeks({ quiet = false } = {}) {
    if (!this._hass) return Promise.resolve();
    if (this._loading) {
      this._refetchQueued = true;
      if (!this._queuedFetch) {
        this._queuedFetch = new Promise((resolve) => {
          this._resolveQueuedFetch = resolve;
        });
      }
      return this._queuedFetch;
    }
    return this._runFetchWeeks(quiet);
  }

  async _runFetchWeeks(quiet) {
    const seq = ++this._fetchSeq;
    this._loading = true;
    this._quietLoad = quiet;
    this._renderChrome();
    this._markReloading();
    try {
      const response = await this.call("get_weeks");
      this._weeks = L.browsableWeeks(response.weeks || []);
      this._account = response.account || null;
      this._error = null;
      this.box.reconcile(this._weeks, seq);
      for (const view of Object.values(this._views)) if (view.onData) view.onData();
      this._ensureWeek();
    } catch (err) {
      this._error = (err && err.message) || String(err);
    } finally {
      this._loading = false;
      this._lastFetched = Date.now();
      this._render();
      if (this._refetchQueued) {
        this._refetchQueued = false;
        const resolve = this._resolveQueuedFetch;
        this._queuedFetch = null;
        this._resolveQueuedFetch = null;
        this._runFetchWeeks(true).then(resolve, resolve);
      }
    }
  }

  // A quiet re-read after a write (views call this; the refresh icon spins, nothing dims).
  async reloadWeeks() {
    await this._fetchWeeks({ quiet: true });
    this._fetchSummary();
  }

  async _fetchSummary() {
    if (!this._hass) return;
    if (this._summaryLoading) {
      this._summaryQueued = true; // same reasoning as the weeks queue: the in-flight one is older
      return;
    }
    this._summaryLoading = true;
    try {
      this._summary = await this.call("get_account_summary");
    } catch (err) {
      // The summary only decorates (header, banners, Account); the weeks error covers outages.
      // eslint-disable-next-line no-console
      console.warn("hellofresh: account summary unavailable", err);
    } finally {
      this._summaryLoading = false;
      this._renderChrome();
      if (this._view === "account") this.renderView();
      if (this._summaryQueued) {
        this._summaryQueued = false;
        this._fetchSummary();
      }
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

  _refreshIfStale() {
    if (!this._hass || !this._fetched || this._loading) return false;
    if (Date.now() - this._lastFetched < this._refetchIntervalMs()) return false;
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
      // buttons, steppers and Save must go away without waiting for the next poll.
      if (this._renderedEditable !== this._currentEditability()) this.renderView();
    }, 60000);
  }

  _currentEditability() {
    return (this._weeks || []).map((w) => (L.isWeekEditable(w) ? "1" : "0")).join("");
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
      // Live tracking, only for accounts where HelloFresh drives its own vans (the Netherlands).
      trackingPhase: find("delivery_tracking_phase", "sensor"),
      trackingEta: find("delivery_tracking_eta", "sensor"),
    };
    this._entityIdsFor = hass.entities;
    return this._entityIds;
  }

  entityState(entityId) {
    return (entityId && this._hass && this._hass.states && this._hass.states[entityId]) || null;
  }

  // One signature per group of entities, so a token-countdown tick never re-renders the menu.
  _entitySignature() {
    const ids = this.entities();
    const sign = (list) =>
      list
        .filter(Boolean)
        .map((id) => {
          const s = this.entityState(id);
          return s ? `${id}=${s.state}@${s.last_updated}` : `${id}=-`;
        })
        .join("|");
    return {
      pantry: sign(ids.prep || []),
      plan: sign([ids.boxSize, ids.deliveryDay]),
      health: sign([ids.accessToken, ids.refreshToken, ids.writeActions, ids.payloadShape]),
      tracking: sign([ids.trackingPhase, ids.trackingEta]),
    };
  }

  _onEntitiesChanged(groups) {
    if (groups.has("pantry")) {
      // A prep list changed underneath us (a meal swap, a tick from another device): re-read it.
      for (const id of this.entities().prep || []) {
        const st = this._pantry[id];
        const s = this.entityState(id);
        if (st && st.items && s && st.sig !== s.last_updated) this.loadPantry(id, { force: true });
      }
      if (this._view === "overview" || this._view === "menu") this.renderView();
    }
    if (this._view === "account" && (groups.has("plan") || groups.has("health"))) this.renderView();
    if (groups.has("tracking")) {
      if (this._view === "overview") this.renderView();
      if (this.sheetKind === "delivery") this.renderSheet();
    }
  }

  // The live delivery (Netherlands), from the tracking sensors; null when nothing is on the road.
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
    if (st.loading || (!force && st.items && st.sig === sig)) return;
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
      this.renderView();
      if (this._sheetState && this._sheetState.kind === "pantry") this.renderSheet();
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
      await this._hass.callService(
        "todo",
        "update_item",
        { item: uid, status: item.status },
        { entity_id: entityId }
      );
    } catch (err) {
      item.status = previous;
      this.toast(`Couldn't update the list: ${(err && err.message) || err}`, true);
      this.renderView();
      if (this._sheetState && this._sheetState.kind === "pantry") this.renderSheet();
    }
  }

  // ---- week actions (shared by Overview and the week views) ------------------------------

  selectWeek(weekId, { render = true, broadcast = true } = {}) {
    if (!weekId || !this.weekById(weekId)) return;
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
      label: "Skip this week",
      render: () => `
        <div class="hf-sheethead"><div class="hf-sheettitle"><h2>Skip ${esc(date)}?</h2></div>
          <button class="hf-iconbtn" data-close-sheet aria-label="Close">${icon("mdi:close")}</button></div>
        <div class="hf-sheetbody"><p class="hf-confirmtext" style="margin:0">No box ships that week and you
          won't be charged for it. You can unskip it until the selection deadline.</p></div>
        <div class="hf-sheetfoot"><button class="hf-btn" data-close-sheet>Keep my box</button>
          <button class="hf-btn primary" data-action="confirm-skip">Skip week</button></div>`,
      onClick: (_ev, el) => {
        if (el && el.dataset.action === "confirm-skip") {
          this.closeSheet();
          this.toggleSkip(week);
        }
      },
    });
  }

  async toggleSkip(week) {
    if (this._busy || !week) return;
    const unskip = L.isSkipped(week);
    const service = unskip ? "unskip_week" : "skip_week";
    this._busy = true;
    this.renderView();
    this._renderBoxBar();
    let failed = null;
    try {
      await this.callAction(service, { week_id: week.week_id });
    } catch (err) {
      failed = (err && err.message) || String(err);
    }
    this._busy = false;
    await this._fetchWeeks({ quiet: true });
    this._fetchSummary();
    if (failed) this.toast(`Couldn't ${unskip ? "unskip" : "skip"} the week: ${failed}`, true);
    else {
      this.toast(unskip ? "Week restored — your box is back on." : "Week skipped.");
      this.broadcastDataChanged();
    }
  }

  async _ensureDeliveryOptions() {
    if (this._deliveryOptions !== null) return;
    this._deliveryOptions = {};
    try {
      const response = await this.call("get_delivery_options");
      const byHandle = {};
      for (const o of response.delivery_options || []) if (o && o.handle) byHandle[o.handle] = o;
      this._deliveryOptions = byHandle;
    } catch (_err) {
      this._deliveryOptions = {}; // date-only labels still work
    }
    if (this._sheetState && this._sheetState.kind === "reschedule") this.renderSheet();
  }

  openReschedule(week) {
    if (!week || !L.canReschedule(week)) return;
    this.openSheet({
      kind: "reschedule",
      narrow: true,
      label: "Change delivery day",
      render: () => {
        const catalog = this._deliveryOptions || {};
        const currency = this._account && this._account.selected_plan_total_price_currency;
        const options = (week.available_one_off_options || []).filter((o) => o && o.handle);
        const buttons = options
          .map((o) => {
            const current = o.delivery_date && o.delivery_date === week.delivery_date;
            const meta = catalog[o.handle];
            const name = (meta && meta.delivery_name) || (o.delivery_date ? L.fmtLongDate(o.delivery_date) : o.handle);
            const date = o.delivery_date && meta && meta.delivery_name ? L.fmtDate(o.delivery_date) : "";
            const price = meta && Number(meta.price) > 0 ? `+${L.fmtPrice(meta.price, currency)}` : "";
            return `<button class="hf-controlrow hf-btn ghost" style="height:auto;border-radius:12px;justify-content:space-between;padding:12px 14px;width:100%"
                data-action="pick-day" data-handle="${esc(o.handle)}" ${current || this._busy ? "disabled" : ""}>
                <span style="text-align:left"><span class="hf-controllabel">${esc(name)}</span>
                  ${date ? `<span class="hf-controlhint">${esc(date)}</span>` : ""}</span>
                <span>${current ? UI.pill("Current", "ok") : price ? `<span class="hf-muted hf-small">${esc(price)}</span>` : ""}</span>
              </button>`;
          })
          .join("");
        return `
          <div class="hf-sheethead"><div class="hf-sheettitle"><h2>Change delivery day</h2>
            <div class="hf-sheetsub">For the box scheduled ${esc(L.fmtLongDate(week.delivery_date))} only</div></div>
            <button class="hf-iconbtn" data-close-sheet aria-label="Close">${icon("mdi:close")}</button></div>
          <div class="hf-sheetbody" style="gap:4px">${buttons || `<div class="hf-empty">No other days are available.</div>`}</div>`;
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

  async reschedule(week, handle) {
    if (this._busy || !week || !handle) return;
    this._busy = true;
    this.renderView();
    let failed = null;
    try {
      await this.callAction("reschedule_week", { week_id: week.week_id, delivery_option: handle });
    } catch (err) {
      failed = (err && err.message) || String(err);
    }
    this._busy = false;
    await this._fetchWeeks({ quiet: true });
    if (failed) this.toast(`Couldn't change the day: ${failed}`, true);
    else {
      this.toast("Delivery day changed for that week.");
      this.broadcastDataChanged();
    }
  }

  openPantry(entityId, week) {
    if (!entityId) return;
    this.openSheet({
      kind: "pantry",
      narrow: true,
      label: "Pantry list",
      render: () => {
        const st = this.pantry(entityId);
        const items = (st && st.items) || [];
        const open = items.filter((i) => i.status !== "completed").length;
        const body = !st || (st.loading && !st.items)
          ? `<div class="hf-empty">Loading…</div>`
          : items.length
            ? UI.pantryList(entityId, items)
            : `<div class="hf-empty">${icon("mdi:check-all")}Nothing extra to buy for this box.</div>`;
        return `
          <div class="hf-sheethead"><div class="hf-sheettitle"><h2>Pantry staples</h2>
            <div class="hf-sheetsub">Not in the ${esc(L.fmtLongDate(week && week.delivery_date))} box · ${open} to get</div></div>
            <button class="hf-iconbtn" data-close-sheet aria-label="Close">${icon("mdi:close")}</button></div>
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
        <div class="hf-sheethead"><div class="hf-sheettitle"><h2>${esc(recipe.name)}</h2></div>
          <button class="hf-iconbtn" data-close-sheet aria-label="Close video">${icon("mdi:close")}</button></div>
        <div class="hf-videobox">
          <video controls autoplay playsinline><source src="${esc(url)}" type="video/mp4"></video>
          <div class="hf-videoerr" hidden>This clip could not be played here.</div>
          <a class="hf-videofallback" href="${esc(url)}" target="_blank" rel="noopener noreferrer">Video not playing? Open it directly</a>
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

  openSheet(sheet) {
    this._sheetState = sheet;
    this._returnFocus = this.shadowRoot.activeElement;
    this.renderSheet();
    const box = this._shell.sheet.querySelector(".hf-sheet");
    if (box) box.focus({ preventScroll: true });
  }

  renderSheet() {
    this._ensureShell();
    const host = this._shell.sheet;
    const sheet = this._sheetState;
    if (!sheet) {
      host.innerHTML = "";
      return;
    }
    const scroller = host.querySelector(".hf-sheetbody");
    const top = scroller ? scroller.scrollTop : 0;
    host.innerHTML = `<div class="hf-sheetwrap"><div class="hf-sheet${sheet.narrow ? " narrow" : ""}${
      sheet.kind === "video" ? " hf-videobox" : ""
    }" role="dialog" aria-modal="true" aria-label="${esc(sheet.label || "")}" tabindex="-1">${sheet.render()}</div></div>`;
    const next = host.querySelector(".hf-sheetbody");
    if (next) next.scrollTop = top;
  }

  closeSheet() {
    if (!this._sheetState) return;
    const sheet = this._sheetState;
    this._sheetState = null;
    if (this._shell) this._shell.sheet.innerHTML = "";
    if (sheet.onClose) sheet.onClose();
    if (this._returnFocus && this._returnFocus.isConnected) this._returnFocus.focus({ preventScroll: true });
    this._returnFocus = null;
  }

  get sheetKind() {
    return this._sheetState ? this._sheetState.kind : null;
  }

  toast(message, isError = false) {
    this._toastState = { message, isError };
    this._renderToast();
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => {
      this._toastState = null;
      this._renderToast();
    }, isError ? 6000 : 3500);
  }

  setSaving(text) {
    this._saving = text;
    this._busy = Boolean(text);
    this._renderToast();
  }

  _renderToast() {
    if (!this._shell) return;
    if (this._saving) {
      this._shell.toast.innerHTML = `<div class="hf-toast saving" role="status"><span class="hf-spinner"></span>${esc(this._saving)}</div>`;
      return;
    }
    const t = this._toastState;
    this._shell.toast.innerHTML = t
      ? `<div class="hf-toast${t.isError ? " error" : ""}" role="${t.isError ? "alert" : "status"}">${icon(
          t.isError ? "mdi:alert-circle-outline" : "mdi:check-circle-outline"
        )}${esc(t.message)}</div>`
      : "";
  }

  // ---- rendering ----------------------------------------------------------------------------

  _ensureShell() {
    if (this._shell) return;
    const card = document.createElement("ha-card");
    card.innerHTML = `
      <div class="hf-app">
        <header class="hf-appbar js-appbar"></header>
        <nav class="hf-tabs js-tabs" aria-label="HelloFresh sections"></nav>
        <div class="hf-banners js-banners"></div>
        <main class="hf-main js-main"></main>
        <div class="hf-boxbar js-boxbar"></div>
      </div>`;
    const sheet = document.createElement("div");
    const toast = document.createElement("div");
    this.shadowRoot.append(card, sheet, toast);
    this._shell = {
      card,
      sheet,
      toast,
      app: card.querySelector(".hf-app"),
      appbar: card.querySelector(".js-appbar"),
      tabs: card.querySelector(".js-tabs"),
      banners: card.querySelector(".js-banners"),
      main: card.querySelector(".js-main"),
      boxbar: card.querySelector(".js-boxbar"),
    };
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
  }

  _activeView() {
    const key = this._view;
    if (!this._views[key]) {
      const factories = {
        overview: () => new OverviewView(this),
        menu: () => new Menu.MealsView(this),
        market: () => new Menu.MarketView(this),
        recipes: () => new RecipesView(this),
        account: () => new AccountView(this),
      };
      this._views[key] = (factories[key] || factories.overview)();
    }
    return this._views[key];
  }

  _render() {
    if (!this.shadowRoot || !this._config) return;
    this._ensureShell();
    this._renderChrome();
    this.renderView();
    this._renderToast();
  }

  _renderChrome() {
    if (!this._shell || !this._config) return;
    this._shell.appbar.innerHTML = this._renderAppbar();
    this._shell.appbar.hidden = !this._shell.appbar.innerHTML.trim();
    this._shell.tabs.innerHTML = this._renderTabs();
    this._shell.tabs.hidden = !this._shell.tabs.innerHTML.trim();
    this._shell.banners.innerHTML = this._renderBanners();
  }

  // Re-render the active view (and the box bar), keeping focus, caret and rail scroll positions.
  renderView() {
    if (!this._shell || !this._config) return;
    const main = this._shell.main;
    const view = this._activeView();
    const ui = this._captureUi(main);
    this._renderedEditable = this._currentEditability();
    main.innerHTML = view.render();
    main.classList.toggle("reloading", this._loading && !this._quietLoad);
    this._restoreUi(main, ui);
    if (view.afterRender) view.afterRender(main);
    this._renderBoxBar();
  }

  _markReloading() {
    if (this._shell) this._shell.main.classList.toggle("reloading", this._loading && !this._quietLoad);
  }

  _renderBoxBar() {
    if (!this._shell) return;
    const week = this.selectedWeek();
    const html = BOX_VIEWS.has(this._view) && week ? Menu.renderBoxBar(this, week) : "";
    const bar = this._shell.boxbar;
    if (bar.innerHTML !== html) bar.innerHTML = html;
  }

  // Public so views can refresh just the bar during a selection edit.
  renderBoxBar() {
    this._renderBoxBar();
  }

  _captureUi(root) {
    const active = this.shadowRoot.activeElement;
    const keyed = active && active.closest && active.closest("[data-focus-key]");
    const state = {
      focusKey: keyed ? keyed.getAttribute("data-focus-key") : null,
      selection:
        active && typeof active.selectionStart === "number"
          ? [active.selectionStart, active.selectionEnd]
          : null,
      scroll: {},
    };
    for (const el of root.querySelectorAll("[data-scroll-key]")) {
      state.scroll[el.getAttribute("data-scroll-key")] = [el.scrollLeft, el.scrollTop];
    }
    return state;
  }

  _restoreUi(root, state) {
    for (const el of root.querySelectorAll("[data-scroll-key]")) {
      const saved = state.scroll[el.getAttribute("data-scroll-key")];
      if (saved) {
        el.scrollLeft = saved[0];
        el.scrollTop = saved[1];
      }
    }
    if (!state.focusKey) return;
    const el = root.querySelector(`[data-focus-key="${CSS.escape(state.focusKey)}"]`);
    if (!el || el.disabled) return;
    el.focus({ preventScroll: true });
    if (state.selection && typeof el.setSelectionRange === "function") {
      try {
        el.setSelectionRange(state.selection[0], state.selection[1]);
      } catch (_e) {
        /* input types without a caret */
      }
    }
  }

  _subtitle() {
    const s = this._summary;
    const parts = [];
    if (s) {
      if (s.selected_plan) parts.push(s.selected_plan);
      const meals = Number(s.required_meal_count);
      const people = Number(s.number_of_people);
      if (meals && people) parts.push(`${meals} meals for ${people}`);
      if (s.subscription_status && String(s.subscription_status).toLowerCase() !== "active") {
        parts.push(L.titleCase(s.subscription_status));
      }
    }
    if (!parts.length && this._weeks && this._weeks.length) {
      const next = L.nextBoxWeek(this._weeks).week;
      if (next && next.display_name) parts.push(next.display_name);
    }
    return parts.join(" · ");
  }

  _renderAppbar() {
    const cfg = this._config;
    const logo = cfg.logo === false || cfg.logo === "" ? "" : cfg.logo === true || cfg.logo == null ? LOGO_URL : String(cfg.logo);
    const title = cfg.title;
    const sub = this._subtitle();
    const menu = this._wantsMenuButton();
    this._menuShown = menu;
    if (!menu && !logo && !title && cfg.views.length < 2) return "";
    return `
      ${menu ? `<button class="hf-iconbtn hf-menubtn" data-action="sidebar" title="Sidebar" aria-label="Open the sidebar">${icon("mdi:menu")}</button>` : ""}
      ${logo ? `<img class="hf-logo" src="${esc(logo)}" alt="">` : ""}
      <div class="hf-apptitle">${title ? `<h1>${esc(title)}</h1>` : ""}${sub ? `<div class="hf-sub">${esc(sub)}</div>` : ""}</div>
      <div class="hf-appactions">
        <button class="hf-iconbtn${this._loading ? " spin" : ""}" data-action="refresh" title="Refresh"
          aria-label="Refresh" ${this._loading ? "disabled" : ""}>${icon("mdi:refresh")}</button>
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
            ? `<span class="hf-count" title="${needs} week${needs === 1 ? "" : "s"} need meal picks">${needs}</span>`
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
        <div class="hf-noticebody">Couldn't refresh: ${esc(this._error)}</div>
        <button class="hf-btn sm" data-action="refresh">Retry</button></div>`);
    }
    const s = this._summary;
    if (s && (s.payment_method_expiring || s.payment_method_expired)) {
      const card = L.cardOnFile(s) || "payment card";
      const when = L.fmtCardExpiry(s.payment_card_expiry);
      const text = s.payment_method_expired
        ? `Your <strong>${esc(card)}</strong> has expired${when ? ` (${esc(when)})` : ""}. Update it on HelloFresh or your next box may not ship.`
        : `Your <strong>${esc(card)}</strong> expires soon${when ? ` (${esc(when)})` : ""}. Update it on HelloFresh before your next box is charged.`;
      out.push(`<div class="hf-notice ${s.payment_method_expired ? "tone-danger" : "tone-warn"}" role="alert">
        ${icon(s.payment_method_expired ? "mdi:credit-card-off-outline" : "mdi:credit-card-clock-outline")}<div class="hf-noticebody">${text}</div></div>`);
    }
    if (s && s.next_holiday_message) {
      const date = s.next_holiday_delivery_date
        ? ` New delivery date: <strong>${esc(L.fmtLongDate(s.next_holiday_delivery_date))}</strong>.`
        : "";
      out.push(`<div class="hf-notice tone-info">${icon("mdi:calendar-star")}
        <div class="hf-noticebody">${esc(s.next_holiday_message)}${date}</div></div>`);
    }
    return out.join("");
  }

  // The full-view loading / error / empty state for views that need weeks.
  weeksPlaceholder() {
    if (this._weeks) return "";
    if (this._error) {
      return `<div class="hf-empty">${icon("mdi:cloud-alert-outline")}Couldn't load your deliveries:
        ${esc(this._error)}<div class="hf-actions" style="justify-content:center;margin-top:12px">
        <button class="hf-btn primary" data-action="refresh">Try again</button></div></div>`;
    }
    return `<div class="hf-skeletons" aria-label="Loading"><div class="hf-skeleton"></div>
      <div class="hf-skeleton"></div><div class="hf-skeleton"></div></div>`;
  }

  // ---- events ---------------------------------------------------------------------------------

  _onClick(ev) {
    const el = ev.target.closest("[data-action]");
    const action = el ? el.getAttribute("data-action") : null;
    if (el && el.disabled) return;
    if (action === "nav") return this.navigate(el.getAttribute("data-view"));
    if (action === "refresh") return this.refresh();
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

customElements.define("hellofresh-card", HelloFreshCard);

// ---- visual config editor --------------------------------------------------------------------

const VIEW_OPTIONS = VIEWS.map((v) => ({ value: v.key, label: v.label }));

const EDITOR_SCHEMA = [
  { name: "title", selector: { text: {} } },
  { name: "logo", selector: { boolean: {} } },
  { name: "views", selector: { select: { multiple: true, mode: "list", options: VIEW_OPTIONS } } },
  { name: "default_view", selector: { select: { mode: "dropdown", options: VIEW_OPTIONS } } },
  {
    name: "accent",
    selector: {
      select: {
        mode: "dropdown",
        options: [
          { value: "brand", label: "HelloFresh green" },
          { value: "theme", label: "Theme primary colour" },
        ],
      },
    },
  },
  { name: "image_width", selector: { number: { min: 100, max: 1200, step: 50, mode: "box" } } },
  { name: "config_entry_id", selector: { config_entry: { integration: "hellofresh" } } },
];

const EDITOR_LABELS = {
  title: "Title",
  logo: "Show HelloFresh logo",
  views: "Sections",
  default_view: "Open on",
  accent: "Accent colour",
  image_width: "Recipe image width (px)",
  config_entry_id: "HelloFresh account",
};

const EDITOR_HELPERS = {
  views: "Pick one section for a focused card without the tab bar.",
  default_view: "Leave empty to reopen the last section used.",
  config_entry_id: "Only needed with more than one HelloFresh account.",
};

class HelloFreshCardEditor extends HTMLElement {
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
    config.title = value.title != null ? value.title : "HelloFresh";
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

customElements.define("hellofresh-card-editor", HelloFreshCardEditor);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "hellofresh-card",
  name: "HelloFresh",
  description:
    "Your whole HelloFresh account in one card: next box, menu and Market with one save, recipes, plan and spending.",
  preview: false,
  documentationURL: "https://github.com/kedube/ha-hellofresh",
});

console.info(
  `%c HELLOFRESH-CARD %c v${CARD_VERSION} `,
  "color:#fff;background:#91c11e;font-weight:700",
  "color:#91c11e;background:#fff"
);
