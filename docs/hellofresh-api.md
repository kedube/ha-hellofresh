# HelloFresh API

HelloFresh has no public API. This page describes the private API its website uses, as far as this
project has observed it: each endpoint, what it takes and what it returns. HelloFresh can change any
of it without notice.

The facts come from three sources:

- the website's own requests, recorded in the browser and in this integration's diagnostics exports;
- HelloFresh's Android app (version 26.41.1, October 2026), read without running it;
- this integration's own requests, which work against real accounts.

Facts known only from the Android app are marked **(app)**. The app uses the same services through
its own gateway, so the website may send or receive something different. Paths the app calls are
written here with the website's `/gw/` prefix. What nobody has observed yet is listed under
[What isn't known](#what-isnt-known). All values in examples are made up.

## Contents

| Section | Covers |
| --- | --- |
| [Basics](#basics) | Markets, request and response conventions, identifiers, product handles |
| [Authentication](#authentication) | Signing in, renewing tokens |
| [Account](#account) | Subscriptions, the customer, plans, box sizes, delivery days |
| [Deliveries](#deliveries) | Weeks, skipping, moving a delivery, history, tracking |
| [Menus](#menus) | A week's menu, filters, the regional menus, meal preference |
| [Cart](#cart) | Saving a week's meals and Market extras |
| [Prices](#prices) | Pricing a week's box and the plan |
| [Billing](#billing) | Orders, credit, the card on file, weekly discounts |
| [Recipes](#recipes) | Recipes, search, the catalog, images, the cookbook, favorites |
| [Food profile](#food-profile) | The preferences HelloFresh picks meals by |
| [Other endpoints](#other-endpoints) | Endpoints seen but not described here |
| [What isn't known](#what-isnt-known) | Open questions |

## Basics

### Markets

Each market has its own website, and the API lives under it at `/gw/`. Requests name the market's
ISO country code and a locale.

| Market | Website | `country` | Default `locale` | Currency |
| --- | --- | --- | --- | --- |
| United States | `https://www.hellofresh.com` | `US` | `en-US` | USD |
| Canada | `https://www.hellofresh.ca` | `CA` | `en-CA` | CAD |
| United Kingdom | `https://www.hellofresh.co.uk` | `GB` | `en-GB` | GBP |
| Australia | `https://www.hellofresh.com.au` | `AU` | `en-AU` | AUD |
| New Zealand | `https://www.hellofresh.co.nz` | `NZ` | `en-NZ` | NZD |
| Germany | `https://www.hellofresh.de` | `DE` | `de-DE` | EUR |
| Austria | `https://www.hellofresh.at` | `AT` | `de-AT` | EUR |
| Switzerland | `https://www.hellofresh.ch` | `CH` | `de-CH` | CHF |
| Netherlands | `https://www.hellofresh.nl` | `NL` | `nl-NL` | EUR |
| Belgium | `https://www.hellofresh.be` | `BE` | `nl-BE` | EUR |
| Luxembourg | `https://www.hellofresh.lu` | `LU` | `fr-LU` | EUR |
| France | `https://www.hellofresh.fr` | `FR` | `fr-FR` | EUR |
| Ireland | `https://www.hellofresh.ie` | `IE` | `en-IE` | EUR |
| Denmark | `https://www.hellofresh.dk` | `DK` | `da-DK` | DKK |
| Norway | `https://www.hellofresh.no` | `NO` | `nb-NO` | NOK |
| Sweden | `https://www.hellofresh.se` | `SE` | `sv-SE` | SEK |

- The United Kingdom is `GB`. `country=UK` makes signing in fail.
- An account's own locale is its subscription's `customer.locale`.
- The app's built-in market list has the same codes, locales, currencies and websites (app).
- Spain and Italy closed in early 2026. Their websites still load and still answer
  `/gw/auth/email/status`, but `/plans` redirects to `/pages/closure`. Japan closed in 2022, and its
  domain no longer resolves.

The Android app uses a separate gateway, `https://mobile.gw.hellofresh.com/`, with its own OAuth
client and app attestation. This page describes the website's gateway.

### Requests

- **URL:** `https://<website>/gw/<path>`.
- **Query:** most calls take `country` (upper case) and `locale`. Each endpoint below lists its
  parameters. The app sends `country` and `locale` on every call (app).
- **Authorization:** `Authorization: Bearer <access token>`; see [Authentication](#authentication).
- **Feature headers:** the website sends these on its account and menu calls:

  ```http
  X-Market-API-Version: 2
  X-Food-Categorization: v1
  x-sort-variations-by-quantity: true
  ```

  The app sends the first two, and only when it asks for a week's menu (app).
- **`x-requested-by`:** on some calls the website names the part of the site making it, such as
  `shopping-experience-web` or `upselling`. The endpoints below give the value where one was seen.
- **Bot protection:** Cloudflare sits in front of every website. A client it takes for a bot gets an
  HTML page (usually with status 403), a `cf-mitigated: challenge` header, or Cloudflare error 1010
  or 1020. That answer comes from Cloudflare, not the API: an HTML 401 or 403 says nothing about the
  credentials. Some markets (the United Kingdom) block on the TLS and HTTP/2 fingerprint of the
  connection before reading any header, so browser-like headers alone don't get through there.

### Responses

- **Collections** usually come as `{count, total, take, skip, items: [...]}`. Some endpoints use
  `data`, `weeks` or a bare list instead; each endpoint says which. The app also accepts `result`
  (app).
- **Weeks** are ISO weeks written `YYYY-Www`, such as `2026-W25`. HelloFresh calls a delivery week
  an `hfWeek`.
- **Timestamps** mostly carry an offset without a colon: `2026-07-20T12:00:00-0700`. A scheduled
  `deliveryDate` is noon local time and a `cutoffDate` is 23:59:59 local time. Carrier timestamps
  are in UTC (`+0000` or `Z`).
- **Errors** from the API are an HTTP status with a JSON body. Known bodies are given with each
  endpoint.
- **Money** comes in different units on different endpoints:

| Unit | Where |
| --- | --- |
| Major units (`76.93`) | orders; `/gw/calculate`; cart price totals; `unitPrice` in a cart price request |
| Minor units (`6594` for 65.94) | a product's `price`, `specialFee` and `shippingPrice`; Market `priceCatalog` and `quantityOptions`; a meal's `charge` amounts; `deliveryOption.priceInCents`; cart price `charges[].amount`; the credit balance |
| Hundredths | a wallet `budget.value`: minor units when `fixed`, hundredths of a percent when `percent` (`1500` is 15%) |
| `{currencyCode, units, nanos}` | a meal's `itemPrice.pricePerUnit`; `nanos` are billionths, so `{"units": 17, "nanos": 980000000}` is 17.98 |

### Identifiers

| Identifier | Looks like | Where it comes from |
| --- | --- | --- |
| Subscription id | `1234567` | `id` in the subscription list; `subscriptionId` on a week |
| Customer id | `2000002` | `customer.id` on a subscription; `id` from `/info` |
| Customer UUID | a UUID | `customer.uuid` on a subscription; `uuid` from `/info`. The payment endpoints use it |
| Customer plan id | a UUID | `customerPlanId` on a subscription. The plan endpoints and the menu use it |
| Week | `2026-W25` | `id` on a week |
| Product handle (SKU) | `US-CBU-3-2-0` | a box; see [Product handles](#product-handles) |
| Delivery option handle | `US-1-0800-2000` | country, weekday (`1` is Monday), and the window's start and end |
| Recipe id | 24 hexadecimal characters | `recipe.id` |
| Course index | `68` | a meal's `index` on one week's menu: what the cart selects |
| Market item index | `10089` | a Market item's `index` |
| Bookmark id | `<recipe id>-<locale>` | a cookbook row's `bookmark_id` |
| Tracking public id | a UUID | the end of a week's `tracking.tracking_link` |

### Product handles

A box's handle encodes its plan: `<COUNTRY>-<FAMILY>-<MEALS>-<PEOPLE>-<N>`. `US-CBU-3-2-0` is a
Classic box (family `classic-box-unified`, prefix `CBU`) with 3 meals for 2 people. The website's own
code names the parts `country`, `familyPrefix`, `meals`, `people` and `recurrency`.

- A week can have a box of its own (the week's `product.handle`), different from the plan's.
- A box's meals are priced under a charge handle, `<COUNTRY>-CHARGE-0-0-0`.
- Market groups have handles of the same shape, such as `US-APR-0-0-0` (proteins) and
  `US-AAB-0-0-0` (appetizers).

## Authentication

The website signs in with an email address and password. It gets a short-lived access token (a JWT)
and a long-lived refresh token, and renews the access token with the refresh token. Its client id is
`senf`.

| Step | Request |
| --- | --- |
| Optional | `POST /gw/auth/token?client_id=senf&grant_type=client_credentials` |
| Sign in | `POST /gw/login` |
| Renew | `POST /gw/refresh` |

### Anonymous token

`POST /gw/auth/token?client_id=senf&grant_type=client_credentials`, with no body, returns a token
that isn't tied to an account. The website fetches it when a page loads. Signing in doesn't need it:
`/gw/login` works without it and without an `Authorization` header. Recipe search accepts it.

### Check an email

`POST /gw/auth/email/status`, without authorization:

```json
{"email": "someone@example.com", "country": "US"}
```

It returns `{"registered": true}`, or `false` when that market has no account with the email. The
sign-in page asks before signing in.

### Sign in

`POST /gw/login?country=<CC>&locale=<locale>`, without authorization:

```json
{"username": "someone@example.com", "password": "<password>"}
```

The website sends `Content-Type: application/json` and `Accept: application/json, text/plain, */*`.
It returns an [auth object](#auth-object).

| Answer | Meaning |
| --- | --- |
| JSON 401 or 403 | Wrong email or password |
| 401 `{"error": "access_denied_password_must_be_rotated"}` | The password has expired. HelloFresh has emailed instructions for changing it |
| 429 | Signing in is locked for a few minutes after too many attempts. Each new attempt can extend the lock (app) |
| HTML 401 or 403 | Cloudflare, not the API; see [Requests](#requests) |

### Renew a token

`POST /gw/refresh?country=<CC>&locale=<locale>`:

```json
{"refresh_token": "<refresh token>"}
```

The website sends this body as `Content-Type: text/plain;charset=UTF-8`. It returns a new
[auth object](#auth-object).

- Refresh tokens can be used once. The new auth object carries a new refresh token, and the old one
  stops working, so two renewals sent with the same refresh token leave one of them with a dead
  token.
- 400, 401 and 403 mean the refresh token is dead; the app signs the customer out on all three (app).
- 408, 429 and 5xx are temporary.

### Auth object

`/gw/login` and `/gw/refresh` both return:

```json
{
  "access_token": "<jwt>",
  "expires_in": 1800,
  "refresh_token": "<refresh token>",
  "refresh_expires_in": 5184000,
  "token_type": "Bearer"
}
```

- The access token lasts 30 minutes and the refresh token 60 days (`expires_in` and
  `refresh_expires_in`, in seconds). The app treats 1800 as the shortest valid lifetime (app).
- The app's model also has `issued_at` (Unix time), `scope`, `user_data` (`{email}`), and
  `redirect_url` and `public_id` for signing in by emailed link (app).
- `token_type` is the authorization scheme: `Authorization: Bearer <access token>`.
- The website keeps this object, as URL-encoded JSON, in its `apiV2Auth` cookie.

### Expired tokens

- A 401 means the access token was rejected: renew it and send the request again.
- The app renews on demand, within 60 seconds of expiry before a request or after a 401, and never
  after a 403 (app).
- A 403 with a valid token means the account isn't allowed to make that call.

## Account

### Subscriptions

`GET /gw/api/customers/me/subscriptions`, with no query, returns `{count, total, take, skip, items}`,
one item per subscription. Fields:

| Field | Meaning |
| --- | --- |
| `id` | Subscription id |
| `customerPlanId` | The plan's UUID, for the plan endpoints and the menu |
| `isActive`, `pausedAt`, `canceledAt` | The subscription's state. There is no status field: it is cancelled when `canceledAt` is set, else paused when `pausedAt` is set, else active or inactive by `isActive` |
| `endlessPausedAt` | A date that stays set on active accounts too; it doesn't show the state |
| `customer` | `{id, uuid, locale}` |
| `product.sku` | The plan's box handle |
| `maintainedSku` | Another box handle; its meaning isn't confirmed |
| `productType` | `{productName, specs: {meals, size}, family: {handle}}`: meals per week, number of people, and the product family (`classic-box-unified`) |
| `preset` | The meal preference preset (`quick`); see [Meal presets](#meal-presets) |
| `couponCode` | The active coupon, or null |
| `deliveryTime` | The delivery option handle of the regular delivery day |
| `deliveryInterval` | Weeks between deliveries (`1`) |
| `nextDeliveryOption.deliveryName` | The next delivery's day and window (`Mondays: 8AM - 8PM`) |
| `shippingAddress` | `{address1, city, postcode, region: {code, name}}`. The app's model also has the address `id`, `region.id` and `country: {id, iso2Code, iso3Code, name}` (app) |
| `nextDelivery`, `nextDeliveryWeek` | The next delivery's date and week |
| `nextModifiableDeliveryDate`, `nextModifiableDeliveryWeek` | The next delivery the customer can still change |
| `nextCutoffDate` | A meal deadline |
| `weekWithLatestMenu` | The last week with a published menu (`2026-W33`); later weeks have no menu yet (app) |
| `isBlocked` | The subscription is blocked (app) |

### Customer

`GET /gw/api/customers/me/info?country=<CC>&locale=<locale>`, header
`x-requested-by: client-platform`, returns one object: `id`, `uuid`, `email`, `firstName`,
`lastName`, `birthday`, `locale`, `createdAt`, `boxesReceived` (boxes delivered so far),
`subscriptionIds`, `activeSubscriptionId`, `activeSubscriptionSkus`, `customerPlanIds`,
`socialLogin`, `experiments`, `freeAddOnSubscriptions`, `freebiesNotSentCount` and `salesRuleCode`.

The customer attributes service has the same count, as `boxes_received`:
`GET /gw/customer-attributes-service/attributes?customer_id=<customer id>&country=<CC>&locale=<locale>`
(app).

### Plans

`GET /gw/api/plans?includeCanceled=false` returns a bare list of the customer's plans, each with
`planItems[].productHandle`, `legacyContractPrice`, `planType` and a status.

### Box sizes

`GET /gw/api/subscriptions/{subscription id}/product_options?country=<CC>&locale=<locale>` lists
the boxes the subscription can switch to. It returns `{count, items}`, one item per product family
(`{handle, name, prefix, products}`). A product:

```json
{
  "handle": "US-CBU-3-2-0",
  "productName": "Classic - 3 meals per week for 2 people",
  "specs": {"meals": 3, "size": 2, "recurrency": 0},
  "price": 6594,
  "shippingPrice": 1299,
  "isActive": true,
  "isSubscribable": true
}
```

- `price` is the whole box, in minor units. `specs.size` is the number of people.
- Products also have `oneOffPrice`, `recurringPrice`, `specialFee`, `mealSwap`, `presetIsEnabled`,
  `isPublic`, `isAvailableForExtraMeals`, `sort`, `family` (the whole family record),
  `weekWithLatestMenu`, `mealSwapDefaultChoice`, `swappableMeals`, `legacyHandles`, `legacyIds`,
  `country`, `id`, `createdAt` and `updatedAt`.
- The list differs between accounts and over time: one account was offered 2 to 6 meals a week at
  one time and 1 to 12 at another. It isn't sorted by size.
- The app adds `all=1` and the header `page_context: one-off-product-options` (app).

### Change the box size

`PATCH /gw/api/plans/{customerPlanId}?country=<CC>&locale=<locale>`:

```json
{"productHandle": "US-CBU-4-2-0"}
```

It returns `204 No Content` and changes every future box and its price. The subscription's
`product.sku` shows the new box on the next read.

### Change the delivery day

`PATCH /gw/api/subscriptions/{subscription id}?country=<cc>&locale=<locale>`, with `country` in
lower case here:

```json
{"subscription": {"id": "1234567", "deliveryTime": "US-2-0800-2000"}}
```

- It changes every future delivery. The handle comes from [delivery options](#delivery-options).
- It returns 200 with the whole subscription, but that copy still has the old `deliveryTime`. A read
  a second later has the new one.
- The app changes the day with `POST /gw/api/plans/{customerPlanId}/changePlanDeliveryDetails` and
  `{"deliveryOption": "<handle>"}`, and has no subscription PATCH (app).

### Delivery options

`GET /gw/api/delivery_dates_options` lists the days and windows a plan can be delivered:

| Parameter | Value |
| --- | --- |
| `country`, `locale` | |
| `family` | The plan's `productType.family.handle` |
| `zip` | The delivery postcode |
| `numDeliveries` | A number, such as `7`; its effect isn't confirmed |
| `customerPriority`, `customerJourney` | `active_subscription` and `account_setting` for the regular day. The app sends `active_oneoff` and `one_off_change` to move one week (app) |

It returns `{items: [{deliveryOptions: [...]}]}`. An option:

```json
{
  "handle": "US-1-0800-2000",
  "deliveryDay": 1,
  "deliveryName": "Mondays: 8AM - 8PM",
  "deliveryFrom": "08:00",
  "deliveryTo": "20:00",
  "priceInCents": 0,
  "isDefault": true
}
```

The same option can appear under several items.

### Meal presets

`GET /gw/api/presets?country=<cc>&locale=<locale>&sort=-weight` (with `country` in lower case) and
`GET /gw/menus-service/presets?country=<CC>&locale=<locale>&take=150` return the same list,
`{items: [{handle, name, description, weight, maxDefault}]}`: the names behind a plan's `preset`
(`quick` is Quick & Easy). The app uses the highest-`weight` preset for a subscription without one
(app).

## Deliveries

### Weeks

`GET /gw/api/customers/me/deliveries?rangeStart=<YYYY-Www>&rangeEnd=<YYYY-Www>` returns the
customer's weeks in the range, past and future, as `{count, total, take, skip, items}`. A range of a
year works. A week:

| Field | Meaning |
| --- | --- |
| `id` | The week (`2026-W25`) |
| `subscriptionId` | The subscription it belongs to |
| `deliveryDate` | The scheduled delivery, noon local time |
| `holidayDelivery`, `holidayMessage`, `isHolidayShiftVisible` | The date a box moved for a holiday arrives instead, HelloFresh's notice about it, and whether to show it |
| `cutoffDate` | The deadline for changing the week |
| `status`, `state`, `subStatus` | Where the week stands; see below |
| `deliveryBlocked` | HelloFresh blocked delivery this week (the area, a carrier problem, a holiday). The app offers no unskip on a blocked week (app) |
| `allowedActions` | `{mealSwap, pause, oneOffChange, updateDeliveryWeekday, updateDeliveryAddress, updatePaymentMethod, donate}`: what the customer may do this week |
| `availableOneOffOptions` | `[{handle, deliveryDate}]`: other days this week's box can come |
| `product` | This week's box: `{handle, displayName, specs: {meals, size}, price, specialFee, shippingPrice}` |
| `deliveryOption` | `{handle, deliveryName, deliveryDay, deliveryFrom, deliveryTo, type, price, priceInCents}`: the day and window |
| `tracking` | Shipment tracking, or null; see below |
| `actionable`, `prepaid`, `orderId` | Meanings not confirmed (`orderId` was null) |

The weeks list no meals. They come from the [week menu](#week-menu) and, for delivered weeks,
[past deliveries](#past-deliveries).

The values the app knows (app):

- `status`: `RUNNING`, `PAUSED`, `DELIVERED`, `PAYMENT_FAILED`, `CANCELLED`.
- `state`, the box's progress: `RUNNING`, `PREPARING`, `ON_THE_WAY`, `DELIVERED`, `FAILED`, `PAUSED`,
  `CANCELLED`.
- `subStatus`: `COOK_IT`, `DELAYED`, `EARLY`, `RATING`. A delayed or early box's new date is
  `tracking.delivery_date`.

Notes:

- A skipped week is `status: "PAUSED"`. There is no `skipped` field.
- `status` can lag: a box being prepared was seen with `status: "DELIVERED"` and
  `state: "PREPARING"`. `state` follows the box.
- Writes send the week's own `deliveryDate`, not `holidayDelivery` (app).

A `tracking` node:

```json
{
  "tracking_id": "",
  "tracking_link": "https://www.hellofresh.com/delivery-tracking/<uuid>",
  "tracking_code": "1Z0000000000000001",
  "tracking_link_type": "hf",
  "estimated_delivery_time": "2026-06-23T14:37:34+0000",
  "delivery_date": "2026-06-23T14:37:34+0000"
}
```

- `tracking` is null on most weeks (350 of 409 in one account), delivered ones included. It is
  filled around delivery day.
- Once `state` is `DELIVERED`, `delivery_date` is when the carrier delivered the box. Before that it
  holds a placeholder.
- `estimated_delivery_time` matched `delivery_date` in every sample (69). The app shows it as the
  arrival time of a box on its way (app).
- `tracking_id` is always empty. `tracking_code` is the carrier's tracking number. The carrier's
  name comes only from [shipment tracking](#shipment-tracking).
- No endpoint gives the carrier's delivery window. The only window is the week's
  `deliveryOption.deliveryFrom` to `deliveryTo`.

Two more routes read weeks:

- `GET /gw/api/subscriptions/{subscription id}/delivery_dates/{week}` returns one week, with the same
  `tracking` node. The app reads the week again this way after every change (app).
- `GET /gw/api/subscriptions/{subscription id}/deliveries` returns one subscription's weeks, all of
  them or those between `rangeStart` and `rangeEnd` given as dates (app).

### Skip and unskip a week

`PATCH /gw/api/subscriptions/{subscription id}/delivery_dates/{week}?country=<CC>&locale=<locale>`:

```json
{
  "delivery": {
    "cutoffDate": "2026-07-15T23:59:59-0700",
    "deliveryDate": "2026-07-20T12:00:00-0700",
    "status": "PAUSED",
    "subscriptionId": "1234567",
    "id": "2026-W29"
  }
}
```

- `status` is `PAUSED` to skip and `RUNNING` to unskip. Both answer `201`.
- The dates are the week's own, as the weeks list gives them. The app sends only `id`, `status` and
  `deliveryDate` (app).
- The response is `{count: 1, items: [week]}`, but that week is incomplete: every `allowedActions`
  flag is false or null and `cutoffDate` is null. Read the week again instead.
- The app offers skip only when `allowedActions.pause` is true, the deadline hasn't passed and the
  week isn't blocked. It offers unskip only on a `PAUSED` week that isn't blocked and allows meal
  swaps. After its deadline, a paused week comes back by [moving it](#move-one-delivery) instead
  (app).

### Move one delivery

`POST /gw/api/subscriptions/{subscription id}/oneoff?country=<CC>&locale=<locale>`:

```json
{"id": "1234567", "delivery_option": "US-2-0800-2000", "week": "2026-W39", "source": "reschedule-delivery-feature"}
```

- It moves one week's box to another day; the regular day stays. `delivery_option` is a handle from
  the week's `availableOneOffOptions`.
- The week's `allowedActions.oneOffChange` says whether it is allowed.
- It answers `201` with the same kind of incomplete week as skipping, here also without `status` and
  `deliveryDate`.
- The app sends `week` and `delivery_option` as query parameters, with no body (app).

### Past deliveries

`GET /gw/my-deliveries/past-deliveries` returns the meals and Market items that were delivered:

| Parameter | Value |
| --- | --- |
| `country`, `locale` | |
| `subscription` | Subscription id |
| `rating-scale` | `5` |
| `from` | The previous page's `nextWeek`; leave it out for the first page |

```json
{
  "weeks": [
    {"week": "2026-W25", "menuId": "<id>", "meals": [], "addons": []}
  ],
  "nextWeek": "2026-W21"
}
```

- A page holds about four delivered weeks. Skipped weeks are left out, so `nextWeek` jumps over them.
- Weeks have no date, only the week.
- A meal has its recipe's fields on itself (`id`, `name`, `headline`, `image`, `category`,
  `cuisines`, `tags`, `label`, `nutrition`, `prepTime`, `totalTime`, `websiteURL`, `videoLink`), with
  an empty `recipe: {}` beside them.
- `addons` (in lower case) lists the Market items the box had:
  `{id, shoppableProductId, name, headline, image, category, tags, nutrition}`. They have no `index`,
  price or quantity. The menu's `addOns` (capital O) is something else: the catalog of what could be
  ordered.

### Recent deliveries

`GET /gw/customer-complaints/users/me/deliveries`, with no query, returns `{data: [...]}`: the last
two or so delivered weeks, for the complaint form. Their recipes have a bare `image_path`
(`/image/<file>`), and so does each ingredient.

### Shipment tracking

`GET /gw/scm/tracking-ids/track/public-id/{public id}?country=<CC>&locale=<locale>`, header
`x-requested-by: shipping-and-tracking`. The public id is the UUID at the end of a week's
`tracking.tracking_link`. It returns `{boxes: [...]}`; a box:

```json
{
  "external_id": "H0000000001",
  "tracking_id": "",
  "carrier": "VEHO",
  "tracking_code": "HF00000000000002",
  "delivery_date": "2026-08-17T12:00:00Z",
  "lane": "NJ_VEHO-SOMPA",
  "public_url": "https://track.shipveho.com/#/trackingId/HF00000000000002",
  "carrier_tracking_url": "https://track.shipveho.com/#/trackingId/HF00000000000002",
  "hf_tracking_url": "https://www.hellofresh.com/delivery-tracking/<uuid>",
  "internal_status": "delivered",
  "status_detail": "delivered",
  "last_status": {
    "status": "delivered",
    "datetime": "2026-08-17T22:53:06Z",
    "est_delivery_time": "2026-08-17T00:00:00Z"
  },
  "statuses": [],
  "proof_of_delivery_photo_urls": null,
  "signed_by": ""
}
```

- `carrier` is the only carrier name anywhere in the API. Only `VEHO` has been seen.
- `last_status` is the current step and `statuses` all of them, newest first. Each has `status`,
  `datetime`, `est_delivery_time` and `message`.
- `status` goes `pre_transit`, `in_transit`, `out_for_delivery`, `delivered`. `status_detail` (or
  `internal_status_detail`) is finer: `label_created`, `received_at_origin_facility`,
  `out_for_delivery`, `delivered`. A carrier can repeat a step.
- `message` is an untranslated key such as `package.droppedOffAtVeho`, not text. Since September 2026
  Veho boxes put these keys in `status` too: `package.delivered` (delivered),
  `package.pickedUpFromVeho` (out for delivery) and `package.droppedOffAtVeho` (in transit, arrived
  at the hub). Such a step can appear twice with one timestamp, once with the key and once with the
  plain status.
- `est_delivery_time` is a date: midnight UTC on the expected day. `delivery_date` is the scheduled
  noon.
- `proof_of_delivery_photo_urls` and `signed_by` were empty on every box seen, delivered ones
  included.
- The website's tracking page shows one step per `internal_status`: `pre_transit` (Preparing box),
  `transit` (Shipped), `out_for_delivery`, `delayed`, `delivered`, `delivery_attempted`, and
  `voided` or `failure` (Unsuccessful).

### Live tracking

In the Netherlands and Germany, where HelloFresh delivers boxes itself, its tracking pages
(`hftrack.nl`, `status.hellofresh.de`) read a Google Cloud Function that needs no account:

`GET https://europe-west1-hellofresh-prod.cloudfunctions.net/c_hf_getTraceyData?token=<token>&screenWidth=500`,
with `Origin` and `Referer` set to the tracking site.

- `token` is the last path segment of the week's tracking link: an opaque code on `hftrack.nl`, and
  the tracking number on `status.hellofresh.de/<number>`.
- It returns `traceyPhase`, `amountOfStopsBefore`, `estimatedTimeOfArrival`, `deliveryTime`,
  `driverName`, `personalCustomerMessage`, `driverLocation: {latitude, longitude}` and
  `customerLocation: {latitude, longitude}`.
- Phases include `AT_DEPOT`, `DRIVER_DEPARTED`, `ON_THE_WAY`, `DELAYED`, `DELIVERED`,
  `DELIVERED_HOME` and `CANCELLED`, and `INVALID_LINK` once a link has expired.
- German `tracking_code`s end in `_internal-A`, which the tracking page's number leaves out.

## Menus

### Week menu

`GET /gw/my-deliveries/menu` returns one week's menu for one subscription, with the customer's
choices:

| Parameter | Value |
| --- | --- |
| `subscription` | Subscription id |
| `customerPlanId` | |
| `week` | `2026-W25` |
| `product-sku` | The week's box (`product.handle`), else the plan's |
| `servings` | The number of people: the week's `product.specs.size`, else the plan's |
| `delivery-option` | The week's `deliveryOption.handle`, else the plan's |
| `postcode` | |
| `preference` | The meal preference, such as `quick`; see [Meal preference](#meal-preference) |
| `locale` | |
| `exclude` | empty |
| `exclude-feedback` | `true` |
| `include-filters` | `true` |
| `include-future-feedback` | `false` |

There is no `country` parameter.

- A week with a box of its own needs that box as `product-sku`. With the plan's box, the menu shows
  the plan box's default picks.
- A week after the subscription's `weekWithLatestMenu` answers `404 {"status": "NOT_FOUND"}`: its
  menu isn't published yet. The app doesn't ask for those weeks (app).
- For a past week the answer can be another week's menu, so check its `week`. For an old week it can
  be a combined menu of many weeks, with over 1,000 dishes.
- Responses run up to about 1.2 MB.

Top-level fields: `id`, `week`, `meals`, `modularity`, `addOns`, `categories`, `filters`, `sorting`,
`menuCollections`, `mealsReady`, and sometimes `mealsPreselected`.

- `mealsPreselected`: HelloFresh picked this week's meals from the food profile. It stays true until
  the customer changes them.
- `mealsReady` was true in every response seen. The app shows a recipe's cooking steps only when it
  is true or the recipe has steps, so false probably means the steps aren't published yet (app).

#### Meals

`meals` has one entry per meal on the menu, dish options included:

| Field | Meaning |
| --- | --- |
| `index` | The course index: what the cart selects |
| `recipe` | The recipe; see below |
| `selection` | `{quantity, limit}`. `quantity` above 0 means the meal is in the box, at that many portions. `limit` is the most portions the box takes (0 for no limit) |
| `charge` | On premium meals: `{label, unitAmount, totalAmount, reason, strategy}`, such as `+7.99/serving` and `799`. `reason` is `premium` on premium meals; the app also knows `addon`, `dinner_to_lunch` and `add_on_modularity`, and its model has `baseUnitAmount`, `baseTotalAmount` and `servings` (app) |
| `itemPrice` | `{group, pricePerUnit}`: the price of one portion, with `group` `premium` or `classic` |
| `relatedCategory` | For extras listed among the meals: `appetizers`, `desserts`, ... |
| `isSoldOut` | Sold out for this customer (app) |

A recipe has `id`, `name`, `headline`, `image` (a full URL), `category`, `tags`, `label`,
`nutrition`, `prepTime`, `totalTime` and `feedback`, and a few have `videoLink`.

- `category` is the main protein: `Beef`, `Poultry`, `Pork`, `Seafood` or `Lamb`. Meatless meals
  have none.
- `prepTime` and `totalTime` are ISO 8601 durations, and their names are swapped: `prepTime`
  (`PT35M`) is the time the website shows, and `totalTime` (`PT5M`) is the smaller one. The app
  swaps them too (app).
- `label` is the badge: `{text, handle, displayLabel, foregroundColor, backgroundColor}`, such as
  `Premium Picks`, `BESTSELLER`, `20 Min or Less` or `GLP-1 Support` (handle `glp-1-friendly`). A
  recipe has at most one, and most have one.
- `feedback` is either `{productDeliveryCount, lastDeliveryWeek}` (how often the customer has had
  the meal and when last) or `{rating, ratingScale}` (their rating), never both.
- `tags` are `{name, type, displayLabel, preferences}`.
- `videoLink` is a promotional video, `.mp4` or `.mov`.

#### Dish options

`modularity` lists each dish's options, such as a doubled protein or a swap:

```json
{
  "defaultCourseIndex": 1,
  "noVariationsDefaultTitle": "No Change",
  "variationsHeadline": "",
  "variations": [
    {
      "index": 161,
      "title": "2x Chicken Cutlets",
      "category": "Poultry",
      "quantity": 1,
      "ingredient": {"name": "Chicken Cutlets", "imageURL": "<url>", "amount": 2, "unit": "unit"}
    }
  ],
  "addOns": [{"index": 10003, "title": "Garlic Bread"}]
}
```

- `defaultCourseIndex` is the dish's own course index, and `variations` are its options, in the order
  the website lists them. Choosing an option means putting its `index` in the cart in place of the
  dish's. An option can have another name and protein, such as a salmon swap on a chicken dish.
- `noVariationsDefaultTitle` names the dish unchanged: `No Change`, `No Protein`, or the base protein
  (`Ground Beef`).
- `addOns` are Market items suggested beside the dish. Their indexes are Market items, not meals.
- Every meal, options included, has an entry of its own, usually without variations. In one US week,
  81 of 86 dishes had options (1 to 13 each), for 463 meals in all.
- The ingredient's `imageURL` is on the retired image host; see [Images](#images).
- The website shows one tile per dish. Under a filter it shows the dish when it matches, else its
  first matching option.

#### Market catalog

`addOns` is the week's Market catalog:

```json
{
  "selectionLimit": 100,
  "groups": [
    {
      "groupType": "protein",
      "sku": "US-APR-0-0-0",
      "selectionLimit": 8,
      "addOns": [
        {
          "index": 10089,
          "sku": "US-APR-0-0-0",
          "isLocked": false,
          "isSoldOut": false,
          "maxQuantity": 6,
          "recipe": {"id": "<recipe id>", "name": "Steelhead Trout", "image": "<url>", "nutrition": {}},
          "priceCatalog": {"basePrice": 899, "pricePerQuantity": []},
          "quantityOptions": [{"quantity": 1, "totalAmount": 899}],
          "selection": {"skipped": false, "oneOffQuantity": 1, "preselectedQuantity": 0, "courses": []}
        }
      ]
    }
  ]
}
```

- `groupType` is `appetizer`, `breakfast`, `dessert`, `lunch`, `protein`, `sides`, `goodchop`,
  `petstable`, `donation`, `lowprices` or `modularity`. A `modularity` group holds pairing extras,
  linked to a meal through `selection.courses: [{index}]` (app). None has been seen in a US menu.
- `selection` is null unless the item is chosen. `preselectedQuantity` is what comes every week, and
  `oneOffQuantity` is this week's quantity when the customer changed it. This week's quantity is
  `oneOffQuantity` when it is above 0 or `skipped` is true, else `preselectedQuantity`; the two don't
  add up (app).
- `maxQuantity` limits an item. The app has no such field: it uses the largest `quantityOptions`
  quantity, and the group's and the catalog's `selectionLimit` (app).
- `basePrice` is one unit's full price. The app shows `pricePerQuantity[].price`, with `basePrice`
  struck through when `displayStrikethrough` is set (app).
- An item without a `sku` of its own is ordered under its group's (app).

#### Menu sections

`categories` holds the website's menu sections:
`{mainCategory: "dinners", categories: [{name, slug, imagePath, items: [{id}], subcategories, related, applicablePromiseType}]}`.

- Sections seen: This Week's Menu (`dinners`), Health Conscious Menu (`nutrition-menu`), Quick & Easy
  Menu (`extra-convenient`), Double Protein, Family Menu, New, Bestsellers, Specialties, Featured,
  Your Top Recipes, Order It Again, and HelloFresh Market (`market`, whose items are Market items).
- `items` are recipe ids. A section such as Featured lists only `subcategories`, which have the same
  shape.
- `applicablePromiseType` is `PROMISE_TYPE_SUBSCRIPTION`, `PROMISE_TYPE_PREMIUM_SURCHARGE` or
  `PROMISE_TYPE_ADDON_BASKET`.
- The `dinners` section lists the dishes in the website's order, with the meals in the box first.

`menuCollections` (`[{handle, name, description, authors}]`) is editorial text for some sections.

#### Filter panel

`filters` defines the website's filter panel: `[{name, type, choice, options: [{name, type, default}]}]`.

- `type` is the slug the [menu filter](#menu-filter) takes, for the group and for each option.
- `choice` is how options combine: `MULTI-AND` for Dietary preference and Ingredients to avoid,
  `MULTI-OR` for Main protein, Cuisine type and Dish type, and `SINGLE` for Total cooking time.
  `sorting` is a single choice too.
- Some names differ from their slugs: Carb Conscious is `carb-smart`, GLP-1 Support
  `glp-1-friendly`, Fiber Powered `fiber-smart` and Sodium Smart `low-sodium`.

### Menu filter

`GET /gw/my-deliveries/courses?country=<CC>&locale=<locale>&week=<YYYY-Www>` filters a week's menu
on the server. Each filter group is a parameter, with several slugs joined by commas
(`dish-type=family-style,salad-and-bowls`):

| Group | Slugs seen |
| --- | --- |
| `diet` | `carb-smart`, `fiber-smart`, `glp-1-friendly`, `gluten-free`, `high-protein`, `low-sodium`, `low-sugar`, `organic-protein`, `under-650-calories`, `vegetarian` |
| `total-cooking-time` | `cooking-time-15`, `cooking-time-20`, `cooking-time-30` |
| `main-protein` | `beef`, `fish-seafood`, `pork`, `poultry`, `vegetarian` |
| `dish-type`, `cuisine`, `exclude-allergens`, `sort-by` | as the menu's `filters` give them |

It returns ids only: `{courses: [{index, parent, recipeFamily, recipeId}]}`.

- The answer covers the week's whole catalog, including other products' meals, ready-to-eat meals
  (`rte`) and Market families (`menu-addons-*`). Match it to the menu by `recipeId`; then `index`
  equals the menu's.
- `parent` is the dish's `defaultCourseIndex` for an option, and null otherwise.
- It can place a meal the menu leaves without a protein: a tofu swap on a beef dish comes back under
  `vegetarian`.

### Regional menus

`GET /gw/menus-service/menus?country=<CC>&locale=<locale>&weeks=<YYYY-Www,...>&exclude=` returns the
market's menus, not the customer's: `{count, total, take, skip, items}`. Each item is a week
(`week`, and an internal `id`) with its meals under `courses`, each wrapping a `recipe`.

- Each week comes in about 16 versions, for the products and presets.
- Courses have `isSoldOut` and `isHidden`, which the customer's menu lacks. They lack `itemPrice`,
  `feedback` and `relatedCategory`.
- One week is 3 to 4 MB.
- Recipes have a bare `imagePath` and a dead `imageLink`.

### Meal preference

`GET /gw/v1/profile/me/unified-preferences`, with no query:

```json
{
  "unifiedPreferences": {
    "plans": {"<customerPlanId>": {"planPreference": "quick"}},
    "cuisines": {"italian": 100, "thai": -100},
    "primaryProteins": {"beef": 100, "plant_based_proteins": -100}
  }
}
```

- `planPreference` is what the website sends as the menu's `preference`. It can differ from the
  subscription's `preset`.
- The [food profile](#food-profile) has it too: `taste.plans[<customerPlanId>].planPreference`, or
  `taste.legacySinglePreference`.
- The app never calls this endpoint. It sends the subscription's `preset`, or the highest-weight
  preset (app).

### Public menu page

`GET /menus` is the public menu, as an HTML page with no account data.

## Cart

### Save a week's box

`PUT /gw/v1/carts/{week}`, header `x-requested-by: shopping-experience-web`, saves a week's meals
and Market items:

| Parameter | Value |
| --- | --- |
| `subscription` | Subscription id |
| `customer` | Customer id |
| `week` | The week |
| `product-sku` | The week's box, with its meal count set to the new selection; see below |
| `preference` | The meal preference. The app always sends it empty (app) |
| `cutoff_time` | The week's deadline |
| `ignore_addons` | `false` |
| `update_quantity` | `true` |

The app sends only `subscription`, `product-sku`, `customer`, `preference` and `cutoff_time` (app).

```json
{
  "meals": [
    {"index": 32, "quantity": 1},
    {"index": 75, "quantity": 2}
  ],
  "extras": [
    {
      "groupType": "protein",
      "sku": "US-APR-0-0-0",
      "selection": [
        {"index": 10089, "oneOffQuantity": 2, "preselectedQuantity": 0, "courses": []}
      ]
    }
  ]
}
```

It returns `{"hasSeamlessDowngraded": false}`.

- The body sets the week's whole selection: meals left out are removed, and so are Market items,
  except those that come every week (below).
- `meals[].quantity` is the number of portions of that meal.
- The box resizes with the selection. The meal count in `product-sku` must equal the number of
  meals, or the request fails with
  `400 MEAL_SIZE_MISMATCH: Product 'US-CBU-3-2-0' requires 3 meals but 4 meals were selected`. On a
  3-meal plan, 2 meals go out as `US-CBU-2-2-0` and 4 meals as `US-CBU-4-2-0`, and the box is priced
  to match. A meal at quantity 2 counts as two meals (app).
- 2 meals is the smallest box. The largest depends on the plan; see [Box sizes](#box-sizes).
- `extras` are grouped by `groupType` and `sku`. `oneOffQuantity` is the quantity wanted this week,
  and `preselectedQuantity` stays the item's weekly quantity. To drop an item that comes every week
  for this week only, send it with `oneOffQuantity: 0` and its `preselectedQuantity`; leaving it out
  doesn't cancel it (app). Send a pairing extra's `courses` back as they were.
- `hasSeamlessDowngraded: true` means HelloFresh saved the cart but shrank the box to fit, so the
  saved selection is smaller than the one sent.

### Resize one week

`PUT /gw/carts/{week}/size?subscription=<subscription id>` with
`{"productHandle": "<sku>", "hfMarketCode": "<CC>"}` changes one week's box, after pricing it with
`/gw/calculate` (app). It can change that week's number of people, which the cart write can't.

## Prices

### Price a week's box

`POST /gw/v1/carts/{week}/price?isFutureWeek=<true|false>`:

```json
{
  "boxSize": 2,
  "isFirstOrder": false,
  "customerID": 2000002,
  "isRecurring": true,
  "subscriptionID": 1234567,
  "planID": "<customerPlanId>",
  "products": [
    {"handle": "US-CBU-3-2-0", "deliveryOption": "US-1-0800-2000", "hfWeek": "2026-W25", "unitPrice": 65.94},
    {
      "handle": "US-CHARGE-0-0-0",
      "boxSku": "US-CBU-3-2-0",
      "hfWeek": "2026-W25",
      "quantityPerCourse": [{"index": 68, "quantity": 2}, {"index": 71, "quantity": 1}],
      "recipeIndexes": ["68", "68", "71"]
    },
    {
      "handle": "US-APR-0-0-0",
      "boxSku": "US-APR-0-0-0",
      "hfWeek": "2026-W25",
      "quantityPerCourse": [{"index": 10089, "quantity": 1}],
      "recipeIndexes": ["10089"]
    }
  ],
  "shippingAddress": {"address1": "1 Example Street", "postcode": "02101", "region": "MA"},
  "locale": "en-US",
  "country": "US"
}
```

- The first product is the box: the week's `product.handle` and `deliveryOption.handle`. `boxSize`
  is the number of people.
- The meals go under `<CC>-CHARGE-0-0-0`, with `boxSku` set to the box. `quantityPerCourse` lists
  each meal once with its quantity, and `recipeIndexes` lists each meal once per portion (app).
- Each chosen Market item is a product of its own, under its SKU (app).
- The app sends only the address's postcode, and no `planID` or `locale` (app).

It returns `grandTotal`, `subTotal`, `shippingAmount` and `discountAmount` in major units, and
`products`. A product's `charges` (`{reason, entity_id, entity_type, strategy, amount}`, `amount` in
minor units) are its surcharges.

A [weekly discount](#weekly-discounts) doesn't show here: for a week it covers, `discountAmount` is
0 and `benefitInfos` is empty.

### Price the plan

`POST /gw/calculate`, with no query, prices the plan's regular box, or one week's box:

```json
{
  "isFirstOrder": false,
  "products": [{"handle": "US-CBU-3-2-0", "deliveryOption": "US-1-0800-2000"}],
  "skipOneOffCalculation": true,
  "isRecurring": true,
  "subscriptionID": 1234567,
  "customerID": 2000002,
  "shippingAddress": {"postcode": "02101"},
  "planID": "<customerPlanId>",
  "couponCode": null,
  "locale": "en-US",
  "country": "US"
}
```

- It returns, in major units, `grandTotal` (shipping included), `subTotal`, `shippingAmount`,
  `discountAmount`, `taxAmount`, `currency`, and `products: [{handle, unitPrice, shippingAmount, currency}]`.
- The response also has `couponCode`, `customerID`, `customerUUID`, `subscriptionID`, `country`,
  `isRecurring`, `shippingAddress`, `minimumValue`, `premiumShippingAmount`,
  `standardShippingAmount`, `standardShippingDiscountAmount`, `shippingDiscountAmount`,
  `shippingTaxAmount`, `remainingDiscountAmount`, `useRemainingDiscountAsCredit` and `Recalculate`.
- `couponCode` is the subscription's active coupon. The app sends it too (app).
- It prices a box, not its meals: premium meals' surcharges aren't included.

## Billing

### Orders

`GET /gw/api/customers/me/orders?country=<CC>&locale=<locale>&limit=200` returns the customer's
charges as `{count, items}`. An order:

```json
{
  "orderNr": "30000000002",
  "createdAt": "2026-06-04T00:13:06-0700",
  "grandTotal": 76.93,
  "shippingAmount": 0,
  "couponCode": "RX-0000",
  "orderLines": [
    {
      "deliveryDate": "2026-06-15T00:00:00-0700",
      "deliveryTime": "US-1-0800-2000",
      "subscription": {"id": "1234567"},
      "couponMoneyValue": 10
    }
  ]
}
```

- A delivery can have several orders, such as the box, Market items and fees. Together they are
  what the delivery cost.
- `createdAt` is when the order was charged, several days before delivery.
- `couponMoneyValue` is a line's discount, in major units, such as 10 for a $10 voucher on the
  premium meals' line.
- Order lines also have `paymentStatus` (`paid`, `failed`, `chargeback` or `canceled`) and
  `shipped`. The app counts only `paid` lines as a successful order (app).
- Responses can approach 1 MB.

### Credit balance

`GET /gw/payments/customers/{customer UUID}/balance?business_unit=<CC>&country=<CC>`:

```json
{"amount": 0, "cash": 0, "bonus": 0, "currencyCode": "USD", "restrictedAmount": 0, "cancellableCredits": 0}
```

- Amounts are in minor units. `amount` is the credit that applies to the next order.
- `cash` is HelloFresh Cash the customer bought, and `bonus` is bonus credit (app).
- The app sends `country` and `locale` rather than `business_unit`. Its model has no
  `cancellableCredits` and makes `restrictedAmount` optional (app).
- Credit applies by itself, except for accounts in HelloFresh's manual credit experiment; see
  [Credit application](#credit-application).

### Credit history

`GET /gw/payments/balance/transactions?customerUUID=<customer UUID>&types=DEBIT` returns a bare list
of credit transactions, not card payments: `{transactionType, id, referenceId, cash, bonus, currencyCode, issuer, createdAt}`.

- `transactionType` is `CREDIT` (credit added), `DEBIT` (credit spent on an order), `REVERSE_DEBIT`
  or `REVERSE_CREDIT`.
- On a `DEBIT`, the order number follows the `:` in `referenceId`.
- The app sends only `customerUUID` (app).

### Card on file

`POST /gw/payments/v1/checktokenstatus?country=<CC>`, with an empty body and the header
`x-requested-by: gateway`:

```json
{
  "isTokenExpiring": false,
  "isTokenExpired": false,
  "primaryToken": {
    "type": "credit_card",
    "provider": "Braintree",
    "method": "visa",
    "is_active": true,
    "is_verified": true,
    "details": {"expiry_month": "05", "expiry_year": "2029", "number": "4242"},
    "billing_address": {"city": "<city>", "postcode": "<postcode>", "full_address": "<address>"}
  }
}
```

- `details.number` is only the card's last four digits.
- `method` is the brand, as an Adyen-style code: `visa`, `mc` (Mastercard), `amex`, `maestro`,
  `bcmc` (Bancontact), `paypal`, `sepa`, `i_deal`, `klarna`, `apple_pay`, ... (app).
- The app treats the card as needing attention when either flag is true (app).
- `GET /gw/payments/us/subscription/{subscription id}` and `GET /gw/payments/us/customers` (header
  `x-requested-by: activations-rte`) return the same `primaryToken` without the flags.

### Weekly discounts

The website asks HelloFresh's wallet which discounts apply to the upcoming weeks:
`POST /gw/customer-wallet/v2/benefit-distribution?externalPartnerBenefits=true&subscription=<subscription id>`,
header `x-requested-by: upselling`.

```json
{
  "deliveries": [
    {
      "delivery": {"hfWeek": "2026-W38", "state": "RUNNING", "cutoffDateTime": "2026-09-09T23:59:59-0700"},
      "selections": {"mealboxHandle": "US-CBU-3-2-0"},
      "subscriptionId": 1234567
    }
  ]
}
```

It returns each week's units and the discounts ("promises") they come from:

```json
{
  "deliveries": [
    {"hfWeek": "2026-W38", "units": [{"promiseId": "<id>", "status": "available", "box": null, "alternatives": []}]},
    {"hfWeek": "2026-W39", "units": []}
  ],
  "promises": [
    {
      "id": "<id>",
      "voucherCode": "RX-0000",
      "source": "unspecified",
      "type": "premiumSurcharge",
      "unlimited": true,
      "expirationDateTime": "2026-09-09T23:59:59-0700",
      "attachmentDateTime": "2026-08-09T13:34:00Z",
      "campaign": {"type": "default"},
      "storeSlug": null,
      "units": [
        {
          "box": null,
          "used": null,
          "benefits": [{"id": "<id>", "applicableTo": "premiumSurcharge", "budget": {"type": "fixed", "value": 1000}}]
        }
      ]
    }
  ]
}
```

- A promise is a discount such as "$10 off premium meals". `budget.value` is in hundredths: 1000 is
  $10 when `fixed`, and 1500 is 15% when `percent` (app). Only fixed discounts on premium meals have
  been seen.
- `applicableTo` is `premiumSurcharge`, `mealbox`, `shipping`, `freeAddons` or `externalProduct`
  (app).
- Each of a promise's `units` is one box, with its own budget: a promise for three boxes has three. A
  week's unit names which `box` it is, 1 when null (app).
- `unlimited: true` means one per box on every box until it expires.
- A week's unit `status` is `applied` (chosen for that week), `available` or `inactive` (app).
  Customers choose with `POST /gw/customer-wallet/v1/selection` and `{promiseId}`.
- A promise shows on every upcoming week until its `expirationDateTime`.
- The discount isn't in the cart price or the subscription's `couponCode`. The bill shows it as a
  coupon: the order's `couponCode` and the line's `couponMoneyValue`.
- Rewards from the loyalty program arrive as promises with `source` `loyalty` or `loyalty_2` (app).

Two related calls:

- `POST /gw/customer-wallet/v2/delivery-benefits` (header `x-requested-by` `shopping-experience-web`
  or `rewards`) answers the same for one week. Its body is
  `{delivery, planId, legacyVoucherCode, legacyVoucherShippedDiscountedBoxes, selections, systemCountry, locale}`.
- `GET /gw/customer-wallet/v1/standalone` holds vouchers not tied to a delivery. It answered
  `{"promises": [], "useLegacyExperience": false}` every time. The app calls
  `/gw/customer-wallet/v2/standalone?customerPlanID=&subscription=` (app).

### Credit application

The website's payment page sends these by itself when it opens:

- `PUT /gw/payments/customers/{customer UUID}/credit-application/settings?country=<CC>&locale=<locale>`,
  header `x-requested-by: upselling`, empty body, returns `{"group": "variation", "optedInWeek": null}`.
  `group` is an experiment group (it was empty earlier, `variation` later), and `optedInWeek` the
  week the customer chose to apply credit to.
- `PUT /gw/payments/customers/{customer UUID}/credit-application/delivery-week/{week}?country=<CC>&locale=<locale>`,
  header `x-requested-by: rewards`, with `{"enabled": true}`, returns an empty 200. It opts a week in
  to applying credit; the page sent it without a click.

In the app's manual credit experiment (`CART_MANUAL_CREDIT_APPLICATION`), credit applies only to the
week opted in (app).

## Recipes

### Recipe

`GET /gw/recipes/recipes/{recipe id}?country=<CC>&locale=<locale>` returns a whole recipe:

| Field | Meaning |
| --- | --- |
| `id`, `name`, `headline`, `description`, `slug`, `websiteUrl` | |
| `imagePath` | The photo's bare path. `imageLink` is on a dead host; see [Images](#images) |
| `prepTime`, `totalTime` | ISO 8601 durations |
| `difficulty` | A number |
| `averageRating`, `ratingsCount`, `favoritesCount` | |
| `category` | `{name}` |
| `cuisines`, `tags`, `utensils`, `allergens` | Lists of objects with a `name` |
| `ingredients` | `[{id, name, imagePath, imageLink, allergens, shipped}]` |
| `yields` | `[{yields: 2, ingredients: [{id, amount, unit}]}]`: the amounts for each number of servings |
| `steps` | See below |
| `nutrition` | `[{type, name, amount, unit}]`, per serving |
| `cardLink` | The printable recipe card, a PDF |
| `videoLink` | A promotional video, on some recipes |
| `promotion`, `uniqueRecipeCode` | A partner's blurb, and a code |

- Ingredient amounts depend on the servings: each `yields` entry lists the ingredients' ids with
  that number of servings' `amount` and `unit`. The website starts with the smallest.
- A `unit` is spelled out with its abbreviation: `tablespoon (tbsp)`, `gram (g)`.
- `shipped: false` marks a pantry staple the customer supplies, such as salt, oil or butter. The
  field has only been seen on US recipes.
- An ingredient's `allergens` are ids of entries in the recipe's `allergens`
  (`{id, name, iconPath, tracesOf, triggersTracesOf}`).
- `nutrition`'s `name` is translated ("Energie" in German, for both energy rows), so calories are the
  row whose `unit` is `kcal`, as the app reads them (app). `type` is a fixed id.
- `tags` with `displayLabel: true` (such as Protein Smart) are the ones the website shows; the rest
  are internal groupings.
- The app's model can also have the customer's `userFavorite` and `userRating` (app).

A step has `index`, `instructions`, `instructionsHTML`, `instructionsMarkdown`, `images`, `timers`,
`ingredients`, `utensils` and `videos`:

- `instructions` is plain text, one paragraph per sub-step, padded with blank lines.
- `instructionsHTML` is a `<ul><li><p>` list with ingredient names in `<strong>` and the quantities
  for four servings in a colored `<span>`. `instructionsMarkdown` holds the same HTML.
- `images` holds one photo: `{path, link, caption}`. The path is `/<recipe id>/step-<hash>.jpg`,
  `link` is on the dead host, and the caption is short ("Cook Rice").
- A timer is `{name, duration, temperature, temperatureUnit}`. `duration` is ISO 8601 with zero-padded
  minutes (`PT05M`), `name` equals the step's caption, and both temperature fields were null in every
  sample.
- `ingredients`, `utensils` and `videos` were empty in every sample.

### Recipe search

`GET /gw/recipes/recipes/search?country=<CC>&locale=<locale>&q=<text>&take=<n>` searches the whole
public catalog. It returns `{items, count, skip, take, total}`, each item shaped like a
[recipe](#recipe), and an empty list when nothing matches. It works with the
[anonymous token](#anonymous-token).

### Recipe catalog

The browsing catalog, about 10,000 recipes, comes from the website's own page data, not from `/gw/`:

| Purpose | Request |
| --- | --- |
| Categories and top rated | `GET /_next/data/<build id>/recipes.json` |
| One category | `GET /_next/data/<build id>/recipes/<path>.json?main-collection=<slug>` |

- The build id changes with every website release. The page HTML has it (`"buildId":"..."`), and a
  stale one answers 404.
- These URLs have also answered 404 for every build id while the pages worked. The pages themselves
  (`GET /recipes`, `GET /recipes/<path>`) carry the same data in `<script id="__NEXT_DATA__">`,
  wrapped in `props`.
- They need no account. The website sends `x-nextjs-data: 1` with these requests.
- In the data, `pageProps.ssrPayload.collection` is the category: `children.docs` are its
  subcategories (`{id, slug, name, thumbnail, breadcrumbs}`) and `recipes` its recipe list.
- `pageProps.ssrPayload.dehydratedState.queries` holds more lists, each with its recipes in
  `state.data.pages` (a plain list, despite the name). Queries named `foodContentHubRecipe.*` whose
  `where` filter has `recipeCollectionTags.id` belong to the category. The one without that filter is
  a top-rated list that every page carries. `bestRatedRecipes` is the same list on the top page.
- A recipe row:
  `{id, recipeId, name, headline, slug, imagePath, websiteUrl, aggregateRating, aggregateRatingsCount, prepTime}`.
- A subcategory can't be fetched by its own slug: `/recipes/ramen-noodles` redirects away. Its path
  is in its last breadcrumb's `url` (`/recipes/noodle-recipes/ramen-noodles`).

### Images

Recipe images come from `img.hellofresh.com`:

```text
https://img.hellofresh.com/<transform>/hellofresh_s3/<path>
```

- `<path>` is the bare path a payload gives, such as `/image/<file>.jpg`. The `hellofresh_s3`
  segment is required; without it the server answers 404.
- `<transform>` sets the size. With `f_auto,fl_lossy,q_auto,w_640` a recipe photo is about 73 KB;
  untransformed it is about 1.7 MB.
- Full URLs on `d3hvwccx09j84u.cloudfront.net` (`imageLink`, `iconLink`, an ingredient's `imageURL`)
  answer 502. The same file loads from `img.hellofresh.com` with the URL's path, and the app rewrites
  them that way (app). An option ingredient's image is at `/ingredient/<file>`.
- `media.hellofresh.com/<transform>/hellofresh_s3/<path>` serves the same files.

| Endpoint | Recipe photo |
| --- | --- |
| `/gw/my-deliveries/menu` | `image`, a full URL on `img.hellofresh.com` |
| `/gw/my-deliveries/past-deliveries` | `image`, a full URL |
| `/gw/menus-service/menus` | `imagePath`, a bare path |
| `/gw/customer-complaints/users/me/deliveries` | `image_path`, a bare path |
| `/gw/recipes/recipes/{id}` | `imagePath`, a bare path, as are each step's `path` and each ingredient's `imagePath` |
| `/gw/cookbook/v1/external-recipes` | `thumbnail_url`, a full URL on `media.hellofresh.com` |
| Recipe catalog rows | `imagePath`, a bare path |

### Cookbook

The customer's saved recipes. Recipes are saved under `internal-recipes` but listed and removed under
`external-recipes`. All four requests take `country` and `locale`.

| Purpose | Request |
| --- | --- |
| Which of these are saved? | `POST /gw/cookbook/v1/internal-recipes/search` |
| Save a recipe | `POST /gw/cookbook/v1/internal-recipes` |
| List | `GET /gw/cookbook/v1/external-recipes` |
| Remove | `DELETE /gw/cookbook/v1/external-recipes/{row id}` |

- **Search:** `{"bookmark_ids": ["<recipe id>-<locale>", ...]}` returns
  `{"recipes": [{id, bookmark_id}]}`, the saved ones only. The website sends about 40 ids at a time.
- **Save:** `{"bookmark_id": "<recipe id>-<locale>", "bookmark_source": "hellofresh"}` returns `201`
  with the new row.
- **List:** `limit` and `cursor` return `{data: [rows], pagination: {total_count, has_more, next_cursor}}`.
  Pages go by cursor: send `next_cursor` back as `cursor`. There is no offset. The website's cookbook
  page shows only 3 rows, but the list has them all.
- **Remove:** by the row's `id`, not its `bookmark_id` or the recipe id, which answer 404. It returns
  `204`.
- A row: `{id, bookmark_id, title, headline, description, thumbnail_url, url, prep_time, total_time, nutrition, created_at}`,
  with ISO 8601 durations. Recipe ids never contain a hyphen, so the recipe id is the part of
  `bookmark_id` before the first one.
- The cookbook also stores recipes imported from other websites and social media. The app imports
  them with `/gw/cookbook/v1/external-recipes/upload-url` and
  `/gw/cookbook/v2/recipes/import/stream` (app). Those rows have no `bookmark_id`.

### Favorites

A second store, separate from the cookbook and not kept in step with it, backs the website's
`/recipes/favorites` page and the app's heart.

- `GET /gw/cfs/v2/favorites/recipe?country=<CC>&locale=<locale>&ids=` returns
  `{items: [{object_id: <recipe id>}], total, count}`. The website sends `ids` empty to list
  everything. The app joins several ids with `|` and pages with `take` and `skip` (app).
- The app sets a heart with `PUT /gw/cfs/v2/favorites/recipe/{recipe id}` and clears it with
  `DELETE` on the same path, both without a body (app).
- Ratings live beside them: `GET` and `PUT /gw/cfs/{v2|v3}/ratings/recipe/{recipe id}`, with
  `{rating: "N", comment, origin, hellofresh_week, menu_id}`. A 404 means the recipe isn't rated
  (app).

## Food profile

The preferences HelloFresh picks meals by: tastes, household and goals. All four requests take
`brand=BRAND_HELLOFRESH`, `exclusion=v2` and `regionCode=<CC>`.

| Purpose | Request |
| --- | --- |
| The profile | `GET /gw/profile-service/v2/customers/me/profile` |
| Every option | `GET /gw/profile-service/v2/profile/options` |
| Change it | `PATCH /gw/profile-service/v2/customers/me/profile`, with `source=food-profile` too |
| How complete it is | `GET /gw/profile-service/v2/customers/me/profile/completion` |

- The profile is `{taste, household, goals}`. Likes and dislikes are maps from an option's slug to
  `100` or `-100` (`cuisines`, `flavors`, `dishTypes`, `primaryProteins`). Other fields hold option
  ids. `taste` also has `plans` (`{<customerPlanId>: {planPreference}}`) and
  `legacySinglePreference`.
- The options list maps those ids to names: `{taste, household, goals, _meta}`. `_meta` says which
  fields allow a "none" choice and which proteins go with which diet.
- The PATCH takes only the sections that change, and returns the saved profile.
- Completion is `{overall: {completed, total}, <tier>: {fields: [{field, completed}]}}`, with the
  tiers by priority (`p0` first).

## Other endpoints

Seen on the website, or in the app where marked, and not described above. The website calls about 95
different `/gw` paths in all.

| Endpoint | What it is |
| --- | --- |
| `GET /gw/configurations` | The website's configuration. It has feature flags (`features.loyaltyProgram.enabled`) and two loyalty tier lists that disagree: `loyalty.levels` (Apprentice at 3 boxes, Sous Chef at 10, Master Chef at 20) and `features.loyaltyBadges` (newbie 0, freshie 2, foodie 5, junior-cook 10, head-cook 25, master-cook 50) |
| `GET /gw/loyalty/v3/programs/opt-in` | Whether the customer can join the rewards program: `opt_in_status` is `not_eligible`, `not_opted_in`, `opted_in` or `opt_in_closed`, with progress toward the next milestone. The app sends `sku`, the plan's box handle (app) |
| `GET /gw/loyalty/enrollments`, `GET /gw/loyalty/onboarding` | The 12-week challenge and the program's intro flags. The app adds `?subscription=&state=in-progress,completed,incomplete`; a 403 means the challenge isn't offered in the country (app) |
| `GET`/`POST /gw/loyalty/v3/programs/enrollment` | Progress once enrolled; the POST, with `{customer_plan_id, sku}`, joins (app) |
| `GET /gw/loyalty/v3/rewards/overview`, `GET /gw/loyalty/v3/rewards/ledger`, `POST /gw/loyalty/v3/rewards/redeem` | The points program (app) |
| `/gw/loyalty/v3/programs/missions/...`, `/gw/loyalty/v3/programs/cart/preview` | Missions and a cart preview (app) |
| `PATCH /gw/api/addresses/{address id}` | Change the delivery address. The full address object (over 20 fields, with numeric `country` and `region` codes) has only been seen in this request's response |
| `PATCH /gw/api/customers/{customer id}` | Name, email, birthday |
| `POST /gw/payments/us/change` | Payment method and billing address |
| `GET /gw/payments-status/us/subscription/{subscription id}/payment-change-status` | `{"status": "ok"}` after a payment change |
| `GET /gw/payments/v1/getpaynoworders` | Orders whose payment failed. It answered `404 Orders not found` every time |
| `GET /gw/benefit-pass/v1/status?customer_id=&plan_id=` | The paid benefit pass: `{cooling_off_eligible, is_eligible, subscription_status, trial_eligible}`. `/gw/benefit-pass/v1/pricing` answered 403 |
| `POST /gw/sps/subscriber/{customer UUID}/subscribe`, `.../unsubscribe` | Marketing preferences per channel (`sms`, `email`, `direct-mail`, `outbound-call`, `push-notification`, `social-media`), header `x-requested-by: commstech`. The response has every channel's topics |
| `GET /gw/customer-complaints/users/me/eligibility` | `{is_eligible, is_logistics_eligible}` for the complaint form |
| `GET /gw/helloshare/v2/credit-achievement?uuid=` | Referral credit: `{pending_credit, achieved_credit, resendable_invites}` (app) |
| `/gw/consumer-planner/v1/meal-plan/entries`, `.../schedule`, `/gw/consumer-planner/v1/grocery/lists` | HelloFresh's own meal planner and grocery list, behind feature flags (app) |

## What isn't known

- Whether `/gw/refresh` answers a used or expired refresh token with `400 {"error": "invalid_grant"}`,
  as the app's handling suggests.
- Whether the website counts a meal at quantity 2 as two meals in the cart's `product-sku`, as the
  app does. Every captured cart write had quantities of 1.
- Whether `oneOffQuantity` is this week's whole quantity on the website, and whether leaving out an
  item that comes every week cancels it for that week. The captured writes only had items with no
  weekly quantity.
- The name of the group in a meal's `itemPrice`: `group`, or `priceGroup` as earlier notes said.
- Whether a menu meal's `charge` ever has a `handle` or `boxSku`.
- Whether the website accepts `PUT /gw/carts/{week}/size`, an empty cart `preference`, and `/oneoff`
  with query parameters, as the app sends them.
- Whether the week menu has `meals[].isSoldOut` or `feedback.favorite`, and whether a recipe has
  `userFavorite` and `userRating`, for a signed-in customer.
- Whether the regional menus' `isSoldOut` follows the customer's own availability.
- Whether `changePlanDeliveryDetails` accepts a `deliveryInterval`.
- What the `x-requested-by` and feature headers change, if anything.
- The address object's full shape outside the address PATCH, and whether the subscription's
  `shippingAddress` has every field that PATCH needs.
- What a filled `proof_of_delivery_photo_urls` or `signed_by` looks like, and which carriers other
  than Veho appear.
- What `getpaynoworders` returns when a payment has failed.
- What the loyalty rewards and `consumer-planner` endpoints return.
- Whether Belgium and Luxembourg have live tracking like the Netherlands and Germany.
- What `maintainedSku` means, which delivery `nextCutoffDate` belongs to, and what a week's
  `actionable`, `prepaid` and `orderId` mean.
