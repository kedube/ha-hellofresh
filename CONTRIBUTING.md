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

## Project layout

- `custom_components/hellofresh/` contains the integration code.
- `tests/` contains the pytest suite.
- `docs/` contains user reference documentation split out of the README (e.g. the [entity reference](docs/entities.md)) — update it alongside entity changes.
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
