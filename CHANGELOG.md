# Changelog

Notable changes for each tagged release. Versions correspond to git tags and to the
`version` field in `custom_components/hellofresh/manifest.json`. Add entries under
**Unreleased** as part of each change; the release workflow rotates that section into a
version heading and publishes it as the release's Highlights.

## 2.48 — 2026-08-10
- Fixed a regression from the comma-decimal work: scientific-notation amount strings
  (e.g. "1.5e3") were silently mangled (1.53) instead of parsing as 1500; non-finite
  values (inf/nan) now coerce to None instead of reaching sensor states.

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
