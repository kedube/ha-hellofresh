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
 * `calendar.delivery_schedule` dashboard widget. It re-pulls automatically on the integration's
 * configured refresh interval (and when a sibling card saves a change), so a permanently open
 * dashboard stays current. Clicking a delivery day or timeline row broadcasts the week-sync
 * event so the meal-planner/market cards jump to that week, and the day/row the sibling cards
 * are showing is ring-highlighted. Timeline rows carry Skip/Unskip and Change-day actions
 * (skip_week/unskip_week/reschedule_week, editable weeks only); meal selection editing stays
 * in the meal-planner card. Holiday-shifted weeks are marked 🎄 on the calendar and timeline.
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
// Shared card helpers. AWAITED AT TOP LEVEL: they are called synchronously during the first
// render, and an un-awaited dynamic import is still a Promise then. The dynamic form carries
// this card's ?v= cache-bust onto the shared module (a static specifier is never stamped).
const {
  esc,
  safeUrl,
  parseLocalDate,
  relativeWeek,
  fmtDate,
  titleCase,
  fmtPrice,
  refetchIntervalMs,
  accountKey,
  syncStorageKey,
  loadSyncedWeekId,
  broadcastWeek,
  broadcastDataChanged,
  WEEK_SYNC_EVENT,
  DATA_CHANGED_EVENT,
} = await import(
  new URL(
    `./hellofresh-shared.js?v=${encodeURIComponent(SCHEDULE_CARD_VERSION)}`,
    import.meta.url,
  ).href
);


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
    // Epoch ms of the last completed fetch — drives the interval-based auto-refresh.
    this._lastFetched = 0;
    this._tickTimer = null;
    // The week a sibling card (or a calendar click here) selected — highlighted on the calendar.
    this._selectedWeekId = null;
    this._busy = false; // a skip/unskip/reschedule write is in flight
    this._actionError = null; // last failed skip/unskip/reschedule, shown as an inline notice
    this._rescheduleWeekId = null; // week whose "Change day" options panel is open
    // handle -> {name, price, day} from get_delivery_options; lazily fetched the first time a
    // "Change day" panel opens, so it enriches the per-week option labels with weekday names
    // and prices. null = not fetched yet; {} = fetched but empty/unavailable.
    this._deliveryOptions = null;
    // Distinguishes our own data-changed broadcasts from siblings' so we don't re-fetch
    // in response to a change we just made (we already re-fetch ourselves).
    this._instanceId = Math.random().toString(36).slice(2);
    this._onSyncWeek = (ev) => this._receiveSyncedWeek(ev);
    this._onDataChanged = (ev) => this._receiveDataChanged(ev);
    this._onVisibility = () => this._onBecameVisible();
  }

  connectedCallback() {
    window.addEventListener(HelloFreshScheduleCard.WEEK_SYNC_EVENT, this._onSyncWeek);
    window.addEventListener(HelloFreshScheduleCard.DATA_CHANGED_EVENT, this._onDataChanged);
    document.addEventListener("visibilitychange", this._onVisibility);
    this._startTick();
    // Switching back to this card's dashboard tab re-connects the (already-loaded) element
    // without re-fetching: pick up a week another card selected while we were hidden, and
    // re-pull if the data aged past the integration's poll interval in the meantime.
    if (this._weeks) {
      this._selectedWeekId = this._loadSyncedWeekId();
      if (!this._refreshIfStale()) this._render();
    }
  }

  disconnectedCallback() {
    window.removeEventListener(HelloFreshScheduleCard.WEEK_SYNC_EVENT, this._onSyncWeek);
    window.removeEventListener(HelloFreshScheduleCard.DATA_CHANGED_EVENT, this._onDataChanged);
    document.removeEventListener("visibilitychange", this._onVisibility);
    this._stopTick();
  }

  setConfig(config) {
    this._config = { title: "Schedule", max_weeks: 8, past_weeks: 4, calendar: true, ...config };
    this._selectedWeekId = this._loadSyncedWeekId();
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (hass && !this._fetched && !this._loading) {
      this._fetched = true;
      this._fetch();
    } else if (this._weeks) {
      // hass updates fire when a dashboard view becomes active. Some HA versions keep hidden
      // views in the DOM (no re-connect), so this is the reliable moment to pick up a week a
      // sibling card selected while this one was hidden. Cheap: no service call.
      const synced = this._loadSyncedWeekId();
      if (synced !== this._selectedWeekId) {
        this._selectedWeekId = synced;
        this._render();
      }
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
    if (!this._hass || this._loading) return;
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
      this._selectedWeekId = this._loadSyncedWeekId();
      this._actionError = null; // fresh data on screen — clear any stale skip-failure notice
    } catch (err) {
      this._error = (err && err.message) || String(err);
    } finally {
      // Set on failure too, so a broken backend is retried on the next interval,
      // not hammered on every minute tick.
      this._lastFetched = Date.now();
      this._loading = false;
      this._render();
      if (this._refetchQueued) {
        // A data-changed event arrived while this fetch was in flight; its response
        // predates the write, so pull once more now that we're settled.
        this._refetchQueued = false;
        this._fetch();
      }
    }
  }

  // ---- auto-refresh ---------------------------------------------------------
  // The card re-pulls on the SAME cadence the integration polls HelloFresh (its
  // "Refresh interval (minutes)" option, surfaced in the get_weeks account payload) —
  // fetching more often would just return the coordinator's identical cached data.

  _refetchIntervalMs() {
    return refetchIntervalMs(this._account);
  }

  // Re-fetch if the data is older than the integration's poll interval. True if a fetch started.
  _refreshIfStale() {
    if (!this._hass || !this._fetched || this._loading) return false;
    if (Date.now() - this._lastFetched < this._refetchIntervalMs()) return false;
    this._fetch();
    return true;
  }

  // A minute tick so deadline countdowns, "in 3 days", and the today marker stay live between
  // fetches. Hidden tabs skip it (no point rendering into an invisible view) and catch up via
  // the visibilitychange handler instead.
  _startTick() {
    if (this._tickTimer) return;
    this._tickTimer = setInterval(() => {
      if (document.visibilityState === "hidden") return;
      if (!this._refreshIfStale() && this._weeks && !this._loading) this._render();
    }, 60000);
  }

  _stopTick() {
    clearInterval(this._tickTimer);
    this._tickTimer = null;
  }

  _onBecameVisible() {
    if (document.visibilityState !== "visible") return;
    if (!this._refreshIfStale() && this._weeks) this._render();
  }

  // ---- cross-card sync -------------------------------------------------------
  // Same conventions as the meal-planner/market cards: the selected week is persisted in
  // localStorage (keyed by account) and announced with a window event. This card both sends
  // (calendar/timeline clicks) and receives (highlighting the selected week's calendar day).

  static get WEEK_SYNC_EVENT() {
    return WEEK_SYNC_EVENT;
  }

  // Fired by the editing cards after a successful write (meal save, market save, skip) so
  // read-only siblings like this card re-pull instead of showing stale data until their
  // next interval refresh.
  static get DATA_CHANGED_EVENT() {
    return DATA_CHANGED_EVENT;
  }

  // Only cards for the SAME account sync, so multi-account dashboards don't cross-drive.
  _accountKey() {
    return accountKey(this._config);
  }

  _syncStorageKey() {
    return syncStorageKey(this._config);
  }

  _loadSyncedWeekId() {
    return loadSyncedWeekId(this._config);
  }

  // Persist + announce a week selection (calendar day / timeline row click) and highlight it.
  _selectWeek(weekId) {
    if (!weekId) return;
    this._selectedWeekId = weekId;
    try {
      window.localStorage.setItem(this._syncStorageKey(), weekId);
    } catch (_e) {
      /* storage unavailable (private mode) — the live event below still works */
    }
    window.dispatchEvent(
      new CustomEvent(HelloFreshScheduleCard.WEEK_SYNC_EVENT, {
        detail: { weekId, accountKey: this._accountKey() },
      })
    );
    this._render();
  }

  _receiveSyncedWeek(ev) {
    const detail = (ev && ev.detail) || {};
    if ((detail.accountKey || "default") !== this._accountKey()) return;
    if (!detail.weekId || detail.weekId === this._selectedWeekId) return;
    this._selectedWeekId = detail.weekId;
    if (this._weeks) this._render();
  }

  _receiveDataChanged(ev) {
    const detail = (ev && ev.detail) || {};
    if (detail.source === this._instanceId) return; // our own write — already re-fetched
    if ((detail.accountKey || "default") !== this._accountKey()) return;
    if (!this._fetched) return;
    if (this._loading) {
      // An in-flight fetch was issued BEFORE the write this event announces; dropping the
      // event here left pre-write data on screen until the next interval (hours). Queue
      // one follow-up fetch to run when the current one settles.
      this._refetchQueued = true;
      return;
    }
    this._fetch();
  }

  _broadcastDataChanged() {
    broadcastDataChanged(this._config, this._instanceId);
  }

  // ---- skip/unskip -----------------------------------------------------------
  // Same service calls as the meal-planner card's Skip week button, but shown ONLY on weeks
  // the action can still change: a locked or delivered week can't be skipped, so a button
  // there is dead weight (allowed_actions.mealSwap merely being PRESENT isn't enough — past
  // weeks carry it as false).

  _canSkip(week) {
    if (this._isEditable(week)) return true; // editable ⇒ skippable
    // A skipped week can be restored (Unskip) while its deadline hasn't passed; once the
    // deadline is gone — or the week is in the past — nothing can bring the box back.
    if (!this._isSkipped(week)) return false;
    const deadline = week.selection_deadline ? Date.parse(week.selection_deadline) : null;
    if (deadline) return deadline > Date.now();
    return this._isCurrent(week); // no deadline info: fall back to "not in the past"
  }

  async _toggleSkip(weekId) {
    if (this._busy || !this._hass) return;
    const week = (this._weeks || []).find((w) => w.week_id === weekId);
    if (!week) return;
    const service = this._isSkipped(week) ? "unskip_week" : "skip_week";
    this._busy = true;
    this._actionError = null;
    this._render();
    // Track the failure locally: the resync in `finally` clears _actionError on success
    // (fresh data path), which used to erase the failure notice within a second AND made
    // the broadcast below read the cleared flag and announce a failed write as "changed".
    let failed = null;
    try {
      const data = { week_id: weekId };
      if (this._config.config_entry_id) data.config_entry_id = this._config.config_entry_id;
      await this._hass.callService("hellofresh", service, data);
    } catch (err) {
      failed = `${service} failed: ${(err && err.message) || err}`;
    } finally {
      this._busy = false;
      await this._fetch(); // resync from the source of truth either way
      if (failed) {
        this._actionError = failed; // survives the resync so the user sees why
        this._render();
      }
    }
    // Announce after our own refetch so any other listening card (e.g. a second schedule
    // card on another view) re-pulls; the source tag stops us re-fetching a second time.
    if (!failed) this._broadcastDataChanged();
  }

  // ---- reschedule (change delivery day) ---------------------------------------
  // HelloFresh offers per-week alternate delivery days (available_one_off_options,
  // {handle, delivery_date}); reschedule_week applies one. Editable weeks only — the same
  // rule as Skip, since both go through the week-modification window.

  _canReschedule(week) {
    if (!this._isEditable(week)) return false;
    return (week.available_one_off_options || []).some((o) => o.handle);
  }

  async _reschedule(weekId, handle) {
    if (this._busy || !this._hass || !weekId || !handle) return;
    this._busy = true;
    this._actionError = null;
    this._render();
    let failed = null;
    try {
      const data = { week_id: weekId, delivery_option: handle };
      if (this._config.config_entry_id) data.config_entry_id = this._config.config_entry_id;
      await this._hass.callService("hellofresh", "reschedule_week", data);
      this._rescheduleWeekId = null; // done — close the options panel
    } catch (err) {
      failed = `reschedule_week failed: ${(err && err.message) || err}`;
    } finally {
      this._busy = false;
      await this._fetch();
      if (failed) {
        this._actionError = failed; // survives the resync's clear — see _toggleSkip
        this._render();
      }
    }
    if (!failed) this._broadcastDataChanged();
  }

  // Lazily fetch the plan's delivery-day catalog (weekday names + prices) the first time a
  // "Change day" panel opens. It's plan-level (not per-week), so one fetch serves every week;
  // most users never reschedule, so we avoid the extra service call until it's needed. Failure
  // is non-fatal — the picker falls back to date-only labels from the per-week options.
  async _ensureDeliveryOptions() {
    if (this._deliveryOptions !== null || !this._hass) return;
    this._deliveryOptions = {}; // mark as attempted so a failure doesn't refetch on every open
    try {
      const data = {};
      if (this._config.config_entry_id) data.config_entry_id = this._config.config_entry_id;
      const result = await this._hass.callService(
        "hellofresh", "get_delivery_options", data, undefined, false, true
      );
      const options = ((result && result.response) || {}).delivery_options || [];
      const byHandle = {};
      for (const o of options) {
        if (o && o.handle) byHandle[o.handle] = o;
      }
      this._deliveryOptions = byHandle;
    } catch (_err) {
      // Keep the empty map — date-only labels still work.
      this._deliveryOptions = {};
    }
    this._render();
  }

  // Parse a date anchored to LOCAL midnight. A bare "YYYY-MM-DD" (how the integration
  // serializes delivery dates) parses as UTC midnight per the JS spec, which reads as the
  // PREVIOUS day anywhere west of UTC — a Monday delivery rendered as Sunday. Full datetime
  // strings (selection deadlines) parse normally.
  _parseLocalDate(value) {
    return parseLocalDate(value);
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
      if (this._isSkipped(week)) {
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

  // Skipped OR paused: the integration treats both as "no box ships this week" (a paused
  // week carries status PAUSED without is_skipped in some payload shapes), so the card must
  // too — otherwise a paused week renders as a normal box, price included.
  _isSkipped(week) {
    return Boolean(week.is_skipped) || String(week.status || "").toUpperCase() === "PAUSED";
  }

  _isEditable(week) {
    if (!week) return false;
    const actions = week.allowed_actions || {};
    if (actions.mealSwap === false) return false;
    if (this._isSkipped(week)) return false;
    const deadline = week.selection_deadline ? Date.parse(week.selection_deadline) : null;
    if (deadline && deadline < Date.now()) return false;
    return Boolean(actions.mealSwap);
  }

  _needsSelection(week) {
    if (!this._isEditable(week)) return false;
    if (week.needs_selection != null) return Boolean(week.needs_selection);
    // Coerced to numbers before any HTML interpolation: integers under the server
    // contract, but the card must not trust that (defense in depth for innerHTML).
    const required = Number(week.meals_required) || 0;
    const selected = Number(week.meals_selected) || 0;
    return required > 0 && selected < required;
  }

  // One of: skipped | needs | ready | delivered | locked — drives the icon, colour and label.
  _weekState(week) {
    if (this._isSkipped(week)) return "skipped";
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
      <span class="title-text">${this._esc(this._config ? this._config.title : "Schedule")}</span>
      <button class="refreshbtn" data-action="refresh" title="Refresh" ${this._loading ? "disabled" : ""}>↻</button>`;
    this._shell.body.innerHTML = this._renderBody();
  }

  _ensureShell() {
    if (this._shell) return;
    const card = document.createElement("ha-card");
    card.innerHTML = `<div class="head"></div><div class="body"></div>`;
    this.shadowRoot.appendChild(card);
    this._shell = { card, head: card.querySelector(".head"), body: card.querySelector(".body") };
    // Timeline rows are focusable divs (role="button" tabindex="0") — the calendar days
    // and every other control here are real <button>s with native keyboard handling, so
    // this covers only the row's week-select, guarding against keystrokes bubbling up
    // from the buttons and links inside the row.
    card.addEventListener("keydown", (ev) => {
      if (ev.key !== "Enter" && ev.key !== " ") return;
      const row = ev.target.closest('.row[data-action="cal-week"]');
      if (!row || ev.target.closest("button, a")) return;
      ev.preventDefault(); // Space must select the week, not scroll the page
      this._selectWeek(row.getAttribute("data-week-id"));
    });
    card.addEventListener("click", (ev) => {
      // Real links (the tracking number) must navigate, not trigger the row's week-select.
      if (ev.target.closest("a")) return;
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
        // Clicking a delivery day (or a timeline row) drives the meal-planner/market cards
        // to that week — the same window event those cards use to stay in step, plus the
        // same localStorage key so cards on other dashboard tabs pick the week up later.
        this._selectWeek(actionEl.getAttribute("data-week-id"));
      } else if (action === "skip-week") {
        this._toggleSkip(actionEl.getAttribute("data-week-id"));
      } else if (action === "reschedule-open") {
        const weekId = actionEl.getAttribute("data-week-id");
        const opening = this._rescheduleWeekId !== weekId;
        this._rescheduleWeekId = opening ? weekId : null;
        this._render();
        // Enrich the picker with weekday names/prices; re-renders when it lands.
        if (opening) this._ensureDeliveryOptions();
      } else if (action === "reschedule") {
        this._reschedule(
          actionEl.getAttribute("data-week-id"),
          actionEl.getAttribute("data-handle")
        );
      }
    });
  }

  _shiftCalMonth(delta) {
    const shown = this._shownCalMonth();
    const target = new Date(shown.getFullYear(), shown.getMonth() + delta, 1);
    const { min, max } = this._calBounds();
    const t = target.getTime();
    if (t < min || t > max) return; // don't page into months the data can't fill
    this._calMonth = target;
    this._render();
  }

  // First-of-month bounds of the loaded data (actual/scheduled delivery days), so month
  // navigation stops where the data ends instead of paging through empty months forever.
  // The real current month is always in range so the Today shortcut never dead-ends.
  _calBounds() {
    let min = null;
    let max = null;
    for (const week of this._weeks || []) {
      const when = week.delivered_at || week.delivery_date;
      if (!when) continue;
      const d = this._parseLocalDate(when);
      const m = new Date(d.getFullYear(), d.getMonth(), 1).getTime();
      if (min === null || m < min) min = m;
      if (max === null || m > max) max = m;
    }
    const now = new Date();
    const cur = new Date(now.getFullYear(), now.getMonth(), 1).getTime();
    return { min: min === null ? cur : Math.min(min, cur), max: max === null ? cur : Math.max(max, cur) };
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
    // Nothing on screen yet: full-body loading/error/empty states.
    if (!this._weeks || this._weeks.length === 0) {
      if (this._loading || !this._fetched) return `<div class="state">Loading schedule…</div>`;
      if (this._error) {
        return `<div class="state error">Could not load schedule: ${this._esc(this._error)}</div>
          <div class="actions"><button data-action="refresh">Retry</button></div>`;
      }
      return `<div class="state">No delivery weeks found.</div>
        <div class="actions"><button data-action="refresh">Refresh</button></div>`;
    }
    // With data on screen, a refresh must never blank the card: keep the last good view
    // (dimmed while reloading) and surface a failed refresh as an inline notice on top of it.
    const notice = this._error
      ? `<div class="notice">Refresh failed: ${this._esc(this._error)}
           <button class="refreshbtn" data-action="refresh">Retry</button></div>`
      : this._actionError
        ? `<div class="notice">${this._esc(this._actionError)}</div>`
        : "";
    return `<div class="${this._loading ? "reloading" : ""}">${notice}${this._renderSummary()}${this._renderCalendar()}${this._renderTimeline()}</div>`;
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
      const isSelected = week.week_id === this._selectedWeekId ? " selected" : "";
      const holiday = this._isHolidayShifted(week);
      const title = `${week.display_name || week.week_id} — ${this._stateLabel(week, state)}${
        holiday ? ` — ${week.holiday_message || "Holiday delivery change"}` : ""
      }`;
      cells.push(`
        <button class="cal-day has ${state}${isToday}${isSelected}" data-action="cal-week"
          data-week-id="${this._esc(week.week_id)}" title="${this._esc(title)}">
          <span class="cal-num">${day}${holiday ? `<span class="cal-holiday">🎄</span>` : ""}</span>
          <span class="cal-mark ${state}"></span>
        </button>`);
    }
    const monthLabel = shown.toLocaleDateString(undefined, { month: "long", year: "numeric" });
    const isCurrentMonth = year === today.getFullYear() && month === today.getMonth();
    const { min, max } = this._calBounds();
    const shownKey = shown.getTime();
    return `
      <div class="calendar">
        <div class="cal-head">
          <button class="cal-nav" data-action="cal-prev" title="Previous month"
            ${shownKey <= min ? "disabled" : ""}>‹</button>
          <span class="cal-title">${this._esc(monthLabel)}${
            // Inside the title span (not after ›) so the ‹ › buttons never move when the
            // Today shortcut appears/disappears while navigating months.
            isCurrentMonth ? "" : `<button class="cal-nav cal-today-btn" data-action="cal-today">Today</button>`
          }</span>
          <button class="cal-nav" data-action="cal-next" title="Next month"
            ${shownKey >= max ? "disabled" : ""}>›</button>
        </div>
        <div class="cal-grid">${dows}${cells.join("")}</div>
      </div>`;
  }

  // The "next box" summary: the nearest upcoming delivery that will actually ship (paused/
  // skipped weeks aren't a "box"; fall back to them only when every upcoming week is skipped)
  // — or, when nothing is upcoming at all (end of data), the most recent box as "Last box".
  _renderSummary() {
    const upcoming =
      this._weeks.find((w) => this._isCurrent(w) && !this._isSkipped(w)) ||
      this._weeks.find((w) => this._isCurrent(w));
    const next = upcoming || this._weeks[this._weeks.length - 1];
    if (!next) return "";
    const order = next.order || {};
    const status = order.tracking_status || order.status || next.status || "—";
    // No price on a skipped/paused week — nothing ships, nothing is charged.
    const price = this._isSkipped(next) ? "" : this._orderPrice(next);
    const deadline = next.selection_deadline ? new Date(next.selection_deadline) : null;
    const rel = this._relativeWeek(next);
    // Subscription-level next charge date and coupon — only meaningful alongside an
    // upcoming box.
    const paymentDate = upcoming && this._account ? this._account.next_payment_date : null;
    const coupon = upcoming && this._account ? this._account.next_box_coupon : null;
    // Discount applied to that box (from its /gw/calculate split) — shown only when non-zero,
    // so the price line's "includes" reads as news rather than a permanent zero.
    const discount = upcoming ? this._nextBoxDiscount() : null;
    // The wallet promise HelloFresh will apply to that box ("$10 off premium meals") — the
    // weekly discount the cart-pricing split above never shows. Only for the box it names.
    const voucher = upcoming ? this._nextBoxVoucher(next) : null;
    // The human-readable courier window ("Mondays: 8AM - 8PM") — the week/order slot_label,
    // the same value the "Delivery Window" (next_delivery_slot) sensor reports. NOT the raw
    // subscription next_delivery_time, which is a machine string.
    const window = upcoming ? next.slot_label || order.slot_label : null;
    return `
      <div class="summary">
        <div class="sumrow">
          <span class="sumlabel">${upcoming ? "Next box" : "Last box"}</span>
          <span class="sumval">${this._esc(this._fmtDate(next.delivery_date))}${rel ? ` <span class="muted">· ${this._esc(rel)}</span>` : ""}</span>
        </div>
        ${window ? `
        <div class="sumrow">
          <span class="sumlabel">Delivery window</span>
          <span class="sumval">${this._esc(window)}</span>
        </div>` : ""}
        ${deadline ? `
        <div class="sumrow">
          <span class="sumlabel">Selection deadline</span>
          <span class="sumval">${this._esc(this._fmtDateTime(deadline))} <span class="${this._deadlineClass(deadline)}">· ${this._esc(this._countdown(deadline))}</span></span>
        </div>` : ""}
        ${paymentDate ? `
        <div class="sumrow">
          <span class="sumlabel">Payment date</span>
          <span class="sumval">${this._esc(this._fmtDate(paymentDate))}</span>
        </div>` : ""}
        ${coupon ? `
        <div class="sumrow">
          <span class="sumlabel">Coupon</span>
          <span class="sumval">${this._esc(coupon)}</span>
        </div>` : ""}
        <div class="sumrow">
          <span class="sumlabel">Status</span>
          <span class="sumval">${this._esc(this._titleCase(status))}${price ? ` <span class="muted">· ${this._esc(price)}</span>` : ""}</span>
        </div>
        ${discount ? `
        <div class="sumrow">
          <span class="sumlabel">Discount</span>
          <span class="sumval">−${this._esc(discount)} <span class="muted">· included in the price</span></span>
        </div>` : ""}
        ${voucher ? `
        <div class="sumrow">
          <span class="sumlabel">Voucher</span>
          <span class="sumval">${this._esc(voucher.label)}${voucher.note ? ` <span class="muted">· ${this._esc(voucher.note)}</span>` : ""}</span>
        </div>` : ""}
      </div>`;
  }

  // The first available wallet promise on a week, or null. Weeks carry their promises as
  // `benefits` (from the integration's benefit-distribution call), each with a ready label.
  _weekBenefit(week) {
    const list = week && Array.isArray(week.benefits) ? week.benefits : [];
    return list.find((b) => b && b.status === "available" && b.label) || null;
  }

  // The next box's voucher as {label, note}: prefers the week's own promise, falling back to
  // the account payload's next_box_discount when it names this week. The note says when it
  // expires and whether it is one-time, since both change what the customer should do.
  _nextBoxVoucher(week) {
    let benefit = this._weekBenefit(week);
    if (!benefit && this._account && this._account.next_box_discount) {
      const candidate = this._account.next_box_discount;
      if (candidate.label && (!candidate.week_id || candidate.week_id === week.week_id)) benefit = candidate;
    }
    if (!benefit) return null;
    const parts = [];
    if (benefit.expires_at) {
      const expires = new Date(benefit.expires_at);
      if (!Number.isNaN(expires.getTime())) parts.push(`expires ${this._fmtDate(expires)}`);
    }
    if (benefit.one_time) parts.push("one-time");
    return { label: benefit.label, note: parts.join(" · ") };
  }

  // The next box's discount from the account payload's price breakdown, formatted, or null
  // when there is none (or the breakdown is unavailable).
  _nextBoxDiscount() {
    const breakdown = this._account && this._account.next_delivery_price_breakdown;
    const amount = breakdown ? Number(breakdown.discount_amount) : NaN;
    if (!Number.isFinite(amount) || amount <= 0) return null;
    return this._fmtPrice(amount, this._account.next_delivery_total_currency);
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
        ${this._config.calendar === false ? "" : this._monthSummary(rows)}
        ${rows.map((w) => this._renderRow(w, w === current, !this._isCurrent(w))).join("")}
      </div>`;
  }

  // One-line roll-up above the month's rows: boxes, skipped weeks, and what the month's boxes
  // cost. Only weeks exposing their OWN billed/cart price are summed (same rule as the row
  // price — the account plan-price is never counted as spend). Skipped for single-row months,
  // where it would just repeat the row.
  _monthSummary(rows) {
    if (rows.length < 2) return "";
    const boxes = rows.filter((w) => this._weekState(w) !== "skipped").length;
    const skipped = rows.length - boxes;
    const parts = [];
    if (boxes) parts.push(`${boxes} box${boxes === 1 ? "" : "es"}`);
    if (skipped) parts.push(`${skipped} skipped`);
    let total = 0;
    let currency = null;
    let priced = 0;
    for (const w of rows) {
      const p = this._weekPriceParts(w);
      if (!p) continue;
      total += p.amount;
      currency = currency || p.currency;
      priced += 1;
    }
    if (priced) parts.push(this._fmtPrice(total, currency));
    return parts.length ? `<div class="monthsum">${this._esc(parts.join(" · "))}</div>` : "";
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
    // Recent past deliveries lead, then the upcoming weeks; if nothing is upcoming,
    // fall back to the most recent history so the card is never empty.
    const pastRows = upcoming.length ? past.slice(-pastMax) : past.slice(-max);
    return [...pastRows, ...upcoming.slice(0, max)];
  }

  // Whether HelloFresh auto-picked (preselected) this week's meals rather than the customer
  // choosing them. Mirrors the meal-planner card: a skipped/paused week's preselection never
  // ships, so it doesn't count.
  _isPreselected(week) {
    if (this._isSkipped(week)) return false;
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
    const isSelected = week.week_id === this._selectedWeekId;
    const holiday = this._isHolidayShifted(week)
      ? `<span class="holiday" title="${this._esc(week.holiday_message || "Holiday delivery change")}">🎄</span>`
      : "";
    // Weekly discount badge: the wallet promise HelloFresh will apply to this box.
    const benefit = state === "skipped" ? null : this._weekBenefit(week);
    const voucherBadge = benefit
      ? `<span class="badge benefit" title="${this._esc(benefit.voucher_code ? `Voucher ${benefit.voucher_code}` : "Voucher applied by HelloFresh")}">${this._esc(benefit.label)}</span>`
      : "";
    return `
      <div class="row ${isCurrent ? "current" : ""}${isPast ? " past" : ""}${isSelected ? " selected" : ""}" data-action="cal-week"
        role="button" tabindex="0"
        data-week-id="${this._esc(week.week_id)}"
        title="Show ${this._esc(week.display_name || week.week_id)} in the meal planner and market cards">
        <span class="dot ${state}" title="${this._esc(label)}">${meta.icon}</span>
        <div class="rowmain">
          <div class="rowtop">
            <span class="rowdate">${this._esc(this._fmtDateShort(week.delivered_at || week.delivery_date))}</span>
            ${holiday}
            <span class="rowweek">${this._esc(week.display_name || week.week_id)}</span>
          </div>
          ${detail ? `<div class="rowsub">${detail}</div>` : ""}
          ${state === "skipped" ? "" : this._rowTracking(week)}
          ${this._rescheduleWeekId === week.week_id ? this._renderRescheduleOptions(week) : ""}
        </div>
        ${voucherBadge}
        ${preselected}
        <span class="badge ${state}" title="${this._esc(badgeTitle)}">${this._esc(label)}</span>
        ${this._canReschedule(week)
          ? `<button class="skipbtn" data-action="reschedule-open" data-week-id="${this._esc(week.week_id)}"
               title="Change this week's delivery day" ${this._busy ? "disabled" : ""}>Change day</button>`
          : ""}
        ${this._canSkip(week)
          ? `<button class="skipbtn" data-action="skip-week" data-week-id="${this._esc(week.week_id)}"
               ${this._busy ? "disabled" : ""}>${this._isSkipped(week) ? "Unskip" : "Skip"}</button>`
          : ""}
      </div>`;
  }

  // Whether HelloFresh has announced a holiday schedule change for this week.
  _isHolidayShifted(week) {
    return Boolean(week.holiday_message || week.holiday_delivery_date);
  }

  // The alternate delivery days HelloFresh offers for THIS week (per-week availability comes
  // from available_one_off_options); picking one calls reschedule_week with its handle. When the
  // plan's delivery-day catalog (get_delivery_options) has loaded, each option is labelled with
  // its weekday name and any surcharge — otherwise it falls back to the option's date.
  _renderRescheduleOptions(week) {
    const options = (week.available_one_off_options || []).filter((o) => o.handle);
    if (!options.length) return "";
    const catalog = this._deliveryOptions || {};
    return `
      <div class="dayopts">
        ${options
          .map((o) => {
            const isCurrent = o.delivery_date && o.delivery_date === week.delivery_date;
            const meta = catalog[o.handle];
            // Prefer the catalog's weekday name; fall back to the option's own date.
            const base = (meta && meta.delivery_name)
              || (o.delivery_date ? this._fmtDate(o.delivery_date) : o.handle);
            const price = meta && Number(meta.price) > 0
              ? ` (+${this._fmtPrice(meta.price, this._priceCurrency())})`
              : "";
            return `<button class="dayopt${isCurrent ? " current" : ""}" data-action="reschedule"
              data-week-id="${this._esc(week.week_id)}" data-handle="${this._esc(o.handle)}"
              ${isCurrent || this._busy ? "disabled" : ""}>${this._esc(base + price)}</button>`;
          })
          .join("")}
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
  // A skipped/paused week's order price is the cart estimate of a box that never ships —
  // no money moved, so it has no price here and never counts toward the month total.
  _weekPriceParts(week) {
    if (this._isSkipped(week)) return null;
    const order = week.order || {};
    if (order.billed_total_price != null) {
      const amount = Number(order.billed_total_price);
      if (Number.isFinite(amount)) {
        return { amount, currency: order.billed_total_currency || order.currency };
      }
    }
    if (order.total_price != null) {
      const amount = Number(order.total_price);
      if (Number.isFinite(amount)) return { amount, currency: order.currency };
    }
    return null;
  }

  _weekPrice(week) {
    const parts = this._weekPriceParts(week);
    return parts ? this._fmtPrice(parts.amount, parts.currency) : "";
  }

  // Order/shipping meta line: the week's order ID, plus carrier + tracking number (number
  // linked when a tracking URL exists). Tracking data only appears once HelloFresh ships the
  // box, so upcoming weeks show just the order ID and shipped/delivered weeks the full line.
  _rowTracking(week) {
    const order = week.order || {};
    const parts = [];
    // When the box actually ARRIVED, from the carrier handover timestamp. Only delivered
    // weeks carry it, so upcoming weeks are unaffected. Leads the line because it is the
    // fact you look for on a past box; carrier/number stay for the shipment itself.
    const arrived = this._fmtArrival(week.delivered_at);
    if (arrived) parts.push(`<span class="arrived" title="When the box was delivered">Delivered ${this._esc(arrived)}</span>`);
    if (order.carrier) parts.push(this._esc(order.carrier));
    if (order.tracking_number) {
      const num = this._esc(order.tracking_number);
      const href = this._safeUrl(order.tracking_url);
      parts.push(
        href
          ? `<a href="${href}" target="_blank" rel="noopener">${num}</a>`
          : num
      );
    }
    if (order.order_id) parts.push(`Order ${this._esc(order.order_id)}`);
    if (!parts.length) return "";
    return `<div class="rowtrack">${parts.join(" · ")}</div>`;
  }

  _rowDetail(week, state) {
    if (state === "skipped") return `<span class="muted">No box this week</span>`;
    // Coerced to numbers before any HTML interpolation: integers under the server
    // contract, but the card must not trust that (defense in depth for innerHTML).
    const required = Number(week.meals_required) || 0;
    const selected = Number(week.meals_selected) || 0;
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
    return relativeWeek(week);
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

  // An undated week (missing delivery_date/delivered_at — a supported state) reaches these
  // via _renderRow. toLocaleDateString on an Invalid Date returns the literal string
  // "Invalid Date" WITHOUT throwing, so the catch alone never fired — guard explicitly.
  _fmtDate(iso) {
    return fmtDate(iso);
  }

  _fmtDateShort(iso) {
    if (!iso) return "—";
    try {
      const d = this._parseLocalDate(iso);
      if (Number.isNaN(d.getTime())) return "—";
      return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
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

  // Arrival stamp for a delivered box: "Aug 17, 6:53 PM". `delivered_at` is a full ISO
  // datetime WITH offset (unlike the bare-date `delivery_date`), so it parses unambiguously
  // and renders in the viewer's timezone — an evening ET handover stays on its local day.
  _fmtArrival(iso) {
    if (!iso) return "";
    try {
      const d = new Date(iso);
      if (Number.isNaN(d.getTime())) return "";
      return d.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      });
    } catch (_e) {
      return "";
    }
  }

  // Best-guess currency for prices not carrying their own (e.g. delivery-option surcharges):
  // the account's plan-price currency, else _fmtPrice's USD default.
  _priceCurrency() {
    return (this._account && this._account.selected_plan_total_price_currency) || null;
  }

  _fmtPrice(amount, currency) {
    return fmtPrice(amount, currency);
  }

  // Normalize an API status like "ON_THE_WAY" / "on_the_way" to "On The Way". The .toLowerCase()
  // is load-bearing: HelloFresh sends SCREAMING_SNAKE ("DELIVERED", "ON_THE_WAY"), and without it
  // this rendered "DELIVERED" here while the meal-planner and market cards showed "Delivered" for
  // the same week. Keep in step with those two.
  _titleCase(value) {
    return titleCase(value);
  }

  _esc(value) {
    return esc(value);
  }

  // Escape a value for use in an href/src. HTML-escaping alone does NOT neutralize a
  // javascript:/data: scheme (a tracking_url from the API is untrusted), so allow only
  // http(s) and return "" otherwise — an empty href renders a dead link, never executes.
  _safeUrl(value) {
    return safeUrl(value);
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
      /* A refresh in flight dims the (still-interactive) last good view instead of blanking it. */
      .reloading { opacity: 0.6; transition: opacity 0.2s; }
      .notice {
        display: flex; align-items: center; gap: 10px; margin-bottom: 12px;
        padding: 8px 12px; border-radius: 10px; font-size: 0.85em;
        background: color-mix(in srgb, var(--error-color, #db4437) 12%, transparent);
        color: var(--error-color, #db4437);
      }
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
      .cal-nav:disabled { opacity: 0.4; cursor: default; }
      .cal-today-btn { font-size: 0.78em; margin-left: 8px; }
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
      /* The week the sibling cards are showing (cross-card sync). After .today so it wins
         when a day is both — the selection ring is the more actionable signal. */
      .cal-day.has.selected { box-shadow: inset 0 0 0 1.5px var(--hf-green); }
      .cal-day.has .cal-num { font-weight: 700; }
      .cal-mark { width: 7px; height: 7px; border-radius: 50%; margin-top: 2px; }
      .cal-mark.ready, .cal-mark.delivered { background: var(--hf-green); }
      .cal-mark.needs { background: var(--warning-color, #ff9800); }
      .cal-mark.locked { background: var(--secondary-text-color); }
      .cal-mark.skipped {
        background: none; box-shadow: inset 0 0 0 1.5px var(--secondary-text-color);
      }
      .cal-holiday { font-size: 0.8em; margin-left: 1px; }
      .cal-day.has.skipped .cal-num { text-decoration: line-through; color: var(--secondary-text-color); }

      .timeline { display: flex; flex-direction: column; }
      .monthsum {
        font-size: 0.82em; color: var(--secondary-text-color);
        padding: 2px 8px 8px; border-bottom: 1px solid var(--divider-color);
      }
      .row {
        display: flex; align-items: center; gap: 12px; padding: 10px 8px;
        border-bottom: 1px solid var(--divider-color); cursor: pointer;
      }
      .row:last-child { border-bottom: none; }
      .row:focus-visible { outline: 2px solid var(--primary-color); outline-offset: -2px; }
      .row:hover { background: var(--secondary-background-color); border-radius: 10px; }
      .row.current { background: color-mix(in srgb, var(--hf-green) 10%, transparent); border-radius: 10px; }
      /* Same green ring as the calendar's selected day — clicking either highlights both. */
      .row.selected { box-shadow: inset 0 0 0 1.5px var(--hf-green); border-radius: 10px; }
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
      .rowtrack .arrived { color: var(--primary-text-color); font-weight: 500; }
      .holiday { flex: none; font-size: 0.9em; cursor: help; }
      .dayopts { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
      .dayopt {
        font-size: 0.78em; padding: 4px 10px; border-radius: 12px; cursor: pointer;
        border: 1px solid var(--divider-color); background: var(--card-background-color);
        color: var(--primary-text-color);
      }
      .dayopt.current { border-color: var(--hf-green); color: var(--hf-green); font-weight: 700; }
      .dayopt:disabled { cursor: default; }
      .dayopt:disabled:not(.current) { opacity: 0.5; }
      /* Same size/shape as the meal-planner card's status chips (.chip) — the two cards
         show the same words ("Preselected", "Editable") and should read identically. */
      .badge {
        flex: none; font-size: 0.8em; font-weight: 700; padding: 4px 10px; border-radius: 14px;
        background: var(--secondary-background-color); color: var(--secondary-text-color);
      }
      .badge.ready { background: color-mix(in srgb, var(--hf-green) 18%, transparent); color: var(--hf-green); }
      .badge.preselected { background: var(--warning-color, #ff9800); color: #fff; }
      .badge.delivered { background: color-mix(in srgb, var(--hf-green) 18%, transparent); color: var(--hf-green); }
      .badge.needs { background: var(--warning-color, #ff9800); color: #fff; }
      .badge.skipped { background: var(--secondary-background-color); color: var(--secondary-text-color); }
      .badge.benefit { background: color-mix(in srgb, var(--hf-green) 18%, transparent); color: var(--hf-green); }

      /* Identical to the meal-planner/market cards' ↻ pill (their .skipbtn) — same default
         button font (no font: inherit), so the ↻ glyph renders the same in all three cards. */
      .refreshbtn {
        margin-left: auto; flex: none;
        font-size: 0.85em; padding: 5px 12px; border-radius: 14px;
        border: 1px solid var(--divider-color); background: var(--card-background-color);
        color: var(--primary-text-color); cursor: pointer;
      }
      .refreshbtn:disabled { opacity: 0.5; cursor: default; }
      /* Same pill as the meal-planner card's Skip week button. */
      .skipbtn {
        flex: none; font-size: 0.85em; padding: 5px 12px; border-radius: 14px;
        border: 1px solid var(--divider-color); background: var(--card-background-color);
        color: var(--primary-text-color); cursor: pointer;
      }
      .skipbtn:disabled { opacity: 0.5; cursor: default; }
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
