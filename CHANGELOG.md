# Changelog

Notable changes for each tagged release. Versions correspond to git tags and to the
`version` field in `custom_components/hellofresh/manifest.json`. Add entries under
**Unreleased** as part of each change; the release workflow rotates that section into a
version heading and publishes it as the release's Highlights.

## 2.93 — 2026-09-04
- Meal planner and market cards: the **+ Add** pill and the ± steppers are pinned to the
  bottom edge of each tile, so the selection controls line up across a row instead of
  landing at different heights under meals with longer descriptions or more chips.

## 2.92 — 2026-09-03
- **Dashboards load faster**: the card files are now served with browser caching enabled.
  The `?v=` version stamp already guarantees freshness (every upgrade registers a new
  URL), but caching was disabled, so browsers re-downloaded every card's JS on every
  dashboard load.
- **Prep-list check-offs survive a Home Assistant restart** (RestoreEntity), and now
  reliably travel with a week when a box lands and the weeks shift up — the tick set is
  shared between the two list entities instead of stranded per entity.
- One malformed week entry in a HelloFresh payload no longer fails the entire refresh:
  the bad week is dropped with a warning and every other week still lands.
- Recipe sheet: rapidly switching serving sizes can no longer show a stale response's
  amounts (per-request sequence guard).
- Sold-out overlay: reads the availability flags straight from the raw menus-service
  payload instead of running the full week normalizer per poll — same matching, a
  fraction of the work.
- Hardening: every dynamic id interpolated into an API path is percent-encoded, and the
  bearer token is no longer sent to HelloFresh's public website pages (catalog browse,
  build-id fetch) that never needed it.
- Housekeeping: platforms declare `PARALLEL_UPDATES`, the coordinator moved to HA's
  `entry.runtime_data`, and the cookbook-favorites and cart-pricing areas moved out of
  `client.py` into their own modules (no behavior change).
- **Privacy (logs)**: log lines and error messages no longer carry raw account
  identifiers or raw API response bodies. Request paths in errors and INFO logs now use
  the same `{id}` templating diagnostics already applied (subscription/plan/customer
  ids), and auth/login/write failures log a parsed error summary (`error=invalid_grant;
  error_description=…`) instead of a raw body slice — a rejected login body can echo the
  submitted email, and logs (unlike diagnostics exports) are not redacted before users
  attach them to GitHub issues. A full audit of every log call found no tokens,
  passwords, addresses, or emails logged anywhere; fingerprint-only token logging holds.
- Docs: corrected the `logo` card option (it works on every card, meal planner
  included), noted keyboard accessibility in the card reference, listed all six options
  in QUALITY_SCALE.md, mentioned both shared JS modules in the YAML-resources note, and
  added the `?v=` cache-bust to the example dashboard's YAML-mode instructions.
- Meal planner card: the recipe sheet now completes the "read it, then decide" flow — on
  editable weeks it carries a pinned footer with the same **+ Add** / **− N +** servings
  controls as the tiles, so you can add or resize a meal right from its ingredients and
  instructions. The footer drives the same pending selection as the grid (Save/Cancel as
  usual) and disappears on weeks you can't edit. Recipes and Market sheets are unchanged.
- **Privacy**: the cart-pricing debug trace's recorded request payloads are now redacted
  in diagnostics exports (`customerID`/`subscriptionID`/`planID` used a spelling missing
  from the redaction list), and the `api_base_url` diagnostic sensor no longer dumps the
  full serialized subscriptions (delivery address, payment descriptor, coupon code) into
  its recorder-stored attributes — it carries a capabilities summary and a subscription
  count instead.
- Meal planner, Market and Recipes cards: recipe tiles are now **keyboard-accessible** —
  tab to a tile and press Enter or Space to open its recipe (with a visible focus
  outline). Since the tile tap is the only way to open a recipe, it has to work without
  a pointer. The Schedule card's timeline rows (which drive the planner/market week sync)
  got the same treatment; every other control was already a real button.
- Subscription card: now registers in the dashboard card picker (`window.customCards`) —
  it was the only one of the seven cards missing, so "Add card" never offered it.
- Market card: fixed an item's price vanishing from its tile the moment its quantity was
  changed, when the item relies on the order's fallback currency (the in-place re-render
  dropped the fallback the full render passes).
- Meal planner card: skipping or unskipping a week now stays on that week after the
  resync instead of jumping back to the current week (saves already behaved this way).
- Meal planner card: **tapping a meal now opens its full recipe on every week** —
  ingredients, instructions, nutrition — matching the Market card. On editable weeks,
  selection moved off the tile onto an explicit **+ Add** pill (unselected meals) and the
  existing **− N +** servings stepper (**−** at one serving removes the meal), so reading
  a recipe can never accidentally change your box. The ⓘ button is gone — the whole tile
  is the recipe now.
- README: documented the **Show data-quality repair warnings** option (it was missing
  from the Options list) and aligned the Market card blurb with its renamed
  **Categories** filter bar.
- Docs: `docs/cards.md` is now **`docs/dashboard.md`** — it covers the dashboard's
  views, not just the packaged cards, including a new **Missing Ingredients view**
  section (the two prep-list to-do cards) with a screenshot. Also added a screenshot of
  the meal planner's full recipe view (ingredients and cooking instructions). All
  in-repo links updated; update any bookmarks pointing at the old path.
- Renamed the **Last delivery date** sensor's display name to **Last delivery day** (all
  languages): the value is the day your last box was *scheduled* for (HelloFresh's own
  `deliveryDate`), not the carrier's actual arrival — that's **Tracked shipment date**.
  Display-name only: the entity ID (`sensor.last_delivery_date`), dashboards, automations
  and recorded history are untouched. The entities doc now spells out the difference.
- Meal planner card: tiles now show the **total cooking time** after the calories
  (`53g protein · 780 kcal · 35 min`) — the same headline number the HelloFresh site
  shows on its tiles, and the number the Total Cooking Time filter matches.
- Meal planner card: dietary tile chips (Under 650 Calories, GLP-1 Support, …) are now
  styled as outlined pills — a deliberate second tier — so a solid-colored chip always
  means HelloFresh's own badge in its own colors (Premium Picks, Night Market Flavors,
  20 Min or Less, …) rather than looking like an inconsistency.
- Meal planner card: the **Highlights** chips (New / Bestsellers / Cooked Before) are now
  **single-select** like Total Cooking Time — they are mutually exclusive views of the
  menu (a meal can't be new *and* cooked before), so combining them only ever confused.
  Picking one replaces the other; tapping the active chip (or All) clears. A previously
  stored multi-selection keeps its first choice.
- **Fixed the meal planner's Total Cooking Time filter matching nothing.** Two stacked
  bugs: the weekly-menu payload sends times as ISO-8601 durations (`prepTime: "PT35M"`),
  which the week normalizer's integer coercion silently dropped — every menu recipe had
  no time data at all — and HelloFresh's field names are swapped (`prepTime` is the
  headline time the site shows; `totalTime` is the smaller hands-on number), so the
  filter now matches the headline time, exactly as the website's own filter does
  (HAR-verified by result counts). Times parse int-first then ISO, so payloads using
  plain minutes keep working.

## 2.91 — 2026-09-02
- Meal planner card: the filter bar is now a collapsible panel. Six chip groups flowing
  into one wrapped row had become confusing, so collapsed (the default) it shows a single
  "Filters · N active" row with each active selection as a removable ✕ chip, and expanded
  it lays out every group on its own aligned line (label column, wrapping chips). The
  expand/collapse state persists like the filters themselves.

## 2.90 — 2026-09-02
- Recipes card: the search field now searches the **whole ~10k-recipe catalog** across
  every category at once (previously it only filtered the loaded view). Backed by a newly
  verified recipes-service endpoint (`GET /gw/recipes/recipes/search`) exposed through
  `hellofresh.get_catalog_recipes`' new `search` field; debounced as you type, with
  stale-response guarding, and immune to the website build-id fragility of the browse
  catalog. The Cookbook view still filters your saved list locally.
- Meal planner card: filter bar aligned with the HelloFresh website's own filter panel —
  groups renamed to Categories (was Menu), Main Protein, Dietary Preference, and a new
  Total Cooking Time group (Under 15/20/30 Minutes, single-select as on the site) split
  out of Dietary. Dietary options now mirror the site's list exactly: Vegetarian and
  Organic Protein added, High Fiber renamed to Fiber Powered, Mediterranean removed
  (it is a cuisine on the site, not a dietary preference).
- Market card: renamed the filter bar's group label from "Section" to "Categories".

## 2.89 — 2026-09-02
- Meal planner card: two new filter groups — Highlights (New / Bestsellers / Cooked
  Before, combining as a union like the site's browse rows) and Menu (the website's own
  sections — This Week's Menu, Health Conscious, Family Menu, Your Top Recipes, … —
  single-select, from the newly parsed `categories` block in each week's menu payload).
- Meal planner card: tile dietary chips now share the filter bar's alias table, fixing
  GLP-1 chips silently vanishing after HelloFresh renamed the tag; menu badges
  (BESTSELLER, NEW, …) now render in HelloFresh's own per-badge colors (hex-gated in the
  integration and re-checked in the card).
- Market card: the internal "modularity" add-on shelf (dips, sides, dessert cups) is now
  labeled "Extras" instead of leaking its internal slug as a heading and filter chip.
- Meal planner card: added dietary filter chips matching HelloFresh's own menu categories
  — GLP-1 Support, Carb Conscious, High Protein, Under 650 Calories, Gluten-Free
  Friendly, Sodium Smart, Low Added Sugar, High Fiber, Mediterranean, and Under
  15/20/30 Minutes. Matched client-side against recipe tags under every HAR-observed
  spelling (GLP-1 alone ships as three different tags), with numeric fallbacks for the
  calorie/time categories; selected chips combine as constraints (AND, matching the menu
  payload's own MULTI-AND declaration), and chosen meals always stay visible.
- Docs: adjudicated the menu payload's `categories` block (the source of the website's
  new sectioned menu), the `filters` block (authoritative filter slugs and MULTI-AND/OR
  semantics), and the expanded `recipe.label` badge shape in HELLOFRESH_API.md.
- Market card: added a section filter bar (All / Appetizers / Breakfast / Desserts /
  Lunch / …) mirroring the meal planner's protein filter — chips built from the viewed
  week's own catalog sections, persisted across reloads, with cart items always visible.
- Recipes card: added a search field under the header that filters the loaded view
  (category, Cookbook, or Top rated) as you type, matching recipe names and headlines.
  Client-side only — HelloFresh exposes no catalog text-search endpoint.

## 2.88 — 2026-08-27
- Fixed the prep-list test suite failing from 2026-08-25 onward (16 CI failures, no
  runtime impact): the todo fixtures used fixed delivery-date literals that fell behind
  `_all_covered_weeks`' `delivery_date >= today` filter once the anchor date passed.
  Fixture dates are now anchored to the day the suite runs, the same convention
  `tests/test_entities.py` already used.

## 2.87 — 2026-08-24
- **Fixed `sensor.tracked_shipment_estimate` showing the wrong day.** The carrier reports its
  estimate as midnight *UTC* of the estimated day (`est_delivery_time: 2026-08-24T00:00:00Z`),
  but the entity was registered as a `TIMESTAMP`, so Home Assistant rendered it in the viewer's
  zone — a US-Eastern user saw **"Aug 23 @ 8:00 PM"** for a box the carrier estimated for
  **Aug 24**: the wrong day, and a time of day the carrier never promised. It is now a `DATE`
  entity reporting the estimated day, with the calendar day read in UTC (converting to local
  first would just relocate the same off-by-one). The result is now identical in every
  timezone — verified from Auckland to Honolulu — because HA serializes a `DATE` state as a
  bare ISO day with no viewer-side conversion. HelloFresh's own date fields were never
  affected: they use a scheduled **noon** anchor, which survives ±12h of shifting.
- Verified against a live out-for-delivery capture: `sensor.shipment_tracking_status` parses
  `out_for_delivery` correctly and humanizes to "Out for delivery". A status that lags the
  website is the **poll interval**, not a parse bug — the box flipped status at 17:55 UTC and
  the default `refresh_interval_minutes` is 180. Lower it, or call `hellofresh.refresh_data`,
  to tighten delivery-day tracking.

## 2.86 — 2026-08-21
- **Fixed blocking I/O on the event loop during setup** (HACS review). `frontend.py` checked for
  the card file with a synchronous `Path.is_file()` inside a coroutine, which stalls Home
  Assistant's event loop and trips its synchronous-I/O detection. The check now runs via
  `hass.async_add_executor_job`. Covered by tests that fail if the blocking call returns.

## 2.85 — 2026-08-21
- **Renamed the prep-list entities** to say which week they are for: **Prep List (current week)**
  and **Prep List (next week)**, replacing "Prep list" and "Prep list (following week)". Shown
  with the device name, these read as *HelloFresh (US) Prep List (current week)*. Translated in
  all six shipped locales. Entity IDs are pinned to a stable key and are **unchanged**
  (`todo.<prefix>_prep_list`, `todo.<prefix>_prep_list_week_2`), so dashboards and automations
  keep working.
- **Fixed the prep lists never combining quantities.** HelloFresh spells ingredient units as
  *name plus a parenthetical abbreviation* — `"tablespoon (tbsp)"`, `"teaspoon (tsp)"` — but the
  unit table matched only the bare `"tablespoon"` or `"tbsp"`. Every real unit therefore looked
  unrecognized, and no conversion ever ran: a list showed
  `Cooking Oil — 4 tablespoon (tbsp) + 3 teaspoon (tsp)` instead of
  `Cooking Oil — 5 tablespoon (tbsp)`. Units are now matched on either half of the compound
  spelling, so the compound, bare, and abbreviated forms are one unit. A side effect is that a
  unit whose abbreviation isn't in the table (`cup (c)`) still resolves via its name.
- The test fixtures in `tests/test_api.py` use the bare spelling, so they did not exercise the
  real format and every test passed while the feature was inert in production. The actual
  payload shape is now documented in
  [docs/HELLOFRESH_API.md](docs/HELLOFRESH_API.md#recipe-detail-gwrecipesrecipesid), with
  regression tests using the live spelling.

## 2.79 — 2026-08-21
- **Added Prep lists (`todo.prep_list`, `todo.prep_list_week_2`).** New to-do entities listing
  the pantry staples that HelloFresh does **not** ship — salt, oil, butter, eggs — for the
  selected meals of your next two deliveries, so they can be on hand before each box arrives
  instead of being discovered mid-recipe. Add them to **To-do list** cards.
- It ships as **two entities**, one per delivery week — `todo.prep_list` for the box on its way
  and `todo.prep_list_week_2` for the one after it — so the dashboard gives each week its own
  card and heading. This is deliberate rather than one combined list: HA's to-do card renders a
  single entity, so two weeks in one entity could only ever be a flat list. Two entities give
  two real sections, each with its own totals, deadline, and check-offs.
- Amounts are added up *within* a week but never across weeks: each box is its own shopping
  trip, and merging both weeks' butter into one line would lose which trip it belongs to.
  Skipped weeks ship nothing and are passed over.
- **Quantities convert between units of the same family** where the conversion is exact, so
  `4 tablespoon (tbsp) + 3 teaspoon (tsp)` reads as **5 tablespoon (tbsp)** rather than two
  lines. HelloFresh spells units as *name plus parenthetical abbreviation*, so both halves are
  recognized (`tbsp`, `tbsp.`, `Tablespoon`, `tablespoon (tbsp)` are all one unit), and a unit
  with no number counts as one of it (`teaspoon (tsp)` → `1 teaspoon (tsp)`), so it adds up
  instead of trailing behind as a bare word. Three guards keep this honest: conversion never
  crosses families (grams never become cups, millilitres never become teaspoons), the total is
  only combined when it lands on a fraction a cook can measure (`1 cup (c) + 1 teaspoon (tsp)`
  stays as two amounts instead of becoming an unmeasurable `1.02 cup`), and the result is always
  expressed in a unit the recipes actually used — never promoted into one that only exists in
  the conversion table. Unrecognized units still total up on their own, and amounts that aren't
  numbers (a range like `1-2`) are kept verbatim rather than guessed at.
- As a box arrives the weeks shift up — `prep_list` always means the next delivery. Check-offs
  are keyed to `(week, ingredient)` rather than to the slot, so a week carries everything
  already ticked with it when it becomes the current box.
- Each entity exposes `week_id` and `delivery_date` attributes, so a dashboard heading or
  automation can name the delivery a list belongs to.
- Each list is a *projection* of that week's selection, so items appear and vanish as meals are
  swapped; it advertises check-off only (no adding or deleting rows), since user-authored edits
  would fight every refresh. Every item is due on its own box's delivery date.
- The list refreshes on every coordinator poll. `CoordinatorEntity` sets `should_poll = False`
  and its `_handle_coordinator_update` is synchronous, so neither hook can await the per-recipe
  detail fetch; the callback is overridden to schedule the rebuild as a task instead.
- **Fixed a latent `shipped` bug this depends on.** `HelloFreshRecipeDetail` coerced a *missing*
  `shipped` key to `False`, making it indistinguishable from a genuine pantry staple. The field
  is now tri-state (`None` = unknown), and the prep list treats only an explicit `False` as
  "you supply this" — otherwise, in any region that omits the field, the entire box would have
  been listed as things to go out and buy.
- **Added a Reconfigure flow.** The integration's entry can now be corrected in place from
  Settings → Devices & Services → Configure without deleting and re-adding it. Credential
  entries re-collect email/password, token-only entries re-collect an `apiV2Auth` token, and
  either can also correct the **country** — previously fixed at setup, so a wrong choice meant
  starting over.
- Reconfigure refuses to repoint an entry at a *different* HelloFresh account (`account_mismatch`):
  the entities, history, and unique ID on disk belong to the original account, so a different one
  is a new integration entry rather than an edit to this one.
- All six shipped locales (de, fr, nl, da, no, sv) were translated alongside English.
- **Declared `"quality_scale": "custom"` in the manifest.** This is the accurate value for a
  custom integration: Home Assistant's loader reports every custom integration as `custom` at
  runtime regardless of the manifest, so a tiered claim would be silently ignored. `QUALITY_SCALE.md`
  now records that the Silver *rules* remain the engineering target even though the badge is
  reserved for integrations merged into core.

## 2.73 — 2026-08-20
- **Fixed past-week browsing in the Market card**, which showed only the last couple of weeks
  while My Menu correctly spanned the configured **Past delivery history (weeks)** window
  (default 26, selectable 1–104). The Market card now honors that same option.
- Root cause: a week's market items were only ever parsed from the browsable `addOns` catalog
  published on its menu, and HelloFresh only serves that menu for about the **Full menu history
  (weeks)** window (default 2). Past that point a week had no market data at all, and the card
  lists a past week only when it carries some, so older weeks silently vanished from the strip.
- The delivered-history endpoint separately reports the add-ons a shipped week actually came
  with, in a distinct lowercase `addons` field that the integration had never read. That field is
  now parsed into each past week's market items, so history reflects **what you ordered** instead
  of depending on a catalog that no longer exists. Verified against a live capture: the week of
  Jun 15 again shows *Pork & Shiitake Gyoza* and *Steelhead Trout*.
- Items keep their categories wherever category data still exists (within the Full menu history
  window). Beyond it, delivered history records what was bought but not which Market shelf it came
  from, so those weeks render as a single ordered list — no invented headings — matching how My
  Menu presents an older week.
- Past weeks are unaffected in My Menu: purchased add-ons are recognized and kept out of the meal
  list even when no catalog survives, so they can't appear as delivered meals.
- Current and future weeks are untouched — a live week keeps its full browsable catalog and stays
  editable.
- **Past-delivery pagination now scales with the configured history window.** The page cap was a
  fixed 20, which stopped roughly 80 weeks back, so a history setting near the 104-week maximum
  silently lost its oldest weeks. It is now derived from the option.
- **HelloFresh is now listed for all 16 supported countries in HACS.** `hacs.json` advertised only
  6 (AU, CA, DE, GB, NL, US), so users in Austria, Belgium, Switzerland, Denmark, France, Ireland,
  Luxembourg, Norway, New Zealand and Sweden could not discover the integration in the HACS store
  even though it works in their market — including every country whose translations shipped in
  2.70. The UK is published as `GB`, its ISO code, rather than the `uk` config key.
- **CI now checks the Lovelace cards.** The ~8k lines of card JavaScript had no automated checks
  at all, which is how the past-week browsing regression reached users: every job in CI was
  Python-only. Two new gates run on each push — a syntax check over all 9 cards, and behavioural
  tests for the week-selection logic that require the Market and My Menu cards to expose the
  **same** past weeks. That parity test fails against the old code, reproducing the reported
  symptom exactly.
- **Documentation** — the README's history option now uses its real UI label (**Past delivery
  history (weeks)**, previously written as "Weeks of past history to load", which could not be
  found in the options dialog); the Market card section documents how far back it browses, where
  past-week data comes from, and why older weeks show no categories or prices; CONTRIBUTING gains
  a "Running the CI checks locally" section; and the API reference documents the `addons` history
  field alongside a comparison table against the similarly-named `addOns` catalog.
- **README restructured for browsability** — it had grown to 581 lines, with the seven card
  references (204 lines) and the service list sitting in the middle of the page everyone scrolls
  through. Those are now [`docs/dashboard.md`](docs/dashboard.md) and [`docs/services.md`](docs/services.md),
  leaving the README as the narrative path: install → configure → what you get → troubleshoot.
  **581 → 408 lines**, with no content lost.
- Card options shared by every card (`title`, `config_entry_id`, `logo`, `image_width`) were
  repeated in all seven YAML examples — `config_entry_id` was explained seven times. They now live
  in one **Common options** table, so each card's example is just its `type` plus anything genuinely
  card-specific.
- Services are grouped by purpose (meals and Market, delivery schedule, plan and account, food
  profile, recipes and favorites) with one heading each, so a service is linkable and findable
  instead of buried in a 24-item bullet list.
- The densest bullets were split into sub-lists — the Schedule card's timeline entry was a single
  242-word sentence; nothing now exceeds 140 words.
- `HELLOFRESH_API.md` moved to [`docs/HELLOFRESH_API.md`](docs/HELLOFRESH_API.md). At 157 KB it was
  the largest file in the repo root and is contributor reference rather than user documentation, so
  it now sits with the other reference docs. `CHANGELOG.md`, `CONTRIBUTING.md` and `QUALITY_SCALE.md`
  stay at the root: the release workflow reads `CHANGELOG.md` by path, and GitHub only links
  contributing guidelines from the root or `.github/`.
- **New consistency tests** pin metadata that is edited by hand and drifts silently: the HACS
  country list against the supported countries, `strings.json` against `en.json`, all six shipped
  locales against the English keys, `services.yaml` against the actually-registered services, and
  every card named in `frontend.py` against the files on disk.

## 2.70 — 2026-08-18
- **Fixed `sensor.tracked_shipment_date` reading Unknown for a box the web UI showed as
  delivered.** The sensor resolves the most recent delivered week via `last_delivery_week`, which
  prefers the past-deliveries history endpoint — but that endpoint reports no carrier timestamp.
  Only the ranged deliveries payload carries `tracking.delivery_date`, so the winning week had
  `delivered_at` unset even though the same week's account entry knew exactly when the box
  arrived. The real arrival time is now back-filled from the matching account week.
- The back-fill matches on week id and requires subscription ids to agree when both sides carry
  one: with two subscriptions the same ISO week is two different boxes, so an unguarded match
  would stamp another subscription's arrival time onto this one.
- **Schedule card now shows when each delivered box arrived**, on the same meta line that already
  carried carrier and tracking number: `Delivered Aug 17, 6:53 PM · OnTrac · D100… · Order 123…`.
  Rendered in the viewer's timezone from the week's `delivered_at` offset, so a late-evening
  handover keeps its local day rather than jumping forward with UTC.
- Upcoming weeks are unaffected — only delivered weeks carry a carrier timestamp, so those rows
  render exactly as before.
- **Added translations for six languages: German, Dutch, French, Danish, Norwegian (Bokmål) and
  Swedish**, covering all 257 strings (entities, services, config/options flows and repair issues).
  Between them these cover 14 of the 16 supported markets; `us`/`uk`/`ca`/`au`/`nz`/`ie` already
  use `en.json`.
- Product terminology was taken from the live regional HelloFresh sites rather than translated
  generically, so the entity names match what the customer sees on their own HelloFresh website:
  **Kochbox** (de/at/ch), **Maaltijdbox** (nl/be), **Box Repas** (fr), **måltidskasse** (dk),
  **matkasse** (no), **matkasse/matkassar** (se). Dutch keeps the English loanword "deadline",
  which is what hellofresh.nl itself uses.
- Note on the browser walkthrough in the token setup step: the prose is translated, but the
  devtools menu labels it names (**Application**, **Storage**, **Cookies**) are localized by the
  browser itself and vary by vendor and version — treat those as best-effort and report
  corrections.

## 2.69 — 2026-08-18
- **New sensor: `sensor.tracked_shipment_date`** — when the most recent box **actually arrived**,
  as a `TIMESTAMP` entity. Sourced from the delivered week's carrier handover timestamp
  (`tracking.delivery_date`), which the integration already parsed into `HelloFreshWeek.delivered_at`
  but had never exposed as an entity.
- Note this is a third, distinct delivery time — the area has three similarly-named values, and they
  disagree on purpose: `last_delivery_date` is the **scheduled** day (a noon anchor),
  `tracked_shipment_estimate` is the carrier's **estimate**, and the new sensor is the **actual**
  arrival. A box handed over at 22:53 ET is already the next day in UTC.
- Gated on delivery: before a box arrives the same field holds a scheduled placeholder, so the
  sensor stays unknown rather than claiming an arrival that has not happened. It never falls back to
  the scheduled date, which would misreport a booking as a delivery.
- **New sensor: `sensor.tracked_shipment_estimate`** — the carrier's own estimated delivery time
  for the box currently in transit, as a `TIMESTAMP` entity.
- Worth knowing which field this is: the `estimated_delivery_time` on the week's `tracking` node is
  **not** usable — re-measured across all 17 captures, it is byte-identical to `delivery_date` in
  all **69** samples carrying both, and never appears without it. The sensor instead reads
  `est_delivery_time` from the SCM tracking lookup's status history, which is a different field
  from a different endpoint and carries a genuinely independent value.
- The estimate is **date precision**: the carrier reports midnight of the estimated day, where
  HelloFresh's `delivery_date` is a scheduled noon anchor — `2026-08-17T00:00:00Z` vs
  `2026-08-17T12:00:00Z` on the same box. So it answers "which day does the carrier now think this
  arrives?", not "which day was it booked for". Values are tz-aware, as `TIMESTAMP` requires.
- Absent estimates read as unknown rather than silently falling back to the scheduled date, and a
  later poll whose box omits the field does not clear an already-resolved value.

## 2.68 — 2026-08-18
- **Reworked the documentation.** `HELLOFRESH_API.md` read like an investigation log rather than a
  reference: a 635-line "Endpoint Matrix" held 20 unrelated subsections, and an "Evidence Gaps"
  section was organized around which HAR capture revealed what. Read endpoints are now split into
  four purpose-based sections (account/deliveries, catalogs/recipes, pricing, menus), the
  capture-organized material is reorganized as "Endpoints not implemented" (grouped by *why*), and a
  Contents table was added.
- Removed all real account data from `HELLOFRESH_API.md` — address, postcode, customer and
  subscription IDs, order number, and tracking codes are now consistent fake values. Removed all
  HAR/capture references, keeping the verified-vs-inferred distinction they carried.
- Corrected the API reference's weekday-change section, which still documented the superseded
  `POST …/changePlanDeliveryDetails` as the primary endpoint and marked it "HAR-verified"; added
  sections for the plan-change and food-profile endpoints, which backed shipped services but were
  undocumented.
- Tightened `README.md`: rewrote "Current Scope" from 21 bullets of implementation narrative into
  four short paragraphs plus a **Known limitations** list, documented the two previously missing
  services (`get_plan_options`, `change_plan`), fixed a stale card-version example, and removed
  implementation trivia from the user-facing card sections.
- Reordered the README's card sections to match the dashboard's actual render order (Recipes sits
  between Market and Food Profile, not last), along with the three other places that listed cards in
  sequence — including `dashboard/hellofresh.yaml`'s own header comment.
- **Audited every implemented write path against the captures; fixed three real defects.**
- `change_delivery_weekday` **swallowed `HelloFreshNotImplementedError`**. That type subclasses
  `HelloFreshError`, so the broad `except` driving the legacy fallback caught it — meaning when
  HelloFresh explicitly said an account cannot change its delivery weekday, the client retried a
  never-captured endpoint *and* suppressed the Repairs issue the service layer raises from that
  exact exception. The user saw a generic failure instead of "this account doesn't support write
  actions". Now re-raised alongside auth errors. Verified skip/unskip/one-off do not share the
  flaw.
- `get_plan_options` **crashed with `AttributeError` on a non-dict response body**. Every other
  read in the client type-guards the decoded payload before indexing it; this one did not, so an
  error envelope served as a bare list or string escaped as a Python error rather than something
  the service layer could report.
- `get_plan_options` **silently dropped a stringified price**. The isinstance check kept the raw
  value in `price_cents` while reporting `price` as `None`, so a plan would read as free. Prices
  are now coerced.
- Documented why `_sku_for_meal_count` has a floor but deliberately **no ceiling**: the
  `product_options` catalog is per-subscription and varies (capture 43 offers 2–6 meals, capture
  37 offers 1–12 for the same account), so a hardcoded upper bound would reject selections that
  are valid on other plans. Confirmed all in-range resizes land on SKUs that exist in the real
  catalog.
- **Confirmed the Rewards (loyalty) program is not yet callable, and found that two conflicting
  tier ladders ship side by side (HAR 44).** A capture of the `/achievements` page made **zero**
  authenticated requests — 52 requests to hellofresh.com, none with an `Authorization` header,
  despite the page holding a valid token — and no `/gw/` loyalty path appears anywhere in its
  760 KB payload. `/gw/configurations` carries `features.loyaltyProgram.enabled = false`.
- The unreleased scheme (`features.loyaltyBadges`: newbie 0, freshie 2, foodie 5, junior-cook 10,
  head-cook 25, master-cook 50) **contradicts** the live one (`loyalty.levels`: Apprentice 3,
  Sous Chef 10, Master Chef 20). Both key off box count, so both would read from
  `sensor.boxes_received`, but a 30-box account is "Master Chef" under one and "head-cook" under
  the other. No tier sensor was added: shipping either would mean guessing which scheme wins, and
  the flag says the new one is off.
- Noted for when it launches: the thresholds live in `GET /gw/configurations` (an endpoint the
  integration does not currently call), so the ladder can be read at runtime instead of hardcoded.

## 2.67 — 2026-08-18
- **New: change your box size from Home Assistant (HAR 43).** Capture 43 exposed the web app's
  "change plan" flow, which the integration did not implement at all. Two new services:
  `hellofresh.get_plan_options` lists every box you can switch to (2–6 meals × 2/3/4/6 servings,
  20 options, with prices), and `hellofresh.change_plan` switches to one via
  `PATCH /gw/api/plans/{planId}` → `204`. Verified against the capture, which switches
  3-meals/2-people → 3-meals/3-people and back, with the subscription SKU reflecting it on the
  next read. This is a recurring, billing-affecting change, deliberately kept separate from the
  per-week box resize that already happens during meal selection.
- Prices from the plan catalog are integer cents in the API (`6594`); the service returns both
  `price_cents` and a converted `price` (65.94), and sorts smallest box first — the API's own
  ordering starts at the largest and is not monotonic.
- Pinned a cross-endpoint inconsistency that would be easy to "clean up" wrongly: the plan PATCH
  sends uppercase `country=US`, while the delivery-weekday PATCH sends lowercase `country=us`.
  Both are exactly as captured.
- **Backfilled seven services that had no UI translations at all** — `get_account_summary`,
  `get_delivery_options`, `get_food_profile`, `get_plans`, `get_presets`, `get_spending` and
  `set_food_profile` were registered and documented in `services.yaml` but missing from
  `strings.json`/`en.json`, so Home Assistant rendered them as raw keys. Found by a new parity
  test that cross-checks service registrations against `services.yaml` and both translation files,
  including per-field coverage; this class of gap was previously invisible to the whole suite.
- Capture 43 re-confirms the capture-42 weekday change independently and shows zero response-shape
  drift against capture 40 across subscriptions, deliveries, menus, and profile.
- **Fixed the recurring delivery-weekday change: it was calling the wrong endpoint entirely
  (HAR 42).** `hellofresh.change_delivery_weekday` sent an inferred
  `POST /gw/api/plans/{planId}/changePlanDeliveryDetails` that appears in **none** of the 16
  retained captures. Capture 42 records the real web app making this change, and it uses
  `PATCH /gw/api/subscriptions/{id}` with a `{"subscription": {"id", "deliveryTime"}}` envelope.
  The service now sends the verified request; the old call is retained as a fallback only, since no
  capture proves the plans endpoint is dead. This supersedes the 2.66 note below, which documented
  the uncertainty rather than resolving it.
- Three details of that write are easy to get wrong and are now pinned by tests: the `country`
  param is lowercase `us` (every other endpoint sends uppercase); the request carries **no**
  `deliveryInterval`, so a non-default interval now logs a warning instead of being silently
  dropped; and the `200` response echoes the **pre-change** `deliveryTime` while a `GET` one second
  later already shows the new value — so the body is discarded rather than merged, which would
  otherwise revert the weekday in the UI until the next poll.
- **All evidence gaps are now closed.** Every endpoint the integration calls is backed by observed
  traffic. Retargeted the one pre-existing test that had pinned the inferred plans shape as though
  it were verified.

## 2.66 — 2026-08-18
- **Corrected a false verification claim.** `async_change_delivery_weekday`'s docstring said
  "HAR-verified", but the endpoint (`POST /gw/api/plans/{id}/changePlanDeliveryDetails`) appears in
  **none** of the 15 retained captures — the only observed `/gw/api/plans` traffic is `GET` reads.
  Re-audited every other `HAR-verified` claim in the source and the rest hold up. The docstring and
  the API reference now separate what is confirmed (`customerPlanId` is byte-identical to the id
  the plans path takes; `deliveryOption`/`deliveryInterval` are HelloFresh's own field names from
  the read payloads) from what is still guessed: the query params. The integration sends `country`
  only, while the sibling `/gw/api/subscriptions/*` writes send `country` **and** `locale` and the
  `/gw/api/plans` reads send neither — so the current choice matches no observed request and is the
  first thing to try if the call 400s.

## 2.65 — 2026-08-18
- **Closed the last major evidence gap: SCM shipment tracking is confirmed working (HAR 41).**
  `GET /gw/scm/tracking-ids/track/public-id/{public_id}` had never appeared in any capture, and
  since HelloFresh's delivery payload carries no carrier field at all, it was unclear whether
  `sensor.tracked_shipment_carrier` could ever populate. It can: the endpoint returns
  `{"boxes": [...]}` with a real `carrier` (`VEHO`), a carrier-hosted tracking URL, and a full
  status history (`pre_transit` → `in_transit` → `out_for_delivery` → `delivered`). Verified the
  whole chain end to end — the delivery payload's tracking link yields the public id that keys the
  request, the parser extracts all four fields, and the statuses render exactly as the README's
  "box on the way" automation expects.
- Added `Veho` to the carrier label map. It is the only carrier value observed in any capture, and
  the API shouts `VEHO` where the company brands itself `Veho`. Unmapped carriers still pass
  through unchanged rather than being guessed at.
- Corrected `docs/entities.md`, which (as of earlier today) said the carrier sensor was "often
  `None`" and steered automations away from it. It resolves whenever a box has published tracking.
- **Verified the two least-evidenced write paths against a fresh capture (HAR 40).** Skip/unskip
  had been confirmed only against a capture no longer on disk, and one-off reschedule had never
  been observed at all. Capture 40 exercises both — skip then unskip on one week, plus two
  reschedules — and the requests the integration builds match field for field, including the
  `source: "reschedule-delivery-feature"` literal that looks decorative. Both contracts are now
  pinned by tests using verbatim capture payloads.
- Documented that **both write endpoints return a stub week** that must not be adopted: every
  `allowedActions` flag comes back false/null, `cutoffDate` is null, and the reschedule response
  carries no status or delivery date at all. Merging any of it into state would leave the week
  looking permanently locked until the next poll. The client already discards these responses;
  there is now a regression test so a future "use the response we already have" optimization
  can't quietly break it.
- Recorded that `delivery_option` handles (e.g. `US-2-0800-2000`) come from the week's own
  `availableOneOffOptions` and are passed through unaltered rather than constructed.
- Capture 40 showed **zero response-shape drift** against captures 34–39 across every shared
  endpoint; capture 41 then closed the SCM tracking gap. The only remaining unobserved endpoint is
  the recurring delivery-weekday change
  (`POST /gw/api/plans/{id}/changePlanDeliveryDetails`).
- **Covered the two shipped platforms that had no tests at all.** `button.py` sat at 0% coverage
  and `intent.py` at 34% — both create user-facing surfaces (the refresh button, and the three
  voice/conversation intents), so a regression in either would have gone unnoticed by the entire
  suite. Now at 100% and 95%. The new tests pin the behaviours most likely to rot silently: the
  button dispatches on its description key rather than refreshing for any future button added to
  `BUTTONS`, and it goes through `async_request_refresh` so repeated presses stay debounced
  instead of fanning out concurrent polls; the next-delivery intent announces the *soonest* box
  across multiple accounts rather than whichever account was configured first, and the
  meal-selection intent survives a week with no `selection_deadline` instead of raising on the
  optional field.
- Added an **Evidence Gaps** section to the API reference: an audit of every endpoint the
  integration calls against every retained HAR capture, listing what no capture exercises and the
  exact UI action that would settle each one. (Skip/unskip and one-off reschedule were on that
  list and have since been closed by capture 40, above; the section now tracks only the SCM
  tracking lookup and the recurring delivery-weekday change.)
- Fixed a latent test-suite fault that produced teardown ERRORs depending on file ordering.
  `test_token_lifecycle_simulation.py` closed an event loop it had installed as the current one,
  and Home Assistant's autouse `verify_cleanup` fixture then hit
  `RuntimeError: Event loop is closed` while tearing down whichever module ran next. Present since
  the first commit; it only surfaced once a new test file happened to sort after it.
- Documented the **real shape of HelloFresh's shipment tracking payload**, surveyed across every
  HAR capture (59 non-null `tracking` nodes out of 409 weeks). A non-null node has exactly six
  keys and **no carrier field**, so `sensor.tracked_shipment_carrier` is often `None` by design
  rather than by fault — `docs/entities.md` previously implied it reliably reports `UPS`/`FedEx`.
  Also recorded that `tracking_id` is always empty (the usable value is `tracking_code`), that
  `tracking` is null for ~86% of weeks including delivered ones, and that
  `estimated_delivery_time` is byte-identical to `delivery_date` in all 45 samples that carry
  both — so it is deliberately not exposed as a separate entity. Added tests pinning the parser
  to verbatim capture payloads.
- Measured and documented the sold-out overlay's bandwidth cost: `/gw/menus-service/menus` returns
  **3.0–3.8 MB even when scoped to a single week**, so the advisory "Sold out" ribbon costs roughly
  3–7 MB per poll (~25–55 MB/day at the default interval). Narrowing the query further cannot help;
  the per-week floor is already over 3 MB.
- Surveyed API drift across all captures: 70 distinct `/gw/` paths, and only two additive changes
  over the whole period (`shippingAddress` on `/gw/calculate`, `mealsReady` on
  `/gw/my-deliveries/menu`) with **no removals or renames**. `mealsReady` is `true` in all 14
  observed responses, so what `false` means can't be inferred and nothing reads it — noted in the
  API reference so it reads as a deliberate omission rather than an oversight.

## 2.64 — 2026-08-17
- Fixed the **All Recipes** card showing a "Refine" row that duplicated its own filter list.
  Selecting "Top rated" fetches the root catalog page, whose child collections *are* the
  top-level categories, so they were offered a second time as sub-categories — pick "Top rated"
  and "Chinese Recipes" appeared in both rows. Only a real category has sub-categories to refine
  by, so the root listing now reports none.
- Fixed the per-serving meal price losing its currency formatting in the Meal planner. The card
  defined `_fmtPrice` **twice**; a class body can't hold two methods of the same name, so the
  later generic one silently replaced the per-serving one and Canadian/Australian users saw
  "CA$9.99"/"A$9.99" where "$9.99" was intended. The per-serving formatter is now named
  distinctly.
- **Extracted the shared card helpers into `hellofresh-shared.js`.** Escaping, URL safety, image
  resizing, local-date parsing, relative/absolute date formatting, status title-casing, money
  formatting, week editable/past state and the whole cross-card sync protocol had been
  hand-copied into up to seven card files. That duplication was the direct cause of most bugs
  fixed in 2.63 — each was one copy drifting from the others, with no error, just cards quietly
  disagreeing. There is now one definition of each.
- Added module-load smoke tests for all seven cards. `node --check` only *parses*, so it happily
  accepts a reference to an undefined variable — exactly the mistake made while migrating (a card
  whose version constant is `CARD_VERSION` got an import interpolating `MEAL_PLANNER_CARD_VERSION`).
  That parses fine and then throws `ReferenceError` in the browser, so the card silently fails to
  register and simply never appears. The new tests import each card as a real ES module and assert
  it registers its custom element.
- Added tests for the service dispatch layer (`__init__.py` 17% → 39%), covering account targeting
  for all 22 services: implicit single-account, explicit `config_entry_id`, unknown entry ids,
  no-account-loaded, refusal to guess between several accounts, and client errors surfacing as
  Home Assistant errors rather than vanishing.

## 2.63 — 2026-08-17
- Fixed the **Market card** treating locked and delivered weeks as editable. Its copy of the
  "can this week still be changed?" check omitted the `allowed_actions.mealSwap` test and returned
  true by default, so a week HelloFresh had closed — or one already delivered, which carries no
  `allowed_actions` at all — showed an "Editable" chip with live ± steppers, letting you build a
  cart the server would then reject. `HelloFreshWeek.is_editable` documents that the backend and
  all three cards stay in lockstep; the Market card was the only one out of step.
- Fixed delivery statuses rendering inconsistently between cards. HelloFresh sends
  SCREAMING_SNAKE (`DELIVERED`, `ON_THE_WAY`), and the Schedule card's title-caser was missing its
  `.toLowerCase()` — so the same week read "DELIVERED" there and "Delivered" in the Meal planner
  and Market cards. A missing status also rendered as the literal "Null" in two cards.
- Added parity tests that execute the real `_isEditable` and `_titleCase` from every card that has
  a copy and assert they agree, plus tests for the coordinator's token-refresh escalation (an
  unrecoverable auth failure must stop the refresh timer and start reauth; a transient one must do
  neither) — that path was the untested gap that let the history-endpoint auth bug hide.
- Fixed favoriting a recipe in the **Recipes card** never refreshing the Meal planner's hearts on
  a multi-account setup. The cross-card event was dispatched with an empty `detail`, and every
  listener drops an event whose `detail.accountKey || "default"` doesn't match its own key — so
  an empty detail didn't mean "all accounts", it meant "the single-account case only", and was
  silently discarded by any card configured with a `config_entry_id`. Nothing errored, which is
  why it went unnoticed.
- Added contract tests for the cross-card sync protocol (event names, the
  `hellofresh:selected-week:<accountKey>` storage key, and how `accountKey` is derived), asserted
  across all cards at once. The protocol is hand-copied into six card files and its correctness
  condition is exact agreement, so drift is now a test failure rather than silence.
- Fixed `image_width` doing **nothing** in the Meal planner and Market cards — the two cards that
  actually offer the option. Both carried a one-line copy of the image-resize helper that only
  rewrote a `/q_auto/` transform, but every image URL HelloFresh really serves is a
  `hellofresh_s3` form (`f_auto,fl_lossy,h_300,q_auto,w_450/hellofresh_s3/…`), which it returned
  untouched. So each tile downloaded the full-size hero JPEG (~1.7 MB) on a grid showing dozens,
  while the setting looked like it was working. The full implementation already existed in the
  Recipes card and the shared module; all four copies are now pinned to identical behaviour by
  test, so the next edit can't fix one and miss the others.
- Fixed the **Subscription card** showing stale data for up to 3 hours after a sibling card saved
  a change. It had the code to drain a queued follow-up fetch, but nothing ever set the flag —
  dead code — so a `hellofresh-data-changed` event arriving while a fetch was already in flight
  was dropped, and that in-flight response predated the write. Now queues like the Schedule and
  Cost cards. Added tests covering all three.
- Fixed an **expired token during history loading being swallowed instead of triggering reauth**.
  `_async_get_past_delivery_weeks` walks candidate endpoints and treats an error as "try the next
  one", but an auth failure fails every candidate identically — so a dead token silently produced
  empty past weeks with no reauth prompt, for the whole poll interval. Its sibling running in the
  same `asyncio.gather` had always re-raised. Fixed in the candidate walk and in the pagination
  loop underneath it, both of which could hide the expiry.
- Fixed the delivery **calendar** dropping deliveries. `async_get_events` returned only events
  that *started* inside the requested window, but Home Assistant's contract is overlap — and a
  delivery is an all-day event starting at midnight, so any window that didn't begin exactly at
  00:00 returned nothing for that day. Also fixed a latent `TypeError` from comparing the naive
  datetimes built out of all-day dates against Home Assistant's timezone-aware bounds, in both
  the event lookup and the next-event sort.
- The calendar platform had **no tests at all** (0% coverage, now 84%) — which is how both defects
  survived. Added nine covering overlap, half-open window boundaries, all-day handling, next-event
  selection, and undated orders.
- Documentation: the **All Recipes** view and the Recipes card are now covered in the README's
  dashboard and scope sections (the card shipped but was missing from both lists, which still
  said "five views" and "six packaged cards"). Added the catalog's two-row-source merge,
  collection-tag discrimination, sub-category `path` requirement and `__NEXT_DATA__` fallback to
  the API reference, along with the shared `hellofresh-recipe-detail.js` module and the two
  layout constraints (fixed positioning, identity-matched backdrop) that its overlay depends on.
- Corrected a long-standing documentation error: `.mov` clips were described as unplayable in
  Chrome and Firefox. They play fine — the CDN serves both `.mp4` and `.mov` as `video/mp4`, and
  declaring that type explicitly is what fixed the "broken" videos in 2.60.
- Corrected the past-history claim. Three places promised "a full calendar year of past boxes"
  while the **Weeks of past history** option has defaulted to 26 weeks (~6 months) since it
  became configurable — the README contradicted itself, since the options section documented 26
  correctly and even suggested ~56 for a year. All three now state the real default and range.
- Fixed the YAML-mode resource note, which listed only five of the seven cards to register (Cost
  and Recipes were missing), and refreshed the `?v=2.06` version examples to the current release.
- Corrected the API reference's `selected_meal_count` note: it claimed the sensor reads only weeks
  with `needs_selection = True`, but it reads `next_configurable_week`, which also covers the
  soonest non-skipped upcoming week carrying selection context.
- Reordered this changelog newest-first; 2.48 had been sitting above 2.58, and 2.61 above 2.62.

## 2.62 — 2026-08-17
- Fixed every meal on a **delivered** week opening the wrong recipe and playing the wrong
  video. Past-delivery meals carry no course index, so all of a delivered week's meals shared
  the same (null) tile key and every tap resolved to the week's *first* meal — tapping the
  Korean-Style Wing Feast opened the Grass-Fed Rib-Eye, and a meal whose neighbour had no clip
  appeared to have a broken video link. Tiles now fall back to the recipe id, which is always
  present and unique. Editable weeks, which do have course indexes, are unchanged.
- Recipe clips are also read from the meal wrapper, not just the recipe node: `videoLink` sits
  on the recipe in menu payloads but on the meal in delivery history, so one payload shape
  silently lost its ▶ button.

## 2.61 — 2026-08-17
- **Full recipe view in the Meal planner and Market cards** — the tap-through recipe sheet the
  Recipes card offers (ingredients with amounts, a servings switcher, step-by-step
  instructions, utensils, allergens, nutrition and the printable PDF) now works there too.
  In My Menu, tapping a meal on a week you can no longer change opens it; on an editable week
  the tap still changes your selection, so the recipe opens from a small **ⓘ** button instead.
  In Market, tapping any item opens it — quantities are changed with the ± steppers, so the
  tile itself is free. Market add-ons turn out to carry ordinary HelloFresh recipe ids, so they
  get the same detail as a meal. The sheet is now one shared module rather than three copies.
  (Fixed before release: the sheet was absolutely positioned, which needs a positioned ancestor
  that neither of the two new cards creates — so it escaped its card and was clipped away,
  showing a grey backdrop with no panel. It is fixed-positioned now, like the video lightbox.)

## 2.60 — 2026-08-17
- Fixed "Sold out" ribbons appearing on **past** weeks. Sold-out is meaningless once a box has
  shipped, but a delivered week's menu payload can still carry a stale flag, and the
  "can this week still be changed?" check never looked at the delivery date — so a past week
  with no recorded deadline still counted as editable. Delivered weeks now have the flag
  cleared, the card refuses to draw the ribbon on them, and no request is spent on them.
- Fixed some recipe videos not playing. HelloFresh's clip URLs end in both `.mp4` and `.mov`,
  and the player assumed `.mov` was unplayable — but the CDN serves both as `video/mp4`, so
  they play fine once the type is declared explicitly. A clip that genuinely fails now shows a
  message instead of a silent black box.

## 2.58 — 2026-08-16
- Fixed categories in the All Recipes card showing far fewer recipes than the website: Noodle
  Recipes listed 10 where the site shows 30, Chicken 14 where the site shows 30. The page
  carries the category's canonical recipe list *and* a couple of small "Quick & Easy" /
  "Most Recent" rails, and only the rails were being read. Both are now merged and deduped —
  Noodle goes 10 → 34, Chicken 14 → 42.
- The All Recipes card now loads **50** recipes per category by default (was 40), so the
  larger categories are no longer clipped — Chicken has 42. Still configurable via the card's
  `limit` option and the `get_catalog_recipes` service (1–200).
- **Sub-categories are now browsable** — selecting a category that has children (Noodle →
  Ramen / Udon / Rice / Soba / Yakisoba; Chicken → Breast / Thighs / Cutlets / …) shows a
  second "Refine" chip row. These children never appear in HelloFresh's top-level category
  list, so they were previously unreachable. They also cannot be fetched by their bare slug —
  `/recipes/ramen-noodles` redirects away — so `get_catalog_recipes` now reports each child's
  full `path` alongside its slug.

## 2.57 — 2026-08-16
- Fixed the All Recipes card reporting **"No recipes found"** for the entire catalog.
  HelloFresh began returning 404 for every `_next/data` catalog URL — on every build id,
  including ones that had just worked — while the corresponding pages kept rendering
  normally, so the build-id re-scrape that normally heals a deploy had nothing to recover.
  The same data is embedded in each page's `__NEXT_DATA__` blob, so the integration now falls
  back to reading it from the page when the JSON route fails. The cheap JSON URL is still
  tried first, and no extra request is made while it works.

## 2.56 — 2026-08-16
- Fixed every category in the All Recipes card showing the same recipes: picking **Indian
  Recipes** listed beef fondue and steak, and so did every other category. A category page
  embeds several recipe queries — some scoped to that category, plus a generic "best rated
  overall" list that is identical on every page — and the integration merged all of them, so
  the generic list (the largest, and last) swamped the real results. Two faults compounded:
  a filter meant to skip the category's *metadata* query matched on the whole serialized
  query key, and the category queries mention the collection inside their filter clause, so
  the correct rows were discarded before the merge even happened. Categories now read only
  their own recipes; the top-level listing still shows the best-rated selection.

## 2.53 — 2026-08-16
- Fixed the recipe detail sheet showing no photo. The payload offers both a bare image path
  and a ready-made absolute URL, and the convenient one is dead — it points at a CloudFront
  host that now answers 502 for every path, the same host behind the missing catalog
  thumbnails. The path is now joined to the verified host instead.
- Fixed the recipe detail sheet's ✕ not closing it — the same two faults as the video
  lightbox, in its twin: the overlay sits outside the element carrying the click handler, and
  a propagation guard meant to protect the panel was swallowing clicks on the ✕ inside it.
  Reading the recipe and using the servings switcher still don't dismiss it.
- **Your cookbook is now browsable** — the Recipes card could add and remove favorites but had
  no way to show them. A ♥ Cookbook chip now lists your saved recipes alongside the browse
  categories, and un-favoriting one removes it from the list rather than leaving a hollow
  heart behind.

## 2.51 — 2026-08-16
- Fixed the integration failing to load entirely (`ServiceRegistry.async_register() got
  multiple values for argument 'schema'`): a duplicated argument in the
  `get_recipe_collections` registration shifted its handler onto the `schema` parameter.
  Service registrations are now checked structurally, so a malformed one fails a test instead
  of setup.
- Fixed every read-only service (`get_spending`, `get_plans`, `get_presets`,
  `get_delivery_options`) raising `'HelloFreshDataUpdateCoordinator' object has no attribute
  'async_get_*'` — most visibly as "Could not load spending" on the cost card. A second helper
  with the same name shadowed the original and returned the coordinator instead of its client.
- Fixed missing images throughout the All Recipes card: catalog rows carry a bare image path,
  and the CDN host it was joined to was inferred rather than observed (the HAR captures never
  re-fetched these images, so none appear in the exports). Every URL it produced 502'd. The
  host is now verified against the live site, and thumbnails are requested at display width —
  the untransformed assets are ~1.7 MB each, versus ~20 KB for a grid tile.
- All Recipes now matches the other dashboard cards' header sizing, which was noticeably
  smaller than My Menu, Market and Food Profile.
- Fixed the recipe video lightbox being impossible to close: neither the ✕ button nor a
  backdrop click dismissed it (only Escape worked). The overlay sits outside the card element
  that carried the click handler, and a propagation guard meant to protect the player was also
  swallowing clicks on the ✕. Clicking the video's own controls still does not close it.

## 2.49 — 2026-08-16
- **Recipe videos in the meal planner** — meals that ship with a HelloFresh promo clip now
  show a ▶ button that opens the video in a lightbox. Coverage is sparse (a few meals out of
  several hundred per week), on past and upcoming weeks alike; the still image always stays
  the base layer, and a `.mov` (which Chrome/Firefox cannot play) falls back to an
  open-directly link.
- **New Recipes card** (`custom:hellofresh-recipes-card`) — browse HelloFresh's public
  ~10,000-recipe catalog by category, with ratings and prep times, and add/remove cookbook
  favorites. This is browse content shared by all customers, unrelated to your subscription,
  so it is fetched on demand rather than polled into entity state. The catalog comes from the
  website's Next.js data URLs, whose build id rotates on every HelloFresh deploy; the
  integration scrapes it on demand and re-scrapes automatically on a 404, so this self-heals.
- **Cookbook favorites** — meals bookmarked in your HelloFresh cookbook now show a ♥ in the
  meal-planner card, via new `get_favorites` / `add_favorite` / `remove_favorite` services.
  `get_favorites` lists the whole cookbook with full detail, or — given `recipe_ids` — uses
  HelloFresh's cheaper filter endpoint to answer which of those are bookmarked. The hearts on
  a week's menu use that filter, one extra batched request per refresh; turn it off with the
  new **Show favorite hearts** option. A failed lookup leaves tiles with no heart rather than
  a misleading empty one. HelloFresh's own collection naming is counter-intuitive and worth
  knowing if you read the code: bookmarks are *created* under `internal-recipes` but *listed
  and deleted* under `external-recipes`, keyed by a server-assigned row id.
- **Per-meal pricing, order history and sold-out state** — fields that were present in menu
  payloads the integration already fetched but never read:
  - Each meal's real per-serving `price` (HelloFresh sends money as `{units, nanos}`; 17 +
    0.98 → $17.98) plus its `premium`/`classic` price group, shown on the planner tile. This
    is distinct from the existing surcharge badge, which is only the premium *uplift*.
  - `delivered_count` / `last_delivered_week` — HelloFresh's own "you've ordered this N times,
    last in W22" signal — and your own star `rating` where you've given one.
  - `is_sold_out` / `is_hidden`: sold-out meals are greyed with a **Sold out** ribbon. The two
    menu sources are disjoint — the primary endpoint has the prices and history, the catalog
    has the availability flags — so the catalog is now fetched once per refresh for the weeks
    you can still change, and *only* its availability flags are overlaid onto the existing
    meals. Delivered and past-cutoff weeks are skipped, since the flag can't change an outcome
    there. Marking is **advisory**: the catalog is HelloFresh's anonymous regional menu and is
    not confirmed to track per-customer availability, so the tile stays tappable and
    `select_meals` still submits (with a warning) rather than blocking a selection HelloFresh
    might well accept.
  - `related_category` (appetizers / desserts / …) for menu grouping.
- **Full recipe detail** — new `get_recipe_detail` service and a tap-through view in the
  Recipes card: ingredients with amounts, a servings switcher that rescales them, step-by-step
  instructions, utensils, allergens, nutrition, and the printable recipe-card PDF. This reads
  a plain HelloFresh API rather than the website, so unlike the browse listing it does not
  depend on the site's Next.js build id and cannot break on a deploy.
- **Food profile completion** — `get_food_profile` now also returns how many profile fields
  HelloFresh considers answered (and which are outstanding), shown as a progress bar in the
  Food Profile card. Best-effort: omitted rather than fatal when the endpoint doesn't answer.
- **Secondary favorites store surfaced** — HelloFresh runs a second favorites service ("cfs")
  behind its /recipes/favorites page, separate from the cookbook and not synchronized with it.
  `get_favorites` now reports it alongside the cookbook under `secondary_favorites` rather
  than merging the two, so mismatched counts stay visible instead of silently disagreeing.
  Its rows are passed through unmodelled — no populated response has ever been observed.
- **Meal price preview** — new `preview_meal_price` service prices a hypothetical selection
  (grand total, subtotal, shipping, tax, discounts, and per-meal premium surcharges) without
  saving it, so a planner UI can show cost before committing. The box SKU is resized for the
  requested meal count, matching what a real save does, so previewing more meals than your
  current box holds is priced rather than rejected.
- Deep-audit fixes across the integration (17+ issues found by a full code review):
- Fixed a stale-data bug where dashboard cards could be served a previous poll's weeks
  for up to a full refresh interval (the get_weeks response cache was keyed by a
  reusable memory address).
- Token-only setup/reauth no longer stores a dead refresh token: validating a pasted
  token could rotate it server-side, and the pre-rotation pair was persisted — the
  root of "reauth successful, 401 minutes later" loops. Rotations are now captured
  and stored.
- Auth reliability: a mid-request transport failure no longer re-sends the one-time
  refresh token over the fallback transport (double-spend); after a credential
  failure the proactive token timer stops and triggers reauth instead of re-submitting
  a stale password every few minutes; network failures during auth now surface as
  "cannot connect" instead of "Unknown error"; concurrent-401 handling compares
  against the token the failed request actually used; bot-block detection now works
  on HTTP/2 lowercase headers.
- Two-subscription accounts: weeks sharing an ISO week id no longer cross-wire — id
  lookups prefer the primary subscription, and delivered-meal history never fills a
  week from the other subscription.
- A week with an explicit "0 meals selected" no longer shows a fabricated full
  selection; skip/select/meal writes are always submitted instead of being silently
  dropped when the previous poll's snapshot already matched; past weeks' billed totals
  are no longer overwritten by a current-plan estimate.
- Deadline sensors: offset-less cutoff timestamps are normalized to UTC so the
  timestamp sensors can't fail to update; very long text states are truncated to HA's
  255-char limit.
- Performance: entities no longer re-notify on every poll when nothing changed; the
  large ranged-deliveries payload is downloaded once per poll instead of N+1 times;
  per-week price enrichment skips past weeks and is concurrency-capped (was up to ~35
  simultaneous requests); plan-preference lookups are deduplicated; the public-menu
  fallback page is parsed off the event loop.
- Dashboard cards: a failed save keeps your unsaved meal/market picks so you can
  retry (they were wiped); a failed skip/reschedule keeps its error notice visible
  and no longer tells sibling cards the data changed; a data-changed event during an
  in-flight fetch now queues a follow-up instead of being dropped; food-profile edits
  made while a save is in flight are no longer reverted; protein/variant filters stay
  consistent when deselecting a filtered-out meal; toasts no longer stick forever or
  rebuild the whole recipe grid; undated weeks show "—" instead of "Invalid Date";
  duplicate Lovelace resource entries from old installs are cleaned up on upgrade.
- Hardening: payment method/gateway descriptors are redacted from diagnostics;
  image URLs are scheme-checked before reaching the DOM; a few integer fields are
  coerced before HTML interpolation (defense in depth); calling a service with no
  account loaded now says so instead of claiming multiple accounts exist.
- Date handling: all delivery-date past/future gating now uses local time
  consistently (was mixed local/UTC, which could misclassify weeks near midnight UTC).

## 2.48 — 2026-08-10
- Fixed a regression from the comma-decimal work: scientific-notation amount strings
  (e.g. "1.5e3") were silently mangled (1.53) instead of parsing as 1500; non-finite
  values (inf/nan) now coerce to None instead of reaching sensor states.

## 2.47 — 2026-08-09
- The "Show data-quality repair warnings" toggle now actually clears existing
  warnings in every case: cleanup runs at startup/reload (not only after a
  successful data refresh), warnings orphaned by previously removed config entries
  are swept away, and removing the integration now deletes its warnings instead of
  leaving them behind forever.

## 2.46 — 2026-08-09
- New "Show data-quality repair warnings" option (on by default): turn it off to
  suppress the advisory Repairs warnings (public menu fallback, unrecognized
  payloads, account data unavailable, blocked write actions) and clear any that are
  already showing. The menu-fallback warning text now also explains that it clears
  itself and how to silence it.

## 2.45 — 2026-08-06
- Fixed the account credit sensor reporting 100× the real balance (e.g. $8992.00
  instead of $89.92): the payments balance endpoint returns cents, now converted
  to major units.
- Comma-decimal markets (DE, NL, FR, ...): localized amount strings like "89,92",
  "1.234,56", or "€ 8,99" now parse correctly everywhere prices are read, so cost,
  credit, and price sensors (and the dashboard cards fed by them) no longer come up
  empty or wrong in those countries.

## 2.43 — 2026-08-05
- Release notes are now curated: Highlights come from this changelog's Unreleased
  section (rotated at release time), followed by the commit list and a compare link.

## 2.42 — 2026-08-04
- Food profile card: keyboard focus is preserved across re-renders, the Save button no
  longer sticks "enabled" after edits are undone, and the refresh button asks before
  discarding unsaved changes.
- Food profile card: saving now notifies the other HelloFresh cards (and the card
  refetches after their writes), so all cards stay in sync without manual refreshes.
- Food profile and meal planner cards can now be configured from the dashboard UI
  (the planner's editor previously rendered blank).
- Accessibility: linked labels, `aria-pressed` chips, and AA-contrast text on the
  brand-green buttons in the food profile card.
- Release process: GitHub releases include a commit list instead of an empty body.

## 2.41 — 2026-08-04
- Fixed a dark mode bug in the food profile card.

## 2.40 — 2026-08-01
- Broader country enablement.

## 2.39 — 2026-08-01
- Updated Netherlands status to "Verified" in the README.

Older releases predate this changelog; see the
[GitHub releases page](https://github.com/kedube/ha-hellofresh/releases).
