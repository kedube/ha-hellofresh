/*
 * HelloFresh panel
 * ----------------
 * The HelloFresh card as a full-screen Home Assistant panel. The integration puts it in the
 * sidebar (frontend.py, the "Show HelloFresh in the sidebar" option), so the whole experience is
 * one click away with no dashboard to build.
 *
 * Home Assistant hands a panel element `hass`, `narrow`, `route` and `panel`; `panel.config`
 * carries the card's config (`config_entry_id` when there is more than one account). The panel
 * hosts one hellofresh-card and passes them on. The card fills the page and, where Home
 * Assistant hides its sidebar (phones), shows the button that opens it.
 *
 * The address names the section: /hellofresh-app/menu opens the Menu, /hellofresh-app/menu/2026-W42
 * that week of it (a notification can link straight there), and switching sections updates the
 * address in place, so a reload or bookmark comes back to the same one.
 */

const SECTIONS = ["overview", "menu", "market", "recipes", "account"];
const WEEK_ID = /^\d{4}-W\d{2}$/;

const PANEL_VERSION = new URL(import.meta.url).searchParams.get("v") || "unknown";

// The card is usually here already as a Lovelace resource: the same URL is the same module, so
// importing it again costs nothing. If a stale resource of another version defined the element
// first, that one is used rather than defining it twice.
const cardReady = customElements.get("hellofresh-card")
  ? Promise.resolve()
  : import(new URL(`./hellofresh-card.js?v=${encodeURIComponent(PANEL_VERSION)}`, import.meta.url).href).catch(
      (err) => {
        if (!customElements.get("hellofresh-card")) throw err;
      }
    );

class HelloFreshPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.shadowRoot.innerHTML = `<style>
        :host {
          display: block; height: 100%; overflow-y: auto; box-sizing: border-box;
          background: var(--primary-background-color);
        }
        .page { box-sizing: border-box; max-width: 1480px; min-height: 100%; margin: 0 auto; padding: 16px; }
        :host([narrow]) .page { padding: 0; }
        .error { padding: 32px 16px; color: var(--error-color, #db4437); }
      </style><div class="page"></div>`;
    this._page = this.shadowRoot.querySelector(".page");
    this._card = null;
    this._hass = null;
    this._narrow = false;
    this._panel = null;
    cardReady.then(
      () => this._mount(),
      (err) => this._fail(err)
    );
  }

  set hass(hass) {
    this._hass = hass;
    if (this._card) this._card.hass = hass;
  }

  get hass() {
    return this._hass;
  }

  set narrow(narrow) {
    this._narrow = Boolean(narrow);
    this.toggleAttribute("narrow", this._narrow);
    if (this._card) this._card.narrow = this._narrow;
  }

  get narrow() {
    return this._narrow;
  }

  set panel(panel) {
    const before = JSON.stringify(this._cardConfig());
    this._panel = panel;
    if (this._card && JSON.stringify(this._cardConfig()) !== before) this._card.setConfig(this._cardConfig());
  }

  get panel() {
    return this._panel;
  }

  set route(route) {
    this._route = route;
    this._applyRoute();
  }

  get route() {
    return this._route;
  }

  // The card's config: whatever the integration put in the panel's config, minus Home
  // Assistant's own bookkeeping.
  _cardConfig() {
    const { _panel_custom: _ignored, ...config } = (this._panel && this._panel.config) || {};
    return { type: "custom:hellofresh-card", ...config };
  }

  _mount() {
    const card = document.createElement("hellofresh-card");
    card.setAttribute("panel", "");
    card.setConfig(this._cardConfig());
    card.narrow = this._narrow;
    if (this._hass) card.hass = this._hass;
    card.addEventListener("hellofresh-navigated", (ev) => this._reflect(ev.detail && ev.detail.view));
    this._page.append(card);
    this._card = card;
    this._applyRoute();
  }

  _prefix() {
    return (this._route && this._route.prefix) || `/${(this._panel && this._panel.url_path) || "hellofresh-app"}`;
  }

  // An address naming a section (and maybe a week) opens it. Each address is applied once, so
  // Home Assistant handing the same route back doesn't undo a section chosen since.
  _applyRoute() {
    if (!this._card || !this._route) return;
    const path = String(this._route.path || "");
    if (path === this._appliedPath) return;
    this._appliedPath = path;
    const [section, week] = path.split("/").filter(Boolean);
    if (!SECTIONS.includes(section)) return;
    this._card.navigate(section, { weekId: WEEK_ID.test(week || "") ? week : null });
  }

  // Keep the address on the section showing, in place (no history entry per tab), while this
  // panel is the page on screen.
  _reflect(section) {
    if (!SECTIONS.includes(section)) return;
    const prefix = this._prefix();
    const url = `${prefix}/${section}`;
    const here = window.location.pathname;
    // Off this page, or already there (a link to a week of this section keeps its week).
    if (!here.startsWith(prefix) || here === url || here.startsWith(`${url}/`)) return;
    this._appliedPath = `/${section}`;
    window.history.replaceState(window.history.state, "", url);
  }

  _fail(err) {
    this._page.innerHTML = `<div class="error">The HelloFresh card couldn't load. Reload the page; if it keeps
      happening, check the browser console.</div>`;
    // eslint-disable-next-line no-console
    console.error("hellofresh: the sidebar panel could not load the card", err);
  }
}

customElements.define("hellofresh-panel", HelloFreshPanel);
