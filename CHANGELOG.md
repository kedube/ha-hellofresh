# Changelog

Notable changes for each tagged release. Versions correspond to git tags and to the
`version` field in `custom_components/hellofresh/manifest.json`. Add entries under
**Unreleased** as part of each change; the release workflow rotates that section into a
version heading and publishes it as the release's Highlights.

## 2.48 — 2026-08-10
- Fixed a regression from the comma-decimal work: scientific-notation amount strings
  (e.g. "1.5e3") were silently mangled (1.53) instead of parsing as 1500; non-finite
  values (inf/nan) now coerce to None instead of reaching sensor states.

## Unreleased
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
