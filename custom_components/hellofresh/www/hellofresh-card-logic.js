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
  formatMinutes,
  fmtPrice,
  focusableIn,
  keepTabInside,
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

// Statuses that mean the box never came.
const UNDELIVERED_STATUSES = new Set(["CANCELLED", "PAYMENT_FAILED", "FAILED"]);

export const SHIPPED_WEEK_STATUSES = new Set(["ON_THE_WAY", "SHIPPED", "IN_TRANSIT", "OUT_FOR_DELIVERY"]);
// A carrier says "delayed" only for a box it already has (Veho: delayed_in_transit), and the
// website's tracking page puts that step after "shipped".
export const SHIPPED_TRACKING_STATUSES = new Set(["in_transit", "out_for_delivery", "shipped", "on_the_way", "delayed"]);

// HelloFresh's own states for a box it is still packing or sending.
const PRE_ARRIVAL_STATUSES = new Set(["PREPARING", ...SHIPPED_WEEK_STATUSES]);

// HelloFresh keeps a box's progress for a few weeks only, so an older week can come with none.
// Like the website, a week whose delivery day has passed then counts as delivered, unless it was
// skipped, never sent or late: "locked" is for a box past its deadline that hasn't come yet.
// That guess is only for a week with nothing live left. While the carrier has a status, the
// carrier decides, so a box it calls delayed, damaged or still on the road is not delivered
// whatever the date; nor is one HelloFresh says it is still preparing or shipping.
export function isDelivered(week, now = new Date()) {
  if (!week) return false;
  if (week.delivered_at) return true;
  if (upper(week.status) === "DELIVERED") return true;
  const tracking = lower(week.order && week.order.tracking_status);
  if (tracking === "delivered") return true;
  return (
    isPastWeek(week, now) &&
    !tracking &&
    !isSkipped(week) &&
    !UNDELIVERED_STATUSES.has(upper(week.status)) &&
    !PRE_ARRIVAL_STATUSES.has(upper(week.status)) &&
    upper(week.sub_status) !== "DELAYED"
  );
}

// A box that has left HelloFresh but not arrived.
export function isShipping(week, now = new Date()) {
  if (!week || isSkipped(week) || isDelivered(week, now)) return false;
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
  if (isDelivered(week, now)) return "delivered";
  if (isShipping(week, now)) return "shipping";
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

// A box HelloFresh moved after the fact: {kind: "delayed" | "early", date} when the week's
// sub-status says so and carries the new date, else null. A box that has arrived, or was
// skipped, has nothing still to arrive.
export function revisedDelivery(week) {
  const kind = upper(week && week.sub_status);
  if ((kind !== "DELAYED" && kind !== "EARLY") || !week.revised_delivery_date) return null;
  if (isSkipped(week) || isDelivered(week)) return null;
  if (Number.isNaN(parseLocalDate(week.revised_delivery_date).getTime())) return null;
  return { kind: kind === "DELAYED" ? "delayed" : "early", date: week.revised_delivery_date };
}

// "Delayed — now arriving Tuesday, October 6" (week.delayed / week.early), the date long or
// short ("Tue, Oct 6"); "" for a box that wasn't moved. With `markup` it is HTML, the date in
// bold; without it, plain text.
export function revisedDeliveryText(week, { long = true, markup = false } = {}) {
  const revised = revisedDelivery(week);
  if (!revised) return "";
  const date = long ? fmtLongDate(revised.date) : fmtDate(revised.date);
  return markup
    ? ht(`week.${revised.kind}`, { date: html(`<strong>${esc(date)}</strong>`) })
    : t(`week.${revised.kind}`, { date });
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
  const currency = account.next_delivery_total_currency
    || account.selected_plan_total_price_currency
    || (account.next_box_discount && account.next_box_discount.currency);
  return fmtPrice(amount, currency);
}

// A meal's surcharge as "+$7.99" in the account currency. HelloFresh sends the amount twice: as
// `surcharge_cents` and inside a display label ("+7.99/serving", "+7,99/portion"). The cents are
// the figure; the label is only parsed when a payload has none, reading the signed number in it
// (so "2x +3.99" is 3.99, not 2), a decimal comma as one, and a leading minus as a discount.
//
// Takes the meal itself, or its label with the cents as a third argument.
export function fmtSurcharge(labelOrMeal, currency, cents = null) {
  const meal = labelOrMeal && typeof labelOrMeal === "object" ? labelOrMeal : null;
  const label = meal ? meal.surcharge_label : labelOrMeal;
  const raw = meal ? meal.surcharge_cents : cents;
  let amount = raw == null || raw === "" ? NaN : Number(raw) / 100;
  if (!Number.isFinite(amount)) amount = surchargeFromLabel(label);
  if (!Number.isFinite(amount)) return String(label ?? "");
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
    return String(label ?? "");
  }
}

// The amount in a surcharge label, or NaN. With both separators the last one is the decimal
// ("1,299.00", "1.299,00"); a lone comma is a decimal comma.
function surchargeFromLabel(label) {
  const text = String(label ?? "").replace(/\u2212/g, "-");
  const m = /([+-])\s*(\d[\d.,]*)/.exec(text) || /()(\d[\d.,]*)/.exec(text);
  if (!m) return NaN;
  let digits = m[2].replace(/[.,]+$/, "");
  const comma = digits.lastIndexOf(",");
  const dot = digits.lastIndexOf(".");
  if (comma >= 0 && dot >= 0) {
    const decimal = comma > dot ? "," : ".";
    digits = digits.split(decimal === "," ? "." : ",").join("").replace(",", ".");
  } else {
    digits = digits.replace(",", ".");
  }
  const amount = Number.parseFloat(digits);
  return m[1] === "-" ? -amount : amount;
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

// Proof-of-delivery photos, at most three: URLs as safeUrl gives them (escape them into markup).
// They load from whatever host the carrier names, as soon as the box's details show, so only
// https is let through (http would be mixed content on a secure Home Assistant) and their <img>
// carries referrerpolicy="no-referrer".
export function deliveryPhotos(week) {
  const order = (week && week.order) || {};
  return (Array.isArray(order.delivery_photo_urls) ? order.delivery_photo_urls : [])
    .map((url) => safeUrl(url, { httpsOnly: true }))
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
    // Every count, 1 included: select_meals keeps a left-out meal at the count the box has, so
    // lowering a doubled meal to one (or undoing a doubling) must say so.
    quantities[recipeId] = qty;
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

// Recipe promo clips go through the same check as every other URL (safeUrl); the views know it
// by this name.
export const safeMediaUrl = safeUrl;

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
// HelloFresh names payment methods with Adyen-style codes (the list in its Android app's
// PaymentMethod); "mc" would otherwise read "Mc". Brand names are not translated.
const PAYMENT_BRANDS = {
  amex: "American Express",
  apple_pay: "Apple Pay",
  bcmc: "Bancontact",
  i_deal: "iDEAL",
  ideal: "iDEAL",
  klarna: "Klarna",
  maestro: "Maestro",
  maestrouk: "Maestro",
  mastercard: "Mastercard",
  mc: "Mastercard",
  paypal: "PayPal",
  ratepay: "Ratepay",
  sepa: "SEPA",
  sepadirectdebit: "SEPA",
  visa: "Visa",
  visadankort: "Visa Dankort",
};

export function cardOnFile(summary) {
  const raw = summary && (summary.payment_card_brand || summary.payment_card_type);
  const name = raw ? String(raw) : "";
  if (!name) return "";
  const humanized = name.replace(/[_-]+/g, " ").replace(/\b\w/g, (ch) => ch.toUpperCase());
  const brand = PAYMENT_BRANDS[name.toLowerCase()] || (humanized === "Credit Card" ? t("account.credit_card") : humanized);
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

// A payload number, or null: Number(null) is 0, which would read as a real amount.
function amountOf(value) {
  if (value == null || value === "") return null;
  const num = Number(value);
  return Number.isFinite(num) ? num : null;
}

// A whole count in the card's number format ("1,234").
export function fmtNumber(value) {
  try {
    return new Intl.NumberFormat(I18n.numberLocale()).format(value);
  } catch (_e) {
    return String(value);
  }
}

// "A, B and C" in the language of the card's words.
export function listText(items) {
  const list = (items || []).map(String);
  try {
    return new Intl.ListFormat(I18n.language(), { type: "conjunction" }).format(list);
  } catch (_e) {
    return list.join(", ");
  }
}

// The boxes a payment problem is about (get_account_summary's payment_problem_weeks), each named
// by its delivery date (the summary's, else the card's own week's, else its week id) and its
// amount in its own currency. Null while there is no problem.
export function paymentProblem(summary, weeks) {
  if (!summary || summary.payment_problem !== true) return null;
  const listed = Array.isArray(summary.payment_problem_weeks) ? summary.payment_problem_weeks : [];
  const fallback = summary.account_credit_currency || summary.selected_plan_total_price_currency || null;
  const boxes = listed.filter((p) => p && typeof p === "object").map((p) => {
    const week = (weeks || []).find((w) => w && p.week_id && w.week_id === p.week_id);
    const day = p.delivery_date || (week && week.delivery_date) || null;
    const amount = amountOf(p.amount);
    return {
      weekId: p.week_id || null,
      date: day ? fmtLongDate(day) : String(p.week_id || ""),
      amount: amount === null ? "" : fmtPrice(amount, p.currency || fallback),
      status: lower(p.status),
    };
  });
  return { boxes: boxes.filter((b) => b.date) };
}

// What Account › Plan & billing lists under the credit: HelloFresh Cash and bonus credit (when
// there is some), in the credit's own currency, and how many boxes are left until a free one.
// [label, value] pairs.
export function creditDetails(summary) {
  if (!summary) return [];
  const currency = summary.account_credit_currency || summary.selected_plan_total_price_currency;
  const out = [];
  const cash = amountOf(summary.account_credit_cash);
  if (cash) out.push([t("account.credit_cash"), fmtPrice(cash, currency)]);
  const bonus = amountOf(summary.account_credit_bonus);
  if (bonus) out.push([t("account.credit_bonus"), fmtPrice(bonus, currency)]);
  const left = summary.loyalty_boxes_until_free_box;
  if (Number.isInteger(left) && left >= 0) {
    out.push([t("account.rewards"), left === 0 ? t("account.free_box_earned") : t("account.boxes_until_free", { count: left })]);
  }
  return out;
}

// ---- one week's number of people -------------------------------------------------------------
//
// A box is a product: so many meals for so many people, in a family (Classic, Veggie, …).
// hellofresh.change_week_servings changes the people for one week's box; get_plan_options lists
// the boxes on offer, which say what that change can be and what it costs.

const positiveInt = (value) => {
  const num = Number(value);
  return Number.isInteger(num) && num > 0 ? num : null;
};

// How many people a week's box is for: its own count, else the plan's. Null when neither says.
export function weekServings(week, summary) {
  return positiveInt(week && week.servings) || positiveInt(summary && summary.number_of_people);
}

// The product family a week's box belongs to. The week names no product, so: the one family that
// sells a box of its size (its meals for its people), else the family whose name the box or the
// plan carries ("Classic" in "Classic Box"), else none.
function boxFamily(week, sameMeals, summary, people) {
  const selling = new Set(
    sameMeals.filter((o) => o.family_handle && positiveInt(o.servings) === people).map((o) => o.family_handle)
  );
  if (selling.size === 1) return [...selling][0];
  const names = [week && week.display_name, summary && summary.selected_plan].map(lower).filter(Boolean);
  const pool = selling.size ? sameMeals.filter((o) => selling.has(o.family_handle)) : sameMeals;
  const named = pool.find((o) => {
    const family = lower(o.family_name);
    return o.family_handle && family && names.some((name) => name.includes(family) || family.includes(name));
  });
  return named ? named.family_handle : null;
}

// The sizes one week's box can be changed to, one per number of people, fewest first: the boxes
// with its own meal count in its own family, or with its meal count in any family when the family
// can't be told. Each is {servings, meals, handle, price, current}; the price is the whole box in
// major units, or null.
export function servingsChoices(week, options, summary) {
  const meals = positiveInt(week && week.meals_required) || positiveInt(summary && summary.required_meal_count);
  if (!meals) return [];
  const people = weekServings(week, summary);
  const sameMeals = (Array.isArray(options) ? options : []).filter(
    (o) => o && positiveInt(o.meals) === meals && positiveInt(o.servings)
  );
  const family = boxFamily(week, sameMeals, summary, people);
  const own = family ? sameMeals.filter((o) => o.family_handle === family) : [];
  const pool = own.length ? own : sameMeals;
  const bySize = new Map();
  for (const option of pool) {
    const servings = positiveInt(option.servings);
    if (!bySize.has(servings)) bySize.set(servings, option);
  }
  return [...bySize.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([servings, option]) => ({
      servings,
      meals,
      handle: option.handle || null,
      price: amountOf(option.price),
      current: servings === people,
    }));
}

// ---- recipes: the catalog a page at a time ---------------------------------------------------

// One page of hellofresh.get_catalog_recipes: its recipes, the total when HelloFresh says it, and
// whether more follow (`more`). An answer without `has_more` (an integration from before paging)
// can't be paged; a full page of it is still "the first N" for the line above the grid (`capped`).
export function catalogPage(response, limit) {
  const recipes = Array.isArray(response && response.recipes) ? response.recipes.filter(Boolean) : [];
  const total = response && Number.isInteger(response.total) && response.total >= 0 ? response.total : null;
  const paged = Boolean(response) && typeof response.has_more === "boolean";
  return {
    recipes,
    total,
    more: paged && response.has_more,
    capped: paged ? response.has_more : recipes.length >= limit,
  };
}

// The recipes shown with a page added after them, each recipe once: a page can repeat one the last
// page had when the catalog moved in between. `added` is what is new.
export function addRecipes(shown, page) {
  const list = (shown || []).slice();
  const seen = new Set(list.map((r) => r && r.recipe_id));
  const added = [];
  for (const recipe of page || []) {
    if (!recipe || seen.has(recipe.recipe_id)) continue;
    seen.add(recipe.recipe_id);
    added.push(recipe);
  }
  return { list: list.concat(added), added };
}

// The line above a catalog list or search, as {key, params}, or null for a whole category with
// nothing more to say: "Showing 24 of 130" once HelloFresh gives the total, "Showing the first 24"
// when there is more but no total, and a search's own count otherwise.
export function catalogNote({ count, total = null, capped = false, search = false }) {
  if (total !== null && total > count) {
    return { key: search ? "recipes.results_of" : "recipes.shown_of", params: { count, total: fmtNumber(total) } };
  }
  if (capped && total === null) return { key: search ? "recipes.results_capped" : "recipes.capped", params: { count } };
  return search ? { key: "recipes.results", params: { count } } : null;
}

// ---- undo ------------------------------------------------------------------------------------
//
// A change that went through can be taken back for a moment: its success toast offers Undo, which
// makes the reverse write with the same service. HelloFresh takes a change to a week only until
// that week's selection deadline, so the offer stands only until then.

// How long the toast offers Undo.
export const UNDO_MS = 10000;

export function undoWindowOpen(week, now = Date.now()) {
  if (!week) return false;
  const deadline = week.selection_deadline ? Date.parse(week.selection_deadline) : NaN;
  if (Number.isFinite(deadline)) return deadline > now;
  return isUpcoming(week, new Date(now));
}

// The write that puts a week's box back as it was saved before a save (`before`: its meals and
// extras then; `changed`: which of the two the save changed). The meals changed: ONE select_meals
// carrying the old meals, servings and Market quantities, the whole old box in one cart write.
// Only the extras changed: select_market_items with the old quantities. Null when the old box
// can't be written back, being smaller than HelloFresh's smallest.
export function boxUndoWrite(week, before, changed) {
  if (!week || !before || !changed || !(changed.meals || changed.market)) return null;
  const meals = before.meals || new Map();
  const market = before.market || new Map();
  if (changed.meals) {
    const write = buildMealWrite(week, meals);
    if (write.recipe_ids.length < MIN_MEALS) return null;
    const data = { week_id: week.week_id, recipe_ids: write.recipe_ids };
    if (Object.keys(write.quantities).length) data.quantities = write.quantities;
    data.market_quantities = buildMarketWrite(market);
    return { service: "select_meals", data };
  }
  return { service: "select_market_items", data: { week_id: week.week_id, quantities: buildMarketWrite(market) } };
}

// The one-off delivery option a week's box is on now (the option for its current date), so a new
// day can be changed back to it. Null when the week lists none for that date.
export function currentDeliveryOption(week) {
  return deliveryOptionForDate(week, week && week.delivery_date);
}

// The handle of a week's one-off delivery option for `date` ("2026-10-13"), or null.
// HelloFresh lists a week's other days as its options, so the day a box is on usually shows up
// among them only once the box has moved away from it: the Undo of a new delivery day looks
// for the old day in the week as it reads after the move.
export function deliveryOptionForDate(week, date) {
  if (!week || !date) return null;
  const option = (week.available_one_off_options || []).find(
    (o) => o && o.handle && o.delivery_date && o.delivery_date === date
  );
  return option ? option.handle : null;
}

// ---- week plan (the Week tab) -------------------------------------------------------------------
//
// The plan itself is the integration's (get_meal_plan): days keyed by date, each holding up to
// three meals, each meal its courses plus a computed start and end. These helpers decide what
// the Week tab offers and shows; the integration validates every write again.

export const PLAN_MEALS = ["breakfast", "lunch", "dinner"];
export const PLAN_COURSES = ["main", "appetizer", "dessert"];
// Market groups that are not food for the household: pet food and charity donations.
export const PLAN_EXCLUDED_GROUPS = new Set(["petstable", "donation"]);
// Where a Market group is offered first; anything else (proteins, Good Chop, …) is a dinner main.
const PLAN_GROUP_SLOTS = {
  breakfast: { meal: "breakfast", course: "main" },
  lunch: { meal: "lunch", course: "main" },
  appetizer: { meal: "dinner", course: "appetizer" },
  dessert: { meal: "dinner", course: "dessert" },
};

// "2026-10-12" for a local date.
export function isoDay(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

// ---- Home Assistant's clock ---------------------------------------------------------------------
//
// The plan belongs to Home Assistant: the integration keys its days by Home Assistant's calendar
// and writes each meal's start and end as that zone's wall clock. A browser somewhere else (a
// phone on a trip, a tablet set to another zone) must read them the same way, so "today" and the
// plan's times come from hass.config.time_zone, not from the browser. Until a card has passed
// hass in, or if the browser doesn't know the zone, the browser's own clock stands in.

export function haTimeZone() {
  return I18n.timeZone();
}

// "2026-10-14": today on Home Assistant's calendar.
export function haToday(now = new Date()) {
  const date = now instanceof Date ? now : new Date(now);
  const zone = I18n.timeZone();
  if (zone) {
    try {
      const parts = new Intl.DateTimeFormat("en-CA", { timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(date);
      const part = (type) => parts.find((p) => p.type === type).value;
      return `${part("year")}-${part("month")}-${part("day")}`;
    } catch (_e) {
      /* a zone this browser doesn't know: its own calendar */
    }
  }
  return isoDay(date);
}

// "6:42 PM" on Home Assistant's clock, for a Date or an ISO datetime (a planned meal's start).
export function fmtHaTime(value) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const options = { hour: "numeric", minute: "2-digit", ...I18n.hourOptions() };
  const zone = I18n.timeZone();
  try {
    if (zone) return date.toLocaleTimeString(I18n.timeLocale(), { ...options, timeZone: zone });
  } catch (_e) {
    /* a zone this browser doesn't know: its own clock */
  }
  try {
    return date.toLocaleTimeString(I18n.timeLocale(), options);
  } catch (_e) {
    return "";
  }
}

// The seven days a week's box covers, starting on its delivery day.
export function planDays(week) {
  if (!week || !week.delivery_date) return [];
  const start = parseLocalDate(week.delivery_date);
  return Array.from({ length: 7 }, (_, i) => isoDay(new Date(start.getFullYear(), start.getMonth(), start.getDate() + i)));
}

// The weeks the Week tab plans: the box being eaten now and every later one.
export function planWeeks(weeks, now = new Date()) {
  const today = haToday(now);
  return (weeks || []).filter((w) => {
    const days = planDays(w);
    return days.length && days[6] >= today;
  });
}

// The week the tab opens on: the one whose seven days include today, else the next one.
export function defaultPlanWeek(weeks, now = new Date()) {
  const today = haToday(now);
  const list = weeks || [];
  return list.find((w) => planDays(w).includes(today)) || list[0] || null;
}

// The cooking time HelloFresh shows on a tile (prep_time_minutes carries it; see matchesDietFilter).
export function headlineMinutes(item) {
  if (!item) return null;
  for (const value of [item.prep_time_minutes, item.total_time_minutes, item.minutes]) {
    const n = Number(value);
    if (Number.isFinite(n) && n > 0) return n;
  }
  return null;
}

// What a week offers the plan: its chosen meals and the Market items bought, each with how many
// times it can be planned. Pet food and donations never appear.
export function planItems(week) {
  if (!week) return [];
  const items = [];
  const seen = new Set();
  for (const r of week.recipes || []) {
    if (!isRecipeSelected(r) || !r.recipe_id || seen.has(r.recipe_id)) continue;
    seen.add(r.recipe_id);
    items.push({
      kind: "meal",
      id: r.recipe_id,
      week_id: week.week_id,
      name: r.name,
      image_url: r.image_url,
      minutes: headlineMinutes(r),
      quantity: recipeQuantity(r),
    });
  }
  for (const m of week.market_items || []) {
    const quantity = marketItemQuantity(m);
    if (quantity <= 0 || PLAN_EXCLUDED_GROUPS.has(m.group_type || "")) continue;
    items.push({
      kind: "market",
      id: m.item_id,
      week_id: week.week_id,
      name: m.name,
      image_url: m.image_url,
      minutes: null,
      quantity,
      group: m.group_type || null,
      recipe_id: m.recipe_id || null,
    });
  }
  return items;
}

// The meal and course an item is offered in first.
export function suggestedSlot(item) {
  if (item && item.kind === "market" && PLAN_GROUP_SLOTS[item.group]) return PLAN_GROUP_SLOTS[item.group];
  return { meal: "dinner", course: "main" };
}

// Whether an item belongs in a slot's first list (the rest sit under "More from this week").
export function itemFits(item, meal, course) {
  const slot = suggestedSlot(item);
  return slot.meal === meal && slot.course === course;
}

export function sameItem(a, b) {
  return Boolean(a && b) && a.kind !== "note" && a.kind === b.kind && a.id === b.id && a.week_id === b.week_id;
}

// A planned meal on a day, or null.
export function planSlot(plan, day, meal) {
  const meals = plan && plan.days && plan.days[day];
  return (meals && meals[meal]) || null;
}

// Every planned course, as { day, meal, course, item }.
export function plannedCourses(plan) {
  const out = [];
  for (const [day, meals] of Object.entries((plan && plan.days) || {})) {
    for (const meal of PLAN_MEALS) {
      const slot = meals && meals[meal];
      if (!slot) continue;
      for (const course of PLAN_COURSES) if (slot[course]) out.push({ day, meal, course, item: slot[course] });
    }
  }
  return out;
}

// How often an item is already planned, and on which days — leaving out one slot (the one
// being edited, whose own choice must not count against it).
export function itemUses(plan, item, skip = null) {
  const days = [];
  for (const c of plannedCourses(plan)) {
    if (skip && c.day === skip.day && c.meal === skip.meal && c.course === skip.course) continue;
    if (sameItem(c.item, item)) days.push(c.day);
  }
  return { count: days.length, days: days.sort() };
}

// What a meal is called: its main, else whatever courses it has.
export function slotTitle(slot) {
  if (!slot) return "";
  if (slot.main && slot.main.name) return slot.main.name;
  return PLAN_COURSES.map((c) => slot[c] && slot[c].name).filter(Boolean).join(", ");
}

// The meals a week shows: dinner always; breakfast and lunch when the options always show them,
// when Market items for them were bought, or when something is already planned for them.
export function planMealsShown(week, plan) {
  const items = planItems(week);
  const days = planDays(week);
  const always = (plan && plan.always_show) || {};
  return PLAN_MEALS.filter((meal) => {
    if (meal === "dinner" || always[meal]) return true;
    if (items.some((i) => i.kind === "market" && i.group === meal)) return true;
    return days.some((day) => planSlot(plan, day, meal));
  });
}

// Box meals (and breakfast or lunch items) not planned as often as they were ordered, with
// how many are left. Appetizers, desserts and proteins stay in the Dinner sheet.
export function trayItems(week, plan) {
  return planItems(week)
    .filter((item) => item.kind === "meal" || item.group === "breakfast" || item.group === "lunch")
    .map((item) => ({ ...item, left: item.quantity - itemUses(plan, item).count }))
    .filter((item) => item.left > 0);
}

// Whether a planned box meal is still chosen in its box. Market items and notes always are; a
// week no longer loaded can't be checked, so it counts as fine.
export function stillInBox(item, weeks) {
  if (!item || item.kind !== "meal") return true;
  const week = (weeks || []).find((w) => w.week_id === item.week_id);
  if (!week) return true;
  return (week.recipes || []).some((r) => r.recipe_id === item.id && isRecipeSelected(r));
}

// Today's planned meals still to come, as { meal, slot }.
export function todayPlan(plan, now = new Date()) {
  const today = haToday(now);
  return PLAN_MEALS.map((meal) => ({ meal, slot: planSlot(plan, today, meal) })).filter(
    ({ slot }) => slot && (!slot.end || new Date(slot.end).getTime() > now.getTime())
  );
}

// "17:30" from a planned meal's ISO start, for a time input. The integration writes the start in
// Home Assistant's zone ("2026-10-14T17:30:00-04:00") and reads an edited time back as that
// zone's wall clock, so the time is taken from the string as written. Converting it to the
// browser's zone first would move the meal by the difference between the two when it is saved.
export function hhmm(iso) {
  const m = /T(\d{2}):(\d{2})/.exec(String(iso ?? ""));
  return m ? `${m[1]}:${m[2]}` : "";
}

// ---- the card's own bookkeeping ----------------------------------------------------------------
//
// Decisions the card element takes that need no DOM, kept here so they run under Node.

// What the configured sections read. A week's whole menu is by far the largest thing the card
// fetches, and the meal plan belongs to the Week tab (the Overview's "Tonight" line only shows
// beside it), so a card without those sections — just Recipes on a kitchen tablet, just Account —
// fetches neither. The summary is small and feeds the header, the banners and Account.
const WEEK_VIEWS = ["overview", "menu", "market", "week"];
export function dataNeeds(views) {
  const list = Array.isArray(views) ? views : [];
  return {
    weeks: list.some((view) => WEEK_VIEWS.includes(view)),
    summary: true,
    mealPlan: list.includes("week"),
  };
}

// Whether a section is drawn from the weeks, so a weeks read that lands has to redraw it. Account
// is (its counters open their week); Recipes isn't, and redrawing it for nothing rebuilt a grid of
// up to 200 photos after every favourite, whose write re-reads the weeks for the Menu's hearts.
const WEEKS_DRAWN_IN = new Set([...WEEK_VIEWS, "account"]);
export function viewShowsWeeks(view) {
  return WEEKS_DRAWN_IN.has(view);
}

// How long after a FAILED load to try again: soon at first (Home Assistant restarting, the
// integration still setting up), then at the usual pace. A failure used to count as a load, so a
// dashboard opened mid-restart kept its error for a whole refresh interval — three hours.
export const RETRY_DELAYS_MS = [15000, 60000, 300000];

// The wait before the next load: the refresh interval after a success, a retry delay after
// `failures` failed loads in a row (never longer than the interval itself).
export function loadDelayMs(failures, intervalMs) {
  const quick = failures > 0 ? RETRY_DELAYS_MS[failures - 1] : undefined;
  return quick === undefined ? intervalMs : Math.min(quick, intervalMs);
}

// A hellofresh.* service that only reads. Everything else the card calls changes the account.
export function isReadService(service) {
  return /^(get|preview)_/.test(String(service || ""));
}

// Writes that change one week's box and nothing else: after one, only that week is re-read
// (get_weeks with week_id) instead of every week with its whole menu.
export const WEEK_WRITE_SERVICES = new Set([
  "select_meals",
  "select_market_items",
  "skip_week",
  "unskip_week",
  "reschedule_week",
  "change_week_servings",
]);

// Writes that leave every week's box as it was (the Week tab's plan is its own payload).
const WEEK_NEUTRAL_SERVICES = new Set(["set_planned_meal", "clear_planned_meal"]);

// Which week to re-read after the writes made since the last read: `noted` is undefined (none
// yet), a week id, or "all". One week's box written, perhaps twice, is that week; a write that
// could have touched more is everything.
export function noteWrite(noted, service, data) {
  if (WEEK_NEUTRAL_SERVICES.has(service)) return noted;
  const weekId = WEEK_WRITE_SERVICES.has(service) && data && data.week_id ? String(data.week_id) : "all";
  return noted === undefined || noted === weekId ? weekId : "all";
}

// A re-read week back in the list it came from (a week the list lacks is added; browsableWeeks
// puts it in order).
export function mergeWeek(weeks, week) {
  const list = (weeks || []).slice();
  const at = list.findIndex((w) => w.week_id === week.week_id);
  if (at >= 0) list[at] = week;
  else list.push(week);
  return list;
}

// ---- one request for several cards ----
//
// Cards on one page (two on a dashboard, one beside the sidebar panel) used to each fetch the
// same account's data at the same moments: page load, every refresh tick, every write a sibling
// announced. A load now joins the same request already in flight for its account.
//
// It must not join a request that began before something changed, whose answer may predate the
// change. Every write and every change seen in Home Assistant's entities moves the account's
// epoch on, and a load only joins a request from the current one.

const sharedLoads = new Map(); // "<account>|<what>" -> { epoch, promise }
const dataEpochs = new Map(); // account -> number
const epochTokens = new Map(); // "<account>|<kind>" -> the last token seen

export function dataEpoch(config) {
  return dataEpochs.get(accountKey(config)) || 0;
}

// Something changed. Several cards seeing the SAME change (one entity update reaches them all)
// pass the same `kind` and `token`, and only the first moves the epoch — the rest can still
// share the request it starts.
export function bumpDataEpoch(config, kind = null, token = null) {
  const account = accountKey(config);
  if (kind !== null) {
    const key = `${account}|${kind}`;
    if (epochTokens.get(key) === token) return;
    epochTokens.set(key, token);
  }
  dataEpochs.set(account, dataEpoch(config) + 1);
}

// Run `start()` (which returns a promise), or join the same load already in flight. `fresh`
// never joins: for a load queued behind another because that one may be out of date.
export function sharedLoad(config, what, start, { fresh = false } = {}) {
  const key = `${accountKey(config)}|${what}`;
  const epoch = dataEpoch(config);
  const running = sharedLoads.get(key);
  if (running && !fresh && running.epoch === epoch) return running.promise;
  const entry = { epoch, promise: null };
  entry.promise = Promise.resolve().then(start);
  sharedLoads.set(key, entry);
  const done = () => {
    if (sharedLoads.get(key) === entry) sharedLoads.delete(key);
  };
  entry.promise.then(done, done);
  return entry.promise;
}

// ---- header, entities, tick ----

// The line under the card's title: the plan ("Classic Plan · 3 meals for 2 people"), its status
// when it isn't active, else the next box's name.
export function cardSubtitle(summary, weeks) {
  const parts = [];
  if (summary) {
    if (summary.selected_plan) parts.push(summary.selected_plan);
    const meals = Number(summary.required_meal_count);
    const people = Number(summary.number_of_people);
    if (meals && people) {
      parts.push(t("card.plan_size", { count: meals, people: t("card.people", { count: people }) }));
    }
    if (summary.subscription_status && lower(summary.subscription_status) !== "active") {
      parts.push(statusLabel(summary.subscription_status));
    }
  }
  if (!parts.length && weeks && weeks.length) {
    const next = nextBoxWeek(weeks).week;
    if (next && next.display_name) parts.push(next.display_name);
  }
  return parts.join(" · ");
}

// One signature per group of the card's entities (`ids` as the card's entities() lists them,
// `stateOf(id)` the entity's state object), so a token countdown ticking never re-renders the
// menu: only a group whose signature moved causes work.
export function entitySignature(ids, stateOf) {
  const sign = (list) =>
    list
      .filter(Boolean)
      .map((id) => {
        const s = stateOf(id);
        return s ? `${id}=${s.state}@${s.last_updated}` : `${id}=-`;
      })
      .join("|");
  const of = ids || {};
  return {
    pantry: sign(of.prep || []),
    plan: sign([of.boxSize, of.deliveryDay]),
    health: sign([of.accessToken, of.refreshToken, of.writeActions, of.payloadShape]),
    tracking: sign([of.trackingPhase, of.trackingEta]),
    mealplan: sign([of.meals]),
  };
}

// The groups that moved between two signatures. The first reading (nothing known before, in any
// group) is not a change.
export function changedEntityGroups(before, after) {
  if (!Object.values(before || {}).some(Boolean)) return [];
  return Object.keys(after).filter((group) => after[group] !== before[group]);
}

// How many of the card's entities are unavailable: all of them while the integration is still
// setting up or reloading. The count going down means it is back.
export function unavailableEntities(ids, stateOf) {
  let count = 0;
  for (const value of Object.values(ids || {})) {
    for (const id of Array.isArray(value) ? value : [value]) {
      const s = id ? stateOf(id) : null;
      if (s && s.state === "unavailable") count += 1;
    }
  }
  return count;
}

// What the weeks still allow, as a string the card's minute tick compares: a deadline passing
// is the one change time alone causes, and the Add buttons, Save and Skip/Unskip must go with it.
export function actionsKey(weeks, now = Date.now()) {
  return (weeks || []).map((w) => `${isWeekEditable(w, now) ? "e" : "-"}${canSkip(w, now) ? "s" : "-"}`).join("");
}

// The "Change day" sheet's rows: the week's one-off options, named and priced from
// get_delivery_options (`catalog`, by handle) when it has them — the date alone otherwise.
export function rescheduleOptions(week, catalog, currency) {
  return ((week && week.available_one_off_options) || [])
    .filter((o) => o && o.handle)
    .map((o) => {
      const meta = (catalog && catalog[o.handle]) || null;
      const named = Boolean(meta && meta.delivery_name);
      return {
        handle: o.handle,
        current: Boolean(o.delivery_date) && o.delivery_date === week.delivery_date,
        name: named ? meta.delivery_name : o.delivery_date ? fmtLongDate(o.delivery_date) : o.handle,
        date: named && o.delivery_date ? fmtDate(o.delivery_date) : "",
        price: meta && Number(meta.price) > 0 ? `+${fmtPrice(meta.price, currency)}` : "",
      };
    });
}

// A pantry item's summary ("Butter — 2 tablespoon (tbsp)", as todo.py writes it) split into the
// name and the amount, with the amount's short form ("2 tbsp").
export function pantryParts(summary) {
  const text = String(summary || "");
  const cut = text.indexOf(" — ");
  const amount = cut >= 0 ? text.slice(cut + 3) : "";
  return { name: cut >= 0 ? text.slice(0, cut) : text, amount, short: shortAmount(amount) };
}

// ---- keeping focus across a re-render ----
//
// The card draws by replacing markup, which destroys the focused element. To put focus back on
// "the same" control it needs a name for it that the new markup shares. Controls say what they do
// in data attributes (data-action plus its arguments), so those are the name; a control without
// any is named by the attributes that identify it. "" means it has no usable name.
const FOCUS_NAMING = ["id", "name", "type", "href", "aria-label", "role"];

// The attributes that name a control, as [name, value] pairs out of all of its attributes.
export function focusNaming(attributes) {
  const pairs = (attributes || []).map(([name, value]) => [String(name), String(value ?? "")]);
  const data = pairs.filter(
    ([name]) => name.startsWith("data-") && name !== "data-scroll-restored" && name !== "data-focus-key"
  );
  return data.length ? data : pairs.filter(([name]) => FOCUS_NAMING.includes(name));
}

export function focusKey(tag, attributes) {
  const named = focusNaming(attributes);
  if (!named.length) return "";
  return `${String(tag || "").toLowerCase()}|${named
    .map(([name, value]) => `${name}=${value}`)
    .sort()
    .join("|")}`;
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
      // config_entry_id since Home Assistant 2026.8; the deprecated config_entries list before.
      const entries = !device ? [] : device.config_entry_id ? [device.config_entry_id] : device.config_entries || [];
      if (!entries.includes(configEntryId)) continue;
    }
    return entry.entity_id;
  }
  return null;
}
