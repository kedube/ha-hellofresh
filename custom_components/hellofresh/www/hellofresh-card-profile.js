/*
 * HelloFresh card — Food preferences
 * ----------------------------------
 * The food profile HelloFresh uses to auto-pick meals, edited exactly as the classic Food
 * Profile card (and hellofresh.com) edits it — the behaviour is carried over, only the look
 * changes:
 *
 *   * options come from the catalog (hellofresh.get_food_profile), so new ones appear by
 *     themselves; labels and descriptions match the website's copy;
 *   * the protein list follows the diet (seafood for pescatarian, meat-free for vegetarian),
 *     re-seeded fully liked when the set changes, kept when it doesn't;
 *   * personal goals cap at three, at least one cooking style is required, "None" is offered
 *     where HelloFresh allows it;
 *   * weighted fields are Like (+100) / Dislike (−100) / neutral;
 *   * Save writes only taste / household / goals via hellofresh.set_food_profile, and the
 *     dirty check is semantic (undoing an edit really undoes it).
 */

const PROFILE_VERSION = new URL(import.meta.url).searchParams.get("v") || "unknown";
const stamp = encodeURIComponent(PROFILE_VERSION);
const [L, UI] = await Promise.all([
  import(new URL(`./hellofresh-card-logic.js?v=${stamp}`, import.meta.url).href),
  import(new URL(`./hellofresh-card-ui.js?v=${stamp}`, import.meta.url).href),
]);

const { esc, t, ht } = L;
const { icon } = UI;

// The profile's fields, by the key their names have in the card's text (profile.field.<key>).
export const FIELD_KEYS = {
  exclusions: "exclusions",
  primaryProteins: "primary_proteins",
  flavors: "flavors",
  goals: "goals",
  nutritions: "nutritions",
  cuisines: "cuisines",
  dishTypes: "dish_types",
  mealTypes: "meal_types",
  dietaryPreferences: "dietary_preferences",
};

// Option labels: the website's own words where it has them, and a name for every slug HelloFresh
// is known to send (profile.value.<slug>), so they read in the card's language; a slug not seen
// yet falls back to sentence case. Hover text for the cooking styles: profile.value_hint.<slug>.
// Each diet's blurb and protein question: profile.diet_hint.<diet>, profile.protein_question.<diet>.

const GOALS_MAX = 3;

const TASTE_LIST_FIELDS = ["exclusions", "nutritions", "mealTypes"];
const TASTE_WEIGHTED_FIELDS = ["cuisines", "flavors", "dishTypes", "primaryProteins"];
const TASTE_SINGLE_FIELDS = ["dietaryPreferences"];

// Titles: profile.panel.<key>.
const PANELS = [
  {
    key: "dietary_habits",
    icon: "mdi:room-service-outline",
    cards: [
      { section: "taste", field: "exclusions" },
      { section: "taste", field: "primaryProteins" },
      { section: "taste", field: "flavors" },
    ],
  },
  {
    key: "goals",
    icon: "mdi:target",
    cards: [
      { section: "goals", field: "goals" },
      { section: "taste", field: "nutritions" },
    ],
  },
  {
    key: "cooking",
    icon: "mdi:silverware-fork-knife",
    cards: [
      { section: "taste", field: "cuisines" },
      { section: "taste", field: "dishTypes" },
      { section: "taste", field: "mealTypes" },
    ],
  },
];

const LIKE = 100;
const DISLIKE = -100;

// A field's or option's name; an option not named yet reads in sentence case ("low-calorie" ->
// "Low calorie"), the site's own convention for unlabelled options.
export function profileLabel(slug) {
  if (FIELD_KEYS[slug]) return t(`profile.field.${FIELD_KEYS[slug]}`);
  const key = `profile.value.${String(slug).toLowerCase()}`;
  if (L.hasText(key)) return t(key);
  const words = String(slug).replace(/^QUANTITY_/, "").replace(/[_-]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

// Optional text keyed by an option or diet slug (a hint, a blurb, a question), or "".
function optionalText(group, slug) {
  const key = `profile.${group}.${String(slug || "").toLowerCase()}`;
  return slug && L.hasText(key) ? t(key) : "";
}

// Semantic fingerprint for the dirty check: empty containers dropped, lists sorted (they're
// sets), keys sorted — so un-liking the only liked cuisine is no longer "dirty".
export function profileFingerprint(value) {
  const norm = (v) => {
    if (Array.isArray(v)) {
      const arr = v.map(norm).filter((x) => x !== undefined);
      return arr.length ? arr.sort() : undefined;
    }
    if (v && typeof v === "object") {
      const out = {};
      for (const k of Object.keys(v).sort()) {
        const nv = norm(v[k]);
        if (nv !== undefined) out[k] = nv;
      }
      return Object.keys(out).length ? out : undefined;
    }
    return v;
  };
  return JSON.stringify(norm(value) ?? {});
}

export class ProfileEditor {
  constructor(card) {
    this.card = card;
    this.loading = false;
    this.busy = false;
    this.error = null;
    this.fetched = false;
    this.options = null;
    this.saved = null;
    this.draft = null;
    this.completion = null;
    this.expanded = new Set();
    this._seeded = false;
  }

  // ---- data ----------------------------------------------------------------------------------

  async fetch() {
    const card = this.card;
    if (!card.hass || this.loading) return;
    this.fetched = true;
    this.loading = true;
    this.error = null;
    card.renderView();
    try {
      const response = await card.call("get_food_profile");
      this.options = response.options || {};
      this.saved = response.profile || { taste: {}, household: {}, goals: {} };
      this.completion = response.completion || null;
      this.draft = this._clone(this.saved);
      this._seedExpansion();
    } catch (err) {
      this.error = (err && err.message) || String(err);
    } finally {
      this.loading = false;
      card.renderView();
    }
  }

  // Another card (or this one) wrote account data: re-read, never over unsaved edits.
  onRefresh() {
    if (!this.fetched || this.loading || this.busy || this.isDirty()) return;
    this.fetch();
  }

  async save() {
    const card = this.card;
    if (!card.hass || !this.draft || this.validationErrors().length) return;
    this.busy = true;
    card.renderView();
    try {
      const response = await card.call("set_food_profile", this.buildChanges());
      const saved = (response && response.profile) || null;
      this.saved = saved || this._clone(this.draft);
      this.draft = this._clone(this.saved);
      card.broadcastDataChanged();
      card.toast(t("profile.saved"));
    } catch (err) {
      card.toast(t("profile.save_failed", { error: (err && err.message) || err }), true);
    } finally {
      this.busy = false;
      card.renderView();
    }
  }

  buildChanges() {
    const t = this.draft.taste || {};
    const taste = {};
    for (const f of TASTE_LIST_FIELDS) if (Array.isArray(t[f])) taste[f] = t[f];
    for (const f of TASTE_SINGLE_FIELDS) if (Array.isArray(t[f])) taste[f] = t[f];
    for (const f of TASTE_WEIGHTED_FIELDS) if (t[f] && typeof t[f] === "object") taste[f] = t[f];
    const household = this.draft.household || {};
    const goals = this.draft.goals || {};
    return {
      taste,
      household: {
        adults: Number(household.adults) || 0,
        children: Number(household.children) || 0,
      },
      goals: { goals: Array.isArray(goals.goals) ? goals.goals : [] },
    };
  }

  _clone(p) {
    return JSON.parse(JSON.stringify(p || { taste: {}, household: {}, goals: {} }));
  }

  isDirty() {
    if (!this.saved || !this.draft) return false;
    return profileFingerprint(this.saved) !== profileFingerprint(this.draft);
  }

  // ---- edits ---------------------------------------------------------------------------------

  _list(section, field) {
    const container = (this.draft && this.draft[section]) || {};
    return Array.isArray(container[field]) ? container[field] : [];
  }

  _listMax(section, field) {
    return section === "goals" && field === "goals" ? GOALS_MAX : null;
  }

  _toggleList(section, field, value) {
    const container = this.draft[section] || (this.draft[section] = {});
    const list = Array.isArray(container[field]) ? container[field].slice() : [];
    const i = list.indexOf(value);
    if (i >= 0) list.splice(i, 1);
    else if (this._listMax(section, field) != null && list.length >= this._listMax(section, field)) return;
    else list.push(value);
    container[field] = list;
  }

  // The protein list for a diet: the catalog's per-diet group (seafood, meat-free, …) or the
  // flat omnivore set.
  proteinOptions(diet) {
    const chosen = diet !== undefined ? diet : this._list("taste", "dietaryPreferences")[0];
    const groups = (this.options && this.options.meta && this.options.meta.primaryProteinsGroups) || {};
    const group = chosen ? groups[chosen] : null;
    if (Array.isArray(group) && group.length) return group.slice();
    const flat = (this.options && this.options.taste && this.options.taste.primaryProteins) || [];
    return Array.isArray(flat) ? flat.slice() : [];
  }

  // Switching to a diet with a different protein set starts it fully liked (as the site does);
  // diets sharing a set keep the user's likes.
  applyDietChange(diet) {
    const taste = this.draft.taste || (this.draft.taste = {});
    const before = new Set(this.proteinOptions(taste.dietaryPreferences && taste.dietaryPreferences[0]));
    taste.dietaryPreferences = diet ? [diet] : [];
    const after = this.proteinOptions(diet);
    const same = after.length === before.size && after.every((slug) => before.has(slug));
    if (same) return;
    const map = {};
    for (const slug of after) map[slug] = LIKE;
    taste.primaryProteins = map;
  }

  _cycleWeighted(field, slug, direction) {
    const taste = this.draft.taste || (this.draft.taste = {});
    const map = taste[field] && typeof taste[field] === "object" ? { ...taste[field] } : {};
    const current = map[slug];
    if (direction === "like") map[slug] = current === LIKE ? undefined : LIKE;
    else map[slug] = current === DISLIKE ? undefined : DISLIKE;
    if (map[slug] === undefined) delete map[slug];
    taste[field] = map;
  }

  _stepHousehold(field, delta) {
    const opts = (this.options.household && this.options.household[field]) || [];
    const min = opts.length ? Math.min(...opts) : 0;
    const max = opts.length ? Math.max(...opts) : 99;
    const hh = this.draft.household || (this.draft.household = {});
    const current = Number(hh[field] != null ? hh[field] : opts[0] || 0) || 0;
    hh[field] = Math.max(min, Math.min(max, current + delta));
  }

  validationErrors() {
    if (!this.draft) return [];
    const errors = [];
    const mealTypes = (this.options && this.options.taste && this.options.taste.mealTypes) || [];
    if (Array.isArray(mealTypes) && mealTypes.length && !this._list("taste", "mealTypes").length) {
      errors.push(t("profile.meal_types_required"));
    }
    return errors;
  }

  _incomplete() {
    const c = this.completion;
    return c && Array.isArray(c.incomplete_fields) ? c.incomplete_fields : [];
  }

  _needs(path) {
    return this._incomplete().includes(path);
  }

  // Open what HelloFresh still wants answered, once, so the completion bar leads somewhere.
  _seedExpansion() {
    if (this._seeded) return;
    this._seeded = true;
    for (const panel of PANELS) {
      for (const c of panel.cards) {
        const key = `${c.section}.${c.field}`;
        if (this._needs(key)) this.expanded.add(key);
      }
    }
  }

  _tell(path) {
    return this._needs(path) ? `<span class="fp-tell">${ht("profile.tell_us_more")}</span>` : "";
  }

  _allowsNone(path) {
    const meta = (this.options && this.options.meta) || {};
    return (meta.fieldsWithNone || []).includes(path);
  }

  // ---- render --------------------------------------------------------------------------------

  render() {
    if (!this.fetched && this.card.hass) queueMicrotask(() => this.fetch());
    if (this.loading && !this.draft) {
      return `<div class="hf-skeletons"><div class="hf-skeleton"></div><div class="hf-skeleton"></div></div>`;
    }
    if (this.error && !this.draft) {
      return `<div class="hf-empty">${icon("mdi:cloud-alert-outline")}${ht("profile.load_failed", { error: this.error })}
        <div class="hf-actions" style="justify-content:center;margin-top:12px"><button class="hf-btn" data-action="fp-refresh">${ht("common.try_again")}</button></div></div>`;
    }
    if (!this.options || !this.draft) return `<div class="hf-empty">${ht("profile.none")}</div>`;
    const c = this.completion;
    const completion =
      c && c.total && c.completed < c.total
        ? `<div class="fp-completion"><div class="fp-bar"><span style="width:${Math.max(0, Math.min(100, Number(c.percent) || 0))}%"></span></div>
            <span class="fp-bartext">${ht("profile.completion", { completed: c.completed, total: c.total })}</span></div>`
        : "";
    return `<div class="fp-hero"><h2 class="hf-h2">${ht("profile.hero_title")}</h2>
        <p>${ht("profile.hero_body")}</p>${completion}</div>
      <div class="fp-panels">
        <div class="fp-toppanels">${this._dietPanel()}${this._householdPanel()}</div>
        ${PANELS.map((p) => this._panel(p)).join("")}
      </div>
      ${this._footer()}`;
  }

  _dietPanel() {
    const opts = (this.options.taste && this.options.taste.dietaryPreferences) || [];
    if (!Array.isArray(opts) || !opts.length) return "";
    const current = this._list("taste", "dietaryPreferences")[0] || "";
    const options = opts
      .map((v) => `<option value="${esc(v)}" ${v === current ? "selected" : ""}>${esc(profileLabel(v))}</option>`)
      .join("");
    const desc = optionalText("diet_hint", current);
    return `<section class="fp-panel"><h3 class="fp-paneltitle">${icon("mdi:sprout-outline")}${ht("profile.your_diet")}${this._tell("taste.dietaryPreferences")}</h3>
        <label class="hf-small hf-muted" for="fp-diet">${ht("profile.field.dietary_preferences")}</label>
        <select class="hf-select" id="fp-diet" data-focus-key="fp-diet" data-fp-diet ${this.busy ? "disabled" : ""}>${options}</select>
        ${desc ? `<p class="fp-dietdesc">${esc(desc)}</p>` : ""}</section>`;
  }

  _householdPanel() {
    const hh = this.options.household || {};
    const draft = this.draft.household || {};
    const stepper = (field, label) => {
      const opts = Array.isArray(hh[field]) ? hh[field] : [];
      if (!opts.length) return "";
      const value = Number(draft[field] != null ? draft[field] : opts[0]) || 0;
      const min = Math.min(...opts);
      const max = Math.max(...opts);
      return `<div class="fp-hhrow"><span class="fp-hhlabel">${esc(label)}</span>
          <span class="hf-stepper" role="group" aria-label="${esc(label)}">
            <button data-action="fp-step" data-field="${esc(field)}" data-delta="-1" data-focus-key="fp-step-${esc(field)}-dec" aria-label="${ht("common.fewer")}" ${value <= min || this.busy ? "disabled" : ""}>${icon("mdi:minus")}</button>
            <span class="hf-qty">${value}</span>
            <button data-action="fp-step" data-field="${esc(field)}" data-delta="1" data-focus-key="fp-step-${esc(field)}-inc" aria-label="${ht("common.more")}" ${value >= max || this.busy ? "disabled" : ""}>${icon("mdi:plus")}</button>
          </span></div>`;
    };
    const rows = stepper("adults", t("profile.adults")) + stepper("children", t("profile.kids"));
    if (!rows) return "";
    return `<section class="fp-panel"><h3 class="fp-paneltitle">${icon("mdi:account-group-outline")}${ht("profile.household")}${this._tell("household.totalPeople")}</h3>${rows}</section>`;
  }

  _panel(panel) {
    const cards = panel.cards.map((c) => this._subcard(c.section, c.field)).filter(Boolean).join("");
    if (!cards) return "";
    return `<section class="fp-panel"><h3 class="fp-paneltitle">${icon(panel.icon)}${ht(`profile.panel.${panel.key}`)}</h3>
        <div class="fp-subcards">${cards}</div></section>`;
  }

  _subcard(section, field) {
    const values = field === "primaryProteins" ? this.proteinOptions() : ((this.options[section] || {})[field]) || [];
    if (!Array.isArray(values) || !values.length) return "";
    const key = `${section}.${field}`;
    const open = this.expanded.has(key);
    return `<div class="fp-subcard${open ? " open" : ""}">
        <button class="fp-subhead" data-action="fp-expand" data-key="${esc(key)}" data-focus-key="fp-expand-${esc(key)}" aria-expanded="${open}">
          <span class="fp-subtitle">${esc(profileLabel(field))}${this._tell(key)}</span>${icon(open ? "mdi:chevron-up" : "mdi:chevron-down")}</button>
        ${open ? this._editor(section, field, values) : this._preview(section, field)}
        ${this._notice(section, field)}
      </div>`;
  }

  _notice(section, field) {
    if (section === "taste" && field === "exclusions" && this._list(section, field).length) {
      return `<p class="fp-note">${ht("profile.exclude_notice")}</p>`;
    }
    if (section === "taste" && field === "mealTypes" && !this._list(section, field).length) {
      return `<p class="fp-note error">${ht("profile.meal_types_required")}</p>`;
    }
    return "";
  }

  _selected(section, field) {
    if (TASTE_WEIGHTED_FIELDS.includes(field)) {
      const map = (this.draft.taste && this.draft.taste[field]) || {};
      return Object.keys(map).filter((k) => map[k] === LIKE);
    }
    return this._list(section, field);
  }

  _preview(section, field) {
    const selected = this._selected(section, field);
    if (!selected.length) return `<div class="fp-preview"><span class="fp-pill muted">${ht("profile.none_option")}</span></div>`;
    const shown = selected.slice(0, 5);
    const extra = selected.length - shown.length;
    return `<div class="fp-preview">${shown.map((v) => `<span class="fp-pill">${esc(profileLabel(v))}</span>`).join("")}${
      extra > 0 ? `<span class="fp-pill muted">+${extra}</span>` : ""
    }</div>`;
  }

  _editor(section, field, values) {
    if (TASTE_WEIGHTED_FIELDS.includes(field)) {
      const question =
        field === "primaryProteins"
          ? `<p class="fp-question">${esc(optionalText("protein_question", this._list("taste", "dietaryPreferences")[0]) || t("profile.protein_question.default"))}</p>`
          : "";
      const map = (this.draft.taste && this.draft.taste[field]) || {};
      const tiles = values
        .map((slug) => {
          const liked = map[slug] === LIKE;
          const disliked = map[slug] === DISLIKE;
          const id = `${field}|${slug}`;
          return `<div class="fp-wtile${liked ? " liked" : disliked ? " disliked" : ""}">
              <span class="fp-wname">${esc(profileLabel(slug))}</span>
              <div class="fp-seg">
                <button class="fp-segbtn like${liked ? " on" : ""}" data-action="fp-weight" data-id="${esc(id)}" data-dir="like"
                  data-focus-key="fp-w-like-${esc(id)}" aria-pressed="${liked}" ${this.busy ? "disabled" : ""}>${icon("mdi:heart")}${ht("profile.like")}</button>
                <button class="fp-segbtn dislike${disliked ? " on" : ""}" data-action="fp-weight" data-id="${esc(id)}" data-dir="dislike"
                  data-focus-key="fp-w-dislike-${esc(id)}" aria-pressed="${disliked}" ${this.busy ? "disabled" : ""}>${icon("mdi:close")}${ht("profile.dislike")}</button>
              </div></div>`;
        })
        .join("");
      return `<div class="fp-editor">${question}<div class="fp-wgrid">${tiles}</div></div>`;
    }
    const selected = this._list(section, field);
    const max = this._listMax(section, field);
    const maxed = max != null && selected.length >= max;
    const hint = max != null ? `<p class="fp-question">${ht("profile.goals_hint", { max })}</p>` : "";
    const none = this._allowsNone(`${section}.${field}`)
      ? `<button class="hf-chip${selected.length === 0 ? " on" : ""}" data-action="fp-none" data-id="${esc(`${section}|${field}`)}"
          data-focus-key="fp-none-${esc(`${section}|${field}`)}" aria-pressed="${selected.length === 0}" ${this.busy ? "disabled" : ""}>${ht("profile.none_option")}</button>`
      : "";
    const chips = values
      .map((v) => {
        const on = selected.includes(v);
        const blocked = maxed && !on;
        const id = `${section}|${field}|${v}`;
        const hintText = optionalText("value_hint", v);
        const tip = hintText ? ` title="${esc(hintText)}"` : "";
        return `<button class="hf-chip fp-chip${on ? " on" : ""}${blocked ? " blocked" : ""}" data-action="fp-list" data-id="${esc(id)}"
            data-focus-key="fp-list-${esc(id)}" aria-pressed="${on}"${tip}${blocked ? ' aria-disabled="true"' : ""} ${this.busy ? "disabled" : ""}>${esc(profileLabel(v))}</button>`;
      })
      .join("");
    return `<div class="fp-editor">${hint}<div class="hf-chiprow">${none}${chips}</div></div>`;
  }

  _footer() {
    const dirty = this.isDirty();
    const errors = this.validationErrors();
    const note = errors.length ? errors[0] : t(dirty ? "profile.save_scope" : "profile.up_to_date");
    // Pinned to the bottom of the screen only while there is something to save.
    return `<div class="fp-footer${dirty ? " sticky" : ""}">
        <span class="fp-footnote${errors.length ? " error" : ""}">${esc(note)}</span>
        <button class="hf-btn ghost" data-action="fp-refresh" ${this.busy ? "disabled" : ""}>${icon("mdi:refresh")}${ht("profile.reload")}</button>
        <button class="hf-btn" data-action="fp-reset" ${!dirty || this.busy ? "disabled" : ""}>${ht("profile.reset")}</button>
        <button class="hf-btn primary" data-action="fp-save" ${!dirty || errors.length || this.busy ? "disabled" : ""}>${
          ht(this.busy ? "profile.saving" : "profile.save")
        }</button>
      </div>`;
  }

  // ---- events --------------------------------------------------------------------------------

  // Returns true when the event was the editor's.
  onClick(_ev, el) {
    if (!el) return false;
    const action = el.getAttribute("data-action");
    if (!action || !action.startsWith("fp-")) return false;
    if (el.disabled || (this.busy && action !== "fp-expand")) return true;
    const id = el.getAttribute("data-id") || "";
    switch (action) {
      case "fp-expand": {
        const key = el.getAttribute("data-key");
        if (this.expanded.has(key)) this.expanded.delete(key);
        else this.expanded.add(key);
        break;
      }
      case "fp-step":
        this._stepHousehold(el.getAttribute("data-field"), Number(el.getAttribute("data-delta")) || 0);
        break;
      case "fp-weight": {
        const [field, slug] = id.split("|");
        this._cycleWeighted(field, slug, el.getAttribute("data-dir"));
        break;
      }
      case "fp-none": {
        const [section, field] = id.split("|");
        const container = this.draft[section] || (this.draft[section] = {});
        container[field] = [];
        break;
      }
      case "fp-list": {
        const [section, field, value] = id.split("|");
        this._toggleList(section, field, value);
        break;
      }
      case "fp-save":
        this.save();
        return true;
      case "fp-reset":
        this.draft = this._clone(this.saved);
        break;
      case "fp-refresh":
        if (this.isDirty() && !window.confirm(t("profile.discard_confirm"))) return true;
        this.fetch();
        return true;
      default:
        return false;
    }
    this.card.renderView();
    return true;
  }

  onChange(ev) {
    const select = ev.target.closest("[data-fp-diet]");
    if (!select) return false;
    this.applyDietChange(select.value);
    this.card.renderView();
    return true;
  }
}
