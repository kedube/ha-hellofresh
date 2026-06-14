# Quality Scale Target

This custom integration is aiming for Home Assistant's Silver quality scale over time.

## Current Status

- Config flow setup and reauthentication are implemented.
- Diagnostics are implemented with sensitive account, address, and token values redacted.
- Tests cover the API parsing, normalization, entity behavior, and token lifecycle helpers.
- Repairs issues are raised for payload-shape changes, fallback menu behavior, and unsupported write actions.
- GitHub Actions run pytest, Hassfest, and HACS validation.

## Remaining Work

- Expand end-to-end testing with Home Assistant's integration test helpers.
- Document any remaining user-facing limitations as HelloFresh changes its private web API.
- Continue improving regional coverage for write actions before claiming a higher quality scale.
