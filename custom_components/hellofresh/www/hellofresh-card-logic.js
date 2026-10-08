/*
 * HelloFresh card — pure logic
 * ----------------------------
 * Everything the unified `custom:hellofresh-card` decides that does not touch the DOM: which
 * delivery weeks exist and what state each is in, how a week's menu is filtered and ordered,
 * what a pending box edit writes, and how money, dates and statuses read. It is kept DOM-free
 * so it runs directly under Node (tests/test_unified_card_logic.py).
 *
 * Most of this is the logic of the classic single-purpose cards (since removed) moved, not
 * rewritten. Where the unified card deliberately differed from them, the function says so:
 *
 *   * `browsableWeeks` is the schedule card's rule (skipped future weeks stay visible so they
 *     can be unskipped) widened to count Market data, because one week list now serves the
 *     menu, the Market and the schedule at once.
 *   * A PAUSED week is treated as skipped everywhere (the schedule card's reading) — the
 *     classic planner offered meal edits on one.
 *   * `weekState` adds a "shipping" state between locked and delivered.
 *
 * Imported with the card's own ?v= cache-bust, and it pulls the shared helper module the
 * same way, so an upgrade never pairs a new card with a stale helper copy.
 */

const LOGIC_VERSION = new URL(import.meta.url).searchParams.get("v") || "unknown";

const [shared, I18n] = await Promise.all([
  import(new URL(`./hellofresh-shared.js?v=${encodeURIComponent(LOGIC_VERSION)}`, import.meta.url).href),
  import(new URL(`./hellofresh-i18n.js?v=${encodeURIComponent(LOGIC_VERSION)}`, import.meta.url).href),
]);

// The card's text (hellofresh-i18n.js), for the views: t() plain, ht() HTML, html() markup.
export const { t, ht, html, has: hasText, en: englishText } = I18n;
export { I18n };

export const {
  esc,
  safeUrl,
  resizedImage,
  parseLocalDate,
  relativeWeek,
  fmtDate,
  titleCase,
  fmtPrice,
  accountKey,
  syncStorageKey,
  loadSyncedWeekId,
  eventMatchesAccount,
  broadcastWeek,
  broadcastDataChanged,
  refetchIntervalMs,
  WEEK_SYNC_EVENT,
  DATA_CHANGED_EVENT,
} = shared;

// ---- limits ------------------------------------------------------------------------------

// Smallest box HelloFresh sells, in DISTINCT meals (the client's MIN_MEALS_PER_WEEK).
export const MIN_MEALS = 2;
// Per-meal servings stepper cap (HelloFresh's per-recipe maximum).
export const MAX_MEAL_SERVINGS = 4;
// Market add-on stepper cap when an item doesn't carry its own max_quantity.
export const MAX_MARKET_QTY = 12;

// ---- filter catalogs (carried over from the classic meal planner card) -----------------

// Protein -> accent colour for the dot on each tile. Mirrors HelloFresh's own grouping.
export const PREFERENCE_COLORS = {
  Poultry: "#f0a202",
  Beef: "#c1432f",
  Pork: "#e6789b",
  Seafood: "#2f8fc1",
  Lamb: "#8d5b3f",
  Veggie: "#4caf50",
};

export const PROTEIN_FILTERS = ["Beef", "Poultry", "Pork", "Seafood", "Lamb", "Veggie"];

// A protein chip's words. The chips' keys stay English: they are what the integration writes in
// each meal's `preference`, and what the classic planner stored.
export function proteinLabel(protein) {
  return t(`filters.protein.${String(protein).toLowerCase()}`);
}

// The website's "Main protein" slugs for the chips it also has (it has no Lamb). The menu leaves
// a few meals without a protein — W42's "2x Tofu" swap of a beef ramen — and only HelloFresh's
// filter service places those, so the chips ask it as well (proteinServerFilters).
export const PROTEIN_SERVER_SLUGS = {
  Beef: "beef",
  Poultry: "poultry",
  Pork: "pork",
  Seafood: "fish-seafood",
  Veggie: "vegetarian",
};

// A filter entry's `label` reads the card's text at the moment it's shown (filters.<group>.<key>),
// so the catalogs below stay plain data in any language.
function labelled(group, entries) {
  return entries.map((entry) =>
    Object.defineProperty(entry, "label", {
      enumerable: true,
      get() {
        return t(`filters.${group}.${entry.key}`);
      },
    })
  );
}

// The website's "Dietary preference" group. `tags` lists every spelling seen in real menu
// payloads (HelloFresh renames these between seasons); whole-string matching is what keeps
// "Contains Gluten" off the gluten-free aliases.
export const DIET_FILTERS = labelled("diet", [
  { key: "vegetarian", tags: ["vegetarian", "veggie", "vegan"] },
  {
    key: "under-650-cal",
    tags: ["under 650 calories", "calorie smart", "calorie-smart"],
    maxCalories: 650,
  },
  { key: "high-protein", tags: ["high protein"] },
  {
    key: "carb-conscious",
    tags: ["carb conscious", "carb smart", "low carb", "max-20-percent-carbs"],
  },
  {
    key: "high-fiber",
    tags: ["high fiber", "fiber filled", "fiber smart", "fiber powered"],
  },
  {
    key: "gluten-free",
    tags: ["gluten-free friendly", "gluten free friendly", "gluten-free", "gluten free"],
  },
  { key: "sodium-smart", tags: ["sodium smart", "low sodium"] },
  { key: "low-sugar", tags: ["low added sugar", "low sugar"] },
  { key: "organic-protein", tags: ["organic protein"] },
  {
    key: "glp1",
    tags: ["glp-1 support", "glp-1 friendly", "glp-1 balance"],
  },
]);

// The website's single-choice "Total cooking time" group.
export const TIME_FILTERS = labelled("time", [
  { key: "under-15-min", tags: ["under 15 minutes"], maxMinutes: 15 },
  { key: "under-20-min", tags: ["under 20 minutes"], maxMinutes: 20 },
  { key: "under-30-min", tags: ["under 30 minutes"], maxMinutes: 30 },
]);

// Single-select highlight views. "Favorites" is new in the unified card: it narrows the week
// to meals already in your cookbook, which is otherwise a hunt through hundreds of tiles.
export const HIGHLIGHT_FILTERS = labelled("highlight", [
  { key: "new" },
  { key: "bestseller" },
  { key: "cooked-before" },
  { key: "favorite" },
]);

// Resolved through HelloFresh's own filter service (hellofresh.get_menu_courses), not tags.
export const SERVER_FILTER_GROUPS = ["cuisine", "dish-type", "exclude-allergens"];

// Names for HelloFresh's Market group slugs (the Market card's table): its brands as they are,
// the rest in the card's language (market.group.<slug>).
export const MARKET_BRANDS = {
  goodchop: "GoodChop",
  petstable: "The Pets Table",
};

// View preferences keep the classic cards' localStorage keys and formats, so the filters someone
// set up there carried over to this card; renaming one would reset it. Unified-card-only
// preferences live under "hellofresh-card:".
export const STORAGE_KEYS = {
  showSelectedOnly: "hellofresh-meal-planner:show-selected-only",
  protein: "hellofresh-meal-planner:protein-filter",
  diet: "hellofresh-meal-planner:diet-filter",
  time: "hellofresh-meal-planner:time-filter",
  highlight: "hellofresh-meal-planner:highlight-filter",
  section: "hellofresh-meal-planner:menu-section",
  server: "hellofresh-meal-planner:server-filter",
  filtersExpanded: "hellofresh-meal-planner:filters-expanded",
  marketSelectedOnly: "hellofresh-market:show-selected-only",
  marketSections: "hellofresh-market:section-filter",
  scheduleMode: "hellofresh-card:schedule-mode",
  accountTab: "hellofresh-card:account-tab",
};

export function viewStorageKey(config) {
  return `hellofresh-card:view:${accountKey(config)}`;
}

// localStorage that never throws (private mode, blocked storage, previews).
export function storageGet(key, fallback = null) {
  try {
    const value = window.localStorage.getItem(key);
    return value === null ? fallback : value;
  } catch (_e) {
    return fallback;
  }
}

export function storageSet(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch (_e) {
    /* storage unavailable: keep the in-memory value for this session */
  }
}

// ---- small text helpers ---------------------------------------------------------------------

const upper = (value) => String(value || "").trim().toUpperCase();
const lower = (value) => String(value || "").trim().toLowerCase();

// "received_at_origin_facility" -> "Received at origin facility".
export function sentenceCase(value) {
  const text = String(value || "").replace(/[_-]+/g, " ").trim().toLowerCase();
  return text ? text[0].toUpperCase() + text.slice(1) : "";
}

// A HelloFresh or carrier status code ("ON_THE_WAY", "out_for_delivery") in the card's words
// (status.<code>); one it doesn't know yet reads title-cased, as before.
export function statusKey(value) {
  return String(value || "").trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
}

export function statusLabel(value) {
  const key = statusKey(value);
  return key && hasText(`status.${key}`) ? t(`status.${key}`) : titleCase(value);
}

// A carrier's scan detail ("received_at_origin_facility"): a known status in the card's words,
// else sentence case.
export function detailLabel(value) {
  const key = statusKey(value);
  return key && hasText(`status.${key}`) ? t(`status.${key}`) : sentenceCase(value);
}

// "In Transit · Received at origin facility"; the status alone when the detail adds nothing.
export function statusWithDetail(status, detail) {
  const head = statusLabel(status);
  const tail = detail && statusKey(detail) !== statusKey(status) ? detailLabel(detail) : "";
  return tail && tail.toLowerCase() !== head.toLowerCase() ? `${head} · ${tail}` : head;
}

// "35 min", "1 h 5 min" (time.duration.*).
export function formatMinutes(minutes) {
  if (!minutes && minutes !== 0) return "";
  if (minutes < 60) return t("time.duration.minutes", { minutes });
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? t("time.duration.hours_minutes", { hours, minutes: rest }) : t("time.duration.hours", { hours });
}

// ---- dates ------------------------------------------------------------------------------------

export function startOfToday(now = new Date()) {
  const d = new Date(now);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

// Whole days from today to a date value (negative = past); null when undated.
export function daysUntil(value, now = new Date()) {
  if (!value) return null;
  const d = parseLocalDate(value);
  if (Number.isNaN(d.getTime())) return null;
  d.setHours(0, 0, 0, 0);
  return Math.round((d.getTime() - startOfToday(now)) / 86400000);
}

// Compact countdown to a deadline: "2d 4h left", "5h left", "12m left", "passed".
export function countdown(when, now = Date.now()) {
  const ms = when.getTime() - now;
  if (ms <= 0) return t("time.left.passed");
  const mins = Math.floor(ms / 60000);
  const days = Math.floor(mins / 1440);
  const hours = Math.floor((mins % 1440) / 60);
  if (days > 0) return hours > 0 ? t("time.left.days_hours", { days, hours }) : t("time.left.days", { days });
  if (hours > 0) return t("time.left.hours", { hours });
  return t("time.left.minutes", { minutes: mins });
}

// "urgent" under 24 hours, "passed" once gone, "soon" otherwise.
export function deadlineTone(when, now = Date.now()) {
  const ms = when.getTime() - now;
  if (ms <= 0) return "passed";
  if (ms < 86400000) return "urgent";
  return "soon";
}

export function fmtDateShort(iso) {
  return fmtDate(iso, { month: "short", day: "numeric" });
}

export function fmtWeekday(iso) {
  return fmtDate(iso, { weekday: "short" });
}

export function fmtLongDate(iso) {
  return fmtDate(iso, { weekday: "long", month: "long", day: "numeric" });
}

// "Thu, Sep 3, 2:59 AM" for deadlines.
export function fmtDateTime(date) {
  try {
    return date.toLocaleString(I18n.dateLocale(), {
      weekday: "short",
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      ...I18n.hourOptions(),
    });
  } catch (_e) {
    return String(date);
  }
}

// Arrival stamp for a delivered box: "Aug 17, 6:53 PM" in the viewer's timezone. Only full
// datetimes (with offset) reach this, so it parses unambiguously.
export function fmtArrival(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    return d.toLocaleString(I18n.dateLocale(), {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      ...I18n.hourOptions(),
    });
  } catch (_e) {
    return "";
  }
}

// ---- week state -----------------------------------------------------------------------------

export function isPaused(week) {
  return Boolean(week) && upper(week.status) === "PAUSED";
}

// Skipped OR paused: no box ships either way.
export function isSkipped(week) {
  return Boolean(week) && (Boolean(week.is_skipped) || isPaused(week));
}

// Whether HelloFresh will still accept a change. The shared rule (allowed_actions.mealSwap,
// not skipped, deadline not passed) plus: a paused week is not editable until it is unskipped.
export function isWeekEditable(week, now = Date.now()) {
  if (!week || isSkipped(week)) return false;
  const actions = week.allowed_actions || {};
  if (actions.mealSwap === false) return false;
  const deadline = week.selection_deadline ? Date.parse(week.selection_deadline) : null;
  if (deadline && deadline < now) return false;
  return Boolean(actions.mealSwap);
}

// Delivery date strictly before today (undated weeks are unscheduled, never past).
export function isPastWeek(week, now = new Date()) {
  if (!week || !week.delivery_date) return false;
  return parseLocalDate(week.delivery_date).getTime() < startOfToday(now);
}

// Delivery date today or later (undated weeks count as upcoming).
export function isUpcoming(week, now = new Date()) {
  if (!week) return false;
  if (!week.delivery_date) return true;
  return parseLocalDate(week.delivery_date).getTime() >= startOfToday(now);
}

// Older than the menu grace window: such weeks render only what was delivered (their full
// menu is gone). A just-delivered week keeps its browsable menu, and its filters.
export function isHistoryWeek(week, graceWeeks = 2, now = new Date()) {
  if (!week || !week.delivery_date) return false;
  const graceMs = (Number.isFinite(graceWeeks) ? graceWeeks : 2) * 7 * 86400000;
  return parseLocalDate(week.delivery_date).getTime() < startOfToday(now) - graceMs;
}

export function isDelivered(week) {
  if (!week) return false;
  if (week.delivered_at) return true;
  if (upper(week.status) === "DELIVERED") return true;
  return lower(week.order && week.order.tracking_status) === "delivered";
}

export const SHIPPED_WEEK_STATUSES = new Set(["ON_THE_WAY", "SHIPPED", "IN_TRANSIT", "OUT_FOR_DELIVERY"]);
export const SHIPPED_TRACKING_STATUSES = new Set(["in_transit", "out_for_delivery", "shipped", "on_the_way"]);

// A box that has left HelloFresh but not arrived.
export function isShipping(week) {
  if (!week || isSkipped(week) || isDelivered(week)) return false;
  if (SHIPPED_WEEK_STATUSES.has(upper(week.status))) return true;
  return SHIPPED_TRACKING_STATUSES.has(lower(week.order && week.order.tracking_status));
}

// HelloFresh auto-picked this week's meals AND that is still fixable — the call to action.
export function isAutoPicked(week, now = Date.now()) {
  if (!week || isSkipped(week)) return false;
  if (week.auto_picked != null) return Boolean(week.auto_picked);
  return Boolean(week.meals_preselected) && isWeekEditable(week, now);
}

// Informational: HelloFresh preselected this week's meals (true even after the deadline).
export function wasPreselected(week) {
  return Boolean(week) && !isSkipped(week) && Boolean(week.meals_preselected);
}

// The week still needs the customer: the server's own flag when present (it accounts for
// resized boxes and paused weeks), else the classic planner's rule.
export function needsSelection(week, now = Date.now()) {
  if (!isWeekEditable(week, now)) return false;
  if (week.needs_selection != null) return Boolean(week.needs_selection);
  if (isAutoPicked(week, now)) return true;
  return savedMealSelection(week).size < MIN_MEALS;
}

// One of: skipped | delivered | shipping | needs | ready | locked.
export function weekState(week, now = Date.now()) {
  if (isSkipped(week)) return "skipped";
  if (isDelivered(week)) return "delivered";
  if (isShipping(week)) return "shipping";
  if (needsSelection(week, now)) return "needs";
  if (isWeekEditable(week, now)) return "ready";
  return "locked";
}

// Each state's colour and icon; `label` (the badge) and `short` (strips, calendar days) read the
// card's text (state.<state>.label / .short).
export const STATE_META = Object.fromEntries(
  Object.entries({
    ready: { icon: "mdi:pencil-outline", tone: "ok" },
    needs: { icon: "mdi:alert-circle-outline", tone: "warn" },
    shipping: { icon: "mdi:truck-fast-outline", tone: "info" },
    delivered: { icon: "mdi:check-circle-outline", tone: "ok" },
    skipped: { icon: "mdi:close-circle-outline", tone: "muted" },
    locked: { icon: "mdi:lock-outline", tone: "muted" },
  }).map(([state, meta]) => [
    state,
    Object.defineProperties(meta, {
      label: { enumerable: true, get: () => t(`state.${state}.label`) },
      short: { enumerable: true, get: () => t(`state.${state}.short`) },
    }),
  ])
);

// Which badge a week wears: its state, except that a week which "needs" attention only because
// HelloFresh picked for it says so ("preselected") instead of the misleading "Needs picking".
export function stateKey(week, state = weekState(week)) {
  return state === "needs" && isAutoPicked(week) ? "preselected" : state;
}

export function stateLabel(week, state = weekState(week)) {
  return t(`state.${stateKey(week, state)}.label`);
}

// Skip/Unskip is offered only where it can still change something: editable weeks, or a
// skipped/paused week whose deadline hasn't passed (or, without a deadline, isn't past).
export function canSkip(week, now = Date.now()) {
  if (!week) return false;
  if (isWeekEditable(week, now)) return true;
  if (!isSkipped(week)) return false;
  const deadline = week.selection_deadline ? Date.parse(week.selection_deadline) : null;
  if (deadline) return deadline > now;
  if (!week.delivery_date) return false;
  return parseLocalDate(week.delivery_date).getTime() >= startOfToday(new Date(now));
}

// Change-day is offered on editable weeks that list alternate delivery options.
export function canReschedule(week, now = Date.now()) {
  if (!isWeekEditable(week, now)) return false;
  return (week.available_one_off_options || []).some((o) => o && o.handle);
}

export function isHolidayShifted(week) {
  return Boolean(week && (week.holiday_message || week.holiday_delivery_date));
}

// ---- week list --------------------------------------------------------------------------------

export function weekSortKey(week) {
  const ms = week && week.delivery_date ? parseLocalDate(week.delivery_date).getTime() : NaN;
  return Number.isNaN(ms) ? Number.POSITIVE_INFINITY : ms;
}

export function sortWeeks(weeks) {
  return (weeks || []).slice().sort((a, b) => weekSortKey(a) - weekSortKey(b));
}

// The weeks the card lets you browse, chronologically. Past and current weeks always stay (they
// are history). HelloFresh schedules deliveries further out than it publishes menus, so future
// weeks are kept only while they still carry data (meals or Market), stopping at the first gap —
// except skipped weeks, which have no menu by design and must stay visible to be unskipped.
export function browsableWeeks(weeks, now = new Date()) {
  const today = startOfToday(now);
  const result = [];
  let ended = false;
  for (const week of sortWeeks(weeks)) {
    const isFuture = week.delivery_date
      ? parseLocalDate(week.delivery_date).getTime() >= today
      : true;
    if (!isFuture) {
      result.push(week);
      continue;
    }
    if (ended) continue;
    if (isSkipped(week)) {
      result.push(week);
      continue;
    }
    const hasData = (week.recipes || []).length > 0 || (week.market_items || []).length > 0;
    if (!hasData) {
      ended = true;
      continue;
    }
    result.push(week);
  }
  return result;
}

// Index of the week nearest today, ties broken toward the upcoming one; -1 when none is dated.
export function currentWeekIndex(weeks, now = new Date()) {
  const today = startOfToday(now);
  let best = -1;
  let bestDays = Infinity;
  (weeks || []).forEach((w, i) => {
    if (!w.delivery_date) return;
    const d = parseLocalDate(w.delivery_date);
    d.setHours(0, 0, 0, 0);
    const days = Math.round((d.getTime() - today) / 86400000);
    const dist = Math.abs(days);
    if (dist < bestDays || (dist === bestDays && days >= 0)) {
      best = i;
      bestDays = dist;
    }
  });
  return best;
}

// The box the Overview leads with: the nearest upcoming week that ships, else the nearest
// upcoming week at all (every one skipped), else the most recent past box ("Last box").
export function nextBoxWeek(weeks, now = new Date()) {
  const list = weeks || [];
  const upcoming =
    list.find((w) => isUpcoming(w, now) && w.delivery_date && !isSkipped(w)) ||
    list.find((w) => isUpcoming(w, now) && w.delivery_date);
  if (upcoming) return { week: upcoming, upcoming: true };
  const past = list.filter((w) => w.delivery_date && !isUpcoming(w, now));
  return { week: past.length ? past[past.length - 1] : null, upcoming: false };
}

// Editable weeks still waiting on the customer, in order.
export function weeksNeedingAttention(weeks, now = Date.now()) {
  return (weeks || []).filter((w) => needsSelection(w, now));
}

// ---- money --------------------------------------------------------------------------------

// The week's OWN billed/cart price — never the account plan-price fallback, so an old week
// can't show today's plan price as if it were its bill. Skipped weeks cost nothing.
export function weekPriceParts(week) {
  if (!week || isSkipped(week)) return null;
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

// What to show as a box's total: its bill, else its cart estimate, else the recurring plan
// price labelled as such (the meal planner's resolution order).
export function boxTotal(week, account) {
  if (!week || isSkipped(week)) return null;
  const own = weekPriceParts(week);
  if (own) return { ...own, estimate: false, label: t("money.total") };
  if (account && account.selected_plan_total_price != null) {
    const amount = Number(account.selected_plan_total_price);
    if (Number.isFinite(amount)) {
      return {
        amount,
        currency: account.selected_plan_total_price_currency,
        estimate: true,
        label: t("money.plan_price"),
      };
    }
  }
  return null;
}

// The first available wallet promise on a week ("$10 off premium meals"), or null.
export function weekBenefit(week) {
  if (!week || isSkipped(week)) return null;
  const list = Array.isArray(week.benefits) ? week.benefits : [];
  return list.find((b) => b && b.status === "available" && b.label) || null;
}

// The next box's voucher as {label, note, code}: the week's own promise, else the account's
// next_box_discount when it names this week.
export function nextBoxVoucher(week, account) {
  let benefit = weekBenefit(week);
  if (!benefit && account && account.next_box_discount) {
    const candidate = account.next_box_discount;
    if (candidate.label && (!candidate.week_id || candidate.week_id === (week && week.week_id))) {
      benefit = candidate;
    }
  }
  if (!benefit) return null;
  const parts = [];
  if (benefit.expires_at) {
    const expires = new Date(benefit.expires_at);
    if (!Number.isNaN(expires.getTime())) parts.push(t("voucher.expires", { date: fmtDate(expires.toISOString()) }));
  }
  if (benefit.one_time) parts.push(t("voucher.one_time"));
  return { label: benefit.label, note: parts.join(" · "), code: benefit.voucher_code || null };
}

// The next box's discount from the account payload's price breakdown, or null when zero.
export function nextBoxDiscount(account) {
  const breakdown = account && account.next_delivery_price_breakdown;
  const amount = breakdown ? Number(breakdown.discount_amount) : NaN;
  if (!Number.isFinite(amount) || amount <= 0) return null;
  return fmtPrice(amount, account.next_delivery_total_currency);
}

// Normalize HelloFresh's surcharge label ("+7.99/serving", "+7,99/portion") to "+$7.99" in the
// account currency. Decimal commas are normalized first so non-English markets don't truncate.
export function fmtSurcharge(label, currency) {
  const m = String(label).replace(/,/g, ".").match(/\+?\s*([\d.]+)/);
  if (!m) return String(label);
  const amount = Number.parseFloat(m[1]);
  if (!Number.isFinite(amount)) return String(label);
  try {
    return amount.toLocaleString(I18n.numberLocale(), {
      style: "currency",
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
      currency: currency || "USD",
      signDisplay: "always",
      currencyDisplay: "narrowSymbol",
    });
  } catch (_e) {
    return String(label);
  }
}

// A per-serving price in its narrow symbol ("$9.99", not "CA$9.99").
export function fmtPerServing(amount, currency) {
  if (typeof amount !== "number" || !Number.isFinite(amount)) return "";
  try {
    return amount.toLocaleString(I18n.numberLocale(), {
      style: "currency",
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
      currency: currency || "USD",
      currencyDisplay: "narrowSymbol",
    });
  } catch (_e) {
    return String(amount);
  }
}

// Whole-unit price for tight labels ("$183").
export function fmtPriceCompact(amount, currency) {
  const num = Number(amount);
  if (!Number.isFinite(num)) return "";
  try {
    return num.toLocaleString(I18n.numberLocale(), {
      style: "currency",
      currency: currency || "USD",
      maximumFractionDigits: 0,
    });
  } catch (_e) {
    return `${Math.round(num)}`;
  }
}

// ---- tracking ------------------------------------------------------------------------------

// The carrier journey's steps (tracking.step.<key>).
export const TRACK_STEPS = ["preparing", "shipped", "out_for_delivery", "delivered"];

// Where a box is in its journey, as an index into TRACK_STEPS; -1 when there is nothing to
// track yet (no order, no carrier data). Driven by the carrier's own status when present.
export function trackingStep(week) {
  if (!week || isSkipped(week)) return -1;
  if (isDelivered(week)) return 3;
  const order = week.order || {};
  const tracking = lower(order.tracking_status);
  const status = upper(week.status);
  if (tracking === "out_for_delivery" || status === "OUT_FOR_DELIVERY") return 2;
  if (SHIPPED_TRACKING_STATUSES.has(tracking) || SHIPPED_WEEK_STATUSES.has(status)) return 1;
  if (tracking === "pre_transit" || order.tracking_number) return 0;
  return -1;
}

// The box/tracking status for a week when it adds something beyond the state badge.
export function rowStatus(week, badgeLabel) {
  const order = (week && week.order) || {};
  const code = order.tracking_status || order.status || (week && week.status) || "";
  const status = code ? statusLabel(code) : "";
  const shown = status && status.toLowerCase() !== String(badgeLabel || "").toLowerCase() ? status : "";
  const detail = order.tracking_status_detail && statusKey(order.tracking_status_detail) !== statusKey(code)
    ? detailLabel(order.tracking_status_detail)
    : "";
  const repeats = [status, badgeLabel].some((s) => s && s.toLowerCase() === detail.toLowerCase());
  return [shown, detail && !repeats ? detail : ""].filter(Boolean).join(" · ");
}

// Proof-of-delivery photos (http(s) only, at most three).
export function deliveryPhotos(week) {
  const order = (week && week.order) || {};
  return (Array.isArray(order.delivery_photo_urls) ? order.delivery_photo_urls : [])
    .map((url) => safeUrl(url))
    .filter(Boolean)
    .slice(0, 3);
}

// ---- live tracking (the Netherlands, Germany) --------------------------------------------
// Where HelloFresh drives its own vans (the integration's TRACEY_COUNTRIES, today the
// Netherlands and Germany) its tracking page (hftrack.nl, status.hellofresh.de) is backed by
// a live tracker. The integration
// polls it into sensors: the phase sensor carries the whole snapshot as attributes, the ETA
// sensor the arrival time. Phases are HelloFresh's own; `step` indexes LIVE_STEPS.
// Steps read live.step.<key>; phases live.phase.<phase>.
export const LIVE_STEPS = ["packed", "on_the_way", "delivered"];
export const LIVE_PHASES = {
  AT_DEPOT: { step: 0 },
  DRIVER_DEPARTED: { step: 1 },
  ON_THE_WAY: { step: 1 },
  DELAYED: { step: 1, tone: "warn" },
  DELIVERED: { step: 2 },
  DELIVERED_HOME: { step: 2 },
  CANCELLED: { step: -1, tone: "danger" },
};

// The live snapshot from the two sensors' states, or null while no delivery is live (the
// sensors read Unknown and `active` is false outside a delivery).
export function liveTracking(phaseState, etaState) {
  const a = (phaseState && phaseState.attributes) || null;
  if (!a || a.active !== true) return null;
  const raw = upper(a.phase);
  const known = LIVE_PHASES[raw];
  const date = (value) => {
    const d = value ? new Date(value) : null;
    return d && !Number.isNaN(d.getTime()) ? d : null;
  };
  const etaValue = etaState && !["unknown", "unavailable", ""].includes(String(etaState.state)) ? etaState.state : null;
  const stops = a.amount_of_stops_before;
  return {
    phase: raw,
    label: known ? t(`live.phase.${raw.toLowerCase()}`) : sentenceCase(phaseState.state || raw),
    step: known ? known.step : 1,
    tone: (known && known.tone) || "",
    eta: date(etaValue),
    window: a.delivery_time ? String(a.delivery_time) : "",
    stops: typeof stops === "number" && Number.isFinite(stops) ? stops : null,
    driver: a.driver_name ? String(a.driver_name) : "",
    message: a.personal_customer_message ? String(a.personal_customer_message) : "",
    trackingUrl: a.tracking_url ? String(a.tracking_url) : "",
    mapUrl: safeUrl(a.tracking_url),
    updated: date(a.last_fetched),
  };
}

// The tracker is keyed by the order's tracking link, so that is what ties it to a week.
export function liveTrackingFor(live, week) {
  const url = week && week.order && week.order.tracking_url;
  return Boolean(live && live.trackingUrl && url && live.trackingUrl === url);
}

// "6:42 PM"
export function fmtTime(date) {
  try {
    return date.toLocaleTimeString(I18n.timeLocale(), { hour: "numeric", minute: "2-digit", ...I18n.hourOptions() });
  } catch (_e) {
    return "";
  }
}

// "in 35 min" / "in 1 h 5 min" / "any minute now"; "" once the time has passed.
export function untilText(date, now = new Date()) {
  const minutes = Math.round((date.getTime() - now.getTime()) / 60000);
  if (minutes < 0) return "";
  if (minutes <= 1) return t("time.any_minute");
  return t("time.in", { duration: formatMinutes(minutes) });
}

// "just now" / "2 min ago" / "1 h 5 min ago".
export function agoText(date, now = new Date()) {
  const minutes = Math.max(0, Math.round((now.getTime() - date.getTime()) / 60000));
  return minutes < 1 ? t("time.just_now") : t("time.ago", { duration: formatMinutes(minutes) });
}

// ---- meal selection -------------------------------------------------------------------------

// Stable selection key: the course index (what the cart writes) when present, else the recipe
// id — delivered meals carry no index, and keying them on null collapsed a week onto one meal.
export function selKey(recipe) {
  return recipe.course_index != null ? recipe.course_index : recipe.recipe_id;
}

export function isRecipeSelected(recipe) {
  return recipe.is_selected === true || recipe.is_selected === "true";
}

export function recipeQuantity(recipe) {
  const q = Number(recipe.selected_quantity);
  return Number.isFinite(q) && q > 0 ? q : 1;
}

// The saved selection as Map(selKey -> servings).
export function savedMealSelection(week) {
  const map = new Map();
  for (const r of (week && week.recipes) || []) {
    if (isRecipeSelected(r)) map.set(selKey(r), recipeQuantity(r));
  }
  return map;
}

export function mealsCount(selection) {
  return selection.size;
}

export function servingsTotal(selection) {
  let total = 0;
  for (const q of selection.values()) total += q;
  return total;
}

export function selectionsEqual(a, b) {
  if (a.size !== b.size) return false;
  for (const [key, qty] of a) if (b.get(key) !== qty) return false;
  return true;
}

// What would visibly tell two same-named tiles apart. Identical signatures are TRUE duplicates
// (one dish listed under several menu categories) and are collapsed into one tile.
export function recipeSignature(r) {
  return JSON.stringify([
    r.name || "",
    r.description || "",
    r.variation_title || "",
    r.surcharge_label || "",
    r.calories_kcal ?? "",
    r.protein_g ?? "",
    (r.tags || []).slice().sort(),
  ]);
}

// Collapse true duplicates, keeping every copy's course index under `_aliasIndexes` so a saved
// selection on any copy still shows as chosen. Also counts names, since after dedupe a name
// only recurs when the copies genuinely differ (variants).
export function dedupeRecipes(recipes) {
  const bySig = new Map();
  for (const r of recipes || []) {
    const sig = recipeSignature(r);
    const existing = bySig.get(sig);
    if (existing) existing.aliasIndexes.push(r.course_index);
    else bySig.set(sig, { recipe: r, aliasIndexes: [r.course_index] });
  }
  const out = [...bySig.values()].map(({ recipe, aliasIndexes }) =>
    aliasIndexes.length > 1 ? { ...recipe, _aliasIndexes: aliasIndexes } : recipe
  );
  const nameCounts = {};
  for (const r of out) nameCounts[r.name] = (nameCounts[r.name] || 0) + 1;
  return { recipes: out, nameCounts };
}

// Whether a (possibly collapsed) tile is in a selection map.
export function tileSelected(selection, recipe) {
  return selection.has(selKey(recipe)) || (recipe._aliasIndexes || []).some((i) => selection.has(i));
}

// The quantity a (possibly collapsed) tile shows.
export function tileQuantity(selection, recipe) {
  const idx = (recipe._aliasIndexes || [selKey(recipe)]).find((i) => selection.has(i));
  return idx === undefined ? 0 : selection.get(idx);
}

// The key a tile writes to: the alias already chosen, else its own course index.
export function activeIndex(selection, recipe) {
  const all = recipe._aliasIndexes || [recipe.course_index];
  const chosen = all.find((i) => selection.has(i));
  return chosen !== undefined ? chosen : recipe.course_index;
}

// Translate a pending selection back into select_meals' recipe ids + serving overrides.
export function buildMealWrite(week, selection) {
  const byIndex = new Map(((week && week.recipes) || []).map((r) => [r.course_index, r.recipe_id]));
  const recipeIds = [];
  const quantities = {};
  for (const [idx, qty] of selection) {
    const recipeId = byIndex.get(idx);
    if (!recipeId || recipeIds.includes(recipeId)) continue;
    recipeIds.push(recipeId);
    if (qty > 1) quantities[recipeId] = qty;
  }
  return { recipe_ids: recipeIds, quantities };
}

// ---- dishes & their customization options ---------------------------------------------------
//
// HelloFresh lists a dish's customization options ("2x Chicken Cutlets", "Salmon", "Added
// Bacon") as separate menu meals — a week's ~460 meals are only ~85 dishes. `variation_group`
// names the base dish each option belongs to. Like the website, the Menu shows ONE tile per dish
// and switches between its options in a customization drawer; choosing one simply selects that
// option's own meal in place of the base.

// "3 teaspoon (tsp)" -> "3 tsp". Pantry amounts use HelloFresh's "unit (abbrev)" spelling (see
// todo.py), and a shopping list wants the abbreviation; an amount without one stays as it is.
export function shortAmount(text) {
  return String(text || "")
    .split(" + ")
    .map((part) => {
      const m = part.trim().match(/^(\S+)\s+[^()]*\(([^()]+)\)$/);
      return m ? `${m[1]} ${m[2].trim()}` : part.trim();
    })
    .join(" + ");
}

export function isDefaultMeal(r) {
  return r.variation_group == null || r.course_index === r.variation_group;
}

// Recipes grouped into dishes, each at its base's place in the catalog (as the website orders
// them; an option can come before its base in the menu). Each dish lists the base first, then
// its options in the website's order: {key, base, members}.
export function dishGroups(recipes) {
  const byKey = new Map();
  const place = new Map();
  (recipes || []).forEach((r, i) => {
    const key = r.variation_group != null ? `g:${r.variation_group}` : `r:${selKey(r)}`;
    if (!byKey.has(key)) byKey.set(key, { key, members: [] });
    byKey.get(key).members.push(r);
    place.set(r, i);
  });
  const rank = (r) => (isDefaultMeal(r) ? -1 : Number.isFinite(r.variation_order) ? r.variation_order : Infinity);
  const groups = [...byKey.values()].map((group) => {
    group.members.sort((a, b) => (rank(a) > rank(b)) - (rank(a) < rank(b)));
    group.base = group.members.find(isDefaultMeal) || group.members[0];
    return group;
  });
  return groups.sort((a, b) => place.get(a.base) - place.get(b.base));
}

// How the customization list names one of a dish's members: an option by its modifier, the
// unchanged dish by the site's own label for it ("No Change", "No Protein", "Ground Beef").
export function optionLabel(recipe) {
  if (!isDefaultMeal(recipe)) return recipe.variation_title || recipe.name || "";
  return recipe.variation_default_title || t("menu.original_recipe");
}

// The Menu grid: one tile per dish — or one per chosen option when several of a dish's options
// are in the box, so nothing chosen is ever hidden. An unchosen dish shows its base, unless the
// filters or search rule the base out, in which case its first option that fits stands in
// ("Crunchy Hot Honey Salmon" under Seafood); a dish with nothing that fits is left out. That is
// the website's own rule (HAR 57: its grid, tile for tile, unfiltered and under each protein).
//
//   * `selectedOnly` shows just the box.
//   * `applyFilters` (current/upcoming weeks outside selected-only) applies protein / dietary /
//     time / highlight filters plus the section and server id-sets; `proteinIds`, HelloFresh's
//     own answer for the protein chips, adds the meals the menu leaves without a protein.
//     Chosen meals always pass them, so a chosen meal never vanishes while editing.
//   * `query` is a plain search and applies to every tile, chosen or not.
//
// Chosen tiles lead; the rest keep catalog order. Returns [{recipe, group}].
export function menuTiles(recipes, options) {
  const {
    sel,
    selectedOnly = false,
    applyFilters = false,
    protein = new Set(),
    proteinIds = null,
    diet = new Set(),
    time = "",
    highlight = "",
    sectionIds = null,
    serverIds = null,
    query = "",
  } = options;
  const passesProtein = (r) =>
    protein.size === 0 || protein.has(r.preference) || Boolean(proteinIds && proteinIds.has(bareRecipeId(r)));
  const passesFilters = (r) => {
    if (!applyFilters) return true;
    if (!passesProtein(r)) return false;
    if (!passesDietFilters(r, diet)) return false;
    if (!passesTimeFilter(r, time)) return false;
    if (!passesHighlightFilter(r, highlight)) return false;
    if (sectionIds && !sectionIds.has(bareRecipeId(r))) return false;
    if (serverIds && !serverIds.has(bareRecipeId(r))) return false;
    return true;
  };
  const tiles = [];
  for (const group of dishGroups(recipes)) {
    const chosen = group.members.filter((r) => sel(r));
    if (chosen.length) {
      for (const recipe of chosen) if (matchesQuery(recipe, query)) tiles.push({ recipe, group });
      continue;
    }
    if (selectedOnly) continue;
    const recipe = group.members.find((r) => passesFilters(r) && matchesQuery(r, query));
    if (recipe) tiles.push({ recipe, group });
  }
  return tiles.sort((a, b) => (sel(b.recipe) ? 1 : 0) - (sel(a.recipe) ? 1 : 0));
}

// ---- menu filters ---------------------------------------------------------------------------

// One dietary/time category: an alias tag, or the category's numeric fallback. The menu
// payload swaps its time names: prep_time_minutes carries the headline time the website shows,
// so it is preferred (HAR-verified against the site's own filter counts).
export function matchesDietFilter(r, f) {
  const tags = (r.tags || []).map((t) => String(t).toLowerCase());
  if (f.tags.some((t) => tags.includes(t))) return true;
  if (f.maxCalories != null && r.calories_kcal != null && r.calories_kcal <= f.maxCalories) {
    return true;
  }
  if (f.maxMinutes != null) {
    const mins = r.prep_time_minutes != null ? r.prep_time_minutes : r.total_time_minutes;
    if (mins != null && mins <= f.maxMinutes) return true;
  }
  return false;
}

// Dietary chips are ANDed constraints (the site's MULTI-AND), unlike proteins (MULTI-OR).
export function passesDietFilters(r, dietKeys) {
  if (!dietKeys || dietKeys.size === 0) return true;
  return DIET_FILTERS.every((f) => !dietKeys.has(f.key) || matchesDietFilter(r, f));
}

export function passesTimeFilter(r, timeKey) {
  if (!timeKey) return true;
  const f = TIME_FILTERS.find((t) => t.key === timeKey);
  return !f || matchesDietFilter(r, f);
}

export function matchesHighlight(r, key) {
  if (key === "cooked-before") return (r.delivered_count || 0) > 0;
  if (key === "favorite") return r.is_favorite === true;
  const tags = (r.tags || []).map((t) => String(t).toLowerCase());
  const badge = String(r.badge || "").toLowerCase();
  return tags.includes(key) || badge === key;
}

export function passesHighlightFilter(r, key) {
  return !key || matchesHighlight(r, key);
}

// Free-text search over what a tile shows (name, modifier, description, tags, badge).
export function matchesQuery(r, query) {
  const q = String(query || "").trim().toLowerCase();
  if (!q) return true;
  const hay = [r.name, r.variation_title, r.description, r.headline, r.badge, ...(r.tags || [])]
    .filter(Boolean)
    .join("\n")
    .toLowerCase();
  return q.split(/\s+/).every((word) => hay.includes(word));
}

// The selected website menu section's bare recipe ids for this week, or null when inactive (a
// section the viewed week doesn't carry must not silently blank it).
export function menuSectionIds(week, slug) {
  if (!slug) return null;
  const row = ((week && week.menu_categories) || []).find((c) => c.slug === slug);
  if (!row || !Array.isArray(row.recipe_ids) || !row.recipe_ids.length) return null;
  return new Set(row.recipe_ids.map((id) => String(id).split("-")[0]));
}

// The server-resolved groups this week declares, in panel order, with their options.
export function serverFilterGroups(week) {
  const declared = (week && Array.isArray(week.menu_filters) && week.menu_filters) || [];
  const out = [];
  for (const slug of SERVER_FILTER_GROUPS) {
    const group = declared.find((g) => g && g.slug === slug);
    const options = group && Array.isArray(group.options) ? group.options : [];
    if (options.length) out.push({ ...group, slug, options });
  }
  return out;
}

// Active server selections that APPLY to this week, as {group: [option slugs]} in the week's
// own option order (deterministic, so it doubles as a cache key).
export function activeServerFilters(week, selections) {
  const out = {};
  for (const group of serverFilterGroups(week)) {
    const set = selections && selections[group.slug];
    if (!set || set.size === 0) continue;
    const slugs = group.options.map((o) => o.slug).filter((s) => set.has(s));
    if (slugs.length) out[group.slug] = slugs;
  }
  return out;
}

// The chosen protein chips as a filter-service query, {"main-protein": [slugs]}, limited to the
// slugs this week's menu declares; {} when there is nothing to ask.
export function proteinServerFilters(week, protein) {
  const group = ((week && week.menu_filters) || []).find((g) => g && g.slug === "main-protein");
  const declared = new Set(((group && group.options) || []).map((o) => o.slug));
  const slugs = PROTEIN_FILTERS.filter((p) => protein && protein.has(p))
    .map((p) => PROTEIN_SERVER_SLUGS[p])
    .filter((slug) => slug && declared.has(slug));
  return slugs.length ? { "main-protein": slugs } : {};
}

export function serverFilterKey(weekId, filters) {
  return `${weekId}|${JSON.stringify(filters)}`;
}

export function bareRecipeId(recipe) {
  return String(recipe.recipe_id).split("-")[0];
}

// Dietary chips for a tile, from the same alias table as the filter (tags only — the numeric
// fallback that widens the filter must not pin a label HelloFresh didn't give the meal). A chip
// saying what the meal's badge already says is dropped; HelloFresh writes its badges in the
// account's language, so the chip is compared in English as well as in the card's language.
export function tileChipLabels(r) {
  const tags = (r.tags || []).map((tag) => String(tag).toLowerCase());
  const chips = tags.includes("double-protein") ? [["filters.double_protein", t("filters.double_protein")]] : [];
  for (const f of DIET_FILTERS) {
    if (f.key === "vegetarian") continue;
    if (f.tags.some((tag) => tags.includes(tag))) chips.push([`filters.diet.${f.key}`, f.label]);
  }
  const badge = String(r.badge || "").toLowerCase();
  const repeatsBadge = ([key, label]) =>
    badge && (label.toLowerCase() === badge || String(englishText(key) || "").toLowerCase() === badge);
  return chips.filter((chip) => !repeatsBadge(chip)).map(([, label]) => label);
}

// HelloFresh's own badge colours, re-validated as #hex so a payload can't inject CSS.
export function badgeStyle(r) {
  const ok = (c) => typeof c === "string" && /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/.test(c);
  const parts = [];
  if (ok(r.badge_background)) parts.push(`background:${r.badge_background}`);
  if (ok(r.badge_foreground)) parts.push(`color:${r.badge_foreground}`);
  return parts.length ? ` style="${parts.join(";")}"` : "";
}

// Recipe promo clips: plain http(s) only, or "".
export function safeMediaUrl(url) {
  if (!url || !/^https?:\/\//i.test(String(url))) return "";
  return String(url);
}

// ---- market ----------------------------------------------------------------------------------

export function marketItemQuantity(item) {
  const q = Number(item.selected_quantity);
  if (Number.isFinite(q) && q > 0) return q;
  return item.is_selected === true ? 1 : 0;
}

export function savedMarketSelection(week) {
  const map = new Map();
  for (const item of (week && week.market_items) || []) {
    const q = marketItemQuantity(item);
    if (q > 0) map.set(item.item_id, q);
  }
  return map;
}

// Distinct add-ons chosen for a week (the schedule card's count).
export function marketCount(week) {
  return ((week && week.market_items) || []).filter((item) => {
    const q = Number(item.selected_quantity);
    return Number.isFinite(q) ? q > 0 : item.is_selected === true;
  }).length;
}

export function marketCap(item) {
  return Math.min(item.max_quantity != null ? item.max_quantity : MAX_MARKET_QTY, MAX_MARKET_QTY);
}

export function marketGroupLabel(slug) {
  if (MARKET_BRANDS[slug]) return MARKET_BRANDS[slug];
  const key = `market.group.${statusKey(slug)}`;
  return hasText(key) ? t(key) : titleCase(slug);
}

// Section slugs present in a week's catalog, in first-appearance order.
export function marketSections(week) {
  const seen = new Set();
  const out = [];
  for (const item of (week && week.market_items) || []) {
    if (item.group_type && !seen.has(item.group_type)) {
      seen.add(item.group_type);
      out.push(item.group_type);
    }
  }
  return out;
}

export function marketTotalCents(week, selection) {
  let cents = 0;
  for (const item of (week && week.market_items) || []) {
    const qty = selection.get(item.item_id) || 0;
    if (qty > 0 && item.price_cents != null) cents += item.price_cents * qty;
  }
  return cents;
}

// A Market item's own currency is often null; fall back to the week's order currency so a
// non-USD account isn't priced with "$".
export function marketCurrency(week) {
  const item = ((week && week.market_items) || []).find((i) => i.currency);
  return (item && item.currency) || (week && week.order && week.order.currency) || null;
}

// The full desired state for select_market_items (omitted items are removed).
export function buildMarketWrite(selection) {
  const quantities = {};
  for (const [id, qty] of selection) if (qty > 0) quantities[id] = qty;
  return quantities;
}

// Group a week's Market items for display. History weeks (no group_type) come back as one
// untitled group in delivery order, like the classic card.
export function marketGroups(week, selection, { selectedOnly = false, sections = null, query = "" } = {}) {
  const qtyOf = (item) => selection.get(item.item_id) || 0;
  let items = ((week && week.market_items) || []).slice();
  if (selectedOnly) items = items.filter((i) => qtyOf(i) > 0);
  if (sections && sections.size > 0) {
    items = items.filter((i) => qtyOf(i) > 0 || sections.has(i.group_type));
  }
  if (query && String(query).trim()) items = items.filter((i) => matchesQuery(i, query));
  const order = [];
  const byGroup = new Map();
  for (const item of items) {
    const key = item.group_type || "";
    if (!byGroup.has(key)) {
      byGroup.set(key, []);
      order.push(key);
    }
    byGroup.get(key).push(item);
  }
  return order.map((key) => ({
    key,
    label: key ? marketGroupLabel(key) : "",
    items: byGroup.get(key),
  }));
}

// ---- calendar ---------------------------------------------------------------------------------

const WEEKDAY_NAMES = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"];

// The calendar's first column (0 = Sunday), from Home Assistant's own "First day of the week"
// setting; left on "language", the language decides (Sunday for US English, Monday for Dutch).
export function firstWeekday(hass) {
  const locale = (hass && hass.locale) || {};
  const setting = String(locale.first_weekday || "language").toLowerCase();
  if (WEEKDAY_NAMES.includes(setting)) return WEEKDAY_NAMES.indexOf(setting);
  const language = locale.language || (hass && hass.language) || "en-US";
  try {
    const intl = new Intl.Locale(language);
    const info = typeof intl.getWeekInfo === "function" ? intl.getWeekInfo() : intl.weekInfo;
    if (info && Number.isFinite(info.firstDay)) return info.firstDay % 7;
  } catch (_e) {
    /* an unknown tag: fall through */
  }
  return /^en(-us)?$/i.test(language) ? 0 : 1;
}

// The day a week sits on: when it actually arrived, else when it is scheduled.
export function weekDay(week) {
  return (week && (week.delivered_at || week.delivery_date)) || null;
}

// First-of-month bounds of the loaded data (the real current month always in range).
export function calBounds(weeks, now = new Date()) {
  let min = null;
  let max = null;
  for (const week of weeks || []) {
    const when = weekDay(week);
    if (!when) continue;
    const d = parseLocalDate(when);
    const m = new Date(d.getFullYear(), d.getMonth(), 1).getTime();
    if (min === null || m < min) min = m;
    if (max === null || m > max) max = m;
  }
  const cur = new Date(now.getFullYear(), now.getMonth(), 1).getTime();
  return {
    min: min === null ? cur : Math.min(min, cur),
    max: max === null ? cur : Math.max(max, cur),
  };
}

// Map of local-midnight ms -> week, for marking calendar days.
export function weeksByDay(weeks) {
  const byDay = new Map();
  for (const week of weeks || []) {
    const when = weekDay(week);
    if (!when) continue;
    const d = parseLocalDate(when);
    byDay.set(new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime(), week);
  }
  return byDay;
}

export function monthWeeks(weeks, month) {
  return (weeks || []).filter((week) => {
    const when = weekDay(week);
    if (!when) return false;
    const d = parseLocalDate(when);
    return d.getFullYear() === month.getFullYear() && d.getMonth() === month.getMonth();
  });
}

// Boxes, skipped weeks and what the boxes cost (own prices only, never the plan fallback).
export function monthRollup(rows) {
  const boxes = rows.filter((w) => !isSkipped(w)).length;
  let total = 0;
  let currency = null;
  let priced = 0;
  for (const w of rows) {
    const p = weekPriceParts(w);
    if (!p) continue;
    total += p.amount;
    currency = currency || p.currency;
    priced += 1;
  }
  return { boxes, skipped: rows.length - boxes, total, currency, priced };
}

// ---- spending -------------------------------------------------------------------------------

// Expand the sparse month list into a contiguous run of `span` months ending at the newest
// month present, zero-filling gaps so the chart's axis is an unbroken timeline.
export function denseMonths(months, span) {
  const byKey = new Map();
  let newest = null;
  for (const m of months || []) {
    if (typeof m.month !== "string" || !/^\d{4}-\d{2}$/.test(m.month)) continue;
    byKey.set(m.month, m);
    if (newest === null || m.month > newest) newest = m.month;
  }
  if (newest === null) return [];
  let year = Number(newest.slice(0, 4));
  let mon = Number(newest.slice(5, 7));
  const out = [];
  for (let i = 0; i < span; i += 1) {
    const key = `${year}-${String(mon).padStart(2, "0")}`;
    out.push(byKey.get(key) || { month: key, amount: 0, currency: null, box_count: 0, upcoming: false });
    mon -= 1;
    if (mon === 0) {
      mon = 12;
      year -= 1;
    }
  }
  return out.reverse().map((m) => ({ ...m, amount: Number(m.amount) || 0 }));
}

// "2026-06" -> "Jun 2026".
export function fmtMonth(key, options = { month: "short", year: "numeric" }) {
  const m = typeof key === "string" && /^(\d{4})-(\d{2})$/.exec(key);
  if (!m) return key || "—";
  try {
    return new Date(Number(m[1]), Number(m[2]) - 1, 1).toLocaleDateString(I18n.dateLocale(), options);
  } catch (_e) {
    return key;
  }
}

// ---- account ---------------------------------------------------------------------------------

// "Visa ending in 4242" — the brand when reported, else the type humanized, plus last four.
export function cardOnFile(summary) {
  const raw = summary && (summary.payment_card_brand || summary.payment_card_type);
  const name = raw ? String(raw) : "";
  if (!name) return "";
  const humanized = name.replace(/[_-]+/g, " ").replace(/\b\w/g, (ch) => ch.toUpperCase());
  const brand = humanized === "Credit Card" ? t("account.credit_card") : humanized;
  const last4 = /^\d{4}$/.test(String(summary.payment_card_last4 || "")) ? summary.payment_card_last4 : "";
  return last4 ? t("account.card_ending", { brand, last4 }) : brand;
}

export function fmtCardExpiry(value) {
  const match = /^(\d{4})-(\d{2})$/.exec(String(value || ""));
  if (!match) return "";
  const date = new Date(Number(match[1]), Number(match[2]) - 1, 1);
  try {
    return date.toLocaleDateString(I18n.dateLocale(), { month: "long", year: "numeric" });
  } catch (_err) {
    return `${match[2]}/${match[1]}`;
  }
}

// One figure from the plan price breakdown; null when missing (or zero, for discounts).
export function breakdownAmount(summary, key, onlyIfPositive = false) {
  const breakdown = summary && summary.selected_plan_price_breakdown;
  const amount = breakdown ? Number(breakdown[key]) : NaN;
  if (!Number.isFinite(amount)) return null;
  if (onlyIfPositive && amount <= 0) return null;
  return amount;
}

// The plan preference's display name: the preset catalog's name when loaded, else the slug
// humanized ("quick-and-easy" -> "Quick And Easy"); null when there is none.
export function preferenceName(slug, presets) {
  if (!slug) return null;
  const target = String(slug).toLowerCase();
  if (Array.isArray(presets)) {
    const match = presets.find((p) => p && String(p.handle || "").toLowerCase() === target);
    if (match && match.name) return match.name;
  }
  return String(slug)
    .replace(/[-_]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

// ---- Home Assistant entities -------------------------------------------------------------------

// This integration's entity for a translation key (e.g. "prep_list", "box_size"), found through
// the frontend's entity registry rather than a guessed entity_id — ids derive from the account
// title, which the user can rename. With a config_entry_id, only that account's device counts.
export function findEntity(hass, translationKey, { configEntryId = null, domain = null } = {}) {
  const entities = (hass && hass.entities) || {};
  const devices = (hass && hass.devices) || {};
  for (const entry of Object.values(entities)) {
    if (!entry || entry.platform !== "hellofresh" || entry.translation_key !== translationKey) {
      continue;
    }
    if (domain && !String(entry.entity_id || "").startsWith(`${domain}.`)) continue;
    if (configEntryId) {
      const device = entry.device_id ? devices[entry.device_id] : null;
      const entries = (device && device.config_entries) || [];
      if (!entries.includes(configEntryId)) continue;
    }
    return entry.entity_id;
  }
  return null;
}
