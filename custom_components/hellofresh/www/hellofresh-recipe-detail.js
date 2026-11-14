/*
 * HelloFresh recipe sheet
 * -----------------------
 * A tap-through "full recipe" sheet — ingredients with amounts, a servings switcher that
 * rescales them (each with its photo and the allergens it contains), step-by-step instructions
 * with HelloFresh's bold ingredient names, step photos and timers, utensils, allergens, the
 * per-serving nutrition table, the recipe video and the printable recipe-card PDF. The
 * HelloFresh card opens it from the Menu, the Market, Recipes, the Week tab and a delivery's
 * details; it loads in the background, so the card's first paint never waits on it.
 *
 * Several non-obvious details are baked in here that were each a shipped bug at some point:
 *
 *   * The overlay is appended to the SHADOW ROOT, beside <ha-card>, so the grid's overflow
 *     cannot clip it. That also means the card's delegated click listener never sees the
 *     overlay's clicks, so it binds its own.
 *   * The backdrop is matched by IDENTITY (`ev.target === overlay`), never by a marker
 *     attribute on the overlay: `closest()` walks *up* from the event target, so a marker there
 *     matches every click inside the panel and would dismiss the sheet when you pick a serving
 *     size or select recipe text.
 *   * No propagation guard on `.detailbox` — the ✕ lives inside it, so swallowing clicks there
 *     also swallows the close button.
 *   * Step text is escaped BEFORE newlines become <br>, or the break markup is escaped away.
 *   * The sheet is drawn in place: the dialog is built once, a tap on Add / + / − redraws the
 *     footer alone, and a change of servings keeps the recipe on screen (dimmed, at the same
 *     scroll position) until the rescaled one arrives. Redrawing the whole sheet sent a reader
 *     at step 6 back to the top on every tap.
 *
 * The card supplies a `callService(service, data)` function and its shadow root; this module
 * owns everything else, including its own styles. Escaping, URL checks, image resizing and
 * durations are the shared helpers' (hellofresh-shared.js), as everywhere in the card.
 *
 * The card may also supply:
 *
 *   * `getSelection()` — consulted while the sheet is open. Returning null renders a plain
 *     read-only sheet; returning `{ qty, maxQty, add, inc, dec }` pins a selection footer to the
 *     sheet ("+ Add" when qty is 0, a ± servings stepper otherwise), completing the "read it,
 *     then decide" flow on an editable week. The callbacks mutate the CARD's pending selection
 *     (the card re-renders its own grid). An optional `unit` ("item") relabels the stepper for
 *     things counted rather than served, like Market add-ons.
 *   * `captureFocus()` / `returnFocus(state)` — what had focus when the sheet opened, and how to
 *     give it back on close. The card re-renders behind the sheet, so only it can find the
 *     control again; without these the element itself is remembered.
 *   * `onToggle()` — called when the sheet opens and closes (the card makes what is behind it
 *     inert meanwhile).
 */

// The shared helpers and the card text layer (hellofresh-i18n.js), loaded with this module's own
// ?v= stamp: the sheet reads in the HelloFresh card's language.
const DETAIL_VERSION = encodeURIComponent(new URL(import.meta.url).searchParams.get("v") || "unknown");
const I18n = await import(new URL(`./hellofresh-i18n.js?v=${DETAIL_VERSION}`, import.meta.url).href);
const shared = await import(new URL(`./hellofresh-shared.js?v=${DETAIL_VERSION}`, import.meta.url).href);
const { t, ht } = I18n;
const { esc, safeUrl, resizedImage, formatMinutes, keepTabInside } = shared;

// Step timers arrive in seconds so a short "rest 30 sec" timer survives; whole minutes read
// exactly like the recipe times above.
export function formatSeconds(seconds) {
  const total = Math.round(Number(seconds));
  if (!Number.isFinite(total) || total <= 0) return "";
  if (total % 60 === 0) return formatMinutes(total / 60);
  if (total < 60) return t("time.duration.seconds", { seconds: total });
  return `${formatMinutes(Math.floor(total / 60))} ${t("time.duration.seconds", { seconds: total % 60 })}`;
}

// Step photos render as thumbnails (at most 180px wide), so request 2x that, not the hero's 640.
const STEP_IMAGE_WIDTH = 360;
// Ingredient cut-outs render at 32px; 96 covers a 3x display.
const INGREDIENT_IMAGE_WIDTH = 96;

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
  /* The dialog takes focus when it opens so Tab starts inside it; its controls show their own
     focus rings. */
  .detailbox:focus { outline: none; }
  /* A change of servings: the recipe stays, dimmed, until the rescaled amounts arrive. */
  .detailscroll.busy { opacity: 0.5; transition: opacity 0.15s; }
  .detailnote { padding: 8px 14px 0; font-size: 0.85em; color: var(--error-color, #db4437); }
  /* The panel itself is capped at 88vh; only the body scrolls, so the title and close
     button stay reachable on a long recipe. */
  .detailscroll { overflow-y: auto; padding: 12px 14px 16px; }
  .detailimg { width: 100%; border-radius: 8px; display: block; margin-bottom: 10px; }
  .detailheadline { color: var(--secondary-text-color); font-size: 0.9em; }
  .detaillabels { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
  .detaillabel {
    font-size: 0.75em; font-weight: 600; padding: 1px 8px; border-radius: 10px;
    background: var(--primary-color); color: var(--text-primary-color, #fff);
  }
  .detaildesc { margin: 8px 0 0; font-size: 0.85em; line-height: 1.45; }
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
  /* Ingredients: a photo per row instead of a bullet, two columns when the sheet is wide. */
  .ing {
    list-style: none; padding-left: 0;
    display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 4px 12px;
  }
  .ing li { display: flex; align-items: center; gap: 8px; }
  .ingimg { flex: 0 0 32px; width: 32px; height: 32px; object-fit: contain; }
  .ingtext { min-width: 0; }
  .ingallerg { display: block; font-size: 0.88em; color: var(--secondary-text-color); }
  .steps li { margin-bottom: 8px; }
  .steppara { margin: 0 0 4px; }
  /* A step photo is a thumbnail beside its text, not a full-width banner that would push the
     instructions a screen apart. The text comes FIRST in the markup: the list number aligns to
     the first item's baseline, which for a photo is its bottom edge. */
  .step { display: flex; gap: 10px; align-items: flex-start; }
  .stepbody { flex: 1; min-width: 0; }
  .stepfig { flex: 0 0 34%; max-width: 180px; margin: 2px 0 0; }
  .stepimg {
    width: 100%; aspect-ratio: 4 / 3; object-fit: cover; border-radius: 6px; display: block;
  }
  .stepfig figcaption { margin-top: 2px; font-size: 0.85em; color: var(--secondary-text-color); }
  .steptimers { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }
  .steptimer {
    font-size: 0.9em; padding: 1px 8px; border-radius: 10px;
    border: 1px solid var(--divider-color); color: var(--secondary-text-color);
  }
  .pantry { color: var(--secondary-text-color); font-size: 0.9em; }
  .nutri {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 0 14px;
    margin: 0; font-size: 0.85em;
  }
  .nutri div {
    display: flex; justify-content: space-between; gap: 6px; padding: 2px 0;
    border-bottom: 1px solid var(--divider-color);
  }
  .nutri dt { color: var(--secondary-text-color); }
  .nutri dd { margin: 0; font-weight: 500; }
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
 * @param {(() => object|null)=} options.captureFocus    optional: what has focus, for returnFocus
 * @param {((state: object) => void)=} options.returnFocus
 * @param {(() => void)=} options.onToggle               optional: the sheet opened or closed
 */
export class RecipeDetailOverlay {
  constructor({ getRoot, callService, getSelection, captureFocus, returnFocus, onToggle }) {
    this._getRoot = getRoot;
    this._callService = callService;
    this._getSelection = getSelection || null;
    this._captureFocus = captureFocus || null;
    this._returnFocus = returnFocus || null;
    this._onToggle = onToggle || null;
    this._id = null;
    this._servings = null;
    this._detail = null;
    this._error = null;
    this._loading = false;
    this._overlay = null;
    this._box = null; // the dialog inside the overlay
    this._shown = null; // the recipe id the dialog last drew (its scroll position is kept)
    this._opener = null; // what had focus when the sheet opened
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
    // The same recipe at another serving size stays on screen until the new amounts arrive;
    // another recipe has nothing worth keeping.
    if (this._id !== String(recipeId)) this._detail = null;
    this._id = String(recipeId);
    this._servings = servings || null;
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
    const wasOpen = Boolean(this._overlay);
    this._id = null;
    this._detail = null;
    this._error = null;
    this._loading = false;
    this._shown = null;
    if (this._keyHandler) {
      document.removeEventListener("keydown", this._keyHandler);
      this._keyHandler = null;
    }
    if (this._overlay) {
      this._overlay.remove();
      this._overlay = null;
      this._box = null;
    }
    if (!wasOpen) return;
    // What is behind the sheet becomes reachable again first, then focus goes back to it.
    if (this._onToggle) this._onToggle();
    const opener = this._opener;
    this._opener = null;
    if (this._returnFocus) this._returnFocus(opener);
    else if (opener && opener.isConnected) opener.focus({ preventScroll: true });
  }

  _render() {
    const root = this._getRoot();
    if (!root) return;
    if (!this._id) {
      this.close();
      return;
    }
    if (!this._overlay) {
      this._opener = this._captureFocus ? this._captureFocus() : root.activeElement;
      const overlay = document.createElement("div");
      overlay.className = "detailwrap";
      const box = document.createElement("div");
      box.className = "detailbox";
      box.setAttribute("role", "dialog");
      box.setAttribute("aria-modal", "true");
      box.tabIndex = -1;
      overlay.append(box);
      // The overlay sits beside <ha-card> in the shadow root, so the host card's delegated
      // listener never sees these clicks — it binds its own. Backdrop matched by identity;
      // see the module header for why a marker attribute would be wrong.
      overlay.addEventListener("click", (ev) => {
        const servingsBtn = ev.target.closest("[data-servings]");
        if (servingsBtn) {
          this.open(this._id, Number(servingsBtn.getAttribute("data-servings")));
          return;
        }
        // Selection footer: mutate the host's pending selection, then redraw the footer alone
        // (the host re-renders its own grid itself). The recipe above it is not touched.
        const selBtn = ev.target.closest("[data-sel]");
        if (selBtn && !selBtn.disabled) {
          const sel = this._getSelection && this._getSelection();
          if (sel) {
            const action = selBtn.getAttribute("data-sel");
            if (action === "add") sel.add();
            else if (action === "inc") sel.inc();
            else if (action === "dec") sel.dec();
            this._renderFooter(action);
          }
          return;
        }
        if (ev.target === overlay || ev.target.closest(".detailclose")) this.close();
      });
      // Tab stays inside the sheet while it is open.
      overlay.addEventListener("keydown", (ev) => keepTabInside(ev, this._box, this._getRoot().activeElement));
      root.appendChild(overlay);
      this._overlay = overlay;
      this._box = box;
      this._keyHandler = (ev) => {
        if (ev.key === "Escape") this.close();
      };
      document.addEventListener("keydown", this._keyHandler);
      if (this._onToggle) this._onToggle();
      box.focus({ preventScroll: true });
    }
    const box = this._box;
    const hadFocus = box.contains(root.activeElement);
    const focused = hadFocus ? this._focusSelector(root.activeElement) : "";
    // The same recipe drawn again (other amounts, a failed re-read) keeps its place.
    const scroller = this._shown === this._id ? box.querySelector(".detailscroll") : null;
    const top = scroller ? scroller.scrollTop : 0;
    this._shown = this._id;
    box.setAttribute("aria-label", (this._detail && this._detail.name) || t("recipe.loading"));
    box.setAttribute("aria-busy", String(this._loading));
    box.innerHTML = this._body();
    const next = box.querySelector(".detailscroll");
    if (next && top) next.scrollTop = top;
    if (hadFocus && !root.activeElement) {
      // The pressed control was redrawn: back onto its replacement, else the dialog itself.
      const again = focused ? box.querySelector(focused) : null;
      (again || box).focus({ preventScroll: true });
    }
  }

  // How to find a control of the sheet again after it has been redrawn.
  _focusSelector(el) {
    if (!el || !el.getAttribute) return "";
    for (const name of ["data-servings", "data-sel"]) {
      if (el.hasAttribute(name)) return `[${name}="${CSS.escape(el.getAttribute(name))}"]`;
    }
    if (el.classList.contains("detailclose")) return ".detailclose";
    return el.localName === "a" && el.getAttribute("href") ? `a[href="${CSS.escape(el.getAttribute("href"))}"]` : "";
  }

  // Redraw the Add/servings footer in place after a tap on it (`pressed`: add, inc or dec).
  _renderFooter(pressed) {
    const box = this._box;
    const root = this._getRoot();
    if (!box || !root) return;
    const old = box.querySelector(".selfooter");
    const hadFocus = Boolean(old) && old.contains(root.activeElement);
    const template = document.createElement("template");
    template.innerHTML = this._selectionBar().trim();
    const fresh = template.content.firstElementChild;
    if (old && fresh) old.replaceWith(fresh);
    else if (old) old.remove();
    else if (fresh) box.append(fresh);
    if (!hadFocus || !fresh) return;
    // The pressed control may be gone: "+ Add" turns into the stepper, and − at one serving
    // brings "+ Add" back. Focus goes to the control standing in for it.
    const stand = { add: ["inc", "dec"], inc: ["inc", "dec"], dec: ["dec", "add", "inc"] }[pressed] || [];
    for (const name of stand) {
      const el = fresh.querySelector(`[data-sel="${name}"]:not([disabled])`);
      if (el) {
        el.focus({ preventScroll: true });
        return;
      }
    }
  }

  // What goes inside the dialog.
  _body() {
    const close = `<button class="detailclose" title="${ht("common.close")}" aria-label="${ht("recipe.close")}"><span aria-hidden="true">✕</span></button>`;
    const r = this._detail;
    if (!r) {
      const message = this._loading
        ? `<div class="msg" role="status">${ht("recipe.loading")}</div>`
        : this._error
          ? `<div class="msg err" role="alert">${esc(this._error)}</div>`
          : "";
      return `<div class="detailhead">${close}</div>${message}`;
    }

    const facts = [];
    if (r.total_time_minutes || r.prep_time_minutes) {
      facts.push(formatMinutes(r.prep_time_minutes || r.total_time_minutes));
    }
    if (r.calories_kcal) facts.push(`${Math.round(r.calories_kcal)} kcal`);
    if (r.difficulty) facts.push(t("recipe.difficulty", { level: r.difficulty }));
    if (r.rating) facts.push(`★ ${Number(r.rating).toFixed(1)}`);

    // Serving switcher: amounts are resolved server-side per yield, so changing this refetches.
    const yields = Array.isArray(r.available_yields) ? r.available_yields : [];
    // While the rescaled amounts are on their way, the size just picked is the one marked.
    const shownYield = this._loading && this._servings ? this._servings : r.servings;
    const servingsBar =
      yields.length > 1
        ? `<div class="servings">
             <span class="slabel">${ht("recipe.servings")}</span>
             ${yields
               .map(
                 (y) =>
                   `<button class="sbtn ${y === shownYield ? "active" : ""}" aria-pressed="${y === shownYield}"
                      data-servings="${esc(y)}">${esc(y)}</button>`,
               )
               .join("")}
           </div>`
        : "";

    const ingredients = (r.ingredients || [])
      .map((i) => {
        const amount = i.amount != null ? `${i.amount}${i.unit ? " " + i.unit : ""} ` : "";
        // Pantry staples you supply yourself are called out; everything else ships in the box.
        const pantry = i.shipped === false ? ` <span class="pantry">${ht("recipe.not_in_box")}</span>` : "";
        const contains = Array.isArray(i.allergens) && i.allergens.length
          ? `<span class="ingallerg">${ht("recipe.contains", { allergens: i.allergens.join(", ") })}</span>`
          : "";
        // The name sits right beside the photo, so the image is decorative (empty alt). A row
        // without one keeps an empty 32px slot so the names stay aligned.
        const thumb = resizedImage(i.image_url, INGREDIENT_IMAGE_WIDTH);
        const pic = thumb
          ? `<img class="ingimg" src="${esc(thumb)}" alt="" loading="lazy" decoding="async">`
          : `<span class="ingimg"></span>`;
        return `<li>${pic}<span class="ingtext">${esc(amount)}${esc(
          i.name || "",
        )}${pantry}${contains}</span></li>`;
      })
      .join("");

    const steps = (r.steps || [])
      .map((s, i) => {
        // Rich paragraphs are plain {text, bold} runs built server-side — never HelloFresh's
        // HTML — so every run is escaped here like any other string. Without them, fall back
        // to the plain text: escape FIRST, then newlines become <br>, or the breaks escape away.
        const paragraphs = Array.isArray(s.paragraphs) ? s.paragraphs.filter(Array.isArray) : [];
        const text = paragraphs.length
          ? paragraphs
              .map(
                (runs) =>
                  `<p class="steppara">${runs
                    .map((run) =>
                      run && run.bold
                        ? `<strong>${esc(run.text)}</strong>`
                        : esc(run && run.text),
                    )
                    .join("")}</p>`,
              )
              .join("")
          : esc(s.instructions || "").replace(/\n/g, "<br>");
        const photo = resizedImage(s.image_url, STEP_IMAGE_WIDTH);
        const caption = photo && s.caption ? String(s.caption) : "";
        const timers = (Array.isArray(s.timers) ? s.timers : [])
          .map((timer) => {
            const time = formatSeconds(timer && timer.seconds);
            if (!time) return "";
            // HelloFresh names a timer after its step, so beside a captioned photo the name
            // would just repeat the caption; show only the time then.
            const name =
              timer.name && String(timer.name).trim().toLowerCase() !== caption.trim().toLowerCase()
                ? timer.name
                : "";
            return `<span class="steptimer">⏱ ${esc(name ? `${name} · ${time}` : time)}</span>`;
          })
          .join("");
        const figure = photo
          ? `<figure class="stepfig">
               <img class="stepimg" src="${esc(photo)}" alt="${ht("recipe.step", { number: i + 1 })}"
                 loading="lazy" decoding="async">
               ${caption ? `<figcaption>${esc(caption)}</figcaption>` : ""}
             </figure>`
          : "";
        return `<li><div class="step">
                  <div class="stepbody">${text}${
                    timers ? `<div class="steptimers">${timers}</div>` : ""
                  }</div>
                  ${figure}
                </div></li>`;
      })
      .join("");

    // HelloFresh's values are per serving, in its own order (calories first).
    const nutrition = Object.entries(r.nutrition || {}).filter(([k, v]) => k && v != null && v !== "");
    const nutritionTable = nutrition.length
      ? `<h4>${ht("recipe.nutrition")} <span class="slabel">${ht("recipe.per_serving")}</span></h4>
         <dl class="nutri">${nutrition
           .map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`)
           .join("")}</dl>`
      : "";
    const labels = (Array.isArray(r.labels) ? r.labels : []).filter(Boolean);

    const img = resizedImage(r.image_url, 640);
    const link = safeUrl(r.url);
    const card = safeUrl(r.card_url);
    const video = safeUrl(r.video_url);
    return `
        <div class="detailhead">
          <span class="detailtitle">${esc(r.name)}</span>
          ${close}
        </div>
        ${this._error ? `<div class="detailnote" role="alert">${esc(this._error)}</div>` : ""}
        <div class="detailscroll${this._loading ? " busy" : ""}">
          ${img ? `<img class="detailimg" src="${esc(img)}" alt="${esc(r.name)}">` : ""}
          ${r.headline ? `<div class="detailheadline">${esc(r.headline)}</div>` : ""}
          ${
            labels.length
              ? `<div class="detaillabels">${labels
                  .map((l) => `<span class="detaillabel">${esc(l)}</span>`)
                  .join("")}</div>`
              : ""
          }
          ${facts.length ? `<div class="facts">${esc(facts.join(" · "))}</div>` : ""}
          ${r.description ? `<p class="detaildesc">${esc(r.description)}</p>` : ""}
          ${
            (r.allergens || []).length
              ? `<div class="allerg">${ht("recipe.allergens", { allergens: r.allergens.join(", ") })}</div>`
              : ""
          }
          ${servingsBar}
          ${ingredients ? `<h4>${ht("recipe.ingredients")}</h4><ul class="ing">${ingredients}</ul>` : ""}
          ${
            (r.utensils || []).length
              ? `<h4>${ht("recipe.utensils")}</h4><div class="utensils">${esc(r.utensils.join(" · "))}</div>`
              : ""
          }
          ${steps ? `<h4>${ht("recipe.instructions")}</h4><ol class="steps">${steps}</ol>` : ""}
          ${nutritionTable}
          <div class="detaillinks">
            ${video ? `<a href="${esc(video)}" target="_blank" rel="noopener noreferrer">${ht("recipe.video")}</a>` : ""}
            ${card ? `<a href="${esc(card)}" target="_blank" rel="noopener noreferrer">${ht("recipe.card_pdf")}</a>` : ""}
            ${link ? `<a href="${esc(link)}" target="_blank" rel="noopener noreferrer">${ht("recipe.view_site")}</a>` : ""}
          </div>
        </div>
        ${this._selectionBar()}`;
  }

  // The pinned Add/servings footer, rendered only when the host supplies selection state for
  // the open recipe (i.e. the meal planner on an editable week). Mirrors the tile controls:
  // "+ Add" selects at one serving; − at one serving removes the meal.
  _selectionBar() {
    const sel = this._getSelection && this._getSelection();
    if (!sel) return "";
    if (!sel.qty) {
      return `<div class="selfooter">
                <span class="selstate">${ht("recipe.not_in_week_box")}</span>
                <button class="sadd" data-sel="add">${ht("recipe.add")}</button>
              </div>`;
    }
    const item = sel.unit === "item";
    const qty = Number(sel.qty);
    return `<div class="selfooter">
              <span class="selstate">${ht("recipe.in_week_box")}</span>
              <span class="selstep">
                <button class="sqbtn" data-sel="dec"
                  title="${qty === 1 ? ht(item ? "market.remove" : "menu.remove_meal") : ht(item ? "recipe.fewer_items" : "menu.fewer_servings")}">−</button>
                <span class="sqval">${qty}</span>
                <button class="sqbtn" data-sel="inc" ${sel.qty >= sel.maxQty ? "disabled" : ""}
                  title="${ht(item ? "recipe.more_items" : "menu.more_servings")}">+</button>
                <span class="slabel">${ht(item ? "recipe.item_unit" : "menu.serving_unit", { count: qty })}</span>
              </span>
            </div>`;
  }
}
