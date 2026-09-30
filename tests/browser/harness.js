import * as mdi from "/mdi.js";
import { buildHass } from "/fixtures.js";

const params = new URLSearchParams(location.search);
const dark = params.get("theme") === "dark";
if (dark) document.documentElement.classList.add("dark");

// ha-card stand-in with Home Assistant's default card look.
customElements.define(
  "ha-card",
  class extends HTMLElement {
    constructor() {
      super();
      const root = this.attachShadow({ mode: "open" });
      root.innerHTML = `<style>:host{display:block;position:relative;box-sizing:border-box;
        background:var(--ha-card-background,var(--card-background-color,#fff));border-radius:var(--ha-card-border-radius,12px);
        border:1px solid var(--divider-color);color:var(--primary-text-color)}</style><slot></slot>`;
    }
  }
);

// ha-icon stand-in drawing the real MDI path for mdi:* names.
customElements.define(
  "ha-icon",
  class extends HTMLElement {
    static get observedAttributes() {
      return ["icon"];
    }
    constructor() {
      super();
      this.attachShadow({ mode: "open" });
    }
    connectedCallback() {
      this._render();
    }
    attributeChangedCallback() {
      this._render();
    }
    _render() {
      const name = this.getAttribute("icon") || "";
      const key = "mdi" + name.replace(/^mdi:/, "").split("-").map((p) => p.charAt(0).toUpperCase() + p.slice(1)).join("");
      const d = mdi[key];
      if (!d) console.warn("missing icon", name);
      this.shadowRoot.innerHTML = `<style>:host{display:inline-flex;align-items:center;justify-content:center;
        width:var(--mdc-icon-size,24px);height:var(--mdc-icon-size,24px);flex:none}svg{width:100%;height:100%;fill:currentColor}</style>
        <svg viewBox="0 0 24 24"><path d="${d || "M4 4h16v16H4z"}"/></svg>`;
    }
  }
);

const { hass, calls, fx } = buildHass(location.origin, {
  dark,
  scenario: params.get("scenario") || "default",
  latency: Number(params.get("latency") || 200),
  fail: params.get("fail") || "",
  language: params.get("lang") || "en",
});
window.__hass = hass;
window.__calls = calls;
window.__fx = fx;

const width = params.get("width");
if (width) document.getElementById("wrap").style.maxWidth = `${width}px`;

// ?panel=1 mounts the sidebar panel (hellofresh-panel.js) the way Home Assistant does: the panel
// module, then an element given hass / narrow / panel. &frame=ha draws a Home Assistant-style
// sidebar beside it (for screenshots); &narrow=1 is a phone; &docked=always_hidden hides the
// sidebar on a wide screen.
if (params.get("panel")) {
  hass.dockedSidebar = params.get("docked") || "docked";
  const narrow = params.get("narrow") === "1";
  const frame = params.get("frame") === "ha" && !narrow;
  document.body.innerHTML = `<div class="ha-frame${frame ? " framed" : ""}">${frame ? sidebarHtml() : ""}<div class="ha-main" id="main"></div></div>`;
  await import(`/hellofresh/hellofresh-panel.js?v=dev`);
  const panel = document.createElement("hellofresh-panel");
  let config = { _panel_custom: { name: "hellofresh-panel" } };
  if (params.get("config")) config = { ...config, ...JSON.parse(params.get("config")) };
  if (params.get("view")) config.default_view = params.get("view");
  panel.panel = { component_name: "custom", url_path: "hellofresh-app", title: "HelloFresh", config };
  panel.narrow = narrow;
  panel.hass = hass;
  // Home Assistant's route for the panel: what follows /hellofresh-app in the address.
  const APP = "/hellofresh-app";
  panel.route = { prefix: APP, path: location.pathname.startsWith(APP) ? location.pathname.slice(APP.length) : "" };
  document.getElementById("main").appendChild(panel);
  window.__panel = panel;
  window.__toggles = 0;
  window.addEventListener("hass-toggle-menu", () => (window.__toggles += 1));
  await customElements.whenDefined("hellofresh-card");
  await new Promise((resolve) => {
    const find = () => {
      const card = panel.shadowRoot.querySelector("hellofresh-card");
      if (card) resolve((window.__card = card));
      else setTimeout(find, 20);
    };
    find();
  });
  window.__ready = true;
} else if (params.get("classic")) {
  // ?classic=hellofresh-schedule-card mounts one of the (deprecated) classic cards.
  const type = params.get("classic");
  await import(`/hellofresh/${type}.js?v=dev`);
  const card = document.createElement(type);
  card.setConfig({ type: `custom:${type}`, ...(params.get("config") ? JSON.parse(params.get("config")) : {}) });
  card.hass = hass;
  document.getElementById("wrap").appendChild(card);
  window.__card = card;
  window.__ready = true;
} else {
  await import(`/hellofresh/hellofresh-card.js?v=dev`);
  const card = document.createElement("hellofresh-card");
  let config = { type: "custom:hellofresh-card" };
  if (params.get("config")) config = { ...config, ...JSON.parse(params.get("config")) };
  if (params.get("view")) config.default_view = params.get("view");
  card.setConfig(config);
  card.hass = hass;
  document.getElementById("wrap").appendChild(card);
  window.__card = card;
  window.__ready = true;
}

// A Home Assistant-style sidebar with HelloFresh selected (screenshots only).
function sidebarHtml() {
  const item = (icon, label, on = false) =>
    `<div class="ha-item${on ? " on" : ""}"><ha-icon icon="${icon}"></ha-icon><span>${label}</span></div>`;
  return `<aside class="ha-sidebar">
      <div class="ha-brand"><ha-icon icon="mdi:menu"></ha-icon><span>Home Assistant</span></div>
      <nav>
        ${item("mdi:view-dashboard", "Overview")}
        ${item("mdi:lightning-bolt", "Energy")}
        ${item("mdi:tooltip-account", "Map")}
        ${item("mdi:format-list-bulleted-type", "Logbook")}
        ${item("mdi:chart-box", "History")}
        ${item("mdi:silverware-variant", "HelloFresh", true)}
        ${item("mdi:clipboard-list", "To-do lists")}
        ${item("mdi:play-box-multiple", "Media")}
      </nav>
      <div class="ha-spacer"></div>
      <nav class="ha-bottom">
        ${item("mdi:hammer", "Developer tools")}
        ${item("mdi:cog", "Settings")}
        ${item("mdi:bell-outline", "Notifications")}
        <div class="ha-item"><span class="ha-avatar">A</span><span>Alex</span></div>
      </nav>
    </aside>`;
}
