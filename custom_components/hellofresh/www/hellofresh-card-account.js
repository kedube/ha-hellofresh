/*
 * HelloFresh card — Account
 * -------------------------
 * Three tabs, mirroring hellofresh.com's account area:
 *
 *   Plan & billing   — the classic Subscription card's account overview (plan, preference with
 *                      its preset name, price split, credit, card on file, number of people,
 *                      address, upcoming counters that jump to their week, the meal-preset
 *                      reference), plus the recurring plan controls that used to need a separate
 *                      entities card (box size, delivery day — via this integration's select
 *                      entities, behind a confirmation because they change every future box and
 *                      the bill) and a small integration-health panel (the old Diagnostics view).
 *   Food preferences — the food profile editor (hellofresh-card-profile.js).
 *   Spending         — the classic Cost card: lifetime total, monthly chart, month roll-up and
 *                      recent boxes with vouchers.
 */

const ACCOUNT_VERSION = new URL(import.meta.url).searchParams.get("v") || "unknown";
const stamp = encodeURIComponent(ACCOUNT_VERSION);
const [L, UI, { ProfileEditor }] = await Promise.all([
  import(new URL(`./hellofresh-card-logic.js?v=${stamp}`, import.meta.url).href),
  import(new URL(`./hellofresh-card-ui.js?v=${stamp}`, import.meta.url).href),
  import(new URL(`./hellofresh-card-profile.js?v=${stamp}`, import.meta.url).href),
]);

const { esc, t, ht } = L;
const { icon, pill, strong } = UI;

// `label` reads account.tab.<key> when shown.
const TABS = [
  { key: "plan", icon: "mdi:card-account-details-outline" },
  { key: "profile", icon: "mdi:food-apple-outline" },
  { key: "spending", icon: "mdi:chart-bar" },
].map((tab) => Object.defineProperty(tab, "label", { enumerable: true, get: () => t(`account.tab.${tab.key}`) }));

// The recurring plan controls (plan.<key>.*): which select entity, and what its confirmation says.
const PLAN_CONTROLS = ["box_size", "delivery_day"];

// How long a plan change HelloFresh accepted waits for its select entity to report the new
// option. Home Assistant's state normally follows within the write itself; should it never
// arrive, the control goes back to showing what the entity says rather than stay closed.
const PLAN_SETTLE_MS = 30000;

// How a plan option reads. The integration names box sizes in English ("3 meals for 2 people");
// the card shows them in its own language and still writes the option exactly as it is. Delivery
// days are HelloFresh's own labels, already in the account's language.
function planOptionLabel(key, option) {
  const size = key === "box_size" && /^(\d+) meals? for (\d+) (?:people|person)$/.exec(String(option));
  if (!size) return String(option);
  return t("card.plan_size", { count: Number(size[1]), people: t("card.people", { count: Number(size[2]) }) });
}

export class AccountView {
  constructor(card) {
    this.card = card;
    const stored = L.storageGet(L.STORAGE_KEYS.accountTab);
    this.tab = TABS.some((t) => t.key === stored) ? stored : "plan";
    this.profile = new ProfileEditor(card);
    this.spending = null;
    this.spendingLoading = false;
    this.spendingError = null;
    this.presets = null;
    this.presetsLoading = false;
    this.presetsOpen = false;
    this.healthOpen = false;
    // A recurring plan change on its way: {entityId, key, option, written}. While it is set the
    // control shows the option being written and neither control takes another change; it is
    // cleared when the write fails, or once the entity reports the new option (_settlePlan).
    this.planPending = null;
  }

  onRefresh() {
    if (this.tab === "profile") this.profile.onRefresh();
    if (this.spending || this.spendingError) this._fetchSpending();
  }

  // A real tablist: the chosen tab is aria-selected (a tab is not a toggle, so no aria-pressed)
  // and the only one in the Tab order; the arrow keys move between them (_onTabKey), and the
  // section below is their tabpanel.
  render() {
    const tabs = `<div class="hf-segment hf-subtabs" role="tablist" aria-label="${ht("account.sections")}">${TABS.map((tab) => {
      const on = this.tab === tab.key;
      return `<button role="tab" id="hf-acct-tab-${tab.key}" data-action="acct-tab" data-tab="${tab.key}" data-focus-key="acct-tab-${tab.key}"
          aria-selected="${on}" aria-controls="hf-acct-panel" tabindex="${on ? 0 : -1}">${icon(tab.icon)}${esc(tab.label)}</button>`;
    }).join("")}</div>`;
    let body = "";
    if (this.tab === "profile") body = this.profile.render();
    else if (this.tab === "spending") body = this._spendingView();
    else body = this._planView();
    return `${tabs}<div role="tabpanel" id="hf-acct-panel" aria-labelledby="hf-acct-tab-${this.tab}">${body}</div>`;
  }

  afterRender(root) {
    const tablist = root.querySelector(".hf-subtabs");
    if (tablist) tablist.addEventListener("keydown", (ev) => this._onTabKey(ev));
  }

  // Left / Right (and Up / Down) move to the neighbouring tab and open it, wrapping at the
  // ends; Home and End go to the first and last.
  _onTabKey(ev) {
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[ev.key];
    const index = TABS.findIndex((tab) => tab.key === this.tab);
    let next;
    if (ev.key === "Home") next = 0;
    else if (ev.key === "End") next = TABS.length - 1;
    else if (step) next = (index + step + TABS.length) % TABS.length;
    else return;
    ev.preventDefault();
    this._selectTab(TABS[next].key, { focus: true });
  }

  _selectTab(key, { focus = false } = {}) {
    if (!TABS.some((tab) => tab.key === key)) return;
    const card = this.card;
    this.tab = key;
    L.storageSet(L.STORAGE_KEYS.accountTab, key);
    card.renderView();
    const root = focus && card.shadowRoot ? card.shadowRoot.querySelector(".js-main") : null;
    if (root) UI.focusKeyed(root, [`acct-tab-${key}`]);
  }

  // ---- plan & billing ------------------------------------------------------------------------

  _planView() {
    const card = this.card;
    const s = card.summary;
    if (!s) {
      // "refresh" is the card's own reload, as on the other sections' error states.
      return card.error
        ? UI.loadError(t("account.load_failed"), "refresh")
        : `<div class="hf-skeletons"><div class="hf-skeleton"></div><div class="hf-skeleton"></div></div>`;
    }
    if (s.plan_preference && this.presets === null && !this.presetsLoading) this._fetchPresets();
    const price = (amount, currency) => (amount == null ? null : L.fmtPrice(amount, currency));
    const shipping = L.breakdownAmount(s, "shipping_amount");
    const discount = L.breakdownAmount(s, "discount_amount", true);
    const cardOnFile = L.cardOnFile(s);
    const expiry = L.fmtCardExpiry(s.payment_card_expiry);
    const cardState = s.payment_method_expired ? "account.card_expired" : s.payment_method_expiring ? "account.card_expiring" : "";
    const cells = [
      [t("account.plan"), s.selected_plan],
      [t("account.meal_preference"), L.preferenceName(s.plan_preference, this.presets)],
      [t("account.people"), s.number_of_people],
      [t("account.meals_per_box"), s.required_meal_count],
      [t("money.plan_price"), price(s.selected_plan_total_price, s.selected_plan_total_price_currency)],
      [t("box.shipping"), shipping == null ? null : price(shipping, s.selected_plan_total_price_currency)],
      [t("money.discount"), discount == null ? null : `−${price(discount, s.selected_plan_total_price_currency)}`],
      [t("account.credit"), price(s.account_credit, s.account_credit_currency)],
      // HelloFresh Cash, bonus credit and boxes until a free one, under the credit.
      ...L.creditDetails(s),
      [
        t("account.card_on_file"),
        cardOnFile
          ? [cardOnFile, expiry ? t("account.card_expiry", { date: expiry }) : "", cardState ? t(cardState) : ""].filter(Boolean).join(" · ")
          : null,
      ],
      [t("account.boxes_received"), s.boxes_received],
      [t("account.account_id"), s.account_id],
      [t("account.delivery_address"), s.delivery_address, true],
    ].filter(([, value]) => value !== null && value !== undefined && value !== "");
    const kv = cells
      .map(([k, v, wide]) => `<div class="${wide ? "wide" : ""}"><div class="hf-k">${esc(k)}</div><div class="hf-v">${esc(v)}</div></div>`)
      .join("");
    const status = s.subscription_status
      ? pill(L.statusLabel(s.subscription_status), String(s.subscription_status).toLowerCase() === "active" ? "ok" : "warn")
      : "";

    const needsId = (s.weeks_needing_selection_ids || [])[0];
    const stat = (label, value, weekId = null, note = "") => {
      if (value === null || value === undefined || value === "") return "";
      const click = weekId && card.weekById(weekId) && card.hasView("menu");
      return `<div class="hf-stat${click ? " clickable" : ""}"${click ? ` role="button" tabindex="0" data-action="goto" data-view="menu" data-week-id="${esc(weekId)}"` : ""}>
          <span class="hf-statlabel">${esc(label)}</span><span class="hf-statvalue">${esc(value)}</span>${note ? `<span class="hf-statnote">${esc(note)}</span>` : ""}</div>`;
    };
    // The summary names weeks by id ("2026-W43"); show the delivery date when the card has it.
    const skippedWeek = s.next_skipped_week_id ? card.weekById(s.next_skipped_week_id) : null;
    const nextSkipped = skippedWeek ? L.fmtDate(skippedWeek.delivery_date) : s.next_skipped_week;
    const stats = [
      stat(t("account.upcoming_boxes"), s.upcoming_delivery_count),
      stat(t("account.need_picks"), s.weeks_needing_selection, needsId, needsId ? t("account.tap_to_review") : ""),
      stat(t("state.skipped.label"), s.skipped_week_count),
      stat(t("account.next_skipped"), nextSkipped, s.next_skipped_week_id),
    ].join("");

    return `<div class="hf-cols two">
        <div style="display:flex;flex-direction:column;gap:16px;min-width:0">
          <section class="hf-panel">
            <div class="hf-sectionhead" style="margin-top:0"><h2 class="hf-h2">${ht("account.your_plan")}</h2>${status}</div>
            <div class="hf-kv">${kv}</div>
          </section>
          ${this._controls()}
        </div>
        <div style="display:flex;flex-direction:column;gap:16px;min-width:0">
          ${stats ? `<section class="hf-panel"><h2 class="hf-h2" style="margin-bottom:12px">${ht("account.upcoming")}</h2><div class="hf-stats">${stats}</div></section>` : ""}
          ${this._presetsPanel(s)}
          ${this._healthPanel()}
        </div>
      </div>`;
  }

  // Box size and delivery day through this integration's select entities (they own the option
  // catalogs and the writes). Shown only when Home Assistant has the entities.
  _controls() {
    const card = this.card;
    const ids = card.entities();
    const rows = [];
    this._settlePlan();
    const pending = this.planPending;
    const control = (entityId, key) => {
      const state = card.entityState(entityId);
      if (!state) return;
      const options = (state.attributes && state.attributes.options) || [];
      const unavailable = state.state === "unavailable" || state.state === "unknown" || !options.length;
      // A change on its way shows as chosen: snapping back to the old option for the seconds
      // HelloFresh takes looked like the change had failed.
      const shown = pending && pending.entityId === entityId ? pending.option : state.state;
      const select = unavailable
        ? `<span class="hf-muted hf-small">${ht("plan.options_loading")}</span>`
        : `<select class="hf-select" style="min-width:200px" data-plan-control="${esc(key)}" data-entity="${esc(entityId)}"
            data-focus-key="plan-${esc(key)}" ${card.busy || pending ? "disabled" : ""} aria-label="${ht(`plan.${key}.label`)}"${
              pending && pending.entityId === entityId ? ' aria-busy="true"' : ""
            }>
            ${options.map((o) => `<option value="${esc(o)}" ${o === shown ? "selected" : ""}>${esc(planOptionLabel(key, o))}</option>`).join("")}</select>`;
      rows.push(`<div class="hf-controlrow"><div><div class="hf-controllabel">${ht(`plan.${key}.label`)}</div>
          <div class="hf-controlhint">${ht(`plan.${key}.hint`)}</div></div>${select}</div>`);
    };
    control(ids.boxSize, "box_size");
    control(ids.deliveryDay, "delivery_day");
    if (!rows.length) return "";
    return `<section class="hf-panel"><h2 class="hf-h2" style="margin-bottom:4px">${ht("plan.settings")}</h2>
        <p class="hf-small hf-muted" style="margin:0 0 6px">${ht("plan.settings_note", {
          change_day: t("week.change_day"),
          skip: t("week.skip"),
        })}</p>${rows.join("")}</section>`;
  }

  _confirmPlanChange(entityId, key, option) {
    const card = this.card;
    const current = card.entityState(entityId);
    if (!PLAN_CONTROLS.includes(key)) return;
    card.openSheet({
      kind: "confirm",
      narrow: true,
      label: t(`plan.${key}.change`),
      render: () => `
        ${UI.sheetHead(
          t(`plan.${key}.title`),
          t("plan.from_to", { from: current ? planOptionLabel(key, current.state) : "—", to: planOptionLabel(key, option) })
        )}
        <div class="hf-sheetbody"><p class="hf-confirmtext" style="margin:0">${ht(`plan.${key}.body`, {
          every_future_box: strong(t("plan.every_future_box")),
        })}</p></div>
        <div class="hf-sheetfoot"><button class="hf-btn" data-close-sheet>${ht("common.cancel")}</button>
          <button class="hf-btn primary" data-action="confirm-plan" ${card.busy || this.planPending ? "disabled" : ""}>${ht(`plan.${key}.change`)}</button></div>`,
      onClick: (_ev, el) => {
        if (el && el.getAttribute("data-action") === "confirm-plan") this._changePlan(entityId, key, option);
      },
      // Cancelling puts the select back to the real value (confirming, to the one being written).
      onClose: () => card.renderView(),
    });
  }

  // Write a confirmed plan change. It changes every future box and the bill, so it runs as one
  // of the card's saves: the card is busy (both selects disabled, "Updating your plan…" shown)
  // until HelloFresh has answered, the select shows the option being written, and nothing else
  // can be written meanwhile. A failed write gives the control back at once; an accepted one
  // keeps showing its option, still closed to another change, until the entity reports it.
  //
  // Its toast offers Undo, which selects the option the plan had before through this same path
  // (`undoing`).
  async _changePlan(entityId, key, option, { undoing = false } = {}) {
    const card = this.card;
    if (card.busy) return;
    if (this.planPending) {
      // A change waiting only for its entity to report it is no reason to refuse taking it back.
      if (!undoing || !this.planPending.written) return;
      clearTimeout(this.planPending.timer);
      this.planPending = null;
    }
    const state = card.entityState(entityId);
    const options = (state && state.attributes && state.attributes.options) || [];
    const before = state && options.includes(state.state) ? state.state : null;
    const pending = { entityId, key, option, written: false, timer: null };
    this.planPending = pending;
    card.setSaving(t(undoing ? "undo.undoing" : "plan.saving"));
    // The confirmation's onClose re-renders the view, now busy and showing the new option.
    if (undoing) card.renderView();
    else card.closeSheet();
    let failed = null;
    try {
      // The card says what happened itself; no second "Failed to perform action" from Home Assistant.
      await card.hass.callService("select", "select_option", { option }, { entity_id: entityId }, false);
    } catch (err) {
      failed = (err && err.message) || String(err);
    }
    card.setSaving(null);
    if (failed) {
      this.planPending = null;
      if (undoing) card.undoResult(failed);
      else card.toast(t(`plan.${key}.failed`, { error: failed }), true);
      card.renderView();
      return;
    }
    pending.written = true;
    pending.timer = setTimeout(() => {
      if (this.planPending !== pending) return;
      this.planPending = null;
      if (card.view === "account") card.renderView();
    }, PLAN_SETTLE_MS);
    if (undoing) card.undoResult(null);
    else {
      const undo = before && before !== option ? { weekId: null, run: () => this._changePlan(entityId, key, before, { undoing: true }) } : null;
      card.toast(t(`plan.${key}.changed`), false, { undo });
    }
    card.broadcastDataChanged();
    card.renderView(); // settles at once when the entity already says the new option
    await card.reloadWeeks();
  }

  // An accepted plan change is over once its entity reports the new option (the card re-renders
  // this view when a plan entity changes) or is gone; until then the control shows the option.
  _settlePlan() {
    const pending = this.planPending;
    if (!pending || !pending.written) return;
    const state = this.card.entityState(pending.entityId);
    if (state && state.state !== pending.option) return;
    clearTimeout(pending.timer);
    this.planPending = null;
  }

  _presetsPanel(s) {
    if (!s) return "";
    const active = String(s.plan_preference || "").toLowerCase();
    const head = `<button class="hf-disclosure" data-action="toggle-presets" aria-expanded="${this.presetsOpen}">
        ${icon(this.presetsOpen ? "mdi:chevron-down" : "mdi:chevron-right")}${ht("account.presets")}</button>`;
    if (!this.presetsOpen) return `<section class="hf-panel">${head}</section>`;
    let list;
    if (this.presetsLoading && this.presets === null) list = `<div class="hf-muted hf-small">${ht("account.presets_loading")}</div>`;
    else if (!this.presets || !this.presets.length) list = `<div class="hf-muted hf-small">${ht("account.presets_none")}</div>`;
    else {
      list = `<div class="hf-presets">${this.presets
        .map((p) => {
          const handle = String((p && p.handle) || "").toLowerCase();
          const mine = handle && handle === active;
          return `<div class="hf-preset${mine ? " active" : ""}"><div class="hf-presetname">${esc(p.name || p.handle)}${mine ? pill(t("account.yours"), "ok") : ""}</div>
              ${p.description ? `<div class="hf-presetdesc">${esc(p.description)}</div>` : ""}</div>`;
        })
        .join("")}</div>`;
    }
    return `<section class="hf-panel">${head}<p class="hf-small hf-muted" style="margin:6px 0 10px">${ht("account.presets_note")}</p>${list}</section>`;
  }

  async _fetchPresets() {
    this.presetsLoading = true;
    try {
      const response = await this.card.call("get_presets");
      this.presets = Array.isArray(response.presets) ? response.presets : [];
    } catch (_err) {
      this.presets = [];
    } finally {
      this.presetsLoading = false;
      if (this.card.view === "account") this.card.renderView();
    }
  }

  _healthPanel() {
    const card = this.card;
    const ids = card.entities();
    const head = `<button class="hf-disclosure" data-action="toggle-health" aria-expanded="${this.healthOpen}">
        ${icon(this.healthOpen ? "mdi:chevron-down" : "mdi:chevron-right")}${ht("health.title")}</button>`;
    if (!this.healthOpen) return `<section class="hf-panel">${head}</section>`;
    const item = (label, text, tone, iconName) =>
      `<div class="hf-stat"><span class="hf-statlabel">${esc(label)}</span><span class="hf-statvalue" style="font-size:0.95em">${pill(text, tone, iconName)}</span></div>`;
    const cells = [];
    const access = card.entityState(ids.accessToken);
    if (access) {
      const mins = Number(access.state);
      const left = Number.isFinite(mins) ? t("health.minutes_left", { count: Math.max(0, Math.round(mins)) }) : access.state;
      cells.push(item(t("health.access_token"), left, Number.isFinite(mins) && mins > 5 ? "ok" : "warn", "mdi:key-outline"));
    }
    const refresh = card.entityState(ids.refreshToken);
    if (refresh) {
      const days = Number(refresh.state);
      const left = Number.isFinite(days) ? t("health.days_left", { count: Math.max(0, Math.round(days)) }) : refresh.state;
      cells.push(item(t("health.refresh_token"), left, Number.isFinite(days) && days > 3 ? "ok" : "warn", "mdi:key-chain-variant"));
    }
    const writes = card.entityState(ids.writeActions);
    if (writes) cells.push(item(t("health.write_actions"), t(writes.state === "on" ? "health.available" : "health.unavailable"), writes.state === "on" ? "ok" : "danger", "mdi:pencil-lock-outline"));
    const shape = card.entityState(ids.payloadShape);
    if (shape) cells.push(item(t("health.api_payloads"), t(shape.state === "on" ? "health.payload_changed" : "health.payload_ok"), shape.state === "on" ? "warn" : "ok", "mdi:code-json"));
    const last = card.lastFetched
      ? new Date(card.lastFetched).toLocaleTimeString(L.I18n.timeLocale(), { hour: "numeric", minute: "2-digit", ...L.I18n.hourOptions() })
      : "—";
    return `<section class="hf-panel">${head}
        ${cells.length ? `<div class="hf-health" style="margin-top:10px">${cells.join("")}</div>` : `<p class="hf-small hf-muted">${ht("health.no_sensors")}</p>`}
        <div class="hf-actions" style="margin-top:12px;justify-content:space-between">
          <span class="hf-small hf-muted">${ht("health.card_updated", { time: last })} · v${esc(ACCOUNT_VERSION)}</span>
          <button class="hf-btn sm" data-action="refresh-data" ${card.busy ? "disabled" : ""}>${icon("mdi:cloud-refresh-outline")}${ht("health.refresh")}</button>
        </div></section>`;
  }

  // ---- spending --------------------------------------------------------------------------------

  async _fetchSpending() {
    if (this.spendingLoading || !this.card.hass) return;
    this.spendingLoading = true;
    if (this.card.view === "account") this.card.renderView();
    try {
      this.spending = (await this.card.call("get_spending")) || { weeks: [], months: [], total: null };
      this.spendingError = null;
    } catch (err) {
      this.spendingError = (err && err.message) || String(err);
    } finally {
      this.spendingLoading = false;
      if (this.card.view === "account") this.card.renderView();
    }
  }

  _spendingView() {
    if (!this.spending && !this.spendingError && !this.spendingLoading) queueMicrotask(() => this._fetchSpending());
    if (!this.spending) {
      if (this.spendingError) return UI.loadError(t("spending.load_failed", { error: this.spendingError }), "spending-retry");
      return `<div class="hf-skeletons"><div class="hf-skeleton"></div><div class="hf-skeleton"></div></div>`;
    }
    const s = this.spending;
    const weeks = Array.isArray(s.weeks) ? s.weeks : [];
    const months = Array.isArray(s.months) ? s.months : [];
    if (!weeks.length && !months.length && !s.total) {
      return `<div class="hf-empty">${icon("mdi:chart-bar")}${ht("spending.none")}</div>`;
    }
    const total = s.total || {};
    const boxes = Number(total.box_count) || 0;
    const avg = boxes > 0 && total.amount != null ? total.amount / boxes : null;
    const saved = Number(total.discount) > 0 ? L.fmtPrice(total.discount, total.currency) : null;
    const kpis = `<div class="hf-stats" style="margin-bottom:16px">
        ${total.amount != null ? `<div class="hf-stat big accent"><span class="hf-statlabel">${ht("spending.total_spent")}</span><span class="hf-statvalue">${esc(L.fmtPrice(total.amount, total.currency))}</span><span class="hf-statnote">${ht("spending.lifetime")}</span></div>` : ""}
        <div class="hf-stat"><span class="hf-statlabel">${ht("spending.boxes")}</span><span class="hf-statvalue">${esc(boxes)}</span></div>
        ${avg != null ? `<div class="hf-stat"><span class="hf-statlabel">${ht("spending.average_box")}</span><span class="hf-statvalue">${esc(L.fmtPrice(avg, total.currency))}</span></div>` : ""}
        ${saved ? `<div class="hf-stat"><span class="hf-statlabel">${ht("spending.saved_vouchers")}</span><span class="hf-statvalue" style="color:var(--hf-ok-fg)">${esc(saved)}</span></div>` : ""}
      </div>`;
    return `${kpis}${this._chart(months)}
      <div class="hf-cols halves" style="margin-top:16px">${this._months(months)}${this._ledger(weeks)}</div>`;
  }

  // Monthly box cost over the last year. Bars and labels are HTML (so text stays its normal
  // size however wide the card is — an SVG viewBox scaled them to 2-3x on a panel view); only
  // the trend line is SVG, stretched over the plot with a non-scaling stroke. Empty months keep
  // their slot so gaps read as gaps, and the trend bridges them rather than dipping to zero.
  _chart(months) {
    const cfg = this.card.config;
    if (cfg.spending_chart === false || !months.length) return "";
    const span = Math.max(1, Math.min(24, Number(cfg.spending_months) || 12));
    const series = L.denseMonths(months, span);
    if (!series.length) return "";
    const currency = (series.find((m) => m.currency) || {}).currency || null;
    const max = series.reduce((m, x) => Math.max(m, x.amount), 0);
    const n = series.length;
    // Bars top out at 82% of the plot so the tallest one's label has room above it.
    const pct = (amount) => (max > 0 ? (amount / max) * 82 : 0);
    const cols = series
      .map((m) => {
        const cls = m.amount <= 0 ? " empty" : m.upcoming ? " upcoming" : "";
        const title = `${L.fmtMonth(m.month)}: ${m.amount > 0 ? L.fmtPrice(m.amount, m.currency || currency) : t("spending.no_box")}${
          m.upcoming ? ` (${t("spending.upcoming_note")})` : ""
        }`;
        const h = pct(m.amount);
        return `<div class="hf-bc-col${cls}" title="${esc(title)}">
            ${m.amount > 0 ? `<span class="hf-bc-val" style="bottom:calc(${h.toFixed(1)}% + 6px)">${esc(L.fmtPriceCompact(m.amount, m.currency || currency))}</span>` : ""}
            <span class="hf-bc-bar" style="height:${Math.max(m.amount > 0 ? 2 : 1, h).toFixed(1)}%"></span>
          </div>`;
      })
      .join("");
    const axis = series
      .map((m, i) => {
        const [yr, mo] = m.month.split("-");
        const year = mo === "01" || i === 0 ? `<small>${esc(yr.slice(2))}</small>` : "";
        return `<span>${esc(L.fmtMonth(m.month, { month: "narrow" }))}${year}</span>`;
      })
      .join("");
    const priced = series.map((m, i) => ({ m, x: ((i + 0.5) / n) * 100, y: 100 - pct(m.amount) })).filter((p) => p.m.amount > 0);
    const trend =
      priced.length >= 2
        ? `<svg class="hf-bc-trend" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
            <polyline points="${priced.map((p) => `${p.x.toFixed(2)},${p.y.toFixed(2)}`).join(" ")}" vector-effect="non-scaling-stroke" /></svg>
           ${priced.map((p) => `<span class="hf-bc-dot" style="left:${p.x.toFixed(2)}%;bottom:${(100 - p.y).toFixed(2)}%"></span>`).join("")}`
        : "";
    return `<section class="hf-panel">
        <div class="hf-sectionhead" style="margin-top:0"><h2 class="hf-h2">${ht("spending.monthly")}</h2>
          ${max > 0 ? `<span class="hf-sectionnote">${ht("spending.peak", { price: L.fmtPrice(max, currency) })}</span>` : ""}</div>
        <div class="hf-barchart" role="img" aria-label="${ht("spending.chart_label", { count: n })}">
          <div class="hf-bc-plot"><div class="hf-bc-cols" style="grid-template-columns:repeat(${n},minmax(0,1fr))">${cols}</div>${trend}</div>
          <div class="hf-bc-axis" style="grid-template-columns:repeat(${n},minmax(0,1fr))">${axis}</div>
        </div>
      </section>`;
  }

  _months(months) {
    const cap = Number(this.card.config.spending_month_rows) || 6;
    const shown = months.slice(0, cap);
    if (!shown.length) return "";
    const max = shown.reduce((m, x) => Math.max(m, Number(x.amount) || 0), 0) || 1;
    return `<section class="hf-panel"><h2 class="hf-h2" style="margin-bottom:12px">${ht("spending.by_month")}</h2><div class="hf-bars">${shown
      .map((m) => {
        const amount = Number(m.amount) || 0;
        const pct = Math.max(2, Math.round((amount / max) * 100));
        const n = Number(m.box_count) || 0;
        return `<div class="hf-barrow${m.upcoming ? " upcoming" : ""}"><span class="hf-muted">${esc(L.fmtMonth(m.month))}</span>
            <span class="hf-bar"><span style="width:${pct}%"></span></span>
            <span class="hf-barval">${esc(L.fmtPrice(amount, m.currency))}<small>${ht("calendar.boxes", { count: n })}</small></span></div>`;
      })
      .join("")}</div></section>`;
  }

  _ledger(weeks) {
    const cap = Number(this.card.config.spending_box_rows) || 8;
    const shown = weeks.slice(0, cap);
    if (!shown.length) return "";
    return `<section class="hf-panel"><h2 class="hf-h2" style="margin-bottom:4px">${ht("spending.recent_boxes")}</h2><div class="hf-ledger">${shown
      .map(
        (w) => `<div class="hf-ledgerrow${w.upcoming ? " upcoming" : ""}">
          <span>${esc(L.fmtDate(w.delivery_date, { month: "short", day: "numeric", year: "numeric" }))}
            ${w.upcoming ? pill(t("spending.upcoming"), "muted") : ""}
            ${Number(w.discount) > 0 ? `<span class="hf-saved" title="${w.coupon_code ? ht("voucher.code", { code: w.coupon_code }) : ht("voucher.label")}">−${esc(L.fmtPrice(w.discount, w.currency))}</span>` : ""}</span>
          <strong>${esc(L.fmtPrice(w.amount, w.currency))}</strong></div>`
      )
      .join("")}</div></section>`;
  }

  // ---- events --------------------------------------------------------------------------------

  onClick(ev, el) {
    if (this.tab === "profile" && this.profile.onClick(ev, el)) return;
    if (!el) return;
    const card = this.card;
    switch (el.getAttribute("data-action")) {
      case "acct-tab":
        this._selectTab(el.getAttribute("data-tab"));
        break;
      case "toggle-presets":
        this.presetsOpen = !this.presetsOpen;
        if (this.presetsOpen && this.presets === null && !this.presetsLoading) this._fetchPresets();
        card.renderView();
        break;
      case "toggle-health":
        this.healthOpen = !this.healthOpen;
        card.renderView();
        break;
      case "refresh-data":
        this._refreshFromHelloFresh();
        break;
      case "spending-retry":
        this._fetchSpending();
        break;
      default:
        break;
    }
  }

  async _refreshFromHelloFresh() {
    const card = this.card;
    try {
      await card.callAction("refresh_data");
      await card.refresh({ quiet: true });
      card.toast(t("health.refreshed"));
    } catch (err) {
      card.toast(t("health.refresh_failed", { error: (err && err.message) || err }), true);
    }
  }

  onChange(ev) {
    if (this.tab === "profile") {
      this.profile.onChange(ev);
      return;
    }
    const select = ev.target.closest("[data-plan-control]");
    if (!select) return;
    this._settlePlan();
    if (this.card.busy || this.planPending) {
      // A write is on its way: this change is not taken, and the select goes back to showing it.
      this.card.renderView();
      return;
    }
    const entityId = select.getAttribute("data-entity");
    const current = this.card.entityState(entityId);
    if (current && select.value === current.state) return;
    this._confirmPlanChange(entityId, select.getAttribute("data-plan-control"), select.value);
  }
}
