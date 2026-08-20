# (Unofficial) HelloFresh Integration for Home Assistant

A custom Home Assistant integration that reads your HelloFresh account and menu data so you can track upcoming deliveries, shipment status, recipe selection deadlines, and week-by-week meal planning — and browse and edit your meals and HelloFresh Market add-ons — directly from Home Assistant.

It also exposes delivery-history summaries, shipment tracking metadata, billing/payment dates, and authenticated menu and profile details when those endpoints are available for your region and account.

> ⚠️ This is an **unofficial** integration, reverse-engineered from the HelloFresh website. It is not affiliated with or endorsed by HelloFresh, and the underlying API may change at any time.

[![CI](https://github.com/kedube/ha-hellofresh/actions/workflows/ci.yml/badge.svg)](https://github.com/kedube/ha-hellofresh/actions/workflows/ci.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)

## Contents

- [Installation](#installation)
- [Configuration](#configuration)
  - [Options](#options)
  - [Supported regions](#supported-regions)
- [What It Provides](#what-it-provides)
  - [Entities](#entities)
  - [Services](#services)
  - [Automation ideas](#automation-ideas)
- [HelloFresh Dashboard](#hellofresh-dashboard)
- [Current Scope](#current-scope)
- [Troubleshooting](#troubleshooting)
- [Diagnostics](#diagnostics)
- [Development](#development)
- [References](#references)

**Reference docs** (split out of this README to keep it browsable):

| Document | Contents |
|---|---|
| [docs/entities.md](docs/entities.md) | Every sensor, binary sensor, switch, and button |
| [docs/cards.md](docs/cards.md) | All seven Lovelace cards: options, features, screenshots |
| [docs/services.md](docs/services.md) | All 24 services: parameters and responses |


## Installation

> Home Assistant requirement:
>
> - Home Assistant Core with support for config flows, diagnostics, and custom integrations

### Method 1: HACS custom repository

1. In Home Assistant, open **HACS**.
2. Open the menu in the top-right corner (**⋮**) and select **Custom repositories**.
3. Paste this repository's URL: `https://github.com/kedube/ha-hellofresh`
4. Set the category to **Integration** and click **Add**.
5. Search for **HelloFresh** in HACS, open it, and click **Download**.
6. Restart Home Assistant.

After restart, add the integration from Home Assistant:

[![Open your Home Assistant instance and start setting up a new HelloFresh integration instance.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=hellofresh)

### Method 2: Manual installation

1. Copy `custom_components/hellofresh` into your Home Assistant `config/custom_components` directory.
2. Restart Home Assistant.
3. Add the integration from **Settings > Devices & services > Add integration**.

[![Open your Home Assistant instance and start setting up a new HelloFresh integration instance.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=hellofresh)

## Configuration

HelloFresh has no official API or OAuth app. Setup offers **two ways** to connect, chosen from a menu when you add the integration:

- **Email and password (recommended)** — the integration logs in the same way the website does, obtaining a short-lived access token plus a long-lived refresh token, then keeps the connection refreshed automatically. No developer tools or token copying required.
- **Access token (advanced backup)** — paste your HelloFresh `apiV2Auth` token from a logged-in browser session. Use this only if email/password sign-in is blocked for you (see [Troubleshooting](#troubleshooting)). It works until the token expires (~60 days), then prompts you for a fresh one.

### Setting up (email and password — recommended)

1. Add the integration (see [Installation](#installation)).
2. Choose **Email and password (recommended)**.
3. Choose your **Country** (see [Supported regions](#supported-regions)).
4. Enter the **email** and **password** you use to sign in to HelloFresh.
5. Submit. Home Assistant signs in, validates the account, and stores the resulting tokens so it can refresh access on its own.

> 🔒 **About your credentials.** Your email and password are stored in the Home Assistant config entry and used only to log in to HelloFresh's own login endpoint and to re-authenticate when the refresh token eventually expires. They are redacted from diagnostics exports. As with any third-party integration, the security of your credentials depends on the security of your Home Assistant installation.

### Setting up (access token — advanced backup)

Use this only when email/password sign-in is blocked. The setup dialog includes step-by-step directions; in short:

1. Add the integration and choose **Access token (advanced)**, then your **Country**.
2. In a desktop browser, sign in to your regional HelloFresh website so you reach your account page.
3. Open developer tools (right-click → **Inspect**, or **F12**), open the cookies view (**Chrome/Edge:** Application → Cookies; **Firefox:** Storage → Cookies), select your HelloFresh site, and copy the **Value** of the **`apiV2Auth`** cookie.
4. Paste it into the token field and submit. A full `apiV2Auth` value includes the refresh token for the longest-lasting connection; a bare access token also works but is shorter-lived.

> ⚠️ A token-only entry **cannot self-heal**: when the refresh token expires (~60 days) or HelloFresh rotates it, there are no stored credentials to log back in, so Home Assistant raises a reauthentication prompt asking for a new token. Prefer email/password whenever it works.

### Why bot protection can matter

HelloFresh fronts its sites with Cloudflare. To pass that layer the integration presents as a real **Google Chrome on Windows 11** browser, including a genuine Chrome TLS/HTTP-2 fingerprint via the bundled `curl_cffi` dependency (installed automatically). Most regions accept this; a region with stricter bot-management rules may still block automated sign-in, which is what the access-token backup path is for.

### Reauthentication

The integration renews the short-lived access token automatically using the long-lived refresh token. For **email/password** entries it falls back to a full login with your stored credentials when the refresh token is rejected or expired; if that login fails (for example you changed your password), Home Assistant prompts you to re-enter your email and password. For **token-only** entries there are no stored credentials, so reauthentication instead asks you to paste a fresh `apiV2Auth` token.

### Options

These settings are adjusted *after* setup, in the integration's **Configure** dialog — separate from the initial connect flow. To open it:

1. Go to **Settings → Devices & Services**.
2. On the **Integrations** tab, find the **HelloFresh** card (or click the badge below to jump straight there).
3. Click **Configure** on the HelloFresh entry. (If you have multiple HelloFresh accounts, each entry has its own **Configure** with independent options.)
4. Adjust the fields described below and click **Submit**.

[![Open your Home Assistant instance and show the HelloFresh integration.](https://my.home-assistant.io/badges/integration.svg)](https://my.home-assistant.io/redirect/integration/?domain=hellofresh)

> 💡 **Configure vs. Add.** Use **Configure** (the button on an *existing* entry) to change these options. The **Add integration** flow is only for connecting a new account, and changes here take effect without re-entering your credentials.

The available options are:

- **Refresh interval (minutes)** — how often account data is polled. Default is **180**; allowed range is **5–1440**. (This is the data-refresh cadence; the bearer token is refreshed on its own faster-running schedule regardless of this value.)
- **Use public menu fallback** — when authenticated menu data is unavailable, scrape the public regional menu page so recipe data still appears.
- **Past delivery history (weeks)** — how many weeks of past deliveries to fetch and make browsable in the cards. Default is **26** (about 6 months); allowed range is **1–104**. Lower it to reduce how much data is pulled each refresh if you don't need a long history; raise it to browse further back (use **~56** for a full year, so the box from ~12 months ago is included). Changing it reloads the integration.
- **Show favorite hearts** — show a ♥ on meals bookmarked in your cookbook. Default **on**; costs one small extra request per refresh. Turning it off only removes the hearts — the favorite services and the [Recipes card](docs/cards.md#recipes-card) keep working.
- **Full menu history (weeks)** — how long a delivered week keeps its full browsable menu (with your meals highlighted) before collapsing to delivered-meals-only. Default **2**, range **0–3** (**0** disables it). HelloFresh stops publishing menus for older weeks, so weeks beyond that fall back automatically regardless. Changing it reloads the integration.

### Supported regions

Choose the matching country during setup. All 16 markets HelloFresh currently operates in are supported. Prices are reported in each region's local currency, taken from your account data where HelloFresh provides it and falling back to the currency listed below.

**Status** reflects real-world testing: ✅ = verified end to end (including write actions); *Untested* = the region is wired up and should work for reading, but no one has confirmed it — reports welcome.

| Region | Code | Website | Currency | Status |
| --- | --- | --- | --- | --- |
| United States | `us` | https://www.hellofresh.com | USD | ✅ Verified |
| United Kingdom | `uk` | https://www.hellofresh.co.uk | GBP | ✅ Verified |
| Canada | `ca` | https://www.hellofresh.ca | CAD | Untested |
| Australia | `au` | https://www.hellofresh.com.au | AUD | Untested |
| New Zealand | `nz` | https://www.hellofresh.co.nz | NZD | Untested |
| Germany | `de` | https://www.hellofresh.de | EUR | ✅ Verified |
| Austria | `at` | https://www.hellofresh.at | EUR | Untested |
| Switzerland | `ch` | https://www.hellofresh.ch | CHF | Untested |
| Netherlands | `nl` | https://www.hellofresh.nl | EUR | ✅ Verified |
| Belgium | `be` | https://www.hellofresh.be | EUR | Untested |
| Luxembourg | `lu` | https://www.hellofresh.lu | EUR | Untested |
| France | `fr` | https://www.hellofresh.fr | EUR | Untested |
| Ireland | `ie` | https://www.hellofresh.ie | EUR | Untested |
| Denmark | `dk` | https://www.hellofresh.dk | DKK | ✅ Verified |
| Norway | `no` | https://www.hellofresh.no | NOK | Untested |
| Sweden | `se` | https://www.hellofresh.se | SEK | Untested |

**Interface language.** The integration ships translations for German, Dutch, French, Danish,
Norwegian (Bokmål) and Swedish alongside English; Home Assistant picks one from *your* profile
language, not from the Country you choose above. Any string not yet translated falls back to
English automatically. Product wording follows each regional HelloFresh site (Kochbox,
Maaltijdbox, Box Repas, måltidskasse, matkasse), so entity names should read the way your own
HelloFresh website does — corrections from native speakers are welcome.

**Not supported:** HelloFresh has exited Spain and Italy (both wound down in early 2026) and Japan (2022), so accounts in those markets can no longer be used.

## What It Provides

### Entities

![HelloFresh integration entities in Home Assistant](images/hellofresh_screenshot-1.png)

The integration creates **50+ entities** per HelloFresh account:

- **Sensors** covering deliveries & orders (dates, weeks, delivery window, holiday shifts), meal selection (deadlines, meal/market counts, preselection flags), billing & payments (box price, account credit, payment dates, coupons), account & subscription (plan, servings, status), shipment tracking (status, carrier, tracking link, carrier ETA, actual arrival time), and history & skipped weeks — plus diagnostic sensors for token expiry and the API base URL.
- **Binary sensors** for automations — most notably `binary_sensor.needs_meal_selection`, the primary signal for "review your meals before the cutoff" reminders — plus tracked-shipment availability and parse-health diagnostics.
- A **delivery calendar** (`calendar.delivery_schedule`), a **refresh button**, and a **skip next week switch**.

The full reference — every entity with its name, ID, device class, and behavior notes — lives in **[docs/entities.md](docs/entities.md)**.

### Voice and Assist

The integration registers HelloFresh intent handlers for:

- next delivery status
- meal-selection status
- manual refresh

These handlers are intended for Home Assistant conversation workflows and future sentence matching support.

### Services

**24 services** cover everything the integration can do, grouped roughly as:

| Group | Examples |
|---|---|
| **Meals and Market** | `get_weeks`, `select_meals`, `select_market_items`, `preview_meal_price` |
| **Delivery schedule** | `skip_week`, `unskip_week`, `reschedule_week`, `change_delivery_weekday` |
| **Plan and account** | `get_account_summary`, `change_plan`, `get_spending` |
| **Food profile** | `get_food_profile`, `set_food_profile` |
| **Recipes and favorites** | `get_catalog_recipes`, `get_recipe_detail`, `add_favorite` |

Many **return a response** (delivery weeks, spending, recipe detail) for use with
`response_variable` in scripts and automations. All of them appear in **Developer tools → Actions**
with inline field help.

**Full reference — every service, its parameters, and what it returns — is in
[docs/services.md](docs/services.md).**


### Automation ideas

Entity IDs below use a `hellofresh_us` prefix as the example — substitute your own (it comes from your config-entry title; see [docs/entities.md](docs/entities.md)).

**Remind me to pick meals before the cutoff.** `binary_sensor.needs_meal_selection` turns on when any upcoming week still needs your attention (HelloFresh auto-picked it, or it has too few meals); pairing it with the selection-deadline sensor puts the actual cutoff in the message:

```yaml
automation:
  - alias: "HelloFresh: meal selection reminder"
    triggers:
      - trigger: state
        entity_id: binary_sensor.hellofresh_us_needs_meal_selection
        to: "on"
    actions:
      - action: notify.mobile_app_your_phone
        data:
          title: "Pick your HelloFresh meals"
          message: >-
            HelloFresh chose meals for an upcoming week. Review them before
            {{ as_timestamp(states('sensor.hellofresh_us_next_selectable_delivery_selection_deadline'))
               | timestamp_custom('%A %-I:%M %p') }}.
```

**Tell me when the box is out for delivery.** The tracked-shipment status follows the carrier feed:

```yaml
automation:
  - alias: "HelloFresh: box on the way"
    triggers:
      - trigger: state
        entity_id: sensor.hellofresh_us_shipment_tracking_status
        to: "Out for delivery"
    actions:
      - action: notify.mobile_app_your_phone
        data:
          title: "HelloFresh is out for delivery"
          message: >-
            Box {{ states('sensor.hellofresh_us_shipment_tracking_number') }} is out
            for delivery ({{ states('sensor.hellofresh_us_tracked_shipment_carrier') }}).
```

Other useful triggers: the `calendar.delivery_schedule` entity for day-of-delivery automations, `sensor.next_selection_deadline` (a timestamp) with a time-based trigger for "24 hours before cutoff" reminders, and `binary_sensor.payload_shape_changed` to get notified if a HelloFresh site change breaks parsing.

## HelloFresh Dashboard

A ready-to-use Lovelace dashboard is included at [`dashboard/hellofresh.yaml`](dashboard/hellofresh.yaml), organized around how you actually use HelloFresh. It is **100% built-in Lovelace plus the integration's packaged cards** — no HACS frontend add-ons required (the Schedule and Diagnostics views use HA's built-in `sections` grid layout, so HA 2024.8+ is expected). Its six views:

- **My Menu** — the packaged [Meal planner card](docs/cards.md#meal-planner-card) (below), shown full width (`panel: true`): browse every week's full menu with images, see your selected meals highlighted, change the selection and per-meal serving quantity on editable weeks, and skip/unskip — all reading per-week recipes on demand via `hellofresh.get_weeks`. A per-week strip at the top shows that week's order (tracking, status, carrier, billed total).
- **Market** — the packaged [Market card](docs/cards.md#market-card): browse and order HelloFresh Market add-ons (appetizers, sides, desserts, proteins, …) per week, grouped by category, with prices and a quantity stepper per item.
- **All Recipes** — the packaged [Recipes card](docs/cards.md#recipes-card): browse HelloFresh's whole public recipe catalog (~10,000 recipes) by category and sub-category, open any recipe in full, and add or remove cookbook favorites. This is the one view that isn't about *your* subscription — the catalog is the same for every customer.
- **Food Profile** — the packaged [Food Profile card](docs/cards.md#food-profile-card): view and edit every preference HelloFresh uses to auto-preselect your meals — taste exclusions, dietary preference, liked/disliked cuisines, proteins, flavors and dish types, nutrition goals, meal types, household size, and goals.
- **Schedule** — the packaged [Schedule card](docs/cards.md#schedule-card): a clean "next box" summary (delivery date, deadline countdown, payment date, status and price), a built-in month calendar of delivery days, and a timeline of recent past and upcoming weeks with their delivery date, status, selection state, tracking, and per-week skip/unskip — plus the packaged [Subscription card](docs/cards.md#subscription-card), a condensed account overview with the holiday-delivery notice built in, and the [Cost card](docs/cards.md#cost-card), a running total of your HelloFresh spend with a monthly-cost chart and roll-up.
- **Diagnostics** — token-expiry and integration-health **tile cards** (state-colored) plus the long-form identifiers, tucked out of the way.

### The packaged cards

The integration ships **seven Lovelace cards**, registered automatically — no manual resource entry
and no HACS frontend add-on. Each reads on demand from the integration's services rather than from
entity attributes, so they show detail (full menus, images, per-item prices) that would never fit
in a sensor.

| Card | Type | What it is for |
|---|---|---|
| Meal planner | `custom:hellofresh-meal-planner-card` | Browse each week's menu, change your meal selection, skip/unskip |
| Market | `custom:hellofresh-market-card` | Browse and order Market add-ons per week |
| Recipes | `custom:hellofresh-recipes-card` | Browse the public ~10,000-recipe catalog and manage favorites |
| Food Profile | `custom:hellofresh-food-profile-card` | View and edit the preferences behind auto-preselection |
| Schedule | `custom:hellofresh-schedule-card` | Next-box summary, delivery calendar, per-week timeline |
| Subscription | `custom:hellofresh-subscription-card` | Condensed account overview |
| Cost | `custom:hellofresh-cost-card` | Spending total with a monthly chart |

Adding one takes a single line:

```yaml
type: custom:hellofresh-meal-planner-card
```

**Full reference — every card's options, features, and screenshots — is in
[docs/cards.md](docs/cards.md).**


### Recorder attribute sizes

Sensor state attributes are kept small so the recorder stores them without hitting Home Assistant's 16 KB per-state attribute limit. The full recipe catalog for a week (which can be large once the authenticated menu loads) is intentionally **not** embedded in any sensor attribute — the per-week `weeks` list on `sensor.hellofresh_us_next_selection_deadline` and the single-week context objects on other sensors carry only scalar week metadata (dates, deadline, meal counts, slot). No recorder `exclude` configuration is required. When you do need per-week recipes (names, selection state, images), call the read-only `hellofresh.get_weeks` service, which returns them on demand without touching the recorder.

The complete recipe and market data is still available where it matters: the `hellofresh.select_meals` and `hellofresh.select_market_items` services read it from the live integration state, and a full serialization (with recipes) is included in the redacted **diagnostics** export for debugging.

## Current Scope

**Reading your account** — deliveries and orders, per-week meal selections (what you actually
picked, including what shipped on past weeks), recipes with nutrition and images, shipment tracking
with carrier detail, billing and account credit, and delivered-box history over a configurable
window. Multiple subscriptions on one account are aggregated.

**Changing your account** — meal selection with per-meal serving quantities, Market add-ons,
skip/unskip, one-off reschedules, recurring delivery day, box size, and food preferences. Choosing
more or fewer meals than your plan resizes that week's box automatically (minimum 2).

**Browsing the public catalog** — ~10,000 recipes by category, full cooking detail, and cookbook
favoriting (including the full cookbook, which HelloFresh's own site only previews).

**In Home Assistant** — 50+ entities, a delivery calendar, seven Lovelace cards, voice intents,
response-returning services for dashboards, and Repairs issues when something needs your attention.

### Known limitations

- **Write actions are verified on the US and UK sites.** Other regions use the same endpoints and
  should work, but fall back to best-effort guesses if a request can't be built.
- **No push updates.** HelloFresh offers no webhook channel, so the integration polls on the
  configurable [refresh interval](#options).
- **No OAuth.** HelloFresh publishes no public API or OAuth app, so the integration signs in with
  your credentials exactly as the website does.
- **Bot protection varies by region.** Some regional sites are tuned more aggressively than others;
  see [Why bot protection can matter](#why-bot-protection-can-matter).

Because HelloFresh publishes no stable contract, write actions stay cautious: the integration uses
the website's own endpoints first, tries a small set of fallbacks if those don't fit your account,
and stops with a clear error rather than guessing.

## Troubleshooting

**The integration keeps asking me to reauthenticate.**
This means HelloFresh rejected a login with your stored credentials — most often because the account password changed, or HelloFresh required an extra verification step. Open the reauthentication prompt and enter your current HelloFresh email and password. Make sure you selected the **correct region** during setup, since each region is a separate HelloFresh login.

**Setup fails with "Invalid authentication."**
HelloFresh rejected the email/password. Double-check the credentials, confirm you can sign in to the **correct regional** HelloFresh website with them, and that you picked the matching **Country**.

**Setup fails with "Could not connect."**
Home Assistant could not reach HelloFresh, or the response wasn't understood. Check Home Assistant's network access and try again; transient site errors usually clear on a retry.

**The log shows "login BLOCKED by bot protection" (HTTP 403 with an HTML page).**
HelloFresh's website fronts its login with Cloudflare bot protection that sometimes blocks automated sign-ins. This is **not** a wrong-password problem — the request was rejected before it reached the login API, so re-entering your credentials won't help. The integration already presents a real Chrome TLS/HTTP-2 fingerprint (via the bundled `curl_cffi`) to get past this, and treats a block as temporary and retries on its next poll, so it usually clears on its own. If a region blocks email/password sign-in persistently, use the **access-token setup path** ([Setting up (access token)](#setting-up-access-token--advanced-backup)) as a backup — it bypasses the login step entirely by reusing a token from your own logged-in browser session. Confirm you can still log in to the HelloFresh website in a normal browser; a server-side block on your account or IP would need to clear regardless.

**Recipe details are missing or a "menu fallback" Repairs issue appears.**
The integration couldn't load structured menu data from the authenticated API and fell back to scraping the public menu page. Delivery tracking still works; recipe details may be less complete until the API payload is recognized again.

**A past week shows the wrong meals selected (or a paused week shows meals).**
For weeks that already shipped, the selection is taken from your **delivery history**, not the editable menu, because the menu reports the system's auto-fill picks for old weeks. Within the [**Full menu history** window](#options) (2 weeks by default) the full published menu is still shown, with your delivered meals highlighted; older weeks show just the delivered meals. A paused week shipped nothing, so it shows no selection. How far back history goes is the [**Past delivery history** option](#options) — 26 weeks by default; weeks older than that aren't loaded, so raise it (to ~56 for a full year) if you need to look further back. If a recent past week still looks wrong, attach a [diagnostics export](#diagnostics) to a GitHub issue.

**A "payload shape changed" Repairs issue appears.**
HelloFresh returned account data the integration couldn't fully parse — usually a sign the website changed. Attaching a [diagnostics export](#diagnostics) to a GitHub issue is the most helpful thing you can do here.

**A card looks outdated or is missing features after an update.**
Every card is versioned with the integration's release version: the card's resource URL carries a `?v=<version>` cache-bust that is stamped from `manifest.json`, and the registered URL is updated automatically on the first Home Assistant restart after an upgrade. If a card still looks stale, restart Home Assistant, then hard-refresh the browser (Ctrl/Cmd+Shift+R) or clear the app cache in the mobile companion app. To confirm which card build the browser actually loaded, open the browser console (F12) — each card logs a startup banner such as `HELLOFRESH-MEAL-PLANNER-CARD v2.68`, and that version should match the integration version shown under **Settings → Devices & services → HelloFresh**. You can also compare the `frontend` block in a [diagnostics export](#diagnostics), which lists the resource URLs this release expects next to the URLs actually registered.

## Diagnostics

This integration includes Home Assistant diagnostics support for config entries, with sensitive values redacted before export. Diagnostics include capability flags, subscription summaries, parsed order data, menu fallback state, delivery/tracking debug attempts, and the normalized serialized account views used by entities.

The export also contains a `frontend` block for verifying card versions: the integration release version, the card resource URLs this release expects (each stamped with `?v=<version>`), and the resource URLs actually registered in Lovelace. If an expected URL and a registered URL differ in their `?v=`, the user is loading an older cached card — a restart re-registers the current URLs.

To download a diagnostics export: **Settings → Devices & services → HelloFresh → ⋮ (the three-dot menu) → Download diagnostics**. Tokens and personal details are redacted automatically, so it is safe to attach to a bug report.

### Debug logging

When troubleshooting (e.g. auth/refresh problems, parsing errors, or before filing an issue), enable debug logging for the integration so its activity is written to the Home Assistant log.

**Option 1 — no restart, temporary.** From **Settings → Devices & services → HelloFresh → ⋮ → Enable debug logging**. Reproduce the problem, then choose **Disable debug logging** to download the captured log. This is the quickest way and resets on the next restart.

**Option 2 — `configuration.yaml`, persistent.** Add a `logger:` block, then restart Home Assistant. The integration logs under the `custom_components.hellofresh` namespace:

```yaml
# configuration.yaml
logger:
  default: warning          # keep everything else quiet
  logs:
    custom_components.hellofresh: debug
```

Tips:

- To trace only the parts you care about, target a submodule instead of the whole package — e.g. `custom_components.hellofresh.client: debug` for the HTTP/auth calls, or `custom_components.hellofresh.normalizers: debug` for payload parsing.
- Debug output can include request paths and parameters (week ids, ranges, endpoints). It does **not** log your password, and access/refresh tokens are not written in full — but treat the log as sensitive and review it before sharing.
- After capturing what you need, remove the `logs:` entry (or set it back to `warning`) and restart, since `debug` is verbose.

Logs appear in **Settings → System → Logs** (and in `config/home-assistant.log`).

For lower-level endpoint details and normalization notes, see [HELLOFRESH_API.md](HELLOFRESH_API.md).

## Development

This repository is structured as a HACS-compatible custom integration repository:

- integration code under `custom_components/hellofresh`
- metadata in `custom_components/hellofresh/manifest.json`
- HACS metadata in `hacs.json`
- translations in `custom_components/hellofresh/translations/`
- local brand assets in `custom_components/hellofresh/brand/`

It also includes:

- a pytest suite for API normalization, serialization behavior, the email/password auth and token-refresh lifecycle, the token-only setup/transport paths, and richer capability helpers
- GitHub Actions workflows for HACS validation, `hassfest`, and `python -m pytest -q`
- issue templates for bug reports and feature requests
- a [contributing guide](CONTRIBUTING.md)
- a ready-to-use [example dashboard](dashboard/hellofresh.yaml) (see [HelloFresh Dashboard](#hellofresh-dashboard))
- a full [entity reference](docs/entities.md) under `docs/`
- a documented [quality-scale target](QUALITY_SCALE.md)

Version history: each push to `main` publishes a tagged release with generated notes — see the [Releases page](https://github.com/kedube/ha-hellofresh/releases). The installed version appears in `manifest.json`, under **Settings → Devices & services → HelloFresh**, and stamped as `?v=` on the card resource URLs (see [Diagnostics](#diagnostics)).

## References

- HACS documentation:
  - https://hacs.xyz/docs/publish/integration/
- Home Assistant developer documentation:
  - https://developers.home-assistant.io/docs/creating_integration_manifest/
  - https://developers.home-assistant.io/docs/core/integration/config_flow/
  - https://developers.home-assistant.io/docs/internationalization/custom_integration/
