/*
 * HelloFresh card — shared UI fragments
 * -------------------------------------
 * Small HTML builders used by more than one view of the unified card (state pills, meal
 * thumbnails, the delivery tracker, deadline countdowns, search fields, steppers, sheet headers),
 * and the few DOM steps the views share (putting focus back on a control, swapping one tile).
 * The builders return strings, like the rest of the card, and escape every payload-derived value
 * on the way in.
 */

const UI_VERSION = new URL(import.meta.url).searchParams.get("v") || "unknown";
const L = await import(
  new URL(`./hellofresh-card-logic.js?v=${encodeURIComponent(UI_VERSION)}`, import.meta.url).href
);

const { esc, t, ht, html } = L;

// A Material Design icon through Home Assistant's own <ha-icon>. Every caller passes a literal
// from the card source; the name is escaped all the same, so one that ever passed payload data
// could not write markup.
export function icon(name) {
  return `<ha-icon icon="${esc(name)}"></ha-icon>`;
}

// The pill colours the stylesheet has (.tone-*). A tone goes into a class name, so anything else
// (a status string passed straight through, say) reads as "muted" instead of becoming markup.
const PILL_TONES = new Set(["muted", "ok", "warn", "info", "danger"]);

export function pill(text, tone = "muted", iconName = null, title = "") {
  return `<span class="hf-pill tone-${PILL_TONES.has(tone) ? tone : "muted"}"${title ? ` title="${esc(title)}"` : ""}>${
    iconName ? icon(iconName) : ""
  }${esc(text)}</span>`;
}

// The week's state as a coloured pill ("Editable", "Preselected", "On the way", …).
export function statePill(week, state = L.weekState(week)) {
  const meta = L.STATE_META[state];
  const title = L.stateKey(week, state) === "preselected" ? t("state.preselected.hint") : "";
  return pill(L.stateLabel(week, state), meta.tone, meta.icon, title);
}

// A live countdown the card refreshes once a minute without re-rendering (see the card's tick).
export function countdownHtml(deadline) {
  const tone = L.deadlineTone(deadline);
  const cls = tone === "urgent" ? "hf-urgent" : "hf-muted";
  return `<span class="${cls}" data-countdown="${esc(deadline.toISOString())}">${esc(
    L.countdown(deadline)
  )}</span>`;
}

// The chosen meals of a week (collapsed duplicates merged), with quantities, in menu order.
export function chosenMeals(week, selection) {
  const out = [];
  const seen = new Set();
  for (const r of (week && week.recipes) || []) {
    const key = L.selKey(r);
    if (!selection.has(key) || seen.has(key)) continue;
    seen.add(key);
    out.push({ recipe: r, qty: selection.get(key) });
  }
  return out;
}

// ---- week strip ----------------------------------------------------------------------------
// The strip shows a page of weeks (as many as fit) and its arrows turn a page at a time. A strip
// that has just appeared, or whose view was just switched to, starts at the chosen week; after
// that it goes to the chosen week only when the choice changes to a week out of view. Either way
// it shows the chosen week beside the week before. Choosing a week on screen moves nothing, and
// a strip the user turned away from the chosen week stays where it is.

const stripChips = (strip) => [...strip.querySelectorAll(".hf-weekchip")];

// A page: as many whole chips as fit inside the strip's padding. The chips share one width.
function stripPage(strip) {
  const chip = strip.querySelector(".hf-weekchip");
  if (!chip) return 1;
  const style = getComputedStyle(strip);
  const room = strip.clientWidth - (parseFloat(style.paddingLeft) || 0) - (parseFloat(style.paddingRight) || 0);
  const gap = parseFloat(getComputedStyle(chip.parentElement).columnGap) || 0;
  return Math.max(1, Math.floor((room + gap + 0.5) / (chip.getBoundingClientRect().width + gap)));
}
const reducedMotion = () => Boolean(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);

// Measured against the strip itself: a chip's offsetLeft is relative to the nearest positioned
// ancestor, which is the card, not the scroller.
function chipShowing(strip, chip) {
  const s = strip.getBoundingClientRect();
  const c = chip.getBoundingClientRect();
  return c.left >= s.left - 1 && c.right <= s.right + 1;
}

// The first chip wholly in view.
function firstShowing(strip) {
  const left = strip.getBoundingClientRect().left;
  const index = stripChips(strip).findIndex((chip) => chip.getBoundingClientRect().left >= left - 1);
  return Math.max(0, index);
}

// Scroll so the page starts at chip `index` (kept within the weeks there are).
function showStripFrom(strip, index, smooth) {
  const chips = stripChips(strip);
  if (!chips.length) return;
  const chip = chips[Math.min(Math.max(0, index), Math.max(0, chips.length - stripPage(strip)))];
  const pad = parseFloat(getComputedStyle(strip).paddingLeft) || 0;
  const left = chip.getBoundingClientRect().left - strip.getBoundingClientRect().left + strip.scrollLeft - pad;
  strip.scrollTo({
    left: Math.min(Math.max(0, left), strip.scrollWidth - strip.clientWidth),
    behavior: smooth && !reducedMotion() ? "smooth" : "instant",
  });
}

function updateStripArrows(strip) {
  const bar = strip.closest(".hf-weekbar");
  if (!bar) return;
  const [earlier, later] = bar.querySelectorAll('[data-action="strip-page"]');
  if (earlier) earlier.disabled = strip.scrollLeft <= 1;
  if (later) later.disabled = strip.scrollLeft >= strip.scrollWidth - strip.clientWidth - 1;
}

// After a render. `entered`: the view was just switched to.
export function syncStrip(root, view, week, { entered = false } = {}) {
  const strip = root.querySelector(".hf-weekstrip");
  if (!strip) return;
  const chip = strip.querySelector('[aria-pressed="true"]');
  // No position carried over from the last render: the strip starts at the oldest week.
  const fresh = !strip.hasAttribute("data-scroll-restored");
  const changed = Boolean(week) && view._stripWeek !== week.week_id;
  if (week) view._stripWeek = week.week_id;
  // A new strip starts at the oldest week, which says nothing about where it should be, so it
  // moves even when the chosen week is somewhere on that first page.
  if (chip && (fresh || entered || (changed && !chipShowing(strip, chip)))) {
    showStripFrom(strip, stripChips(strip).indexOf(chip) - 1, !(fresh || entered));
  }
  updateStripArrows(strip);
  strip.addEventListener("scroll", () => updateStripArrows(strip), { passive: true });
}

// An arrow beside the strip: the page before (-1) or after (1).
export function pageStrip(arrow, direction) {
  const bar = arrow.closest(".hf-weekbar");
  const strip = bar && bar.querySelector(".hf-weekstrip");
  if (strip) showStripFrom(strip, firstShowing(strip) + direction * stripPage(strip), true);
}

// A row of meal thumbnails ("+N" when more), or an empty-state line.
export function thumbRow(meals, { max = 4, width = 120, empty = t("meals.none_chosen"), size = "" } = {}) {
  if (!meals.length) return `<div class="hf-thumbs empty">${esc(empty)}</div>`;
  const shown = meals.slice(0, max);
  const extra = meals.length - shown.length;
  const imgs = shown
    .map(({ recipe }) => {
      const src = L.resizedImage(recipe.image_url, width);
      return src
        ? `<img class="hf-thumb" loading="lazy" src="${esc(src)}" alt="${esc(recipe.name)}" title="${esc(recipe.name)}">`
        : `<span class="hf-thumb" title="${esc(recipe.name)}"></span>`;
    })
    .join("");
  // `size` is a class name: only the one the stylesheet has (.hf-thumbs.sm).
  return `<div class="hf-thumbs${size === "sm" ? " sm" : ""}">${imgs}${extra > 0 ? `<span class="hf-thumb more">+${extra}</span>` : ""}</div>`;
}

// Delivery progress for a shipped/delivered box: a four-step bar, the carrier's latest step,
// the tracking link, an expandable scan history, and proof of delivery when the carrier gave
// one. Empty string when there is nothing to track yet.
export function trackingBlock(week, { historyOpen = false } = {}) {
  const step = L.trackingStep(week);
  if (step < 0) return "";
  const order = (week && week.order) || {};
  const steps = L.TRACK_STEPS.map((key, i) => {
    const cls = i < step || (i === step && step === L.TRACK_STEPS.length - 1) ? "done" : i === step ? "current" : "";
    return `<div class="hf-step ${cls}"><div class="hf-stepbar"></div><div class="hf-steplabel">${ht(`tracking.step.${key}`)}</div></div>`;
  }).join("");

  const parts = [];
  const arrived = L.fmtArrival(week.delivered_at);
  if (arrived) parts.push(`<span>${ht("tracking.delivered_at", { when: html(`<strong>${esc(arrived)}</strong>`) })}</span>`);
  else {
    const status = L.statusWithDetail(order.tracking_status || order.status || week.status, order.tracking_status_detail);
    if (status) parts.push(`<strong>${esc(status)}</strong>`);
  }
  if (!arrived && order.estimated_delivery) {
    parts.push(`<span>${ht("tracking.estimated", { date: L.fmtDate(order.estimated_delivery) })}</span>`);
  }
  if (order.carrier) parts.push(`<span>${esc(order.carrier)}</span>`);
  if (order.tracking_number) {
    const href = L.safeUrl(order.tracking_url);
    const num = esc(order.tracking_number);
    parts.push(href ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${num}</a>` : `<span>${num}</span>`);
  }
  const events = Array.isArray(order.tracking_events) ? order.tracking_events : [];
  if (events.length) {
    parts.push(
      `<button class="hf-link" data-action="toggle-history" data-week-id="${esc(week.week_id)}"
        aria-expanded="${historyOpen}">${historyOpen ? ht("tracking.hide_history") : ht("tracking.history", { count: events.length })}</button>`
    );
  }
  const history =
    historyOpen && events.length
      ? `<ul class="hf-history" aria-label="${ht("tracking.history_label")}">${events
          .map((event) => {
            const when = L.fmtArrival(event && event.time) || "—";
            const what = L.detailLabel((event && (event.detail || event.status)) || "");
            return `<li><span class="hf-when">${esc(when)}</span>${esc(what)}</li>`;
          })
          .join("")}</ul>`
      : "";
  const photos = deliveryPhotoLinks(week);
  const signed = order.delivery_signed_by;
  const pod =
    photos.length || signed
      ? `<div class="hf-pod">${photos.join("")}${signed ? `<span>${ht("tracking.signed_by", { name: signed })}</span>` : ""}</div>`
      : "";
  return `<div class="hf-tracker">
      <div class="hf-steps" aria-label="${ht("tracking.progress")}">${steps}</div>
      ${parts.length ? `<div class="hf-trackline">${parts.join('<span aria-hidden="true">·</span>')}</div>` : ""}
      ${history}${pod}
    </div>`;
}

// Label/value cells describing a week's order (status, carrier, tracking, delivered, total, id).
export function orderCells(week, account) {
  const order = week.order;
  const cells = [];
  if (order) {
    const status = order.tracking_status || order.status;
    if (status) cells.push([t("order.status"), esc(L.statusLabel(status))]);
    if (order.carrier) cells.push([t("order.carrier"), esc(order.carrier)]);
    if (order.tracking_number) {
      const href = L.safeUrl(order.tracking_url);
      const num = esc(order.tracking_number);
      cells.push([t("order.tracking"), href ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${num}</a>` : num]);
    }
    if (L.isDelivered(week)) {
      const when = week.delivered_at || week.delivery_date;
      if (when) cells.push([t("order.delivered"), esc(L.fmtDate(when))]);
    }
  }
  const total = L.boxTotal(week, account);
  if (total) cells.push([total.label, esc(L.fmtPrice(total.amount, total.currency))]);
  if (order && order.order_id) cells.push([t("order.order"), esc(order.order_id)]);
  return cells;
}

export function orderStrip(week, account) {
  const cells = orderCells(week, account);
  if (!cells.length) return "";
  return `<div class="hf-orderstrip">${cells
    .map(([k, v]) => `<div><div class="hf-k">${esc(k)}</div><div class="hf-v">${v}</div></div>`)
    .join("")}</div>`;
}

// One pantry checklist (a prep-list to-do entity's items).
export function pantryList(entityId, items, { limit = 0 } = {}) {
  const open = items.filter((i) => i.status !== "completed");
  const done = items.filter((i) => i.status === "completed");
  const ordered = [...open, ...done];
  const shown = limit > 0 ? ordered.slice(0, limit) : ordered;
  return `<div class="hf-pantry">${shown
    .map((item) => {
      const isDone = item.status === "completed";
      // The amount is split off the name so it can follow it in a quieter, abbreviated form.
      const { name, amount, short } = L.pantryParts(item.summary);
      return `<button class="hf-pantryitem${isDone ? " done" : ""}" data-action="pantry-toggle"
          data-entity="${esc(entityId)}" data-uid="${esc(item.uid)}" aria-pressed="${isDone}">
          <span class="hf-check">${isDone ? icon("mdi:check") : ""}</span>
          <span class="hf-pantrytext"><span class="hf-pantryname">${esc(name)}</span>${
            amount ? `<span class="hf-pantryamt"${short !== amount ? ` title="${esc(amount)}"` : ""}>${esc(short)}</span>` : ""
          }</span>
        </button>`;
    })
    .join("")}</div>`;
}

// ---- fragments and small behaviours the views share ---------------------------------------
// One copy of what Menu, Market, Recipes, Week, Overview and Account each used to spell out.

// Emphasis inside a sentence built by ht(): the text is escaped here, only the tag passes.
export function strong(text) {
  return html(`<strong>${esc(text)}</strong>`);
}

// "N extras" is the sum of the quantities everywhere it is said (two of one add-on are two).
export function extrasCount(selection) {
  let total = 0;
  for (const qty of selection.values()) total += Number(qty) || 0;
  return total;
}

// A search field. Its input node stays in the page while the user types (the views redraw only
// their results), so the clear button is always there and merely hidden while the field is empty.
export function searchBox(key, value, placeholder, { label = placeholder, wide = false } = {}) {
  return `<label class="hf-search"${wide ? ' style="max-width:none"' : ""}><span class="sr-only">${esc(label)}</span>${icon("mdi:magnify")}
      <input type="search" data-focus-key="${esc(key)}" data-input="${esc(key)}" value="${esc(value)}"
        placeholder="${esc(placeholder)}" spellcheck="false" autocomplete="off">
      <button class="hf-iconbtn hf-clear" data-action="clear-search" aria-label="${ht("common.clear_search")}"${value ? "" : " hidden"}>${icon("mdi:close")}</button>
    </label>`;
}

// A keystroke in a view's search field: keep the text on the view, show or hide the clear
// button, and run `apply` once the typing pauses. Returns the input, or null when the event
// came from somewhere else.
export function searchTyped(view, ev, key, apply, wait = 160) {
  const input = ev.target && ev.target.closest ? ev.target.closest(`[data-input='${key}']`) : null;
  if (!input) return null;
  view.query = input.value;
  syncSearchBox(input);
  clearTimeout(view._searchTimer);
  view._searchTimer = setTimeout(apply, wait);
  return input;
}

// Bring a search field in the page in line with the view's text without replacing its input
// (the clear button empties it this way, keeping focus in the field).
export function syncSearchBox(input, value = input ? input.value : "") {
  if (!input) return;
  if (input.value !== value) input.value = value;
  const clear = input.parentElement && input.parentElement.querySelector(".hf-clear");
  if (clear) clear.hidden = !value;
}

// A sheet's header: its title, an optional line under it, and the close button (`close` names
// it, "Close" unless the sheet says what closes). `extra` is markup the caller built (a pill);
// the texts are escaped here.
export function sheetHead(title, sub = "", extra = "", { close = t("common.close") } = {}) {
  return `<div class="hf-sheethead"><div class="hf-sheettitle"><h2>${esc(title)}</h2>
      ${sub ? `<div class="hf-sheetsub">${esc(sub)}</div>` : ""}</div>${extra}
      <button class="hf-iconbtn" data-close-sheet aria-label="${esc(close)}">${icon("mdi:close")}</button></div>`;
}

// A payment HelloFresh couldn't collect (L.paymentProblem): the boxes it is about, by delivery
// date and amount, and the way to the account's HelloFresh site to put it right. `site` is that
// site's address; without one (or with anything but an https URL) only the words say it.
//
// It is part of the section it heads, not news arriving while you look, so it carries no live
// role: as one it would be read out again every time the Overview or Account is opened.
export function paymentBanner(problem, site = "") {
  if (!problem) return "";
  const boxes = problem.boxes.map((box) => (box.amount ? t("banner.payment_box", { date: box.date, amount: box.amount }) : box.date));
  const text = boxes.length
    ? ht("banner.payment_boxes", { count: boxes.length, boxes: L.listText(boxes) })
    : ht("banner.payment_any");
  const href = L.safeUrl(site, { httpsOnly: true });
  const fix = href
    ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${ht("banner.payment_fix")}</a>`
    : `<span>${ht("banner.payment_fix")}</span>`;
  return `<div class="hf-notice tone-warn hf-paymentnotice">${icon("mdi:credit-card-remove-outline")}
      <div class="hf-noticebody">${text} ${fix}</div></div>`;
}

// A view or panel that couldn't load: the message and a button that tries again.
export function loadError(message, action, { primary = false } = {}) {
  return `<div class="hf-empty">${icon("mdi:cloud-alert-outline")}${esc(message)}
      <div class="hf-actions" style="justify-content:center;margin-top:12px">
      <button class="hf-btn${primary ? " primary" : ""}" data-action="${esc(action)}">${ht("common.try_again")}</button></div></div>`;
}

// A − N + stepper. `attrs` is the data-attribute markup both buttons carry (its values escaped
// by the caller). Each button has a focus key, so the keyboard stays on it when its tile or
// panel is redrawn; `dec` and `inc` are {label, disabled, icon}.
export function stepper({ label, attrs, focusKey, qty, unit = "", dec, inc, wide = false }) {
  const button = (side, delta, spec, fallbackIcon) =>
    `<button ${attrs} data-delta="${delta}" data-focus-key="${esc(`${focusKey}:${side}`)}" aria-label="${esc(spec.label)}" ${
      spec.disabled ? "disabled" : ""
    }>${icon(spec.icon || fallbackIcon)}</button>`;
  return `<span class="hf-stepper${wide ? " wide" : ""}" role="group" aria-label="${esc(label)}">
      ${button("dec", -1, dec, "mdi:minus")}
      <span class="hf-qty">${Number(qty) || 0}${unit ? ` <span class="hf-unit">${esc(unit)}</span>` : ""}</span>
      ${button("inc", 1, inc, "mdi:plus")}
    </span>`;
}

// Put keyboard focus on the first of these controls (by focus key) that is in the page and
// enabled: the one just used, else what took its place (+ Add turns into a stepper and back).
export function focusKeyed(root, keys) {
  if (!root) return false;
  for (const key of keys) {
    const el = root.querySelector(`[data-focus-key="${CSS.escape(key)}"]`);
    if (el && !el.disabled) {
      el.focus({ preventScroll: true });
      return true;
    }
  }
  return false;
}

// Swap one rendered element for fresh markup (a single tile, a heart), leaving everything
// around it — images already loaded, scroll, the search field — untouched.
export function replaceNode(node, markup) {
  const tpl = document.createElement("template");
  tpl.innerHTML = String(markup).trim();
  const fresh = tpl.content.firstElementChild;
  if (!fresh) return null;
  node.replaceWith(fresh);
  return fresh;
}

// "Make changes by Thu, Oct 1, 2:59 AM · 1d 14h left": the week header's line, or the next
// box's block on the Overview (`hero`). The countdown keeps ticking without a re-render.
export function deadlineLine(deadline, { hero = false } = {}) {
  const urgent = L.deadlineTone(deadline) === "urgent" ? " urgent" : "";
  const text = `${ht("week.make_changes_by", { date: strong(L.fmtDateTime(deadline)) })} · ${countdownHtml(deadline)}`;
  return hero
    ? `<div class="hf-deadline${urgent}">${icon("mdi:timer-outline")}
        <div>${text}</div></div>`
    : `<div class="hf-weekdeadline${urgent}">${icon("mdi:timer-outline")}${text}</div>`;
}

// A box HelloFresh moved (L.revisedDelivery): "Delayed — now arriving Tuesday, October 6" as a
// notice (the next box, a week's header) or as a short line on a week card or row; "" for a box
// that wasn't moved.
const REVISED_ICONS = { delayed: "mdi:truck-alert-outline", early: "mdi:truck-fast-outline" };

export function revisedNotice(week) {
  const revised = L.revisedDelivery(week);
  if (!revised) return "";
  return `<div class="hf-notice ${revised.kind === "delayed" ? "tone-warn" : "tone-info"} hf-revisednotice">${icon(REVISED_ICONS[revised.kind])}
      <div class="hf-noticebody">${L.revisedDeliveryText(week, { markup: true })}</div></div>`;
}

export function revisedLine(week) {
  const revised = L.revisedDelivery(week);
  if (!revised) return "";
  return `<span class="hf-revised ${revised.kind}">${icon(REVISED_ICONS[revised.kind])}${esc(L.revisedDeliveryText(week, { long: false }))}</span>`;
}

// The button that opens a week's pantry list, with how many staples are still to tick off; ""
// when the integration has no prep list for that week.
export function pantryButton(card, week, { small = false, label = t("week.pantry") } = {}) {
  const entityId = card.pantryEntityFor(week);
  if (!entityId) return "";
  const state = card.entityState(entityId);
  const count = state && Number(state.state);
  return `<button class="hf-btn${small ? " sm" : ""}" data-action="pantry" data-entity="${esc(entityId)}" data-week-id="${esc(week.week_id)}">
      ${icon("mdi:basket-outline")}${esc(label)}${Number.isFinite(count) && count > 0 ? ` · ${count}` : ""}</button>`;
}

// Proof-of-delivery photos as links to the full picture. They load from whatever host the
// carrier names, so only https ones are shown (L.deliveryPhotos: plain http would be mixed
// content on an https Home Assistant) and the browser is told to send that host no referrer.
export function deliveryPhotoLinks(week, { title = t("tracking.open_photo") } = {}) {
  const photos = L.deliveryPhotos(week);
  return photos.map(
    (url, i) =>
      `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer" title="${esc(title)}"><img src="${esc(url)}" alt="${
        photos.length > 1 ? ht("tracking.photo_n", { number: i + 1 }) : ht("tracking.photo")
      }" loading="lazy" referrerpolicy="no-referrer"></a>`
  );
}

// A skipped week's Unskip is offered until its deadline, and that deadline passing changes
// nothing the card's minute tick watches (the week was not editable before or after). So the
// view showing such a week re-renders itself when the soonest of those deadlines goes by.
export function watchSkipCutoff(card, view) {
  clearTimeout(view._cutoffTimer);
  view._cutoffTimer = null;
  let soonest = Infinity;
  for (const week of card.weeks || []) {
    if (!L.isSkipped(week) || !L.canSkip(week)) continue;
    const deadline = week.selection_deadline ? Date.parse(week.selection_deadline) : NaN;
    if (Number.isFinite(deadline)) soonest = Math.min(soonest, deadline);
  }
  const wait = soonest - Date.now();
  // Only a cutoff within the day gets a timer; the card refreshes long before a later one.
  if (!(wait > 0 && wait < 86400000)) return;
  view._cutoffTimer = setTimeout(() => {
    view._cutoffTimer = null;
    if (card.isConnected !== false) card.renderView();
  }, wait + 1000);
}
