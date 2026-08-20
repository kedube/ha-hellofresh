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
```

Formatting is enforced, so run `ruff format .` before committing rather than hand-aligning code.

### Why the card checks exist

The Lovelace cards under `custom_components/hellofresh/www/` are several thousand lines of
hand-written JavaScript that the Python test suite cannot reach. A past-week browsing regression
once shipped precisely because nothing validated them. Two guards now cover that gap:

- **`check_card_syntax.py`** proves each card parses as an ES module, so a typo cannot reach users
  as a blank dashboard panel. It blanks `import` lines before checking (preserving line numbers)
  because `node --check` would otherwise fail resolving the shared modules.
- **`check_card_logic.mjs`** extracts the cards' real `_browsableWeeks` from the shipped sources
  and exercises it. The load-bearing assertion is that the **Market and meal-planner cards expose
  the same past weeks** — they read the same `get_weeks` response and share a week cursor, so any
  divergence is a bug. Add a case here when you change week filtering.

Because the logic tests parse the card sources with a regex, renaming `_browsableWeeks` or
reindenting it will break extraction — the script fails loudly rather than silently passing.

## Project layout

- `custom_components/hellofresh/` contains the integration code.
- `custom_components/hellofresh/www/` contains the Lovelace cards (plain ES modules, no build step).
- `tests/` contains the pytest suite. `tests/test_repo_consistency.py` pins hand-edited metadata
  (HACS country list, translation completeness, `services.yaml`, card registration) that otherwise
  drifts out of step with the code.
- `.github/scripts/` contains the CI helper scripts, including the card checks described above.
- `docs/` contains the user reference documentation split out of the README. Keep the README as the
  narrative landing page (install → configure → what you get → troubleshoot) and put detail here:
  - [`docs/entities.md`](docs/entities.md) — every sensor, binary sensor, switch and button.
  - [`docs/cards.md`](docs/cards.md) — the seven Lovelace cards. Options shared by every card live
    in its **Common options** table, so per-card examples stay minimal; don't repeat them.
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
