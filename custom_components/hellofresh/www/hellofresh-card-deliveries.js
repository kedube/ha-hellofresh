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
 *
 * A shipped or past box's row opens its delivery details — the carrier's delivery photo and
 * signature, the scan-by-scan timeline, what was in the box and what it cost. That replaces the
 * old example dashboard's Activity view (a logbook of the delivery-events entity), with the
 * carrier's own timestamps instead of whenever a poll happened to notice a change.
 */

const OVERVIEW_VERSION = new URL(import.meta.url).searchParams.get("v") || "unknown";
const stamp = encodeURIComponent(OVERVIEW_VERSION);
const [L, UI] = await Promise.all([
  import(new URL(`./hellofresh-card-logic.js?v=${stamp}`, import.meta.url).href),
  import(new URL(`./hellofresh-card-ui.js?v=${stamp}`, import.meta.url).href),
]);

const { esc, t, ht, html } = L;
const { icon, pill } = UI;
const strong = (text) => html(`<strong>${esc(text)}</strong>`);

const PAST_STEP = 6;

export class OverviewView {
  constructor(card) {
    this.card = card;
    this.mode = L.storageGet(L.STORAGE_KEYS.scheduleMode) === "calendar" ? "calendar" : "list";
    this.calMonth = null;
    this.historyOpen = null; // the next box's inline scan history
    this.pastShown = 4;
  }

  render() {
    const card = this.card;
    const placeholder = card.weeksPlaceholder();
    if (placeholder) return placeholder;
    const weeks = card.weeks;
    if (!weeks.length) {
      return `<div class="hf-empty">${icon("mdi:truck-outline")}${ht("overview.no_deliveries")}</div>`;
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
    const eyebrow = t(
      !upcoming ? "overview.last_box" : days === 0 ? "overview.arriving_today" : days === 1 ? "overview.arriving_tomorrow" : "overview.next_box"
    );
    const deadline = week.selection_deadline ? new Date(week.selection_deadline) : null;

    const meta = [];
    const rel = L.relativeWeek(week);
    if (rel && upcoming) meta.push(`<span>${icon("mdi:calendar-outline")}${esc(rel)}</span>`);
    const slot = week.slot_label || (week.order && week.order.slot_label);
    if (slot) meta.push(`<span>${icon("mdi:clock-outline")}${esc(slot)}</span>`);
    if (week.display_name) meta.push(`<span>${icon("mdi:package-variant-closed")}${esc(week.display_name)}</span>`);
    if (L.isHolidayShifted(week)) {
      meta.push(`<span title="${esc(week.holiday_message || t("week.holiday_change"))}">${icon("mdi:calendar-star")}${ht("week.holiday_schedule")}</span>`);
    }

    const blocks = [];
    if (editable && deadline) {
      const tone = L.deadlineTone(deadline);
      blocks.push(`<div class="hf-deadline${tone === "urgent" ? " urgent" : ""}">${icon("mdi:timer-outline")}
        <div>${ht("week.make_changes_by", { date: strong(L.fmtDateTime(deadline)) })} · ${UI.countdownHtml(deadline)}</div></div>`);
    }
    if (skipped) {
      blocks.push(`<div class="hf-notice tone-muted">${icon("mdi:calendar-remove-outline")}<div class="hf-noticebody">
        <strong>${ht("overview.skipped_title")}</strong> ${ht(L.canSkip(week) ? "overview.skipped_body_unskip" : "overview.skipped_body")}</div></div>`);
    } else if (L.isAutoPicked(week)) {
      blocks.push(`<div class="hf-notice tone-warn">${icon("mdi:auto-fix")}<div class="hf-noticebody">
        <strong>${ht("week.autopicked_title")}</strong> ${ht("overview.autopicked_body")}</div></div>`);
    }
    if (card.box.dirty(week)) {
      blocks.push(`<div class="hf-notice tone-info">${icon("mdi:content-save-alert-outline")}<div class="hf-noticebody">
        ${ht("overview.unsaved")}</div>
        <button class="hf-btn sm primary" data-action="goto" data-view="menu" data-week-id="${esc(week.week_id)}">${ht("overview.finish")}</button></div>`);
    }
    // The live tracker (Netherlands, Germany) is the finer view of the same journey, so it stands in for
    // the carrier's tracker rather than stacking a second progress bar under it.
    const live = this._live(week, { hero: true });
    const tracking = live || UI.trackingBlock(week, { historyOpen: this.historyOpen === week.week_id });
    if (tracking) blocks.push(tracking);

    const money = [];
    const total = L.boxTotal(week, card.account);
    if (total) money.push(`<span>${ht(total.estimate ? "overview.price_plan" : "overview.price_total", { price: strong(L.fmtPrice(total.amount, total.currency)) })}</span>`);
    const account = card.account || {};
    if (upcoming && !skipped) {
      // The subscription's next charge belongs to this box only while it is still open; once
      // the deadline passes this box is paid for and the date is the NEXT box's.
      if (account.next_payment_date && editable) {
        money.push(`<span>${icon("mdi:credit-card-outline")}${ht("overview.charged", { date: L.fmtDate(account.next_payment_date) })}</span>`);
      }
      const discount = L.nextBoxDiscount(account);
      if (discount) money.push(`<span>${icon("mdi:tag-outline")}${ht("overview.discount", { amount: discount })}</span>`);
      const voucher = L.nextBoxVoucher(week, account);
      if (voucher) {
        money.push(`<span title="${voucher.code ? ht("voucher.code", { code: voucher.code }) : ""}">${icon("mdi:ticket-percent-outline")}${esc(voucher.label)}${voucher.note ? ` <span class="hf-muted">(${esc(voucher.note)})</span>` : ""}</span>`);
      }
      if (account.next_box_coupon) money.push(`<span>${icon("mdi:ticket-outline")}${ht("overview.coupon", { code: account.next_box_coupon })}</span>`);
    }

    // Primary actions are buttons; the rarer week changes (another day, skipping) sit in a quiet
    // link row underneath, the way hellofresh.com tucks them under the box.
    const actions = [];
    const links = [];
    const weekAttr = `data-week-id="${esc(week.week_id)}"`;
    if (editable) {
      const label = ht(L.isAutoPicked(week) ? "overview.review_meals" : meals.length ? "overview.edit_meals" : "overview.choose_meals");
      if (card.hasView("menu")) actions.push(`<button class="hf-btn primary" data-action="goto" data-view="menu" ${weekAttr}>${icon("mdi:silverware-fork-knife")}${label}</button>`);
      if (card.hasView("market") && (week.market_items || []).length) {
        actions.push(`<button class="hf-btn" data-action="goto" data-view="market" ${weekAttr}>${icon("mdi:storefront-outline")}${ht("overview.add_extras")}</button>`);
      }
    } else if (!skipped && card.hasView("menu")) {
      actions.push(`<button class="hf-btn" data-action="goto" data-view="menu" ${weekAttr}>${icon("mdi:silverware-fork-knife")}${ht("overview.view_meals")}</button>`);
    }
    const pantryId = card.pantryEntityFor(week);
    if (pantryId && !skipped) {
      const s = card.entityState(pantryId);
      const count = s && Number(s.state);
      actions.push(`<button class="hf-btn" data-action="pantry" data-entity="${esc(pantryId)}" ${weekAttr}>${icon("mdi:basket-outline")}${ht("pantry.list")}${Number.isFinite(count) && count > 0 ? ` · ${count}` : ""}</button>`);
    }
    if (L.canSkip(week) && skipped) {
      actions.push(`<button class="hf-btn primary" data-action="skip" ${weekAttr} ${card.busy ? "disabled" : ""}>${icon("mdi:restore")}${ht("week.unskip_this_week")}</button>`);
    }
    if (L.canReschedule(week)) {
      links.push(`<button class="hf-link quiet" data-action="reschedule" ${weekAttr} ${card.busy ? "disabled" : ""}>${icon("mdi:calendar-edit")}${ht("reschedule.title")}</button>`);
    }
    if (L.canSkip(week) && !skipped) {
      links.push(`<button class="hf-link quiet" data-action="skip" ${weekAttr} ${card.busy ? "disabled" : ""}>${icon("mdi:debug-step-over")}${ht("week.skip_this_week")}</button>`);
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

  // The live delivery in the Netherlands or Germany (HelloFresh's own vans): phase, ETA, stops left before
  // yours, the driver, HelloFresh's message and a link to its live map. It belongs to the week
  // whose order carries the tracked link; the next box also shows it when no week does (the link
  // can reach the tracker before the order data catches up).
  _live(week, { hero = false } = {}) {
    const card = this.card;
    const live = card.liveTracking();
    if (!live) return "";
    const owner = (card.weeks || []).find((w) => L.liveTrackingFor(live, w));
    if (owner ? owner !== week : !hero) return "";
    const steps = L.LIVE_STEPS.map((key, i) => {
      const cls = live.step < 0 ? "" : i < live.step ? " done" : i === live.step ? " now" : "";
      return `<li class="hf-livestep${cls}"><span class="hf-livebar"></span>${ht(`live.step.${key}`)}</li>`;
    }).join("");
    const facts = [];
    if (live.eta && live.step < 2) {
      const until = L.untilText(live.eta);
      facts.push(`<span>${icon("mdi:clock-outline")}${ht("live.arrives", { time: strong(L.fmtTime(live.eta)) })}${until ? ` · ${esc(until)}` : ""}</span>`);
    }
    if (live.stops != null && live.step === 1) {
      facts.push(`<span>${icon("mdi:map-marker-path")}${
        live.stops === 0
          ? `<strong>${ht("live.youre_next")}</strong>`
          : ht("live.stops_before", { stops: strong(t("live.stops", { count: live.stops })) })
      }</span>`);
    }
    if (live.driver) facts.push(`<span>${icon("mdi:account-outline")}${ht("live.driver", { name: strong(live.driver) })}</span>`);
    if (live.window) facts.push(`<span>${icon("mdi:calendar-clock-outline")}${ht("live.window", { window: live.window })}</span>`);
    return `<div class="hf-live${live.tone ? ` tone-${live.tone}` : ""}" aria-live="polite">
        <div class="hf-livehead">
          <span class="hf-livebadge"><span class="hf-livedot"></span>${ht("live.badge")}</span>
          <span class="hf-livephase">${esc(live.label)}</span>
          ${live.mapUrl ? `<a class="hf-livemap" href="${live.mapUrl}" target="_blank" rel="noopener noreferrer">${icon("mdi:map-marker-radius-outline")}${ht("live.map")}</a>` : ""}
        </div>
        ${live.step >= 0 ? `<ol class="hf-livesteps" aria-label="${ht("tracking.progress")}">${steps}</ol>` : ""}
        ${facts.length ? `<div class="hf-livefacts">${facts.join("")}</div>` : ""}
        ${live.message ? `<div class="hf-livemsg">${icon("mdi:message-text-outline")}<span>${esc(live.message)}</span></div>` : ""}
        ${live.updated ? `<div class="hf-livefoot">${ht("live.updated", { ago: L.agoText(live.updated) })}</div>` : ""}
      </div>`;
  }

  _heroMeals(week, meals, editable, skipped) {
    const card = this.card;
    if (skipped) {
      return `<div class="hf-heroempty">${icon("mdi:calendar-remove-outline")}<div>${ht("overview.no_box")}</div></div>`;
    }
    const extras = [...card.box.displayMarket(week).entries()];
    const extrasLine = extras.length
      ? `<div class="hf-small hf-muted" style="grid-column:1/-1">${icon("mdi:plus-circle-outline")} ${ht("overview.extras_line", {
          extras: t("meals.extras", { count: extras.reduce((a, [, q]) => a + q, 0) }),
          names: `${extras
            .map(([id]) => ((week.market_items || []).find((i) => i.item_id === id) || {}).name)
            .filter(Boolean)
            .slice(0, 3)
            .join(", ")}${extras.length > 3 ? "…" : ""}`,
        })}</div>`
      : "";
    if (!meals.length) {
      return `<div class="hf-heroempty">${icon("mdi:silverware-variant")}<div>${ht("overview.no_meals")}</div>
        ${editable && card.hasView("menu") ? `<button class="hf-btn primary sm" data-action="goto" data-view="menu" data-week-id="${esc(week.week_id)}">${ht("overview.pick_meals")}</button>` : ""}</div>`;
    }
    // Columns follow the count so a 3-meal box doesn't leave a hole in a 2x2 grid.
    const shown = meals.slice(0, meals.length > 4 ? 5 : 4);
    const tiles = shown
      .map(({ recipe, qty }) => {
        const img = L.resizedImage(recipe.image_url, 360);
        return `<div class="hf-heromeal" role="button" tabindex="0" data-action="recipe" data-recipe-id="${esc(recipe.recipe_id)}"
            aria-label="${ht("recipe.open", { name: recipe.name })}">
            <div class="hf-heromealwrap">${img ? `<img loading="lazy" src="${esc(img)}" alt="">` : `<div class="hf-noimg" style="aspect-ratio:16/10"></div>`}
              ${qty > 1 ? `<span class="hf-overlaypill hf-qtytag">${qty}×</span>` : ""}</div>
            <div class="hf-heromealname">${esc(recipe.name)}</div>
            ${recipe.variation_title ? `<div class="hf-heromealopt">${esc(recipe.variation_title)}</div>` : ""}
          </div>`;
      })
      .join("");
    const more =
      meals.length > shown.length
        ? `<button class="hf-more" data-action="goto" data-view="menu" data-week-id="${esc(week.week_id)}">${ht("overview.more", { count: meals.length - shown.length })}</button>`
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
            ${esc(L.fmtDateShort(w.delivery_date))} · ${ht(L.isAutoPicked(w) ? "overview.chip_review" : "overview.chip_pick")}${deadline ? ` · ${UI.countdownHtml(deadline)}` : ""}</button>`;
      })
      .join("");
    return `<div class="hf-notice tone-warn" style="margin-top:14px;align-items:center">${icon("mdi:alert-circle-outline")}
        <div class="hf-noticebody"><strong>${ht("overview.more_weeks_need_picks", { count: weeks.length })}</strong>
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
    return `<div class="hf-sectionhead"><div><h2 class="hf-h2">${ht("overview.before_it_arrives")}</h2>
        <div class="hf-sectionnote">${open ? ht("overview.pantry_note", { count: open }) : ht("overview.pantry_done")}</div></div>
        ${items.length > 8 ? `<button class="hf-btn sm" data-action="pantry" data-entity="${esc(entityId)}" data-week-id="${esc(next.week_id)}">${ht("overview.see_all", { count: items.length })}</button>` : ""}</div>
      <div class="hf-panel">${UI.pantryList(entityId, items, { limit: 8 })}</div>`;
  }

  // ---- coming up ----------------------------------------------------------------------------

  _comingUp(next) {
    const card = this.card;
    const upcoming = card.weeks.filter((w) => L.isUpcoming(w) && w !== next);
    const toggle = `<div class="hf-segment" role="group" aria-label="${ht("overview.layout")}">
        <button data-action="mode" data-mode="list" aria-pressed="${this.mode === "list"}">${icon("mdi:view-grid-outline")}${ht("overview.weeks")}</button>
        <button data-action="mode" data-mode="calendar" aria-pressed="${this.mode === "calendar"}">${icon("mdi:calendar-month-outline")}${ht("overview.calendar")}</button>
      </div>`;
    const head = `<div class="hf-sectionhead"><h2 class="hf-h2">${ht(this.mode === "calendar" ? "overview.delivery_calendar" : "overview.coming_up")}</h2>${toggle}</div>`;
    if (this.mode === "calendar") return head + this._calendar();
    if (!upcoming.length) {
      const status = String((card.summary && card.summary.subscription_status) || "").toLowerCase();
      const message =
        status && status !== "active"
          ? ht("overview.subscription_inactive", { status: L.statusLabel(status).toLowerCase() })
          : ht("overview.no_later_weeks");
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
      meta.push(`<strong>${ht("meals.count", { count: meals.length })}</strong>`);
      if (extras) meta.push(ht("meals.extras", { count: extras }));
      const price = L.weekPriceParts(week);
      if (price) meta.push(esc(L.fmtPrice(price.amount, price.currency)));
    }
    const weekAttr = `data-week-id="${esc(week.week_id)}"`;
    const buttons = [];
    if (editable && card.hasView("menu")) {
      const label = ht(L.isAutoPicked(week) ? "week.review" : meals.length ? "week.edit" : "week.choose");
      buttons.push(`<button class="hf-btn sm${state === "needs" ? " primary" : ""}" data-action="goto" data-view="menu" ${weekAttr}>${label}</button>`);
    }
    if (L.canReschedule(week)) buttons.push(`<button class="hf-btn sm ghost" data-action="reschedule" ${weekAttr} ${card.busy ? "disabled" : ""}>${ht("week.change_day")}</button>`);
    if (L.canSkip(week)) {
      buttons.push(`<button class="hf-btn sm${skipped ? " primary" : " ghost"}" data-action="skip" ${weekAttr} ${card.busy ? "disabled" : ""}>${ht(skipped ? "week.unskip" : "week.skip")}</button>`);
    }
    return `<div class="hf-weekcard${skipped ? " skipped" : ""}" role="button" tabindex="0" data-action="goto" data-view="menu" ${weekAttr}
        aria-label="${esc(`${L.fmtLongDate(week.delivery_date)}: ${L.stateLabel(week, state)}`)}">
        <div class="hf-weekcardhead">
          <div><div class="hf-wdate">${esc(L.fmtDate(week.delivery_date))}${L.isHolidayShifted(week) ? ` <span title="${esc(week.holiday_message || t("week.holiday_change"))}">${icon("mdi:calendar-star")}</span>` : ""}</div>
            <div class="hf-wsub">${esc([L.relativeWeek(week), week.display_name].filter(Boolean).join(" · "))}</div></div>
          ${UI.statePill(week, state)}
        </div>
        ${skipped ? `<div class="hf-thumbs empty">${ht("week.nothing_ships")}</div>` : UI.thumbRow(meals, { max: 4 })}
        ${benefit ? `<div>${pill(benefit.label, "ok", "mdi:ticket-percent-outline")}</div>` : ""}
        <div class="hf-weekcardfoot">
          <span class="hf-wmeta">${meta.join(" · ")}${editable && deadline ? `${meta.length ? "<br>" : ""}${ht("week.edit_by", { date: L.fmtDateTime(deadline) })} · ${UI.countdownHtml(deadline)}` : ""}</span>
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

  // A month of rounded day tiles, in whole weeks starting on Home Assistant's first day of the
  // week (the neighbouring months' days faded).
  // A delivery day wears its state's colour, icon and short label, and opens what its row
  // beside the calendar opens: the delivery details once a box ships, the week's menu before.
  _calendar() {
    const card = this.card;
    const weeks = card.weeks;
    const shown = this._shownMonth();
    const year = shown.getFullYear();
    const month = shown.getMonth();
    const byDay = L.weeksByDay(weeks);
    const today = new Date();
    const todayKey = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime();
    const isCurrentMonth = year === today.getFullYear() && month === today.getMonth();
    const first = L.firstWeekday(card.hass);
    const dows = [...Array(7)]
      .map((_, i) => {
        const dow = (first + i) % 7;
        const day = new Date(2023, 0, 1 + dow); // 1 Jan 2023 was a Sunday
        const cls = `${dow === 0 || dow === 6 ? " weekend" : ""}${isCurrentMonth && dow === today.getDay() ? " today" : ""}`;
        const name = (weekday) => esc(day.toLocaleDateString(L.I18n.dateLocale(), { weekday }));
        return `<span class="hf-caldow${cls}" title="${name("long")}">
            <span class="hf-dowshort">${name("short")}</span>
            <span class="hf-dowletter">${name("narrow")}</span></span>`;
      })
      .join("");
    const lead = (new Date(year, month, 1).getDay() - first + 7) % 7;
    const span = Math.ceil((lead + new Date(year, month + 1, 0).getDate()) / 7) * 7;
    const states = new Set();
    const cells = [];
    for (let i = 0; i < span; i += 1) {
      cells.push(this._calDay(new Date(year, month, 1 - lead + i), month, todayKey, byDay, states));
    }
    const legend = ["delivered", "shipping", "ready", "needs", "locked", "skipped"]
      .filter((s) => states.has(s))
      .map((s) => pill(L.STATE_META[s].short, L.STATE_META[s].tone, L.STATE_META[s].icon));
    if (isCurrentMonth) legend.push(`<span class="hf-legendtoday"><span class="hf-todaydot"></span>${ht("calendar.today")}</span>`);
    const { min, max } = L.calBounds(weeks);
    const shownKey = shown.getTime();
    const rows = L.monthWeeks(weeks, shown);
    const rollup = rows.length > 1 ? L.monthRollup(rows) : null;
    const rollupText = rollup
      ? [
          rollup.boxes ? t("calendar.boxes", { count: rollup.boxes }) : "",
          rollup.skipped ? t("calendar.skipped", { count: rollup.skipped }) : "",
          rollup.priced ? L.fmtPrice(rollup.total, rollup.currency) : "",
        ]
          .filter(Boolean)
          .join(" · ")
      : "";
    return `<div class="hf-cols two">
        <div class="hf-cal">
          <div class="hf-calhead">
            <span class="hf-calnav"><button class="hf-iconbtn" data-action="cal-shift" data-delta="-1" aria-label="${ht("calendar.previous_month")}" ${shownKey <= min ? "disabled" : ""}>${icon("mdi:chevron-left")}</button></span>
            <span class="hf-caltitle">${esc(shown.toLocaleDateString(L.I18n.dateLocale(), { month: "long", year: "numeric" }))}</span>
            <span class="hf-calnav end">${isCurrentMonth ? "" : `<button class="hf-btn sm ghost" data-action="cal-today">${ht("calendar.today")}</button>`}
              <button class="hf-iconbtn" data-action="cal-shift" data-delta="1" aria-label="${ht("calendar.next_month")}" ${shownKey >= max ? "disabled" : ""}>${icon("mdi:chevron-right")}</button></span>
          </div>
          <div class="hf-caldows">${dows}</div>
          <div class="hf-calgrid">${cells.join("")}</div>
          ${legend.length ? `<div class="hf-callegend">${legend.join("")}</div>` : ""}
        </div>
        <div>${
          rows.length
            ? `<div class="hf-rows">${rollupText ? `<div class="hf-rollup">${esc(rollupText)}</div>` : ""}${rows.map((w) => this._row(w)).join("")}</div>`
            : `<div class="hf-empty">${ht("calendar.no_deliveries", { month: shown.toLocaleDateString(L.I18n.dateLocale(), { month: "long" }) })}</div>`
        }</div>
      </div>`;
  }

  _calDay(date, month, todayKey, byDay, states) {
    const key = date.getTime();
    const inMonth = date.getMonth() === month;
    const cls = ["hf-cal-day"];
    if (!inMonth) cls.push("other");
    if (date.getDay() === 0 || date.getDay() === 6) cls.push("weekend");
    if (key === todayKey) cls.push("today");
    else if (key < todayKey) cls.push("past");
    const current = key === todayKey ? ' aria-current="date"' : "";
    const num = `<span class="hf-calnum">${date.getDate()}</span>`;
    const week = byDay.get(key);
    if (!week) {
      return `<span class="${cls.join(" ")}"${current}${inMonth ? "" : ' aria-hidden="true"'}><span class="hf-caltop">${num}</span></span>`;
    }
    const state = L.weekState(week);
    const meta = L.STATE_META[state];
    const label = L.stateLabel(week, state);
    if (inMonth) states.add(state);
    cls.push("has", state, `tone-${meta.tone}`);
    if (week.week_id === this.card.selectedWeekId) cls.push("selected");
    const holiday = L.isHolidayShifted(week);
    const title = `${week.display_name || week.week_id} — ${label}${holiday ? ` — ${week.holiday_message || t("week.holiday_change")}` : ""}`;
    const weekAttr = `data-week-id="${esc(week.week_id)}"`;
    const open = this._hasDelivery(week) ? `data-action="delivery" ${weekAttr}` : `data-action="goto" data-view="menu" ${weekAttr}`;
    return `<button class="${cls.join(" ")}" ${open}${current} title="${esc(title)}"
        aria-label="${esc(`${L.fmtLongDate(L.weekDay(week))}: ${label}${holiday ? ` (${t("calendar.holiday_note")})` : ""}`)}">
        <span class="hf-caltop">${num}${holiday ? `<span class="hf-calholiday" aria-hidden="true">${icon("mdi:calendar-star")}</span>` : ""}</span>
        <span class="hf-calstate" aria-hidden="true">${icon(meta.icon)}<span class="hf-calstatetext">${ht(`state.${L.stateKey(week, state)}.short`)}</span></span>
      </button>`;
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
    if (skipped) parts.push(ht("week.no_box"));
    else {
      const meals = card.box.displayMeals(week).size || Number(week.meals_selected) || 0;
      const required = Number(week.meals_required) || 0;
      if (state === "needs") {
        parts.push(L.isAutoPicked(week) ? ht("week.review_picks") : required ? ht("week.pick_n_meals", { count: required }) : ht("week.pick_your_meals"));
      } else if (meals) {
        parts.push(`<strong>${ht("meals.count", { count: meals })}</strong>${required && required !== meals ? ` <span class="hf-muted">(${ht("week.plan_n", { count: required })})</span>` : ""}`);
      }
      const market = L.marketCount(week);
      if (market) parts.push(ht("meals.extras", { count: market }));
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
    if (L.wasPreselected(week) && L.stateKey(week, state) !== "preselected") side.push(pill(t("state.preselected.label"), "warn"));
    side.push(`<span class="hf-pill tone-${meta.tone}">${esc(label)}</span>`);
    if (L.canReschedule(week)) side.push(`<button class="hf-btn sm ghost" data-action="reschedule" ${weekAttr} ${card.busy ? "disabled" : ""}>${ht("week.change_day")}</button>`);
    if (L.canSkip(week)) side.push(`<button class="hf-btn sm${skipped ? " primary" : " ghost"}" data-action="skip" ${weekAttr} ${card.busy ? "disabled" : ""}>${ht(skipped ? "week.unskip" : "week.skip")}</button>`);
    const when = L.weekDay(week);
    const details = this._hasDelivery(week);
    if (details) side.push(`<span class="hf-rowchev" aria-hidden="true">${icon("mdi:chevron-right")}</span>`);
    const open = details
      ? `data-action="delivery" ${weekAttr} aria-label="${ht("delivery.row_label", { date: L.fmtLongDate(when), state: label })}"`
      : `data-action="goto" data-view="menu" ${weekAttr} aria-label="${esc(`${L.fmtLongDate(when)}: ${label}`)}"`;
    const actions = L.canReschedule(week) || L.canSkip(week);
    return `<div class="hf-row${actions ? " actions" : ""}" role="button" tabindex="0" ${open}>
        <span class="hf-rowicon tone-${meta.tone}">${icon(meta.icon)}</span>
        <div style="min-width:0">
          <div class="hf-rowtitle">${esc(L.fmtDate(when))}${L.isHolidayShifted(week) ? ` <span title="${esc(week.holiday_message || t("week.holiday_change"))}">${icon("mdi:calendar-star")}</span>` : ""}
            <span class="hf-muted">${esc(week.display_name || week.week_id)}</span></div>
          ${parts.length ? `<div class="hf-rowsub">${parts.join(" · ")}</div>` : ""}
          ${extra.length ? `<div class="hf-rowextra">${extra.join("")}</div>` : ""}
        </div>
        <div class="hf-rowside">${side.join("")}</div>
      </div>`;
  }

  // A box whose row opens its delivery details rather than its menu: anything shipped or
  // delivered, and any past box that wasn't skipped.
  _hasDelivery(week) {
    if (L.isSkipped(week)) return false;
    const order = week.order || {};
    return L.isPastWeek(week) || L.isDelivered(week) || L.isShipping(week) || Boolean(order.tracking_number);
  }

  // The compact shipment line of a row: arrival, carrier and whether the carrier left a photo.
  // The tracking number, scan history and proof of delivery are in the delivery details.
  _trackLine(week) {
    const order = week.order || {};
    const parts = [];
    const arrived = L.fmtArrival(week.delivered_at);
    if (arrived) parts.push(`<span>${ht("tracking.delivered_at", { when: strong(arrived) })}</span>`);
    if (order.carrier) parts.push(`<span>${esc(order.carrier)}</span>`);
    if (L.deliveryPhotos(week).length) parts.push(`<span class="hf-podhint">${icon("mdi:camera-outline")}${ht("tracking.photo_hint")}</span>`);
    return parts.length ? `<div class="hf-trackline">${parts.join('<span aria-hidden="true">·</span>')}</div>` : "";
  }

  // ---- delivery details ---------------------------------------------------------------------------

  _openDelivery(week) {
    const card = this.card;
    card.openSheet({
      kind: "delivery",
      narrow: true,
      label: t("delivery.label", { date: L.fmtLongDate(L.weekDay(week)) }),
      render: () => this._renderDelivery(card.weekById(week.week_id) || week),
      onClick: (_ev, el) => {
        const action = el && el.getAttribute("data-action");
        if (action === "recipe") card.openRecipe(el.getAttribute("data-recipe-id"));
        else if (action === "delivery-menu") {
          card.closeSheet();
          card.navigate("menu", { weekId: week.week_id });
        }
      },
    });
  }

  _renderDelivery(week) {
    const card = this.card;
    const order = week.order || {};
    const delivered = L.isDelivered(week);
    const when = L.weekDay(week);
    const arrived = L.fmtArrival(week.delivered_at);
    const label = L.stateLabel(week, L.weekState(week));
    const title = delivered
      ? t("delivery.delivered_title", { when: arrived || L.fmtLongDate(when) })
      : t("delivery.box_for", { date: L.fmtLongDate(when) });
    const sub = [week.display_name || week.week_id, order.order_id ? t("delivery.order", { id: order.order_id }) : ""].filter(Boolean);
    const sections = [];
    const live = this._live(week);
    if (live) sections.push(live);

    // Proof of delivery. HelloFresh passes on the carrier's photo and signer only when the
    // carrier provides them; Veho, the main US carrier, doesn't — so say so rather than leave
    // a gap that looks like a loading failure.
    const photos = L.deliveryPhotos(week);
    if (photos.length || order.delivery_signed_by) {
      sections.push(`<div class="hf-dphotos">${photos
        .map((url) => `<a href="${url}" target="_blank" rel="noopener noreferrer" title="${ht("delivery.open_full_photo")}"><img src="${url}" alt="${ht("tracking.photo")}" loading="lazy"></a>`)
        .join("")}</div>${order.delivery_signed_by ? `<div class="hf-dnote">${icon("mdi:draw")}${ht("tracking.signed_by", { name: order.delivery_signed_by })}</div>` : ""}`);
    } else if (delivered) {
      sections.push(`<div class="hf-dnote">${icon("mdi:camera-off-outline")}${
        order.carrier ? ht("delivery.no_photo_carrier", { carrier: order.carrier }) : ht("delivery.no_photo")
      }</div>`);
    }

    // The carrier's scan history, newest first.
    const events = Array.isArray(order.tracking_events) ? order.tracking_events : [];
    const track = [];
    if (order.carrier || order.tracking_number) {
      const href = L.safeUrl(order.tracking_url);
      const number = order.tracking_number
        ? href
          ? `<a href="${href}" target="_blank" rel="noopener noreferrer">${esc(order.tracking_number)}</a>`
          : `<span>${esc(order.tracking_number)}</span>`
        : "";
      track.push(`<div class="hf-dcarrier">${icon("mdi:truck-outline")}<span>${esc(order.carrier || t("order.carrier"))}</span>${number}</div>`);
    }
    if (events.length) {
      track.push(`<ol class="hf-timeline" aria-label="${ht("tracking.history_label")}">${events
        .map((e) => `<li><span class="hf-when">${esc(L.fmtArrival(e && e.time) || "—")}</span><span>${esc(
          L.detailLabel((e && (e.detail || e.status)) || "")
        )}</span></li>`)
        .join("")}</ol>`);
    } else if (!delivered && L.isShipping(week)) {
      track.push(`<div class="hf-dnote">${esc(L.rowStatus(week, label) || t("state.shipping.short"))}</div>`);
    }
    if (track.length) sections.push(`<section class="hf-dsec"><h3>${ht("order.tracking")}</h3>${track.join("")}</section>`);

    // What was in the box.
    const meals = UI.chosenMeals(week, card.box.displayMeals(week));
    const marketItems = new Map((week.market_items || []).map((i) => [i.item_id, i]));
    const extras = [...card.box.displayMarket(week).entries()].map(([id, qty]) => [marketItems.get(id), qty]).filter(([i]) => i);
    if (meals.length || extras.length) {
      const rows = [
        ...meals.map(({ recipe, qty }) => {
          const img = L.resizedImage(recipe.image_url, 120);
          const note = [recipe.variation_title, qty > 1 ? t("meals.servings", { count: qty }) : ""].filter(Boolean).join(" · ");
          return `<button class="hf-dmeal" data-action="recipe" data-recipe-id="${esc(recipe.recipe_id)}" aria-label="${ht("recipe.open", { name: recipe.name })}">
              ${img ? `<img src="${esc(img)}" alt="" loading="lazy">` : `<span class="hf-noimg"></span>`}
              <span class="hf-dmealtext"><span>${esc(recipe.name)}</span>${note ? `<span class="hf-muted">${esc(note)}</span>` : ""}</span>
            </button>`;
        }),
        ...extras.map(([item, qty]) => {
          const img = L.resizedImage(item.image_url, 120);
          return `<div class="hf-dmeal">${img ? `<img src="${esc(img)}" alt="" loading="lazy">` : `<span class="hf-noimg"></span>`}
              <span class="hf-dmealtext"><span>${esc(item.name)}</span><span class="hf-muted">${ht("views.market")}${qty > 1 ? ` · ${qty}×` : ""}</span></span></div>`;
        }),
      ];
      sections.push(`<section class="hf-dsec"><h3>${ht("delivery.in_box")}</h3><div class="hf-dmeals">${rows.join("")}</div></section>`);
    }

    // What it cost.
    const price = L.weekPriceParts(week);
    const money = [];
    if (price) money.push(`<div class="hf-dline"><span>${ht(order.billed_total_price != null ? "delivery.charged" : "delivery.order_total")}</span><strong>${esc(L.fmtPrice(price.amount, price.currency))}</strong></div>`);
    if (Number(order.discount_amount) > 0) {
      money.push(`<div class="hf-dline"><span>${ht("money.discount")}${order.coupon_code ? ` (${esc(order.coupon_code)})` : ""}</span><span>−${esc(L.fmtPrice(order.discount_amount, price ? price.currency : order.currency))}</span></div>`);
    }
    if (money.length) sections.push(`<section class="hf-dsec"><h3>${ht("delivery.charges")}</h3>${money.join("")}</section>`);

    const menuButton = card.hasView("menu")
      ? `<button class="hf-btn ghost" data-action="delivery-menu">${icon("mdi:silverware-fork-knife")}${ht("delivery.view_menu")}</button>`
      : "";
    return `
      <div class="hf-sheethead"><div class="hf-sheettitle"><h2>${esc(title)}</h2>
        <div class="hf-sheetsub">${esc(sub.join(" · "))}</div></div>
        ${delivered ? "" : pill(label, "info")}
        <button class="hf-iconbtn" data-close-sheet aria-label="${ht("common.close")}">${icon("mdi:close")}</button></div>
      <div class="hf-sheetbody">${sections.join("") || `<div class="hf-empty">${ht("delivery.no_details")}</div>`}</div>
      ${menuButton ? `<div class="hf-sheetfoot">${menuButton}</div>` : ""}`;
  }

  // ---- past deliveries ------------------------------------------------------------------------

  _past() {
    const card = this.card;
    const past = card.weeks.filter((w) => L.isPastWeek(w)).reverse();
    if (!past.length) return "";
    const shown = past.slice(0, this.pastShown);
    return `<div class="hf-sectionhead"><h2 class="hf-h2">${ht("overview.recent_deliveries")}</h2>
        <span class="hf-sectionnote">${ht("overview.history_weeks", { count: past.length })}</span></div>
      <div class="hf-rows">${shown.map((w) => this._row(w)).join("")}</div>
      ${past.length > shown.length ? `<div class="hf-more-row"><button class="hf-btn" data-action="more-past">${ht("common.show_more")}</button></div>` : ""}`;
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
      case "delivery":
        if (week) this._openDelivery(week);
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
