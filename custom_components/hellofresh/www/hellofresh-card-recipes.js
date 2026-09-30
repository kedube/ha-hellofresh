/*
 * HelloFresh card — Recipes
 * -------------------------
 * HelloFresh's public recipe catalog (~10,000 recipes) and your cookbook, as in the classic
 * Recipes card: every category HelloFresh publishes, sub-category refinement, catalog-wide text
 * search (the cookbook filters locally instead), favourite hearts and the full recipe sheet.
 *
 * Layout change: the ~60 categories live in one scrollable rail (with an "All categories"
 * expander) instead of a wall of wrapped chips above the grid.
 *
 * Nothing here is tied to delivery weeks; it is fetched on demand, never polled.
 */

const RECIPES_VERSION = new URL(import.meta.url).searchParams.get("v") || "unknown";
const stamp = encodeURIComponent(RECIPES_VERSION);
const [L, UI] = await Promise.all([
  import(new URL(`./hellofresh-card-logic.js?v=${stamp}`, import.meta.url).href),
  import(new URL(`./hellofresh-card-ui.js?v=${stamp}`, import.meta.url).href),
]);

const { esc, t, ht } = L;
const { icon } = UI;

// Sentinel collection for the customer's own cookbook (a different service than the catalog);
// the "@" can't collide with a real category slug.
const COOKBOOK = "@cookbook";
const DEFAULT_LIMIT = 50;

export class RecipesView {
  constructor(card) {
    this.card = card;
    this.collections = [];
    this.subcollections = [];
    this.recipes = [];
    this.collection = null;
    this.loading = false;
    this.error = null;
    this.busyIds = new Set();
    this.query = "";
    this.searchResults = null;
    this.searchLoading = false;
    this._searchSeq = 0;
    this._searchTimer = null;
    this.showAll = false;
    this.fetched = false;
  }

  _limit() {
    return Number(this.card.config.recipes_limit) || DEFAULT_LIMIT;
  }

  async _fetch() {
    const card = this.card;
    if (!card.hass || this.loading) return;
    this.fetched = true;
    this.loading = true;
    this.error = null;
    card.renderView();
    try {
      if (!this.collections.length) {
        const response = await card.call("get_recipe_collections");
        this.collections = response.collections || [];
      }
      if (this.collection === COOKBOOK) {
        const payload = await card.call("get_favorites");
        // The cookbook IS the favourites, so every heart is filled.
        this.recipes = (payload.favorites || []).map((f) => ({ ...f, is_favorite: true }));
        this.subcollections = [];
      } else {
        const payload = await card.call("get_catalog_recipes", {
          ...(this.collection ? { collection: this.collection } : {}),
          limit: this._limit(),
        });
        this.recipes = payload.recipes || [];
        // Nested categories (Noodle -> Ramen/Udon/…) are absent from the top-level list; this
        // second row is the only route to them.
        this.subcollections = payload.subcollections || [];
      }
    } catch (err) {
      this.error = (err && err.message) || String(err);
    } finally {
      this.loading = false;
      if (card.view === "recipes") card.renderView();
    }
  }

  onRefresh() {
    if (this.fetched) this._fetch();
  }

  _select(slug) {
    if (this.loading || slug === this.collection) return;
    this.collection = slug;
    this.recipes = [];
    if (this.query) this._resetSearch();
    this._fetch();
  }

  _scheduleSearch() {
    clearTimeout(this._searchTimer);
    const query = this.query.trim();
    if (!query || this.collection === COOKBOOK) {
      this.searchResults = null;
      this.searchLoading = false;
      return;
    }
    this.searchLoading = true;
    this._searchTimer = setTimeout(() => this._runSearch(query), 350);
  }

  async _runSearch(query) {
    const seq = ++this._searchSeq;
    try {
      const payload = await this.card.call("get_catalog_recipes", { search: query, limit: this._limit() });
      if (seq !== this._searchSeq || this.query.trim() !== query) return;
      this.searchResults = payload.recipes || [];
    } catch (err) {
      if (seq !== this._searchSeq) return;
      this.searchResults = null;
      this.card.toast(t("recipes.search_failed", { error: (err && err.message) || err }), true);
    } finally {
      if (seq === this._searchSeq) {
        this.searchLoading = false;
        if (this.card.view === "recipes") this.card.renderView();
      }
    }
  }

  _resetSearch() {
    clearTimeout(this._searchTimer);
    this._searchSeq += 1;
    this.query = "";
    this.searchResults = null;
    this.searchLoading = false;
  }

  _visible() {
    if (this.query.trim() && this.collection !== COOKBOOK && this.searchResults) return this.searchResults;
    return this.recipes;
  }

  async _toggleFavorite(recipe) {
    const card = this.card;
    if (!card.hass || this.busyIds.has(recipe.recipe_id)) return;
    const makeFavorite = recipe.is_favorite !== true;
    this.busyIds.add(recipe.recipe_id);
    card.renderView();
    try {
      await card.call(makeFavorite ? "add_favorite" : "remove_favorite", { recipe_id: recipe.recipe_id });
      recipe.is_favorite = makeFavorite;
      if (!makeFavorite && this.collection === COOKBOOK) {
        this.recipes = this.recipes.filter((r) => r.recipe_id !== recipe.recipe_id);
      }
      card.toast(t(makeFavorite ? "recipes.saved" : "recipes.removed"));
      // The menu's hearts come from get_weeks, which the favourite write just refreshed.
      card.broadcastDataChanged();
      card.reloadWeeks();
    } catch (err) {
      card.toast(t(makeFavorite ? "recipes.save_failed" : "recipes.remove_failed", { error: (err && err.message) || err }), true);
    } finally {
      this.busyIds.delete(recipe.recipe_id);
      if (card.view === "recipes") card.renderView();
    }
  }

  // ---- render --------------------------------------------------------------------------------

  render() {
    // A configured starting category (the classic Recipes card's `collection`) applies until
    // the user picks another one.
    if (this.collection === null) {
      this.collection = this.card.config.recipes_collection || this.card.config.collection || "";
    }
    if (!this.fetched && this.card.hass) {
      // First visit: load after this render so the skeleton paints immediately.
      queueMicrotask(() => this._fetch());
    }
    const search = `<div class="hf-toolbar"><label class="hf-search" style="max-width:none">
        <span class="sr-only">${ht("recipes.search_label")}</span>${icon("mdi:magnify")}
        <input type="search" data-focus-key="recipe-search" data-input="recipe-search" value="${esc(this.query)}"
          placeholder="${ht(this.collection === COOKBOOK ? "recipes.filter_cookbook" : "recipes.search_catalog")}" spellcheck="false" autocomplete="off">
        ${this.query ? `<button class="hf-iconbtn hf-clear" data-action="clear-search" aria-label="${ht("common.clear_search")}">${icon("mdi:close")}</button>` : ""}
      </label></div>`;
    return `${search}${this._chips()}${this._body()}`;
  }

  _chip(slug, label, extra = "") {
    const on = (this.collection || "") === slug;
    return `<button class="hf-chip${on ? " on" : ""}" data-action="collection" data-slug="${esc(slug)}" aria-pressed="${on}"${extra}>${label}</button>`;
  }

  _chips() {
    const fixed = [
      this._chip("", `${icon("mdi:star-outline")}${ht("recipes.top_rated")}`),
      this._chip(COOKBOOK, `${icon("mdi:heart-outline")}${ht("recipes.cookbook")}`),
    ];
    const cats = this.collections.map((c) => this._chip(c.path || c.slug, esc(c.name)));
    const toggle = this.collections.length
      ? `<button class="hf-chip" data-action="show-all-cats" aria-expanded="${this.showAll}">${icon(
          this.showAll ? "mdi:chevron-up" : "mdi:dots-grid"
        )}${ht(this.showAll ? "recipes.fewer_categories" : "recipes.all_categories")}</button>`
      : "";
    const main = this.showAll
      ? `<div class="hf-chiprow" style="margin-bottom:10px">${fixed.join("")}${toggle}${cats.join("")}</div>`
      : `<div class="hf-rail" data-scroll-key="recipe-cats" style="margin-bottom:10px">${fixed.join("")}${toggle}${cats.join("")}</div>`;
    const subs = this.subcollections.length
      ? `<div class="hf-rail" data-scroll-key="recipe-subcats" style="margin-bottom:12px;align-items:center">
          <span class="hf-eyebrow" style="padding-right:4px">${ht("recipes.refine")}</span>
          ${this.subcollections.map((c) => this._chip(c.path || c.slug, esc(c.name))).join("")}</div>`
      : "";
    return main + subs;
  }

  _body() {
    if (this.error) {
      return `<div class="hf-empty">${icon("mdi:cloud-alert-outline")}${esc(this.error)}
        <div class="hf-actions" style="justify-content:center;margin-top:12px"><button class="hf-btn" data-action="retry">${ht("common.try_again")}</button></div></div>`;
    }
    const query = this.query.trim();
    if (query && this.collection !== COOKBOOK) {
      if (this.searchResults === null) {
        return this.searchLoading
          ? `<div class="hf-skeletons"><div class="hf-skeleton"></div><div class="hf-skeleton"></div><div class="hf-skeleton"></div></div>`
          : `<div class="hf-empty">${ht("recipes.none")}</div>`;
      }
      if (!this.searchResults.length) {
        return `<div class="hf-empty">${icon("mdi:magnify-close")}${ht("recipes.no_match", { query })}</div>`;
      }
      return `<p class="hf-resultnote">${ht("recipes.results", { count: this.searchResults.length })}</p>${this._grid(this.searchResults)}`;
    }
    if (this.loading && !this.recipes.length) {
      return `<div class="hf-skeletons"><div class="hf-skeleton"></div><div class="hf-skeleton"></div><div class="hf-skeleton"></div><div class="hf-skeleton"></div></div>`;
    }
    if (!this.recipes.length) {
      return this.collection === COOKBOOK
        ? `<div class="hf-empty">${icon("mdi:heart-outline")}${ht("recipes.cookbook_empty")}</div>`
        : `<div class="hf-empty">${ht("recipes.none")}</div>`;
    }
    const visible = query
      ? this.recipes.filter((r) => `${r.name || ""}\n${r.headline || ""}`.toLowerCase().includes(query.toLowerCase()))
      : this.recipes;
    if (!visible.length) {
      return `<div class="hf-empty">${ht("recipes.no_saved_match", { query })}</div>`;
    }
    return this._grid(visible);
  }

  _grid(recipes) {
    return `<div class="hf-grid">${recipes.map((r) => this._tile(r)).join("")}</div>`;
  }

  _tile(recipe) {
    const img = L.resizedImage(recipe.image_url, 480);
    const link = L.safeUrl(recipe.url);
    const busy = this.busyIds.has(recipe.recipe_id);
    const fav = recipe.is_favorite === true;
    const stats = [];
    if (recipe.rating) {
      const count = recipe.ratings_count ? ` (${Number(recipe.ratings_count).toLocaleString(L.I18n.numberLocale())})` : "";
      stats.push(`<span class="hf-rating">${icon("mdi:star")}${esc(Number(recipe.rating).toFixed(1))}${esc(count)}</span>`);
    }
    const time = L.formatMinutes(recipe.prep_time_minutes);
    if (time) stats.push(`<span>${icon("mdi:timer-outline")}${esc(time)}</span>`);
    return `<div class="hf-tile" role="button" tabindex="0" data-action="open" data-id="${esc(recipe.recipe_id)}"
        aria-label="${ht("recipe.open", { name: recipe.name })}">
        <div class="hf-media">
          ${img ? `<img loading="lazy" src="${esc(img)}" alt="">` : `<div class="hf-noimg"></div>`}
          <div class="hf-tr"><button class="hf-favbtn${fav ? " on" : ""}" data-action="fav" data-id="${esc(recipe.recipe_id)}"
            ${busy ? "disabled" : ""} aria-pressed="${fav}" aria-label="${ht(fav ? "recipes.remove_aria" : "recipes.save_aria", { name: recipe.name })}"
            title="${ht(fav ? "recipes.remove_title" : "recipes.save_title")}">${icon(busy ? "mdi:dots-horizontal" : fav ? "mdi:heart" : "mdi:heart-outline")}</button></div>
        </div>
        <div class="hf-tbody">
          <div class="hf-tname"><span>${link ? `<a href="${link}" target="_blank" rel="noopener noreferrer">${esc(recipe.name)}</a>` : esc(recipe.name)}</span></div>
          ${recipe.headline ? `<div class="hf-tdesc">${esc(recipe.headline)}</div>` : ""}
          ${stats.length ? `<div class="hf-tmeta" style="margin-top:auto">${stats.join("")}</div>` : ""}
        </div>
      </div>`;
  }

  // ---- events --------------------------------------------------------------------------------

  onInput(ev) {
    const input = ev.target.closest("[data-input='recipe-search']");
    if (!input) return;
    this.query = input.value;
    this._scheduleSearch();
    this.card.renderView();
  }

  onClick(ev, el) {
    if (!el) return;
    const action = el.getAttribute("data-action");
    const id = el.getAttribute("data-id");
    switch (action) {
      case "fav": {
        const recipe = this._visible().find((r) => r.recipe_id === id);
        if (recipe) this._toggleFavorite(recipe);
        break;
      }
      case "open":
        // The name is a real link to hellofresh.com; let it navigate instead of opening the sheet.
        if (!ev.target.closest("a")) this.card.openRecipe(id);
        break;
      case "collection":
        this._select(el.getAttribute("data-slug") || "");
        break;
      case "show-all-cats":
        this.showAll = !this.showAll;
        this.card.renderView();
        break;
      case "clear-search":
        this._resetSearch();
        this.card.renderView();
        break;
      case "retry":
        this._fetch();
        break;
      default:
        break;
    }
  }
}
