/*
 * HelloFresh Food Profile Card
 * ----------------------------
 * A custom Lovelace card for viewing and editing the HelloFresh "food profile" — the set of
 * preferences HelloFresh uses to automatically pre-select meals for upcoming weeks.
 *
 * The profile and the full catalog of selectable options are read on demand from the
 * response-returning `hellofresh.get_food_profile` service (the profile is not part of the
 * regular sensor poll) and saved back via `hellofresh.set_food_profile`.
 *
 * Field kinds (driven entirely by the options catalog, so new options appear automatically):
 *   • list fields   — multi-select chips: exclusions, nutritions, mealTypes, goals.
 *   • single field  — pick one: dietaryPreferences.
 *   • weighted      — tri-state (Like / neutral / Dislike): cuisines, flavors, dishTypes,
 *                     primaryProteins. Stored as {slug: +100 | -100}; neutral = absent.
 *   • household     — − N + steppers for adults / children.
 *
 * Config:
 *   type: custom:hellofresh-food-profile-card
 *   config_entry_id: <optional>     # required only when multiple HelloFresh accounts exist
 *   title: Food Profile             # optional card header
 *   logo: true                      # optional bundled HelloFresh logo in the header
 *
 * No build step: hand-written ES2020 served from the integration's www/ directory.
 */

// The integration stamps its release version onto the resource URL as ?v= (cache-bust),
// so the banner reports exactly which build the browser actually loaded.
const FOOD_PROFILE_CARD_VERSION = new URL(import.meta.url).searchParams.get("v") || "unknown";

// Sub-card titles, matching the labels HelloFresh's web food-profile page uses (which differ
// from the raw API slugs, e.g. exclusions -> "Exclude", goals -> "Personal").
const FIELD_LABELS = {
  exclusions: "Exclude",
  primaryProteins: "Proteins",
  flavors: "Flavors",
  goals: "Personal",
  nutritions: "Nutrition",
  cuisines: "Cuisines",
  dishTypes: "Dish types",
  mealTypes: "Cooking styles",
  dietaryPreferences: "Diet type",
};

// Pretty labels for individual option slugs where a plain title-case isn't ideal.
const VALUE_LABELS = {
  "classic-american": "Classic American",
  "main-plus-sides": "Main + sides",
  "quick-easy": "Quick recipes",
  batch: "Batch recipes",
  "chef-style": "Chef-style recipes",
  "family-style": "Family-style recipes",
  "glp1-support": "GLP-1 support",
};

// Short blurb shown under the selected diet type (mirrors HelloFresh's descriptions).
const DIET_DESCRIPTIONS = {
  flexitarian: "A flexible mix of meat, fish, and plant-based meals.",
  "mostly-meat": "Prioritizes meat as the central protein source for every meal.",
  vegetarian: "Meat-free meals built around vegetables, dairy, and plant proteins.",
  pescatarian: "Seafood-forward meals with no meat or poultry.",
};

// How each taste field is edited once a sub-card is expanded.
const TASTE_LIST_FIELDS = ["exclusions", "nutritions", "mealTypes"];
const TASTE_WEIGHTED_FIELDS = ["cuisines", "flavors", "dishTypes", "primaryProteins"];
const TASTE_SINGLE_FIELDS = ["dietaryPreferences"];

// The page's outer panels and the sub-cards inside each, matching the web layout. Each sub-card
// names the section it edits ("taste" / "goals") and the field key within it.
const PANELS = [
  { title: "Dietary habits", icon: "mdi:room-service-outline", cards: [
    { section: "taste", field: "exclusions" },
    { section: "taste", field: "primaryProteins" },
    { section: "taste", field: "flavors" },
  ] },
  { title: "Goals", icon: "mdi:target", cards: [
    { section: "goals", field: "goals" },
    { section: "taste", field: "nutritions" },
  ] },
  { title: "Cooking preferences", icon: "mdi:silverware-fork-knife", cards: [
    { section: "taste", field: "cuisines" },
    { section: "taste", field: "dishTypes" },
    { section: "taste", field: "mealTypes" },
  ] },
];

const LIKE = 100;
const DISLIKE = -100;

class HelloFreshFoodProfileCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.shadowRoot.adoptedStyleSheets = [HelloFreshFoodProfileCard._sheet()];
    this._hass = null;
    this._loading = false;
    this._busy = false;
    this._error = null;
    this._fetched = false;
    this._options = null; // catalog of all selectable values
    this._saved = null; // server's current profile {taste, household, goals}
    this._draft = null; // working copy the user edits
    this._toast = null;
    this._expanded = new Set(); // sub-cards currently expanded for editing (by "section.field")
    this._onDataChanged = (ev) => this._receiveDataChanged(ev);
  }

  setConfig(config) {
    this._config = { title: "Food Profile", ...config };
    this._render();
  }

  connectedCallback() {
    window.addEventListener(HelloFreshFoodProfileCard.DATA_CHANGED_EVENT, this._onDataChanged);
  }

  disconnectedCallback() {
    window.removeEventListener(HelloFreshFoodProfileCard.DATA_CHANGED_EVENT, this._onDataChanged);
    // Drop the pending toast timer so a detached card isn't kept alive only to re-render
    // into a shadow root nobody can see.
    clearTimeout(this._toastTimer);
    // Also drop the toast itself: with the timer cancelled, a toast shown just before
    // disconnect would otherwise repaint forever once the card reconnects.
    this._toast = null;
  }

  static get DATA_CHANGED_EVENT() {
    return "hellofresh-data-changed";
  }

  _accountKey() {
    return (this._config && this._config.config_entry_id) || "default";
  }

  // Another HelloFresh card wrote account data. Re-pull the profile so this card stays
  // current — but never clobber unsaved edits, our own save (busy), or an in-flight fetch.
  _receiveDataChanged(ev) {
    const detail = (ev && ev.detail) || {};
    if ((detail.accountKey || "default") !== this._accountKey()) return;
    if (!this._fetched || this._loading || this._busy || this._isDirty()) return;
    this._fetch();
  }

  // Tell sibling cards a write changed account data, so they re-pull immediately instead of
  // waiting for their next interval refresh (same contract as the planner/market cards).
  _broadcastDataChanged() {
    window.dispatchEvent(
      new CustomEvent(HelloFreshFoodProfileCard.DATA_CHANGED_EVENT, {
        detail: { accountKey: this._accountKey() },
      })
    );
  }

  set hass(hass) {
    this._hass = hass;
    // Follow HA's theme dark-mode flag (not prefers-color-scheme: the two can differ
    // when the user pins a theme) so the brand palette can adapt via :host([dark]).
    this.toggleAttribute("dark", Boolean(hass?.themes?.darkMode));
    if (hass && !this._fetched && !this._loading) {
      this._fetched = true;
      this._fetch();
    }
  }

  getCardSize() {
    return 14;
  }

  static getConfigElement() {
    return document.createElement("hellofresh-food-profile-card-editor");
  }

  static getStubConfig() {
    return { type: "custom:hellofresh-food-profile-card" };
  }

  // ---- data ----------------------------------------------------------------

  async _fetch() {
    // Reentry guard: a refresh tap racing a data-changed refetch would otherwise run two
    // concurrent service calls, and the slower response could clobber the newer state.
    if (!this._hass || this._loading) return;
    this._loading = true;
    this._error = null;
    this._render();
    try {
      const data = {};
      if (this._config.config_entry_id) data.config_entry_id = this._config.config_entry_id;
      const result = await this._hass.callService(
        "hellofresh",
        "get_food_profile",
        data,
        undefined,
        false,
        true
      );
      const response = (result && result.response) || {};
      this._options = response.options || {};
      this._saved = response.profile || { taste: {}, household: {}, goals: {} };
      this._draft = this._cloneProfile(this._saved);
    } catch (err) {
      this._error = (err && err.message) || String(err);
    } finally {
      this._loading = false;
      this._render();
    }
  }

  async _save() {
    if (!this._hass || !this._draft) return;
    this._busy = true;
    this._render();
    try {
      const data = this._buildChanges();
      if (this._config.config_entry_id) data.config_entry_id = this._config.config_entry_id;
      const result = await this._hass.callService(
        "hellofresh",
        "set_food_profile",
        data,
        undefined,
        false,
        true
      );
      const saved = (result && result.response && result.response.profile) || null;
      if (saved) {
        this._saved = saved;
        this._draft = this._cloneProfile(saved);
      } else {
        // No response payload returned: treat the current draft as the new baseline.
        this._saved = this._cloneProfile(this._draft);
      }
      this._broadcastDataChanged();
      this._flash("Food profile saved.");
    } catch (err) {
      this._flash(`Save failed: ${(err && err.message) || err}`, true);
    } finally {
      this._busy = false;
      this._render();
    }
  }

  _resetDraft() {
    this._draft = this._cloneProfile(this._saved);
    this._render();
  }

  // Only send the sections the card edits (taste, household, goals); the client normalizes
  // weighted fields to +/-100 and preserves untouched server-side extras.
  _buildChanges() {
    const t = this._draft.taste || {};
    const taste = {};
    for (const f of TASTE_LIST_FIELDS) if (Array.isArray(t[f])) taste[f] = t[f];
    for (const f of TASTE_SINGLE_FIELDS) if (Array.isArray(t[f])) taste[f] = t[f];
    for (const f of TASTE_WEIGHTED_FIELDS) if (t[f] && typeof t[f] === "object") taste[f] = t[f];
    return {
      taste,
      household: {
        adults: Number(this._draft.household.adults) || 0,
        children: Number(this._draft.household.children) || 0,
      },
      goals: { goals: Array.isArray(this._draft.goals.goals) ? this._draft.goals.goals : [] },
    };
  }

  _cloneProfile(p) {
    return structuredClone(p || { taste: {}, household: {}, goals: {} });
  }

  _isDirty() {
    if (!this._saved || !this._draft) return false;
    return this._fingerprint(this._saved) !== this._fingerprint(this._draft);
  }

  // Semantic fingerprint for the dirty check. Raw JSON comparison sticks "dirty" after edits
  // are undone: un-liking the only liked cuisine leaves taste.cuisines = {} where the saved
  // profile had no key at all, and removing + re-adding a chip reorders its list. Normalize
  // both sides — drop empty containers, sort lists (they're sets) and object keys.
  _fingerprint(value) {
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

  // ---- edit operations (mutate the draft, then re-render) -------------------

  _toggleListValue(section, field, value) {
    const container = this._draft[section] || (this._draft[section] = {});
    const list = Array.isArray(container[field]) ? container[field].slice() : [];
    const i = list.indexOf(value);
    if (i >= 0) list.splice(i, 1);
    else list.push(value);
    container[field] = list;
    this._render();
  }

  // Tri-state cycle for a weighted field: neutral -> like -> dislike -> neutral.
  _cycleWeighted(field, slug, direction) {
    const taste = this._draft.taste || (this._draft.taste = {});
    const map = taste[field] && typeof taste[field] === "object" ? { ...taste[field] } : {};
    const current = map[slug];
    if (direction === "like") {
      map[slug] = current === LIKE ? undefined : LIKE;
    } else {
      map[slug] = current === DISLIKE ? undefined : DISLIKE;
    }
    if (map[slug] === undefined) delete map[slug];
    taste[field] = map;
    this._render();
  }

  // Step a household count by ±1, clamped to the option range from the catalog.
  _stepHousehold(field, delta) {
    const opts = (this._options.household && this._options.household[field]) || [];
    const min = opts.length ? Math.min(...opts) : 0;
    const max = opts.length ? Math.max(...opts) : 99;
    const hh = this._draft.household || (this._draft.household = {});
    const current = Number(hh[field] != null ? hh[field] : (opts[0] || 0)) || 0;
    hh[field] = Math.max(min, Math.min(max, current + delta));
    this._render();
  }

  // ---- rendering -----------------------------------------------------------

  _render() {
    if (!this.shadowRoot) return;
    this._ensureShell();
    const focus = this._captureFocus();
    this._shell.head.innerHTML = `
      ${this._renderLogo()}
      <span class="title-text">${this._esc(this._config ? this._config.title : "Food Profile")}</span>`;
    this._shell.body.innerHTML = this._renderBody();
    this._shell.toast.innerHTML = this._toast
      ? `<div class="toast ${this._toast.isError ? "error" : ""}">${this._esc(this._toast.message)}</div>`
      : "";
    this._restoreFocus(focus);
  }

  // innerHTML re-renders destroy the focused element, dumping keyboard users back to the
  // document after every toggle. Remember the focused control by its data-* identity and
  // re-focus its replacement after the render.
  static get FOCUS_ATTRS() {
    return ["data-expand", "data-step", "data-weight", "data-none", "data-list", "data-action", "data-single-select"];
  }

  _captureFocus() {
    const active = this.shadowRoot.activeElement;
    if (!active) return null;
    const attrs = HelloFreshFoodProfileCard.FOCUS_ATTRS;
    const host = active.closest(attrs.map((a) => `[${a}]`).join(","));
    if (!host) return null;
    for (const attr of attrs) {
      const value = host.getAttribute(attr);
      if (value != null) return { attr, value };
    }
    return null;
  }

  _restoreFocus(target) {
    if (!target) return;
    const el = this._shell.card.querySelector(`[${target.attr}="${CSS.escape(target.value)}"]`);
    if (el && !el.disabled) el.focus();
  }

  _ensureShell() {
    if (this._shell) return;
    const card = document.createElement("ha-card");
    card.innerHTML = `
      <div class="head"></div>
      <div class="body"></div>
      <div class="toastwrap"></div>`;
    this.shadowRoot.appendChild(card);
    this._shell = {
      card,
      head: card.querySelector(".head"),
      body: card.querySelector(".body"),
      toast: card.querySelector(".toastwrap"),
    };
    card.addEventListener("click", (ev) => this._onClick(ev));
    card.addEventListener("change", (ev) => this._onChange(ev));
  }

  _renderLogo() {
    const logo = this._config && this._config.logo;
    if (!logo) return "";
    const logoUrl = logo === true ? "/hellofresh/hellofresh-logo.png" : logo;
    return `<img class="logo" src="${this._esc(logoUrl)}" alt="HelloFresh">`;
  }

  _renderBody() {
    if (this._loading) return `<div class="state">Loading food profile…</div>`;
    if (this._error) {
      return `<div class="state error">Could not load food profile: ${this._esc(this._error)}</div>
        <div class="actions"><button data-action="refresh">Retry</button></div>`;
    }
    if (!this._options || !this._draft) {
      return `<div class="state">No food profile available.</div>
        <div class="actions"><button data-action="refresh">Refresh</button></div>`;
    }
    return `
      <div class="hero">
        <h2 class="herotitle">Meals matched to your taste</h2>
        <p class="herosub">Your food profile helps HelloFresh set your meal picks and sort the menu by
          what's most relevant to you. You'll always have access to the full menu.</p>
      </div>
      <div class="panelgrid top">
        ${this._renderDietPanel()}
        ${this._renderHouseholdPanel()}
      </div>
      ${PANELS.map((p) => this._renderPanel(p)).join("")}
      ${this._renderFooter()}
    `;
  }

  // ---- panels --------------------------------------------------------------

  // "Your diet": a select for the single dietary preference plus its description.
  _renderDietPanel() {
    const opts = (this._options.taste && this._options.taste.dietaryPreferences) || [];
    if (!Array.isArray(opts) || !opts.length) return "";
    const current = this._draftList("taste", "dietaryPreferences")[0] || "";
    const options = opts
      .map((v) => `<option value="${this._esc(v)}" ${v === current ? "selected" : ""}>${this._esc(this._label(v))}</option>`)
      .join("");
    const desc = DIET_DESCRIPTIONS[current] || "";
    return `
      <section class="panel">
        <h2 class="paneltitle">${this._icon("mdi:sprout-outline")}Your diet</h2>
        <div class="dietfield">
          <label class="dietlabel" for="hf-diet-select">Diet type</label>
          <select class="dietselect" id="hf-diet-select" data-single-select="taste|dietaryPreferences">${options}</select>
        </div>
        ${desc ? `<p class="dietdesc">${this._esc(desc)}</p>` : ""}
      </section>`;
  }

  // "Household": − N + steppers for adults and kids.
  _renderHouseholdPanel() {
    const hh = this._options.household || {};
    const draft = this._draft.household || {};
    const stepper = (field, label) => {
      const opts = Array.isArray(hh[field]) ? hh[field] : [];
      if (!opts.length) return "";
      const value = Number(draft[field] != null ? draft[field] : opts[0]) || 0;
      const min = Math.min(...opts);
      const max = Math.max(...opts);
      return `
        <div class="hhrow">
          <span class="hhlabel">${this._esc(label)}</span>
          <div class="stepper">
            <button class="stepbtn" data-step="${this._esc(field)}|dec" ${value <= min ? "disabled" : ""}>−</button>
            <span class="stepval">${value}</span>
            <button class="stepbtn" data-step="${this._esc(field)}|inc" ${value >= max ? "disabled" : ""}>+</button>
          </div>
        </div>`;
    };
    const adults = stepper("adults", "Number of adults");
    const kids = stepper("children", "Number of kids");
    if (!adults && !kids) return "";
    return `
      <section class="panel">
        <h2 class="paneltitle">${this._icon("mdi:account-group-outline")}Household</h2>
        ${adults}${kids}
      </section>`;
  }

  // A grouping panel (Dietary habits, Goals, Cooking preferences) holding sub-cards.
  _renderPanel(panel) {
    const cards = panel.cards
      .map((c) => this._renderSubCard(c.section, c.field))
      .filter(Boolean)
      .join("");
    if (!cards) return "";
    return `
      <section class="panel">
        <h2 class="paneltitle">${this._icon(panel.icon)}${this._esc(panel.title)}</h2>
        <div class="subcards">${cards}</div>
      </section>`;
  }

  // A sub-card: heading + chevron, a collapsed chip preview, and (when expanded) the editor.
  _renderSubCard(section, field) {
    const values = ((this._options[section] || {})[field]) || [];
    if (!Array.isArray(values) || !values.length) return "";
    const key = `${section}.${field}`;
    const open = this._expanded.has(key);
    return `
      <div class="subcard ${open ? "open" : ""}">
        <button class="subhead" data-expand="${this._esc(key)}" aria-expanded="${open ? "true" : "false"}">
          <span class="subtitle">${this._esc(this._label(field))}</span>
          <span class="chev">${this._icon(open ? "mdi:chevron-down" : "mdi:chevron-right")}</span>
        </button>
        ${open ? this._renderEditor(section, field, values) : this._renderPreview(section, field)}
      </div>`;
  }

  // Collapsed state: a single row of green pills for the current selection, truncated with +N.
  _renderPreview(section, field) {
    const selected = this._previewSelection(section, field);
    if (!selected.length) {
      return `<div class="preview"><span class="pill muted">None</span></div>`;
    }
    const MAX = 4;
    const shown = selected.slice(0, MAX);
    const extra = selected.length - shown.length;
    const pills = shown.map((v) => `<span class="pill">${this._esc(this._label(v))}</span>`).join("");
    const more = extra > 0 ? `<span class="pill more">+${extra}</span>` : "";
    return `<div class="preview">${pills}${more}</div>`;
  }

  // The slugs to preview for a field: liked entries for weighted maps, the list for list fields.
  _previewSelection(section, field) {
    if (TASTE_WEIGHTED_FIELDS.includes(field)) {
      const map = (this._draft.taste && this._draft.taste[field]) || {};
      return Object.keys(map).filter((k) => map[k] === LIKE);
    }
    return this._draftList(section, field);
  }

  // Expanded state: the editor appropriate to the field kind.
  _renderEditor(section, field, values) {
    if (TASTE_WEIGHTED_FIELDS.includes(field)) return this._renderWeighted(field, values);
    const allowNone = this._fieldAllowsNone(`${section}.${field}`);
    return this._renderList(section, field, values, allowNone);
  }

  // Multi-select chips backed by a list field (with an optional "None" choice).
  _renderList(section, field, values, allowNone) {
    const selected = this._draftList(section, field);
    const noneChip = allowNone
      ? `<button class="chip ${selected.length === 0 ? "on" : ""}" aria-pressed="${selected.length === 0 ? "true" : "false"}"
           data-none="${this._esc(section)}|${this._esc(field)}">None</button>`
      : "";
    return `<div class="editor"><div class="chips">${noneChip}${this._chips(section, field, values, selected)}</div></div>`;
  }

  _chips(section, field, values, selected) {
    return values
      .map((v) => {
        const on = selected.includes(v);
        return `<button class="chip ${on ? "on" : ""}" aria-pressed="${on ? "true" : "false"}"
          data-list="${this._esc(section)}|${this._esc(field)}|${this._esc(v)}">${this._esc(
          this._label(v)
        )}</button>`;
      })
      .join("");
  }

  // Weighted tri-state, laid out as a responsive grid of tiles. The data-weight contract is
  // unchanged, so editing/saving behave exactly as before.
  _renderWeighted(field, values) {
    const map = (this._draft.taste && this._draft.taste[field]) || {};
    const tiles = values
      .map((slug) => {
        const weight = map[slug];
        const liked = weight === LIKE;
        const disliked = weight === DISLIKE;
        const state = liked ? "liked" : disliked ? "disliked" : "neutral";
        return `
          <div class="wtile ${state}">
            <span class="wname">${this._esc(this._label(slug))}</span>
            <div class="seg">
              <button class="segbtn like ${liked ? "on" : ""}"
                data-weight="${this._esc(field)}|${this._esc(slug)}|like"
                aria-pressed="${liked ? "true" : "false"}">
                ${this._icon("mdi:heart")}<span class="seglabel">Like</span>
              </button>
              <button class="segbtn dislike ${disliked ? "on" : ""}"
                data-weight="${this._esc(field)}|${this._esc(slug)}|dislike"
                aria-pressed="${disliked ? "true" : "false"}">
                ${this._icon("mdi:close")}<span class="seglabel">Dislike</span>
              </button>
            </div>
          </div>`;
      })
      .join("");
    return `<div class="editor"><div class="wgrid">${tiles}</div></div>`;
  }

  _renderFooter() {
    const dirty = this._isDirty();
    return `
      <div class="footer">
        <button class="savebtn" data-action="save" ${!dirty || this._busy ? "disabled" : ""}>
          ${this._busy ? "Saving…" : "Save profile"}
        </button>
        <button class="resetbtn" data-action="reset" ${!dirty || this._busy ? "disabled" : ""}>
          Reset
        </button>
        <button class="resetbtn iconbtn" data-action="refresh" aria-label="Refresh" title="Refresh" ${this._busy ? "disabled" : ""}>${this._icon("mdi:refresh")}</button>
      </div>`;
  }

  // ---- events --------------------------------------------------------------

  _onClick(ev) {
    const expand = ev.target.closest("[data-expand]");
    if (expand) {
      const key = expand.getAttribute("data-expand");
      if (this._expanded.has(key)) this._expanded.delete(key);
      else this._expanded.add(key);
      this._render();
      return;
    }
    // Editor controls are inert while a save is in flight: only the footer buttons carry a
    // disabled attribute, so without this guard a chip toggled mid-save was silently
    // reverted when the save's response overwrote the draft.
    const editing = ev.target.closest("[data-step],[data-weight],[data-none],[data-list]");
    if (editing && this._busy) return;
    const step = ev.target.closest("[data-step]");
    if (step && !step.disabled) {
      const [field, dir] = step.getAttribute("data-step").split("|");
      this._stepHousehold(field, dir === "inc" ? 1 : -1);
      return;
    }
    const weight = ev.target.closest("[data-weight]");
    if (weight) {
      const [field, slug, dir] = weight.getAttribute("data-weight").split("|");
      this._cycleWeighted(field, slug, dir);
      return;
    }
    const none = ev.target.closest("[data-none]");
    if (none) {
      const [section, field] = none.getAttribute("data-none").split("|");
      const container = this._draft[section] || (this._draft[section] = {});
      container[field] = [];
      this._render();
      return;
    }
    const list = ev.target.closest("[data-list]");
    if (list) {
      const [section, field, value] = list.getAttribute("data-list").split("|");
      this._toggleListValue(section, field, value);
      return;
    }
    const actionEl = ev.target.closest("[data-action]");
    if (!actionEl || actionEl.disabled) return;
    const action = actionEl.getAttribute("data-action");
    if (action === "save") this._save();
    else if (action === "reset") this._resetDraft();
    else if (action === "refresh") {
      // Refetching rebuilds the draft from the server; don't silently throw away edits.
      if (this._isDirty() && !window.confirm("Discard unsaved changes and reload the profile?")) return;
      this._fetch();
    }
  }

  _onChange(ev) {
    const sel = ev.target.closest("[data-single-select]");
    if (sel) {
      const [section, field] = sel.getAttribute("data-single-select").split("|");
      const container = this._draft[section] || (this._draft[section] = {});
      container[field] = sel.value ? [sel.value] : [];
      this._render();
    }
  }

  // ---- helpers -------------------------------------------------------------

  _draftList(section, field) {
    const container = this._draft[section] || {};
    return Array.isArray(container[field]) ? container[field] : [];
  }

  _fieldAllowsNone(path) {
    const meta = this._options.meta || {};
    const fields = meta.fieldsWithNone || [];
    return fields.includes(path);
  }

  _label(slug) {
    if (FIELD_LABELS[slug]) return FIELD_LABELS[slug];
    if (VALUE_LABELS[slug]) return VALUE_LABELS[slug];
    return String(slug)
      .replace(/^QUANTITY_/, "")
      .replace(/[_-]+/g, " ")
      .replace(/\b\w/g, (c) => c.toUpperCase());
  }

  _flash(message, isError = false) {
    this._toast = { message, isError };
    this._render();
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => {
      this._toast = null;
      this._render();
    }, 3500);
  }

  // Render a Material Design icon via HA's global <ha-icon>. The icon name is always a
  // hard-coded literal from this file (never user input), so it is safe to inline.
  _icon(name) {
    return `<ha-icon icon="${name}"></ha-icon>`;
  }

  _esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[c]));
  }

  static _sheet() {
    if (!HelloFreshFoodProfileCard.__sheet) {
      const sheet = new CSSStyleSheet();
      sheet.replaceSync(HelloFreshFoodProfileCard._styles());
      HelloFreshFoodProfileCard.__sheet = sheet;
    }
    return HelloFreshFoodProfileCard.__sheet;
  }

  static _styles() {
    // HelloFresh palette: lime-green accent, with panels/tiles on HA theme vars. The brand
    // pill colors flip between a light and dark lime pairing via the host [dark] attribute
    // (set from hass.themes.darkMode) so tiles don't glow light-lime on dark themes.
    return `
      :host {
        --hf-green: #91c11e;
        --hf-pill-bg: #c9ec6a;
        --hf-pill-fg: #1d2b08;
        --hf-panel-bg: var(--ha-card-background, #fbfaf5);
        --hf-card-bg: var(--card-background-color, #fff);
        --hf-hover-brightness: 0.96;
      }
      :host([dark]) {
        --hf-pill-bg: #3a4d10;
        --hf-pill-fg: #c9ec6a;
        --hf-panel-bg: var(--ha-card-background, #1c1c1c);
        --hf-card-bg: var(--card-background-color, #262626);
        --hf-hover-brightness: 1.25;
      }
      ha-card { padding: 16px 16px 16px; background: transparent; }
      .head { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
      .head .logo { height: 40px; width: 40px; border-radius: 8px; object-fit: cover; flex: none; }
      .title-text { font-size: 1.5em; font-weight: 500; }
      .state { text-align: center; padding: 28px 8px; color: var(--secondary-text-color); }
      .state.error { color: var(--error-color, #db4437); }

      .hero { margin: 4px 4px 14px; }
      .herotitle { font-size: 1.35em; font-weight: 800; margin: 0 0 6px; }
      .herosub { color: var(--secondary-text-color); font-size: 0.9em; margin: 0; max-width: 60ch; }

      .panelgrid { display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }
      .panelgrid.top { margin-bottom: 16px; }
      .panel {
        background: var(--hf-panel-bg); border: 1px solid var(--divider-color);
        border-radius: 16px; padding: 18px 18px; margin: 0 0 16px;
      }
      .panelgrid .panel { margin: 0; }
      .paneltitle {
        display: flex; align-items: center; gap: 10px;
        font-size: 1.15em; font-weight: 800; margin: 0 0 14px;
      }
      .paneltitle ha-icon { --mdc-icon-size: 22px; color: var(--hf-green); flex: none; }

      /* "Your diet" */
      .dietfield {
        display: flex; flex-direction: column; gap: 4px;
        border: 1px solid var(--divider-color); border-radius: 10px; padding: 8px 12px;
        background: var(--hf-card-bg);
      }
      .dietlabel { font-size: 0.72em; color: var(--secondary-text-color); }
      .dietselect {
        font: inherit; font-size: 1em; font-weight: 600; border: none; background: transparent;
        color: var(--primary-text-color); padding: 0; cursor: pointer; appearance: auto;
      }
      .dietdesc { color: var(--secondary-text-color); font-size: 0.85em; margin: 10px 2px 0; }

      /* Household steppers */
      .hhrow { display: flex; align-items: center; justify-content: space-between; padding: 8px 0; }
      .hhlabel { font-weight: 600; font-size: 0.95em; }
      .stepper {
        display: flex; align-items: center; gap: 0;
        border: 1px solid var(--divider-color); border-radius: 10px; overflow: hidden;
        background: var(--hf-card-bg);
      }
      .stepbtn {
        font: inherit; font-size: 1.2em; line-height: 1; width: 38px; height: 36px;
        border: none; background: transparent; cursor: pointer; color: var(--primary-text-color);
      }
      .stepbtn:disabled { opacity: 0.35; cursor: default; }
      .stepbtn:not(:disabled):hover { background: var(--secondary-background-color); }
      .stepval { min-width: 32px; text-align: center; font-weight: 700; }

      /* Sub-cards (collapsed preview + expand to edit) */
      .subcards { display: grid; gap: 12px; grid-template-columns: repeat(2, minmax(0, 1fr)); }
      @media (max-width: 600px) { .subcards { grid-template-columns: 1fr; } }
      .subcard {
        background: var(--hf-card-bg); border: 1px solid var(--divider-color);
        border-radius: 12px; padding: 12px 14px;
      }
      .subhead {
        display: flex; align-items: center; justify-content: space-between; width: 100%;
        font: inherit; border: none; background: none; cursor: pointer; padding: 0;
        color: var(--primary-text-color);
      }
      .subtitle { font-weight: 700; font-size: 0.98em; }
      .chev { display: flex; align-items: center; }
      .chev ha-icon { --mdc-icon-size: 22px; color: var(--secondary-text-color); }
      .preview { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
      .pill {
        font-size: 0.78em; font-weight: 700; padding: 4px 10px; border-radius: 6px;
        background: var(--hf-pill-bg); color: var(--hf-pill-fg);
      }
      .pill.more { background: var(--secondary-background-color); color: var(--secondary-text-color); }
      .pill.muted { background: var(--secondary-background-color); color: var(--secondary-text-color); }

      .editor { margin-top: 12px; }
      .chips { display: flex; flex-wrap: wrap; gap: 8px; }
      .chip {
        font: inherit; font-size: 0.82em; font-weight: 600; cursor: pointer;
        padding: 6px 12px; border-radius: 8px; border: 1px solid var(--divider-color);
        background: var(--secondary-background-color); color: var(--primary-text-color);
      }
      .chip:hover { filter: brightness(var(--hf-hover-brightness)); }
      .chip.on { background: var(--hf-pill-bg); color: var(--hf-pill-fg); border-color: var(--hf-pill-bg); }

      /* Weighted items: responsive grid of tiles, each with a Like/Dislike segmented control. */
      .wgrid { display: grid; gap: 10px; grid-template-columns: repeat(auto-fill, minmax(168px, 1fr)); }
      .wtile {
        display: flex; flex-direction: column; gap: 8px;
        padding: 10px 12px; border-radius: 10px;
        border: 1px solid var(--divider-color); background: var(--hf-card-bg);
        transition: border-color 0.15s;
      }
      .wtile.liked { border-color: var(--hf-green); }
      .wtile.disliked { border-color: var(--error-color, #db4437); }
      .wtile .wname { font-size: 0.9em; font-weight: 600; }
      .seg {
        display: grid; grid-template-columns: 1fr 1fr;
        border: 1px solid var(--divider-color); border-radius: 8px; overflow: hidden;
      }
      .segbtn {
        display: flex; align-items: center; justify-content: center; gap: 5px;
        font: inherit; font-size: 0.78em; font-weight: 600; cursor: pointer;
        padding: 6px 4px; border: none; background: var(--secondary-background-color);
        color: var(--secondary-text-color);
      }
      .segbtn + .segbtn { border-left: 1px solid var(--divider-color); }
      .segbtn ha-icon { --mdc-icon-size: 16px; flex: none; }
      .segbtn:hover { filter: brightness(var(--hf-hover-brightness)); }
      /* Dark olive on brand green: white text on #91c11e is ~2:1 contrast, olive passes AA. */
      .segbtn.like.on { background: var(--hf-green); color: #1d2b08; }
      .segbtn.dislike.on { background: var(--error-color, #db4437); color: #fff; }

      .footer { display: flex; align-items: center; gap: 8px; margin-top: 4px; }
      .savebtn {
        font: inherit; font-size: 0.9em; font-weight: 700; cursor: pointer;
        padding: 9px 20px; border-radius: 10px; border: none;
        background: var(--hf-green); color: #1d2b08;
      }
      .savebtn:disabled { opacity: 0.5; cursor: default; }
      .resetbtn {
        font: inherit; font-size: 0.9em; cursor: pointer;
        padding: 9px 14px; border-radius: 10px; border: 1px solid var(--divider-color);
        background: var(--hf-card-bg); color: var(--primary-text-color);
      }
      .resetbtn:disabled { opacity: 0.5; cursor: default; }
      .iconbtn { display: inline-flex; align-items: center; justify-content: center; padding: 9px; }
      .iconbtn ha-icon { --mdc-icon-size: 18px; }
      .toastwrap:empty { display: none; }
      .toast {
        margin: 12px 0 0; padding: 8px 12px; border-radius: 8px;
        background: var(--secondary-background-color); font-size: 0.85em; text-align: center;
      }
      .toast.error { background: var(--error-color, #db4437); color: #fff; }
      .actions { text-align: center; margin-top: 8px; }
      .actions button { padding: 6px 16px; border-radius: 8px; cursor: pointer; }
    `;
  }
}

customElements.define("hellofresh-food-profile-card", HelloFreshFoodProfileCard);

// ---- visual config editor ---------------------------------------------------
// Minimal ha-form-based editor so the card is configurable from the dashboard UI, not just
// YAML. `logo` is a boolean toggle here; a custom logo path set via YAML is preserved as
// long as the toggle stays on.

const EDITOR_SCHEMA = [
  { name: "title", selector: { text: {} } },
  { name: "logo", selector: { boolean: {} } },
  { name: "config_entry_id", selector: { text: {} } },
];
const EDITOR_LABELS = {
  title: "Title",
  logo: "Show HelloFresh logo",
  config_entry_id: "Config entry ID",
};
const EDITOR_HELPERS = {
  config_entry_id: "Only needed when multiple HelloFresh accounts are configured.",
};

class HelloFreshFoodProfileCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = { ...(config || {}) };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (this._form) this._form.hass = hass;
  }

  _render() {
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.schema = EDITOR_SCHEMA;
      this._form.computeLabel = (s) => EDITOR_LABELS[s.name] || s.name;
      this._form.computeHelper = (s) => EDITOR_HELPERS[s.name] || "";
      this._form.addEventListener("value-changed", (ev) => this._onFormChanged(ev));
      this.appendChild(this._form);
    }
    if (this._hass) this._form.hass = this._hass;
    this._form.data = {
      title: this._config.title != null ? this._config.title : "Food Profile",
      logo: Boolean(this._config.logo),
      config_entry_id: this._config.config_entry_id || "",
    };
  }

  _onFormChanged(ev) {
    ev.stopPropagation();
    const value = (ev.detail && ev.detail.value) || {};
    const config = { ...this._config, title: value.title || "Food Profile" };
    if (value.logo) config.logo = typeof this._config.logo === "string" ? this._config.logo : true;
    else delete config.logo;
    if (value.config_entry_id) config.config_entry_id = value.config_entry_id;
    else delete config.config_entry_id;
    this._config = config;
    this.dispatchEvent(
      new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true })
    );
  }
}

customElements.define("hellofresh-food-profile-card-editor", HelloFreshFoodProfileCardEditor);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "hellofresh-food-profile-card",
  name: "HelloFresh Food Profile Card",
  description: "View and edit the preferences HelloFresh uses to auto-preselect meals.",
});

console.info(
  `%c HELLOFRESH-FOOD-PROFILE-CARD %c v${FOOD_PROFILE_CARD_VERSION} `,
  "color:#fff;background:#91c11e;font-weight:700",
  "color:#91c11e;background:#fff"
);
