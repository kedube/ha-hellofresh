/*
 * HelloFresh Recipes Card
 * -----------------------
 * Browse HelloFresh's public recipe catalog (~10,000 recipes) and manage cookbook favorites.
 *
 * This is BROWSE content, not account data: the catalog is the same for every customer and is
 * unrelated to your delivery weeks or meal selection. It is fetched on demand from the
 * response-returning `hellofresh.get_recipe_collections` / `hellofresh.get_catalog_recipes`
 * services (never part of the sensor poll — 10k recipes have no business in entity state).
 *
 * Favorites are read/written through `hellofresh.add_favorite` / `hellofresh.remove_favorite`,
 * with the heart state resolved for the recipes currently on screen. HelloFresh's cookbook
 * collections are named confusingly — bookmarks are created under `internal-recipes` but
 * listed and deleted under `external-recipes` — but the integration hides that entirely.
 *
 * Config:
 *   type: custom:hellofresh-recipes-card
 *   config_entry_id: <optional>     # required only when multiple HelloFresh accounts exist
 *   title: HelloFresh Recipes       # optional card header
 *   collection: chicken-recipes     # optional starting category slug
 *   limit: 40                       # optional recipes per category (1-200)
 *   logo: true                      # optional bundled HelloFresh logo in the header
 *
 * No build step: hand-written ES2020 served from the integration's www/ directory.
 */

// The integration stamps its release version onto the resource URL as ?v= (cache-bust),
// so the banner reports exactly which build the browser actually loaded.
const RECIPES_CARD_VERSION = new URL(import.meta.url).searchParams.get("v") || "unknown";

const LOGO_URL = "/hellofresh/hellofresh-logo.png";
const DEFAULT_LIMIT = 40;
// Sentinel "collection" for the customer's own cookbook, which comes from a different service
// than the browse catalog. The "@" prefix cannot collide with a real HelloFresh category slug.
const COOKBOOK_SLUG = "@cookbook";

// Rewrite a HelloFresh image URL to the width we actually display. This matters more than it
// looks: the catalog's hero JPEGs are ~1.7 MB untransformed and ~73 KB at w_640, and the grid
// shows dozens at once.
//
// Three URL shapes occur across HelloFresh's payloads:
//   1. Cloudinary WITH a transform — img.hellofresh.com/f_auto,q_auto,w_450/hellofresh_s3/image/…
//      Rewrite an existing w_<n>, or append one to the transform segment when absent.
//   2. Cloudinary WITHOUT a transform — img.hellofresh.com/hellofresh_s3/image/…
//      Insert a transform segment before `hellofresh_s3`. Skipping this is what serves the
//      full 1.7 MB asset.
//   3. CloudFront crop form — …/<w>,<h>/image/… , where "0,0" means unresized.
// Only plain http(s) URLs may reach an <img src>, so an attacker-chosen scheme never lands in
// the DOM; an unrecognized shape is returned unchanged rather than mangled.
function resizedImage(url, width) {
  if (!url || !/^https?:\/\//i.test(String(url))) return "";
  const raw = String(url);
  if (!width) return raw;
  if (raw.includes("/hellofresh_s3/")) {
    if (/[/,]w_\d+/.test(raw)) return raw.replace(/([/,])w_\d+/, `$1w_${width}`);
    if (/\/(?:[a-z]{1,2}_[^/]+)\/hellofresh_s3\//.test(raw)) {
      return raw.replace("/hellofresh_s3/", `,w_${width}/hellofresh_s3/`);
    }
    return raw.replace(
      "/hellofresh_s3/",
      `/f_auto,fl_lossy,q_auto,w_${width}/hellofresh_s3/`,
    );
  }
  if (raw.includes("/q_auto/")) return raw.replace("/q_auto/", `/q_auto,w_${width}/`);
  return raw.replace(/\/\d+,\d+\/image\//, `/${width},0/image/`);
}

function safeLinkUrl(url) {
  return url && /^https?:\/\//i.test(String(url)) ? String(url) : "";
}

// ISO-8601 minute counts arrive already converted by the integration; this only formats them.
function formatMinutes(minutes) {
  if (!minutes && minutes !== 0) return "";
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} h ${rest} min` : `${hours} h`;
}

class HelloFreshRecipesCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.shadowRoot.adoptedStyleSheets = [HelloFreshRecipesCard._sheet()];
    this._collections = [];
    this._subcollections = [];
    this._recipes = [];
    this._collection = null;
    this._loading = false;
    this._error = null;
    this._busyIds = new Set();
    this._flashMessage = null;
    this._flashIsError = false;
  }

  setConfig(config) {
    this._config = {
      title: "HelloFresh Recipes",
      limit: DEFAULT_LIMIT,
      ...(config || {}),
    };
    // A configured starting category applies until the user picks another one.
    if (this._collection === null) this._collection = this._config.collection || "";
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this.toggleAttribute("dark", Boolean(hass?.themes?.darkMode));
    if (hass && !this._fetched && !this._loading) {
      this._fetched = true;
      this._fetch();
    }
  }

  connectedCallback() {
    this._render();
  }

  disconnectedCallback() {
    clearTimeout(this._flashTimer);
    this._flashMessage = null;
    // The detail overlay sits beside the card in the shadow root and holds a document-level
    // key listener, so it must be torn down explicitly rather than with the card's markup.
    this._closeDetail();
  }

  getCardSize() {
    return 12;
  }

  static getConfigElement() {
    return document.createElement("hellofresh-recipes-card-editor");
  }

  static getStubConfig() {
    return { type: "custom:hellofresh-recipes-card" };
  }

  // ---- data ----------------------------------------------------------------

  _serviceData(extra) {
    const data = { ...(extra || {}) };
    if (this._config.config_entry_id) data.config_entry_id = this._config.config_entry_id;
    return data;
  }

  async _call(service, extra) {
    const result = await this._hass.callService(
      "hellofresh",
      service,
      this._serviceData(extra),
      undefined,
      false,
      true
    );
    return (result && result.response) || {};
  }

  async _fetch() {
    // Reentry guard: a refresh tap racing an in-flight load would otherwise run two concurrent
    // service calls, and the slower response could clobber the newer state.
    if (!this._hass || this._loading) return;
    this._loading = true;
    this._error = null;
    this._render();
    try {
      // Categories are only fetched once — they change on HelloFresh's schedule, not the user's.
      if (!this._collections.length) {
        const collections = await this._call("get_recipe_collections");
        this._collections = collections.collections || [];
      }
      if (this._collection === COOKBOOK_SLUG) {
        // The cookbook is the customer's own saved list, served by a different endpoint than
        // the browse catalog. Its rows use the same field names the grid already reads, except
        // that everything in it is by definition a favorite — the list IS the favorites — so
        // the heart is filled here rather than resolved per recipe.
        const payload = await this._call("get_favorites");
        this._recipes = (payload.favorites || []).map((f) => ({ ...f, is_favorite: true }));
        this._subcollections = [];
      } else {
        const payload = await this._call("get_catalog_recipes", {
          ...(this._collection ? { collection: this._collection } : {}),
          limit: Number(this._config.limit) || DEFAULT_LIMIT,
        });
        this._recipes = payload.recipes || [];
        // Categories can nest (Noodle -> Ramen/Udon/…). These children are absent from the
        // top-level category list, so this second row is the only route to them.
        this._subcollections = payload.subcollections || [];
      }
    } catch (err) {
      this._error = (err && err.message) || String(err);
    } finally {
      this._loading = false;
      this._render();
    }
  }

  async _selectCollection(slug) {
    if (this._loading || slug === this._collection) return;
    this._collection = slug;
    this._recipes = [];
    await this._fetch();
  }

  async _toggleFavorite(recipe) {
    if (!this._hass || this._busyIds.has(recipe.recipe_id)) return;
    const makeFavorite = recipe.is_favorite !== true;
    this._busyIds.add(recipe.recipe_id);
    this._render();
    try {
      await this._call(makeFavorite ? "add_favorite" : "remove_favorite", {
        recipe_id: recipe.recipe_id,
      });
      recipe.is_favorite = makeFavorite;
      // In the cookbook view the list IS the favorites, so an un-favorited row no longer
      // belongs in it — drop it rather than leaving a hollow heart on a recipe that is no
      // longer saved. Elsewhere the row is browse content and stays put.
      if (!makeFavorite && this._collection === COOKBOOK_SLUG) {
        this._recipes = this._recipes.filter((r) => r.recipe_id !== recipe.recipe_id);
      }
      this._flash(makeFavorite ? "Added to your cookbook." : "Removed from your cookbook.");
      // Let the meal-planner card re-read its hearts without a full page refresh.
      window.dispatchEvent(new CustomEvent("hellofresh-data-changed", { detail: {} }));
    } catch (err) {
      this._flash(
        `${makeFavorite ? "Add" : "Remove"} failed: ${(err && err.message) || err}`,
        true
      );
    } finally {
      this._busyIds.delete(recipe.recipe_id);
      this._render();
    }
  }

  _flash(message, isError = false) {
    this._flashMessage = message;
    this._flashIsError = isError;
    clearTimeout(this._flashTimer);
    this._flashTimer = setTimeout(() => {
      this._flashMessage = null;
      this._render();
    }, 4000);
  }

  // ---- rendering -----------------------------------------------------------

  _render() {
    if (!this.shadowRoot || !this._config) return;
    this._ensureShell();
    this._shell.header.innerHTML = this._renderHeader();
    this._shell.content.innerHTML = this._renderBody();
    this._shell.toast.innerHTML = this._flashMessage
      ? `<div class="toast ${this._flashIsError ? "err" : ""}">${this._esc(this._flashMessage)}</div>`
      : "";
  }

  _ensureShell() {
    if (this._shell) return;
    const card = document.createElement("ha-card");
    card.innerHTML = `
      <div class="js-header"></div>
      <div class="js-content"></div>
      <div class="js-toast"></div>`;
    this.shadowRoot.appendChild(card);
    this._shell = {
      header: card.querySelector(".js-header"),
      content: card.querySelector(".js-content"),
      toast: card.querySelector(".js-toast"),
    };
    this._bindDelegated(card);
  }

  _renderHeader() {
    const logo = this._config.logo
      ? typeof this._config.logo === "string"
        ? this._config.logo
        : LOGO_URL
      : "";
    return `
      <div class="head">
        ${logo ? `<img class="logo" src="${this._esc(logo)}" alt="HelloFresh">` : ""}
        <span class="title">${this._esc(this._config.title || "HelloFresh Recipes")}</span>
        <button class="iconbtn" data-action="refresh" title="Refresh" ${this._loading ? "disabled" : ""}>⟳</button>
      </div>`;
  }

  _renderBody() {
    if (this._error) {
      return `<div class="msg err">${this._esc(this._error)}</div>`;
    }
    const chips = this._renderCollections();
    if (this._loading && !this._recipes.length) {
      return `${chips}<div class="msg">Loading recipes…</div>`;
    }
    if (!this._recipes.length) {
      // An empty cookbook is a normal state, not a failed search — say so plainly.
      const empty = this._collection === COOKBOOK_SLUG
        ? "Your cookbook is empty. Tap the ♥ on any recipe to save it here."
        : "No recipes found.";
      return `${chips}<div class="msg">${empty}</div>`;
    }
    return `${chips}<div class="grid">${this._recipes.map((r) => this._renderRecipe(r)).join("")}</div>`;
  }

  _renderCollections() {
    if (!this._collections.length) return "";
    const chip = (slug, name) => {
      const active = (this._collection || "") === slug ? "active" : "";
      return `<button class="chip ${active}" data-collection="${this._esc(slug)}">${this._esc(name)}</button>`;
    };
    // The cookbook is not one of HelloFresh's browse categories — it is the customer's own
    // saved list, from a different service — so it gets a sentinel slug rather than being
    // mixed into the catalog collections. A leading "@" cannot collide with a real slug.
    const cookbook = `<button class="chip ${this._collection === COOKBOOK_SLUG ? "active" : ""}"
         data-collection="${COOKBOOK_SLUG}">♥ Cookbook</button>`;
    // Second row: the selected category's own children (Noodle -> Ramen / Udon / Rice / …).
    // These are absent from the top-level list, so without this row they are unreachable.
    // Rendered only when the current category has any, so the bar stays one row elsewhere.
    // A nested category must be requested by its PATH ("noodle-recipes/ramen-noodles"), not
    // its bare slug — /recipes/ramen-noodles 301-redirects away and yields no recipes.
    const subs = this._subcollections.length
      ? `<div class="chips subchips">
           <span class="sublabel">Refine</span>
           ${this._subcollections.map((c) => chip(c.path || c.slug, c.name)).join("")}
         </div>`
      : "";
    return `
      <div class="chips">
        ${chip("", "Top rated")}
        ${cookbook}
        ${this._collections.map((c) => chip(c.path || c.slug, c.name)).join("")}
      </div>
      ${subs}`;
  }

  _renderRecipe(recipe) {
    const img = resizedImage(recipe.image_url, 320);
    const link = safeLinkUrl(recipe.url);
    const busy = this._busyIds.has(recipe.recipe_id);
    // is_favorite is tri-state: true (bookmarked), false (checked, not bookmarked), and
    // null/undefined (never checked). Only a definite true fills the heart.
    const isFav = recipe.is_favorite === true;
    const stats = [];
    if (recipe.rating) {
      const count = recipe.ratings_count ? ` (${recipe.ratings_count.toLocaleString()})` : "";
      stats.push(`★ ${recipe.rating.toFixed(1)}${count}`);
    }
    const time = formatMinutes(recipe.prep_time_minutes);
    if (time) stats.push(time);
    return `
      <div class="recipe" data-detail="${this._esc(recipe.recipe_id)}">
        <div class="imgwrap">
          ${img
            ? `<img loading="lazy" src="${this._esc(img)}" alt="${this._esc(recipe.name)}">`
            : `<div class="noimg"></div>`}
          <button class="fav ${isFav ? "on" : ""}" data-fav="${this._esc(recipe.recipe_id)}"
                  ${busy ? "disabled" : ""}
                  title="${isFav ? "Remove from cookbook" : "Add to cookbook"}"
                  aria-label="${isFav ? "Remove" : "Add"} ${this._esc(recipe.name)}">
            ${busy ? "…" : isFav ? "♥" : "♡"}
          </button>
        </div>
        <div class="meta">
          <div class="name">${
            link
              ? `<a href="${this._esc(link)}" target="_blank" rel="noopener noreferrer">${this._esc(recipe.name)}</a>`
              : this._esc(recipe.name)
          }</div>
          ${recipe.headline ? `<div class="desc">${this._esc(recipe.headline)}</div>` : ""}
          ${stats.length ? `<div class="stats">${this._esc(stats.join(" · "))}</div>` : ""}
        </div>
      </div>`;
  }

  // A SINGLE delegated listener, attached once, so re-rendering never re-binds handlers.
  _bindDelegated(card) {
    card.addEventListener("click", (ev) => {
      const favBtn = ev.target.closest("[data-fav]");
      if (favBtn && !favBtn.disabled) {
        const id = favBtn.getAttribute("data-fav");
        const recipe = this._recipes.find((r) => r.recipe_id === id);
        if (recipe) this._toggleFavorite(recipe);
        return;
      }
      const chip = ev.target.closest("[data-collection]");
      if (chip) {
        this._selectCollection(chip.getAttribute("data-collection"));
        return;
      }
      // NOTE: the detail overlay binds its own listener (it lives outside <ha-card>, so
      // clicks in it never arrive here) — see _renderDetail.
      const tile = ev.target.closest("[data-detail]");
      if (tile) {
        this._openDetail(tile.getAttribute("data-detail"));
        return;
      }
      const action = ev.target.closest("[data-action]");
      if (action && action.getAttribute("data-action") === "refresh") this._fetch();
    });
  }

  // ---- recipe detail overlay ------------------------------------------------

  // Full detail (steps, ingredients, utensils) is fetched on demand rather than with the
  // listing: it is one request per recipe, and the grid shows dozens. The overlay is appended
  // to the shadow root so the grid's layout can't clip it.
  async _openDetail(recipeId, servings) {
    if (!this._hass || !recipeId) return;
    this._detailId = recipeId;
    this._detailServings = servings || null;
    this._detail = null;
    this._detailError = null;
    this._detailLoading = true;
    this._renderDetail();
    try {
      const payload = await this._call("get_recipe_detail", {
        recipe_id: recipeId,
        ...(servings ? { servings } : {}),
      });
      // Ignore a response for a recipe the user has already navigated away from.
      if (this._detailId !== recipeId) return;
      this._detail = payload.recipe || null;
    } catch (err) {
      if (this._detailId !== recipeId) return;
      this._detailError = (err && err.message) || String(err);
    } finally {
      this._detailLoading = false;
      this._renderDetail();
    }
  }

  _closeDetail() {
    this._detailId = null;
    this._detail = null;
    this._detailError = null;
    this._detailLoading = false;
    if (this._detailKeyHandler) {
      document.removeEventListener("keydown", this._detailKeyHandler);
      this._detailKeyHandler = null;
    }
    if (this._detailOverlay) {
      this._detailOverlay.remove();
      this._detailOverlay = null;
    }
  }

  _renderDetail() {
    if (!this.shadowRoot) return;
    if (!this._detailId) {
      this._closeDetail();
      return;
    }
    if (!this._detailOverlay) {
      const overlay = document.createElement("div");
      overlay.className = "detailwrap";
      // The overlay is a sibling of the <ha-card>, so the card's delegated listener never
      // sees its clicks — it needs its own. The servings switcher refetches at the chosen
      // yield; backdrop and ✕ dismiss.
      //
      // The backdrop is matched by IDENTITY, not by a marker attribute on the overlay:
      // closest() walks *up* from the event target, so a marker there matches every click
      // inside the panel and would dismiss the sheet when you picked a serving size or
      // selected recipe text.
      overlay.addEventListener("click", (ev) => {
        const servingsBtn = ev.target.closest("[data-servings]");
        if (servingsBtn) {
          this._openDetail(this._detailId, Number(servingsBtn.getAttribute("data-servings")));
          return;
        }
        if (ev.target === overlay || ev.target.closest(".detailclose")) this._closeDetail();
      });
      this.shadowRoot.appendChild(overlay);
      this._detailOverlay = overlay;
      this._detailKeyHandler = (ev) => {
        if (ev.key === "Escape") this._closeDetail();
      };
      document.addEventListener("keydown", this._detailKeyHandler);
    }
    // No propagation guard on .detailbox: the ✕ lives inside it, so swallowing every click
    // there also swallowed the close button. The handler above distinguishes the backdrop by
    // identity instead, which needs no guard — and attaching one here would re-bind on every
    // render anyway, since this innerHTML assignment replaces the node it was bound to.
    this._detailOverlay.innerHTML = this._renderDetailBody();
  }

  _renderDetailBody() {
    const close = `<button class="detailclose" title="Close" aria-label="Close recipe">✕</button>`;
    if (this._detailLoading && !this._detail) {
      return `<div class="detailbox"><div class="detailhead">${close}</div>
                <div class="msg">Loading recipe…</div></div>`;
    }
    if (this._detailError) {
      return `<div class="detailbox"><div class="detailhead">${close}</div>
                <div class="msg err">${this._esc(this._detailError)}</div></div>`;
    }
    const r = this._detail;
    if (!r) return `<div class="detailbox"><div class="detailhead">${close}</div></div>`;

    const facts = [];
    if (r.total_time_minutes || r.prep_time_minutes) {
      facts.push(formatMinutes(r.prep_time_minutes || r.total_time_minutes));
    }
    if (r.calories_kcal) facts.push(`${Math.round(r.calories_kcal)} kcal`);
    if (r.difficulty) facts.push(`Difficulty ${r.difficulty}`);
    if (r.rating) facts.push(`★ ${r.rating.toFixed(1)}`);

    // Serving switcher: amounts are resolved server-side per yield, so changing this refetches.
    const yields = Array.isArray(r.available_yields) ? r.available_yields : [];
    const servingsBar = yields.length > 1
      ? `<div class="servings">
           <span class="slabel">Servings</span>
           ${yields.map((y) => `<button class="sbtn ${y === r.servings ? "active" : ""}"
                data-servings="${y}">${y}</button>`).join("")}
         </div>`
      : "";

    const ingredients = (r.ingredients || []).map((i) => {
      const amount = i.amount != null ? `${i.amount}${i.unit ? " " + i.unit : ""} ` : "";
      // Pantry staples you supply yourself are called out; everything else ships in the box.
      const pantry = i.shipped === false ? ` <span class="pantry">(not in box)</span>` : "";
      return `<li>${this._esc(amount)}${this._esc(i.name || "")}${pantry}</li>`;
    }).join("");

    // A step's instructions can span several lines; escape first, then turn the newlines into
    // <br> so the break markup survives escaping rather than being escaped away.
    const steps = (r.steps || []).map(
      (s) => `<li>${this._esc(s.instructions || "").replace(/\n/g, "<br>")}</li>`
    ).join("");

    const img = resizedImage(r.image_url, 640);
    const link = safeLinkUrl(r.url);
    const card = safeLinkUrl(r.card_url);
    return `
      <div class="detailbox">
        <div class="detailhead">
          <span class="detailtitle">${this._esc(r.name)}</span>
          ${close}
        </div>
        <div class="detailscroll">
          ${img ? `<img class="detailimg" src="${this._esc(img)}" alt="${this._esc(r.name)}">` : ""}
          ${r.headline ? `<div class="detailheadline">${this._esc(r.headline)}</div>` : ""}
          ${facts.length ? `<div class="facts">${this._esc(facts.join(" · "))}</div>` : ""}
          ${(r.allergens || []).length
            ? `<div class="allerg">Allergens: ${this._esc(r.allergens.join(", "))}</div>` : ""}
          ${servingsBar}
          ${ingredients ? `<h4>Ingredients</h4><ul class="ing">${ingredients}</ul>` : ""}
          ${(r.utensils || []).length
            ? `<h4>You'll need</h4><div class="utensils">${this._esc(r.utensils.join(" · "))}</div>`
            : ""}
          ${steps ? `<h4>Instructions</h4><ol class="steps">${steps}</ol>` : ""}
          <div class="detaillinks">
            ${card ? `<a href="${this._esc(card)}" target="_blank" rel="noopener noreferrer">Printable recipe card (PDF)</a>` : ""}
            ${link ? `<a href="${this._esc(link)}" target="_blank" rel="noopener noreferrer">View on hellofresh.com</a>` : ""}
          </div>
        </div>
      </div>`;
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
    if (!HelloFreshRecipesCard.__sheet) {
      const sheet = new CSSStyleSheet();
      sheet.replaceSync(`
        :host { display: block; }
        /* Header sizing is deliberately identical to the other HelloFresh cards (planner,
           market, food profile): 12px gap, a 40px square logo and a 1.5em/500 title. The
           title size lives on .title rather than .head so it cannot cascade into the toolbar
           button beside it. */
        .head {
          display: flex; align-items: center; gap: 12px;
          padding: 16px 16px 8px;
        }
        .head .title { flex: 1; font-size: 1.5em; font-weight: 500; }
        .logo { height: 40px; width: 40px; border-radius: 8px; object-fit: cover; flex: none; }
        .iconbtn {
          border: none; background: transparent; cursor: pointer; font-size: 1.1em;
          color: var(--secondary-text-color); padding: 4px 8px; border-radius: 6px;
        }
        .iconbtn:hover:not([disabled]) { background: var(--divider-color); }
        .iconbtn[disabled] { opacity: 0.4; cursor: default; }
        .chips {
          display: flex; flex-wrap: wrap; gap: 6px; padding: 4px 16px 12px;
        }
        /* Sub-category row: visually subordinate to the main bar so the two don't read as one
           flat list of equals. Only rendered when the selected category has children. */
        .subchips {
          align-items: center; padding-top: 0; margin-top: -6px;
        }
        .sublabel {
          font-size: 0.72em; text-transform: uppercase; letter-spacing: 0.04em;
          color: var(--secondary-text-color); margin-right: 2px;
        }
        .subchips .chip { font-size: 0.78em; }
        .chip {
          border: 1px solid var(--divider-color); background: transparent; cursor: pointer;
          border-radius: 14px; padding: 4px 10px; font-size: 0.82em;
          color: var(--primary-text-color);
        }
        .chip:hover { background: var(--divider-color); }
        .chip.active {
          background: var(--primary-color); border-color: var(--primary-color);
          color: var(--text-primary-color, #fff);
        }
        .grid {
          display: grid; gap: 12px; padding: 0 16px 16px;
          grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
        }
        .recipe {
          border: 1px solid var(--divider-color); border-radius: 10px; overflow: hidden;
          display: flex; flex-direction: column;
        }
        .imgwrap { position: relative; aspect-ratio: 1 / 1; background: var(--divider-color); }
        .imgwrap img { width: 100%; height: 100%; object-fit: cover; display: block; }
        .noimg { width: 100%; height: 100%; }
        .fav {
          position: absolute; top: 6px; right: 6px; width: 28px; height: 28px;
          border: none; border-radius: 50%; cursor: pointer; font-size: 0.95em;
          background: rgba(0, 0, 0, 0.45); color: #fff; line-height: 1;
          display: flex; align-items: center; justify-content: center;
        }
        .fav.on { color: #ff5a7a; }
        .fav:hover:not([disabled]) { background: rgba(0, 0, 0, 0.7); }
        .fav[disabled] { opacity: 0.6; cursor: default; }
        .meta { padding: 8px 10px 10px; display: flex; flex-direction: column; gap: 3px; }
        .name { font-weight: 600; font-size: 0.9em; line-height: 1.25; }
        .name a { color: inherit; text-decoration: none; }
        .name a:hover { text-decoration: underline; }
        .desc {
          font-size: 0.8em; color: var(--secondary-text-color); line-height: 1.3;
          display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
          overflow: hidden;
        }
        .stats { font-size: 0.76em; color: var(--secondary-text-color); }
        .recipe { cursor: pointer; }
        .recipe:hover { border-color: var(--primary-color); }
        .detailwrap {
          position: fixed; inset: 0; z-index: 9; background: rgba(0, 0, 0, 0.7);
          display: flex; align-items: center; justify-content: center; padding: 16px;
        }
        .detailbox {
          background: var(--card-background-color, #fff); border-radius: 12px;
          max-width: min(640px, 94vw); width: 100%; max-height: 88vh;
          display: flex; flex-direction: column; overflow: hidden;
          box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
        }
        .detailhead {
          display: flex; align-items: center; justify-content: space-between; gap: 8px;
          padding: 10px 8px 10px 14px; border-bottom: 1px solid var(--divider-color);
        }
        .detailtitle { font-weight: 600; }
        .detailclose {
          border: none; background: transparent; cursor: pointer; font-size: 1.1em;
          color: var(--secondary-text-color); padding: 4px 8px; border-radius: 6px;
        }
        .detailclose:hover { background: var(--divider-color); }
        /* The panel itself is capped at 88vh; only the body scrolls, so the title and close
           button stay reachable on a long recipe. */
        .detailscroll { overflow-y: auto; padding: 12px 14px 16px; }
        .detailimg { width: 100%; border-radius: 8px; display: block; margin-bottom: 10px; }
        .detailheadline { color: var(--secondary-text-color); font-size: 0.9em; }
        .facts { margin-top: 6px; font-size: 0.85em; color: var(--secondary-text-color); }
        .allerg { margin-top: 4px; font-size: 0.8em; color: var(--secondary-text-color); }
        .servings { display: flex; align-items: center; gap: 6px; margin-top: 10px; }
        .slabel { font-size: 0.82em; color: var(--secondary-text-color); }
        .sbtn {
          border: 1px solid var(--divider-color); background: transparent; cursor: pointer;
          border-radius: 12px; min-width: 30px; padding: 3px 9px; font-size: 0.82em;
          color: var(--primary-text-color);
        }
        .sbtn.active {
          background: var(--primary-color); border-color: var(--primary-color);
          color: var(--text-primary-color, #fff);
        }
        .detailbox h4 { margin: 14px 0 6px; font-size: 0.92em; }
        .ing, .steps { margin: 0; padding-left: 20px; font-size: 0.87em; line-height: 1.5; }
        .steps li { margin-bottom: 8px; }
        .pantry { color: var(--secondary-text-color); font-size: 0.9em; }
        .utensils { font-size: 0.85em; color: var(--secondary-text-color); }
        .detaillinks { margin-top: 14px; display: flex; flex-direction: column; gap: 4px; }
        .detaillinks a { font-size: 0.85em; color: var(--primary-color); }
        .msg { padding: 8px 16px 20px; color: var(--secondary-text-color); }
        .msg.err { color: var(--error-color, #db4437); }
        .toast { margin: 0 16px 14px; padding: 8px 10px; border-radius: 8px;
          background: var(--divider-color); font-size: 0.85em; }
        .toast.err { background: var(--error-color, #db4437); color: #fff; }
      `);
      HelloFreshRecipesCard.__sheet = sheet;
    }
    return HelloFreshRecipesCard.__sheet;
  }
}

customElements.define("hellofresh-recipes-card", HelloFreshRecipesCard);

const EDITOR_SCHEMA = [
  { name: "title", selector: { text: {} } },
  { name: "collection", selector: { text: {} } },
  { name: "limit", selector: { number: { min: 1, max: 200, mode: "box" } } },
  { name: "logo", selector: { boolean: {} } },
  { name: "config_entry_id", selector: { text: {} } },
];

const EDITOR_LABELS = {
  title: "Title",
  collection: "Starting category",
  limit: "Recipes per category",
  logo: "Show HelloFresh logo",
  config_entry_id: "Config entry ID",
};

const EDITOR_HELPERS = {
  collection: "Category slug, e.g. chicken-recipes. Leave empty to start on Top rated.",
  limit: "How many recipes to load per category (1-200).",
  config_entry_id: "Only needed when multiple HelloFresh accounts are configured.",
};

class HelloFreshRecipesCardEditor extends HTMLElement {
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
      title: this._config.title != null ? this._config.title : "HelloFresh Recipes",
      collection: this._config.collection || "",
      limit: this._config.limit != null ? this._config.limit : DEFAULT_LIMIT,
      logo: Boolean(this._config.logo),
      config_entry_id: this._config.config_entry_id || "",
    };
  }

  _onFormChanged(ev) {
    ev.stopPropagation();
    const value = (ev.detail && ev.detail.value) || {};
    const config = { ...this._config, title: value.title || "HelloFresh Recipes" };
    if (value.collection) config.collection = value.collection;
    else delete config.collection;
    if (value.limit) config.limit = Number(value.limit);
    else delete config.limit;
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

customElements.define("hellofresh-recipes-card-editor", HelloFreshRecipesCardEditor);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "hellofresh-recipes-card",
  name: "HelloFresh Recipes Card",
  description: "Browse HelloFresh's recipe catalog and manage your cookbook favorites.",
});

console.info(
  `%c HELLOFRESH-RECIPES-CARD %c v${RECIPES_CARD_VERSION} `,
  "color:#fff;background:#91c11e;font-weight:700",
  "color:#91c11e;background:#fff"
);
