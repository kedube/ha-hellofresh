/*
 * HelloFresh Delivery Tracking Card
 * ---------------------------------
 * Live last-mile box tracking for markets where HelloFresh runs its own delivery fleet
 * (currently the Netherlands — the Tracey stack behind hftrack.nl, see issue #6). Shows the
 * delivery phase as a progress timeline (Box packed → On the way → Delivered), the
 * minute-precision ETA, how many stops the driver has before you, the driver's name, and
 * HelloFresh's personal message, with a link to the official live map.
 *
 * Reads everything from the response-returning `hellofresh.get_delivery_tracking` service,
 * which does a live (server-side throttled) fetch of the tracking endpoint — fresher than
 * the sensors' own poll. While a delivery is on the road the card refetches every 2 minutes;
 * idle, every 15. Read-only.
 *
 * Regional restriction: for accounts outside the supported countries the service answers
 * `available: false` and the card explains the restriction instead of rendering a dead
 * tracker (or renders nothing at all with `hide_if_unavailable: true`) — the backing
 * sensors are likewise only created for supported countries.
 *
 * Config:
 *   type: custom:hellofresh-delivery-tracking-card
 *   config_entry_id: <optional>    # required only when multiple HelloFresh accounts exist
 *   title: Delivery Tracking       # optional card header
 *   logo: true                     # optional bundled HelloFresh logo in the header
 *   hide_if_unavailable: false     # optional: render nothing for unsupported countries
 *
 * No build step: hand-written ES2020 served from the integration's www/ directory.
 */

// The integration stamps its release version onto the resource URL as ?v= (cache-bust),
// so the banner reports exactly which build the browser actually loaded.
const TRACKING_CARD_VERSION = new URL(import.meta.url).searchParams.get("v") || "unknown";
// Shared card helpers. AWAITED AT TOP LEVEL: they are called synchronously during the first
// render, and an un-awaited dynamic import is still a Promise then. The dynamic form carries
// this card's ?v= cache-bust onto the shared module (a static specifier is never stamped).
const { esc, parseLocalDate, fmtDate, safeUrl } = await import(
  new URL(
    `./hellofresh-shared.js?v=${encodeURIComponent(TRACKING_CARD_VERSION)}`,
    import.meta.url,
  ).href
);

// Refetch cadence: live deliveries change by the minute; idle days barely change at all.
// The integration additionally throttles the underlying endpoint fetch to once a minute,
// so several open dashboards never multiply requests.
const ACTIVE_REFETCH_MS = 2 * 60000;
const IDLE_REFETCH_MS = 15 * 60000;

// Phase → timeline step. DELAYED keeps the "on the way" step lit and adds a banner;
// CANCELLED collapses the timeline into a banner only.
const PHASE_STEPS = [
  { key: "packed", label: "Box packed", phases: ["AT_DEPOT"] },
  { key: "moving", label: "On the way", phases: ["DRIVER_DEPARTED", "ON_THE_WAY", "DELAYED"] },
  { key: "done", label: "Delivered", phases: ["DELIVERED", "DELIVERED_HOME"] },
];

class HelloFreshDeliveryTrackingCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.shadowRoot.adoptedStyleSheets = [HelloFreshDeliveryTrackingCard._sheet()];
    this._hass = null;
    this._tracking = null;
    this._loading = false;
    this._error = null;
    this._fetched = false;
    this._lastFetched = 0;
    this._tickTimer = null;
    this._onVisibility = () => this._onBecameVisible();
  }

  connectedCallback() {
    document.addEventListener("visibilitychange", this._onVisibility);
    this._startTick();
    if (this._tracking) this._refreshIfStale();
  }

  disconnectedCallback() {
    document.removeEventListener("visibilitychange", this._onVisibility);
    this._stopTick();
  }

  setConfig(config) {
    this._config = { title: "Delivery Tracking", ...config };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (hass && !this._fetched && !this._loading) {
      this._fetched = true;
      this._fetch();
    }
  }

  getCardSize() {
    return 4;
  }

  static getStubConfig() {
    return { type: "custom:hellofresh-delivery-tracking-card" };
  }

  // ---- data ----------------------------------------------------------------

  async _fetch() {
    if (!this._hass || this._loading) return;
    this._loading = true;
    this._error = null;
    this._render();
    try {
      const data = {};
      if (this._config.config_entry_id) data.config_entry_id = this._config.config_entry_id;
      const result = await this._hass.callService(
        "hellofresh", "get_delivery_tracking", data, undefined, false, true
      );
      this._tracking = (result && result.response) || {};
    } catch (err) {
      this._error = (err && err.message) || String(err);
    } finally {
      this._lastFetched = Date.now();
      this._loading = false;
      this._render();
    }
  }

  // ---- auto-refresh ----------------------------------------------------------

  _isLive() {
    const t = this._tracking;
    if (!t || t.available === false || !t.active) return false;
    // Once delivered (or cancelled) there is nothing left to watch closely.
    return !["DELIVERED", "DELIVERED_HOME", "CANCELLED"].includes(t.phase);
  }

  _refetchIntervalMs() {
    return this._isLive() ? ACTIVE_REFETCH_MS : IDLE_REFETCH_MS;
  }

  _refreshIfStale() {
    if (!this._hass || !this._fetched || this._loading) return false;
    // Never poll for a region that can't have data — one answer is definitive.
    if (this._tracking && this._tracking.available === false) return false;
    if (Date.now() - this._lastFetched < this._refetchIntervalMs()) return false;
    this._fetch();
    return true;
  }

  _startTick() {
    if (this._tickTimer) return;
    this._tickTimer = setInterval(() => {
      if (document.visibilityState === "hidden") return;
      this._refreshIfStale();
    }, 30000);
  }

  _stopTick() {
    clearInterval(this._tickTimer);
    this._tickTimer = null;
  }

  _onBecameVisible() {
    if (document.visibilityState !== "visible") return;
    this._refreshIfStale();
  }

  // ---- rendering -------------------------------------------------------------

  _render() {
    if (!this.shadowRoot) return;
    const t = this._tracking;
    if (t && t.available === false && this._config && this._config.hide_if_unavailable) {
      // Nothing useful to show and the user asked for silence — collapse entirely.
      this.shadowRoot.innerHTML = "";
      this._shell = null;
      this.style.display = "none";
      return;
    }
    this.style.display = "";
    this._ensureShell();
    this._shell.head.innerHTML = `
      ${this._renderLogo()}
      <span class="title-text">${esc(this._config ? this._config.title : "Delivery Tracking")}</span>
      <button class="refreshbtn" data-action="refresh" title="Refresh" ${this._loading ? "disabled" : ""}>↻</button>`;
    this._shell.body.innerHTML = this._renderBody();
  }

  _ensureShell() {
    if (this._shell) return;
    const card = document.createElement("ha-card");
    card.innerHTML = `<div class="head"></div><div class="body"></div>`;
    this.shadowRoot.appendChild(card);
    this._shell = { card, head: card.querySelector(".head"), body: card.querySelector(".body") };
    card.addEventListener("click", (ev) => {
      const actionEl = ev.target.closest("[data-action]");
      if (!actionEl) return;
      if (actionEl.getAttribute("data-action") === "refresh") this._fetch();
    });
  }

  _renderLogo() {
    const logo = this._config && this._config.logo;
    if (!logo) return "";
    const logoUrl = logo === true ? "/hellofresh/hellofresh-logo.png" : logo;
    return `<img class="logo" src="${esc(logoUrl)}" alt="HelloFresh">`;
  }

  _renderBody() {
    const t = this._tracking;
    if (!t) {
      if (this._loading || !this._fetched) return `<div class="state">Loading delivery tracking…</div>`;
      return `<div class="state error">Could not load delivery tracking: ${esc(this._error || "no data")}</div>
        <div class="actions"><button data-action="refresh">Retry</button></div>`;
    }
    const notice = this._error
      ? `<div class="notice">Refresh failed: ${esc(this._error)}
           <button class="refreshbtn" data-action="refresh">Retry</button></div>`
      : "";
    if (t.available === false) return `${notice}${this._renderUnsupported()}`;
    if (!t.active) return `${notice}${this._renderIdle()}`;
    return `<div class="${this._loading ? "reloading" : ""}">
      ${notice}
      ${this._renderBanner()}
      ${this._renderTimeline()}
      ${this._renderDetails()}
      ${this._renderMessage()}
      ${this._renderFooter()}
    </div>`;
  }

  _renderUnsupported() {
    return `
      <div class="empty">
        <div class="empty-icon">🌍</div>
        <div class="empty-title">Not available in your region</div>
        <div class="empty-text">
          Live delivery tracking uses HelloFresh's own delivery-fleet tracker, which currently
          exists only for HelloFresh <b>Netherlands</b> accounts. Boxes elsewhere are delivered by
          third-party carriers, whose coarser status is on the schedule card instead.
        </div>
      </div>`;
  }

  _renderIdle() {
    const t = this._tracking;
    const next = t.next_delivery_date ? parseLocalDate(t.next_delivery_date) : null;
    const nextLine = next
      ? `Your next delivery is <b>${esc(fmtDate(t.next_delivery_date))}</b> — live tracking
         starts once the box is packed at the depot.`
      : `Live tracking starts once a box is packed at the depot.`;
    return `
      <div class="empty">
        <div class="empty-icon">📦</div>
        <div class="empty-title">No delivery on the road</div>
        <div class="empty-text">${nextLine}</div>
      </div>`;
  }

  // DELAYED / CANCELLED get a banner; the timeline below still shows the last known step.
  _renderBanner() {
    const phase = this._tracking.phase;
    if (phase === "DELAYED") {
      return `<div class="banner warn"><span class="banner-icon">⏳</span>
        <span>Your delivery is delayed. The ETA below is HelloFresh's latest estimate.</span></div>`;
    }
    if (phase === "CANCELLED") {
      return `<div class="banner error"><span class="banner-icon">⚠️</span>
        <span>HelloFresh cancelled this delivery. Check your account for details.</span></div>`;
    }
    return "";
  }

  _renderTimeline() {
    const phase = this._tracking.phase;
    let activeIndex = PHASE_STEPS.findIndex((step) => step.phases.includes(phase));
    if (activeIndex === -1) activeIndex = 0; // unknown phase: show the tracker armed, not broken
    const delivered = activeIndex === PHASE_STEPS.length - 1;
    const steps = PHASE_STEPS.map((step, index) => {
      const stateClass = index < activeIndex ? "past" : index === activeIndex ? "current" : "todo";
      const marker = index < activeIndex || (delivered && index === activeIndex) ? "✓" : "";
      return `
        <div class="step ${stateClass}">
          <div class="dot">${marker}</div>
          <div class="slabel">${esc(step.label)}</div>
        </div>
        ${index < PHASE_STEPS.length - 1 ? `<div class="bar ${index < activeIndex ? "past" : "todo"}"></div>` : ""}`;
    }).join("");
    return `<div class="timeline">${steps}</div>`;
  }

  _renderDetails() {
    const t = this._tracking;
    const items = [];
    const eta = this._fmtEta(t.eta);
    if (eta && !["DELIVERED", "DELIVERED_HOME", "CANCELLED"].includes(t.phase)) {
      items.push(["Estimated arrival", eta]);
    }
    if (t.stops_before !== null && t.stops_before !== undefined) {
      items.push(["Stops before you", String(t.stops_before)]);
    }
    if (t.driver_name) items.push(["Driver", t.driver_name]);
    if (t.delivery_time) items.push(["Delivery window", t.delivery_time]);
    if (!items.length) return "";
    const cells = items
      .map(
        ([label, value]) => `
          <div class="item">
            <span class="ilabel">${esc(label)}</span>
            <span class="ival">${esc(value)}</span>
          </div>`
      )
      .join("");
    return `<div class="grid">${cells}</div>`;
  }

  _renderMessage() {
    const message = this._tracking.message;
    if (!message) return "";
    return `<div class="pmsg">💬 ${esc(message)}</div>`;
  }

  _renderFooter() {
    const t = this._tracking;
    const mapUrl = safeUrl(t.tracking_url);
    const link = mapUrl
      ? `<a class="maplink" href="${esc(mapUrl)}" target="_blank" rel="noopener noreferrer">Open live map ↗</a>`
      : "";
    const fetched = t.fetched_at ? new Date(t.fetched_at) : null;
    const updated =
      fetched && !Number.isNaN(fetched.getTime())
        ? `<span class="updated">Updated ${fetched.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>`
        : "";
    if (!link && !updated) return "";
    return `<div class="footer">${link}${updated}</div>`;
  }

  // ---- helpers ---------------------------------------------------------------

  // ETA renders as a local wall-clock time; the day is added only when it isn't today
  // (a late-evening box can slip past midnight).
  _fmtEta(iso) {
    if (!iso) return null;
    const when = new Date(iso);
    if (Number.isNaN(when.getTime())) return null;
    const time = when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const today = new Date();
    const sameDay =
      when.getFullYear() === today.getFullYear() &&
      when.getMonth() === today.getMonth() &&
      when.getDate() === today.getDate();
    if (sameDay) return time;
    return `${when.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })}, ${time}`;
  }

  static _sheet() {
    if (!HelloFreshDeliveryTrackingCard.__sheet) {
      const sheet = new CSSStyleSheet();
      sheet.replaceSync(HelloFreshDeliveryTrackingCard._styles());
      HelloFreshDeliveryTrackingCard.__sheet = sheet;
    }
    return HelloFreshDeliveryTrackingCard.__sheet;
  }

  static _styles() {
    return `
      ha-card { padding: 16px; }
      .head { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
      .head .logo { height: 40px; width: 40px; border-radius: 8px; object-fit: cover; flex: none; }
      .title-text { font-size: 1.5em; font-weight: 500; }
      .state { text-align: center; padding: 28px 8px; color: var(--secondary-text-color); }
      .state.error { color: var(--error-color, #db4437); }
      .reloading { opacity: 0.6; transition: opacity 0.2s; }
      .notice {
        display: flex; align-items: center; gap: 10px; margin-bottom: 12px;
        padding: 8px 12px; border-radius: 10px; font-size: 0.85em;
        background: color-mix(in srgb, var(--error-color, #db4437) 12%, transparent);
        color: var(--error-color, #db4437);
      }
      .banner {
        display: flex; align-items: center; gap: 10px; margin-bottom: 12px;
        padding: 10px 14px; border-radius: 10px; color: #fff; font-size: 0.92em;
      }
      .banner.warn { background: var(--warning-color, #ff9800); }
      .banner.error { background: var(--error-color, #db4437); }
      .banner-icon { font-size: 1.2em; flex: none; }
      .empty { text-align: center; padding: 20px 12px 24px; }
      .empty-icon { font-size: 2.2em; margin-bottom: 8px; }
      .empty-title { font-weight: 600; margin-bottom: 6px; }
      .empty-text {
        color: var(--secondary-text-color); font-size: 0.9em; max-width: 420px; margin: 0 auto;
      }
      .timeline {
        display: flex; align-items: flex-start; margin: 8px 4px 16px;
      }
      .step { display: flex; flex-direction: column; align-items: center; gap: 6px; flex: none; width: 84px; }
      .dot {
        width: 26px; height: 26px; border-radius: 50%; display: flex; align-items: center;
        justify-content: center; font-size: 0.8em; font-weight: 700; color: #fff;
        background: var(--disabled-color, #9e9e9e); border: 2px solid transparent;
      }
      .step.past .dot { background: var(--primary-color); }
      .step.current .dot {
        background: var(--primary-color);
        box-shadow: 0 0 0 4px color-mix(in srgb, var(--primary-color) 25%, transparent);
      }
      .slabel { font-size: 0.75em; text-align: center; color: var(--secondary-text-color); }
      .step.current .slabel { color: var(--primary-text-color); font-weight: 600; }
      .bar { flex: 1; height: 3px; border-radius: 2px; margin-top: 12px; background: var(--divider-color); min-width: 16px; }
      .bar.past { background: var(--primary-color); }
      .grid {
        display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
        gap: 10px 16px; margin-bottom: 12px;
      }
      .item { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
      .ilabel {
        font-size: 0.68em; text-transform: uppercase; letter-spacing: 0.04em;
        color: var(--secondary-text-color);
      }
      .ival { font-size: 1.05em; font-weight: 600; word-break: break-word; }
      .pmsg {
        padding: 10px 12px; border-radius: 10px; background: var(--secondary-background-color);
        font-size: 0.9em; margin-bottom: 12px;
      }
      .footer { display: flex; align-items: center; gap: 12px; }
      .maplink { color: var(--primary-color); font-size: 0.9em; text-decoration: none; }
      .maplink:hover { text-decoration: underline; }
      .updated { margin-left: auto; font-size: 0.78em; color: var(--secondary-text-color); }
      /* Identical to the other cards' ↻ pill. */
      .refreshbtn {
        margin-left: auto; flex: none;
        font-size: 0.85em; padding: 5px 12px; border-radius: 14px;
        border: 1px solid var(--divider-color); background: var(--card-background-color);
        color: var(--primary-text-color); cursor: pointer;
      }
      .refreshbtn:disabled { opacity: 0.5; cursor: default; }
      .actions { text-align: center; margin-top: 8px; }
      .actions button { padding: 6px 16px; border-radius: 8px; cursor: pointer; }
    `;
  }
}

customElements.define("hellofresh-delivery-tracking-card", HelloFreshDeliveryTrackingCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "hellofresh-delivery-tracking-card",
  name: "HelloFresh Delivery Tracking Card",
  description:
    "Live last-mile box tracking: phase, ETA, stops before you, and driver (Netherlands only).",
});

console.info(
  `%c HELLOFRESH-DELIVERY-TRACKING-CARD %c v${TRACKING_CARD_VERSION} `,
  "color: #fff; background: #91c11e; font-weight: 700;",
  "color: #91c11e; background: #333; font-weight: 700;"
);
