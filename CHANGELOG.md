# Changelog

Notable changes for each tagged release. Versions correspond to git tags and to the
`version` field in `custom_components/hellofresh/manifest.json`. Add entries under
**Unreleased** as part of each change; the release workflow rotates that section into a
version heading and publishes it as the release's Highlights.

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
