/*
 * HelloFresh shared card helpers
 * ------------------------------
 * Small helpers plus the cross-card sync protocol. The HelloFresh card reaches them through
 * hellofresh-card-logic.js, which re-exports them; the recipe sheet (hellofresh-recipe-detail.js)
 * imports them directly. They started out as one copy of what had been hand-copied into the seven
 * classic cards (since removed), where the copies drifted: an image resize that silently did
 * nothing, "DELIVERED" in one card and "Delivered" in another, and a cross-card event dispatched
 * with an empty `detail` that every listener dropped on multi-account setups. None of those
 * threw. Keep one definition of each here.
 *
 * ---------------------------------------------------------------------------------------------
 * IMPORTANT — how to import this
 *
 * Importers load it with a *dynamic* import so the integration's `?v=` cache-bust propagates here
 * (a static "./hellofresh-shared.js" specifier is never version-stamped by Lovelace, so a
 * browser would keep serving a stale copy after an upgrade), and they `await` that import at
 * MODULE TOP LEVEL, before anything that renders is defined:
 *
 *     const { esc, fmtPrice } = await import(
 *       new URL(`./hellofresh-shared.js?v=${CARD_VERSION}`, import.meta.url).href
 *     );
 *
 * The top-level await is load-bearing. These helpers are called synchronously during the first
 * render, and an un-awaited dynamic import is still a Promise at that point — reading a helper
 * off it throws `TypeError: x is not a function`. Awaiting at top level keeps the `?v=` stamp
 * AND guarantees the helpers exist before anything can render.
 * ---------------------------------------------------------------------------------------------
 */

// The card text layer (hellofresh-i18n.js): dates and prices below follow Home Assistant's
// language and number format once the HelloFresh card has passed hass to it. Loaded with this
// module's own ?v= stamp, so every importer shares one copy.
const I18n = await import(
  new URL(
    `./hellofresh-i18n.js?v=${encodeURIComponent(new URL(import.meta.url).searchParams.get("v") || "unknown")}`,
    import.meta.url
  ).href
);

// ---- escaping / URL safety --------------------------------------------------------------

// Escape a value for interpolation into card HTML. Every payload-derived `${…}` must go through
// this: card markup is built as template strings, so an unescaped recipe name is an injection.
// Defined in the text layer (which this module imports, and which escapes for ht()), so there is
// one definition.
export const { esc } = I18n;

// The one URL check for anything a payload names: plain http(s) only, or "" — so a
// `javascript:`/`data:` URL never reaches an href or src. It returns the URL itself, NOT HTML:
// `esc()` it where it goes into markup, like any other value, and pass it as it is to
// setAttribute or window.open.
//
// The characters that could end an attribute or start a tag have no business raw in a URL, so
// they come back percent-encoded (as a browser would send them). That way a sink that forgot its
// esc() still cannot be broken out of.
// eslint-disable-next-line no-control-regex -- control characters are among what it must find
const URL_UNSAFE = /["'<>`\s\u0000-\u001f\u007f]/g;
export function safeUrl(value, { httpsOnly = false } = {}) {
  const raw = String(value ?? "").trim();
  if (!(httpsOnly ? /^https:\/\//i : /^https?:\/\//i).test(raw)) return "";
  return raw.replace(URL_UNSAFE, (c) => `%${c.charCodeAt(0).toString(16).toUpperCase().padStart(2, "0")}`);
}

// ---- images -----------------------------------------------------------------------------

// Insert/replace a Cloudinary width transform so grid thumbnails download small instead of
// pulling the full-size hero JPEG (~1.7 MB each, on a grid showing dozens).
//
// Must handle every URL shape HelloFresh serves, not just `/q_auto/`. Two cards once carried a
// stub that only rewrote that one form, so on the `hellofresh_s3` URLs the API actually emits
// (`f_auto,fl_lossy,h_300,q_auto,w_450/hellofresh_s3/…`) it returned the URL unchanged and
// `image_width` silently did nothing. An unrecognized shape is returned as-is, never mangled.
export function resizedImage(url, width) {
  // Only plain web URLs may reach an <img src>: an attacker-chosen scheme/host has no business
  // in the DOM even though javascript:/data: are inert there in modern browsers.
  const raw = safeUrl(url);
  if (!raw || !width) return raw;
  if (raw.includes("/hellofresh_s3/")) {
    // An existing w_NNN (comma- or slash-delimited) is retargeted rather than duplicated.
    if (/[/,]w_\d+/.test(raw)) return raw.replace(/([/,])w_\d+/, `$1w_${width}`);
    // A transform segment is already present: append the width to it.
    if (/\/(?:[a-z]{1,2}_[^/]+)\/hellofresh_s3\//.test(raw)) {
      return raw.replace("/hellofresh_s3/", `,w_${width}/hellofresh_s3/`);
    }
    // Bare `hellofresh_s3` path with no transform at all: insert a full one.
    return raw.replace("/hellofresh_s3/", `/f_auto,fl_lossy,q_auto,w_${width}/hellofresh_s3/`);
  }
  if (raw.includes("/q_auto/")) return raw.replace("/q_auto/", `/q_auto,w_${width}/`);
  return raw.replace(/\/\d+,\d+\/image\//, `/${width},0/image/`);
}

// ---- dates ------------------------------------------------------------------------------

// Parse a date value as LOCAL time.
//
// Load-bearing: `new Date("2026-08-20")` parses a bare YYYY-MM-DD as UTC midnight, which reads
// as the PREVIOUS day for any viewer west of UTC — delivery dates would render a day early.
// Full datetime strings carry their own offset and are handed to Date as-is.
export function parseLocalDate(value) {
  const m = typeof value === "string" && /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (m) return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return new Date(value);
}

// "today" / "tomorrow" / "in 3 days" / "next week" / "2 days ago" — how far off a week's
// delivery is, in the language's own words (Intl.RelativeTimeFormat).
export function relativeWeek(week) {
  if (!week || !week.delivery_date) return "";
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const d = parseLocalDate(week.delivery_date);
  d.setHours(0, 0, 0, 0);
  const days = Math.round((d - today) / 86400000);
  if (Number.isNaN(days)) return "";
  let format;
  try {
    format = new Intl.RelativeTimeFormat(I18n.dateLocale(), { numeric: "auto" });
  } catch (_e) {
    format = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  }
  if (days < 7) return format.format(days, "day");
  return format.format(Math.round(days / 7), "week");
}

// Format an ISO date for display. Guards Invalid Date explicitly: toLocaleDateString on one
// returns the literal string "Invalid Date" *without* throwing, so a try/catch never fires.
export function fmtDate(iso, options) {
  if (!iso) return "—";
  try {
    const d = parseLocalDate(iso);
    if (Number.isNaN(d.getTime())) return "—";
    return d.toLocaleDateString(
      I18n.dateLocale(),
      options || { weekday: "short", month: "short", day: "numeric" }
    );
  } catch (_e) {
    return iso || "—";
  }
}

// ---- text / money -----------------------------------------------------------------------

// Normalize an API status like "ON_THE_WAY" / "on_the_way" to "On The Way".
//
// The `.toLowerCase()` is load-bearing: HelloFresh sends SCREAMING_SNAKE, and without it this
// returns "ON THE WAY". The `|| ""` keeps a null status from rendering as the literal "Null".
export function titleCase(value) {
  return String(value || "")
    .replace(/[_-]+/g, " ")
    .toLowerCase()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

// "35 min", "1 h 5 min" (time.duration.*).
export function formatMinutes(minutes) {
  if (!minutes && minutes !== 0) return "";
  if (minutes < 60) return I18n.t("time.duration.minutes", { minutes });
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest
    ? I18n.t("time.duration.hours_minutes", { hours, minutes: rest })
    : I18n.t("time.duration.hours", { hours });
}

// Render an amount in its currency, falling back to a plain 2-dp figure WITH the currency code
// when Intl rejects it — dropping the code entirely would make an unknown currency look like USD.
// A missing amount is a dash: Number(null) is 0, which would read as a real "$0.00".
export function fmtPrice(amount, currency) {
  if (amount == null || amount === "") return "—";
  const num = Number(amount);
  if (!Number.isFinite(num)) return String(amount);
  try {
    return num.toLocaleString(I18n.numberLocale(), { style: "currency", currency: currency || "USD" });
  } catch (_e) {
    return `${num.toFixed(2)} ${currency || ""}`.trim();
  }
}

// ---- cross-card sync protocol -----------------------------------------------------------
//
// Two window events plus a localStorage key let the cards follow each other's week selection and
// re-read after a sibling writes. The whole correctness condition is EXACT agreement, so every
// piece of the wire format lives here and nowhere else.

export const WEEK_SYNC_EVENT = "hellofresh-week-selected";
export const DATA_CHANGED_EVENT = "hellofresh-data-changed";

// Which HelloFresh account a card is bound to. Listeners filter on this, so a card that omits it
// is invisible to its siblings rather than broadcasting to all of them.
export function accountKey(config) {
  return (config && config.config_entry_id) || "default";
}

export function syncStorageKey(config) {
  return `hellofresh:selected-week:${accountKey(config)}`;
}

export function loadSyncedWeekId(config) {
  try {
    return window.localStorage.getItem(syncStorageKey(config)) || null;
  } catch (_e) {
    return null; // storage unavailable (private mode) — the live event still works
  }
}

// Does an inbound event belong to this card's account? Missing keys default to "default" on both
// sides so single-account setups (no config_entry_id anywhere) still match.
export function eventMatchesAccount(detail, config) {
  return ((detail && detail.accountKey) || "default") === accountKey(config);
}

// Announce a week selection so sibling cards jump to the same week.
export function broadcastWeek(config, weekId) {
  if (!weekId) return;
  try {
    window.localStorage.setItem(syncStorageKey(config), weekId);
  } catch (_e) {
    /* storage unavailable: the live event below still reaches mounted cards */
  }
  window.dispatchEvent(
    new CustomEvent(WEEK_SYNC_EVENT, {
      detail: { weekId, accountKey: accountKey(config) },
    })
  );
}

// Announce that a write changed account data, so read-only siblings re-pull immediately instead
// of waiting for their next interval refresh. `source` lets a card ignore its own broadcast;
// `weekId`, when the write touched one week's box only, lets them re-read just that week.
export function broadcastDataChanged(config, source, weekId) {
  window.dispatchEvent(
    new CustomEvent(DATA_CHANGED_EVENT, {
      detail: { accountKey: accountKey(config), ...(source ? { source } : {}), ...(weekId ? { weekId } : {}) },
    })
  );
}

// ---- auto-refresh cadence --------------------------------------------------------------

// How long a card should wait before re-pulling its service, from the account/summary
// payload's refresh contract. Normally the integration's "Refresh interval" option (fetching
// more often would just return the coordinator's identical cached data). While
// `delivery_in_progress` is true the integration's delivery-day watch is updating the data
// every `delivery_watch_interval_minutes` instead, so the card drops to that cadence — the
// whole point of the watch is that a dashboard open on delivery day sees the box land within
// minutes, not at the next multi-hour poll. A watch interval of 0 (option off) is ignored.
export function refetchIntervalMs(contract) {
  const refresh = Number(contract && contract.refresh_interval_minutes);
  let mins = Number.isFinite(refresh) && refresh >= 1 ? refresh : 180;
  if (contract && contract.delivery_in_progress) {
    const watch = Number(contract.delivery_watch_interval_minutes);
    if (Number.isFinite(watch) && watch >= 1 && watch < mins) mins = watch;
  }
  return mins * 60000;
}

// ---- dialogs -----------------------------------------------------------------------------

// What Tab can reach inside a dialog, in order.
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), ' +
  'textarea:not([disabled]), video[controls], summary, [tabindex]:not([tabindex="-1"])';

export function focusableIn(container) {
  return [...container.querySelectorAll(FOCUSABLE)].filter(
    (el) => !el.closest("[inert]") && (el.getClientRects().length > 0 || el === container.ownerDocument.activeElement)
  );
}

// Keep Tab inside an open dialog (a keydown listener's body): from its last control Tab goes to
// its first, and Shift+Tab the other way. `active` is the focused element as the dialog's own
// root sees it (shadowRoot.activeElement), since document.activeElement stops at the card.
export function keepTabInside(ev, container, active) {
  if (ev.key !== "Tab" || !container) return;
  const stops = focusableIn(container);
  if (!stops.length) {
    ev.preventDefault();
    container.focus({ preventScroll: true });
    return;
  }
  const first = stops[0];
  const last = stops[stops.length - 1];
  const inside = active && container.contains(active);
  if (ev.shiftKey && (!inside || active === first || active === container)) {
    ev.preventDefault();
    last.focus();
  } else if (!ev.shiftKey && (!inside || active === last)) {
    ev.preventDefault();
    first.focus();
  }
}
