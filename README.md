# HelloFresh Integration for Home Assistant

A custom Home Assistant integration that reads your HelloFresh account and menu data so you can track upcoming deliveries, shipment status, recipe selection deadlines, and week-by-week meal planning directly from Home Assistant.

It also exposes delivery-history summaries, shipment tracking metadata, billing/payment dates, and authenticated menu and profile details when those endpoints are available for your region and account.

> ⚠️ This is an **unofficial** integration, reverse-engineered from the HelloFresh website. It is not affiliated with or endorsed by HelloFresh, and the underlying API may change at any time.

![Alt screenshot](images/hellofresh_screenshot.png)

[![CI](https://github.com/kedube/ha-hellofresh/actions/workflows/ci.yml/badge.svg)](https://github.com/kedube/ha-hellofresh/actions/workflows/ci.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)

## Contents

- [Installation](#installation)
- [Configuration](#configuration)
- [What It Provides](#what-it-provides)
- [Example Dashboard](#example-dashboard)
- [Current Scope](#current-scope)
- [Troubleshooting](#troubleshooting)
- [Diagnostics](#diagnostics)
- [Development](#development)
- [References](#references)

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

Open the integration options to adjust:

- **Refresh interval (minutes)** — how often account data is polled. Default is **180**; allowed range is **5–1440**. (This is the data-refresh cadence; the bearer token is refreshed on its own faster-running schedule regardless of this value.)
- **Use public menu fallback** — when authenticated menu data is unavailable, scrape the public regional menu page so recipe data still appears.

[![Open your Home Assistant instance and show the HelloFresh integration.](https://my.home-assistant.io/badges/integration.svg)](https://my.home-assistant.io/redirect/integration/?domain=hellofresh)

### Supported regions

Choose the matching country during setup:

| Region | Code | Website | Status |
| --- | --- | --- | --- |
| United States | `us` | https://www.hellofresh.com | ✅ |
| Canada | `ca` | https://www.hellofresh.ca | Untested |
| United Kingdom | `uk` | https://www.hellofresh.co.uk | ✅ |  
| Australia | `au` | https://www.hellofresh.com.au | Untested |
| Germany | `de` | https://www.hellofresh.de | Untested |
| Netherlands | `nl` | https://www.hellofresh.nl | Untested |

## What It Provides

### Entities

#### Sensors

Sensors are grouped below by purpose. The **Name** column is the friendly label shown in Home Assistant; the **Entity** column is the entity ID suffix. Most expose extra detail (full order, week, subscription, and tracking objects) as entity attributes.

**Deliveries & orders**

| Name | Entity | Description |
| --- | --- | --- |
| Next delivery date | `sensor.next_delivery_date` | Delivery date of the subscription's next delivery (API `nextDelivery`). `Date` device class. |
| Next delivery week | `sensor.next_delivery_week` | **ISO week identifier** of the next delivery (e.g. `2026-W25`), from the API `nextDeliveryWeek` and normalized against the delivery date. A week label, deliberately distinct from `Next delivery date`. Plain string (no device class). |
| Next selectable delivery date | `sensor.next_selectable_delivery_date` | Date of the next delivery the customer can still modify (API `nextModifiableDeliveryDate`) — typically the week after the next delivery. `Date` device class. |
| Next selectable delivery week | `sensor.next_selectable_delivery_week` | **ISO week identifier** of the next modifiable delivery (API `nextModifiableDeliveryWeek`). Plain string (no device class). |
| Delivery Window | `sensor.next_delivery_slot` | Delivery time-slot label for the next order (e.g. `Mondays: 8AM - 8PM`); `None` when no preferred window is set. |
| Upcoming delivery count | `sensor.upcoming_delivery_count` | Number of non-skipped deliveries with a delivery date today or later. |
| Next delivery count | `sensor.delivery_count_this_week` | Number of deliveries scheduled within the current calendar week (Mon–Sun). |
| Next delivery blocked | `sensor.next_delivery_blocked` | `True`/`False` flag for whether HelloFresh has blocked delivery for the next configurable week (e.g. unavailable in your area that week). |
| Holiday delivery date | `sensor.next_holiday_delivery_date` | Rescheduled delivery date when the next week's box is shifted for a holiday; `None` when no holiday shift applies. `Date` device class. |
| Holiday delivery message | `sensor.next_holiday_message` | HelloFresh's holiday-shift notice for the next week (e.g. why the date moved); `None` when no holiday message is present. |

**Meal selection**

| Name | Entity | Description |
| --- | --- | --- |
| Weeks needing meal selection | `sensor.weeks_needing_selection` | Count of upcoming weeks that still require meal selection. Attributes list every pending week. |
| Next delivery selection deadline | `sensor.next_selection_deadline` | Cutoff timestamp for the **next delivery** week (that week's `cutoffDate`, falling back to the subscription's `nextCutoffDate`) — the "Edit delivery by …" deadline. `Timestamp` device class. |
| Next selectable delivery selection deadline | `sensor.next_selectable_delivery_selection_deadline` | Cutoff timestamp for the **next modifiable** delivery week (`nextModifiableDeliveryWeek`'s `cutoffDate`) — the soonest box the customer can still change. `Timestamp` device class. |
| Next delivery meal count | `sensor.selected_meal_count` | Meals already chosen for the next pending/configurable week; `0` when none is pending. Excludes add-on and market items. |
| Next selectable delivery meal count | `sensor.next_selectable_delivery_meal_count` | Meals already chosen for the next modifiable delivery week (`nextModifiableDeliveryWeek`); `0` when no modifiable week is resolved. |
| Number of meals | `sensor.required_meal_count` | Meals that must be selected for the next pending week; falls back to the subscription's plan count when the week doesn't specify one. |

**Billing & payments**

| Name | Entity | Description |
| --- | --- | --- |
| Next delivery total price | `sensor.next_box_total_price` | Sum of all charges for the next upcoming delivery date, across every billing item for that date. Monetary device class; the unit reflects the subscription currency. |
| Account credit | `sensor.account_credit` | Spendable account credit that applies automatically to the next order (API `/gw/payments/customers/{uuid}/balance` → `amount`). Monetary device class; unit from `currencyCode`. |
| Selected plan total price | `sensor.selected_plan_total_price` | Standing weekly plan price **including shipping** (the `grandTotal` from a recurring `/gw/calculate` for the primary subscription) — the price shown in plan settings, distinct from the next box's actual charge. Monetary device class; unit reflects the subscription currency. |
| Recent payment date | `sensor.recent_payment_date` | Date of the most recent HelloFresh charge that has **already been billed** (the order's `createdAt`), from order history. Because HelloFresh bills a box a few days before it ships, this reflects your last actual charge even when that box's delivery is still upcoming. Charges dated in the future are ignored. `Date` device class. |
| Next delivery payment date | `sensor.next_payment_date` | Estimated date of the next charge — the upcoming order's delivery date, falling back to the subscription's next cutoff date. `Date` device class. |
| Next delivery order ID | `sensor.recent_order_id` | Order number for the next upcoming delivery, as shown in the HelloFresh UI (the `orderNr` field). |
| Next delivery coupon | `sensor.next_box_coupon` | Active promo/coupon code applied to the primary subscription; `None` when no coupon is on file. |

**Account & subscription**

| Name | Entity | Description |
| --- | --- | --- |
| Selected plan | `sensor.selected_plan` | Plan name from the primary subscription (e.g. `Meat & Veggies`); shows the display name when a specific plan name isn't returned. |
| Number of people | `sensor.number_of_people` | Servings-per-box setting from the primary subscription (e.g. `2`). |
| Account subscription count | `sensor.subscription_count` | Number of subscriptions on the account. Most sensors report on the primary (first) subscription only, so a value above 1 means additional subscriptions aren't individually surfaced. |
| Delivery address | `sensor.delivery_address` | Single-line delivery address from the primary subscription; redacted in diagnostics exports. |
| Account ID | `sensor.account_id` | HelloFresh customer account ID. |
| Boxes received | `sensor.boxes_received` | Lifetime count of boxes delivered to the account, from the authenticated profile endpoint. |
| Available menu recipe count | `sensor.public_menu_recipe_count` | Number of recipes on the current week's public menu; `0` when menu data is unavailable. |

**Shipment tracking**

| Name | Entity | Description |
| --- | --- | --- |
| Tracked shipment status | `sensor.shipment_tracking_status` | Carrier tracking status of the best-tracked shipment (in transit, out for delivery, delivered, exception), from the SCM tracking feed. The icon reflects the state; `None` when no tracked shipment exists. |
| Next delivery status | `sensor.next_order_status` | Box lifecycle status of the next delivery (e.g. `preparing`, `running`, `on_the_way`, `delivered`). The icon reflects the current state. |
| Tracked shipment number | `sensor.shipment_tracking_number` | Parcel/tracking number for the tracked shipment; shares attributes with the tracking-status sensor. |
| Tracked shipment carrier | `sensor.tracked_shipment_carrier` | Carrier for the tracked shipment (e.g. `UPS`, `FedEx`, `DoorDash`); `None` when no tracking data is present. |
| Tracked shipment URL | `sensor.next_delivery_tracking_url` | Direct carrier tracking link for the best-tracked order; `None` when no link is available. |

**History & skipped weeks**

| Name | Entity | Description |
| --- | --- | --- |
| Last delivery date | `sensor.last_delivery_date` | Delivery date of the most recently completed week from delivery history. `Date` device class. |
| Skipped week count | `sensor.skipped_week_count` | Number of upcoming weeks marked as skipped. |
| Next skipped week | `sensor.next_skipped_week` | Display name of the nearest upcoming skipped week (e.g. `2026-W24`); `None` when none are skipped. |

**Diagnostic** (shown under the device's *Diagnostic* section)

| Name | Entity | Description |
| --- | --- | --- |
| Account delivery subscription ID | `sensor.next_delivery_subscription` | Internal HelloFresh subscription ID for the next order. Diagnostic. |
| Access token time remaining | `sensor.access_token_minutes_remaining` | Whole minutes until the current access token expires (unit `min`). Access tokens are short-lived (~30 min) and auto-refreshed. Attributes expose the exact `expires_at` timestamp and `seconds_remaining`. Diagnostic. |
| Refresh token time remaining | `sensor.refresh_token_days_remaining` | Whole days until the refresh token expires (unit `d`). When the refresh token expires the integration logs in again with your stored credentials; if that login fails you are prompted to reauthenticate. Attributes expose the exact `expires_at` timestamp and `seconds_remaining`. Diagnostic. |
| API base URL | `sensor.api_base_url` | Regional API base URL the integration is using. Diagnostic. |

#### Binary sensors

| Entity | Notes |
| --- | --- |
| `binary_sensor.needs_meal_selection` | `True` when at least one upcoming delivery week still requires meal selection; the primary signal for reminder automations |
| `binary_sensor.write_actions_available` | `True` when the account advertises at least one supported write action (meal selection, skip/unskip, reschedule, delivery-weekday change, etc.); diagnostic entity |
| `binary_sensor.tracked_shipment_available` | `True` when the most-recent order has active shipment tracking data (carrier, tracking number, or tracking URL) |
| `binary_sensor.payload_shape_changed` | `True` when HelloFresh returned authenticated data that the integration could not fully parse; signals that an API update may require integration changes; a matching Repairs issue is also raised; diagnostic entity |

#### Other

| Entity | Notes |
| --- | --- |
| `calendar.delivery_schedule` | Calendar entity showing all upcoming and recent HelloFresh deliveries as calendar events; each event title includes the delivery week and order status |
| `button.refresh_data` | Triggers an immediate coordinator refresh outside the normal polling interval |
| `switch.skip_next_modifiable_week` | Shown as **Skip next selectable delivery week**. On = skip the next modifiable delivery week (no box ships); off = restore it. State reflects whether that week is currently skipped. |

Order, week, menu, subscription, capability, and tracking details are exposed as entity attributes, and authenticated history endpoints feed recent delivered-week context into the delivery-history sensors' attributes. Full per-week recipe lists are intentionally **not** included in attributes (to stay under the recorder's size limit — see [Recorder attribute sizes](#recorder-attribute-sizes)); they remain available in the diagnostics export.

Several entity IDs differ from their displayed names — for example `sensor.required_meal_count` shows as **Number of meals**, `sensor.public_menu_recipe_count` as **Available menu recipe count**, `sensor.recent_order_id` as **Next delivery order ID**, `sensor.next_delivery_slot` as **Delivery Window**, `sensor.selected_meal_count` as **Next delivery meal count**, `sensor.delivery_count_this_week` as **Next delivery count**, `sensor.next_order_status` as **Next delivery status**, `sensor.shipment_tracking_status` as **Tracked shipment status**, and `switch.skip_next_modifiable_week` as **Skip next selectable delivery week** (see the Name columns above).

### Voice and Assist

The integration now registers HelloFresh intent handlers for:

- next delivery status
- meal-selection status
- manual refresh

These handlers are intended for Home Assistant conversation workflows and future sentence matching support.

### Services

- `hellofresh.refresh_data`
- `hellofresh.get_weeks` — **returns a response**: delivery weeks with full recipe and selection detail (recipes aren't exposed as entity attributes). Optionally filter to one `week_id`. Powers the example "Meal planner" dashboard view that scrolls between weeks and shows selected meals.
- `hellofresh.select_meals`
- `hellofresh.skip_week`
- `hellofresh.unskip_week`
- `hellofresh.reschedule_week` — move a single week's delivery to a different delivery option (one-off)
- `hellofresh.change_delivery_weekday` — change the recurring delivery option/interval for a plan (affects all future deliveries)

When multiple HelloFresh accounts are configured, service calls can target a specific entry with `config_entry_id`.

The integration also supports a lightweight actionable flow inside Home Assistant:

- `switch.skip_next_modifiable_week` (**Skip next selectable delivery week**) skips or restores the next modifiable delivery week; turning it on skips the box, turning it off ships it
- the `hellofresh.skip_week` / `hellofresh.unskip_week` services do the same for a chosen week

Skip/unskip use the same write endpoints the HelloFresh website uses. If one is unavailable for your region or account shape, the integration tries a small set of fallbacks and, if none work, raises a Repairs issue instead of silently sending more guesses.

## Example Dashboard

A ready-to-use Lovelace dashboard is included at [`examples/dashboard.yaml`](examples/dashboard.yaml), organized around how you actually use HelloFresh:

- **Overview** — a hero "next box" card (date with a relative countdown and status-colored icon) plus key facts as chips (including account credit), meal-selection progress as a gauge, the next/selectable delivery meal counts and deadlines, the skip switch, a per-week breakdown table of weeks still needing a selection, and a shipment-tracking card with a tappable carrier link. Conditional banners surface only when relevant: meals needing selection, a holiday delivery change, an approaching reauthentication deadline, or an unexpected-payload warning.
- **Planning** — the delivery calendar, upcoming/skipped counts, key dates (next + next-selectable delivery), and a 90-day order/tracking history graph.
- **Account** — billing dates, account credit, active coupon, subscription details, and refresh + skip actions.
- **Diagnostics** — token-expiry and integration-health entities, tucked out of the way.
- **Meal planner** — a week-cursor pattern (an `input_number` index with ◀ ▶) that reads per-week recipes and selection state on demand via the `hellofresh.get_weeks` service, plus per-week skip. Requires a couple of helper entities documented inline in the file; for full recipe-with-images browsing a custom card would consume the same service.

The **Overview** view uses two popular HACS frontend cards — [Mushroom](https://github.com/piitaya/lovelace-mushroom) (hero card, chips, tappable tracking link) and is otherwise built-in. The Planning, Account, and Diagnostics views need no add-ons. Each Mushroom card has a commented built-in fallback (e.g. a `glance` "next box" and a plain `attribute` tracking row) inline in the file, so you can drop the HACS dependency entirely if you prefer.

The per-week breakdown and the tappable tracking link read directly from entity **attributes** (the `weeks` list on the selection-deadline sensor and `tracking_url` on the tracking-status sensor) — data the headline state alone doesn't show.

To use it:

1. Optionally install **Mushroom** via **HACS → Frontend** (or use the built-in fallbacks noted in the file).
2. Open **Settings → Dashboards → ⋮ → Edit in YAML** (or add a new YAML-mode dashboard) and paste the file's contents.
3. Update the entity-ID prefix. Because entities use `has_entity_name`, their IDs derive from your config-entry title — a "HelloFresh (US)" account produces IDs like `sensor.hellofresh_us_next_delivery_date`. The example uses the `hellofresh_us_` prefix throughout; **find-and-replace it** with the prefix your account actually uses (check **Settings → Devices & Services → HelloFresh → entities** for the real IDs).

### Recorder attribute sizes

Sensor state attributes are kept small so the recorder stores them without hitting Home Assistant's 16 KB per-state attribute limit. The full recipe catalog for a week (which can be large once the authenticated menu loads) is intentionally **not** embedded in any sensor attribute — the per-week `weeks` list on `sensor.hellofresh_us_next_selection_deadline` and the single-week context objects on other sensors carry only scalar week metadata (dates, deadline, meal counts, slot). No recorder `exclude` configuration is required. When you do need per-week recipes (names, selection state, images), call the read-only `hellofresh.get_weeks` service, which returns them on demand without touching the recorder.

The complete recipe data is still available where it matters: the `hellofresh.select_meals` service reads it from the live integration state, and a full serialization (with recipes) is included in the redacted **diagnostics** export for debugging.

## Current Scope

What works:

- email/password login through the HelloFresh `/gw` auth gateway, with automatic access-token refresh and credential-based re-login
- an alternative token-only setup path (paste an `apiV2Auth` token) as a backup when login is blocked, valid until the refresh token expires
- a real Chrome-on-Windows-11 browser fingerprint (Client Hints plus a genuine TLS/HTTP-2 fingerprint via `curl_cffi`) to pass Cloudflare bot protection on the auth and data requests
- token validation against `/gw/api/customers/me/subscriptions`
- account delivery and order parsing from verified or likely `/gw/...` delivery endpoints
- aggregation across multiple subscriptions on the same HelloFresh account
- account profile metrics such as delivered box counts when exposed by authenticated profile endpoints
- delivered-week history summaries from authenticated past-delivery endpoints
- richer recipe parsing including ingredient, nutrition, image, and tag metadata when present
- authenticated menu API attempts before falling back to public HTML scraping
- shipment tracking extraction and SCM enrichment when the payload includes carrier, parcel, or HelloFresh tracking-page details
- public menu scraping from the regional `/menus` page
- reminders driven by `binary_sensor.needs_meal_selection`
- delivery calendar plus selection-deadline timestamp sensors for both the next delivery and the next selectable (modifiable) delivery
- account credit balance from the payments balance endpoint
- skipping/restoring the next modifiable delivery week from a switch, plus `skip_week` / `unskip_week` / `select_meals` services that use the website's own write endpoints with conservative fallbacks
- on-demand per-week recipe and selection detail via the response-returning `hellofresh.get_weeks` service (recipes are kept out of entity attributes to respect the recorder size limit)
- Repairs issues when the integration falls back to public menu data, sees unexpected payload shapes, or cannot verify a write action

What is not implemented yet:

- a first-party OAuth / account-linking flow (the integration logs in directly with your stored email and password, or reuses a pasted token, instead)
- verification of the write endpoints beyond the US site (the US meal-selection and skip/unskip requests are confirmed; other regions fall back to best-effort guesses)
- live push updates from HelloFresh, if an official push channel exists
- a packaged custom Lovelace card for browsing recipes week-by-week (the data path exists — the `hellofresh.get_weeks` service returns full per-week recipe detail — and an example YAML "Meal planner" dashboard view drives it with built-in cards plus optional Mushroom, see [Example Dashboard](#example-dashboard))

Because HelloFresh does not publish a stable consumer integration contract here, write actions stay cautious: the integration uses the website's confirmed write endpoints first, tries a small set of fallbacks if those don't fit your account, and stops with a clear error rather than guessing endlessly.

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

**A "payload shape changed" Repairs issue appears.**
HelloFresh returned account data the integration couldn't fully parse — usually a sign the website changed. Attaching a [diagnostics export](#diagnostics) to a GitHub issue is the most helpful thing you can do here.

## Diagnostics

This integration includes Home Assistant diagnostics support for config entries, with sensitive values redacted before export. Diagnostics include capability flags, subscription summaries, parsed order data, menu fallback state, delivery/tracking debug attempts, and the normalized serialized account views used by entities.

To download a diagnostics export: **Settings → Devices & services → HelloFresh → ⋮ (the three-dot menu) → Download diagnostics**. Tokens and personal details are redacted automatically, so it is safe to attach to a bug report.

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
- a ready-to-use [example dashboard](examples/dashboard.yaml) (see [Example Dashboard](#example-dashboard))
- a documented [quality-scale target](QUALITY_SCALE.md)

Recent local verification included:

- `python3 -m pytest -q`
- `python3 -m compileall custom_components/hellofresh`

## References

- Reverse-engineered example used to validate current auth and endpoint assumptions:
  - https://github.com/CNoetzel/HelloFresh-RecipeDownloader
  - https://raw.githubusercontent.com/CNoetzel/HelloFresh-RecipeDownloader/master/downloader.py
- HACS documentation:
  - https://hacs.xyz/docs/publish/integration/
- Home Assistant developer documentation:
  - https://developers.home-assistant.io/docs/creating_integration_manifest/
  - https://developers.home-assistant.io/docs/core/integration/config_flow/
  - https://developers.home-assistant.io/docs/internationalization/custom_integration/
