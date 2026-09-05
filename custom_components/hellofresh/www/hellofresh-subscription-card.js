/*
 * HelloFresh Subscription Card
 * ----------------------------
 * A condensed account/subscription overview: the plan, credit, servings and address, plus
 * the upcoming-delivery counters — in a compact label-over-value grid instead of a long
 * entities list. Deliberately shows nothing the schedule card already covers (payment date,
 * coupon, preselected flag, per-box detail). A holiday-delivery
 * notice (message + shifted date), when HelloFresh announces one, renders as a banner at the
 * top, replacing the separate conditional markdown card the example dashboard used to need.
 *
 * Reads everything in one call from the response-returning `hellofresh.get_account_summary`
 * service — the same values the corresponding sensors report (the service and the sensors
 * share one value dispatcher, so they can never disagree). Re-pulls automatically on the
 * integration's configured refresh interval and when a sibling card saves a change.
 * Read-only.
 *
 * Config:
 *   type: custom:hellofresh-subscription-card
 *   config_entry_id: <optional>   # required only when multiple HelloFresh accounts exist
 *   title: Subscription           # optional card header
 *   logo: true                    # optional bundled HelloFresh logo in the header
 *
 * No build step: hand-written ES2020 served from the integration's www/ directory.
 */

// The integration stamps its release version onto the resource URL as ?v= (cache-bust),
// so the banner reports exactly which build the browser actually loaded.
const SUBSCRIPTION_CARD_VERSION = new URL(import.meta.url).searchParams.get("v") || "unknown";
// Shared card helpers. AWAITED AT TOP LEVEL: they are called synchronously during the first
// render, and an un-awaited dynamic import is still a Promise then. The dynamic form carries
// this card's ?v= cache-bust onto the shared module (a static specifier is never stamped).
const {
  esc,
  parseLocalDate,
  fmtDate,
  fmtPrice,
  refetchIntervalMs,
  accountKey,
  broadcastWeek,
  DATA_CHANGED_EVENT,
} = await import(
  new URL(
    `./hellofresh-shared.js?v=${encodeURIComponent(SUBSCRIPTION_CARD_VERSION)}`,
    import.meta.url,
  ).href
);


class HelloFreshSubscriptionCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.shadowRoot.adoptedStyleSheets = [HelloFreshSubscriptionCard._sheet()];
    this._hass = null;
    this._summary = null;
    this._loading = false;
    this._error = null;
    this._fetched = false;
    this._lastFetched = 0;
    this._tickTimer = null;
    // Region preset catalog (get_presets). Lazily fetched the first time the user expands the
    // "Meal presets" reference section; null = not yet fetched, [] = fetched/unavailable.
    this._presets = null;
    this._presetsExpanded = false;
    this._presetsLoading = false;
    this._onDataChanged = (ev) => this._receiveDataChanged(ev);
    this._onVisibility = () => this._onBecameVisible();
  }

  connectedCallback() {
    window.addEventListener(HelloFreshSubscriptionCard.DATA_CHANGED_EVENT, this._onDataChanged);
    document.addEventListener("visibilitychange", this._onVisibility);
    this._startTick();
    if (this._summary) this._refreshIfStale();
  }

  disconnectedCallback() {
    window.removeEventListener(HelloFreshSubscriptionCard.DATA_CHANGED_EVENT, this._onDataChanged);
    document.removeEventListener("visibilitychange", this._onVisibility);
    this._stopTick();
  }

  setConfig(config) {
    this._config = { title: "Subscription", ...config };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (hass && !this._fetched && !this._loading) {
      this._fetched = true;
      this._fetch();
    }
  }

  getCardSize() {
    return 6;
  }

  static getStubConfig() {
    return { type: "custom:hellofresh-subscription-card" };
  }

  // ---- data ----------------------------------------------------------------

  async _fetch() {
    if (!this._hass || this._loading) return;
    this._loading = true;
    this._error = null;
    this._render();
    try {
      const data = {};
      if (this._config.config_entry_id) data.config_entry_id = this._config.config_entry_id;
      const result = await this._hass.callService(
        "hellofresh", "get_account_summary", data, undefined, false, true
      );
      this._summary = (result && result.response) || {};
    } catch (err) {
      this._error = (err && err.message) || String(err);
    } finally {
      this._lastFetched = Date.now();
      this._loading = false;
      this._render();
      if (this._refetchQueued) {
        // A data-changed event arrived mid-fetch; that response predates the write.
        this._refetchQueued = false;
        this._fetch();
      }
    }
    // Resolve the preset catalog once so the Preference row can show the full name ("Quick &
    // Easy") rather than the bare slug — but only when there's a preference to name and the
    // catalog isn't already loaded. _fetchPresets guards against a double-fetch if the user
    // also expands the "Meal presets" section. A failure is silent (the slug fallback stands).
    if (
      this._summary &&
      this._summary.plan_preference &&
      this._presets === null &&
      !this._presetsLoading
    ) {
      this._fetchPresets();
    }
  }

  // Toggle the "Meal presets" reference section, lazily fetching the catalog on first expand.
  _togglePresets() {
    this._presetsExpanded = !this._presetsExpanded;
    this._render();
    if (this._presetsExpanded && this._presets === null && !this._presetsLoading) {
      this._fetchPresets();
    }
  }

  async _fetchPresets() {
    if (!this._hass || this._presetsLoading) return;
    this._presetsLoading = true;
    this._render();
    try {
      const data = {};
      if (this._config.config_entry_id) data.config_entry_id = this._config.config_entry_id;
      const result = await this._hass.callService(
        "hellofresh", "get_presets", data, undefined, false, true
      );
      const presets = ((result && result.response) || {}).presets;
      this._presets = Array.isArray(presets) ? presets : [];
    } catch (_err) {
      // A reference list is non-essential; on failure show an empty list rather than an error.
      this._presets = [];
    } finally {
      this._presetsLoading = false;
      this._render();
    }
  }

  // ---- auto-refresh (same contract as the schedule card) --------------------

  _refetchIntervalMs() {
    return refetchIntervalMs(this._summary);
  }


  _refreshIfStale() {
    if (!this._hass || !this._fetched || this._loading) return false;
    if (Date.now() - this._lastFetched < this._refetchIntervalMs()) return false;
    this._fetch();
    return true;
  }

  _startTick() {
    if (this._tickTimer) return;
    this._tickTimer = setInterval(() => {
      if (document.visibilityState === "hidden") return;
      this._refreshIfStale();
    }, 60000);
  }

  _stopTick() {
    clearInterval(this._tickTimer);
    this._tickTimer = null;
  }

  _onBecameVisible() {
    if (document.visibilityState !== "visible") return;
    this._refreshIfStale();
  }

  static get DATA_CHANGED_EVENT() {
    return DATA_CHANGED_EVENT;
  }

  _accountKey() {
    return accountKey(this._config);
  }

  _receiveDataChanged(ev) {
    const detail = (ev && ev.detail) || {};
    if ((detail.accountKey || "default") !== this._accountKey()) return;
    if (!this._fetched) return;
    if (this._loading) {
      // An in-flight fetch was issued BEFORE the write this event announces, so its response
      // predates the change and dropping the event left pre-write data on screen until the next
      // interval (up to 3 hours). Queue one follow-up fetch, drained in _fetch's finally block.
      // That drain already existed here but nothing ever set the flag, so it was dead code and
      // this card still had the staleness bug the schedule and cost cards fixed.
      this._refetchQueued = true;
      return;
    }
    this._fetch();
  }

  // Broadcast a week selection over the shared cross-card channel (localStorage key +
  // window event, same scheme as the other cards): the schedule card ring-highlights the
  // week and the meal-planner/market cards jump to it when mounted.
  _gotoWeek(weekId) {
    broadcastWeek(this._config, weekId);
  }

  // ---- rendering -------------------------------------------------------------

  _render() {
    if (!this.shadowRoot) return;
    this._ensureShell();
    this._shell.head.innerHTML = `
      ${this._renderLogo()}
      <span class="title-text">${this._esc(this._config ? this._config.title : "Subscription")}</span>
      <button class="refreshbtn" data-action="refresh" title="Refresh" ${this._loading ? "disabled" : ""}>↻</button>`;
    this._shell.body.innerHTML = this._renderBody();
  }

  _ensureShell() {
    if (this._shell) return;
    const card = document.createElement("ha-card");
    card.innerHTML = `<div class="head"></div><div class="body"></div>`;
    this.shadowRoot.appendChild(card);
    this._shell = { card, head: card.querySelector(".head"), body: card.querySelector(".body") };
    card.addEventListener("click", (ev) => {
      const actionEl = ev.target.closest("[data-action]");
      if (!actionEl) return;
      const action = actionEl.getAttribute("data-action");
      if (action === "refresh") this._fetch();
      else if (action === "goto-week") this._gotoWeek(actionEl.getAttribute("data-week-id"));
      else if (action === "toggle-presets") this._togglePresets();
    });
  }

  _renderLogo() {
    const logo = this._config && this._config.logo;
    if (!logo) return "";
    const logoUrl = logo === true ? "/hellofresh/hellofresh-logo.png" : logo;
    return `<img class="logo" src="${this._esc(logoUrl)}" alt="HelloFresh">`;
  }

  _renderBody() {
    if (!this._summary) {
      if (this._loading || !this._fetched) return `<div class="state">Loading subscription…</div>`;
      return `<div class="state error">Could not load subscription: ${this._esc(this._error || "no data")}</div>
        <div class="actions"><button data-action="refresh">Retry</button></div>`;
    }
    const notice = this._error
      ? `<div class="notice">Refresh failed: ${this._esc(this._error)}
           <button class="refreshbtn" data-action="refresh">Retry</button></div>`
      : "";
    return `<div class="${this._loading ? "reloading" : ""}">
      ${notice}
      ${this._renderPaymentBanner()}
      ${this._renderHolidayBanner()}
      ${this._renderSections()}
      ${this._renderPresets()}
    </div>`;
  }

  // Collapsible reference list of the region's meal presets (get_presets): the human-readable
  // names + descriptions behind the plan preference slugs. Read-only — HelloFresh exposes no
  // API to change a plan's preset, so this is a "what do these mean / which is mine" reference.
  // The user's active preference is highlighted. Fetched lazily on first expand.
  _renderPresets() {
    if (!this._summary) return "";
    const active = String(this._summary.plan_preference || "").toLowerCase();
    const header = `
      <button class="preset-toggle" data-action="toggle-presets"
              aria-expanded="${this._presetsExpanded ? "true" : "false"}">
        <span class="preset-caret">${this._presetsExpanded ? "▾" : "▸"}</span>
        <span>Meal presets</span>
      </button>`;
    if (!this._presetsExpanded) return `<div class="section presets">${header}</div>`;

    let list;
    if (this._presetsLoading && this._presets === null) {
      list = `<div class="preset-empty">Loading presets…</div>`;
    } else if (!this._presets || this._presets.length === 0) {
      list = `<div class="preset-empty">No presets available.</div>`;
    } else {
      list = this._presets
        .map((p) => {
          const handle = String((p && p.handle) || "").toLowerCase();
          const isActive = handle && handle === active;
          const name = (p && (p.name || p.handle)) || "";
          const desc = (p && p.description) || "";
          return `
            <div class="preset-item${isActive ? " active" : ""}">
              <span class="preset-name">${this._esc(name)}${
                isActive ? ` <span class="preset-badge">Yours</span>` : ""
              }</span>
              ${desc ? `<span class="preset-desc">${this._esc(desc)}</span>` : ""}
            </div>`;
        })
        .join("");
    }
    return `<div class="section presets">${header}<div class="preset-list">${list}</div></div>`;
  }

  // Payment-method warning: HelloFresh's own "card expiring / expired" check (the
  // payment_method_expiring binary sensor). Shown only while the gateway flags it — a
  // box silently failing to ship is the usual consequence, so this is the one notice worth
  // shouting about. Red when already expired, amber when merely expiring.
  _renderPaymentBanner() {
    const s = this._summary;
    if (!s || (!s.payment_method_expiring && !s.payment_method_expired)) return "";
    const card = this._cardOnFile() || "payment card";
    const when = this._fmtCardExpiry(s.payment_card_expiry);
    const text = s.payment_method_expired
      ? `Your ${card} on file has expired${when ? ` (${when})` : ""}. Update it on HelloFresh or your next box may not ship.`
      : `Your ${card} on file expires soon${when ? ` (${when})` : ""}. Update it on HelloFresh before your next box is charged.`;
    return `
      <div class="banner${s.payment_method_expired ? " danger" : ""}">
        <span class="banner-icon">💳</span>
        <span class="banner-text">${this._esc(text)}</span>
      </div>`;
  }

  // "Visa ending in 4242" — the card brand when HelloFresh reports one, else the type
  // humanized, plus the last four digits when known. The billing address is never stored.
  _cardOnFile() {
    const s = this._summary;
    const raw = s && (s.payment_card_brand || s.payment_card_type);
    const name = raw ? String(raw) : "";
    if (!name) return "";
    const humanized = name.replace(/[_-]+/g, " ").replace(/\b\w/g, (ch) => ch.toUpperCase());
    const brand = humanized.replace(/^Credit Card$/, "Credit card");
    const last4 = /^\d{4}$/.test(String(s.payment_card_last4 || "")) ? s.payment_card_last4 : "";
    return last4 ? `${brand} ending in ${last4}` : brand;
  }

  _fmtCardExpiry(value) {
    const match = /^(\d{4})-(\d{2})$/.exec(String(value || ""));
    if (!match) return "";
    const date = new Date(Number(match[1]), Number(match[2]) - 1, 1);
    try {
      return date.toLocaleDateString(undefined, { month: "long", year: "numeric" });
    } catch (_err) {
      return `${match[2]}/${match[1]}`;
    }
  }

  // Holiday delivery notice: HelloFresh's message plus the shifted delivery date, shown only
  // while the API announces one (the message clears once the holiday week passes).
  _renderHolidayBanner() {
    const s = this._summary;
    if (!s.next_holiday_message) return "";
    const date = s.next_holiday_delivery_date
      ? ` New date: <b>${this._esc(this._fmtDate(s.next_holiday_delivery_date))}</b>.`
      : "";
    return `
      <div class="banner">
        <span class="banner-icon">🎄</span>
        <span class="banner-text">${this._esc(s.next_holiday_message)}${date}</span>
      </div>`;
  }

  _renderSections() {
    const s = this._summary;
    // No "Next box" section: payment date, coupon, and the preselected flag are already on
    // the schedule card (summary rows / week badge), so repeating them here is noise.
    const sections = [
      ["Account", [
        ["Account ID", s.account_id],
        ["Status", s.subscription_status],
        ["Plan", s.selected_plan],
        // The active meal-preference preset HelloFresh uses to auto-preselect meals. Shown as its
        // full catalog name ("Quick & Easy") once the preset catalog is loaded, humanized slug
        // ("Quick") otherwise. Distinct from the plan itself.
        ["Preference", this._preferenceName(s.plan_preference)],
        ["Plan total", this._fmtPrice(s.selected_plan_total_price, s.selected_plan_total_price_currency)],
        // The plan total's split (from /gw/calculate): shipping always, discount only when
        // one applies — a permanent "$0.00 discount" cell would just be noise.
        ["Shipping", this._breakdownPrice("shipping_amount", false)],
        ["Discount", this._breakdownPrice("discount_amount", true)],
        ["Credit", this._fmtPrice(s.account_credit, s.account_credit_currency)],
        ["Card on file", this._cardOnFileCell()],
        ["Servings", s.number_of_people],
        ["Meals per box", s.required_meal_count],
        ["Boxes received", s.boxes_received],
        ["Address", s.delivery_address, true],
      ]],
      ["Upcoming", [
        ["Deliveries", s.upcoming_delivery_count],
        // Counters with a week behind them are clickable: the click jumps the schedule /
        // meal-planner cards to that week over the cross-card sync channel.
        ["Need selecting", s.weeks_needing_selection,
          false, (s.weeks_needing_selection_ids || [])[0]],
        ["Skipped", s.skipped_week_count],
        ["Next skipped", s.next_skipped_week, false, s.next_skipped_week_id],
      ]],
    ];
    return sections
      .map(([title, items]) => {
        // Absent values drop their cell entirely — that's what keeps the card condensed.
        const cells = items
          .filter(([, value]) => value !== null && value !== undefined && value !== "")
          .map(([label, value, wide, weekId]) => {
            const click = weekId
              ? ` data-action="goto-week" data-week-id="${this._esc(weekId)}"
                  title="Show this week in the schedule and meal-planner cards"`
              : "";
            return `
              <div class="item${wide ? " wide" : ""}${weekId ? " click" : ""}"${click}>
                <span class="ilabel">${this._esc(label)}</span>
                <span class="ival">${this._esc(value)}</span>
              </div>`;
          })
          .join("");
        if (!cells) return "";
        return `<div class="section"><div class="stitle">${this._esc(title)}</div><div class="grid">${cells}</div></div>`;
      })
      .join("");
  }

  // ---- helpers ---------------------------------------------------------------

  // Parse a date anchored to LOCAL midnight — bare "YYYY-MM-DD" strings otherwise parse as
  // UTC midnight and render as the previous day west of UTC.
  _parseLocalDate(value) {
    return parseLocalDate(value);
  }

  _fmtDate(iso) {
    return fmtDate(iso);
  }

  // Display name for the active plan preference. Prefers the preset catalog's human-readable
  // name (e.g. "quick" -> "Quick & Easy") when it's already been fetched for the "Meal presets"
  // section; falls back to the humanized slug ("Quick") otherwise, so it's never blank and never
  // triggers an eager catalog fetch just for this one label. Returns null for empty values so
  // the row drops out entirely.
  _preferenceName(slug) {
    if (!slug) return null;
    const target = String(slug).toLowerCase();
    if (Array.isArray(this._presets)) {
      const match = this._presets.find(
        (p) => p && String(p.handle || "").toLowerCase() === target
      );
      if (match && match.name) return match.name;
    }
    return this._fmtPreference(slug);
  }

  // Humanize a plan-preference slug for display: "quick" -> "Quick", "quick-and-easy" ->
  // "Quick And Easy". Returns null for empty values so the row drops out entirely.
  _fmtPreference(slug) {
    if (!slug) return null;
    return String(slug)
      .replace(/[-_]+/g, " ")
      .replace(/\b\w/g, (c) => c.toUpperCase());
  }

  _fmtPrice(amount, currency) {
    return fmtPrice(amount, currency);
  }

  // One figure from the plan's price breakdown, formatted in the plan's currency; null when
  // the breakdown is missing, the figure is absent, or (for discounts) it is zero.
  _breakdownPrice(key, onlyIfPositive) {
    const s = this._summary;
    const breakdown = s && s.selected_plan_price_breakdown;
    const amount = breakdown ? Number(breakdown[key]) : NaN;
    if (!Number.isFinite(amount)) return null;
    if (onlyIfPositive && amount <= 0) return null;
    const text = this._fmtPrice(amount, s.selected_plan_total_price_currency);
    return onlyIfPositive ? `−${text}` : text;
  }

  _cardOnFileCell() {
    const card = this._cardOnFile();
    if (!card) return null;
    const when = this._fmtCardExpiry(this._summary.payment_card_expiry);
    const state = this._summary.payment_method_expired
      ? " · expired"
      : this._summary.payment_method_expiring
        ? " · expiring"
        : "";
    return `${card}${when ? ` · exp. ${when}` : ""}${state}`;
  }

  _esc(value) {
    return esc(value);
  }

  static _sheet() {
    if (!HelloFreshSubscriptionCard.__sheet) {
      const sheet = new CSSStyleSheet();
      sheet.replaceSync(HelloFreshSubscriptionCard._styles());
      HelloFreshSubscriptionCard.__sheet = sheet;
    }
    return HelloFreshSubscriptionCard.__sheet;
  }

  static _styles() {
    return `
      ha-card { padding: 16px 16px 16px; }
      .head { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
      .head .logo { height: 40px; width: 40px; border-radius: 8px; object-fit: cover; flex: none; }
      .title-text { font-size: 1.5em; font-weight: 500; }
      .state { text-align: center; padding: 28px 8px; color: var(--secondary-text-color); }
      .state.error { color: var(--error-color, #db4437); }
      .reloading { opacity: 0.6; transition: opacity 0.2s; }
      .notice {
        display: flex; align-items: center; gap: 10px; margin-bottom: 12px;
        padding: 8px 12px; border-radius: 10px; font-size: 0.85em;
        background: color-mix(in srgb, var(--error-color, #db4437) 12%, transparent);
        color: var(--error-color, #db4437);
      }
      .banner {
        display: flex; align-items: center; gap: 10px; margin-bottom: 12px;
        padding: 10px 14px; border-radius: 10px;
        background: var(--warning-color, #ff9800); color: #fff;
      }
      .banner.danger { background: var(--error-color, #db4437); }
      .banner-icon { font-size: 1.2em; flex: none; }
      .banner-text { font-size: 0.92em; }
      .section { margin-top: 10px; }
      .section:first-child { margin-top: 0; }
      .stitle {
        font-size: 0.72em; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em;
        color: var(--secondary-text-color); margin-bottom: 6px;
      }
      .grid {
        display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
        gap: 8px 16px;
      }
      .item { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
      .item.wide { grid-column: 1 / -1; }
      .item.click { cursor: pointer; }
      .item.click .ival { text-decoration: underline; text-decoration-color: var(--divider-color); text-underline-offset: 3px; }
      .item.click:hover .ival { text-decoration-color: currentColor; }
      .ilabel {
        font-size: 0.68em; text-transform: uppercase; letter-spacing: 0.04em;
        color: var(--secondary-text-color);
      }
      .ival { font-size: 0.9em; font-weight: 600; word-break: break-word; }
      /* Identical to the other cards' ↻ pill (their .skipbtn / .refreshbtn). */
      .refreshbtn {
        margin-left: auto; flex: none;
        font-size: 0.85em; padding: 5px 12px; border-radius: 14px;
        border: 1px solid var(--divider-color); background: var(--card-background-color);
        color: var(--primary-text-color); cursor: pointer;
      }
      .refreshbtn:disabled { opacity: 0.5; cursor: default; }
      .actions { text-align: center; margin-top: 8px; }
      .actions button { padding: 6px 16px; border-radius: 8px; cursor: pointer; }
      .section.presets { margin-top: 12px; border-top: 1px solid var(--divider-color); padding-top: 8px; }
      .preset-toggle {
        display: flex; align-items: center; gap: 6px; width: 100%;
        background: none; border: none; padding: 2px 0; cursor: pointer;
        color: var(--secondary-text-color); font: inherit;
        font-size: 0.68em; text-transform: uppercase; letter-spacing: 0.04em; font-weight: 600;
      }
      .preset-toggle:hover { color: var(--primary-text-color); }
      .preset-caret { font-size: 0.9em; }
      .preset-list { display: flex; flex-direction: column; gap: 6px; margin-top: 6px; }
      .preset-item {
        display: flex; flex-direction: column; gap: 1px;
        padding: 6px 8px; border-radius: 8px; background: var(--secondary-background-color);
      }
      .preset-item.active { outline: 2px solid var(--primary-color); }
      .preset-name { font-size: 0.9em; font-weight: 600; }
      .preset-badge {
        font-size: 0.7em; font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em;
        color: var(--text-primary-color, #fff); background: var(--primary-color);
        padding: 1px 6px; border-radius: 8px; vertical-align: middle;
      }
      .preset-desc { font-size: 0.8em; color: var(--secondary-text-color); }
      .preset-empty { font-size: 0.85em; color: var(--secondary-text-color); padding: 6px 2px; }
    `;
  }
}

customElements.define("hellofresh-subscription-card", HelloFreshSubscriptionCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "hellofresh-subscription-card",
  name: "HelloFresh Subscription Card",
  description: "Condensed HelloFresh account overview: plan, status, credit, and notices.",
});

console.info(
  `%c HELLOFRESH-SUBSCRIPTION-CARD %c v${SUBSCRIPTION_CARD_VERSION} `,
  "color: #fff; background: #91c11e; font-weight: 700;",
  "color: #91c11e; background: #333; font-weight: 700;"
);
