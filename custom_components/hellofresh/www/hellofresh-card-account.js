/*
 * HelloFresh card — Account
 * -------------------------
 * Three tabs, mirroring hellofresh.com's account area:
 *
 *   Plan & billing   — the classic Subscription card's account overview (plan, preference with
 *                      its preset name, price split, credit, card on file, servings, address,
 *                      upcoming counters that jump to their week, the meal-preset reference),
 *                      plus the recurring plan controls that used to need a separate entities
 *                      card (box size, delivery day — via this integration's select entities,
 *                      behind a confirmation because they change every future box and the bill)
 *                      and a small integration-health panel (the old Diagnostics view).
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

const { esc } = L;
const { icon, pill } = UI;

const TABS = [
  { key: "plan", label: "Plan & billing", icon: "mdi:card-account-details-outline" },
  { key: "profile", label: "Food preferences", icon: "mdi:food-apple-outline" },
  { key: "spending", label: "Spending", icon: "mdi:chart-bar" },
];

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
  }

  onRefresh() {
    if (this.tab === "profile") this.profile.onRefresh();
    if (this.spending || this.spendingError) this._fetchSpending();
  }

  render() {
    const tabs = `<div class="hf-segment hf-subtabs" role="tablist" aria-label="Account sections">${TABS.map(
      (t) => `<button role="tab" data-action="acct-tab" data-tab="${t.key}" aria-pressed="${this.tab === t.key}" aria-selected="${this.tab === t.key}">${icon(t.icon)}${esc(t.label)}</button>`
    ).join("")}</div>`;
    let body = "";
    if (this.tab === "profile") body = this.profile.render();
    else if (this.tab === "spending") body = this._spendingView();
    else body = this._planView();
    return `${tabs}${body}`;
  }

  // ---- plan & billing ------------------------------------------------------------------------

  _planView() {
    const card = this.card;
    const s = card.summary;
    if (!s) {
      return card.error
        ? `<div class="hf-empty">${icon("mdi:cloud-alert-outline")}Couldn't load your account.</div>`
        : `<div class="hf-skeletons"><div class="hf-skeleton"></div><div class="hf-skeleton"></div></div>`;
    }
    if (s.plan_preference && this.presets === null && !this.presetsLoading) this._fetchPresets();
    const price = (amount, currency) => (amount == null ? null : L.fmtPrice(amount, currency));
    const shipping = L.breakdownAmount(s, "shipping_amount");
    const discount = L.breakdownAmount(s, "discount_amount", true);
    const cardOnFile = L.cardOnFile(s);
    const expiry = L.fmtCardExpiry(s.payment_card_expiry);
    const cells = [
      ["Plan", s.selected_plan],
      ["Meal preference", L.preferenceName(s.plan_preference, this.presets)],
      ["Servings", s.number_of_people],
      ["Meals per box", s.required_meal_count],
      ["Plan price", price(s.selected_plan_total_price, s.selected_plan_total_price_currency)],
      ["Shipping", shipping == null ? null : price(shipping, s.selected_plan_total_price_currency)],
      ["Discount", discount == null ? null : `−${price(discount, s.selected_plan_total_price_currency)}`],
      ["Credit", price(s.account_credit, s.account_credit_currency)],
      [
        "Card on file",
        cardOnFile
          ? `${cardOnFile}${expiry ? ` · exp. ${expiry}` : ""}${s.payment_method_expired ? " · expired" : s.payment_method_expiring ? " · expiring" : ""}`
          : null,
      ],
      ["Boxes received", s.boxes_received],
      ["Account ID", s.account_id],
      ["Delivery address", s.delivery_address, true],
    ].filter(([, value]) => value !== null && value !== undefined && value !== "");
    const kv = cells
      .map(([k, v, wide]) => `<div class="${wide ? "wide" : ""}"><div class="hf-k">${esc(k)}</div><div class="hf-v">${esc(v)}</div></div>`)
      .join("");
    const status = s.subscription_status
      ? pill(L.titleCase(s.subscription_status), String(s.subscription_status).toLowerCase() === "active" ? "ok" : "warn")
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
      stat("Upcoming boxes", s.upcoming_delivery_count),
      stat("Need picks", s.weeks_needing_selection, needsId, needsId ? "Tap to review" : ""),
      stat("Skipped", s.skipped_week_count),
      stat("Next skipped", nextSkipped, s.next_skipped_week_id),
    ].join("");

    return `<div class="hf-cols two">
        <div style="display:flex;flex-direction:column;gap:16px;min-width:0">
          <section class="hf-panel">
            <div class="hf-sectionhead" style="margin-top:0"><h2 class="hf-h2">Your plan</h2>${status}</div>
            <div class="hf-kv">${kv}</div>
          </section>
          ${this._controls()}
        </div>
        <div style="display:flex;flex-direction:column;gap:16px;min-width:0">
          ${stats ? `<section class="hf-panel"><h2 class="hf-h2" style="margin-bottom:12px">Upcoming</h2><div class="hf-stats">${stats}</div></section>` : ""}
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
    const control = (entityId, key, label, hint) => {
      const state = card.entityState(entityId);
      if (!state) return;
      const options = (state.attributes && state.attributes.options) || [];
      const unavailable = state.state === "unavailable" || state.state === "unknown" || !options.length;
      const select = unavailable
        ? `<span class="hf-muted hf-small">Options load shortly…</span>`
        : `<select class="hf-select" style="min-width:200px" data-plan-control="${esc(key)}" data-entity="${esc(entityId)}"
            data-focus-key="plan-${esc(key)}" ${card.busy ? "disabled" : ""} aria-label="${esc(label)}">
            ${options.map((o) => `<option value="${esc(o)}" ${o === state.state ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
      rows.push(`<div class="hf-controlrow"><div><div class="hf-controllabel">${esc(label)}</div>
          <div class="hf-controlhint">${esc(hint)}</div></div>${select}</div>`);
    };
    control(ids.boxSize, "box_size", "Box size", "Meals per week and servings per meal");
    control(ids.deliveryDay, "delivery_day", "Delivery day", "Your usual delivery day and window");
    if (!rows.length) return "";
    return `<section class="hf-panel"><h2 class="hf-h2" style="margin-bottom:4px">Plan settings</h2>
        <p class="hf-small hf-muted" style="margin:0 0 6px">These change every future box — and what you're billed.
          To change a single week, use Change day or Skip on that week.</p>${rows.join("")}</section>`;
  }

  _confirmPlanChange(entityId, key, option) {
    const card = this.card;
    const current = card.entityState(entityId);
    const what = key === "box_size" ? "box size" : "delivery day";
    card.openSheet({
      kind: "confirm",
      narrow: true,
      label: `Change ${what}`,
      render: () => `
        <div class="hf-sheethead"><div class="hf-sheettitle"><h2>Change your ${esc(what)}?</h2>
          <div class="hf-sheetsub">From ${esc(current ? current.state : "—")} to ${esc(option)}</div></div>
          <button class="hf-iconbtn" data-close-sheet aria-label="Close">${icon("mdi:close")}</button></div>
        <div class="hf-sheetbody"><p class="hf-confirmtext" style="margin:0">This applies to <strong>every future box</strong>${
          key === "box_size" ? " and changes what you're billed" : ""
        }. Boxes past their selection deadline keep their current setting.</p></div>
        <div class="hf-sheetfoot"><button class="hf-btn" data-close-sheet>Cancel</button>
          <button class="hf-btn primary" data-action="confirm-plan">Change ${esc(what)}</button></div>`,
      onClick: async (_ev, el) => {
        if (!el || el.getAttribute("data-action") !== "confirm-plan") return;
        card.closeSheet();
        try {
          await card.hass.callService("select", "select_option", { option }, { entity_id: entityId });
          card.toast(`${what[0].toUpperCase()}${what.slice(1)} changed.`);
          card.broadcastDataChanged();
          card.reloadWeeks();
        } catch (err) {
          card.toast(`Couldn't change the ${what}: ${(err && err.message) || err}`, true);
          card.renderView();
        }
      },
      // Cancelling puts the select back to the real value.
      onClose: () => card.renderView(),
    });
  }

  _presetsPanel(s) {
    if (!s) return "";
    const active = String(s.plan_preference || "").toLowerCase();
    const head = `<button class="hf-disclosure" data-action="toggle-presets" aria-expanded="${this.presetsOpen}">
        ${icon(this.presetsOpen ? "mdi:chevron-down" : "mdi:chevron-right")}Meal presets</button>`;
    if (!this.presetsOpen) return `<section class="hf-panel">${head}</section>`;
    let list;
    if (this.presetsLoading && this.presets === null) list = `<div class="hf-muted hf-small">Loading presets…</div>`;
    else if (!this.presets || !this.presets.length) list = `<div class="hf-muted hf-small">No presets available.</div>`;
    else {
      list = `<div class="hf-presets">${this.presets
        .map((p) => {
          const handle = String((p && p.handle) || "").toLowerCase();
          const mine = handle && handle === active;
          return `<div class="hf-preset${mine ? " active" : ""}"><div class="hf-presetname">${esc(p.name || p.handle)}${mine ? pill("Yours", "ok") : ""}</div>
              ${p.description ? `<div class="hf-presetdesc">${esc(p.description)}</div>` : ""}</div>`;
        })
        .join("")}</div>`;
    }
    return `<section class="hf-panel">${head}<p class="hf-small hf-muted" style="margin:6px 0 10px">What each preset means — HelloFresh
        uses yours to pick meals. Change it on hellofresh.com.</p>${list}</section>`;
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
        ${icon(this.healthOpen ? "mdi:chevron-down" : "mdi:chevron-right")}Integration status</button>`;
    if (!this.healthOpen) return `<section class="hf-panel">${head}</section>`;
    const item = (label, text, tone, iconName) =>
      `<div class="hf-stat"><span class="hf-statlabel">${esc(label)}</span><span class="hf-statvalue" style="font-size:0.95em">${pill(text, tone, iconName)}</span></div>`;
    const cells = [];
    const access = card.entityState(ids.accessToken);
    if (access) {
      const mins = Number(access.state);
      cells.push(item("Access token", Number.isFinite(mins) ? `${Math.max(0, Math.round(mins))} min left` : access.state, Number.isFinite(mins) && mins > 5 ? "ok" : "warn", "mdi:key-outline"));
    }
    const refresh = card.entityState(ids.refreshToken);
    if (refresh) {
      const days = Number(refresh.state);
      cells.push(item("Refresh token", Number.isFinite(days) ? `${Math.max(0, Math.round(days))} days left` : refresh.state, Number.isFinite(days) && days > 3 ? "ok" : "warn", "mdi:key-chain-variant"));
    }
    const writes = card.entityState(ids.writeActions);
    if (writes) cells.push(item("Write actions", writes.state === "on" ? "Available" : "Unavailable", writes.state === "on" ? "ok" : "danger", "mdi:pencil-lock-outline"));
    const shape = card.entityState(ids.payloadShape);
    if (shape) cells.push(item("API payloads", shape.state === "on" ? "Changed — check logs" : "As expected", shape.state === "on" ? "warn" : "ok", "mdi:code-json"));
    const last = card.lastFetched ? new Date(card.lastFetched).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }) : "—";
    return `<section class="hf-panel">${head}
        ${cells.length ? `<div class="hf-health" style="margin-top:10px">${cells.join("")}</div>` : `<p class="hf-small hf-muted">Status sensors aren't available.</p>`}
        <div class="hf-actions" style="margin-top:12px;justify-content:space-between">
          <span class="hf-small hf-muted">Card updated ${esc(last)} · v${esc(ACCOUNT_VERSION)}</span>
          <button class="hf-btn sm" data-action="refresh-data" ${card.busy ? "disabled" : ""}>${icon("mdi:cloud-refresh-outline")}Refresh from HelloFresh</button>
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
      if (this.spendingError) {
        return `<div class="hf-empty">${icon("mdi:cloud-alert-outline")}Couldn't load your spending: ${esc(this.spendingError)}
          <div class="hf-actions" style="justify-content:center;margin-top:12px"><button class="hf-btn" data-action="spending-retry">Try again</button></div></div>`;
      }
      return `<div class="hf-skeletons"><div class="hf-skeleton"></div><div class="hf-skeleton"></div></div>`;
    }
    const s = this.spending;
    const weeks = Array.isArray(s.weeks) ? s.weeks : [];
    const months = Array.isArray(s.months) ? s.months : [];
    if (!weeks.length && !months.length && !s.total) {
      return `<div class="hf-empty">${icon("mdi:chart-bar")}No spending history yet.</div>`;
    }
    const total = s.total || {};
    const boxes = Number(total.box_count) || 0;
    const avg = boxes > 0 && total.amount != null ? total.amount / boxes : null;
    const saved = Number(total.discount) > 0 ? L.fmtPrice(total.discount, total.currency) : null;
    const kpis = `<div class="hf-stats" style="margin-bottom:16px">
        ${total.amount != null ? `<div class="hf-stat big accent"><span class="hf-statlabel">Total spent</span><span class="hf-statvalue">${esc(L.fmtPrice(total.amount, total.currency))}</span><span class="hf-statnote">Delivered boxes, lifetime</span></div>` : ""}
        <div class="hf-stat"><span class="hf-statlabel">Boxes</span><span class="hf-statvalue">${esc(boxes)}</span></div>
        ${avg != null ? `<div class="hf-stat"><span class="hf-statlabel">Average box</span><span class="hf-statvalue">${esc(L.fmtPrice(avg, total.currency))}</span></div>` : ""}
        ${saved ? `<div class="hf-stat"><span class="hf-statlabel">Saved with vouchers</span><span class="hf-statvalue" style="color:var(--hf-ok-fg)">${esc(saved)}</span></div>` : ""}
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
        const title = `${L.fmtMonth(m.month)}: ${m.amount > 0 ? L.fmtPrice(m.amount, m.currency || currency) : "no box"}${m.upcoming ? " (upcoming)" : ""}`;
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
        <div class="hf-sectionhead" style="margin-top:0"><h2 class="hf-h2">Monthly box cost</h2>
          ${max > 0 ? `<span class="hf-sectionnote">Peak ${esc(L.fmtPrice(max, currency))}</span>` : ""}</div>
        <div class="hf-barchart" role="img" aria-label="Monthly HelloFresh box cost over the last ${n} months">
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
    return `<section class="hf-panel"><h2 class="hf-h2" style="margin-bottom:12px">By month</h2><div class="hf-bars">${shown
      .map((m) => {
        const amount = Number(m.amount) || 0;
        const pct = Math.max(2, Math.round((amount / max) * 100));
        const n = Number(m.box_count) || 0;
        return `<div class="hf-barrow${m.upcoming ? " upcoming" : ""}"><span class="hf-muted">${esc(L.fmtMonth(m.month))}</span>
            <span class="hf-bar"><span style="width:${pct}%"></span></span>
            <span class="hf-barval">${esc(L.fmtPrice(amount, m.currency))}<small>${esc(L.plural(n, "box", "boxes"))}</small></span></div>`;
      })
      .join("")}</div></section>`;
  }

  _ledger(weeks) {
    const cap = Number(this.card.config.spending_box_rows) || 8;
    const shown = weeks.slice(0, cap);
    if (!shown.length) return "";
    return `<section class="hf-panel"><h2 class="hf-h2" style="margin-bottom:4px">Recent boxes</h2><div class="hf-ledger">${shown
      .map(
        (w) => `<div class="hf-ledgerrow${w.upcoming ? " upcoming" : ""}">
          <span>${esc(L.fmtDate(w.delivery_date, { month: "short", day: "numeric", year: "numeric" }))}
            ${w.upcoming ? pill("Upcoming", "muted") : ""}
            ${Number(w.discount) > 0 ? `<span class="hf-saved" title="${esc(w.coupon_code ? `Voucher ${w.coupon_code}` : "Voucher")}">−${esc(L.fmtPrice(w.discount, w.currency))}</span>` : ""}</span>
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
        this.tab = el.getAttribute("data-tab");
        L.storageSet(L.STORAGE_KEYS.accountTab, this.tab);
        card.renderView();
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
      card.toast("Refreshed from HelloFresh.");
    } catch (err) {
      card.toast(`Refresh failed: ${(err && err.message) || err}`, true);
    }
  }

  onChange(ev) {
    if (this.tab === "profile") {
      this.profile.onChange(ev);
      return;
    }
    const select = ev.target.closest("[data-plan-control]");
    if (!select) return;
    const entityId = select.getAttribute("data-entity");
    const current = this.card.entityState(entityId);
    if (current && select.value === current.state) return;
    this._confirmPlanChange(entityId, select.getAttribute("data-plan-control"), select.value);
  }
}
