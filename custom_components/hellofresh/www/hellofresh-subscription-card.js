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
    }
  }

  // ---- auto-refresh (same contract as the schedule card) --------------------

  _refetchIntervalMs() {
    const mins = Number(this._summary && this._summary.refresh_interval_minutes);
    return (Number.isFinite(mins) && mins >= 1 ? mins : 180) * 60000;
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
    return "hellofresh-data-changed";
  }

  _accountKey() {
    return (this._config && this._config.config_entry_id) || "default";
  }

  _receiveDataChanged(ev) {
    const detail = (ev && ev.detail) || {};
    if ((detail.accountKey || "default") !== this._accountKey()) return;
    if (this._fetched && !this._loading) this._fetch();
  }

  // Broadcast a week selection over the shared cross-card channel (localStorage key +
  // window event, same scheme as the other cards): the schedule card ring-highlights the
  // week and the meal-planner/market cards jump to it when mounted.
  _gotoWeek(weekId) {
    if (!weekId) return;
    try {
      window.localStorage.setItem(`hellofresh:selected-week:${this._accountKey()}`, weekId);
    } catch (_e) {
      /* storage unavailable (private mode) — the live event below still works */
    }
    window.dispatchEvent(
      new CustomEvent("hellofresh-week-selected", {
        detail: { weekId, accountKey: this._accountKey() },
      })
    );
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
      ${this._renderHolidayBanner()}
      ${this._renderSections()}
    </div>`;
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
        ["Status", s.subscription_status],
        ["Plan", s.selected_plan],
        ["Plan total", this._fmtPrice(s.selected_plan_total_price, s.selected_plan_total_price_currency)],
        ["Credit", this._fmtPrice(s.account_credit, s.account_credit_currency)],
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
    const m = typeof value === "string" && /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
    if (m) return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    return new Date(value);
  }

  _fmtDate(iso) {
    try {
      return this._parseLocalDate(iso).toLocaleDateString(undefined, {
        weekday: "short", month: "short", day: "numeric",
      });
    } catch (_e) {
      return iso || "—";
    }
  }

  _fmtPrice(amount, currency) {
    if (amount == null) return null;
    const num = Number(amount);
    if (!Number.isFinite(num)) return String(amount);
    try {
      return num.toLocaleString(undefined, { style: "currency", currency: currency || "USD" });
    } catch (_e) {
      return `${num.toFixed(2)} ${currency || ""}`.trim();
    }
  }

  _esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[c]));
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
    `;
  }
}

customElements.define("hellofresh-subscription-card", HelloFreshSubscriptionCard);

console.info(
  `%c HELLOFRESH-SUBSCRIPTION-CARD %c v${SUBSCRIPTION_CARD_VERSION} `,
  "color: #fff; background: #91c11e; font-weight: 700;",
  "color: #91c11e; background: #333; font-weight: 700;"
);
