/*
 * HelloFresh Schedule Card
 * ------------------------
 * A clean, scannable overview of your HelloFresh delivery schedule: a summary of the next box
 * (delivery date, selection deadline countdown, status and price) followed by a timeline of
 * upcoming weeks — each showing its state (meals chosen / needs picking / skipped / locked).
 *
 * Like the other HelloFresh cards it reads per-week data on demand from the response-returning
 * `hellofresh.get_weeks` service (none of this is exposed as entity attributes), so one call
 * builds the whole view. Read-only; it links into the My Menu card for editing.
 *
 * Config:
 *   type: custom:hellofresh-schedule-card
 *   config_entry_id: <optional>   # required only when multiple HelloFresh accounts exist
 *   title: Schedule               # optional card header
 *   logo: true                    # optional bundled HelloFresh logo in the header
 *   max_weeks: 8                  # optional cap on timeline rows (default 8)
 *
 * No build step: hand-written ES2020 served from the integration's www/ directory.
 */

// The integration stamps its release version onto the resource URL as ?v= (cache-bust),
// so the banner reports exactly which build the browser actually loaded.
const SCHEDULE_CARD_VERSION = new URL(import.meta.url).searchParams.get("v") || "unknown";

class HelloFreshScheduleCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.shadowRoot.adoptedStyleSheets = [HelloFreshScheduleCard._sheet()];
    this._hass = null;
    this._weeks = null;
    this._account = null;
    this._loading = false;
    this._error = null;
    this._fetched = false;
  }

  setConfig(config) {
    this._config = { title: "Schedule", max_weeks: 8, ...config };
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
    return 10;
  }

  static getStubConfig() {
    return { type: "custom:hellofresh-schedule-card" };
  }

  // ---- data ----------------------------------------------------------------

  async _fetch() {
    if (!this._hass) return;
    this._loading = true;
    this._error = null;
    this._render();
    try {
      const data = {};
      if (this._config.config_entry_id) data.config_entry_id = this._config.config_entry_id;
      const result = await this._hass.callService("hellofresh", "get_weeks", data, undefined, false, true);
      const response = (result && result.response) || {};
      // Sort by delivery date so the timeline reads chronologically; undated weeks sink last.
      this._weeks = (response.weeks || [])
        .slice()
        .sort((a, b) => this._dateKey(a) - this._dateKey(b));
      this._account = response.account || null;
    } catch (err) {
      this._error = (err && err.message) || String(err);
    } finally {
      this._loading = false;
      this._render();
    }
  }

  _dateKey(week) {
    const d = week && week.delivery_date ? Date.parse(week.delivery_date) : NaN;
    return Number.isNaN(d) ? Number.POSITIVE_INFINITY : d;
  }

  // ---- week-state classification (mirrors the meal-planner card's conventions) ----

  _isEditable(week) {
    if (!week) return false;
    const actions = week.allowed_actions || {};
    if (actions.mealSwap === false) return false;
    if (week.is_skipped) return false;
    const deadline = week.selection_deadline ? Date.parse(week.selection_deadline) : null;
    if (deadline && deadline < Date.now()) return false;
    return Boolean(actions.mealSwap);
  }

  _needsSelection(week) {
    if (!this._isEditable(week)) return false;
    if (week.needs_selection != null) return Boolean(week.needs_selection);
    const required = week.meals_required || 0;
    const selected = week.meals_selected || 0;
    return required > 0 && selected < required;
  }

  // One of: skipped | needs | ready | delivered | locked — drives the icon, colour and label.
  _weekState(week) {
    if (week.is_skipped) return "skipped";
    const status = String(week.status || "").toUpperCase();
    if (status === "DELIVERED") return "delivered";
    if (this._needsSelection(week)) return "needs";
    if (this._isEditable(week)) return "ready";
    return "locked";
  }

  _isCurrent(week) {
    // The "current" box: the nearest non-past delivery (the first upcoming/today week).
    if (!week.delivery_date) return false;
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    return Date.parse(week.delivery_date) >= today.getTime();
  }

  // ---- rendering -----------------------------------------------------------

  _render() {
    if (!this.shadowRoot) return;
    this._ensureShell();
    this._shell.head.innerHTML = `
      ${this._renderLogo()}
      <span class="title-text">${this._esc(this._config ? this._config.title : "Schedule")}</span>`;
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
      if (actionEl && actionEl.getAttribute("data-action") === "refresh") this._fetch();
    });
  }

  _renderLogo() {
    const logo = this._config && this._config.logo;
    if (!logo) return "";
    const logoUrl = logo === true ? "/hellofresh/hellofresh-logo.png" : logo;
    return `<img class="logo" src="${this._esc(logoUrl)}" alt="HelloFresh">`;
  }

  _renderBody() {
    if (this._loading) return `<div class="state">Loading schedule…</div>`;
    if (this._error) {
      return `<div class="state error">Could not load schedule: ${this._esc(this._error)}</div>
        <div class="actions"><button data-action="refresh">Retry</button></div>`;
    }
    if (!this._weeks || this._weeks.length === 0) {
      return `<div class="state">No delivery weeks found.</div>
        <div class="actions"><button data-action="refresh">Refresh</button></div>`;
    }
    return `${this._renderSummary()}${this._renderTimeline()}${this._renderFooter()}`;
  }

  // The "next box" summary: the nearest upcoming delivery, or the most recent if none upcoming.
  _renderSummary() {
    const next = this._weeks.find((w) => this._isCurrent(w)) || this._weeks[this._weeks.length - 1];
    if (!next) return "";
    const order = next.order || {};
    const status = order.tracking_status || order.status || next.status || "—";
    const price = this._orderPrice(next);
    const deadline = next.selection_deadline ? new Date(next.selection_deadline) : null;
    const rel = this._relativeWeek(next);
    return `
      <div class="summary">
        <div class="sumrow">
          <span class="sumlabel">Next box</span>
          <span class="sumval">${this._esc(this._fmtDate(next.delivery_date))}${rel ? ` <span class="muted">· ${this._esc(rel)}</span>` : ""}</span>
        </div>
        ${deadline ? `
        <div class="sumrow">
          <span class="sumlabel">Selection deadline</span>
          <span class="sumval">${this._esc(this._fmtDateTime(deadline))} <span class="${this._deadlineClass(deadline)}">· ${this._esc(this._countdown(deadline))}</span></span>
        </div>` : ""}
        <div class="sumrow">
          <span class="sumlabel">Status</span>
          <span class="sumval">${this._esc(this._titleCase(status))}${price ? ` <span class="muted">· ${this._esc(price)}</span>` : ""}</span>
        </div>
      </div>`;
  }

  _renderTimeline() {
    const max = Number(this._config.max_weeks) || 8;
    // Show upcoming weeks first; if there aren't enough, backfill with the most recent past ones.
    const upcoming = this._weeks.filter((w) => this._isCurrent(w));
    const past = this._weeks.filter((w) => !this._isCurrent(w));
    const rows = (upcoming.length ? upcoming : past.slice(-max)).slice(0, max);
    if (!rows.length) return "";
    const current = upcoming[0];
    return `
      <div class="timeline">
        ${rows.map((w) => this._renderRow(w, w === current)).join("")}
      </div>`;
  }

  _renderRow(week, isCurrent) {
    const state = this._weekState(week);
    const meta = HelloFreshScheduleCard.STATE_META[state];
    const detail = this._rowDetail(week, state);
    return `
      <div class="row ${isCurrent ? "current" : ""}">
        <span class="dot ${state}" title="${this._esc(meta.label)}">${meta.icon}</span>
        <div class="rowmain">
          <div class="rowtop">
            <span class="rowdate">${this._esc(this._fmtDateShort(week.delivery_date))}</span>
            <span class="rowweek">${this._esc(week.display_name || week.week_id)}</span>
          </div>
          ${detail ? `<div class="rowsub">${detail}</div>` : ""}
        </div>
        <span class="badge ${state}">${this._esc(meta.label)}</span>
      </div>`;
  }

  _rowDetail(week, state) {
    if (state === "skipped") return `<span class="muted">No box this week</span>`;
    const required = week.meals_required || 0;
    const selected = week.meals_selected || 0;
    if (state === "needs") {
      const deadline = week.selection_deadline ? new Date(week.selection_deadline) : null;
      return `Pick ${required || "your"} meals${deadline ? ` <span class="urgent">· ${this._esc(this._countdown(deadline))}</span>` : ""}`;
    }
    if (required) {
      return `${selected}/${required} meals${week.meals_preselected ? ` <span class="muted">· preselected</span>` : ""}`;
    }
    return "";
  }

  _renderFooter() {
    return `<div class="footer"><button class="refreshbtn" data-action="refresh">Refresh</button></div>`;
  }

  // ---- helpers -------------------------------------------------------------

  _orderPrice(week) {
    const order = week.order || {};
    if (order.billed_total_price != null) {
      return this._fmtPrice(order.billed_total_price, order.billed_total_currency || order.currency);
    }
    if (order.total_price != null) return this._fmtPrice(order.total_price, order.currency);
    if (this._account && this._account.selected_plan_total_price != null) {
      return this._fmtPrice(this._account.selected_plan_total_price, this._account.selected_plan_total_price_currency);
    }
    return "";
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

  // Compact countdown to a deadline/date, e.g. "2d 4h", "5h", "passed".
  _countdown(when) {
    const ms = when.getTime() - Date.now();
    if (ms <= 0) return "passed";
    const mins = Math.floor(ms / 60000);
    const days = Math.floor(mins / 1440);
    const hours = Math.floor((mins % 1440) / 60);
    if (days > 0) return hours > 0 ? `${days}d ${hours}h left` : `${days}d left`;
    if (hours > 0) return `${hours}h left`;
    return `${mins}m left`;
  }

  _deadlineClass(deadline) {
    const ms = deadline.getTime() - Date.now();
    if (ms <= 0) return "muted";
    if (ms < 86400000) return "urgent"; // under 24h
    return "soon";
  }

  _fmtDate(iso) {
    try {
      return new Date(iso).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
    } catch (_e) {
      return iso || "—";
    }
  }

  _fmtDateShort(iso) {
    try {
      return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
    } catch (_e) {
      return iso || "—";
    }
  }

  _fmtDateTime(d) {
    try {
      return d.toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" });
    } catch (_e) {
      return String(d);
    }
  }

  _fmtPrice(amount, currency) {
    const num = Number(amount);
    if (!Number.isFinite(num)) return String(amount);
    try {
      return num.toLocaleString(undefined, { style: "currency", currency: currency || "USD" });
    } catch (_e) {
      return `${num.toFixed(2)} ${currency || ""}`.trim();
    }
  }

  _titleCase(value) {
    return String(value || "")
      .replace(/[_-]+/g, " ")
      .replace(/\b\w/g, (c) => c.toUpperCase());
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

  static get STATE_META() {
    return {
      ready: { icon: "●", label: "Set", cls: "ready" },
      needs: { icon: "!", label: "Needs picking", cls: "needs" },
      delivered: { icon: "✓", label: "Delivered", cls: "delivered" },
      skipped: { icon: "⊘", label: "Skipped", cls: "skipped" },
      locked: { icon: "●", label: "Locked", cls: "locked" },
    };
  }

  static _sheet() {
    if (!HelloFreshScheduleCard.__sheet) {
      const sheet = new CSSStyleSheet();
      sheet.replaceSync(HelloFreshScheduleCard._styles());
      HelloFreshScheduleCard.__sheet = sheet;
    }
    return HelloFreshScheduleCard.__sheet;
  }

  static _styles() {
    return `
      :host { --hf-green: #91c11e; }
      ha-card { padding: 16px 16px 16px; }
      .head { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
      .head .logo { height: 40px; width: 40px; border-radius: 8px; object-fit: cover; flex: none; }
      .title-text { font-size: 1.5em; font-weight: 500; }
      .state { text-align: center; padding: 28px 8px; color: var(--secondary-text-color); }
      .state.error { color: var(--error-color, #db4437); }
      .muted { color: var(--secondary-text-color); }
      .soon { color: var(--secondary-text-color); }
      .urgent { color: var(--error-color, #db4437); font-weight: 600; }

      .summary {
        background: var(--secondary-background-color); border-radius: 12px;
        padding: 12px 14px; margin-bottom: 14px; display: flex; flex-direction: column; gap: 6px;
      }
      .sumrow { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
      .sumlabel { font-size: 0.82em; color: var(--secondary-text-color); flex: none; }
      .sumval { font-size: 0.95em; font-weight: 600; text-align: right; }

      .timeline { display: flex; flex-direction: column; }
      .row {
        display: flex; align-items: center; gap: 12px; padding: 10px 8px;
        border-bottom: 1px solid var(--divider-color);
      }
      .row:last-child { border-bottom: none; }
      .row.current { background: color-mix(in srgb, var(--hf-green) 10%, transparent); border-radius: 10px; }
      .dot {
        flex: none; width: 26px; height: 26px; border-radius: 50%;
        display: flex; align-items: center; justify-content: center;
        font-size: 0.85em; font-weight: 700; line-height: 1;
        background: var(--secondary-background-color); color: var(--secondary-text-color);
      }
      .dot.ready { background: color-mix(in srgb, var(--hf-green) 22%, transparent); color: var(--hf-green); }
      .dot.needs { background: color-mix(in srgb, var(--warning-color, #ff9800) 22%, transparent); color: var(--warning-color, #ff9800); }
      .dot.delivered { background: color-mix(in srgb, var(--hf-green) 22%, transparent); color: var(--hf-green); }
      .dot.skipped { background: var(--secondary-background-color); color: var(--secondary-text-color); }
      .rowmain { flex: 1; min-width: 0; }
      .rowtop { display: flex; align-items: baseline; gap: 10px; }
      .rowdate { font-weight: 700; font-size: 0.95em; }
      .rowweek { font-size: 0.82em; color: var(--secondary-text-color); }
      .rowsub { font-size: 0.82em; color: var(--primary-text-color); margin-top: 2px; }
      .badge {
        flex: none; font-size: 0.72em; font-weight: 700; padding: 4px 10px; border-radius: 12px;
        background: var(--secondary-background-color); color: var(--secondary-text-color);
      }
      .badge.ready { background: color-mix(in srgb, var(--hf-green) 18%, transparent); color: var(--hf-green); }
      .badge.delivered { background: color-mix(in srgb, var(--hf-green) 18%, transparent); color: var(--hf-green); }
      .badge.needs { background: var(--warning-color, #ff9800); color: #fff; }
      .badge.skipped { background: var(--secondary-background-color); color: var(--secondary-text-color); }

      .footer { margin-top: 12px; text-align: right; }
      .refreshbtn {
        font: inherit; font-size: 0.85em; cursor: pointer;
        padding: 6px 14px; border-radius: 10px; border: 1px solid var(--divider-color);
        background: var(--secondary-background-color); color: var(--primary-text-color);
      }
      .actions { text-align: center; margin-top: 8px; }
      .actions button { padding: 6px 16px; border-radius: 8px; cursor: pointer; }
    `;
  }
}

customElements.define("hellofresh-schedule-card", HelloFreshScheduleCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "hellofresh-schedule-card",
  name: "HelloFresh Schedule Card",
  description: "A clean overview of upcoming HelloFresh deliveries and their selection state.",
});

console.info(
  `%c HELLOFRESH-SCHEDULE-CARD %c v${SCHEDULE_CARD_VERSION} `,
  "color:#fff;background:#91c11e;font-weight:700",
  "color:#91c11e;background:#fff"
);
