# Entity reference

Every entity the HelloFresh integration creates, grouped by purpose. For installation, configuration, services, and the packaged dashboard cards, see the [README](../README.md#what-it-provides).

## Sensors

Sensors are grouped below by purpose. The **Name** column is the friendly label shown in Home Assistant; the **Entity** column is the entity ID suffix. Most expose extra detail (full order, week, subscription, and tracking objects) as entity attributes.

A few conventions used in the tables:

- **Entity ID prefix.** Because entities use `has_entity_name`, the real IDs are prefixed from your config-entry title — a "HelloFresh (US)" account produces `sensor.hellofresh_us_next_delivery_date`, etc. The tables show the suffix only; substitute your own prefix.
- **Device class.** Where noted (`Date`, `Timestamp`, monetary), Home Assistant formats and graphs the value accordingly. Sensors without a noted device class are plain strings or numbers.
- **`None` / unavailable.** Many sensors report `None` (shown as *Unknown* in the UI) when the underlying field isn't set for your account or region — e.g. no holiday shift, no tracked shipment. This is normal, not an error. A few sensors where "empty" is a definite *"there isn't one"* rather than missing data instead show the literal **`None`** so the dashboard reads clearly: **Next skipped week**, **Next delivery coupon**, and **Holiday delivery message**.
- **Attributes vs. state.** The state is the single headline value; richer context (the full week, order, subscription, or tracking object) is exposed as entity **attributes**. Full per-week recipe lists are deliberately kept out of attributes (recorder size limit) and are read on demand via `hellofresh.get_weeks` — see [Recorder attribute sizes](../README.md#recorder-attribute-sizes).

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
| Next delivery blocked | `sensor.next_delivery_blocked` | `True`/`False` for whether **HelloFresh** has blocked delivery for the next configurable week (its `deliveryBlocked` flag) — e.g. your area is temporarily out of the delivery zone that week, a carrier/weather disruption, or a no-delivery holiday week. This is imposed by HelloFresh, distinct from *you* skipping a week; usually `False`. |
| Holiday delivery date | `sensor.next_holiday_delivery_date` | Rescheduled delivery date when the next week's box is shifted for a holiday; `None` when no holiday shift applies. `Date` device class. |
| Holiday delivery message | `sensor.next_holiday_message` | HelloFresh's holiday-shift notice for the next week (e.g. why the date moved); shows **`None`** when no holiday message is present. |

**Meal selection**

| Name | Entity | Description |
| --- | --- | --- |
| Weeks preselected by HelloFresh | `sensor.weeks_needing_selection` | Count of upcoming weeks whose meals HelloFresh **auto-picked** (preselected) for you rather than you choosing them — the weeks worth reviewing before the cutoff. Attributes (`weeks`) list each one. (The entity ID keeps the older `weeks_needing_selection` suffix for backward compatibility.) |
| Next delivery selection deadline | `sensor.next_selection_deadline` | Cutoff timestamp for the **next delivery** week (that week's `cutoffDate`, falling back to the subscription's `nextCutoffDate`) — the "Edit delivery by …" deadline. `Timestamp` device class. |
| Next selectable delivery selection deadline | `sensor.next_selectable_delivery_selection_deadline` | Cutoff timestamp for the **next modifiable** delivery week (`nextModifiableDeliveryWeek`'s `cutoffDate`) — the soonest box the customer can still change. `Timestamp` device class. |
| Next delivery meal count | `sensor.selected_meal_count` | Meals already chosen for the next pending/configurable week; `0` when none is pending. Excludes add-on and market items. |
| Next selectable delivery meal count | `sensor.next_selectable_delivery_meal_count` | Meals already chosen for the next modifiable delivery week (`nextModifiableDeliveryWeek`); `0` when no modifiable week is resolved. |
| Next delivery market count | `sensor.selected_market_count` | Number of distinct HelloFresh Market add-ons (extras) selected for the next configurable week; `0` when none. Counts market items only — meals are counted by *Next delivery meal count*. |
| Next selectable delivery market count | `sensor.next_selectable_delivery_market_count` | Number of distinct HelloFresh Market add-ons selected for the next modifiable delivery week; `0` when none or no modifiable week is resolved. |
| Next delivery preselected | `sensor.next_delivery_preselected` | `True`/`False` for whether HelloFresh **auto-picked** the meals for the next configurable week (the week's `mealsPreselected` flag) rather than you choosing them; `None` when no configurable week is resolved. A `True` here is the per-week signal behind *Weeks preselected by HelloFresh*. |
| Next selectable delivery preselected | `sensor.next_selectable_delivery_preselected` | `True`/`False` for whether HelloFresh auto-picked the meals for the next modifiable delivery week; `None` when no modifiable week is resolved. |
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
| Next delivery coupon | `sensor.next_box_coupon` | Active promo/coupon code applied to the primary subscription; shows **`None`** when no coupon is on file. |

**Account & subscription**

| Name | Entity | Description |
| --- | --- | --- |
| Selected plan | `sensor.selected_plan` | Plan name from the primary subscription (e.g. `Meat & Veggies`); shows the display name when a specific plan name isn't returned. |
| Number of people | `sensor.number_of_people` | Servings-per-box setting from the primary subscription (e.g. `2`). |
| Account subscription count | `sensor.subscription_count` | Number of subscriptions on the account. Most sensors report on the primary (first) subscription only, so a value above 1 means additional subscriptions aren't individually surfaced. |
| Subscription status | `sensor.subscription_status` | Plan-level status of the primary subscription (e.g. `active`, `paused`), lowercased from the API. This is the whole-plan state, distinct from per-week skip state; `None` when the account doesn't report it. |
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
| Tracked shipment carrier | `sensor.tracked_shipment_carrier` | Carrier for the tracked shipment (e.g. `Veho`, `UPS`, `FedEx`, `DoorDash`). Resolved from HelloFresh's shipment-tracking lookup — the delivery payload itself carries no carrier field — so it is `None` for weeks with no published tracking, and appears once a box is in transit. |
| Tracked shipment estimate | `sensor.tracked_shipment_estimate` | The **carrier's own** estimated delivery time for the tracked shipment, from the same shipment-tracking lookup as the carrier above. Date precision — the carrier reports midnight of the estimated day, unlike the scheduled noon anchor behind `next_delivery_date` — so it answers "which day does the carrier now think this arrives?" rather than "which day was it booked for". `None` until a box has published tracking. |
| Tracked shipment date | `sensor.tracked_shipment_date` | When the most recent box **actually arrived** — the carrier's handover timestamp, not the day it was scheduled for. A box delivered at 22:53 ET is already the next day in UTC, so this can differ from `last_delivery_date` (which reports the scheduled day). `None` until a box has been delivered with carrier tracking attached; it never falls back to the scheduled date. |
| Tracked shipment URL | `sensor.next_delivery_tracking_url` | Direct carrier tracking link for the best-tracked order; `None` when no link is available. |

**History & skipped weeks**

| Name | Entity | Description |
| --- | --- | --- |
| Last delivery date | `sensor.last_delivery_date` | Delivery date of the most recently completed week from delivery history. `Date` device class. |
| Skipped week count | `sensor.skipped_week_count` | Number of upcoming weeks marked as skipped. |
| Next skipped week | `sensor.next_skipped_week` | Display name of the nearest upcoming skipped week (e.g. `2026-W24`); shows **`None`** when no weeks are skipped. |

**Diagnostic** (shown under the device's *Diagnostic* section)

| Name | Entity | Description |
| --- | --- | --- |
| Account delivery subscription ID | `sensor.next_delivery_subscription` | Internal HelloFresh subscription ID for the next order. Diagnostic. |
| Access token time remaining | `sensor.access_token_minutes_remaining` | Whole minutes until the current access token expires (unit `min`). Access tokens are short-lived (~30 min) and auto-refreshed. Attributes expose the exact `expires_at` timestamp and `seconds_remaining`. Diagnostic. |
| Refresh token time remaining | `sensor.refresh_token_days_remaining` | Whole days until the refresh token expires (unit `d`). When the refresh token expires the integration logs in again with your stored credentials; if that login fails you are prompted to reauthenticate. Attributes expose the exact `expires_at` timestamp and `seconds_remaining`. Diagnostic. |
| API base URL | `sensor.api_base_url` | Regional API base URL the integration is using. Diagnostic. |

## Binary sensors

| Entity | Notes |
| --- | --- |
| `binary_sensor.needs_meal_selection` | `True` when at least one upcoming week still needs your attention — HelloFresh auto-picked its meals (review them) or it has fewer than the minimum 2 meals. A week you deliberately resized to fewer meals than your plan (e.g. 2 on a 3-meal plan) counts as complete and does **not** trigger this. The primary signal for reminder automations. |
| `binary_sensor.tracked_shipment_available` | `True` when the most-recent order has active shipment tracking data (carrier, tracking number, or tracking URL) |

**Diagnostic** (shown under the device's *Diagnostic* section)

| Entity | Notes |
| --- | --- |
| `binary_sensor.write_actions_available` | `True` when the account advertises at least one supported write action (meal selection, skip/unskip, reschedule, delivery-weekday change, etc.) |
| `binary_sensor.payload_shape_changed` | `True` when HelloFresh returned authenticated data that the integration could not fully parse; signals that an API update may require integration changes; a matching Repairs issue is also raised |
| `calendar.delivery_schedule` | Calendar of all upcoming and recent HelloFresh deliveries (each event title includes the delivery week and order status). Add it to a **Calendar** dashboard card to see the dates, or use it in calendar-trigger automations. Its own state is just `on`/`off` (a delivery is active today or not) — the standard for any calendar entity — so it's filed here under Diagnostic to keep it out of the main entities list. |

## Other

| Entity | Notes |
| --- | --- |
| `button.refresh_data` | Triggers an immediate coordinator refresh outside the normal polling interval |
| `switch.skip_next_modifiable_week` | Shown as **Skip next selectable delivery week**. On = skip the next modifiable delivery week (no box ships); off = restore it. State reflects whether that week is currently skipped. |
| `todo.prep_list` | Shown as **Prep list**. The pantry staples that HelloFresh does **not** ship — salt, oil, butter, eggs — for the selected meals of your **next delivery**. Add it to a **To-do list** card. Quantities are added up, converting between units of the same family where that is exact — 4 tablespoon + 3 teaspoon shows as **5 tablespoon**, and `tbsp`/`tablespoon` are recognized as one unit. Conversion stops when the result would be unmeasurable (`1 cup + 1 teaspoon` stays as two amounts rather than becoming "1.02 cup") and never crosses between weight and volume, or metric and imperial. A unit with no number means one of it (`teaspoon` → **1 teaspoon**). Items are due on the delivery date. It is a projection of that week's selection, so it cannot be added to or deleted from, only checked off. Attributes carry `week_id` and `delivery_date` for dashboard headings. |
| `todo.prep_list_week_2` | Shown as **Prep list (following week)**. The same, for the delivery *after* the next one — a **separate entity** so each week gets its own card and section rather than one merged list. Empty until a second box is scheduled with meals selected; skipped weeks ship nothing and are passed over. As a box arrives the weeks shift up: `prep_list` always means the next delivery, and check-offs travel with the week they belong to, so anything already ticked here stays ticked when it becomes the current box. |

## Notes

Order, week, menu, subscription, capability, and tracking details are exposed as entity attributes. Full per-week recipe lists are deliberately kept out of attributes (recorder size limit — see [Recorder attribute sizes](../README.md#recorder-attribute-sizes)) and read on demand via `hellofresh.get_weeks`.

Some entity IDs keep an older suffix that no longer matches the displayed name (e.g. `sensor.weeks_needing_selection` shows as **Weeks preselected by HelloFresh**, `sensor.required_meal_count` as **Number of meals**). The **Name** and **Entity** columns above are authoritative — match on the entity ID, not the label.
