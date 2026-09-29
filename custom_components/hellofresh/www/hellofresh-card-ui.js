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

const { esc } = L;

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
  const label = L.stateLabel(week, state);
  const title =
    label === "Preselected"
      ? "HelloFresh picked these meals for you — review and adjust before the deadline."
      : "";
  return pill(label, meta.tone, meta.icon, title);
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

// A row of meal thumbnails ("+N" when more), or an empty-state line.
export function thumbRow(meals, { max = 4, width = 120, empty = "No meals chosen yet", size = "" } = {}) {
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
  const steps = L.TRACK_STEPS.map((label, i) => {
    const cls = i < step || (i === step && step === L.TRACK_STEPS.length - 1) ? "done" : i === step ? "current" : "";
    return `<div class="hf-step ${cls}"><div class="hf-stepbar"></div><div class="hf-steplabel">${esc(label)}</div></div>`;
  }).join("");

  const parts = [];
  const arrived = L.fmtArrival(week.delivered_at);
  if (arrived) parts.push(`<span>Delivered <strong>${esc(arrived)}</strong></span>`);
  else {
    const status = L.statusWithDetail(order.tracking_status || order.status || week.status, order.tracking_status_detail);
    if (status) parts.push(`<strong>${esc(status)}</strong>`);
  }
  if (!arrived && order.estimated_delivery) {
    parts.push(`<span>Estimated ${esc(L.fmtDate(order.estimated_delivery))}</span>`);
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
        aria-expanded="${historyOpen}">${historyOpen ? "Hide history" : `History (${events.length})`}</button>`
    );
  }
  const history =
    historyOpen && events.length
      ? `<ul class="hf-history" aria-label="Tracking history">${events
          .map((event) => {
            const when = L.fmtArrival(event && event.time) || "—";
            const what = L.sentenceCase((event && (event.detail || event.status)) || "");
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
              `<a href="${url}" target="_blank" rel="noopener noreferrer" title="Open delivery photo"><img src="${url}" alt="Delivery photo${photos.length > 1 ? ` ${i + 1}` : ""}" loading="lazy"></a>`
          )
          .join("")}${signed ? `<span>Signed by ${esc(signed)}</span>` : ""}</div>`
      : "";
  return `<div class="hf-tracker">
      <div class="hf-steps" aria-label="Delivery progress">${steps}</div>
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
    if (status) cells.push(["Status", esc(L.titleCase(status))]);
    if (order.carrier) cells.push(["Carrier", esc(order.carrier)]);
    if (order.tracking_number) {
      const href = L.safeUrl(order.tracking_url);
      const num = esc(order.tracking_number);
      cells.push(["Tracking", href ? `<a href="${href}" target="_blank" rel="noopener">${num}</a>` : num]);
    }
    if (L.isDelivered(week)) {
      const when = week.delivered_at || week.delivery_date;
      if (when) cells.push(["Delivered", esc(L.fmtDate(when))]);
    }
  }
  const total = L.boxTotal(week, account);
  if (total) cells.push([total.label, esc(L.fmtPrice(total.amount, total.currency))]);
  if (order && order.order_id) cells.push(["Order", esc(order.order_id)]);
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
      // Summaries read "Butter — 2 tablespoon" (todo.py); split the amount off so it can sit
      // quietly beside the name.
      const summary = String(item.summary || "");
      const cut = summary.indexOf(" — ");
      const name = cut >= 0 ? summary.slice(0, cut) : summary;
      const amount = cut >= 0 ? summary.slice(cut + 3) : "";
      return `<button class="hf-pantryitem${isDone ? " done" : ""}" data-action="pantry-toggle"
          data-entity="${esc(entityId)}" data-uid="${esc(item.uid)}" aria-pressed="${isDone}">
          <span class="hf-check">${isDone ? icon("mdi:check") : ""}</span>
          <span class="hf-pantrytext">${esc(name)}</span>
          ${amount ? `<span class="hf-pantryamt">${esc(amount)}</span>` : ""}
        </button>`;
    })
    .join("")}</div>`;
}
