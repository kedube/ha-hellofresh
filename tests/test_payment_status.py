"""Payment-method health (``checktokenstatus``) and the price breakdowns."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from custom_components.hellofresh.api import HelloFreshAccountData, HelloFreshClient
from custom_components.hellofresh.binary_sensor import SENSORS, HelloFreshBinarySensor

# Shape verbatim from the capture (values changed); the card number and billing address are
# exactly what must NOT survive parsing.
TOKEN_RESPONSE = {
    "isTokenExpiring": True,
    "isTokenExpired": False,
    "primaryToken": {
        "type": "credit_card",
        "provider": "braintree",
        "method": "card",
        "is_active": True,
        "details": {"expiry_month": "05", "expiry_year": "2027", "number": "4242"},
        "billing_address": {"city": "Boston", "postcode": "02101", "full_address": "1 Main St"},
    },
}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _client() -> HelloFreshClient:
    return HelloFreshClient(session=object(), access_token="token")  # type: ignore[arg-type]


def test_payment_status_keeps_flags_and_card_type_but_never_the_number() -> None:
    client = _client()
    calls: list[tuple] = []

    async def fake_request(method, path, params=None, json_payload=None, extra_headers=None, **_):
        calls.append((method, path, params, json_payload, extra_headers))
        return SimpleNamespace(status=200)

    async def fake_json(_response):
        return TOKEN_RESPONSE

    client._async_api_request = fake_request  # type: ignore[method-assign]
    client._async_response_json = fake_json  # type: ignore[method-assign]
    data = HelloFreshAccountData()
    _run(client._async_enrich_payment_method_status(data))

    assert calls == [
        (
            "POST",
            "/gw/payments/v1/checktokenstatus",
            {"country": "US"},
            None,
            {"x-requested-by": "gateway"},
        )
    ]
    assert data.payment_method_expiring is True
    assert data.payment_method_expired is False
    assert data.payment_card_type == "credit_card"
    assert data.payment_card_provider == "braintree"
    assert data.payment_card_expiry == "2027-05"
    dumped = repr(data)
    assert "4242" not in dumped
    assert "Boston" not in dumped
    assert "02101" not in dumped


def test_payment_status_failure_leaves_the_sensor_unknown() -> None:
    from custom_components.hellofresh.api import HelloFreshError

    client = _client()

    async def fake_request(*_args, **_kwargs):
        raise HelloFreshError("gateway down")

    client._async_api_request = fake_request  # type: ignore[method-assign]
    data = HelloFreshAccountData()
    _run(client._async_enrich_payment_method_status(data))
    assert data.payment_method_expiring is None
    assert data.payment_card_expiry is None


def test_price_breakdown_from_calculate() -> None:
    breakdown = HelloFreshClient._price_breakdown_from_calculate(
        {
            "grandTotal": 76.93,
            "subTotal": "65.94",
            "shippingAmount": 10.99,
            "discountAmount": 0,
            "couponCode": "",
        }
    )
    assert breakdown == {
        "sub_total": 65.94,
        "shipping_amount": 10.99,
        "discount_amount": 0.0,
        "tax_amount": None,
        "grand_total": 76.93,
        "coupon_code": None,
    }
    assert HelloFreshClient._price_breakdown_from_calculate({"currency": "USD"}) is None


def _binary(data: HelloFreshAccountData) -> HelloFreshBinarySensor:
    description = next(d for d in SENSORS if d.key == "payment_method_expiring")
    coordinator = SimpleNamespace(
        data=data,
        config_entry=SimpleNamespace(entry_id="entry-1", title="HelloFresh"),
        last_update_success=True,
    )
    return HelloFreshBinarySensor(coordinator, description)


def test_payment_binary_sensor_states_and_attributes() -> None:
    unknown = _binary(HelloFreshAccountData().finalize())
    assert unknown.entity_id == "binary_sensor.hellofresh_payment_method_expiring"
    assert unknown.available is False

    fine = _binary(
        HelloFreshAccountData(
            payment_method_expiring=False,
            payment_method_expired=False,
            payment_card_type="credit_card",
            payment_card_expiry="2028-01",
        ).finalize()
    )
    assert fine.available is True
    assert fine.is_on is False
    assert fine.icon == "mdi:credit-card-check-outline"
    assert fine.extra_state_attributes == {
        "expiring": False,
        "expired": False,
        "card_type": "credit_card",
        "card_provider": None,
        "card_expiry": "2028-01",
    }

    expired = _binary(
        HelloFreshAccountData(payment_method_expiring=False, payment_method_expired=True).finalize()
    )
    assert expired.is_on is True
    assert expired.icon == "mdi:credit-card-clock-outline"
