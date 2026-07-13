/*
 * HelloFresh Cost Card
 * --------------------
 * A running-cost view of your HelloFresh spending: a headline lifetime total, a per-month
 * roll-up (with a simple bar chart), and a recent per-box weekly list — so you can see what
 * HelloFresh actually costs you over time.
 *
 * Reads everything in one call from the response-returning `hellofresh.get_spending` service,
 * which aggregates the full billing history (up to ~200 past + upcoming charges) — so the
 * running total is your lifetime spend, not just the weeks the schedule card happens to show.
 * The same billing totals back the payment sensors, so the figures agree with them. Re-pulls
 * automatically on the integration's configured refresh interval and when a sibling card saves
 * a change. Read-only.
 *
 * Upcoming (not-yet-delivered) boxes are shown but marked "upcoming" and are excluded from the
 * running total — a running cost is money already spent.
 *
 * Config:
 *   type: custom:hellofresh-cost-card
 *   config_entry_id: <optional>   # required only when multiple HelloFresh accounts exist
 *   title: Cost                   # optional card header
 *   logo: true                    # optional bundled HelloFresh logo in the header
 *   chart: true                   # monthly-cost bar chart (default true; set false to hide)
 *   chart_months: 12              # months spanned by the chart (default 12 — the last year)
 *   months: 6                     # months to show in the roll-up list (default 6, 0 hides it)
 *   weeks: 6                      # recent boxes to list (default 6, 0 hides the section)
 *
 * No build step: hand-written ES2020 served from the integration's www/ directory.
 */

// The integration stamps its release version onto the resource URL as ?v= (cache-bust), so the
// banner reports exactly which build the browser actually loaded.
const COST_CARD_VERSION = new URL(import.meta.url).searchParams.get("v") || "unknown";

class HelloFreshCostCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.shadowRoot.adoptedStyleSheets = [HelloFreshCostCard._sheet()];
    this._hass = null;
    this._spending = null; // { weeks, months, total } from get_spending
    this._loading = false;
    this._error = null;
    this._fetched = false;
    this._lastFetched = 0;
    this._tickTimer = null;
    this._onDataChanged = (ev) => this._receiveDataChanged(ev);
    this._onVisibility = () => this._onBecameVisible();
  }

  connectedCallback() {
    window.addEventListener(HelloFreshCostCard.DATA_CHANGED_EVENT, this._onDataChanged);
    document.addEventListener("visibilitychange", this._onVisibility);
    this._startTick();
    if (this._spending) this._refreshIfStale();
  }

  disconnectedCallback() {
    window.removeEventListener(HelloFreshCostCard.DATA_CHANGED_EVENT, this._onDataChanged);
    document.removeEventListener("visibilitychange", this._onVisibility);
    this._stopTick();
  }

  setConfig(config) {
    this._config = {
      title: "Cost",
      chart: true,
      chart_months: 12,
      months: 6,
      weeks: 6,
      ...config,
    };
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
    return { type: "custom:hellofresh-cost-card" };
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
        "hellofresh", "get_spending", data, undefined, false, true
      );
      this._spending = (result && result.response) || { weeks: [], months: [], total: null };
    } catch (err) {
      this._error = (err && err.message) || String(err);
    } finally {
      this._lastFetched = Date.now();
      this._loading = false;
      this._render();
    }
  }

  // ---- auto-refresh (same contract as the schedule/subscription cards) -------
  // The spending ledger changes at most once a week (a new box is billed), so there's no need
  // for a per-account refresh_interval read here — the shared 3h default plus the data-changed
  // event and visibility re-check keep it fresh without hammering the billing endpoint.

  _refetchIntervalMs() {
    return 180 * 60000;
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

  // ---- rendering -------------------------------------------------------------

  _render() {
    if (!this.shadowRoot) return;
    this._ensureShell();
    this._shell.head.innerHTML = `
      ${this._renderLogo()}
      <span class="title-text">${this._esc(this._config ? this._config.title : "Cost")}</span>
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
      if (actionEl.getAttribute("data-action") === "refresh") this._fetch();
    });
  }

  _renderLogo() {
    const logo = this._config && this._config.logo;
    if (!logo) return "";
    const logoUrl = logo === true ? "/hellofresh/hellofresh-logo.png" : logo;
    return `<img class="logo" src="${this._esc(logoUrl)}" alt="HelloFresh">`;
  }

  _renderBody() {
    if (!this._spending) {
      if (this._loading || !this._fetched) return `<div class="state">Loading spending…</div>`;
      return `<div class="state error">Could not load spending: ${this._esc(this._error || "no data")}</div>
        <div class="actions"><button data-action="refresh">Retry</button></div>`;
    }
    const s = this._spending;
    const weeks = Array.isArray(s.weeks) ? s.weeks : [];
    const months = Array.isArray(s.months) ? s.months : [];
    if (!weeks.length && !months.length && !s.total) {
      return `<div class="state">No spending history yet.</div>`;
    }
    const notice = this._error
      ? `<div class="notice">Refresh failed: ${this._esc(this._error)}
           <button class="refreshbtn" data-action="refresh">Retry</button></div>`
      : "";
    return `<div class="${this._loading ? "reloading" : ""}">
      ${notice}
      ${this._renderTotal(s.total)}
      ${this._renderChart(months)}
      ${this._renderMonths(months)}
      ${this._renderWeeks(weeks)}
    </div>`;
  }

  // The headline: the running lifetime total (past deliveries only) and its box count, plus a
  // derived per-box average — the single "what has HelloFresh cost me" figure.
  _renderTotal(total) {
    if (!total || total.amount == null) return "";
    const boxes = Number(total.box_count) || 0;
    const avg = boxes > 0 ? total.amount / boxes : null;
    return `
      <div class="total">
        <div class="total-main">
          <span class="total-label">Total spent</span>
          <span class="total-amount">${this._esc(this._fmtPrice(total.amount, total.currency))}</span>
        </div>
        <div class="total-sub">
          ${boxes} box${boxes === 1 ? "" : "es"}${
            avg != null ? ` · ${this._esc(this._fmtPrice(avg, total.currency))} avg` : ""
          }
        </div>
      </div>`;
  }

  // A histogram of monthly box cost over the last year (config.chart_months slots, default 12),
  // with a dollar amount printed above each priced bar and a trend line connecting the bar tops.
  // Rendered as a self-contained inline SVG — no external chart library, so it works inside the
  // CSP-restricted card sandbox. Months with no delivery show an empty slot so gaps
  // (paused/skipped months) read correctly on the timeline rather than vanishing.
  _renderChart(months) {
    if (this._config.chart === false || !months.length) return "";
    const span = Math.max(1, Math.min(24, Number(this._config.chart_months) || 12));
    const series = this._denseMonths(months, span);
    if (!series.length) return "";
    const currency = series.find((m) => m.currency)?.currency || null;
    const max = series.reduce((m, x) => Math.max(m, x.amount), 0);

    // Geometry (viewBox units; the SVG scales responsively to the card width). The top padding
    // leaves room for the vertical value labels standing above the tallest bar.
    const W = 320;
    const H = 168;
    const padL = 4;
    const padR = 4;
    const padTop = 34; // headroom for the "$" value labels above the bars
    const axisH = 22; // room for the month labels under the baseline
    const plotH = H - padTop - axisH;
    const baseY = padTop + plotH;
    const n = series.length;
    const slot = (W - padL - padR) / n;
    const barW = Math.max(3, slot * 0.6);

    // Precompute each slot's bar top-center; priced months feed the trend line.
    const points = series.map((m, i) => {
      const cx = padL + i * slot + slot / 2;
      const h = max > 0 ? (m.amount / max) * plotH : 0;
      return { cx, top: baseY - h, h, m };
    });

    const bars = points
      .map(({ cx, top, h, m }, i) => {
        const x = cx - barW / 2;
        const cls = m.amount <= 0 ? "cbar empty" : m.upcoming ? "cbar upcoming" : "cbar";
        const title = `${this._fmtMonth(m.month)}: ${
          m.amount > 0 ? this._fmtPrice(m.amount, m.currency || currency) : "no box"
        }`;
        const [yr, mo] = m.month.split("-");
        const monthLabel = this._monthInitial(Number(mo));
        const yearLabel = mo === "01" || i === 0 ? yr.slice(2) : "";
        // Dollar amount standing vertically above the bar (compact, no cents). Skipped for
        // empty months. Rotated -90° so it fits above a narrow 12-bar column.
        const valueLabel =
          m.amount > 0
            ? `<text class="cvalue" x="${cx.toFixed(1)}" y="${(top - 3).toFixed(1)}"
                     transform="rotate(-90 ${cx.toFixed(1)} ${(top - 3).toFixed(1)})">${this._esc(
                       this._fmtPriceCompact(m.amount, m.currency || currency)
                     )}</text>`
            : "";
        return `
          <rect class="${cls}" x="${x.toFixed(1)}" y="${top.toFixed(1)}"
                width="${barW.toFixed(1)}" height="${Math.max(0, h).toFixed(1)}" rx="1.5">
            <title>${this._esc(title)}</title>
          </rect>
          ${valueLabel}
          <text class="clabel" x="${cx.toFixed(1)}" y="${baseY + 12}">${this._esc(monthLabel)}</text>
          ${
            yearLabel
              ? `<text class="cyear" x="${cx.toFixed(1)}" y="${baseY + 20}">${this._esc(yearLabel)}</text>`
              : ""
          }`;
      })
      .join("");

    // Trend line across priced months only (an empty month has no meaningful cost to plot, so
    // the line bridges the gap rather than dipping to zero). Needs at least two priced points.
    const priced = points.filter((p) => p.m.amount > 0);
    const trend =
      priced.length >= 2
        ? `<polyline class="ctrend"
             points="${priced.map((p) => `${p.cx.toFixed(1)},${p.top.toFixed(1)}`).join(" ")}" />
           ${priced
             .map((p) => `<circle class="cdot" cx="${p.cx.toFixed(1)}" cy="${p.top.toFixed(1)}" r="2" />`)
             .join("")}`
        : "";

    const maxLabel = max > 0 ? this._fmtPrice(max, currency) : "";
    return `<div class="section">
      <div class="stitle">Monthly box cost${maxLabel ? ` · peak ${this._esc(maxLabel)}` : ""}</div>
      <svg class="chart" viewBox="0 0 ${W} ${H}"
           role="img" aria-label="Monthly HelloFresh box cost over the last year">
        <line class="cbaseline" x1="${padL}" y1="${baseY}" x2="${W - padR}" y2="${baseY}" />
        ${bars}
        ${trend}
      </svg>
    </div>`;
  }

  // Expand the sparse month list (delivery months only) into a CONTIGUOUS run of `span` months
  // ending at the most recent month present, filling absent months with a zero-cost slot so the
  // chart's x-axis is an unbroken timeline. Anchored on the newest data month (not "today")
  // because the card has no clock of its own.
  _denseMonths(months, span) {
    const byKey = new Map();
    let newest = null;
    for (const m of months) {
      if (typeof m.month !== "string" || !/^\d{4}-\d{2}$/.test(m.month)) continue;
      byKey.set(m.month, m);
      if (newest === null || m.month > newest) newest = m.month;
    }
    if (newest === null) return [];
    let year = Number(newest.slice(0, 4));
    let mon = Number(newest.slice(5, 7)); // 1-12
    const out = [];
    for (let i = 0; i < span; i += 1) {
      const key = `${year}-${String(mon).padStart(2, "0")}`;
      const existing = byKey.get(key);
      out.push(
        existing || { month: key, amount: 0, currency: null, box_count: 0, upcoming: false }
      );
      mon -= 1;
      if (mon === 0) {
        mon = 12;
        year -= 1;
      }
    }
    // Built newest-first above; the chart reads left-to-right oldest-to-newest.
    return out.reverse().map((m) => ({ ...m, amount: Number(m.amount) || 0 }));
  }

  // Per-month roll-up (newest first, capped by config.months): each month's total with a bar
  // scaled to the largest month in view, so the trend is scannable at a glance.
  _renderMonths(months) {
    const cap = Number(this._config.months);
    if (!(cap > 0) || !months.length) return "";
    const shown = months.slice(0, cap);
    const max = shown.reduce((m, x) => Math.max(m, Number(x.amount) || 0), 0) || 1;
    const rows = shown
      .map((m) => {
        const amount = Number(m.amount) || 0;
        const pct = Math.max(2, Math.round((amount / max) * 100));
        const boxes = Number(m.box_count) || 0;
        return `
          <div class="mrow${m.upcoming ? " upcoming" : ""}">
            <span class="mlabel">${this._esc(this._fmtMonth(m.month))}</span>
            <span class="mbar"><span class="mfill" style="width:${pct}%"></span></span>
            <span class="mval">${this._esc(this._fmtPrice(amount, m.currency))}
              <span class="mcount">${boxes} box${boxes === 1 ? "" : "es"}</span></span>
          </div>`;
      })
      .join("");
    return `<div class="section">
      <div class="stitle">By month</div>
      <div class="months">${rows}</div>
    </div>`;
  }

  // Recent per-box list (newest first, capped by config.weeks): delivery date + amount, with an
  // "upcoming" tag on boxes that haven't been delivered/charged as spent yet.
  _renderWeeks(weeks) {
    const cap = Number(this._config.weeks);
    if (!(cap > 0) || !weeks.length) return "";
    const rows = weeks
      .slice(0, cap)
      .map(
        (w) => `
          <div class="wrow${w.upcoming ? " upcoming" : ""}">
            <span class="wdate">${this._esc(this._fmtDate(w.delivery_date))}${
              w.upcoming ? ` <span class="tag">upcoming</span>` : ""
            }</span>
            <span class="wval">${this._esc(this._fmtPrice(w.amount, w.currency))}</span>
          </div>`
      )
      .join("");
    return `<div class="section">
      <div class="stitle">Recent boxes</div>
      <div class="weeks">${rows}</div>
    </div>`;
  }

  // ---- helpers ---------------------------------------------------------------

  // Parse a date anchored to LOCAL midnight — bare "YYYY-MM-DD" strings otherwise parse as UTC
  // midnight and render as the previous day west of UTC.
  _parseLocalDate(value) {
    const m = typeof value === "string" && /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
    if (m) return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    return new Date(value);
  }

  _fmtDate(iso) {
    try {
      return this._parseLocalDate(iso).toLocaleDateString(undefined, {
        month: "short", day: "numeric", year: "numeric",
      });
    } catch (_e) {
      return iso || "—";
    }
  }

  // "2026-06" -> "Jun 2026".
  _fmtMonth(key) {
    const m = typeof key === "string" && /^(\d{4})-(\d{2})$/.exec(key);
    if (!m) return key || "—";
    try {
      return new Date(Number(m[1]), Number(m[2]) - 1, 1).toLocaleDateString(undefined, {
        month: "short", year: "numeric",
      });
    } catch (_e) {
      return key;
    }
  }

  // Single-letter month label for the chart axis (J F M A M J J A S O N D), 1-indexed.
  _monthInitial(mon) {
    return "JFMAMJJASOND"[(Number(mon) - 1) % 12] || "";
  }

  _fmtPrice(amount, currency) {
    if (amount == null) return "—";
    const num = Number(amount);
    if (!Number.isFinite(num)) return String(amount);
    try {
      return num.toLocaleString(undefined, { style: "currency", currency: currency || "USD" });
    } catch (_e) {
      return `${num.toFixed(2)} ${currency || ""}`.trim();
    }
  }

  // Compact whole-dollar price for on-bar chart labels (e.g. "$183") — drops the cents so the
  // vertical label stays short above a narrow bar.
  _fmtPriceCompact(amount, currency) {
    const num = Number(amount);
    if (!Number.isFinite(num)) return "";
    try {
      return num.toLocaleString(undefined, {
        style: "currency",
        currency: currency || "USD",
        maximumFractionDigits: 0,
      });
    } catch (_e) {
      return `${Math.round(num)}`;
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
    if (!HelloFreshCostCard.__sheet) {
      const sheet = new CSSStyleSheet();
      sheet.replaceSync(HelloFreshCostCard._styles());
      HelloFreshCostCard.__sheet = sheet;
    }
    return HelloFreshCostCard.__sheet;
  }

  static _styles() {
    return `
      :host { display: block; }
      ha-card { padding: 16px; }
      .head { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
      .logo { height: 40px; width: 40px; border-radius: 8px; object-fit: cover; flex: none; }
      .title-text { font-size: 1.5em; font-weight: 500; }
      .state { padding: 24px 8px; text-align: center; color: var(--secondary-text-color); }
      .state.error { color: var(--error-color, #db4437); }
      .reloading { opacity: 0.6; transition: opacity 0.2s; }
      .notice {
        margin: 8px 0; padding: 8px 12px; border-radius: 8px; font-size: 0.85em;
        background: var(--secondary-background-color);
      }
      .actions { text-align: center; margin-top: 8px; }
      .actions button { padding: 6px 16px; border-radius: 8px; cursor: pointer; }
      .refreshbtn {
        margin-left: auto; flex: none;
        font-size: 0.85em; padding: 5px 12px; border-radius: 14px;
        border: 1px solid var(--divider-color); background: var(--card-background-color);
        color: var(--primary-text-color); cursor: pointer;
      }
      .refreshbtn:disabled { opacity: 0.5; cursor: default; }

      .total {
        margin: 8px 0 4px; padding: 12px 14px; border-radius: 12px;
        background: var(--secondary-background-color);
      }
      .total-main { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
      .total-label {
        font-size: 0.72em; text-transform: uppercase; letter-spacing: 0.04em;
        color: var(--secondary-text-color);
      }
      .total-amount { font-size: 1.6em; font-weight: 700; color: var(--primary-color); }
      .total-sub { font-size: 0.82em; color: var(--secondary-text-color); margin-top: 2px; }

      .section { margin-top: 14px; }
      .stitle {
        font-size: 0.68em; text-transform: uppercase; letter-spacing: 0.04em; font-weight: 600;
        color: var(--secondary-text-color); margin-bottom: 6px;
      }

      .chart { display: block; width: 100%; height: auto; overflow: visible; }
      .cbaseline { stroke: var(--divider-color); stroke-width: 1; }
      .cbar { fill: var(--primary-color); }
      .cbar.upcoming { fill: var(--primary-color); opacity: 0.45; }
      .cbar.empty { fill: var(--divider-color); opacity: 0.5; }
      .clabel { fill: var(--secondary-text-color); font-size: 8px; text-anchor: middle; }
      .cyear { fill: var(--secondary-text-color); font-size: 7px; text-anchor: middle; opacity: 0.8; }
      .cvalue {
        fill: var(--primary-text-color); font-size: 8px; font-weight: 600;
        text-anchor: start; /* rotated -90°, so start = bottom, growing upward from the bar top */
      }
      .ctrend {
        fill: none; stroke: var(--accent-color, #ff9800); stroke-width: 1.5;
        stroke-linejoin: round; stroke-linecap: round;
      }
      .cdot { fill: var(--accent-color, #ff9800); }

      .months { display: flex; flex-direction: column; gap: 6px; }
      .mrow { display: grid; grid-template-columns: 4.5em 1fr auto; align-items: center; gap: 8px; }
      .mrow.upcoming { opacity: 0.65; }
      .mlabel { font-size: 0.82em; color: var(--secondary-text-color); }
      .mbar {
        height: 8px; border-radius: 4px; overflow: hidden;
        background: var(--divider-color); min-width: 20px;
      }
      .mfill { display: block; height: 100%; background: var(--primary-color); border-radius: 4px; }
      .mval { font-size: 0.88em; font-weight: 600; text-align: right; white-space: nowrap; }
      .mcount {
        display: block; font-size: 0.72em; font-weight: 400; color: var(--secondary-text-color);
      }

      .weeks { display: flex; flex-direction: column; }
      .wrow {
        display: flex; align-items: center; justify-content: space-between; gap: 8px;
        padding: 6px 0; border-bottom: 1px solid var(--divider-color);
      }
      .wrow:last-child { border-bottom: none; }
      .wrow.upcoming { opacity: 0.7; }
      .wdate { font-size: 0.88em; color: var(--primary-text-color); }
      .wval { font-size: 0.9em; font-weight: 600; white-space: nowrap; }
      .tag {
        font-size: 0.65em; text-transform: uppercase; letter-spacing: 0.03em;
        color: var(--secondary-text-color); border: 1px solid var(--divider-color);
        border-radius: 8px; padding: 0 5px; margin-left: 4px; vertical-align: middle;
      }
    `;
  }
}

customElements.define("hellofresh-cost-card", HelloFreshCostCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "hellofresh-cost-card",
  name: "HelloFresh Cost Card",
  description: "Running HelloFresh cost: lifetime total, monthly roll-up, and recent boxes.",
});

console.info(
  `%c HELLOFRESH-COST-CARD %c v${COST_CARD_VERSION} `,
  "color: #fff; background: #91c11e; font-weight: 700;",
  "color: #91c11e; background: #333; font-weight: 700;"
);
