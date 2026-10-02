/*
 * HelloFresh card — styles
 * ------------------------
 * The unified card's design system: tokens first, then components. Everything is scoped to the
 * card's shadow root. Colours come from two places:
 *
 *   * Home Assistant's theme variables for surfaces and text (so any theme, light or dark,
 *     keeps the card legible), and
 *   * a small HelloFresh brand palette for the accent (buttons, selection, progress). The
 *     `accent: theme` option swaps the brand accent for the HA theme's primary colour.
 *
 * Dark mode follows HA's own flag (the host gets a [dark] attribute from hass.themes.darkMode),
 * not prefers-color-scheme — the two differ whenever a user pins a theme.
 *
 * Layout adapts to the CARD's width through container queries, not the viewport: the same card
 * can sit in a phone, a narrow sections column, or a full-width panel view.
 */

export const CARD_STYLES = `
  :host {
    display: block;
    --hf-lime: #91c11e;
    --hf-lime-ink: #1d2b08;
    --hf-forest: #067a46;
    --hf-accent: var(--hf-forest);
    --hf-accent-ink: #fff;
    --hf-ring: var(--hf-lime);
    --hf-surface: var(--ha-card-background, var(--card-background-color, #fff));
    --hf-text: var(--primary-text-color, #1f2328);
    --hf-muted: var(--secondary-text-color, #6b7280);
    --hf-border: var(--divider-color, rgba(0, 0, 0, 0.12));
    --hf-surface-2: color-mix(in srgb, var(--hf-text) 4%, var(--hf-surface));
    --hf-surface-3: color-mix(in srgb, var(--hf-text) 8%, var(--hf-surface));
    --hf-ok-fg: #3b6d0c;
    --hf-ok-bg: color-mix(in srgb, var(--hf-lime) 20%, var(--hf-surface));
    --hf-warn-fg: #92400e;
    --hf-warn-bg: color-mix(in srgb, #f59e0b 20%, var(--hf-surface));
    --hf-info-fg: #1d4ed8;
    --hf-info-bg: color-mix(in srgb, #3b82f6 15%, var(--hf-surface));
    --hf-danger-fg: #b91c1c;
    --hf-danger-bg: color-mix(in srgb, #ef4444 14%, var(--hf-surface));
    --hf-muted-bg: var(--hf-surface-3);
    --hf-radius: 14px;
    --hf-radius-sm: 10px;
    --hf-radius-lg: 20px;
    --hf-shadow: 0 1px 2px rgba(16, 24, 16, 0.06), 0 6px 20px rgba(16, 24, 16, 0.07);
    --hf-shadow-lg: 0 12px 40px rgba(0, 0, 0, 0.22);
    --hf-gap: 16px;
  }
  :host([dark]) {
    --hf-accent: var(--hf-lime);
    --hf-accent-ink: var(--hf-lime-ink);
    --hf-ok-fg: #b8e05a;
    --hf-ok-bg: color-mix(in srgb, var(--hf-lime) 18%, var(--hf-surface));
    --hf-warn-fg: #fbbf24;
    --hf-warn-bg: color-mix(in srgb, #f59e0b 18%, var(--hf-surface));
    --hf-info-fg: #93c5fd;
    --hf-info-bg: color-mix(in srgb, #3b82f6 20%, var(--hf-surface));
    --hf-danger-fg: #fca5a5;
    --hf-danger-bg: color-mix(in srgb, #ef4444 18%, var(--hf-surface));
    --hf-shadow: 0 1px 2px rgba(0, 0, 0, 0.35), 0 8px 24px rgba(0, 0, 0, 0.3);
  }
  :host([accent="theme"]) {
    --hf-accent: var(--primary-color, #03a9f4);
    --hf-accent-ink: var(--text-primary-color, #fff);
    --hf-ring: var(--primary-color, #03a9f4);
  }

  * { box-sizing: border-box; }
  ha-card { overflow: visible; }
  button { font: inherit; color: inherit; }
  a { color: var(--hf-accent); }
  :host([dark]) a { color: var(--hf-ok-fg); }
  ha-icon { --mdc-icon-size: 20px; display: inline-flex; flex: none; }
  .sr-only {
    position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0);
    white-space: nowrap;
  }

  /* ---- app frame ------------------------------------------------------------------------ */
  .hf-app { container-type: inline-size; container-name: hfapp; color: var(--hf-text); }
  .hf-appbar {
    display: flex; align-items: center; gap: 12px; padding: 14px 18px 10px;
  }
  .hf-logo { width: 38px; height: 38px; border-radius: 10px; object-fit: cover; flex: none; }
  .hf-apptitle { flex: 1; min-width: 0; }
  .hf-apptitle h1 {
    margin: 0; font-size: 1.35em; font-weight: 700; letter-spacing: -0.01em; line-height: 1.2;
  }
  .hf-apptitle .hf-sub {
    margin-top: 2px; font-size: 0.82em; color: var(--hf-muted);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .hf-appactions { display: flex; align-items: center; gap: 6px; flex: none; }
  /* Full screen as the sidebar panel (hellofresh-panel.js): on phones the card runs edge to edge
     and its header carries the button that opens Home Assistant's sidebar. */
  .hf-menubtn { margin: 0 -6px 0 -8px; color: var(--hf-text); }
  :host([panel][narrow]) ha-card { border: none; border-radius: 0; box-shadow: none; }

  .hf-tabs {
    display: flex; gap: 2px; padding: 0 12px; border-bottom: 1px solid var(--hf-border);
    overflow-x: auto; scrollbar-width: none;
  }
  .hf-tabs::-webkit-scrollbar { display: none; }
  .hf-tab {
    position: relative; display: inline-flex; align-items: center; gap: 8px;
    padding: 10px 12px 12px; border: none; background: none; cursor: pointer;
    color: var(--hf-muted); font-weight: 600; font-size: 0.92em; white-space: nowrap;
    border-radius: 10px 10px 0 0;
  }
  .hf-tab ha-icon { --mdc-icon-size: 19px; }
  .hf-tab:hover { color: var(--hf-text); background: var(--hf-surface-2); }
  .hf-tab[aria-current="page"] { color: var(--hf-text); }
  .hf-tab[aria-current="page"]::after {
    content: ""; position: absolute; left: 10px; right: 10px; bottom: -1px; height: 3px;
    border-radius: 3px 3px 0 0; background: var(--hf-accent);
  }
  .hf-tab .hf-count {
    min-width: 18px; height: 18px; padding: 0 5px; border-radius: 9px;
    display: inline-flex; align-items: center; justify-content: center;
    font-size: 0.72em; font-weight: 700; background: var(--hf-warn-bg); color: var(--hf-warn-fg);
  }
  .hf-tab:focus-visible, .hf-btn:focus-visible, .hf-chip:focus-visible,
  .hf-weekchip:focus-visible, .hf-tile:focus-visible, .hf-row:focus-visible,
  .hf-iconbtn:focus-visible, .hf-cal-day:focus-visible {
    outline: 2px solid var(--hf-accent); outline-offset: 2px;
  }

  .hf-banners { display: flex; flex-direction: column; gap: 8px; padding: 12px 18px 0; }
  .hf-banners:empty { display: none; }
  .hf-main { padding: 16px 18px 20px; }
  /* Phones and narrow columns: the tab bar becomes an evenly spread icon-over-label bar. */
  @container hfapp (max-width: 520px) {
    .hf-appbar { padding: 12px 14px 6px; }
    .hf-tabs { padding: 0 4px; gap: 0; }
    .hf-tab {
      flex: 1 1 0; min-width: 0; flex-direction: column; gap: 3px; padding: 8px 2px 10px;
      font-size: 0.72em; border-radius: 10px 10px 0 0;
    }
    .hf-tab ha-icon { --mdc-icon-size: 22px; }
    .hf-tab span:not(.hf-count) { max-width: 100%; overflow: hidden; text-overflow: ellipsis; }
    .hf-tab .hf-count { position: absolute; top: 4px; left: calc(50% + 6px); font-size: 0.95em; }
    .hf-tab[aria-current="page"]::after { left: 22%; right: 22%; }
    .hf-banners { padding: 10px 12px 0; }
    .hf-main { padding: 14px 12px 18px; }
    .hf-hero { padding: 16px; }
    .hf-hero .hf-herodate { font-size: 1.5em; }
  }
  .hf-main.reloading { opacity: 0.7; transition: opacity 0.2s; }

  /* ---- type ---------------------------------------------------------------------------- */
  .hf-h2 { margin: 0; font-size: 1.12em; font-weight: 700; letter-spacing: -0.005em; }
  .hf-h3 { margin: 0; font-size: 0.98em; font-weight: 700; }
  .hf-eyebrow {
    font-size: 0.7em; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;
    color: var(--hf-muted);
  }
  .hf-muted { color: var(--hf-muted); }
  .hf-small { font-size: 0.84em; }
  .hf-strong { font-weight: 700; }
  .hf-urgent { color: var(--hf-danger-fg); font-weight: 700; }
  .hf-sectionhead {
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    margin: 26px 0 12px;
  }
  .hf-sectionhead:first-child { margin-top: 4px; }
  .hf-sectionhead .hf-sectionnote { font-size: 0.85em; color: var(--hf-muted); }
  .hf-empty {
    padding: 36px 16px; text-align: center; color: var(--hf-muted); font-size: 0.95em;
  }
  .hf-empty ha-icon { --mdc-icon-size: 36px; display: block; margin: 0 auto 8px; opacity: 0.6; }

  /* ---- buttons ------------------------------------------------------------------------- */
  .hf-btn {
    display: inline-flex; align-items: center; justify-content: center; gap: 6px;
    height: 36px; padding: 0 16px; border-radius: 999px; cursor: pointer;
    border: 1px solid var(--hf-border); background: var(--hf-surface); color: var(--hf-text);
    font-weight: 600; font-size: 0.9em; white-space: nowrap;
    transition: background 0.15s, border-color 0.15s, filter 0.15s;
  }
  .hf-btn ha-icon { --mdc-icon-size: 18px; }
  .hf-btn:hover:not(:disabled) { background: var(--hf-surface-2); }
  .hf-btn.primary {
    background: var(--hf-accent); border-color: var(--hf-accent); color: var(--hf-accent-ink);
  }
  .hf-btn.primary:hover:not(:disabled) { background: var(--hf-accent); filter: brightness(1.08); }
  .hf-btn.ghost { border-color: transparent; background: transparent; }
  .hf-btn.ghost:hover:not(:disabled) { background: var(--hf-surface-2); }
  .hf-btn.danger { color: var(--hf-danger-fg); }
  .hf-btn.sm { height: 30px; padding: 0 12px; font-size: 0.84em; }
  .hf-btn.block { width: 100%; }
  .hf-btn:disabled { opacity: 0.45; cursor: default; }
  .hf-iconbtn {
    display: inline-flex; align-items: center; justify-content: center;
    width: 36px; height: 36px; border-radius: 50%; border: none; cursor: pointer;
    background: transparent; color: var(--hf-muted);
  }
  .hf-iconbtn:hover:not(:disabled) { background: var(--hf-surface-2); color: var(--hf-text); }
  .hf-iconbtn:disabled { opacity: 0.4; cursor: default; }
  .hf-iconbtn.spin ha-icon { animation: hf-spin 0.9s linear infinite; }
  .hf-link {
    border: none; background: none; padding: 0; cursor: pointer; color: var(--hf-accent);
    font-weight: 600; text-decoration: underline; text-underline-offset: 2px;
  }
  :host([dark]) .hf-link:not(.quiet) { color: var(--hf-ok-fg); }
  .hf-actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }

  /* ---- pills, chips, notices ------------------------------------------------------------ */
  .hf-pill {
    display: inline-flex; align-items: center; gap: 5px; height: 24px; padding: 0 10px;
    border-radius: 999px; font-size: 0.76em; font-weight: 700; white-space: nowrap;
    background: var(--hf-muted-bg); color: var(--hf-muted);
  }
  .hf-pill ha-icon { --mdc-icon-size: 15px; }
  .tone-ok { background: var(--hf-ok-bg); color: var(--hf-ok-fg); }
  .tone-warn { background: var(--hf-warn-bg); color: var(--hf-warn-fg); }
  .tone-info { background: var(--hf-info-bg); color: var(--hf-info-fg); }
  .tone-danger { background: var(--hf-danger-bg); color: var(--hf-danger-fg); }
  .tone-muted { background: var(--hf-muted-bg); color: var(--hf-muted); }

  .hf-chip {
    display: inline-flex; align-items: center; gap: 6px; height: 32px; padding: 0 13px;
    border-radius: 999px; border: 1px solid var(--hf-border); background: var(--hf-surface);
    color: var(--hf-text); cursor: pointer; font-size: 0.85em; font-weight: 600;
    white-space: nowrap; transition: background 0.15s, border-color 0.15s;
  }
  .hf-chip:hover { border-color: color-mix(in srgb, var(--hf-accent) 50%, var(--hf-border)); }
  .hf-chip.on {
    background: color-mix(in srgb, var(--hf-accent) 14%, var(--hf-surface));
    border-color: var(--hf-accent); color: var(--hf-text);
  }
  .hf-chip .hf-dot { width: 9px; height: 9px; border-radius: 50%; flex: none; }
  .hf-chip ha-icon { --mdc-icon-size: 16px; }
  .hf-chip .hf-x { --mdc-icon-size: 14px; opacity: 0.7; margin-right: -4px; }
  .hf-chiprow { display: flex; flex-wrap: wrap; gap: 8px; }
  .hf-rail {
    display: flex; gap: 8px; overflow-x: auto; padding: 2px 2px 6px; scrollbar-width: thin;
    scroll-snap-type: x proximity;
  }
  .hf-rail > * { scroll-snap-align: start; flex: none; }

  .hf-notice {
    display: flex; align-items: flex-start; gap: 12px; padding: 12px 14px;
    border-radius: var(--hf-radius); font-size: 0.9em; line-height: 1.4;
  }
  .hf-notice ha-icon { --mdc-icon-size: 20px; margin-top: 1px; }
  .hf-notice .hf-noticebody { flex: 1; min-width: 0; }
  .hf-notice .hf-noticebody strong { font-weight: 700; }
  .hf-notice .hf-btn { flex: none; }
  .hf-notice.clickable { cursor: pointer; }

  /* ---- panels & stats ------------------------------------------------------------------- */
  .hf-panel {
    background: var(--hf-surface); border: 1px solid var(--hf-border);
    border-radius: var(--hf-radius); padding: 16px;
  }
  .hf-panel.flat { background: var(--hf-surface-2); border-color: transparent; }
  .hf-cols { display: grid; gap: var(--hf-gap); grid-template-columns: 1fr; }
  @container hfapp (min-width: 900px) {
    .hf-cols.two { grid-template-columns: minmax(0, 3fr) minmax(0, 2fr); }
    .hf-cols.halves { grid-template-columns: 1fr 1fr; }
  }
  .hf-stats { display: grid; gap: 10px; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); }
  .hf-stat {
    padding: 12px 14px; border-radius: var(--hf-radius-sm); background: var(--hf-surface-2);
    display: flex; flex-direction: column; gap: 3px; min-width: 0;
  }
  .hf-stat .hf-statlabel {
    font-size: 0.72em; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase;
    color: var(--hf-muted);
  }
  .hf-stat .hf-statvalue { font-size: 1.05em; font-weight: 700; word-break: break-word; }
  .hf-stat .hf-statnote { font-size: 0.78em; color: var(--hf-muted); }
  .hf-stat.big .hf-statvalue { font-size: 1.6em; letter-spacing: -0.02em; }
  .hf-stat.accent .hf-statvalue { color: var(--hf-ok-fg); }
  .hf-stat.clickable { cursor: pointer; }
  .hf-stat.clickable:hover { background: var(--hf-surface-3); }
  .hf-kv { display: grid; gap: 10px 18px; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); }
  .hf-kv .hf-k {
    font-size: 0.72em; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase;
    color: var(--hf-muted);
  }
  .hf-kv .hf-v { font-size: 0.93em; font-weight: 600; margin-top: 2px; word-break: break-word; }
  .hf-kv .wide { grid-column: 1 / -1; }
  .hf-kv .clickable { cursor: pointer; }
  .hf-kv .clickable .hf-v {
    text-decoration: underline; text-underline-offset: 3px;
    text-decoration-color: color-mix(in srgb, currentColor 35%, transparent);
  }

  /* ---- overview: hero ------------------------------------------------------------------- */
  .hf-hero {
    position: relative; overflow: hidden; border-radius: var(--hf-radius-lg);
    border: 1px solid color-mix(in srgb, var(--hf-lime) 35%, var(--hf-border));
    background:
      radial-gradient(120% 140% at 100% 0%, color-mix(in srgb, var(--hf-lime) 22%, transparent), transparent 55%),
      linear-gradient(180deg, color-mix(in srgb, var(--hf-lime) 9%, var(--hf-surface)), var(--hf-surface));
    padding: 20px; display: grid; gap: 20px; grid-template-columns: 1fr;
  }
  @container hfapp (min-width: 760px) {
    .hf-hero { grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr); padding: 24px; }
  }
  .hf-hero .hf-herodate {
    margin: 6px 0 2px; font-size: 1.75em; font-weight: 800; letter-spacing: -0.02em; line-height: 1.15;
  }
  .hf-hero .hf-heromain { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
  .hf-hero .hf-herometa { display: flex; flex-wrap: wrap; gap: 6px 14px; color: var(--hf-muted); font-size: 0.9em; }
  .hf-hero .hf-herometa span { display: inline-flex; align-items: center; gap: 5px; }
  .hf-hero .hf-herometa ha-icon { --mdc-icon-size: 16px; }
  .hf-deadline {
    display: flex; align-items: center; gap: 10px; padding: 10px 12px; border-radius: 12px;
    background: color-mix(in srgb, var(--hf-surface) 70%, transparent); border: 1px solid var(--hf-border);
    font-size: 0.9em;
  }
  .hf-deadline.urgent { border-color: color-mix(in srgb, #ef4444 45%, var(--hf-border)); }
  .hf-deadline ha-icon { --mdc-icon-size: 20px; color: var(--hf-muted); }
  .hf-deadline.urgent ha-icon { color: var(--hf-danger-fg); }
  .hf-heromeals { display: grid; gap: 10px; grid-template-columns: repeat(2, minmax(0, 1fr)); align-content: start; }
  .hf-heromeals.cols-1 { grid-template-columns: minmax(0, 1fr); }
  .hf-heromeals.cols-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  @container hfapp (max-width: 420px) {
    .hf-heromeals.cols-3 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
  .hf-linkrow { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 18px; margin-top: -2px; }
  .hf-link.quiet {
    display: inline-flex; align-items: center; gap: 5px; color: var(--hf-muted); font-size: 0.86em;
    text-decoration: none; padding: 4px 0;
  }
  .hf-link.quiet ha-icon { --mdc-icon-size: 16px; }
  .hf-link.quiet:hover:not(:disabled) { color: var(--hf-text); text-decoration: underline; }
  .hf-link:disabled { opacity: 0.45; cursor: default; }
  .hf-heromeal {
    display: flex; flex-direction: column; border-radius: 12px; overflow: hidden; cursor: pointer;
    background: var(--hf-surface); border: 1px solid var(--hf-border); min-width: 0;
  }
  .hf-heromeal img, .hf-heromeal .hf-noimg { width: 100%; aspect-ratio: 16 / 10; object-fit: cover; display: block; }
  .hf-heromeal .hf-heromealname {
    padding: 7px 9px; font-size: 0.8em; font-weight: 600; line-height: 1.25;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  }
  .hf-heromeal .hf-heromealopt { padding: 0 9px 7px; margin-top: -4px; font-size: 0.72em; font-weight: 700; color: var(--hf-warn-fg); }
  .hf-heromeal .hf-qtytag {
    position: absolute; top: 6px; right: 6px;
  }
  .hf-heromealwrap { position: relative; }
  .hf-heromeals .hf-more {
    display: flex; align-items: center; justify-content: center; border-radius: 12px;
    border: 1px dashed var(--hf-border); color: var(--hf-muted); font-weight: 600; font-size: 0.85em;
    min-height: 80px; cursor: pointer; background: transparent;
  }
  .hf-heroempty {
    display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px;
    border-radius: 14px; border: 1px dashed var(--hf-border); padding: 24px; text-align: center;
    color: var(--hf-muted); font-size: 0.9em;
  }
  .hf-heroempty ha-icon { --mdc-icon-size: 32px; opacity: 0.7; }

  /* ---- tracking ------------------------------------------------------------------------- */
  .hf-tracker { display: flex; flex-direction: column; gap: 10px; }
  .hf-steps { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 6px; }
  .hf-step { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
  .hf-step .hf-stepbar { height: 6px; border-radius: 3px; background: var(--hf-surface-3); }
  .hf-step.done .hf-stepbar { background: var(--hf-lime); }
  .hf-step.current .hf-stepbar {
    background: linear-gradient(90deg, var(--hf-lime) 55%, var(--hf-surface-3) 55%);
  }
  .hf-step .hf-steplabel {
    font-size: 0.74em; font-weight: 600; color: var(--hf-muted);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .hf-step.done .hf-steplabel, .hf-step.current .hf-steplabel { color: var(--hf-text); }
  .hf-trackline { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 10px; font-size: 0.85em; color: var(--hf-muted); }
  .hf-trackline strong { color: var(--hf-text); font-weight: 600; }
  .hf-history {
    list-style: none; margin: 0; padding: 0 0 0 12px; border-left: 2px solid var(--hf-border);
    font-size: 0.82em; color: var(--hf-muted); display: flex; flex-direction: column; gap: 3px;
  }
  .hf-history li:first-child { color: var(--hf-text); }
  .hf-history .hf-when { display: inline-block; min-width: 9.5em; margin-right: 6px; }
  .hf-rowchev { display: inline-flex; color: var(--hf-muted); }
  .hf-rowchev ha-icon { --mdc-icon-size: 20px; }
  .hf-podhint { display: inline-flex; align-items: center; gap: 3px; }
  .hf-podhint ha-icon { --mdc-icon-size: 15px; }
  /* delivery details sheet */
  .hf-dsec { display: flex; flex-direction: column; gap: 8px; }
  .hf-dsec h3 {
    margin: 0; font-size: 0.74em; font-weight: 800; text-transform: uppercase; letter-spacing: 0.06em; color: var(--hf-muted);
  }
  .hf-dphotos { display: grid; gap: 8px; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); }
  .hf-dphotos img {
    width: 100%; aspect-ratio: 4 / 3; object-fit: cover; display: block;
    border-radius: var(--hf-radius-sm); border: 1px solid var(--hf-border);
  }
  .hf-dnote { display: flex; align-items: center; gap: 8px; font-size: 0.85em; color: var(--hf-muted); }
  .hf-dnote ha-icon { --mdc-icon-size: 18px; flex: none; }
  .hf-dcarrier { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 0.9em; font-weight: 600; }
  .hf-dcarrier ha-icon { --mdc-icon-size: 18px; color: var(--hf-muted); }
  .hf-dcarrier a { color: var(--hf-accent); text-underline-offset: 2px; }
  :host([dark]) .hf-dcarrier a { color: var(--hf-ok-fg); }
  .hf-timeline { list-style: none; margin: 0; padding: 0 0 0 6px; display: flex; flex-direction: column; }
  .hf-timeline li {
    position: relative; display: flex; flex-direction: column; gap: 1px; padding: 0 0 12px 18px;
    border-left: 2px solid var(--hf-border); font-size: 0.86em;
  }
  .hf-timeline li:last-child { border-left-color: transparent; padding-bottom: 0; }
  .hf-timeline li::before {
    content: ""; position: absolute; left: -7px; top: 2px; width: 12px; height: 12px; border-radius: 50%;
    background: var(--hf-surface); border: 2px solid var(--hf-border); box-sizing: border-box;
  }
  .hf-timeline li:first-child::before { background: var(--hf-accent); border-color: var(--hf-accent); }
  .hf-timeline .hf-when { font-size: 0.88em; color: var(--hf-muted); }
  .hf-dmeals { display: flex; flex-direction: column; gap: 2px; }
  .hf-dmeal {
    display: flex; align-items: center; gap: 12px; width: 100%; padding: 6px; text-align: left;
    border: none; border-radius: var(--hf-radius-sm); background: none; color: var(--hf-text); font: inherit;
  }
  button.hf-dmeal { cursor: pointer; }
  button.hf-dmeal:hover { background: var(--hf-surface-2); }
  .hf-dmeal img, .hf-dmeal .hf-noimg { width: 48px; height: 48px; border-radius: 10px; object-fit: cover; flex: none; }
  .hf-dmealtext { display: flex; flex-direction: column; min-width: 0; font-size: 0.88em; }
  .hf-dmealtext span:first-child { font-weight: 600; }
  .hf-dmealtext .hf-muted { font-size: 0.9em; }
  .hf-dline { display: flex; justify-content: space-between; gap: 12px; font-size: 0.9em; }
  .hf-pod { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 0.82em; color: var(--hf-muted); }
  .hf-pod img { width: 64px; height: 64px; object-fit: cover; border-radius: 8px; border: 1px solid var(--hf-border); display: block; }

  /* ---- overview: week cards & rows ------------------------------------------------------- */
  .hf-weekcards { display: grid; gap: 12px; grid-template-columns: repeat(auto-fill, minmax(270px, 1fr)); }
  .hf-weekcard {
    display: flex; flex-direction: column; gap: 12px; padding: 14px; cursor: pointer;
    border-radius: var(--hf-radius); border: 1px solid var(--hf-border); background: var(--hf-surface);
    transition: border-color 0.15s, box-shadow 0.15s;
  }
  .hf-weekcard:hover { border-color: color-mix(in srgb, var(--hf-accent) 45%, var(--hf-border)); box-shadow: var(--hf-shadow); }
  .hf-weekcard.skipped { background: var(--hf-surface-2); }
  .hf-weekcardhead { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }
  .hf-weekcardhead .hf-wdate { font-weight: 800; font-size: 1.05em; }
  .hf-weekcardhead .hf-wsub { font-size: 0.8em; color: var(--hf-muted); margin-top: 1px; }
  .hf-thumbs { display: flex; gap: 6px; min-height: 52px; }
  .hf-thumb {
    width: 52px; height: 52px; border-radius: 10px; object-fit: cover; flex: none;
    background: var(--hf-surface-3); border: 1px solid var(--hf-border);
  }
  .hf-thumb.more {
    display: flex; align-items: center; justify-content: center; font-size: 0.8em; font-weight: 700;
    color: var(--hf-muted);
  }
  .hf-thumbs.empty { align-items: center; color: var(--hf-muted); font-size: 0.85em; }
  .hf-weekcardfoot { display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap; }
  .hf-weekcardfoot .hf-wmeta { font-size: 0.84em; color: var(--hf-muted); }
  .hf-weekcardfoot .hf-wmeta strong { color: var(--hf-text); }

  /* A list sizes its rows to its own width: beside the calendar it is as narrow as a phone's. */
  .hf-rows {
    container-type: inline-size; container-name: hfrows;
    display: flex; flex-direction: column; border: 1px solid var(--hf-border); border-radius: var(--hf-radius); overflow: hidden;
  }
  .hf-row {
    display: grid; grid-template-columns: auto minmax(0, 1fr) fit-content(50%); align-items: start; gap: 12px;
    padding: 12px 14px; border-bottom: 1px solid var(--hf-border); cursor: pointer; background: var(--hf-surface);
  }
  .hf-row .hf-rowside { align-self: center; }
  .hf-thumbs.sm { min-height: 40px; gap: 5px; }
  .hf-thumbs.sm .hf-thumb { width: 40px; height: 40px; border-radius: 8px; }
  .hf-row:last-child { border-bottom: none; }
  .hf-row:hover { background: var(--hf-surface-2); }
  .hf-row.selected { box-shadow: inset 3px 0 0 var(--hf-ring); }
  .hf-row .hf-rowicon {
    width: 34px; height: 34px; border-radius: 50%; display: flex; align-items: center; justify-content: center;
  }
  .hf-row .hf-rowicon ha-icon { --mdc-icon-size: 18px; }
  .hf-row .hf-rowtitle { font-weight: 700; font-size: 0.95em; display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px; }
  .hf-row .hf-rowtitle .hf-muted { font-weight: 500; font-size: 0.9em; }
  .hf-row .hf-rowsub { font-size: 0.83em; color: var(--hf-muted); margin-top: 2px; }
  .hf-row .hf-rowsub strong { color: var(--hf-text); font-weight: 600; }
  .hf-row .hf-rowextra { margin-top: 8px; display: flex; flex-direction: column; gap: 8px; }
  .hf-row .hf-rowside { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
  .hf-rollup { padding: 10px 14px; font-size: 0.85em; color: var(--hf-muted); background: var(--hf-surface-2); border-bottom: 1px solid var(--hf-border); }
  /* Phone-narrow lists put every row's side under its text; a narrow list beside the calendar
     does so only for rows with buttons, so a delivered row stays one compact line. */
  @container hfrows (max-width: 440px) {
    .hf-row { grid-template-columns: auto minmax(0, 1fr); }
    .hf-row .hf-rowside { grid-column: 2; justify-content: flex-start; }
  }
  @container hfrows (max-width: 600px) {
    .hf-row.actions { grid-template-columns: auto minmax(0, 1fr); }
    .hf-row.actions .hf-rowside { grid-column: 2; justify-content: flex-start; }
  }
  .hf-dayopts { display: flex; flex-wrap: wrap; gap: 6px; }

  .hf-segment {
    display: inline-flex; padding: 3px; border-radius: 999px; background: var(--hf-surface-3); gap: 2px;
  }
  .hf-segment button {
    border: none; background: transparent; cursor: pointer; height: 30px; padding: 0 14px;
    border-radius: 999px; font-size: 0.84em; font-weight: 600; color: var(--hf-muted);
    display: inline-flex; align-items: center; gap: 6px;
  }
  .hf-segment button ha-icon { --mdc-icon-size: 16px; }
  .hf-segment button[aria-pressed="true"] {
    background: var(--hf-surface); color: var(--hf-text); box-shadow: 0 1px 3px rgba(0, 0, 0, 0.12);
  }

  /* ---- pantry --------------------------------------------------------------------------- */
  .hf-pantry { display: grid; gap: 2px 18px; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); }
  .hf-sheet .hf-pantry { grid-template-columns: 1fr; }
  .hf-pantryitem {
    display: flex; align-items: center; gap: 10px; padding: 7px 4px; border-radius: 8px;
    cursor: pointer; border: none; background: none; text-align: left; width: 100%;
  }
  .hf-pantryitem:hover { background: var(--hf-surface-2); }
  .hf-check {
    width: 20px; height: 20px; border-radius: 6px; border: 2px solid var(--hf-border); flex: none;
    display: flex; align-items: center; justify-content: center; color: var(--hf-accent-ink);
  }
  .hf-check ha-icon { --mdc-icon-size: 14px; }
  .hf-pantryitem.done .hf-check { background: var(--hf-accent); border-color: var(--hf-accent); }
  .hf-pantryitem .hf-pantrytext {
    flex: 1; min-width: 0; font-size: 0.9em; display: flex; flex-wrap: wrap; align-items: baseline; gap: 0 8px;
  }
  .hf-pantryitem.done .hf-pantrytext { text-decoration: line-through; color: var(--hf-muted); }
  .hf-pantryitem .hf-pantryamt { font-size: 0.86em; color: var(--hf-muted); white-space: nowrap; }

  /* ---- live tracking (the Netherlands, Germany) ---------------------------------------- */
  .hf-live {
    display: flex; flex-direction: column; gap: 10px; padding: 14px 16px; border-radius: var(--hf-radius);
    border: 1px solid color-mix(in srgb, var(--hf-accent) 35%, var(--hf-border));
    background: color-mix(in srgb, var(--hf-accent) 7%, var(--hf-surface));
  }
  .hf-live.tone-warn { border-color: color-mix(in srgb, var(--hf-warn-fg) 40%, var(--hf-border)); background: var(--hf-warn-bg); }
  .hf-live.tone-danger { border-color: color-mix(in srgb, var(--hf-danger-fg) 40%, var(--hf-border)); background: var(--hf-danger-bg); }
  .hf-livehead { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .hf-livebadge {
    display: inline-flex; align-items: center; gap: 6px; height: 22px; padding: 0 9px; border-radius: 11px;
    background: var(--hf-accent); color: var(--hf-accent-ink);
    font-size: 0.68em; font-weight: 800; letter-spacing: 0.08em; text-transform: uppercase;
  }
  .hf-livedot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; animation: hf-pulse 1.6s ease-in-out infinite; }
  .hf-livephase { flex: 1; min-width: 0; font-weight: 800; font-size: 1.02em; }
  .hf-live.tone-warn .hf-livephase { color: var(--hf-warn-fg); }
  .hf-live.tone-danger .hf-livephase { color: var(--hf-danger-fg); }
  .hf-livemap { display: inline-flex; align-items: center; gap: 4px; font-size: 0.86em; font-weight: 600; }
  .hf-livemap ha-icon { --mdc-icon-size: 16px; }
  .hf-livesteps { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; }
  .hf-livestep { display: flex; flex-direction: column; gap: 5px; font-size: 0.74em; font-weight: 600; color: var(--hf-muted); }
  .hf-livebar { height: 6px; border-radius: 3px; background: var(--hf-surface-3); }
  .hf-livestep.done, .hf-livestep.now { color: var(--hf-text); }
  .hf-livestep.done .hf-livebar { background: var(--hf-accent); }
  .hf-livestep.now .hf-livebar {
    background: linear-gradient(90deg, var(--hf-accent) 0 55%, var(--hf-surface-3) 55% 100%);
  }
  .hf-livefacts { display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 0.88em; }
  .hf-livefacts > span { display: inline-flex; align-items: center; gap: 6px; }
  .hf-livefacts ha-icon { --mdc-icon-size: 17px; color: var(--hf-muted); }
  .hf-livemsg { display: flex; align-items: flex-start; gap: 8px; font-size: 0.86em; color: var(--hf-muted); }
  .hf-livemsg ha-icon { --mdc-icon-size: 17px; flex: none; }
  .hf-livefoot { font-size: 0.74em; color: var(--hf-muted); }
  @keyframes hf-pulse { 50% { opacity: 0.3; } }

  /* ---- calendar ------------------------------------------------------------------------- */
  /* A month of rounded day tiles. The calendar is its own container: its rows are sized from
     its width, and beside the month's list it stretches to the list's height, its rows sharing
     the extra, so no blank band is left under the last week. */
  .hf-cal {
    container-type: inline-size; container-name: hfcal;
    display: flex; flex-direction: column; min-width: 0; padding: 14px;
    border: 1px solid var(--hf-border); border-radius: var(--hf-radius); background: var(--hf-surface);
  }
  .hf-calhead { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
  .hf-calnav { display: flex; align-items: center; gap: 6px; }
  .hf-calnav.end { justify-content: flex-end; }
  .hf-calhead .hf-caltitle { flex: 1; min-width: 0; text-align: center; font-weight: 800; font-size: 1.08em; letter-spacing: -0.01em; }
  .hf-calhead .hf-iconbtn { width: 34px; height: 34px; border: 1px solid var(--hf-border); color: var(--hf-text); }
  .hf-caldows, .hf-calgrid { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 6px; }
  .hf-caldows {
    margin-bottom: 8px; padding: 8px 6px; border-radius: var(--hf-radius-sm);
    background: color-mix(in srgb, var(--hf-accent) 11%, var(--hf-surface));
  }
  .hf-caldow {
    text-align: center; font-size: 0.74em; font-weight: 800; letter-spacing: 0.08em;
    text-transform: uppercase; color: var(--hf-text); white-space: nowrap; overflow: hidden;
  }
  .hf-caldow.weekend { color: var(--hf-muted); }
  .hf-caldow.today { color: var(--hf-accent); }
  :host([dark]) .hf-caldow.today { color: var(--hf-ok-fg); }
  .hf-caldow .hf-dowletter { display: none; }
  .hf-calgrid { flex: 1; grid-auto-rows: minmax(clamp(46px, 10.5cqi, 104px), 1fr); }
  .hf-cal-day {
    position: relative; min-width: 0; padding: 7px 8px; border: 1px solid var(--hf-border);
    border-radius: var(--hf-radius-sm); font-size: 0.85em; text-align: left;
    display: flex; flex-direction: column; justify-content: space-between; gap: 4px;
  }
  .hf-cal-day:not(.has) { background: var(--hf-surface); color: var(--hf-text); }
  .hf-cal-day.weekend:not(.has) { background: color-mix(in srgb, var(--hf-text) 2.5%, var(--hf-surface)); }
  .hf-cal-day.other { opacity: 0.45; }
  .hf-cal-day.other:not(.has) { border-color: transparent; background: none; }
  .hf-caltop { display: flex; align-items: flex-start; justify-content: space-between; gap: 4px; }
  .hf-calnum { font-size: 1.08em; font-weight: 600; font-variant-numeric: tabular-nums; line-height: 24px; }
  .hf-cal-day.past .hf-calnum { color: var(--hf-muted); }
  .hf-cal-day.has { cursor: pointer; border-color: color-mix(in srgb, currentColor 32%, transparent); }
  .hf-cal-day.has .hf-calnum { color: inherit; font-weight: 800; }
  .hf-cal-day.has:hover { filter: brightness(0.96); }
  :host([dark]) .hf-cal-day.has:hover { filter: brightness(1.18); }
  .hf-cal-day.skipped .hf-calnum { text-decoration: line-through; }
  .hf-cal-day.today .hf-calnum {
    min-width: 24px; height: 24px; padding: 0 6px; margin: -3px 0 0 -4px; border-radius: 12px;
    text-align: center; background: var(--hf-accent); color: var(--hf-accent-ink); font-weight: 800;
  }
  .hf-cal-day.selected { box-shadow: 0 0 0 2px var(--hf-ring); }
  .hf-calstate { display: flex; align-items: center; gap: 4px; min-width: 0; font-size: 0.82em; font-weight: 700; }
  .hf-calstate ha-icon { --mdc-icon-size: 16px; }
  .hf-calstatetext { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .hf-calholiday { display: inline-flex; }
  .hf-calholiday ha-icon { --mdc-icon-size: 15px; }
  .hf-callegend { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: 12px; }
  .hf-legendtoday { display: inline-flex; align-items: center; gap: 6px; padding: 0 6px; font-size: 0.78em; font-weight: 600; color: var(--hf-muted); }
  .hf-todaydot { width: 10px; height: 10px; border-radius: 50%; background: var(--hf-accent); }
  /* The month stays centred whether or not "Today" shows beside the arrows. */
  @container hfcal (min-width: 420px) {
    .hf-calhead { display: grid; grid-template-columns: 1fr auto 1fr; }
  }
  /* Wide enough for words: each delivery tile names its state, so the legend has nothing to add. */
  @container hfcal (min-width: 660px) {
    .hf-callegend { display: none; }
  }
  /* Narrower (phones, a sections column): the date and its state icon, centred. */
  @container hfcal (max-width: 659px) {
    .hf-caldows, .hf-calgrid { gap: 4px; }
    .hf-cal-day { padding: 4px 2px; align-items: center; justify-content: center; gap: 1px; }
    .hf-caltop { justify-content: center; }
    .hf-calholiday { position: absolute; top: 2px; right: 2px; }
    .hf-calholiday ha-icon { --mdc-icon-size: 12px; }
    .hf-calstatetext { display: none; }
    .hf-calstate ha-icon { --mdc-icon-size: 15px; }
    .hf-calnum { font-size: 1em; }
    .hf-cal-day.today .hf-calnum { margin: 0; min-width: 22px; height: 22px; line-height: 22px; padding: 0 4px; }
    .hf-caldow { letter-spacing: 0.03em; }
  }
  @container hfcal (max-width: 240px) {
    .hf-caldow { letter-spacing: 0; }
    .hf-caldow .hf-dowshort { display: none; }
    .hf-caldow .hf-dowletter { display: inline; }
  }
  @container hfapp (max-width: 520px) {
    .hf-cal { padding: 10px; }
  }

  /* ---- week strip ----------------------------------------------------------------------- */
  .hf-weekbar { display: flex; align-items: center; gap: 6px; margin-bottom: 14px; }
  .hf-weekstrip {
    flex: 1; display: flex; gap: 8px; overflow-x: auto; padding: 8px 2px 8px;
    scroll-snap-type: x proximity; scrollbar-width: none; scroll-behavior: smooth;
    -webkit-mask-image: linear-gradient(to right, transparent, #000 20px, #000 calc(100% - 20px), transparent);
    mask-image: linear-gradient(to right, transparent, #000 20px, #000 calc(100% - 20px), transparent);
  }
  .hf-weekstrip::-webkit-scrollbar { display: none; }
  .hf-weekstrip > :first-child { margin-left: 14px; }
  .hf-weekstrip > :last-child { margin-right: 14px; }
  .hf-weekchip {
    position: relative; flex: none; scroll-snap-align: center;
    display: flex; flex-direction: column; align-items: flex-start; gap: 2px;
    min-width: 92px; padding: 8px 12px 9px; border-radius: 14px; cursor: pointer;
    border: 1px solid var(--hf-border); background: var(--hf-surface); text-align: left;
  }
  .hf-weekchip:hover { border-color: color-mix(in srgb, var(--hf-accent) 45%, var(--hf-border)); }
  .hf-weekchip .hf-wcday { font-size: 0.68em; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; color: var(--hf-muted); }
  .hf-weekchip .hf-wcdate { font-size: 0.98em; font-weight: 800; }
  .hf-weekchip .hf-wcstate { display: inline-flex; align-items: center; gap: 5px; font-size: 0.72em; font-weight: 600; color: var(--hf-muted); }
  .hf-weekchip .hf-wcdot { width: 7px; height: 7px; border-radius: 50%; background: var(--hf-muted); }
  .hf-weekchip .hf-wcdot.ready, .hf-weekchip .hf-wcdot.delivered { background: var(--hf-lime); }
  .hf-weekchip .hf-wcdot.needs { background: #f59e0b; }
  .hf-weekchip .hf-wcdot.shipping { background: #3b82f6; }
  .hf-weekchip.past { opacity: 0.72; }
  .hf-weekchip.skipped .hf-wcdate { text-decoration: line-through; color: var(--hf-muted); }
  .hf-weekchip[aria-pressed="true"] {
    opacity: 1; border-color: var(--hf-ring); box-shadow: inset 0 0 0 1px var(--hf-ring);
    background: color-mix(in srgb, var(--hf-lime) 12%, var(--hf-surface));
  }
  .hf-weekchip .hf-wcnow {
    position: absolute; top: -7px; right: 8px; height: 16px; padding: 0 6px; border-radius: 8px;
    font-size: 0.62em; font-weight: 800; letter-spacing: 0.04em; text-transform: uppercase;
    display: inline-flex; align-items: center; background: var(--hf-accent); color: var(--hf-accent-ink);
  }
  .hf-weekchip .hf-wcdirty {
    position: absolute; top: 8px; right: 8px; width: 8px; height: 8px; border-radius: 50%;
    background: #f59e0b; box-shadow: 0 0 0 2px var(--hf-surface);
  }

  /* ---- week header ----------------------------------------------------------------------- */
  .hf-weekhead {
    display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px;
    margin-bottom: 14px;
  }
  .hf-weekhead .hf-weektitle { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
  .hf-weekhead .hf-weekdate { font-size: 1.3em; font-weight: 800; letter-spacing: -0.015em; }
  .hf-weekhead .hf-weekpills { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .hf-weekhead .hf-weeksub { font-size: 0.86em; color: var(--hf-muted); }
  .hf-weekdeadline { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 6px; font-size: 0.86em; color: var(--hf-muted); }
  .hf-weekdeadline ha-icon { --mdc-icon-size: 16px; }
  .hf-weekdeadline strong { color: var(--hf-text); font-weight: 600; }
  .hf-weekdeadline.urgent ha-icon { color: var(--hf-danger-fg); }
  .hf-stack { display: flex; flex-direction: column; gap: 10px; margin-bottom: 14px; }
  .hf-stack:empty { display: none; }
  .hf-orderstrip {
    display: flex; flex-wrap: wrap; gap: 10px 26px; padding: 12px 14px; border-radius: var(--hf-radius);
    background: var(--hf-surface-2);
  }
  .hf-orderstrip .hf-k { font-size: 0.7em; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; color: var(--hf-muted); }
  .hf-orderstrip .hf-v { font-size: 0.92em; font-weight: 700; margin-top: 2px; }

  /* ---- toolbar & filters ----------------------------------------------------------------- */
  .hf-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 12px; }
  .hf-search {
    position: relative; flex: 1 1 240px; min-width: 180px; max-width: 460px;
  }
  .hf-search ha-icon { position: absolute; left: 11px; top: 50%; transform: translateY(-50%); --mdc-icon-size: 18px; color: var(--hf-muted); pointer-events: none; }
  .hf-search input {
    width: 100%; height: 38px; padding: 0 34px 0 36px; border-radius: 999px;
    border: 1px solid var(--hf-border); background: var(--hf-surface-2); color: var(--hf-text);
    font: inherit; font-size: 0.9em; outline: none;
  }
  .hf-search input:focus { border-color: var(--hf-accent); background: var(--hf-surface); }
  .hf-search input::-webkit-search-cancel-button { display: none; }
  .hf-search .hf-clear {
    position: absolute; right: 4px; top: 50%; transform: translateY(-50%); width: 30px; height: 30px;
  }
  .hf-search .hf-clear ha-icon { position: static; transform: none; --mdc-icon-size: 16px; pointer-events: none; }
  .hf-toolbar .hf-spacer { flex: 1; }
  .hf-filterpanel {
    border: 1px solid var(--hf-border); border-radius: var(--hf-radius); padding: 6px 14px 10px;
    margin-bottom: 12px; background: var(--hf-surface);
  }
  .hf-frow { display: grid; grid-template-columns: 150px minmax(0, 1fr); gap: 6px 12px; padding: 8px 0; border-bottom: 1px solid var(--hf-border); }
  .hf-frow:last-child { border-bottom: none; }
  .hf-frow .hf-flabel { padding-top: 7px; font-size: 0.72em; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; color: var(--hf-muted); }
  @container hfapp (max-width: 620px) {
    .hf-frow { grid-template-columns: 1fr; }
    .hf-frow .hf-flabel { padding-top: 0; }
  }
  .hf-activefilters { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: -4px 0 12px; }
  .hf-activefilters .hf-chip { height: 28px; font-size: 0.8em; }
  .hf-busynote { font-size: 0.8em; font-style: italic; color: var(--hf-muted); }
  .hf-resultnote { font-size: 0.82em; color: var(--hf-muted); margin: 0 0 10px; }

  /* ---- tile grids ------------------------------------------------------------------------- */
  .hf-grid { display: grid; gap: 14px; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); }
  .hf-grid.market { grid-template-columns: repeat(auto-fill, minmax(175px, 1fr)); }
  @container hfapp (max-width: 480px) {
    .hf-grid, .hf-grid.market { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
  }
  .hf-grid.busy { opacity: 0.6; pointer-events: none; }
  .hf-group { margin-top: 22px; }
  .hf-group:first-child { margin-top: 0; }
  .hf-grouphead { display: flex; align-items: baseline; gap: 8px; margin-bottom: 10px; }
  .hf-grouphead .hf-groupcount { font-size: 0.8em; color: var(--hf-muted); }
  .hf-more-row { display: flex; justify-content: center; margin-top: 16px; }

  .hf-tile {
    position: relative; display: flex; flex-direction: column; min-width: 0; cursor: pointer;
    border-radius: var(--hf-radius); overflow: hidden; background: var(--hf-surface);
    border: 1px solid var(--hf-border);
    transition: box-shadow 0.15s, border-color 0.15s, transform 0.15s;
  }
  .hf-tile:hover { box-shadow: var(--hf-shadow); transform: translateY(-1px); }
  .hf-tile.selected { border-color: var(--hf-ring); box-shadow: 0 0 0 2px var(--hf-ring); }
  .hf-tile.soldout .hf-media img { filter: grayscale(1); opacity: 0.6; }
  .hf-media { position: relative; aspect-ratio: 4 / 3; background: var(--hf-surface-3); overflow: hidden; }
  .hf-media img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .hf-noimg { width: 100%; height: 100%; background: var(--hf-surface-3); }
  .hf-media .hf-tl, .hf-media .hf-tr, .hf-media .hf-bl, .hf-media .hf-br {
    position: absolute; display: flex; gap: 5px; align-items: center; max-width: calc(100% - 16px);
  }
  .hf-media .hf-tl { top: 8px; left: 8px; }
  .hf-media .hf-tr { top: 8px; right: 8px; }
  .hf-media .hf-bl { bottom: 8px; left: 8px; }
  .hf-media .hf-br { bottom: 8px; right: 8px; }
  .hf-badge {
    height: 22px; padding: 0 8px; border-radius: 6px; font-size: 0.66em; font-weight: 800;
    letter-spacing: 0.03em; text-transform: uppercase; display: inline-flex; align-items: center;
    background: #242424; color: #fff; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    max-width: 100%;
  }
  .hf-overlaypill {
    height: 24px; padding: 0 8px; border-radius: 999px; font-size: 0.74em; font-weight: 700;
    display: inline-flex; align-items: center; gap: 4px; background: rgba(15, 15, 15, 0.72); color: #fff;
    backdrop-filter: blur(4px);
  }
  .hf-overlaypill ha-icon { --mdc-icon-size: 14px; }
  .hf-favdot {
    width: 28px; height: 28px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center;
    background: rgba(15, 15, 15, 0.55); color: #ff6b8a; backdrop-filter: blur(4px);
  }
  .hf-favdot ha-icon { --mdc-icon-size: 16px; }
  .hf-checkmark {
    width: 28px; height: 28px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center;
    background: var(--hf-lime); color: var(--hf-lime-ink); box-shadow: 0 2px 6px rgba(0, 0, 0, 0.25);
  }
  .hf-checkmark ha-icon { --mdc-icon-size: 18px; }
  .hf-play {
    width: 34px; height: 34px; border-radius: 50%; border: none; cursor: pointer;
    display: inline-flex; align-items: center; justify-content: center;
    background: rgba(15, 15, 15, 0.62); color: #fff; backdrop-filter: blur(4px);
  }
  .hf-play:hover { background: rgba(15, 15, 15, 0.8); }
  .hf-play ha-icon { --mdc-icon-size: 20px; }
  .hf-soldout {
    position: absolute; left: 0; right: 0; top: 50%; transform: translateY(-50%);
    background: rgba(0, 0, 0, 0.62); color: #fff; text-align: center; padding: 5px 0;
    font-size: 0.74em; font-weight: 800; letter-spacing: 0.08em; text-transform: uppercase;
  }
  .hf-tbody { display: flex; flex-direction: column; gap: 6px; padding: 11px 12px 12px; flex: 1; min-width: 0; }
  .hf-tname {
    font-weight: 700; font-size: 0.93em; line-height: 1.28; display: flex; gap: 7px; align-items: baseline;
  }
  .hf-tname .hf-pdot { width: 8px; height: 8px; border-radius: 50%; flex: none; transform: translateY(-1px); }
  .hf-tname span:last-child {
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  }
  .hf-tvariant { font-size: 0.78em; font-weight: 700; color: var(--hf-warn-fg); }
  .hf-optselect {
    display: flex; align-items: center; gap: 6px; width: 100%; min-height: 32px; padding: 4px 8px 4px 10px;
    border-radius: var(--hf-radius-sm); border: 1px solid var(--hf-border); background: var(--hf-surface-2);
    color: var(--hf-text); cursor: pointer; font: inherit; font-size: 0.8em; text-align: left;
  }
  .hf-optselect:hover { border-color: color-mix(in srgb, var(--hf-accent) 55%, var(--hf-border)); }
  .hf-optselect.custom {
    background: color-mix(in srgb, var(--hf-accent) 12%, var(--hf-surface));
    border-color: color-mix(in srgb, var(--hf-accent) 45%, var(--hf-border));
  }
  .hf-optselect ha-icon { --mdc-icon-size: 16px; color: var(--hf-muted); flex: none; }
  .hf-optselect .hf-optlabel {
    flex: 1; min-width: 0; font-weight: 700; line-height: 1.25;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  }
  .hf-optselect .hf-optcount { color: var(--hf-muted); white-space: nowrap; }
  .hf-tdesc {
    font-size: 0.8em; color: var(--hf-muted); line-height: 1.35;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  }
  .hf-tmeta { display: flex; flex-wrap: wrap; gap: 3px 10px; font-size: 0.76em; color: var(--hf-muted); }
  .hf-tmeta span { display: inline-flex; align-items: center; gap: 3px; }
  .hf-tmeta ha-icon { --mdc-icon-size: 14px; }
  .hf-tchips { display: flex; flex-wrap: wrap; gap: 4px; }
  .hf-tchip {
    height: 20px; padding: 0 7px; border-radius: 999px; font-size: 0.68em; font-weight: 600;
    display: inline-flex; align-items: center; border: 1px solid var(--hf-border); color: var(--hf-muted);
  }
  .hf-tchip.veggie { background: #43a047; border-color: transparent; color: #fff; }
  .hf-thist { font-size: 0.74em; color: var(--hf-muted); }
  .hf-tfoot {
    margin-top: auto; padding-top: 6px; display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between;
    gap: 6px 8px; min-height: 34px;
  }
  .hf-tprice { font-size: 0.82em; font-weight: 700; }
  .hf-tprice span { font-weight: 500; color: var(--hf-muted); font-size: 0.92em; }
  .hf-add {
    height: 32px; padding: 0 14px; border-radius: 999px; border: none; cursor: pointer;
    background: var(--hf-accent); color: var(--hf-accent-ink); font-weight: 700; font-size: 0.85em;
    display: inline-flex; align-items: center; gap: 4px; margin-left: auto;
  }
  .hf-add ha-icon { --mdc-icon-size: 17px; }
  .hf-add:hover { filter: brightness(1.08); }
  .hf-add:disabled { opacity: 0.45; cursor: default; filter: none; }
  .hf-stepper {
    display: inline-flex; align-items: center; gap: 2px; margin-left: auto; height: 32px;
    border-radius: 999px; background: color-mix(in srgb, var(--hf-accent) 12%, var(--hf-surface));
    border: 1px solid color-mix(in srgb, var(--hf-accent) 40%, var(--hf-border));
  }
  .hf-stepper button {
    width: 30px; height: 30px; border-radius: 50%; border: none; background: transparent; cursor: pointer;
    display: inline-flex; align-items: center; justify-content: center; color: var(--hf-text);
  }
  .hf-stepper button:hover:not(:disabled) { background: color-mix(in srgb, var(--hf-accent) 18%, transparent); }
  .hf-stepper button:disabled { opacity: 0.35; cursor: default; }
  .hf-stepper button ha-icon { --mdc-icon-size: 17px; }
  .hf-stepper .hf-qty { min-width: 18px; text-align: center; font-weight: 800; font-size: 0.9em; }
  .hf-stepper .hf-unit { font-size: 0.86em; font-weight: 600; color: var(--hf-muted); }
  .hf-stepper.wide { width: 100%; margin-left: 0; justify-content: space-between; }
  .hf-stepper.wide .hf-qty { flex: 1; }
  .hf-ordered { margin-left: auto; font-size: 0.8em; font-weight: 700; color: var(--hf-ok-fg); }

  /* catalog (recipes) tiles */
  .hf-favbtn {
    width: 32px; height: 32px; border-radius: 50%; border: none; cursor: pointer;
    display: inline-flex; align-items: center; justify-content: center;
    background: rgba(15, 15, 15, 0.5); color: #fff; backdrop-filter: blur(4px);
  }
  .hf-favbtn.on { color: #ff6b8a; }
  .hf-favbtn:hover:not(:disabled) { background: rgba(15, 15, 15, 0.72); }
  .hf-favbtn:disabled { opacity: 0.6; cursor: default; }
  .hf-favbtn ha-icon { --mdc-icon-size: 18px; }
  .hf-rating { display: inline-flex; align-items: center; gap: 3px; }
  .hf-rating ha-icon { --mdc-icon-size: 14px; color: #f5a524; }
  .hf-tname a { color: inherit; text-decoration: none; }
  .hf-tname a:hover { text-decoration: underline; }

  /* ---- box bar (sticky) ------------------------------------------------------------------- */
  .hf-boxbar {
    position: sticky; bottom: 0; z-index: 3;
    border-top: 1px solid var(--hf-border);
    border-radius: 0 0 var(--ha-card-border-radius, 12px) var(--ha-card-border-radius, 12px);
    background: color-mix(in srgb, var(--hf-surface) 88%, transparent);
    backdrop-filter: saturate(1.4) blur(10px); -webkit-backdrop-filter: saturate(1.4) blur(10px);
    box-shadow: 0 -6px 24px rgba(0, 0, 0, 0.08);
  }
  .hf-boxbar:empty { display: none; }
  .hf-boxbarinner {
    display: flex; align-items: center; gap: 14px; padding: 12px 18px; flex-wrap: wrap;
  }
  .hf-boxslots { display: flex; gap: 4px; align-items: center; }
  .hf-slot { width: 22px; height: 8px; border-radius: 4px; background: var(--hf-surface-3); }
  .hf-slot.filled { background: var(--hf-lime); }
  .hf-slot.extra { background: #f59e0b; }
  .hf-boxsummary { flex: 1; min-width: 180px; display: flex; flex-direction: column; gap: 2px; }
  .hf-boxsummary .hf-boxline { font-weight: 700; font-size: 0.95em; display: flex; flex-wrap: wrap; align-items: center; gap: 4px 10px; }
  .hf-boxsummary .hf-boxnote { font-size: 0.8em; color: var(--hf-muted); }
  .hf-boxsummary .hf-boxnote.warn { color: var(--hf-warn-fg); font-weight: 600; }
  .hf-boxtotal { text-align: right; display: flex; flex-direction: column; gap: 1px; }
  .hf-boxtotal .hf-boxamount { font-size: 1.08em; font-weight: 800; }
  .hf-boxtotal .hf-boxamountnote { font-size: 0.72em; color: var(--hf-muted); }
  .hf-boxbar .hf-actions { flex-wrap: nowrap; }
  @container hfapp (max-width: 560px) {
    .hf-boxbarinner { padding: 10px 12px; gap: 10px; }
    .hf-boxslots, .hf-boxtotal { display: none; }
    .hf-boxbar .hf-actions .hf-btn.ghost { display: none; }
  }

  /* ---- sheets, dialogs, toast (outside ha-card) ------------------------------------------ */
  .hf-sheetwrap {
    position: fixed; inset: 0; z-index: 20; display: flex; align-items: center; justify-content: center;
    padding: 16px; background: rgba(0, 0, 0, 0.5); isolation: isolate; animation: hf-fade 0.15s ease-out;
    color: var(--hf-text);
  }
  .hf-sheet {
    width: min(620px, 100%); max-height: min(88vh, 900px); display: flex; flex-direction: column;
    background: var(--hf-surface); border-radius: var(--hf-radius-lg); box-shadow: var(--hf-shadow-lg);
    overflow: hidden; animation: hf-rise 0.18s ease-out;
  }
  /* The dialog takes focus so Tab starts inside it; its controls show their own focus rings. */
  .hf-sheet:focus { outline: none; }
  .hf-sheet.narrow { width: min(440px, 100%); }
  .hf-sheethead {
    display: flex; align-items: center; gap: 12px; padding: 16px 16px 14px 20px;
    border-bottom: 1px solid var(--hf-border);
  }
  .hf-sheethead .hf-sheettitle { flex: 1; min-width: 0; }
  .hf-sheethead h2 { margin: 0; font-size: 1.1em; font-weight: 800; }
  .hf-sheethead .hf-sheetsub { font-size: 0.82em; color: var(--hf-muted); margin-top: 2px; }
  .hf-sheetbody { overflow-y: auto; padding: 16px 20px 20px; display: flex; flex-direction: column; gap: 16px; }
  .hf-sheetfoot {
    display: flex; align-items: center; justify-content: flex-end; gap: 8px; padding: 12px 16px;
    border-top: 1px solid var(--hf-border); flex-wrap: wrap;
  }
  .hf-sheetfoot .hf-spacer { flex: 1; }
  .hf-optlist { gap: 6px; }
  .hf-optrow {
    display: flex; align-items: center; gap: 12px; width: 100%; padding: 8px 12px 8px 10px; text-align: left;
    border-radius: var(--hf-radius-sm); border: 1px solid var(--hf-border); background: var(--hf-surface);
    color: var(--hf-text); cursor: pointer; font: inherit;
  }
  .hf-optrow:hover:not(:disabled) { border-color: color-mix(in srgb, var(--hf-accent) 55%, var(--hf-border)); }
  .hf-optrow.on { border-color: var(--hf-accent); background: color-mix(in srgb, var(--hf-accent) 10%, var(--hf-surface)); }
  .hf-optrow:disabled { cursor: default; opacity: 0.55; }
  .hf-optrow .hf-radio {
    width: 18px; height: 18px; border-radius: 50%; border: 2px solid var(--hf-border); flex: none;
    display: flex; align-items: center; justify-content: center;
  }
  .hf-optrow.on .hf-radio { border-color: var(--hf-accent); }
  .hf-optrow.on .hf-radio::after { content: ""; width: 8px; height: 8px; border-radius: 50%; background: var(--hf-accent); }
  .hf-optimg { width: 44px; height: 44px; border-radius: 10px; object-fit: contain; flex: none; background: var(--hf-surface-2); }
  .hf-optimg.dish { object-fit: cover; }
  .hf-opttext { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 1px; }
  .hf-opttext .hf-optname { font-weight: 700; font-size: 0.9em; }
  .hf-opttext .hf-optsub { font-size: 0.76em; color: var(--hf-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .hf-optprice { font-size: 0.82em; font-weight: 700; white-space: nowrap; }
  .hf-optprice span { font-weight: 500; color: var(--hf-muted); }
  .hf-optprice.included { font-weight: 600; color: var(--hf-muted); }
  .hf-lineitems { display: flex; flex-direction: column; }
  .hf-lineitem { display: flex; align-items: center; gap: 12px; padding: 8px 0; border-bottom: 1px solid var(--hf-border); }
  .hf-lineitem:last-child { border-bottom: none; }
  .hf-lineitem img, .hf-lineitem .hf-noimg { width: 52px; height: 52px; border-radius: 10px; object-fit: cover; flex: none; }
  .hf-lineitem .hf-lineitemtext { flex: 1; min-width: 0; }
  .hf-lineitem .hf-lineitemname { font-weight: 700; font-size: 0.9em; }
  .hf-lineitem .hf-lineitemsub { font-size: 0.78em; color: var(--hf-muted); }
  .hf-lineitem.removed { opacity: 0.55; }
  .hf-lineitem.removed .hf-lineitemname { text-decoration: line-through; }
  .hf-lineitem .hf-changetag { font-size: 0.7em; font-weight: 800; text-transform: uppercase; letter-spacing: 0.04em; }
  .hf-pricetable { display: flex; flex-direction: column; gap: 6px; font-size: 0.9em; }
  .hf-pricetable .hf-prow { display: flex; justify-content: space-between; gap: 12px; }
  .hf-pricetable .hf-prow.total { font-weight: 800; font-size: 1.05em; padding-top: 8px; border-top: 1px solid var(--hf-border); }
  .hf-pricetable .hf-prow .hf-muted { font-weight: 400; }
  .hf-confirmtext { font-size: 0.93em; line-height: 1.5; }
  .hf-select {
    width: 100%; height: 40px; border-radius: 10px; border: 1px solid var(--hf-border);
    background: var(--hf-surface); color: var(--hf-text); padding: 0 10px; font: inherit;
  }

  .hf-toast {
    position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%); z-index: 30;
    display: flex; align-items: center; gap: 10px; max-width: calc(100vw - 32px);
    padding: 11px 16px; border-radius: 12px; font-size: 0.9em; font-weight: 600;
    background: #1f2328; color: #fff; box-shadow: var(--hf-shadow-lg); animation: hf-rise 0.18s ease-out;
  }
  .hf-toast.error { background: #b91c1c; }
  .hf-toast.saving { top: 16px; bottom: auto; background: var(--hf-accent); color: var(--hf-accent-ink); }
  .hf-toast ha-icon { --mdc-icon-size: 18px; }
  .hf-spinner {
    width: 16px; height: 16px; border-radius: 50%; border: 2px solid currentColor;
    border-right-color: transparent; animation: hf-spin 0.7s linear infinite; flex: none;
  }
  .hf-skeleton {
    border-radius: var(--hf-radius); min-height: 120px;
    background: linear-gradient(90deg, var(--hf-surface-2) 25%, var(--hf-surface-3) 50%, var(--hf-surface-2) 75%);
    background-size: 200% 100%; animation: hf-shimmer 1.4s ease-in-out infinite;
  }
  .hf-skeletons { display: grid; gap: 14px; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); }
  .hf-skeletons .hf-skeleton { min-height: 240px; }

  .hf-videobox { width: min(640px, 100%); }
  .hf-videobox video { display: block; width: 100%; max-height: 70vh; background: #000; }
  .hf-videobox .hf-videoerr { padding: 8px 16px 0; font-size: 0.85em; color: var(--hf-danger-fg); }
  .hf-videobox .hf-videofallback { display: block; padding: 10px 16px 14px; font-size: 0.84em; color: var(--hf-muted); }

  @keyframes hf-spin { to { transform: rotate(360deg); } }
  @keyframes hf-fade { from { opacity: 0; } }
  @keyframes hf-rise { from { opacity: 0; transform: translateY(8px); } }
  @keyframes hf-shimmer { to { background-position: -200% 0; } }
  .hf-toast.saving { animation: none; }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation: none !important; transition: none !important; scroll-behavior: auto !important; }
  }

  /* ---- account & spending ---------------------------------------------------------------- */
  .hf-subtabs { margin-bottom: 18px; }
  .hf-barchart { display: flex; flex-direction: column; gap: 6px; }
  .hf-bc-plot { position: relative; height: 220px; border-bottom: 1px solid var(--hf-border); }
  .hf-bc-cols { position: absolute; inset: 0; display: grid; gap: 0; }
  .hf-bc-col { position: relative; height: 100%; }
  .hf-bc-bar {
    position: absolute; bottom: 0; left: 50%; transform: translateX(-50%); width: min(62%, 44px);
    border-radius: 6px 6px 2px 2px; background: color-mix(in srgb, var(--hf-accent) 80%, var(--hf-surface));
  }
  :host([dark]) .hf-bc-bar { background: color-mix(in srgb, var(--hf-lime) 78%, var(--hf-surface)); }
  .hf-bc-col.upcoming .hf-bc-bar { opacity: 0.45; }
  .hf-bc-col.empty .hf-bc-bar { background: var(--hf-border); }
  .hf-bc-val {
    position: absolute; left: 50%; transform: translateX(-50%); white-space: nowrap;
    font-size: 0.74em; font-weight: 700; color: var(--hf-text);
  }
  .hf-bc-trend { position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible; pointer-events: none; }
  .hf-bc-trend polyline { fill: none; stroke: #f59e0b; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
  .hf-bc-dot {
    position: absolute; width: 7px; height: 7px; border-radius: 50%; background: #f59e0b;
    transform: translate(-50%, 50%); pointer-events: none;
  }
  .hf-bc-axis { display: grid; text-align: center; font-size: 0.74em; color: var(--hf-muted); }
  .hf-bc-axis small { display: block; font-size: 0.9em; opacity: 0.8; }
  @container hfapp (max-width: 560px) {
    .hf-bc-val { display: none; }
    .hf-bc-plot { height: 160px; }
  }
  .hf-bars { display: flex; flex-direction: column; gap: 8px; }
  .hf-barrow { display: grid; grid-template-columns: 5.5em minmax(0, 1fr) auto; align-items: center; gap: 10px; font-size: 0.88em; }
  .hf-barrow .hf-bar { height: 8px; border-radius: 4px; background: var(--hf-surface-3); overflow: hidden; }
  .hf-barrow .hf-bar span { display: block; height: 100%; border-radius: 4px; background: var(--hf-accent); }
  :host([dark]) .hf-barrow .hf-bar span { background: var(--hf-lime); }
  .hf-barrow.upcoming { opacity: 0.6; }
  .hf-barrow .hf-barval { text-align: right; font-weight: 700; white-space: nowrap; }
  .hf-barrow .hf-barval small { display: block; font-weight: 500; color: var(--hf-muted); font-size: 0.8em; }
  .hf-ledger { display: flex; flex-direction: column; }
  .hf-ledgerrow { display: flex; justify-content: space-between; align-items: center; gap: 10px; padding: 9px 0; border-bottom: 1px solid var(--hf-border); font-size: 0.9em; }
  .hf-ledgerrow:last-child { border-bottom: none; }
  .hf-ledgerrow.upcoming { opacity: 0.7; }
  .hf-ledgerrow .hf-saved { color: var(--hf-ok-fg); font-weight: 700; font-size: 0.85em; }
  .hf-presets { display: flex; flex-direction: column; gap: 8px; }
  .hf-preset { padding: 10px 12px; border-radius: 10px; background: var(--hf-surface-2); }
  .hf-preset.active { box-shadow: inset 0 0 0 2px var(--hf-ring); }
  .hf-preset .hf-presetname { font-weight: 700; font-size: 0.9em; display: flex; gap: 8px; align-items: center; }
  .hf-preset .hf-presetdesc { font-size: 0.82em; color: var(--hf-muted); margin-top: 2px; }
  .hf-controlrow { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--hf-border); }
  .hf-controlrow:last-child { border-bottom: none; }
  .hf-controlrow .hf-controllabel { font-weight: 700; font-size: 0.92em; }
  .hf-controlrow .hf-controlhint { font-size: 0.8em; color: var(--hf-muted); margin-top: 2px; }
  .hf-disclosure { border: none; background: none; cursor: pointer; padding: 4px 0; display: inline-flex; align-items: center; gap: 6px; color: var(--hf-muted); font-weight: 700; font-size: 0.8em; letter-spacing: 0.05em; text-transform: uppercase; }
  .hf-disclosure:hover { color: var(--hf-text); }
  .hf-disclosure ha-icon { --mdc-icon-size: 18px; }
  .hf-health { display: grid; gap: 8px; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); }

  /* ---- food profile ---------------------------------------------------------------------- */
  .fp-hero { margin-bottom: 16px; }
  .fp-hero p { margin: 6px 0 0; color: var(--hf-muted); font-size: 0.9em; max-width: 64ch; line-height: 1.45; }
  .fp-completion { display: flex; align-items: center; gap: 10px; margin-top: 12px; max-width: 64ch; }
  .fp-bar { flex: 1; height: 8px; border-radius: 4px; background: var(--hf-surface-3); overflow: hidden; }
  .fp-bar span { display: block; height: 100%; background: var(--hf-lime); transition: width 0.3s; }
  .fp-bartext { font-size: 0.8em; color: var(--hf-muted); white-space: nowrap; }
  .fp-panels { display: flex; flex-direction: column; gap: 14px; }
  .fp-toppanels { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }
  .fp-panel { border: 1px solid var(--hf-border); border-radius: var(--hf-radius); padding: 16px; background: var(--hf-surface); }
  .fp-paneltitle { display: flex; align-items: center; gap: 10px; margin: 0 0 12px; font-size: 1em; font-weight: 800; }
  .fp-paneltitle ha-icon { color: var(--hf-ok-fg); }
  .fp-subcards { display: grid; gap: 10px; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); }
  .fp-subcard { border: 1px solid var(--hf-border); border-radius: 12px; padding: 12px 14px; background: var(--hf-surface-2); }
  .fp-subcard.open { background: var(--hf-surface); grid-column: 1 / -1; }
  .fp-subhead { display: flex; align-items: center; justify-content: space-between; width: 100%; border: none; background: none; cursor: pointer; padding: 0; text-align: left; }
  .fp-subtitle { font-weight: 700; font-size: 0.93em; }
  .fp-preview { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
  .fp-pill { height: 24px; padding: 0 9px; border-radius: 7px; font-size: 0.76em; font-weight: 700; display: inline-flex; align-items: center; background: var(--hf-ok-bg); color: var(--hf-ok-fg); }
  .fp-pill.muted { background: var(--hf-muted-bg); color: var(--hf-muted); }
  .fp-tell { margin-left: 8px; height: 20px; padding: 0 8px; border-radius: 999px; font-size: 0.68em; font-weight: 800; display: inline-flex; align-items: center; background: var(--hf-warn-bg); color: var(--hf-warn-fg); vertical-align: middle; }
  .fp-editor { margin-top: 12px; }
  .fp-question { margin: 0 0 10px; font-size: 0.85em; color: var(--hf-muted); }
  .fp-note { margin: 10px 0 0; font-size: 0.8em; color: var(--hf-muted); }
  .fp-note.error { color: var(--hf-danger-fg); font-weight: 600; }
  .fp-chip.blocked { opacity: 0.45; cursor: not-allowed; }
  .fp-wgrid { display: grid; gap: 8px; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); }
  .fp-wtile { display: flex; flex-direction: column; gap: 8px; padding: 10px 12px; border-radius: 10px; border: 1px solid var(--hf-border); background: var(--hf-surface); }
  .fp-wtile.liked { border-color: var(--hf-lime); }
  .fp-wtile.disliked { border-color: color-mix(in srgb, #ef4444 60%, var(--hf-border)); }
  .fp-wname { font-weight: 600; font-size: 0.88em; }
  .fp-seg { display: grid; grid-template-columns: 1fr 1fr; border: 1px solid var(--hf-border); border-radius: 8px; overflow: hidden; }
  .fp-segbtn { display: flex; align-items: center; justify-content: center; gap: 4px; height: 30px; border: none; cursor: pointer; background: var(--hf-surface-2); color: var(--hf-muted); font-size: 0.78em; font-weight: 700; }
  .fp-segbtn + .fp-segbtn { border-left: 1px solid var(--hf-border); }
  .fp-segbtn ha-icon { --mdc-icon-size: 15px; }
  .fp-segbtn.like.on { background: var(--hf-lime); color: var(--hf-lime-ink); }
  .fp-segbtn.dislike.on { background: #dc2626; color: #fff; }
  .fp-hhrow { display: flex; align-items: center; justify-content: space-between; padding: 8px 0; }
  .fp-hhlabel { font-weight: 600; font-size: 0.93em; }
  .fp-dietdesc { margin: 10px 2px 0; font-size: 0.85em; color: var(--hf-muted); }
  .fp-footer { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 16px; padding: 12px 0; border-top: 1px solid var(--hf-border); }
  .fp-footer.sticky { position: sticky; bottom: 0; z-index: 2; background: color-mix(in srgb, var(--hf-surface) 90%, transparent); backdrop-filter: blur(8px); }
  .fp-footer .fp-footnote { flex: 1; min-width: 200px; font-size: 0.8em; color: var(--hf-muted); }
  .fp-footer .fp-footnote.error { color: var(--hf-danger-fg); font-weight: 600; }

  /* ---- shared recipe sheet (hellofresh-recipe-detail.js), restyled to match ---------------- */
  .detailwrap { color: var(--hf-text); }
  .detailbox { border-radius: var(--hf-radius-lg) !important; max-width: min(720px, 94vw) !important; }
  .detailhead { padding: 14px 12px 14px 20px !important; }
  .detailtitle { font-size: 1.08em; font-weight: 800 !important; }
  .detailscroll { padding: 16px 20px 20px !important; }
  .detailimg { border-radius: 14px !important; aspect-ratio: 16 / 9; object-fit: cover; }
  .detaillabel { background: var(--hf-ok-bg) !important; color: var(--hf-ok-fg) !important; }
  .sbtn.active { background: var(--hf-accent) !important; border-color: var(--hf-accent) !important; color: var(--hf-accent-ink) !important; }
  .sadd { background: var(--hf-accent) !important; color: var(--hf-accent-ink) !important; }
  .detaillinks a { color: var(--hf-accent) !important; }
  :host([dark]) .detaillinks a { color: var(--hf-ok-fg) !important; }
`;
