/*
 * HelloFresh Schedule Card
 * ------------------------
 * A clean, scannable overview of your HelloFresh delivery schedule: a summary of the next box
 * (delivery date, selection deadline countdown, status and price), a month calendar with every
 * delivery day marked by its state, and a timeline of recent past and upcoming weeks — each
 * showing its delivery date, box status, and state (meals chosen / needs picking / skipped /
 * delivered / locked).
 *
 * Like the other HelloFresh cards it reads per-week data on demand from the response-returning
 * `hellofresh.get_weeks` service (none of this is exposed as entity attributes), so one call
 * builds the whole view — including the calendar, which replaces a separate
 * `calendar.delivery_schedule` dashboard widget. Read-only; clicking a delivery day broadcasts
 * the week-sync event so the meal-planner/market cards jump to that week.
 *
 * Config:
 *   type: custom:hellofresh-schedule-card
 *   config_entry_id: <optional>   # required only when multiple HelloFresh accounts exist
 *   title: Schedule               # optional card header
 *   logo: true                    # optional bundled HelloFresh logo in the header
 *   calendar: true                # optional month calendar section (default true); the timeline
 *                                 # below it follows the displayed month
 *   max_weeks: 8                  # timeline cap on upcoming rows (default 8; calendar: false only)
 *   past_weeks: 4                 # recent past deliveries in the timeline (default 4, 0 hides;
 *                                 # calendar: false only)
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
    // First-of-month currently shown in the calendar; null = the real current month.
    this._calMonth = null;
  }

  setConfig(config) {
    this._config = { title: "Schedule", max_weeks: 8, past_weeks: 4, calendar: true, ...config };
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
    return this._config && this._config.calendar === false ? 10 : 13;
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
      const sorted = (response.weeks || [])
        .slice()
        .sort((a, b) => this._dateKey(a) - this._dateKey(b));
      this._weeks = this._displayWeeks(sorted);
      this._account = response.account || null;
    } catch (err) {
      this._error = (err && err.message) || String(err);
    } finally {
      this._loading = false;
      this._render();
    }
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

  _dateKey(week) {
    const d = week && week.delivery_date ? this._parseLocalDate(week.delivery_date).getTime() : NaN;
    return Number.isNaN(d) ? Number.POSITIVE_INFINITY : d;
  }

  // The deliveries range extends further ahead than HelloFresh publishes menus, so the far
  // future comes back as empty scheduling shells with no meal data. Mirror the meal-planner
  // card: keep every past/current week, and future weeks only while they still carry meals —
  // stop at the first empty one so later shells never resurface.
  _displayWeeks(weeks) {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const result = [];
    let futureMenuEnded = false;
    for (const week of weeks) {
      const isFuture = week.delivery_date
        ? this._parseLocalDate(week.delivery_date).getTime() >= today.getTime()
        : true; // undated weeks can't be anchored to the past
      if (!isFuture) {
        result.push(week);
        continue;
      }
      // A skipped/paused future week legitimately has no meals — show it (the schedule must
      // reflect the gap) and don't let it end the published-menu chain.
      if (week.is_skipped) {
        if (!futureMenuEnded) result.push(week);
        continue;
      }
      const hasMeals = (week.recipes || []).length > 0;
      if (futureMenuEnded || !hasMeals) {
        futureMenuEnded = true;
        continue;
      }
      result.push(week);
    }
    return result;
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
    return this._parseLocalDate(week.delivery_date).getTime() >= today.getTime();
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
      if (!actionEl) return;
      const action = actionEl.getAttribute("data-action");
      if (action === "refresh") this._fetch();
      else if (action === "cal-prev") this._shiftCalMonth(-1);
      else if (action === "cal-next") this._shiftCalMonth(1);
      else if (action === "cal-today") {
        this._calMonth = null;
        this._render();
      } else if (action === "cal-week") {
        // Clicking a delivery day drives the meal-planner/market cards to that week — the
        // same window event those cards use to stay in step, plus the same localStorage key
        // so cards on other dashboard tabs pick the week up when they reconnect.
        const weekId = actionEl.getAttribute("data-week-id");
        if (weekId) {
          const accountKey = (this._config && this._config.config_entry_id) || "default";
          try {
            window.localStorage.setItem(`hellofresh:selected-week:${accountKey}`, weekId);
          } catch (_e) {
            /* storage unavailable (private mode) — live sync below still works */
          }
          window.dispatchEvent(
            new CustomEvent("hellofresh-week-selected", { detail: { weekId, accountKey } })
          );
        }
      }
    });
  }

  _shiftCalMonth(delta) {
    const shown = this._shownCalMonth();
    this._calMonth = new Date(shown.getFullYear(), shown.getMonth() + delta, 1);
    this._render();
  }

  _shownCalMonth() {
    if (this._calMonth) return this._calMonth;
    const d = new Date();
    return new Date(d.getFullYear(), d.getMonth(), 1);
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
    return `${this._renderSummary()}${this._renderCalendar()}${this._renderTimeline()}${this._renderFooter()}`;
  }

  // A month grid with every delivery day marked by its week's state (replaces the separate
  // calendar.delivery_schedule dashboard widget). Days with a delivery are clickable and sync
  // the sibling HelloFresh cards to that week.
  _renderCalendar() {
    if (this._config.calendar === false) return "";
    const shown = this._shownCalMonth();
    const year = shown.getFullYear();
    const month = shown.getMonth();
    const byDay = new Map();
    for (const week of this._weeks) {
      // Delivered weeks sit on the day the box ACTUALLY arrived (tracking timestamp);
      // upcoming weeks on their scheduled date.
      const when = week.delivered_at || week.delivery_date;
      if (!when) continue;
      const d = this._parseLocalDate(when);
      byDay.set(new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime(), week);
    }
    const today = new Date();
    const todayKey = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime();
    // Weekday header, Sunday-first (2023-01-01 was a Sunday), in the viewer's locale.
    const dows = [...Array(7)]
      .map((_, i) => new Date(2023, 0, 1 + i).toLocaleDateString(undefined, { weekday: "narrow" }))
      .map((d) => `<span class="cal-dow">${this._esc(d)}</span>`)
      .join("");
    const cells = [];
    const firstDow = new Date(year, month, 1).getDay();
    for (let i = 0; i < firstDow; i += 1) cells.push(`<span class="cal-day blank"></span>`);
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    for (let day = 1; day <= daysInMonth; day += 1) {
      const key = new Date(year, month, day).getTime();
      const week = byDay.get(key);
      const isToday = key === todayKey ? " today" : "";
      if (!week) {
        cells.push(`<span class="cal-day${isToday}"><span class="cal-num">${day}</span></span>`);
        continue;
      }
      const state = this._weekState(week);
      const title = `${week.display_name || week.week_id} — ${this._stateLabel(week, state)}`;
      cells.push(`
        <button class="cal-day has ${state}${isToday}" data-action="cal-week"
          data-week-id="${this._esc(week.week_id)}" title="${this._esc(title)}">
          <span class="cal-num">${day}</span><span class="cal-mark ${state}"></span>
        </button>`);
    }
    const monthLabel = shown.toLocaleDateString(undefined, { month: "long", year: "numeric" });
    const isCurrentMonth = year === today.getFullYear() && month === today.getMonth();
    return `
      <div class="calendar">
        <div class="cal-head">
          <button class="cal-nav" data-action="cal-prev" title="Previous month">‹</button>
          <span class="cal-title">${this._esc(monthLabel)}</span>
          <button class="cal-nav" data-action="cal-next" title="Next month">›</button>
          ${isCurrentMonth ? "" : `<button class="cal-nav cal-today-btn" data-action="cal-today">Today</button>`}
        </div>
        <div class="cal-grid">${dows}${cells.join("")}</div>
      </div>`;
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
    const current = this._weeks.find((w) => this._isCurrent(w));
    // With the calendar shown, the timeline mirrors the MONTH IN FOCUS: navigating months
    // swaps the rows to that month's delivery weeks, so calendar and list always agree.
    // Without the calendar there's no month cursor, so fall back to the fixed
    // past_weeks + max_weeks window.
    const rows =
      this._config.calendar === false ? this._defaultTimelineRows() : this._monthTimelineRows();
    if (!rows.length) {
      const monthLabel = this._shownCalMonth().toLocaleDateString(undefined, {
        month: "long",
        year: "numeric",
      });
      return this._config.calendar === false
        ? ""
        : `<div class="timeline"><div class="state">No deliveries in ${this._esc(monthLabel)}.</div></div>`;
    }
    return `
      <div class="timeline">
        ${rows.map((w) => this._renderRow(w, w === current, !this._isCurrent(w))).join("")}
      </div>`;
  }

  // The delivery weeks whose (actual or scheduled) delivery day falls in the calendar's
  // displayed month.
  _monthTimelineRows() {
    const shown = this._shownCalMonth();
    return this._weeks.filter((week) => {
      const when = week.delivered_at || week.delivery_date;
      if (!when) return false;
      const d = this._parseLocalDate(when);
      return d.getFullYear() === shown.getFullYear() && d.getMonth() === shown.getMonth();
    });
  }

  _defaultTimelineRows() {
    const max = Number(this._config.max_weeks) || 8;
    const pastRaw = Number(this._config.past_weeks);
    const pastMax = Number.isFinite(pastRaw) ? Math.max(0, pastRaw) : 4;
    const upcoming = this._weeks.filter((w) => this._isCurrent(w));
    const past = this._weeks.filter((w) => !this._isCurrent(w));
    // Recent past deliveries lead (dimmed), then the upcoming weeks; if nothing is upcoming,
    // fall back to the most recent history so the card is never empty.
    const pastRows = upcoming.length ? past.slice(-pastMax) : past.slice(-max);
    return [...pastRows, ...upcoming.slice(0, max)];
  }

  // Whether HelloFresh auto-picked (preselected) this week's meals rather than the customer
  // choosing them. Mirrors the meal-planner card: a skipped/paused week's preselection never
  // ships, so it doesn't count.
  _isPreselected(week) {
    if (week.is_skipped || String(week.status || "").toUpperCase() === "PAUSED") return false;
    return Boolean(week.meals_preselected);
  }

  // The badge/tooltip label for a week's state. When a week is in the "needs" state BECAUSE
  // HelloFresh preselected its meals, showing both an amber "Needs picking" badge and an
  // amber "Preselected" badge is redundant — collapse them into a single "Preselected" badge.
  // A genuinely under-filled week (too few meals, not auto-picked) keeps "Needs picking".
  _stateLabel(week, state) {
    if (state === "needs" && this._isPreselected(week)) return "Preselected";
    return HelloFreshScheduleCard.STATE_META[state].label;
  }

  _renderRow(week, isCurrent, isPast) {
    const state = this._weekState(week);
    const meta = HelloFreshScheduleCard.STATE_META[state];
    const label = this._stateLabel(week, state);
    const detail = this._rowDetail(week, state);
    // Standalone Preselected badge only when the state badge doesn't already say it
    // (e.g. a locked preselected week whose deadline passed).
    const preselected =
      this._isPreselected(week) && label !== "Preselected"
        ? `<span class="badge preselected" title="HelloFresh auto-picked these meals — review and adjust before the deadline.">Preselected</span>`
        : "";
    const badgeTitle =
      label === "Preselected"
        ? "HelloFresh auto-picked these meals — review and adjust before the deadline."
        : label;
    return `
      <div class="row ${isCurrent ? "current" : ""}${isPast ? " past" : ""}">
        <span class="dot ${state}" title="${this._esc(label)}">${meta.icon}</span>
        <div class="rowmain">
          <div class="rowtop">
            <span class="rowdate">${this._esc(this._fmtDateShort(week.delivered_at || week.delivery_date))}</span>
            <span class="rowweek">${this._esc(week.display_name || week.week_id)}</span>
          </div>
          ${detail ? `<div class="rowsub">${detail}</div>` : ""}
          ${state === "skipped" ? "" : this._rowTracking(week)}
        </div>
        ${preselected}
        <span class="badge ${state}" title="${this._esc(badgeTitle)}">${this._esc(label)}</span>
      </div>`;
  }

  // The box/tracking status for a week (e.g. "Preparing", "On the way", "Delivered"), when it
  // adds information beyond the state badge — a status that just repeats the badge is dropped.
  _rowStatus(week, badgeLabel) {
    const order = week.order || {};
    const status = this._titleCase(order.tracking_status || order.status || week.status || "");
    if (!status || status.toLowerCase() === badgeLabel.toLowerCase()) return "";
    return status;
  }

  // Distinct market add-ons selected for a week (mirrors the market card's selection test).
  _marketCount(week) {
    return (week.market_items || []).filter((item) => {
      const q = Number(item.selected_quantity);
      return Number.isFinite(q) ? q > 0 : item.is_selected === true;
    }).length;
  }

  // The week's own billed/cart price — deliberately NOT the account plan-price fallback the
  // summary uses, so an old week never shows today's plan price as if it were its bill.
  _weekPrice(week) {
    const order = week.order || {};
    if (order.billed_total_price != null) {
      return this._fmtPrice(order.billed_total_price, order.billed_total_currency || order.currency);
    }
    if (order.total_price != null) return this._fmtPrice(order.total_price, order.currency);
    return "";
  }

  // Carrier + tracking number line (number linked when a tracking URL exists). Tracking data
  // only appears once HelloFresh ships the box, so presence is the gate — this naturally
  // covers shipping and delivered weeks and stays absent on unshipped/skipped ones.
  _rowTracking(week) {
    const order = week.order || {};
    if (!order.carrier && !order.tracking_number) return "";
    const parts = [];
    if (order.carrier) parts.push(this._esc(order.carrier));
    if (order.tracking_number) {
      const num = this._esc(order.tracking_number);
      parts.push(
        order.tracking_url
          ? `<a href="${this._esc(order.tracking_url)}" target="_blank" rel="noopener">${num}</a>`
          : num
      );
    }
    return `<div class="rowtrack">${parts.join(" · ")}</div>`;
  }

  _rowDetail(week, state) {
    if (state === "skipped") return `<span class="muted">No box this week</span>`;
    const required = week.meals_required || 0;
    const selected = week.meals_selected || 0;
    if (state === "needs") {
      const deadline = week.selection_deadline ? new Date(week.selection_deadline) : null;
      const deadlineSuffix = deadline
        ? ` <span class="urgent">· ${this._esc(this._countdown(deadline))}</span>`
        : "";
      // A preselected week is already full — the action is reviewing HelloFresh's picks,
      // not picking from scratch.
      if (this._isPreselected(week)) {
        return `Review meals${deadlineSuffix}`;
      }
      return `Pick ${required || "your"} meals${deadlineSuffix}`;
    }
    const parts = [];
    // Show the ACTUAL selected count, not selected/required: a week can be resized to more
    // or fewer meals than the plan (the box SKU changes with it), so a plan-based "3/3" is
    // wrong for a 4-meal week. The plan count only appears when it differs, as context.
    // (Preselection is surfaced as a row badge, not here.)
    if (selected) {
      const plan =
        required && required !== selected ? ` <span class="muted">(plan: ${required})</span>` : "";
      parts.push(`${selected} meal${selected === 1 ? "" : "s"}${plan}`);
    } else if (required) {
      parts.push(`<span class="muted">No meals selected</span>`);
    }
    const market = this._marketCount(week);
    if (market) parts.push(`${market} market item${market === 1 ? "" : "s"}`);
    const price = this._weekPrice(week);
    if (price) parts.push(`<span class="muted">${this._esc(price)}</span>`);
    const status = this._rowStatus(week, HelloFreshScheduleCard.STATE_META[state].label);
    if (status) parts.push(`<span class="muted">${this._esc(status)}</span>`);
    return parts.join(" · ");
  }

  _renderFooter() {
    // Same compact ↻ pill the meal-planner and market cards use for their refresh action.
    return `<div class="footer"><button class="refreshbtn" data-action="refresh" title="Refresh" ${this._loading ? "disabled" : ""}>↻</button></div>`;
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
    const d = this._parseLocalDate(week.delivery_date);
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
      return this._parseLocalDate(iso).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
    } catch (_e) {
      return iso || "—";
    }
  }

  _fmtDateShort(iso) {
    try {
      return this._parseLocalDate(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
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
      ready: { icon: "●", label: "Editable", cls: "ready" },
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

      .calendar {
        border: 1px solid var(--divider-color); border-radius: 12px;
        padding: 10px 12px 12px; margin-bottom: 14px;
      }
      .cal-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
      .cal-title { flex: 1; text-align: center; font-weight: 600; font-size: 0.95em; }
      .cal-nav {
        font: inherit; font-size: 0.9em; line-height: 1; cursor: pointer;
        padding: 4px 10px; border-radius: 8px; border: 1px solid var(--divider-color);
        background: var(--secondary-background-color); color: var(--primary-text-color);
      }
      .cal-today-btn { font-size: 0.78em; }
      .cal-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 2px; }
      .cal-dow {
        text-align: center; font-size: 0.7em; font-weight: 700;
        color: var(--secondary-text-color); padding-bottom: 4px;
      }
      .cal-day {
        position: relative; display: flex; flex-direction: column; align-items: center;
        justify-content: flex-start; min-height: 34px; padding: 3px 0 2px;
        border: none; border-radius: 8px; background: none; font: inherit;
        color: var(--primary-text-color);
      }
      .cal-day.blank { min-height: 0; }
      .cal-num { font-size: 0.8em; line-height: 1.4; }
      .cal-day.today { box-shadow: inset 0 0 0 1.5px var(--primary-color); }
      .cal-day.has { cursor: pointer; background: var(--secondary-background-color); }
      .cal-day.has .cal-num { font-weight: 700; }
      .cal-mark { width: 7px; height: 7px; border-radius: 50%; margin-top: 2px; }
      .cal-mark.ready, .cal-mark.delivered { background: var(--hf-green); }
      .cal-mark.needs { background: var(--warning-color, #ff9800); }
      .cal-mark.locked { background: var(--secondary-text-color); }
      .cal-mark.skipped {
        background: none; box-shadow: inset 0 0 0 1.5px var(--secondary-text-color);
      }
      .cal-day.has.skipped .cal-num { text-decoration: line-through; color: var(--secondary-text-color); }

      .timeline { display: flex; flex-direction: column; }
      .row {
        display: flex; align-items: center; gap: 12px; padding: 10px 8px;
        border-bottom: 1px solid var(--divider-color);
      }
      .row:last-child { border-bottom: none; }
      .row.current { background: color-mix(in srgb, var(--hf-green) 10%, transparent); border-radius: 10px; }
      .row.past { opacity: 0.65; }
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
      .rowtrack { font-size: 0.78em; color: var(--secondary-text-color); margin-top: 2px; }
      .rowtrack a { color: inherit; }
      .badge {
        flex: none; font-size: 0.72em; font-weight: 700; padding: 4px 10px; border-radius: 12px;
        background: var(--secondary-background-color); color: var(--secondary-text-color);
      }
      .badge.ready { background: color-mix(in srgb, var(--hf-green) 18%, transparent); color: var(--hf-green); }
      .badge.preselected { background: var(--warning-color, #ff9800); color: #fff; }
      .badge.delivered { background: color-mix(in srgb, var(--hf-green) 18%, transparent); color: var(--hf-green); }
      .badge.needs { background: var(--warning-color, #ff9800); color: #fff; }
      .badge.skipped { background: var(--secondary-background-color); color: var(--secondary-text-color); }

      .footer { margin-top: 12px; text-align: right; }
      /* Matches the meal-planner/market cards' ↻ pill (their .skipbtn styling). */
      .refreshbtn {
        font: inherit; font-size: 0.85em; cursor: pointer;
        padding: 5px 12px; border-radius: 14px; border: 1px solid var(--divider-color);
        background: var(--card-background-color); color: var(--primary-text-color);
      }
      .refreshbtn:disabled { opacity: 0.5; cursor: default; }
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
  description:
    "HelloFresh delivery schedule: next-box summary, a month calendar of delivery days, and a timeline of past and upcoming weeks with status.",
});

console.info(
  `%c HELLOFRESH-SCHEDULE-CARD %c v${SCHEDULE_CARD_VERSION} `,
  "color:#fff;background:#91c11e;font-weight:700",
  "color:#91c11e;background:#fff"
);
