# Quality Scale Target

`manifest.json` declares `"quality_scale": "custom"`. That is the only accurate value
available: Home Assistant's loader hardcodes every custom integration to `custom` at runtime
regardless of what the manifest says (`Integration.quality_scale` returns `"custom"` when
`not is_built_in`), so declaring `silver` here would pass hassfest and still be ignored by
Home Assistant. The tiered scales are reserved for integrations merged into core.

The Silver *rules* remain the engineering target — they are a good checklist whether or not
the badge is claimable — and the status below tracks them on that basis.

## Current Status

- Config flow setup (email/password and token paths) and reauthentication are implemented, plus an options flow for refresh interval, history window, menu grace window, and the public-menu fallback toggle. Options changes reload the entry automatically.
- Reconfiguration (`async_step_reconfigure`) lets the country and credentials (or token) be corrected without deleting the entry; it refuses to repoint an entry at a different HelloFresh account.
- Diagnostics are implemented with sensitive account, address, and token values redacted (by key name at any nesting depth, including captured request params), and include token-health timing and a `frontend` block comparing expected vs. registered card resource versions.
- Repairs issues are raised for payload-shape changes, fallback menu behavior, and unsupported write actions.
- Entities use `has_entity_name`, device classes where applicable, and diagnostic entity categories; user-facing strings live in `strings.json`/`translations/en.json`.
- The test suite (250+ tests) covers API parsing and normalization, entity behavior, config and options flow, diagnostics redaction, the token-refresh lifecycle (including a simulation harness), the TLS transport, and frontend resource registration/versioning.
- GitHub Actions run ruff, pytest, Hassfest, and HACS validation on every push; releases are tagged and published automatically with the manifest version bump.

## Remaining Work

- Expand end-to-end testing with Home Assistant's integration test helpers (`hass` fixtures / `pytest-homeassistant-custom-component`) rather than direct client/normalizer calls.
- Document any remaining user-facing limitations as HelloFresh changes its private web API.
- Continue improving regional coverage for write actions.
