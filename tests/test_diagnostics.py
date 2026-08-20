"""Diagnostics redaction tests.

The diagnostics export is meant to be shared on GitHub issues, so PII and stable account
identifiers must never survive redaction — including when they ride along inside the captured
request params in ``debug_trace`` (menu_attempts / history_attempts), which are nested several
levels deep.
"""

from __future__ import annotations

from homeassistant.components.diagnostics import async_redact_data

from custom_components.hellofresh.api import HelloFreshClient
from custom_components.hellofresh.diagnostics import TO_REDACT
from custom_components.hellofresh.normalizers import _template_debug_path


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


def test_new_identifier_keys_redacted() -> None:
    """customerUUID / public_id / coupon_code must not survive redaction."""
    diagnostics = {
        "runtime": {
            "debug_trace": {
                "payment_attempts": [
                    {
                        "path": "/gw/payments/balance/transactions",
                        "params": {"customerUUID": "cust-uuid-123", "types": "DEBIT"},
                    }
                ],
                "tracking_attempts": [{"path": "/gw/scm/...", "public_id": "track-uuid-456"}],
            },
            "subscriptions": [{"coupon_code": "HF-VOUCHER-789"}],
        }
    }

    redacted = _redact(diagnostics)
    trace = redacted["runtime"]["debug_trace"]
    assert trace["payment_attempts"][0]["params"]["customerUUID"] == "**REDACTED**"
    assert trace["tracking_attempts"][0]["public_id"] == "**REDACTED**"
    assert redacted["runtime"]["subscriptions"][0]["coupon_code"] == "**REDACTED**"


def test_template_debug_path_strips_account_identifiers() -> None:
    """Ids baked into a path string (unreachable by key-name redaction) are templated out."""
    cases = {
        "/gw/api/subscriptions/6959884/delivery_dates/2026-W25": (
            "/gw/api/subscriptions/{id}/delivery_dates/2026-W25"
        ),
        "/gw/api/subscriptions/6959884/oneoff": "/gw/api/subscriptions/{id}/oneoff",
        "/gw/api/customers/me/subscriptions/6959884/deliveries": (
            "/gw/api/customers/me/subscriptions/{id}/deliveries"
        ),
        "/gw/api/plans/1e989989-eb15-49b3-94e2-a089bc0e2082/changePlanDeliveryDetails": (
            "/gw/api/plans/{id}/changePlanDeliveryDetails"
        ),
        "/gw/payments/customers/cust-uuid-123/balance": ("/gw/payments/customers/{id}/balance"),
        "/gw/scm/tracking-ids/track/public-id/track-uuid-456": (
            "/gw/scm/tracking-ids/track/public-id/{id}"
        ),
        # A non-identifier path (ISO week) is left untouched.
        "/gw/v1/carts/2026-W25": "/gw/v1/carts/2026-W25",
        # The balance regex must NOT fire on /customers/me/subscriptions/... (no /balance).
        "/gw/api/customers/me/info": "/gw/api/customers/me/info",
    }
    for raw, expected in cases.items():
        assert _template_debug_path(raw) == expected, raw


def test_record_debug_attempt_templates_path() -> None:
    """The recorder itself templates the path, so the stored trace never holds a raw id."""
    client = HelloFreshClient(session=None)  # type: ignore[arg-type]
    client._record_debug_attempt(
        "delivery_attempts",
        {
            "path": "/gw/api/customers/me/subscriptions/6959884/deliveries",
            "subscription_id": "6959884",
        },
    )
    stored = client._debug_trace["delivery_attempts"][0]
    assert stored["path"] == "/gw/api/customers/me/subscriptions/{id}/deliveries"
    # The separate subscription_id KEY is left for key-name redaction to scrub downstream.
    assert stored["subscription_id"] == "6959884"


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


def test_payment_descriptors_redacted() -> None:
    """Subscription payment method/gateway fields never reach a diagnostics export."""
    diagnostics = {
        "subscriptions": [
            {
                "payment_method": "visa **** 4242",
                "payment_gateway": "braintree",
                "paymentMethod": "PayPal user@example.com",
                "paymentGateway": "paypal",
            }
        ]
    }

    redacted = _redact(diagnostics)["subscriptions"][0]
    assert redacted["payment_method"] == "**REDACTED**"
    assert redacted["payment_gateway"] == "**REDACTED**"
    assert redacted["paymentMethod"] == "**REDACTED**"
    assert redacted["paymentGateway"] == "**REDACTED**"
