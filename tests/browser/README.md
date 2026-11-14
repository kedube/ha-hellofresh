# Browser tests

The HelloFresh card, driven in headless Chrome against a fake Home Assistant (`harness.js`,
`fixtures.js`) holding made-up account data. Nothing here talks to HelloFresh.

```sh
cd tests/browser
npm ci
npm test              # every flow in flows.mjs; exits non-zero on a failure or page error
npm run screenshots   # regenerate the README / docs images in images/
npm run serve         # the harness on http://localhost:8765 to poke at by hand
```

Chrome is found at its usual macOS or Linux location, or set `CHROME_PATH`. The fixtures build
their weeks around the page's date in the local time zone; CI pins `TZ=America/New_York`. The
flows run on a fixed clock (Tuesday 2026-09-29, noon), because from Thursday to Monday the next
box is past its meal deadline and the flows that edit it have nothing to click. The harness and
screenshots use today's date unless given `now`.

The card shows a proof-of-delivery photo only from an https address, so the fixtures name theirs
on a made-up https host and the flows and screenshots answer it with a local picture
(`serveDeliveryPhotos` in `chrome.mjs`). Opened by hand with `npm run serve`, that one photo
stays blank.

`FLOWS=<regex> npm test` runs only the flows whose name matches (case-insensitive), e.g.
`FLOWS='undo|load more' npm test`.

Harness URL parameters: `view` (overview, menu, market, recipes, week, account), `theme=dark`,
`scenario` (`clean` without the expiring-card banner, `live` for a Netherlands delivery on the
road, `delivery`, `payment` for two boxes HelloFresh couldn't collect a payment for, `delayed` for
a next box HelloFresh moved a day later and the one after a day earlier), `now` (a fixed clock,
e.g. `2026-10-05T17:40:00`), `panel=1` to mount the sidebar panel instead of the card (with
`narrow=1` for a phone, `frame=ha` for a Home Assistant-style sidebar, and the section in the
path: `/hellofresh-app/menu`), `latency`, `fail=<service>`, and `mealplan=1` for a Week tab
already half planned (with a few Market items bought for it).

The fake Home Assistant answers every service the card calls, as the integration does: among them
`get_catalog_recipes` a page at a time (`limit`, `offset`, with `total` and `has_more`),
`get_plan_options` and `change_week_servings` (one week's box for another number of people, in
the Classic family; a size it doesn't sell fails with a message saying so).
