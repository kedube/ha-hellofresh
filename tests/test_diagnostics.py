"""Diagnostics redaction tests.

The diagnostics export is meant to be shared on GitHub issues, so PII and stable account
identifiers must never survive redaction — including when they ride along inside the captured
request params in ``debug_trace`` (menu_attempts / history_attempts), which are nested several
levels deep.
"""

from __future__ import annotations

from homeassistant.components.diagnostics import async_redact_data

from custom_components.hellofresh.diagnostics import TO_REDACT


def _redact(payload):
    return async_redact_data(payload, TO_REDACT)


def test_debug_trace_params_redact_pii_and_identifiers() -> None:
    """postcode / customerPlanId / subscription must be redacted inside debug_trace params."""
    diagnostics = {
        "runtime": {
            "debug_trace": {
                "menu_attempts": [
                    {
                        "path": "/gw/my-deliveries/menu",
                        "params": {
                            "postcode": "01930",
                            "customerPlanId": "1e989989-eb15-49b3-94e2-a089bc0e2082",
                            "subscription": "6959884",
                            "product-sku": "US-CBU-3-2-0",
                        },
                    }
                ],
                "history_attempts": [
                    {
                        "path": "/gw/my-deliveries/past-deliveries",
                        "params": {"subscription": "6959884", "postalCode": "01930"},
                    }
                ],
            }
        }
    }

    redacted = _redact(diagnostics)
    menu_params = redacted["runtime"]["debug_trace"]["menu_attempts"][0]["params"]
    history_params = redacted["runtime"]["debug_trace"]["history_attempts"][0]["params"]

    assert menu_params["postcode"] == "**REDACTED**"
    assert menu_params["customerPlanId"] == "**REDACTED**"
    assert menu_params["subscription"] == "**REDACTED**"
    assert history_params["subscription"] == "**REDACTED**"
    assert history_params["postalCode"] == "**REDACTED**"
    # Non-PII generic box/plan SKU is preserved (useful for debugging box-type issues).
    assert menu_params["product-sku"] == "US-CBU-3-2-0"


def test_tokens_and_credentials_still_redacted() -> None:
    """The original token/credential redaction must remain intact."""
    diagnostics = {
        "config_entry": {
            "data": {
                "access_token": "secret-access",
                "refresh_token": "secret-refresh",
                "username": "user@example.com",
                "password": "hunter2",
            }
        }
    }

    data = _redact(diagnostics)["config_entry"]["data"]
    assert data["access_token"] == "**REDACTED**"
    assert data["refresh_token"] == "**REDACTED**"
    assert data["username"] == "**REDACTED**"
    assert data["password"] == "**REDACTED**"
