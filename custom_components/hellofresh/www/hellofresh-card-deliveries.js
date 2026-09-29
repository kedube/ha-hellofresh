/*
 * HelloFresh card — Overview
 * --------------------------
 * The card's landing view, modelled on hellofresh.com's deliveries page: the next box front and
 * centre (date, what's in it, the selection deadline, delivery tracking and the pantry list),
 * then what's coming up — as week cards or a month calendar — and recent deliveries.
 *
 * Everything the classic Schedule card showed is here: the next-box summary (window, deadline
 * countdown, payment date, coupon, discount, voucher, status with the carrier's finer step), the
 * month calendar with per-state markers and holiday flags, per-week skip/unskip and change-day,
 * tracking numbers, scan history, proof of delivery and the month roll-up.
 */

const OVERVIEW_VERSION = new URL(import.meta.url).searchParams.get("v") || "unknown";
const stamp = encodeURIComponent(OVERVIEW_VERSION);
const [L, UI] = await Promise.all([
  import(new URL(`./hellofresh-card-logic.js?v=${stamp}`, import.meta.url).href),
  import(new URL(`./hellofresh-card-ui.js?v=${stamp}`, import.meta.url).href),
]);

const { esc } = L;
const { icon, pill } = UI;

const PAST_STEP = 6;

export class OverviewView {
  constructor(card) {
    this.card = card;
    this.mode = L.storageGet(L.STORAGE_KEYS.scheduleMode) === "calendar" ? "calendar" : "list";
    this.calMonth = null;
    this.historyOpen = null;
    this.pastShown = 4;
  }

  render() {
    const card = this.card;
    const placeholder = card.weeksPlaceholder();
    if (placeholder) return placeholder;
    const weeks = card.weeks;
    if (!weeks.length) {
      return `<div class="hf-empty">${icon("mdi:truck-outline")}No deliveries found for this account.</div>`;
    }
    const { week: next, upcoming } = L.nextBoxWeek(weeks);
    return [
      this._hero(next, upcoming),
      this._attention(next),
      this._pantry(next),
      this._comingUp(next),
      this._past(),
    ].join("");
  }

  // ---- next box ----------------------------------------------------------------------------

  _hero(week, upcoming) {
    if (!week) return "";
    const card = this.card;
    const state = L.weekState(week);
    const editable = L.isWeekEditable(week);
    const skipped = L.isSkipped(week);
    const selection = card.box.displayMeals(week);
    const meals = UI.chosenMeals(week, selection);
    const days = L.daysUntil(week.delivery_date);
    const eyebrow = !upcoming ? "Last box" : days === 0 ? "Arriving today" : days === 1 ? "Arriving tomorrow" : "Next box";
    const deadline = week.selection_deadline ? new Date(week.selection_deadline) : null;

    const meta = [];
    const rel = L.relativeWeek(week);
    if (rel && upcoming) meta.push(`<span>${icon("mdi:calendar-outline")}${esc(rel)}</span>`);
    const slot = week.slot_label || (week.order && week.order.slot_label);
    if (slot) meta.push(`<span>${icon("mdi:clock-outline")}${esc(slot)}</span>`);
    if (week.display_name) meta.push(`<span>${icon("mdi:package-variant-closed")}${esc(week.display_name)}</span>`);
    if (L.isHolidayShifted(week)) {
      meta.push(`<span title="${esc(week.holiday_message || "Holiday delivery change")}">${icon("mdi:calendar-star")}Holiday schedule</span>`);
    }

    const blocks = [];
    if (editable && deadline) {
      const tone = L.deadlineTone(deadline);
      blocks.push(`<div class="hf-deadline${tone === "urgent" ? " urgent" : ""}">${icon("mdi:timer-outline")}
        <div>Make changes by <strong>${esc(L.fmtDateTime(deadline))}</strong> · ${UI.countdownHtml(deadline)}</div></div>`);
    }
    if (skipped) {
      blocks.push(`<div class="hf-notice tone-muted">${icon("mdi:calendar-remove-outline")}<div class="hf-noticebody">
        <strong>This box is skipped.</strong> Nothing ships and you won't be charged${L.canSkip(week) ? " — unskip it until the deadline" : ""}.</div></div>`);
    } else if (L.isAutoPicked(week)) {
      blocks.push(`<div class="hf-notice tone-warn">${icon("mdi:auto-fix")}<div class="hf-noticebody">
        <strong>HelloFresh picked these meals for you.</strong> Keep them or swap any before the deadline.</div></div>`);
    }
    if (card.box.dirty(week)) {
      blocks.push(`<div class="hf-notice tone-info">${icon("mdi:content-save-alert-outline")}<div class="hf-noticebody">
        You have unsaved changes to this box.</div>
        <button class="hf-btn sm primary" data-action="goto" data-view="menu" data-week-id="${esc(week.week_id)}">Finish</button></div>`);
    }
    const tracking = UI.trackingBlock(week, { historyOpen: this.historyOpen === week.week_id });
    if (tracking) blocks.push(tracking);

    const money = [];
    const total = L.boxTotal(week, card.account);
    if (total) money.push(`<span><strong>${esc(L.fmtPrice(total.amount, total.currency))}</strong> ${esc(total.estimate ? "plan price" : "total")}</span>`);
    const account = card.account || {};
    if (upcoming && !skipped) {
      // The subscription's next charge belongs to this box only while it is still open; once
      // the deadline passes this box is paid for and the date is the NEXT box's.
      if (account.next_payment_date && editable) {
        money.push(`<span>${icon("mdi:credit-card-outline")}Charged ${esc(L.fmtDate(account.next_payment_date))}</span>`);
      }
      const discount = L.nextBoxDiscount(account);
      if (discount) money.push(`<span>${icon("mdi:tag-outline")}−${esc(discount)} discount</span>`);
      const voucher = L.nextBoxVoucher(week, account);
      if (voucher) {
        money.push(`<span title="${esc(voucher.code ? `Voucher ${voucher.code}` : "")}">${icon("mdi:ticket-percent-outline")}${esc(voucher.label)}${voucher.note ? ` <span class="hf-muted">(${esc(voucher.note)})</span>` : ""}</span>`);
      }
      if (account.next_box_coupon) money.push(`<span>${icon("mdi:ticket-outline")}Coupon ${esc(account.next_box_coupon)}</span>`);
    }

    // Primary actions are buttons; the rarer week changes (another day, skipping) sit in a quiet
    // link row underneath, the way hellofresh.com tucks them under the box.
    const actions = [];
    const links = [];
    const weekAttr = `data-week-id="${esc(week.week_id)}"`;
    if (editable) {
      const label = L.isAutoPicked(week) ? "Review meals" : meals.length ? "Edit meals" : "Choose meals";
      if (card.hasView("menu")) actions.push(`<button class="hf-btn primary" data-action="goto" data-view="menu" ${weekAttr}>${icon("mdi:silverware-fork-knife")}${label}</button>`);
      if (card.hasView("market") && (week.market_items || []).length) {
        actions.push(`<button class="hf-btn" data-action="goto" data-view="market" ${weekAttr}>${icon("mdi:storefront-outline")}Add extras</button>`);
      }
    } else if (!skipped && card.hasView("menu")) {
      actions.push(`<button class="hf-btn" data-action="goto" data-view="menu" ${weekAttr}>${icon("mdi:silverware-fork-knife")}View meals</button>`);
    }
    const pantryId = card.pantryEntityFor(week);
    if (pantryId && !skipped) {
      const s = card.entityState(pantryId);
      const count = s && Number(s.state);
      actions.push(`<button class="hf-btn" data-action="pantry" data-entity="${esc(pantryId)}" ${weekAttr}>${icon("mdi:basket-outline")}Pantry list${Number.isFinite(count) && count > 0 ? ` · ${count}` : ""}</button>`);
    }
    if (L.canSkip(week) && skipped) {
      actions.push(`<button class="hf-btn primary" data-action="skip" ${weekAttr} ${card.busy ? "disabled" : ""}>${icon("mdi:restore")}Unskip this week</button>`);
    }
    if (L.canReschedule(week)) {
      links.push(`<button class="hf-link quiet" data-action="reschedule" ${weekAttr} ${card.busy ? "disabled" : ""}>${icon("mdi:calendar-edit")}Change delivery day</button>`);
    }
    if (L.canSkip(week) && !skipped) {
      links.push(`<button class="hf-link quiet" data-action="skip" ${weekAttr} ${card.busy ? "disabled" : ""}>${icon("mdi:debug-step-over")}Skip this week</button>`);
    }

    return `<section class="hf-hero" aria-label="${esc(eyebrow)}">
        <div class="hf-heromain">
          <div class="hf-actions"><span class="hf-eyebrow">${esc(eyebrow)}</span>${UI.statePill(week, state)}</div>
          <div class="hf-herodate">${esc(L.fmtLongDate(week.delivery_date))}</div>
          ${meta.length ? `<div class="hf-herometa">${meta.join("")}</div>` : ""}
          ${blocks.join("")}
          ${money.length ? `<div class="hf-herometa">${money.join("")}</div>` : ""}
          ${actions.length ? `<div class="hf-actions">${actions.join("")}</div>` : ""}
          ${links.length ? `<div class="hf-linkrow">${links.join("")}</div>` : ""}
        </div>
        ${this._heroMeals(week, meals, editable, skipped)}
      </section>`;
  }

  _heroMeals(week, meals, editable, skipped) {
    const card = this.card;
    if (skipped) {
      return `<div class="hf-heroempty">${icon("mdi:calendar-remove-outline")}<div>No box this week.</div></div>`;
    }
    const extras = [...card.box.displayMarket(week).entries()];
    const extrasLine = extras.length
      ? `<div class="hf-small hf-muted" style="grid-column:1/-1">${icon("mdi:plus-circle-outline")} ${esc(
          L.plural(extras.reduce((a, [, q]) => a + q, 0), "extra")
        )}: ${esc(
          extras
            .map(([id]) => ((week.market_items || []).find((i) => i.item_id === id) || {}).name)
            .filter(Boolean)
            .slice(0, 3)
            .join(", ")
        )}${extras.length > 3 ? "…" : ""}</div>`
      : "";
    if (!meals.length) {
      return `<div class="hf-heroempty">${icon("mdi:silverware-variant")}<div>No meals chosen yet.</div>
        ${editable && card.hasView("menu") ? `<button class="hf-btn primary sm" data-action="goto" data-view="menu" data-week-id="${esc(week.week_id)}">Pick meals</button>` : ""}</div>`;
    }
    // Columns follow the count so a 3-meal box doesn't leave a hole in a 2x2 grid.
    const shown = meals.slice(0, meals.length > 4 ? 5 : 4);
    const tiles = shown
      .map(({ recipe, qty }) => {
        const img = L.resizedImage(recipe.image_url, 360);
        return `<div class="hf-heromeal" role="button" tabindex="0" data-action="recipe" data-recipe-id="${esc(recipe.recipe_id)}"
            aria-label="Open recipe: ${esc(recipe.name)}">
            <div class="hf-heromealwrap">${img ? `<img loading="lazy" src="${esc(img)}" alt="">` : `<div class="hf-noimg" style="aspect-ratio:16/10"></div>`}
              ${qty > 1 ? `<span class="hf-overlaypill hf-qtytag">${qty}×</span>` : ""}</div>
            <div class="hf-heromealname">${esc(recipe.name)}</div>
          </div>`;
      })
      .join("");
    const more =
      meals.length > shown.length
        ? `<button class="hf-more" data-action="goto" data-view="menu" data-week-id="${esc(week.week_id)}">+${meals.length - shown.length} more</button>`
        : "";
    const cells = shown.length + (more ? 1 : 0);
    const cols = cells <= 2 ? cells : cells === 4 ? 2 : 3;
    return `<div class="hf-heromeals cols-${cols}">${tiles}${more}${extrasLine}</div>`;
  }

  // ---- other weeks needing picks -----------------------------------------------------------

  _attention(next) {
    const card = this.card;
    const weeks = L.weeksNeedingAttention(card.weeks).filter((w) => w !== next);
    if (!weeks.length || !card.hasView("menu")) return "";
    const chips = weeks
      .map((w) => {
        const deadline = w.selection_deadline ? new Date(w.selection_deadline) : null;
        return `<button class="hf-chip" data-action="goto" data-view="menu" data-week-id="${esc(w.week_id)}">
            ${esc(L.fmtDateShort(w.delivery_date))}${L.isAutoPicked(w) ? " · review" : " · pick meals"}${deadline ? ` · ${UI.countdownHtml(deadline)}` : ""}</button>`;
      })
      .join("");
    return `<div class="hf-notice tone-warn" style="margin-top:14px;align-items:center">${icon("mdi:alert-circle-outline")}
        <div class="hf-noticebody"><strong>${esc(L.plural(weeks.length, "more week"))} need${weeks.length === 1 ? "s" : ""} your picks</strong>
          <div class="hf-chiprow" style="margin-top:8px">${chips}</div></div></div>`;
  }

  // ---- pantry --------------------------------------------------------------------------------

  _pantry(next) {
    const card = this.card;
    if (!next || L.isSkipped(next)) return "";
    const entityId = card.pantryEntityFor(next);
    if (!entityId) return "";
    const st = card.pantry(entityId);
    if (!st || (!st.items && !st.loading)) {
      card.loadPantry(entityId);
      return "";
    }
    const items = st.items || [];
    if (!items.length) return "";
    const open = items.filter((i) => i.status !== "completed").length;
    return `<div class="hf-sectionhead"><div><h2 class="hf-h2">Before it arrives</h2>
        <div class="hf-sectionnote">${open ? `${esc(L.plural(open, "pantry staple"))} the box doesn't include` : "All pantry staples ticked off"}</div></div>
        ${items.length > 8 ? `<button class="hf-btn sm" data-action="pantry" data-entity="${esc(entityId)}" data-week-id="${esc(next.week_id)}">See all ${items.length}</button>` : ""}</div>
      <div class="hf-panel">${UI.pantryList(entityId, items, { limit: 8 })}</div>`;
  }

  // ---- coming up ----------------------------------------------------------------------------

  _comingUp(next) {
    const card = this.card;
    const upcoming = card.weeks.filter((w) => L.isUpcoming(w) && w !== next);
    const toggle = `<div class="hf-segment" role="group" aria-label="Schedule layout">
        <button data-action="mode" data-mode="list" aria-pressed="${this.mode === "list"}">${icon("mdi:view-grid-outline")}Weeks</button>
        <button data-action="mode" data-mode="calendar" aria-pressed="${this.mode === "calendar"}">${icon("mdi:calendar-month-outline")}Calendar</button>
      </div>`;
    const head = `<div class="hf-sectionhead"><h2 class="hf-h2">${this.mode === "calendar" ? "Delivery calendar" : "Coming up"}</h2>${toggle}</div>`;
    if (this.mode === "calendar") return head + this._calendar();
    if (!upcoming.length) {
      const status = String((card.summary && card.summary.subscription_status) || "").toLowerCase();
      const message =
        status && status !== "active"
          ? `Your subscription is ${esc(L.titleCase(status).toLowerCase())} — no boxes are scheduled.`
          : "HelloFresh hasn't published later weeks yet.";
      return `${head}<div class="hf-empty">${icon("mdi:calendar-blank-outline")}${message}</div>`;
    }
    return `${head}<div class="hf-weekcards">${upcoming.map((w) => this._weekCard(w)).join("")}</div>`;
  }

  _weekCard(week) {
    const card = this.card;
    const state = L.weekState(week);
    const skipped = L.isSkipped(week);
    const editable = L.isWeekEditable(week);
    const meals = UI.chosenMeals(week, card.box.displayMeals(week));
    const extras = card.box.displayMarket(week).size;
    const deadline = week.selection_deadline ? new Date(week.selection_deadline) : null;
    const benefit = L.weekBenefit(week);
    const meta = [];
    if (!skipped && meals.length) {
      meta.push(`<strong>${esc(L.plural(meals.length, "meal"))}</strong>`);
      if (extras) meta.push(esc(L.plural(extras, "extra")));
      const price = L.weekPriceParts(week);
      if (price) meta.push(esc(L.fmtPrice(price.amount, price.currency)));
    }
    const weekAttr = `data-week-id="${esc(week.week_id)}"`;
    const buttons = [];
    if (editable && card.hasView("menu")) {
      const label = L.isAutoPicked(week) ? "Review" : meals.length ? "Edit" : "Choose";
      buttons.push(`<button class="hf-btn sm${state === "needs" ? " primary" : ""}" data-action="goto" data-view="menu" ${weekAttr}>${label}</button>`);
    }
    if (L.canReschedule(week)) buttons.push(`<button class="hf-btn sm ghost" data-action="reschedule" ${weekAttr} ${card.busy ? "disabled" : ""}>Change day</button>`);
    if (L.canSkip(week)) {
      buttons.push(`<button class="hf-btn sm${skipped ? " primary" : " ghost"}" data-action="skip" ${weekAttr} ${card.busy ? "disabled" : ""}>${skipped ? "Unskip" : "Skip"}</button>`);
    }
    return `<div class="hf-weekcard${skipped ? " skipped" : ""}" role="button" tabindex="0" data-action="goto" data-view="menu" ${weekAttr}
        aria-label="${esc(`${L.fmtLongDate(week.delivery_date)}: ${L.stateLabel(week, state)}`)}">
        <div class="hf-weekcardhead">
          <div><div class="hf-wdate">${esc(L.fmtDate(week.delivery_date))}${L.isHolidayShifted(week) ? ` <span title="${esc(week.holiday_message || "Holiday delivery change")}">${icon("mdi:calendar-star")}</span>` : ""}</div>
            <div class="hf-wsub">${esc([L.relativeWeek(week), week.display_name].filter(Boolean).join(" · "))}</div></div>
          ${UI.statePill(week, state)}
        </div>
        ${skipped ? `<div class="hf-thumbs empty">Nothing ships this week.</div>` : UI.thumbRow(meals, { max: 4 })}
        ${benefit ? `<div>${pill(benefit.label, "ok", "mdi:ticket-percent-outline")}</div>` : ""}
        <div class="hf-weekcardfoot">
          <span class="hf-wmeta">${meta.join(" · ")}${editable && deadline ? `${meta.length ? "<br>" : ""}Edit by ${esc(L.fmtDateTime(deadline))} · ${UI.countdownHtml(deadline)}` : ""}</span>
          <span class="hf-actions">${buttons.join("")}</span>
        </div>
      </div>`;
  }

  // ---- calendar (the classic Schedule card's month view) -------------------------------------

  _shownMonth() {
    if (this.calMonth) return this.calMonth;
    const now = new Date();
    return new Date(now.getFullYear(), now.getMonth(), 1);
  }

  _calendar() {
    const card = this.card;
    const weeks = card.weeks;
    const shown = this._shownMonth();
    const year = shown.getFullYear();
    const month = shown.getMonth();
    const byDay = L.weeksByDay(weeks);
    const today = new Date();
    const todayKey = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime();
    const dows = [...Array(7)]
      .map((_, i) => `<span class="hf-caldow">${esc(new Date(2023, 0, 1 + i).toLocaleDateString(undefined, { weekday: "narrow" }))}</span>`)
      .join("");
    const cells = [];
    const firstDow = new Date(year, month, 1).getDay();
    for (let i = 0; i < firstDow; i += 1) cells.push(`<span class="hf-cal-day blank"></span>`);
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    for (let day = 1; day <= daysInMonth; day += 1) {
      const key = new Date(year, month, day).getTime();
      const week = byDay.get(key);
      const isToday = key === todayKey ? " today" : "";
      if (!week) {
        cells.push(`<span class="hf-cal-day${isToday}"><span class="hf-calnum">${day}</span></span>`);
        continue;
      }
      const state = L.weekState(week);
      const selected = week.week_id === card.selectedWeekId ? " selected" : "";
      const holiday = L.isHolidayShifted(week);
      const title = `${week.display_name || week.week_id} — ${L.stateLabel(week, state)}${holiday ? ` — ${week.holiday_message || "Holiday delivery change"}` : ""}`;
      cells.push(`<button class="hf-cal-day has ${state}${isToday}${selected}" data-action="goto" data-view="menu"
          data-week-id="${esc(week.week_id)}" title="${esc(title)}" aria-label="${esc(`${L.fmtLongDate(week.delivery_date)}: ${L.stateLabel(week, state)}`)}">
          <span class="hf-calnum">${day}${holiday ? "*" : ""}</span><span class="hf-calmark ${state}"></span></button>`);
    }
    const { min, max } = L.calBounds(weeks);
    const shownKey = shown.getTime();
    const isCurrentMonth = year === today.getFullYear() && month === today.getMonth();
    const rows = L.monthWeeks(weeks, shown);
    const rollup = rows.length > 1 ? L.monthRollup(rows) : null;
    const rollupText = rollup
      ? [
          rollup.boxes ? L.plural(rollup.boxes, "box", "boxes") : "",
          rollup.skipped ? `${rollup.skipped} skipped` : "",
          rollup.priced ? L.fmtPrice(rollup.total, rollup.currency) : "",
        ]
          .filter(Boolean)
          .join(" · ")
      : "";
    return `<div class="hf-cols two">
        <div class="hf-cal">
          <div class="hf-calhead">
            <button class="hf-iconbtn" data-action="cal-shift" data-delta="-1" aria-label="Previous month" ${shownKey <= min ? "disabled" : ""}>${icon("mdi:chevron-left")}</button>
            <span class="hf-caltitle">${esc(shown.toLocaleDateString(undefined, { month: "long", year: "numeric" }))}</span>
            ${isCurrentMonth ? "" : `<button class="hf-btn sm ghost" data-action="cal-today">Today</button>`}
            <button class="hf-iconbtn" data-action="cal-shift" data-delta="1" aria-label="Next month" ${shownKey >= max ? "disabled" : ""}>${icon("mdi:chevron-right")}</button>
          </div>
          <div class="hf-calgrid">${dows}${cells.join("")}</div>
        </div>
        <div>${
          rows.length
            ? `<div class="hf-rows">${rollupText ? `<div class="hf-rollup">${esc(rollupText)}</div>` : ""}${rows.map((w) => this._row(w)).join("")}</div>`
            : `<div class="hf-empty">No deliveries in ${esc(shown.toLocaleDateString(undefined, { month: "long" }))}.</div>`
        }</div>
      </div>`;
  }

  // ---- rows (calendar month & past deliveries) -----------------------------------------------

  _row(week) {
    const card = this.card;
    const state = L.weekState(week);
    const meta = L.STATE_META[state];
    const label = L.stateLabel(week, state);
    const skipped = L.isSkipped(week);
    const past = L.isPastWeek(week);
    const parts = [];
    if (skipped) parts.push("No box this week");
    else {
      const meals = card.box.displayMeals(week).size || Number(week.meals_selected) || 0;
      const required = Number(week.meals_required) || 0;
      if (state === "needs") {
        parts.push(L.isAutoPicked(week) ? "Review HelloFresh's picks" : `Pick ${required || "your"} meals`);
      } else if (meals) {
        parts.push(`<strong>${esc(L.plural(meals, "meal"))}</strong>${required && required !== meals ? ` <span class="hf-muted">(plan ${required})</span>` : ""}`);
      }
      const market = L.marketCount(week);
      if (market) parts.push(esc(L.plural(market, "extra")));
      const price = L.weekPriceParts(week);
      if (price) parts.push(esc(L.fmtPrice(price.amount, price.currency)));
      const status = L.rowStatus(week, label);
      if (status) parts.push(esc(status));
    }
    const extra = [];
    if (past && !skipped) {
      const meals = UI.chosenMeals(week, card.box.displayMeals(week));
      if (meals.length) extra.push(UI.thumbRow(meals, { max: 5, width: 100, size: "sm" }));
    }
    if (!skipped && (week.order || week.delivered_at)) {
      const t = this._trackLine(week);
      if (t) extra.push(t);
    }
    const weekAttr = `data-week-id="${esc(week.week_id)}"`;
    const side = [];
    const benefit = L.weekBenefit(week);
    if (benefit) side.push(pill(benefit.label, "ok", "mdi:ticket-percent-outline"));
    if (L.wasPreselected(week) && label !== "Preselected") side.push(pill("Preselected", "warn"));
    side.push(`<span class="hf-pill tone-${meta.tone}">${esc(label)}</span>`);
    if (L.canReschedule(week)) side.push(`<button class="hf-btn sm ghost" data-action="reschedule" ${weekAttr} ${card.busy ? "disabled" : ""}>Change day</button>`);
    if (L.canSkip(week)) side.push(`<button class="hf-btn sm${skipped ? " primary" : " ghost"}" data-action="skip" ${weekAttr} ${card.busy ? "disabled" : ""}>${skipped ? "Unskip" : "Skip"}</button>`);
    const when = L.weekDay(week);
    return `<div class="hf-row" role="button" tabindex="0"
        data-action="goto" data-view="menu" ${weekAttr} aria-label="${esc(`${L.fmtLongDate(when)}: ${label}`)}">
        <span class="hf-rowicon tone-${meta.tone}">${icon(meta.icon)}</span>
        <div style="min-width:0">
          <div class="hf-rowtitle">${esc(L.fmtDate(when))}${L.isHolidayShifted(week) ? ` <span title="${esc(week.holiday_message || "Holiday delivery change")}">${icon("mdi:calendar-star")}</span>` : ""}
            <span class="hf-muted">${esc(week.display_name || week.week_id)}</span></div>
          ${parts.length ? `<div class="hf-rowsub">${parts.join(" · ")}</div>` : ""}
          ${extra.length ? `<div class="hf-rowextra">${extra.join("")}</div>` : ""}
        </div>
        <div class="hf-rowside">${side.join("")}</div>
      </div>`;
  }

  // The compact shipment line of a row: arrival, carrier, linked tracking number, order id and
  // the scan-history / proof-of-delivery disclosure.
  _trackLine(week) {
    const order = week.order || {};
    const parts = [];
    const arrived = L.fmtArrival(week.delivered_at);
    if (arrived) parts.push(`<span>Delivered <strong>${esc(arrived)}</strong></span>`);
    if (order.carrier) parts.push(`<span>${esc(order.carrier)}</span>`);
    if (order.tracking_number) {
      const href = L.safeUrl(order.tracking_url);
      const num = esc(order.tracking_number);
      parts.push(href ? `<a href="${href}" target="_blank" rel="noopener">${num}</a>` : `<span>${num}</span>`);
    }
    if (order.order_id) parts.push(`<span>Order ${esc(order.order_id)}</span>`);
    const events = Array.isArray(order.tracking_events) ? order.tracking_events : [];
    const open = this.historyOpen === week.week_id;
    if (events.length) {
      parts.push(`<button class="hf-link" data-action="toggle-history" data-week-id="${esc(week.week_id)}" aria-expanded="${open}">${open ? "Hide history" : `History (${events.length})`}</button>`);
    }
    if (!parts.length) return "";
    const history = open
      ? `<ul class="hf-history" aria-label="Tracking history">${events
          .map((e) => `<li><span class="hf-when">${esc(L.fmtArrival(e && e.time) || "—")}</span>${esc(L.sentenceCase((e && (e.detail || e.status)) || ""))}</li>`)
          .join("")}</ul>`
      : "";
    const photos = L.deliveryPhotos(week);
    const pod =
      photos.length || order.delivery_signed_by
        ? `<div class="hf-pod">${photos
            .map((url) => `<a href="${url}" target="_blank" rel="noopener noreferrer" title="Open delivery photo"><img src="${url}" alt="Delivery photo" loading="lazy"></a>`)
            .join("")}${order.delivery_signed_by ? `<span>Signed by ${esc(order.delivery_signed_by)}</span>` : ""}</div>`
        : "";
    return `<div class="hf-trackline">${parts.join('<span aria-hidden="true">·</span>')}</div>${history}${pod}`;
  }

  // ---- past deliveries ------------------------------------------------------------------------

  _past() {
    const card = this.card;
    const past = card.weeks.filter((w) => L.isPastWeek(w)).reverse();
    if (!past.length) return "";
    const shown = past.slice(0, this.pastShown);
    return `<div class="hf-sectionhead"><h2 class="hf-h2">Recent deliveries</h2>
        <span class="hf-sectionnote">${esc(L.plural(past.length, "week"))} of history</span></div>
      <div class="hf-rows">${shown.map((w) => this._row(w)).join("")}</div>
      ${past.length > shown.length ? `<div class="hf-more-row"><button class="hf-btn" data-action="more-past">Show more</button></div>` : ""}`;
  }

  // ---- events ---------------------------------------------------------------------------------

  onClick(ev, el) {
    if (!el) return;
    const card = this.card;
    const action = el.getAttribute("data-action");
    const week = card.weekById(el.getAttribute("data-week-id"));
    switch (action) {
      case "skip":
        card.confirmSkip(week);
        break;
      case "reschedule":
        card.openReschedule(week);
        break;
      case "pantry":
        card.openPantry(el.getAttribute("data-entity"), week || L.nextBoxWeek(card.weeks).week);
        break;
      case "recipe":
        card.openRecipe(el.getAttribute("data-recipe-id"));
        break;
      case "toggle-history": {
        const id = el.getAttribute("data-week-id");
        this.historyOpen = this.historyOpen === id ? null : id;
        card.renderView();
        break;
      }
      case "mode":
        this.mode = el.getAttribute("data-mode") === "calendar" ? "calendar" : "list";
        L.storageSet(L.STORAGE_KEYS.scheduleMode, this.mode);
        card.renderView();
        break;
      case "cal-shift": {
        const shown = this._shownMonth();
        const target = new Date(shown.getFullYear(), shown.getMonth() + (Number(el.getAttribute("data-delta")) || 0), 1);
        const { min, max } = L.calBounds(card.weeks);
        if (target.getTime() >= min && target.getTime() <= max) {
          this.calMonth = target;
          card.renderView();
        }
        break;
      }
      case "cal-today":
        this.calMonth = null;
        card.renderView();
        break;
      case "more-past":
        this.pastShown += PAST_STEP;
        card.renderView();
        break;
      default:
        break;
    }
  }
}
