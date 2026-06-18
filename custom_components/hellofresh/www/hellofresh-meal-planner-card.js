/*
 * HelloFresh Meal Planner Card
 * ----------------------------
 * A custom Lovelace card for browsing HelloFresh delivery weeks recipe-by-recipe and
 * (where the week is still editable) changing the meal selection.
 *
 * Why a custom card rather than YAML + built-in cards: per-week recipes are NOT exposed as
 * entity attributes (they would exceed Home Assistant's 16 KB recorder attribute cap), so
 * the data is read on demand from the response-returning `hellofresh.get_weeks` service.
 * Lovelace cards can't bind a response-service result, so this card calls the service
 * directly via hass.callService(..., return_response=true) and renders the result. That also
 * sidesteps the size limit entirely — full recipe detail and images flow through live.
 *
 * Config:
 *   type: custom:hellofresh-meal-planner-card
 *   config_entry_id: <optional>   # required only when multiple HelloFresh accounts exist
 *   title: HelloFresh Meal Planner # optional card header
 *   image_width: 400               # optional Cloudinary resize width for recipe images
 *
 * No build step: this is hand-written ES2020 served straight from the integration's www/
 * directory, registered as a Lovelace resource by the integration at startup.
 */

const CARD_VERSION = "0.1.2";

// HelloFresh recipe images are Cloudinary URLs containing a `/q_auto/` transform segment.
// Inserting a width transform keeps grid thumbnails small/fast instead of loading full-size
// hero JPEGs. Unknown URL shapes are returned unchanged.
function resizedImage(url, width) {
  if (!url || !width) return url;
  return url.replace("/q_auto/", `/q_auto,w_${width}/`);
}

// Preference -> accent color for the little protein dot. Mirrors HelloFresh's own grouping.
const PREFERENCE_COLORS = {
  Poultry: "#f0a202",
  Beef: "#c1432f",
  Pork: "#e6789b",
  Seafood: "#2f8fc1",
  Veggie: "#4caf50",
};

class HelloFreshMealPlannerCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._weeks = null; // cached get_weeks response
    this._cursor = 0; // index into this._weeks
    this._loading = false;
    this._error = null;
    this._busy = false; // a write (select/skip) is in flight
    this._fetched = false; // guard so we only auto-fetch once per hass attach
    this._hass = null;
  }

  setConfig(config) {
    this._config = {
      title: "HelloFresh Meal Planner",
      image_width: 400,
      ...config,
    };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    // Fetch once when hass first becomes available. Re-fetching happens explicitly after a
    // write or via the refresh button — polling on every hass update would hammer the service.
    if (hass && !this._fetched && !this._loading) {
      this._fetched = true;
      this._fetchWeeks();
    }
  }

  getCardSize() {
    return 8;
  }

  static getConfigElement() {
    return document.createElement("hellofresh-meal-planner-card-editor");
  }

  static getStubConfig() {
    return { type: "custom:hellofresh-meal-planner-card" };
  }

  async _fetchWeeks() {
    if (!this._hass) return;
    this._loading = true;
    this._error = null;
    this._render();
    try {
      const data = {};
      if (this._config.config_entry_id) {
        data.config_entry_id = this._config.config_entry_id;
      }
      const result = await this._hass.callService("hellofresh", "get_weeks", data, undefined, false, true);
      const weeks = (result && result.response && result.response.weeks) || [];
      this._weeks = weeks;
      this._cursor = this._defaultCursor(weeks);
    } catch (err) {
      this._error = (err && err.message) || String(err);
    } finally {
      this._loading = false;
      this._render();
    }
  }

  // Land on the first week that still needs/allows a selection, else the first week.
  _defaultCursor(weeks) {
    const idx = weeks.findIndex((w) => this._isEditable(w));
    return idx >= 0 ? idx : 0;
  }

  _isEditable(week) {
    if (!week) return false;
    const actions = week.allowed_actions || {};
    if (actions.mealSwap === false) return false;
    if (week.is_skipped) return false;
    const deadline = week.selection_deadline ? Date.parse(week.selection_deadline) : null;
    if (deadline && deadline < Date.now()) return false;
    return Boolean(actions.mealSwap);
  }

  _step(delta) {
    if (!this._weeks || this._weeks.length === 0) return;
    const count = this._weeks.length;
    this._cursor = (this._cursor + delta + count) % count;
    this._render();
  }

  async _toggleRecipe(week, recipe) {
    if (this._busy || !this._isEditable(week)) return;
    const selected = week.recipes.filter((r) => r.is_selected);
    const isSelected = recipe.is_selected;
    const required = week.meals_required;

    // Optimistic UI: flip locally, then submit. On failure we re-fetch to resync.
    if (isSelected) {
      recipe.is_selected = false;
    } else {
      if (required && selected.length >= required) {
        // At capacity: don't silently swap — ask the user to deselect one first.
        this._flash(`This week takes ${required} meals. Deselect one before adding another.`);
        return;
      }
      recipe.is_selected = true;
    }
    this._render();

    const desired = week.recipes.filter((r) => r.is_selected);
    // Only submit once a full valid selection is in place; partial selections aren't a valid
    // cart for HelloFresh (it requires exactly meals_required).
    if (required && desired.length !== required) {
      return;
    }
    await this._submitSelection(week, desired.map((r) => r.recipe_id));
  }

  async _submitSelection(week, recipeIds) {
    this._busy = true;
    this._render();
    try {
      const data = { week_id: week.week_id, recipe_ids: recipeIds };
      if (this._config.config_entry_id) data.config_entry_id = this._config.config_entry_id;
      await this._hass.callService("hellofresh", "select_meals", data);
      this._flash("Meal selection updated.");
    } catch (err) {
      this._flash(`Selection failed: ${(err && err.message) || err}`, true);
    } finally {
      this._busy = false;
      await this._fetchWeeks(); // resync from the source of truth
    }
  }

  async _toggleSkip(week) {
    if (this._busy) return;
    this._busy = true;
    this._render();
    const service = week.is_skipped ? "unskip_week" : "skip_week";
    try {
      const data = { week_id: week.week_id };
      if (this._config.config_entry_id) data.config_entry_id = this._config.config_entry_id;
      await this._hass.callService("hellofresh", service, data);
    } catch (err) {
      this._flash(`${service} failed: ${(err && err.message) || err}`, true);
    } finally {
      this._busy = false;
      await this._fetchWeeks();
    }
  }

  _flash(message, isError = false) {
    this._toast = { message, isError };
    this._render();
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => {
      this._toast = null;
      this._render();
    }, 4000);
  }

  _render() {
    if (!this.shadowRoot) return;
    const week = this._weeks ? this._weeks[this._cursor] : null;
    this.shadowRoot.innerHTML = `
      <style>${this._styles()}</style>
      <ha-card>
        ${this._config.title ? `<h1 class="card-header">${this._esc(this._config.title)}</h1>` : ""}
        <div class="content">
          ${this._renderBody(week)}
        </div>
        ${this._toast ? `<div class="toast ${this._toast.isError ? "error" : ""}">${this._esc(this._toast.message)}</div>` : ""}
      </ha-card>
    `;
    this._bind();
  }

  _renderBody(week) {
    if (this._loading) return `<div class="state">Loading weeks…</div>`;
    if (this._error) {
      return `<div class="state error">Could not load weeks: ${this._esc(this._error)}</div>
        <div class="actions"><button data-action="refresh">Retry</button></div>`;
    }
    if (!this._weeks || this._weeks.length === 0) {
      return `<div class="state">No delivery weeks found.</div>
        <div class="actions"><button data-action="refresh">Refresh</button></div>`;
    }
    return `${this._renderHeader(week)}${this._renderGrid(week)}`;
  }

  _renderHeader(week) {
    const editable = this._isEditable(week);
    const required = week.meals_required;
    const selected = (week.recipes || []).filter((r) => r.is_selected).length;
    const deadline = week.selection_deadline ? new Date(week.selection_deadline) : null;
    const rel = this._relativeWeek(week);
    return `
      <div class="header">
        <button class="nav" data-action="prev" aria-label="Previous week">‹</button>
        <div class="weekinfo">
          <div class="weektitle">${this._esc(week.display_name || week.week_id)}</div>
          <div class="weeksub">
            ${week.delivery_date ? `${this._esc(this._fmtDate(week.delivery_date))}` : ""}
            ${rel ? ` · ${this._esc(rel)}` : ""}
            ${week.is_skipped ? ` · <span class="skipped">Skipped</span>` : ""}
          </div>
        </div>
        <button class="nav" data-action="next" aria-label="Next week">›</button>
      </div>
      <div class="statusrow">
        <span class="chip ${required && selected >= required ? "ok" : "warn"}">
          ${selected}${required ? `/${required}` : ""} chosen
        </span>
        ${deadline ? `<span class="chip">Deadline ${this._esc(this._fmtDateTime(deadline))}</span>` : ""}
        <span class="chip ${editable ? "editable" : "locked"}">${editable ? "Editable" : "Locked"}</span>
        ${week.allowed_actions && week.allowed_actions.mealSwap !== undefined
          ? `<button class="skipbtn" data-action="skip" ${this._busy ? "disabled" : ""}>${week.is_skipped ? "Unskip week" : "Skip week"}</button>`
          : ""}
        <button class="skipbtn" data-action="refresh" ${this._busy ? "disabled" : ""}>↻</button>
      </div>
    `;
  }

  _renderGrid(week) {
    const recipes = week.recipes || [];
    if (recipes.length === 0) {
      return `<div class="state">No menu available for this week yet.</div>`;
    }
    const editable = this._isEditable(week);
    // Selected meals first, otherwise keep the menu's order. Array.prototype.sort is stable
    // in modern engines, so unselected recipes retain their original relative order.
    const ordered = recipes
      .slice()
      .sort((a, b) => (b.is_selected ? 1 : 0) - (a.is_selected ? 1 : 0));
    const cards = ordered
      .map((r) => {
        const color = PREFERENCE_COLORS[r.preference] || "var(--secondary-text-color)";
        const cals = r.calories_kcal ? `${Math.round(r.calories_kcal)} kcal` : "";
        return `
          <div class="recipe ${r.is_selected ? "selected" : ""} ${editable ? "editable" : ""}"
               data-index="${this._esc(String(r.course_index))}">
            <div class="imgwrap">
              ${r.image_url
                ? `<img loading="lazy" src="${this._esc(resizedImage(r.image_url, this._config.image_width))}" alt="${this._esc(r.name)}">`
                : `<div class="noimg"></div>`}
              ${r.is_selected ? `<div class="check">✓</div>` : ""}
            </div>
            <div class="meta">
              <div class="name"><span class="dot" style="background:${color}"></span>${this._esc(r.name)}</div>
              ${r.description ? `<div class="desc">${this._esc(r.description)}</div>` : ""}
              ${cals ? `<div class="cals">${this._esc(cals)}</div>` : ""}
            </div>
          </div>`;
      })
      .join("");
    return `<div class="grid ${this._busy ? "busy" : ""}">${cards}</div>`;
  }

  _bind() {
    const root = this.shadowRoot;
    const week = this._weeks ? this._weeks[this._cursor] : null;
    root.querySelectorAll("[data-action]").forEach((el) => {
      const action = el.getAttribute("data-action");
      el.addEventListener("click", () => {
        if (action === "prev") this._step(-1);
        else if (action === "next") this._step(1);
        else if (action === "refresh") this._fetchWeeks();
        else if (action === "skip" && week) this._toggleSkip(week);
      });
    });
    if (week && this._isEditable(week)) {
      root.querySelectorAll(".recipe.editable").forEach((el) => {
        const index = el.getAttribute("data-index");
        el.addEventListener("click", () => {
          const recipe = (week.recipes || []).find((r) => String(r.course_index) === index);
          if (recipe) this._toggleRecipe(week, recipe);
        });
      });
    }
  }

  // ---- small helpers -------------------------------------------------------

  _relativeWeek(week) {
    if (!week.delivery_date) return "";
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const d = new Date(week.delivery_date);
    d.setHours(0, 0, 0, 0);
    const days = Math.round((d - today) / 86400000);
    if (days === 0) return "today";
    if (days < 0) return days === -1 ? "yesterday" : `${-days} days ago`;
    if (days < 7) return `in ${days} days`;
    const weeks = Math.round(days / 7);
    return weeks === 1 ? "next week" : `in ${weeks} weeks`;
  }

  _fmtDate(iso) {
    try {
      return new Date(iso).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
    } catch (_e) {
      return iso;
    }
  }

  _fmtDateTime(d) {
    try {
      return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
    } catch (_e) {
      return String(d);
    }
  }

  _esc(value) {
    if (value === null || value === undefined) return "";
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  _styles() {
    return `
      ha-card { overflow: hidden; }
      .card-header { padding: 16px 16px 0; }
      .content { padding: 8px 16px 16px; }
      .state { padding: 24px 8px; text-align: center; color: var(--secondary-text-color); }
      .state.error, .toast.error { color: var(--error-color); }
      .header { display: flex; align-items: center; gap: 8px; }
      .header .weekinfo { flex: 1; text-align: center; }
      .weektitle { font-size: 1.2em; font-weight: 600; }
      .weeksub { color: var(--secondary-text-color); font-size: 0.9em; }
      .skipped { color: var(--error-color); font-weight: 600; }
      .nav {
        font-size: 1.6em; line-height: 1; border: none; background: none; cursor: pointer;
        color: var(--primary-text-color); padding: 4px 12px; border-radius: 50%;
      }
      .nav:hover { background: var(--secondary-background-color); }
      .statusrow { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 12px 0; }
      .chip {
        font-size: 0.8em; padding: 4px 10px; border-radius: 14px;
        background: var(--secondary-background-color); color: var(--primary-text-color);
      }
      .chip.ok { background: var(--success-color, #4caf50); color: #fff; }
      .chip.warn { background: var(--warning-color, #ff9800); color: #fff; }
      .chip.locked { opacity: 0.7; }
      .chip.editable { background: var(--primary-color); color: var(--text-primary-color, #fff); }
      .skipbtn {
        margin-left: auto; font-size: 0.85em; padding: 5px 12px; border-radius: 14px;
        border: 1px solid var(--divider-color); background: var(--card-background-color);
        color: var(--primary-text-color); cursor: pointer;
      }
      .skipbtn + .skipbtn { margin-left: 4px; }
      .skipbtn:disabled { opacity: 0.5; cursor: default; }
      .grid {
        display: grid; gap: 12px; margin-top: 4px;
        /* auto-fill spreads the catalog across as many ~180px columns as the card width
           allows. In a panel-mode view (full page width) this becomes a wide multi-column
           grid; in a normal column it falls back to one or two columns. */
        grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
      }
      .grid.busy { opacity: 0.6; pointer-events: none; }
      .recipe {
        border: 2px solid transparent; border-radius: 10px; overflow: hidden;
        background: var(--secondary-background-color); transition: border-color 0.15s, transform 0.1s;
      }
      .recipe.editable { cursor: pointer; }
      .recipe.editable:hover { transform: translateY(-2px); }
      .recipe.selected { border-color: var(--primary-color); }
      .imgwrap { position: relative; aspect-ratio: 1 / 1; background: var(--divider-color); }
      .imgwrap img { width: 100%; height: 100%; object-fit: cover; display: block; }
      .noimg { width: 100%; height: 100%; }
      .check {
        position: absolute; top: 6px; right: 6px; width: 26px; height: 26px;
        background: var(--primary-color); color: var(--text-primary-color, #fff);
        border-radius: 50%; display: flex; align-items: center; justify-content: center;
        font-weight: 700;
      }
      .meta { padding: 8px; }
      .name { font-size: 0.9em; font-weight: 600; display: flex; align-items: baseline; gap: 6px; }
      .dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
      .desc { font-size: 0.8em; color: var(--secondary-text-color); margin-top: 2px; }
      .cals { font-size: 0.75em; color: var(--secondary-text-color); margin-top: 4px; }
      .toast {
        margin: 4px 16px 12px; padding: 8px 12px; border-radius: 8px;
        background: var(--secondary-background-color); font-size: 0.85em; text-align: center;
      }
      .actions { text-align: center; margin-top: 8px; }
      .actions button { padding: 6px 16px; border-radius: 8px; cursor: pointer; }
    `;
  }
}

customElements.define("hellofresh-meal-planner-card", HelloFreshMealPlannerCard);

// Register with Lovelace's card picker.
window.customCards = window.customCards || [];
window.customCards.push({
  type: "hellofresh-meal-planner-card",
  name: "HelloFresh Meal Planner",
  description: "Browse HelloFresh weeks and select meals with images.",
  preview: false,
  documentationURL: "https://github.com/kedube/ha-hellofresh",
});

console.info(`%c HELLOFRESH-MEAL-PLANNER-CARD %c v${CARD_VERSION} `, "color:#fff;background:#91c11e;font-weight:700", "color:#91c11e;background:#fff");
