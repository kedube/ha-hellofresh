/*
 * HelloFresh shared recipe-detail overlay
 * ---------------------------------------
 * A tap-through "full recipe" sheet — ingredients with amounts, a servings switcher that
 * rescales them, step-by-step instructions, utensils, allergens, nutrition and the printable
 * recipe-card PDF — used by the Recipes, Meal planner and Market cards.
 *
 * Extracted so the three cards share one implementation. Several non-obvious details are baked
 * in here that were each a shipped bug at some point:
 *
 *   * The overlay is appended to the SHADOW ROOT, beside <ha-card>, so the grid's overflow
 *     cannot clip it. That also means a host card's delegated click listener never sees the
 *     overlay's clicks, so it binds its own.
 *   * The backdrop is matched by IDENTITY (`ev.target === overlay`), never by a marker
 *     attribute on the overlay: `closest()` walks *up* from the event target, so a marker there
 *     matches every click inside the panel and would dismiss the sheet when you pick a serving
 *     size or select recipe text.
 *   * No propagation guard on `.detailbox` — the ✕ lives inside it, so swallowing clicks there
 *     also swallows the close button.
 *   * Step text is escaped BEFORE newlines become <br>, or the break markup is escaped away.
 *
 * The host card supplies a `callService(service, data)` function and its shadow root; this
 * module owns everything else, including its own styles.
 *
 * A host may also supply `getSelection()` — consulted on every render while the sheet is
 * open. Returning null renders a plain read-only sheet; returning
 * `{ qty, maxQty, add, inc, dec }` pins a selection footer to the sheet ("+ Add" when
 * qty is 0, a ± servings stepper otherwise), completing the "read it, then decide" flow
 * on the meal planner's editable weeks. The callbacks mutate the HOST's pending selection
 * (the host re-renders its own grid); the sheet only re-renders its footer.
 */

// ---- helpers (self-contained: this module must not depend on a host card's internals) ------

export function escapeHtml(value) {
  return String(value == null ? "" : value).replace(
    /[&<>"']/g,
    (ch) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch],
  );
}

export function safeHttpUrl(url) {
  return url && /^https?:\/\//i.test(String(url)) ? String(url) : "";
}

// Request a HelloFresh image at the width actually displayed: the hero JPEGs are ~1.7 MB
// untransformed and ~73 KB at w_640. Mirrors the recipes card's resizer, including the
// `hellofresh_s3` path segment the CDN requires.
export function resizedImage(url, width) {
  if (!url || !/^https?:\/\//i.test(String(url))) return "";
  const raw = String(url);
  if (!width) return raw;
  if (raw.includes("/hellofresh_s3/")) {
    if (/[/,]w_\d+/.test(raw)) return raw.replace(/([/,])w_\d+/, `$1w_${width}`);
    if (/\/(?:[a-z]{1,2}_[^/]+)\/hellofresh_s3\//.test(raw)) {
      return raw.replace("/hellofresh_s3/", `,w_${width}/hellofresh_s3/`);
    }
    return raw.replace("/hellofresh_s3/", `/f_auto,fl_lossy,q_auto,w_${width}/hellofresh_s3/`);
  }
  if (raw.includes("/q_auto/")) return raw.replace("/q_auto/", `/q_auto,w_${width}/`);
  return raw.replace(/\/\d+,\d+\/image\//, `/${width},0/image/`);
}

export function formatMinutes(minutes) {
  if (!minutes && minutes !== 0) return "";
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} h ${rest} min` : `${hours} h`;
}

export const DETAIL_STYLES = `
  /* FIXED, not absolute. \`position: absolute\` resolves against the nearest positioned
     ancestor, and the host cards do not create one — so the sheet escaped its card, and the
     planner's \`ha-card { overflow: hidden }\` then clipped the panel away entirely. The result
     was a grey backdrop with nothing in it. Fixed positioning needs no positioned ancestor and
     cannot be clipped by an ancestor's overflow, so the sheet centres on the viewport in every
     card regardless of its layout. */
  .detailwrap {
    position: fixed; inset: 0; z-index: 20;
    display: flex; align-items: center; justify-content: center;
    background: rgba(0, 0, 0, 0.55); padding: 16px;
    /* Its own stacking context, so a card's positioned descendants can't paint over the sheet
       (the planner's video lightbox sits at z-index 9). */
    isolation: isolate;
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
  .detailbox .msg { padding: 24px 8px; text-align: center; color: var(--secondary-text-color); }
  .detailbox .msg.err { color: var(--error-color, #db4437); }
  /* Selection footer (hosts that supply getSelection): pinned below the scroll area so the
     Add/servings controls stay reachable however long the recipe is. */
  .selfooter {
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    padding: 10px 14px; border-top: 1px solid var(--divider-color);
  }
  .sadd {
    border: none; border-radius: 16px; cursor: pointer; padding: 7px 18px;
    background: var(--primary-color); color: var(--text-primary-color, #fff);
    font-weight: 600; font-size: 0.9em;
  }
  .selstate { font-size: 0.85em; color: var(--secondary-text-color); }
  .selstep { display: flex; align-items: center; gap: 8px; }
  .sqbtn {
    width: 28px; height: 28px; border-radius: 50%; border: 1px solid var(--divider-color);
    background: var(--card-background-color); color: var(--primary-text-color);
    cursor: pointer; font-size: 1em; line-height: 1;
  }
  .sqbtn:disabled { opacity: 0.4; cursor: default; }
  .sqval { min-width: 18px; text-align: center; font-weight: 600; }
`;

/**
 * Owns the recipe-detail sheet for one card.
 *
 * @param {object} options
 * @param {() => ShadowRoot|null} options.getRoot        where to append the overlay
 * @param {(service: string, data: object) => Promise<object>} options.callService
 * @param {(() => ({qty: number, maxQty: number, add: Function, inc: Function, dec: Function}|null))=} options.getSelection
 *        optional: selection state + mutators for the OPEN recipe, or null for read-only
 */
export class RecipeDetailOverlay {
  constructor({ getRoot, callService, getSelection }) {
    this._getRoot = getRoot;
    this._callService = callService;
    this._getSelection = getSelection || null;
    this._id = null;
    this._servings = null;
    this._detail = null;
    this._error = null;
    this._loading = false;
    this._overlay = null;
    this._keyHandler = null;
    this._seq = 0; // stale-response guard: comparing ids alone can't tell servings apart
  }

  get isOpen() {
    return this._id !== null;
  }

  /** Fetch and show one recipe. Safe to call repeatedly; a stale response is discarded. */
  async open(recipeId, servings) {
    if (!recipeId) return;
    // A per-call sequence, not just the id: rapid taps on the servings switcher issue
    // several fetches for the SAME id, and whichever response lands last would otherwise
    // win — showing amounts for a serving size the user has already switched away from.
    const seq = ++this._seq;
    this._id = String(recipeId);
    this._servings = servings || null;
    this._detail = null;
    this._error = null;
    this._loading = true;
    this._render();
    try {
      const payload = await this._callService("get_recipe_detail", {
        recipe_id: this._id,
        ...(servings ? { servings } : {}),
      });
      // Ignore a response the user has already navigated away from (another recipe, another
      // serving size, or a closed sheet).
      if (seq !== this._seq || this._id === null) return;
      this._detail = (payload && payload.recipe) || null;
    } catch (err) {
      if (seq !== this._seq || this._id === null) return;
      this._error = (err && err.message) || String(err);
    } finally {
      if (seq === this._seq) {
        this._loading = false;
        this._render();
      }
    }
  }

  /** Tear the sheet down, including its document-level key listener. */
  close() {
    this._seq += 1; // orphan any in-flight fetch
    this._id = null;
    this._detail = null;
    this._error = null;
    this._loading = false;
    if (this._keyHandler) {
      document.removeEventListener("keydown", this._keyHandler);
      this._keyHandler = null;
    }
    if (this._overlay) {
      this._overlay.remove();
      this._overlay = null;
    }
  }

  _render() {
    const root = this._getRoot();
    if (!root) return;
    if (!this._id) {
      this.close();
      return;
    }
    if (!this._overlay) {
      const overlay = document.createElement("div");
      overlay.className = "detailwrap";
      // The overlay sits beside <ha-card> in the shadow root, so the host card's delegated
      // listener never sees these clicks — it binds its own. Backdrop matched by identity;
      // see the module header for why a marker attribute would be wrong.
      overlay.addEventListener("click", (ev) => {
        const servingsBtn = ev.target.closest("[data-servings]");
        if (servingsBtn) {
          this.open(this._id, Number(servingsBtn.getAttribute("data-servings")));
          return;
        }
        // Selection footer: mutate the host's pending selection, then re-render so the
        // footer reflects the new quantity (the host re-renders its own grid itself).
        const selBtn = ev.target.closest("[data-sel]");
        if (selBtn && !selBtn.disabled) {
          const sel = this._getSelection && this._getSelection();
          if (sel) {
            const action = selBtn.getAttribute("data-sel");
            if (action === "add") sel.add();
            else if (action === "inc") sel.inc();
            else if (action === "dec") sel.dec();
            this._render();
          }
          return;
        }
        if (ev.target === overlay || ev.target.closest(".detailclose")) this.close();
      });
      root.appendChild(overlay);
      this._overlay = overlay;
      this._keyHandler = (ev) => {
        if (ev.key === "Escape") this.close();
      };
      document.addEventListener("keydown", this._keyHandler);
    }
    this._overlay.innerHTML = this._body();
  }

  _body() {
    const close = `<button class="detailclose" title="Close" aria-label="Close recipe">✕</button>`;
    if (this._loading && !this._detail) {
      return `<div class="detailbox"><div class="detailhead">${close}</div>
                <div class="msg">Loading recipe…</div></div>`;
    }
    if (this._error) {
      return `<div class="detailbox"><div class="detailhead">${close}</div>
                <div class="msg err">${escapeHtml(this._error)}</div></div>`;
    }
    const r = this._detail;
    if (!r) return `<div class="detailbox"><div class="detailhead">${close}</div></div>`;

    const facts = [];
    if (r.total_time_minutes || r.prep_time_minutes) {
      facts.push(formatMinutes(r.prep_time_minutes || r.total_time_minutes));
    }
    if (r.calories_kcal) facts.push(`${Math.round(r.calories_kcal)} kcal`);
    if (r.difficulty) facts.push(`Difficulty ${r.difficulty}`);
    if (r.rating) facts.push(`★ ${Number(r.rating).toFixed(1)}`);

    // Serving switcher: amounts are resolved server-side per yield, so changing this refetches.
    const yields = Array.isArray(r.available_yields) ? r.available_yields : [];
    const servingsBar =
      yields.length > 1
        ? `<div class="servings">
             <span class="slabel">Servings</span>
             ${yields
               .map(
                 (y) =>
                   `<button class="sbtn ${y === r.servings ? "active" : ""}"
                      data-servings="${escapeHtml(y)}">${escapeHtml(y)}</button>`,
               )
               .join("")}
           </div>`
        : "";

    const ingredients = (r.ingredients || [])
      .map((i) => {
        const amount = i.amount != null ? `${i.amount}${i.unit ? " " + i.unit : ""} ` : "";
        // Pantry staples you supply yourself are called out; everything else ships in the box.
        const pantry = i.shipped === false ? ` <span class="pantry">(not in box)</span>` : "";
        return `<li>${escapeHtml(amount)}${escapeHtml(i.name || "")}${pantry}</li>`;
      })
      .join("");

    // Escape FIRST, then turn newlines into <br> — the reverse order escapes the breaks away.
    const steps = (r.steps || [])
      .map((s) => `<li>${escapeHtml(s.instructions || "").replace(/\n/g, "<br>")}</li>`)
      .join("");

    const img = resizedImage(r.image_url, 640);
    const link = safeHttpUrl(r.url);
    const card = safeHttpUrl(r.card_url);
    return `
      <div class="detailbox">
        <div class="detailhead">
          <span class="detailtitle">${escapeHtml(r.name)}</span>
          ${close}
        </div>
        <div class="detailscroll">
          ${img ? `<img class="detailimg" src="${escapeHtml(img)}" alt="${escapeHtml(r.name)}">` : ""}
          ${r.headline ? `<div class="detailheadline">${escapeHtml(r.headline)}</div>` : ""}
          ${facts.length ? `<div class="facts">${escapeHtml(facts.join(" · "))}</div>` : ""}
          ${
            (r.allergens || []).length
              ? `<div class="allerg">Allergens: ${escapeHtml(r.allergens.join(", "))}</div>`
              : ""
          }
          ${servingsBar}
          ${ingredients ? `<h4>Ingredients</h4><ul class="ing">${ingredients}</ul>` : ""}
          ${
            (r.utensils || []).length
              ? `<h4>You'll need</h4><div class="utensils">${escapeHtml(r.utensils.join(" · "))}</div>`
              : ""
          }
          ${steps ? `<h4>Instructions</h4><ol class="steps">${steps}</ol>` : ""}
          <div class="detaillinks">
            ${card ? `<a href="${escapeHtml(card)}" target="_blank" rel="noopener noreferrer">Printable recipe card (PDF)</a>` : ""}
            ${link ? `<a href="${escapeHtml(link)}" target="_blank" rel="noopener noreferrer">View on hellofresh.com</a>` : ""}
          </div>
        </div>
        ${this._selectionBar()}
      </div>`;
  }

  // The pinned Add/servings footer, rendered only when the host supplies selection state for
  // the open recipe (i.e. the meal planner on an editable week). Mirrors the tile controls:
  // "+ Add" selects at one serving; − at one serving removes the meal.
  _selectionBar() {
    const sel = this._getSelection && this._getSelection();
    if (!sel) return "";
    if (!sel.qty) {
      return `<div class="selfooter">
                <span class="selstate">Not in this week's box</span>
                <button class="sadd" data-sel="add">+ Add</button>
              </div>`;
    }
    return `<div class="selfooter">
              <span class="selstate">In this week's box</span>
              <span class="selstep">
                <button class="sqbtn" data-sel="dec"
                  title="${sel.qty === 1 ? "Remove meal" : "Fewer servings"}">−</button>
                <span class="sqval">${Number(sel.qty)}</span>
                <button class="sqbtn" data-sel="inc" ${sel.qty >= sel.maxQty ? "disabled" : ""}
                  title="More servings">+</button>
                <span class="slabel">serving${sel.qty === 1 ? "" : "s"}</span>
              </span>
            </div>`;
  }
}
