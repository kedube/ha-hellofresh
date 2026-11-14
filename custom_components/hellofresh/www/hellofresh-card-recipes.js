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
 * A category or a search comes `recipes_limit` recipes at a time: "Load more" below the grid
 * asks for the next page (hellofresh.get_catalog_recipes with `offset`) and adds it.
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
// The most hellofresh.get_catalog_recipes takes for `limit`. A larger `recipes_limit` fails the
// service's validation, and the tab would show that error in place of any recipes.
const MAX_LIMIT = 200;

export class RecipesView {
  constructor(card) {
    this.card = card;
    this.collections = [];
    this.subcollections = [];
    this.recipes = [];
    this.page = null; // the category list's L.catalogPage: its total, and whether more follow
    this.collection = null;
    this.loading = false;
    this.error = null;
    this.busyIds = new Set();
    this.query = "";
    this.searchResults = null;
    this.searchPage = null; // the same for the search results
    this.searchFor = ""; // the text the search results answer
    this.searchLoading = false;
    this.searchError = null; // the last catalog search failed: said where its results would be
    this._searchSeq = 0;
    this._searchTimer = null;
    this._refetch = false; // asked to fetch while a fetch was running: go again when it ends
    // The next page on its way ({status: "loading"}) or failed ({status: "failed", error}).
    // `_moreSeq` moves whenever the list on screen is replaced, and a page asked for before
    // that is dropped when it lands.
    this.more = null;
    this._moreSeq = 0;
    this.showAll = false;
    this.fetched = false;
  }

  // `recipes_limit`, kept within what the service accepts (1 to MAX_LIMIT).
  _limit() {
    const configured = Number(this.card.config.recipes_limit);
    if (!Number.isFinite(configured) || configured === 0) return DEFAULT_LIMIT;
    return Math.max(1, Math.min(MAX_LIMIT, Math.round(configured)));
  }

  async _fetch() {
    const card = this.card;
    if (!card.hass) return;
    if (this.loading) {
      // The fetch in flight is for whatever was chosen when it started. Queue one more for
      // what is chosen now, rather than dropping the click that changed it.
      this._refetch = true;
      if (card.view === "recipes") card.renderView();
      return;
    }
    this.fetched = true;
    this.loading = true;
    this.error = null;
    const collection = this.collection;
    card.renderView();
    try {
      if (!this.collections.length) {
        const response = await card.call("get_recipe_collections");
        this.collections = response.collections || [];
      }
      let recipes;
      let page = null;
      let subcollections = [];
      if (collection === COOKBOOK) {
        const payload = await card.call("get_favorites");
        // The cookbook IS the favourites, so every heart is filled. It comes whole.
        recipes = (payload.favorites || []).map((f) => ({ ...f, is_favorite: true }));
      } else {
        // The first page (offset 0, the service's default).
        const payload = await card.call("get_catalog_recipes", {
          ...(collection ? { collection } : {}),
          limit: this._limit(),
        });
        page = L.catalogPage(payload, this._limit());
        recipes = page.recipes;
        // Nested categories (Noodle -> Ramen/Udon/…) are absent from the top-level list; this
        // second row is the only route to them.
        subcollections = payload.subcollections || [];
      }
      // A category chosen while this was on its way has its own fetch queued; these recipes
      // belong to the one before it.
      if (collection === this.collection) {
        this._dropMore();
        this.recipes = recipes;
        this.page = page;
        this.subcollections = subcollections;
      }
    } catch (err) {
      if (collection === this.collection) this.error = (err && err.message) || String(err);
    } finally {
      this.loading = false;
      if (this._refetch) {
        this._refetch = false;
        this._fetch();
      } else if (card.view === "recipes") card.renderView();
    }
  }

  // The catalog is public: it does not change when the account does, and the card refreshes
  // the account every few minutes on a delivery day and after every write on any HelloFresh
  // card. Only the cookbook, which is the account's, is re-read then.
  onRefresh() {
    if (this.fetched && this.collection === COOKBOOK) this._fetch();
  }

  _select(slug) {
    if (slug === this.collection) return;
    this.collection = slug;
    this._dropMore();
    this.recipes = [];
    this.page = null;
    if (this.query) this._resetSearch();
    this._fetch();
  }

  // The list on screen is being replaced (another category, another search): a next page asked
  // for the old one is dropped when it lands, and its Retry goes.
  _dropMore() {
    this._moreSeq += 1;
    this.more = null;
  }

  // What the body is showing, in a word. Typing redraws it only when this changes.
  _mode() {
    if (!this.query.trim() || this.collection === COOKBOOK) return "list";
    if (this.searchResults === null) return this.searchLoading ? "searching" : "none";
    return "results";
  }

  // The typing paused: search the catalog, or filter the cookbook (which is all here already).
  _search() {
    const query = this.query.trim();
    if (this.collection === COOKBOOK) this._redrawResults();
    else if (query) this._runSearch(query);
  }

  async _runSearch(query) {
    const seq = ++this._searchSeq;
    try {
      const payload = await this.card.call("get_catalog_recipes", { search: query, limit: this._limit() });
      if (seq !== this._searchSeq) return;
      const page = L.catalogPage(payload, this._limit());
      this._dropMore();
      this.searchResults = page.recipes;
      this.searchPage = page;
      this.searchFor = query;
      this.searchError = null;
    } catch (err) {
      if (seq !== this._searchSeq) return;
      // Shown where the results would be, with Try again: "No recipes found" would be untrue.
      this._dropMore();
      this.searchResults = null;
      this.searchPage = null;
      this.searchError = (err && err.message) || String(err);
    } finally {
      if (seq === this._searchSeq) {
        this.searchLoading = false;
        this._redrawResults();
      }
    }
  }

  _resetSearch() {
    clearTimeout(this._searchTimer);
    this._searchSeq += 1;
    this._dropMore();
    this.query = "";
    this.searchResults = null;
    this.searchPage = null;
    this.searchFor = "";
    this.searchLoading = false;
    this.searchError = null;
  }

  // "Load more": the next page of the list on screen (the category, or the search results), the
  // same number of recipes from where it ends. The new ones are added after it, each recipe once,
  // and a screen reader hears how many came. `focused`: the button had the keyboard, which then
  // goes on to the first new recipe (or stays on the button, the Retry that replaces it on a
  // failure). A page for a list that has been replaced meanwhile is dropped.
  async _loadMore(focused = false) {
    const card = this.card;
    const search = this._mode() === "results";
    const list = search ? this.searchResults : this.recipes;
    const page = search ? this.searchPage : this.page;
    if (this.collection === COOKBOOK || !list || !page || !page.more) return;
    if (this.more && this.more.status === "loading") return;
    if (search && (this.searchLoading || this.searchFor !== this.query.trim())) return;
    const seq = ++this._moreSeq;
    const offset = list.length;
    this.more = { status: "loading" };
    this._redrawMore(focused);
    let payload = null;
    let failed = null;
    try {
      payload = await card.call("get_catalog_recipes", {
        ...(search ? { search: this.searchFor } : this.collection ? { collection: this.collection } : {}),
        limit: this._limit(),
        offset,
      });
    } catch (err) {
      failed = (err && err.message) || String(err);
    }
    if (seq !== this._moreSeq) return;
    if (failed) {
      this.more = { status: "failed", error: failed };
      this._redrawMore(focused);
      return;
    }
    const next = L.catalogPage(payload, this._limit());
    const { list: longer, added } = L.addRecipes(list, next.recipes);
    // A page that brings nothing new ends the list, rather than offering the same page again.
    const state = {
      total: next.total !== null ? next.total : page.total,
      more: next.more && added.length > 0,
      capped: next.capped && added.length > 0,
    };
    if (search) {
      this.searchResults = longer;
      this.searchPage = state;
    } else {
      this.recipes = longer;
      this.page = state;
    }
    this.more = null;
    this._redrawResults();
    if (card.view !== "recipes") return;
    card.announce(added.length ? t("recipes.added", { count: added.length }) : t("recipes.none_added"));
    if (!focused) return;
    const host = this._results();
    const first = added.length && host ? host.querySelectorAll(".hf-tile .hf-open")[offset] : null;
    if (first) first.focus({ preventScroll: true });
    else this._focusMore();
  }

  // Redraw the results for the Load more button's new state, keeping the keyboard on it.
  _redrawMore(focused) {
    this._redrawResults();
    if (focused) this._focusMore();
  }

  _focusMore() {
    const host = this._results();
    if (host) UI.focusKeyed(host, ["recipes-more"]);
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
    this._patchHeart(recipe);
    let removed = false;
    try {
      await card.call(makeFavorite ? "add_favorite" : "remove_favorite", { recipe_id: recipe.recipe_id });
      recipe.is_favorite = makeFavorite;
      if (!makeFavorite && this.collection === COOKBOOK) {
        this.recipes = this.recipes.filter((r) => r.recipe_id !== recipe.recipe_id);
        removed = true;
      }
      card.toast(t(makeFavorite ? "recipes.saved" : "recipes.removed"));
      // Other HelloFresh cards re-read on the broadcast. This card's own Menu takes its hearts
      // from get_weeks, which the favourite write just refreshed; without a Menu there is
      // nothing here to re-read.
      card.broadcastDataChanged();
      if (card.hasView("menu")) card.reloadWeeks();
    } catch (err) {
      card.toast(t(makeFavorite ? "recipes.save_failed" : "recipes.remove_failed", { error: (err && err.message) || err }), true);
    } finally {
      this.busyIds.delete(recipe.recipe_id);
      if (removed) this._redrawResults();
      else this._patchHeart(recipe);
    }
  }

  // The area under the chips, in the page; null when this view isn't drawn (or in a test).
  _results() {
    const card = this.card;
    if (card.view !== "recipes" || !card.shadowRoot) return null;
    return card.shadowRoot.querySelector(".js-main .js-results");
  }

  // Redraw the results alone: the search field (and what is being typed into it) and the
  // category chips stay as they are.
  _redrawResults() {
    const card = this.card;
    if (card.view !== "recipes") return;
    const host = this._results();
    if (host) host.innerHTML = this._body();
    else card.renderView();
  }

  // One heart changed: swap that button rather than redraw a grid of up to 200 photos.
  _patchHeart(recipe) {
    const card = this.card;
    if (card.view !== "recipes") return;
    const host = this._results();
    if (!host) {
      card.renderView();
      return;
    }
    const button = host.querySelector(`.hf-favbtn[data-id="${CSS.escape(String(recipe.recipe_id))}"]`);
    if (!button) return; // its tile is no longer on screen
    const hadFocus = card.shadowRoot.activeElement === button;
    const fresh = UI.replaceNode(button, this._heart(recipe));
    if (hadFocus && fresh) fresh.focus({ preventScroll: true });
  }

  // ---- render --------------------------------------------------------------------------------

  render() {
    // A configured starting category (the classic Recipes card's `collection`) applies until
    // the user picks another one.
    if (this.collection === null) {
      this.collection = this.card.config.recipes_collection || this.card.config.collection || "";
    }
    if (!this.fetched && this.card.hass) {
      // First visit: load after this render so the skeleton paints immediately. Marked as
      // fetched now, so a second render before the load starts doesn't ask for another.
      this.fetched = true;
      queueMicrotask(() => this._fetch());
    }
    const cookbook = this.collection === COOKBOOK;
    const search = UI.searchBox("recipe-search", this.query, t(cookbook ? "recipes.filter_cookbook" : "recipes.search_catalog"), {
      label: t("recipes.search_label"),
      wide: true,
    });
    return `<div class="hf-toolbar">${search}</div>${this._chips()}<div class="js-results">${this._body()}</div>`;
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

  _skeletons(count) {
    return `<div class="hf-skeletons">${'<div class="hf-skeleton"></div>'.repeat(count)}</div>`;
  }

  _body() {
    if (this.error) return UI.loadError(this.error, "retry");
    const query = this.query.trim();
    if (query && this.collection !== COOKBOOK) {
      if (this.searchError) return UI.loadError(t("recipes.search_failed", { error: this.searchError }), "retry-search");
      // Nothing to show yet while a search is being typed or is on its way; "No recipes found"
      // is only said once a search has actually answered with nothing.
      if (this.searchResults === null) {
        return this.searchLoading ? this._skeletons(3) : `<div class="hf-empty">${ht("recipes.none")}</div>`;
      }
      if (!this.searchResults.length) {
        return `<div class="hf-empty">${icon("mdi:magnify-close")}${ht("recipes.no_match", { query })}</div>`;
      }
      // While the search for newer text runs, the results on screen are an older search's: no
      // more of those.
      const more = this.searchLoading ? null : this.searchPage;
      return `${this._note(this.searchResults.length, this.searchPage, true)}${this._grid(this.searchResults)}${this._moreRow(more)}`;
    }
    if (this.loading && !this.recipes.length) return this._skeletons(4);
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
    // The cookbook comes whole; a category comes `limit` recipes at a time.
    const page = this.collection === COOKBOOK ? null : this.page;
    return `${this._note(this.recipes.length, page, false)}${this._grid(visible)}${this._moreRow(page)}`;
  }

  // The line above the grid: "Showing 24 of 130 recipes", "Showing the first 24 recipes" when
  // HelloFresh gives no total, a search's count, or nothing for a whole category (L.catalogNote).
  _note(count, page, search) {
    const note = L.catalogNote({ count, total: page ? page.total : null, capped: Boolean(page && page.capped), search });
    return note ? `<p class="hf-resultnote">${ht(note.key, note.params)}</p>` : "";
  }

  // Below the grid: "Load more" while there is more (aria-disabled, not disabled, while its page
  // is on the way, so it keeps the keyboard), or Retry in its place after a page failed.
  _moreRow(page) {
    const more = this.more;
    if (more && more.status === "failed") {
      return `<div class="hf-more-row hf-morefailed"><span class="hf-small hf-muted" role="alert">${ht("recipes.more_failed", {
        error: more.error,
      })}</span><button class="hf-btn" data-action="load-more" data-focus-key="recipes-more">${icon("mdi:refresh")}${ht("common.retry")}</button></div>`;
    }
    if (!page || !page.more) return "";
    const loading = Boolean(more && more.status === "loading");
    return `<div class="hf-more-row"><button class="hf-btn" data-action="load-more" data-focus-key="recipes-more"${
      loading ? ' aria-disabled="true"' : ""
    }>${loading ? `<span class="hf-spinner"></span>${ht("recipes.loading_more")}` : ht("recipes.load_more")}</button></div>`;
  }

  _grid(recipes) {
    return `<div class="hf-grid">${recipes.map((r) => this._tile(r)).join("")}</div>`;
  }

  // While its write is on its way the heart is aria-disabled, not disabled: a disabled button
  // would drop the keyboard's focus. (A second click is ignored in _toggleFavorite.)
  _heart(recipe) {
    const id = esc(recipe.recipe_id);
    const busy = this.busyIds.has(recipe.recipe_id);
    const fav = recipe.is_favorite === true;
    return `<button class="hf-favbtn${fav ? " on" : ""}" data-action="fav" data-id="${id}" data-focus-key="fav:${id}"
        ${busy ? 'aria-disabled="true"' : ""} aria-pressed="${fav}" aria-label="${ht(fav ? "recipes.remove_aria" : "recipes.save_aria", { name: recipe.name })}"
        title="${ht(fav ? "recipes.remove_title" : "recipes.save_title")}">${icon(busy ? "mdi:dots-horizontal" : fav ? "mdi:heart" : "mdi:heart-outline")}</button>`;
  }

  // The tile is a plain container. Its "open" control is the photo, a real button; the heart
  // and the name's link to hellofresh.com sit beside it, not inside another control. (A click
  // elsewhere on the tile still opens the recipe: the container keeps the action for the mouse.)
  _tile(recipe) {
    const id = esc(recipe.recipe_id);
    const img = L.resizedImage(recipe.image_url, 480);
    const link = L.safeUrl(recipe.url);
    const stats = [];
    if (recipe.rating) {
      const count = recipe.ratings_count ? ` (${Number(recipe.ratings_count).toLocaleString(L.I18n.numberLocale())})` : "";
      stats.push(`<span class="hf-rating">${icon("mdi:star")}${esc(Number(recipe.rating).toFixed(1))}${esc(count)}</span>`);
    }
    const time = L.formatMinutes(recipe.prep_time_minutes);
    if (time) stats.push(`<span>${icon("mdi:timer-outline")}${esc(time)}</span>`);
    return `<div class="hf-tile" data-action="open" data-id="${id}">
        <div class="hf-media">
          <button class="hf-open" data-action="open" data-id="${id}" data-focus-key="recipe:${id}" aria-label="${ht("recipe.open", { name: recipe.name })}">
            ${img ? `<img loading="lazy" src="${esc(img)}" alt="">` : `<span class="hf-noimg"></span>`}</button>
          <div class="hf-tr">${this._heart(recipe)}</div>
        </div>
        <div class="hf-tbody">
          <div class="hf-tname"><span>${link ? `<a href="${esc(link)}" target="_blank" rel="noopener noreferrer">${esc(recipe.name)}</a>` : esc(recipe.name)}</span></div>
          ${recipe.headline ? `<div class="hf-tdesc">${esc(recipe.headline)}</div>` : ""}
          ${stats.length ? `<div class="hf-tmeta" style="margin-top:auto">${stats.join("")}</div>` : ""}
        </div>
      </div>`;
  }

  // ---- events --------------------------------------------------------------------------------

  onInput(ev) {
    const before = this._mode();
    const cookbook = this.collection === COOKBOOK;
    if (!UI.searchTyped(this, ev, "recipe-search", () => this._search(), cookbook ? 160 : 350)) return;
    // A catalog search in flight is for text that has changed since. Its answer is dropped, and
    // it must not switch "searching" off when it lands: the search for the new text hasn't even
    // started yet, and for that gap the tab read "No recipes found." So is a next page of the
    // old results.
    this._searchSeq += 1;
    this._dropMore();
    this.searchError = null;
    if (!this.query.trim() || cookbook) {
      this.searchResults = null;
      this.searchLoading = false;
    } else this.searchLoading = true;
    // Typing on keeps what is on screen until the new search answers (redrawing 200 tiles per
    // keystroke is what made this slow); only starting or leaving a search changes it now.
    if (!cookbook && this._mode() !== before) this._redrawResults();
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
      case "clear-search": {
        this._resetSearch();
        // Emptied in place, so the caret stays in the field the button belongs to.
        const input = el.parentElement && el.parentElement.querySelector("input");
        UI.syncSearchBox(input, "");
        if (input) input.focus({ preventScroll: true });
        this._redrawResults();
        break;
      }
      case "retry":
        this._fetch();
        break;
      case "load-more":
        // The button had the keyboard (or a click put it there): it goes on to the new recipes.
        this._loadMore(Boolean(this.card.shadowRoot && this.card.shadowRoot.activeElement === el));
        break;
      case "retry-search": {
        const query = this.query.trim();
        if (!query || this.collection === COOKBOOK) break;
        this.searchError = null;
        this.searchLoading = true;
        this._redrawResults();
        this._runSearch(query);
        break;
      }
      default:
        break;
    }
  }
}
