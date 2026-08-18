# Changelog

Notable changes for each tagged release. Versions correspond to git tags and to the
`version` field in `custom_components/hellofresh/manifest.json`. Add entries under
**Unreleased** as part of each change; the release workflow rotates that section into a
version heading and publishes it as the release's Highlights.

## Unreleased
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
