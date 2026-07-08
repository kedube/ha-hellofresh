# Quality Scale Target

This custom integration is aiming for Home Assistant's Silver quality scale over time.

## Current Status

- Config flow setup (email/password and token paths) and reauthentication are implemented, plus an options flow for refresh interval, history window, menu grace window, and the public-menu fallback toggle. Options changes reload the entry automatically.
- Diagnostics are implemented with sensitive account, address, and token values redacted (by key name at any nesting depth, including captured request params), and include token-health timing and a `frontend` block comparing expected vs. registered card resource versions.
- Repairs issues are raised for payload-shape changes, fallback menu behavior, and unsupported write actions.
- Entities use `has_entity_name`, device classes where applicable, and diagnostic entity categories; user-facing strings live in `strings.json`/`translations/en.json`.
- The test suite (250+ tests) covers API parsing and normalization, entity behavior, config and options flow, diagnostics redaction, the token-refresh lifecycle (including a simulation harness), the TLS transport, and frontend resource registration/versioning.
- GitHub Actions run ruff, pytest, Hassfest, and HACS validation on every push; releases are tagged and published automatically with the manifest version bump.

## Remaining Work

- Expand end-to-end testing with Home Assistant's integration test helpers (`hass` fixtures / `pytest-homeassistant-custom-component`) rather than direct client/normalizer calls.
- Document any remaining user-facing limitations as HelloFresh changes its private web API.
- Continue improving regional coverage for write actions before claiming a higher quality scale.
