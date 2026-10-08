/*
 * HelloFresh card text
 * --------------------
 * The card's words are Home Assistant translations, like every other string the integration
 * shows: English in strings.json (the source) and each language in translations/<code>.json,
 * under `config_panel.card`. Home Assistant hands them over in the user's own language through
 * its frontend/get_translations command, filling any key a language lacks from English.
 *
 * English also ships as a module (hellofresh-i18n-en.js, generated from strings.json by
 * .github/scripts/generate_card_strings.py), so English never waits on Home Assistant.
 *
 *   t("menu.add")                        -> "Add"
 *   t("box.meals", { count: 3 })         -> "3 meals"   (a key with one/other forms is a plural,
 *                                                        chosen by `count` with the language's
 *                                                        own plural rules)
 *   ht("hero.deadline", { when: html(`<strong>${esc(date)}</strong>`) })
 *                                        -> HTML: the text and every value escaped, except values
 *                                           wrapped in html(), which are markup the caller built
 *                                           (and escaped) itself.
 *
 * Once a card passes hass in (useHass), dates, times and numbers follow the Home Assistant
 * profile as well: its language, number format and 12/24-hour clock.
 */

const I18N_VERSION = new URL(import.meta.url).searchParams.get("v") || "unknown";
const { EN } = await import(
  new URL(`./hellofresh-i18n-en.js?v=${encodeURIComponent(I18N_VERSION)}`, import.meta.url).href
);

// How Home Assistant names this integration's card strings in a translations response.
const PREFIX = "component.hellofresh.config_panel.card.";

let strings = EN;
let wanted = null; // the language last asked for
let pending = null; // its load, while one is in flight
let profile = {}; // hass.locale
const listeners = new Set();
const warned = new Set();
const pluralRules = new Map();

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

class Markup {
  constructor(markup) {
    this.markup = String(markup ?? "");
  }

  toString() {
    return this.markup;
  }
}

// Markup for ht(): inserted as it is, so the caller escapes whatever data it holds.
export function html(markup) {
  return new Markup(markup);
}

// The language of the text on show: the catalog says ("meta.language"), so a language Home
// Assistant filled entirely from English counts as English and gets English plural rules.
export function language() {
  return strings["meta.language"] || "en";
}

function pluralCategory(count) {
  const lang = language();
  if (!pluralRules.has(lang)) {
    let rules;
    try {
      rules = new Intl.PluralRules(lang);
    } catch (_e) {
      rules = new Intl.PluralRules("en");
    }
    pluralRules.set(lang, rules);
  }
  return pluralRules.get(lang).select(count);
}

function template(key, params) {
  for (const table of strings === EN ? [EN] : [strings, EN]) {
    if (typeof table[key] === "string") return table[key];
    if (params && typeof params.count === "number" && typeof table[`${key}.other`] === "string") {
      const form = table[`${key}.${pluralCategory(params.count)}`];
      return typeof form === "string" ? form : table[`${key}.other`];
    }
  }
  if (!warned.has(key)) {
    warned.add(key);
    // eslint-disable-next-line no-console
    console.warn(`hellofresh: no card text for "${key}"`);
  }
  return key;
}

function formatCount(count) {
  try {
    return new Intl.NumberFormat(numberLocale()).format(count);
  } catch (_e) {
    return String(count);
  }
}

function render(key, params, forHtml) {
  const text = template(key, params);
  return text
    .split(/(\{\w+\})/)
    .map((part) => {
      const name = /^\{(\w+)\}$/.exec(part);
      if (!name || !params || !(name[1] in params)) return forHtml ? escapeHtml(part) : part;
      const value = params[name[1]];
      if (value instanceof Markup) return value.markup;
      const shown = name[1] === "count" && typeof value === "number" ? formatCount(value) : String(value ?? "");
      return forHtml ? escapeHtml(shown) : shown;
    })
    .join("");
}

// Plain text: for textContent, attributes built by hand, confirm(), or a helper that escapes.
export function t(key, params) {
  return render(key, params, false);
}

// HTML: safe to interpolate into markup or an attribute as it is.
export function ht(key, params) {
  return render(key, params, true);
}

export function has(key) {
  return typeof strings[key] === "string" || typeof EN[key] === "string";
}

// The English text, for matching words HelloFresh itself sends in English.
export function en(key) {
  return EN[key];
}

// ---- Home Assistant's profile ------------------------------------------------------------------

// Load the text for hass's language (a no-op once it's there). True when the text is ready
// now; false while it loads, and onChange listeners hear when it lands.
export function useHass(hass) {
  profile = (hass && hass.locale) || {};
  const lang = String(profile.language || (hass && hass.language) || "en");
  if (lang === wanted) return !pending;
  wanted = lang;
  if (lang === "en" || !hass || typeof hass.callWS !== "function") {
    pending = null;
    if (strings !== EN) {
      strings = EN;
      notify();
    }
    return true;
  }
  const load = hass
    .callWS({ type: "frontend/get_translations", language: lang, category: "config_panel", integration: ["hellofresh"] })
    .then((result) => {
      const next = {};
      for (const [key, value] of Object.entries((result && result.resources) || {})) {
        if (key.startsWith(PREFIX) && typeof value === "string") next[key.slice(PREFIX.length)] = value;
      }
      return Object.keys(next).length ? { ...EN, ...next } : EN;
    })
    .catch((err) => {
      // eslint-disable-next-line no-console
      console.warn("hellofresh: the card's translations didn't load, so it shows English", err);
      return EN;
    })
    .then((next) => {
      if (pending !== load) return; // another language was asked for meanwhile
      pending = null;
      strings = next;
      notify();
    });
  pending = load;
  return false;
}

function notify() {
  for (const listener of listeners) listener();
}

// Hear when the text changes language; returns the unsubscribe function.
export function onChange(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

// For tests: show a catalog (flat keys, as the generated English module has them).
export function setStrings(catalog) {
  wanted = null;
  pending = null;
  strings = catalog === EN || !catalog ? EN : { ...EN, ...catalog };
}

// Dates and times in Home Assistant's language (undefined until a card passes hass in, which
// means the browser's own, as before).
export function dateLocale() {
  return profile.language || undefined;
}

export function timeLocale() {
  return profile.time_format === "system" ? undefined : dateLocale();
}

// Home Assistant's 12/24-hour choice, as Intl options.
export function hourOptions() {
  if (profile.time_format === "am_pm") return { hour12: true };
  if (profile.time_format === "24") return { hour12: false };
  return {};
}

// Numbers and money in Home Assistant's number format (as its own frontend maps it).
export function numberLocale() {
  switch (profile.number_format) {
    case "comma_decimal":
      return ["en-US", "en"];
    case "decimal_comma":
      return ["de", "es", "it"];
    case "space_comma":
      return ["fr", "sv", "cs"];
    case "quote_decimal":
      return ["de-CH"];
    case "system":
      return undefined;
    default:
      return profile.language || undefined;
  }
}
