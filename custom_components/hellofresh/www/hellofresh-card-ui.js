/*
 * HelloFresh card — shared UI fragments
 * -------------------------------------
 * Small HTML builders used by more than one view of the unified card (state pills, meal
 * thumbnails, the delivery tracker, deadline countdowns). They return strings, like the rest of
 * the card, and escape every payload-derived value on the way in.
 */

const UI_VERSION = new URL(import.meta.url).searchParams.get("v") || "unknown";
const L = await import(
  new URL(`./hellofresh-card-logic.js?v=${encodeURIComponent(UI_VERSION)}`, import.meta.url).href
);

const { esc, t, ht, html } = L;

// A Material Design icon through Home Assistant's own <ha-icon>. Names are literals from the
// card source, never payload data.
export function icon(name) {
  return `<ha-icon icon="${name}"></ha-icon>`;
}

export function pill(text, tone = "muted", iconName = null, title = "") {
  return `<span class="hf-pill tone-${tone}"${title ? ` title="${esc(title)}"` : ""}>${
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
  return `<div class="hf-thumbs${size ? ` ${size}` : ""}">${imgs}${extra > 0 ? `<span class="hf-thumb more">+${extra}</span>` : ""}</div>`;
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
    parts.push(href ? `<a href="${href}" target="_blank" rel="noopener">${num}</a>` : `<span>${num}</span>`);
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
  const photos = L.deliveryPhotos(week);
  const signed = order.delivery_signed_by;
  const pod =
    photos.length || signed
      ? `<div class="hf-pod">${photos
          .map(
            (url, i) =>
              `<a href="${url}" target="_blank" rel="noopener noreferrer" title="${ht("tracking.open_photo")}"><img src="${url}" alt="${
                photos.length > 1 ? ht("tracking.photo_n", { number: i + 1 }) : ht("tracking.photo")
              }" loading="lazy"></a>`
          )
          .join("")}${signed ? `<span>${ht("tracking.signed_by", { name: signed })}</span>` : ""}</div>`
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
      cells.push([t("order.tracking"), href ? `<a href="${href}" target="_blank" rel="noopener">${num}</a>` : num]);
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
      // Summaries read "Butter — 2 tablespoon (tbsp)" (todo.py); the amount is split off so it
      // can follow the name in a quieter, abbreviated form ("2 tbsp").
      const summary = String(item.summary || "");
      const cut = summary.indexOf(" — ");
      const name = cut >= 0 ? summary.slice(0, cut) : summary;
      const amount = cut >= 0 ? summary.slice(cut + 3) : "";
      const short = L.shortAmount(amount);
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
