/*
 * HelloFresh shared card helpers
 * ------------------------------
 * Small, pure helpers plus the cross-card sync protocol, shared by every HelloFresh Lovelace
 * card. This module exists because hand-copying these into seven card files produced a steady
 * stream of bugs whose only symptom was that the cards quietly DISAGREED with each other:
 *
 *   * `resizedImage` was a one-line stub in two cards, so `image_width` silently did nothing and
 *     every tile downloaded the full-size hero JPEG.
 *   * `_titleCase` was missing `.toLowerCase()` in one card, so the same delivery read
 *     "DELIVERED" there and "Delivered" elsewhere.
 *   * `_isEditable` omitted the `allowed_actions.mealSwap` check in one card, offering edits on
 *     weeks HelloFresh had already locked.
 *   * A cross-card event was dispatched with an empty `detail`, so every listener dropped it on
 *     multi-account setups.
 *
 * None of those threw. Centralizing them turns "the copies drifted" from a silent runtime
 * behaviour difference into something that cannot happen.
 *
 * ---------------------------------------------------------------------------------------------
 * IMPORTANT — how host cards must import this
 *
 * Cards load it with a *dynamic* import so the integration's `?v=` cache-bust propagates here
 * (a static "./hellofresh-shared.js" specifier is never version-stamped by Lovelace, so a
 * browser would keep serving a stale copy after an upgrade), and they `await` that import at
 * MODULE TOP LEVEL, before the card class is defined:
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

// ---- escaping / URL safety --------------------------------------------------------------

// Escape a value for interpolation into card HTML. Every payload-derived `${…}` must go through
// this: card markup is built as template strings, so an unescaped recipe name is an injection.
export function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[c]));
}

// An href that is safe to place in the DOM: plain http(s) only, then escaped. Returns "" for
// anything else, so `javascript:`/`data:` URLs from a payload never reach an anchor.
export function safeUrl(value) {
  const raw = String(value ?? "").trim();
  if (!/^https?:\/\//i.test(raw)) return "";
  return esc(raw);
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
  if (!url || !/^https?:\/\//i.test(String(url))) return "";
  const raw = String(url);
  if (!width) return raw;
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

// "today" / "yesterday" / "in 3 days" / "next week" — how far off a week's delivery is.
export function relativeWeek(week) {
  if (!week || !week.delivery_date) return "";
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const d = parseLocalDate(week.delivery_date);
  d.setHours(0, 0, 0, 0);
  const days = Math.round((d - today) / 86400000);
  if (days === 0) return "today";
  if (days < 0) return days === -1 ? "yesterday" : `${-days} days ago`;
  if (days < 7) return `in ${days} days`;
  const weeks = Math.round(days / 7);
  return weeks === 1 ? "next week" : `in ${weeks} weeks`;
}

// Format an ISO date for display. Guards Invalid Date explicitly: toLocaleDateString on one
// returns the literal string "Invalid Date" *without* throwing, so a try/catch never fires.
export function fmtDate(iso, options) {
  if (!iso) return "—";
  try {
    const d = parseLocalDate(iso);
    if (Number.isNaN(d.getTime())) return "—";
    return d.toLocaleDateString(
      undefined,
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

// Render an amount in its currency, falling back to a plain 2-dp figure WITH the currency code
// when Intl rejects it — dropping the code entirely would make an unknown currency look like USD.
export function fmtPrice(amount, currency) {
  const num = Number(amount);
  if (!Number.isFinite(num)) return String(amount);
  try {
    return num.toLocaleString(undefined, { style: "currency", currency: currency || "USD" });
  } catch (_e) {
    return `${num.toFixed(2)} ${currency || ""}`.trim();
  }
}

// ---- week state -------------------------------------------------------------------------

// Whether HelloFresh will still accept a change to this week.
//
// Must match `HelloFreshWeek.is_editable` in models.py, whose docstring states the backend and
// the cards are kept in lockstep. Absent `allowed_actions` reads as LOCKED (the safe direction):
// a delivered week carries none, and defaulting to editable offered ± steppers and a save the
// server would reject.
export function isEditable(week) {
  if (!week) return false;
  const actions = week.allowed_actions || {};
  if (actions.mealSwap === false) return false;
  if (week.is_skipped) return false;
  const deadline = week.selection_deadline ? Date.parse(week.selection_deadline) : null;
  if (deadline && deadline < Date.now()) return false;
  return Boolean(actions.mealSwap);
}

// A week whose delivery date is before today. Undated weeks are NOT past (they are unscheduled).
export function isPast(week) {
  if (!week || !week.delivery_date) return false;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return parseLocalDate(week.delivery_date).getTime() < today.getTime();
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
// of waiting for their next interval refresh. `source` lets a card ignore its own broadcast.
export function broadcastDataChanged(config, source) {
  window.dispatchEvent(
    new CustomEvent(DATA_CHANGED_EVENT, {
      detail: { accountKey: accountKey(config), ...(source ? { source } : {}) },
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
