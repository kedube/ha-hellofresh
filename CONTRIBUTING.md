# Contributing

Thanks for helping improve the HelloFresh Home Assistant integration.

## Development setup

1. Fork the repository and create a feature branch.
2. Create a virtual environment.
3. Install test dependencies:

```bash
pip install -r requirements_test.txt
```

4. Run the test suite:

```bash
pytest -q
```

## Running the CI checks locally

CI runs five jobs; all of them except the two hosted actions (HACS validation and hassfest) can
be reproduced locally. Running these before pushing avoids a round-trip:

```bash
ruff check .                                  # lint
ruff format --check .                         # formatting (enforced -- see note below)
pytest -q                                     # Python test suite
python .github/scripts/check_card_syntax.py   # every Lovelace card parses (needs node)
node .github/scripts/check_card_logic.mjs     # card week-selection behaviour
python .github/scripts/generate_card_strings.py --check   # the card's English module is current
```

Formatting is enforced, so run `ruff format .` before committing rather than hand-aligning code.

### Why the card checks exist

The HelloFresh card under `custom_components/hellofresh/www/` is several thousand lines of
hand-written JavaScript with no build step. A past-week browsing regression once shipped precisely
because nothing validated it. Two guards now cover that gap:

- **`check_card_syntax.py`** proves each card module parses as an ES module, so a typo cannot reach
  users as a blank dashboard panel. It blanks `import` lines before checking (preserving line
  numbers) because `node --check` would otherwise fail resolving the shared modules.
- **`check_card_logic.mjs`** imports the card's real `browsableWeeks` and exercises it. The
  load-bearing assertion is that **every past week in the window stays browsable**, whatever data
  it carries, because one week list serves the Menu, the Market and the Overview. Add a case here
  when you change week filtering.

The card (`hellofresh-card.js` and its `hellofresh-card-*.js` modules) keeps its decisions in a
DOM-free module, `hellofresh-card-logic.js`, which the checks and
`tests/test_unified_card_logic.py` import directly. That test also pins the decisions the card took
over from the classic cards it replaced — filter tags, week states, skip rules, prices, month
roll-ups — so change them there deliberately, not by accident.

## Project layout

- `custom_components/hellofresh/` contains the integration code.
- `custom_components/hellofresh/www/` contains the Lovelace card (plain ES modules, no build step):
  `hellofresh-card.js` with its view, logic and style modules (`hellofresh-card-*.js`), the shared
  helpers (`hellofresh-shared.js`), the recipe sheet (`hellofresh-recipe-detail.js`), the card's
  text layer (`hellofresh-i18n.js`, with the generated `hellofresh-i18n-en.js`) and the sidebar
  panel that hosts it (`hellofresh-panel.js`).
- The HelloFresh card's words are translations like the rest of the integration's text: English in
  `strings.json` under `config_panel.card` (copy it to `translations/en.json`; the two stay
  identical), each language in `translations/<code>.json`. Home Assistant serves the card the
  user's language. In the card, `t("group.key", { name })` gives plain text and `ht(…)` text safe
  to put in HTML; a key with `one`/`other` forms is a plural, picked by the `count` value. After
  changing card text, add the key to every language (`tests/test_translations.py` requires the
  same keys everywhere) and run `python .github/scripts/generate_card_strings.py`, which rewrites
  `www/hellofresh-i18n-en.js` (CI checks it's current).
- `tests/` contains the pytest suite. `tests/test_repo_consistency.py` pins hand-edited metadata
  (HACS country list, translation completeness, `services.yaml`, card registration) that otherwise
  drifts out of step with the code.
- `.github/scripts/` contains the CI helper scripts, including the card checks described above.
- `docs/` contains the user reference documentation split out of the README. Keep the README as the
  narrative landing page (install → configure → what you get → troubleshoot) and put detail here:
  - [`docs/entities.md`](docs/entities.md) — every sensor, binary sensor, switch, button, the delivery calendar, and the prep-list to-do entities.
  - [`docs/dashboard.md`](docs/dashboard.md) — the HelloFresh card, section by section, its options, and moving from the
    classic cards it replaced.
  - [`docs/services.md`](docs/services.md) — all 24 services, grouped by purpose.
  - [`docs/HELLOFRESH_API.md`](docs/HELLOFRESH_API.md) — the endpoint and normalization reference.
    This one is for contributors rather than users: payload shapes, why each endpoint is called,
    and the reasoning behind the merge order. Read it before changing anything in `client.py` or
    `normalizers.py`.

  `tests/test_repo_consistency.py` checks that every relative Markdown link resolves to a real file
  and heading, and that the README stays under 500 lines — if that trips, move the newest reference
  material into `docs/` rather than raising the limit.
- `hacs.json` and `manifest.json` contain release and integration metadata.

## Pull requests

Please keep pull requests focused and include:

- a clear summary of the change
- tests for behavior changes when practical
- updated documentation when setup, behavior, or services change

If you are fixing a bug, linking the issue in the pull request description is helpful.

## Reporting issues

Please use the GitHub issue templates for bug reports and feature requests. When possible, include:

- Home Assistant version
- integration version
- installation method
- relevant logs with secrets removed

## Notes

- Do not commit secrets, tokens, or exported diagnostics with private account data.
- The `main` branch is intended to stay stable and should be updated through pull requests.
- Successful pushes to `main` automatically bump the manifest version in `custom_components/hellofresh/manifest.json` using `major.minor` format, create a matching git tag, and publish a GitHub release.
- Lovelace card versions are **not** bumped by hand. `frontend.py` reads the release version from `manifest.json` and stamps it as the `?v=` cache-bust on every card resource URL, so the automatic manifest bump on each release invalidates cached card JS by itself — do not add per-card version constants. On startup the integration updates any already-registered resource whose `?v=` is stale, and each card's console banner reads its version from its own script URL (`import.meta.url`), so the cards must remain ES modules. The diagnostics export's `frontend` block shows expected vs. registered resource URLs for debugging stale installs (see `tests/test_frontend.py`).
