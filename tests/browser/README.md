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
their weeks around today's date in the local time zone; CI pins `TZ=America/New_York`.

Harness URL parameters: `view` (overview, menu, market, recipes, account), `theme=dark`,
`scenario` (`clean` without the expiring-card banner, `live` for a Netherlands delivery on the
road, `delivery`), `now` (a fixed clock, e.g. `2026-10-05T17:40:00`), `panel=1` to mount the
sidebar panel instead of the card (with `narrow=1` for a phone, `frame=ha` for a Home
Assistant-style sidebar, and the section in the path: `/hellofresh-app/menu`), `classic=<card>`
for one of the deprecated classic cards, `latency` and `fail=<service>`.
