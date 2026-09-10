"""Weekly discounts: wallet promises (``benefit-distribution``) and realized coupon lines."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

from homeassistant.components.diagnostics import async_redact_data

from custom_components.hellofresh.api import (
    HelloFreshAccountData,
    HelloFreshClient,
    HelloFreshError,
    HelloFreshOrder,
    HelloFreshSubscription,
    HelloFreshWeek,
)
from custom_components.hellofresh.diagnostics import TO_REDACT
from custom_components.hellofresh.sensor_helpers import (
    sensor_extra_state_attributes,
    sensor_native_value,
)

TODAY = date.today()
NEXT_WEEK = TODAY + timedelta(days=5)
WEEK_AFTER = TODAY + timedelta(days=12)
PROMISE_ID = "5bd74469-69cd-55d6-8217-e24e25df8839"

# Verbatim shape from capture 51 (ids changed): one $10-off-premium-meals voucher, available on
# the next box only.
WALLET_RESPONSE = {
    "deliveries": [
        {
            "hfWeek": "2026-W38",
            "units": [
                {"promiseId": PROMISE_ID, "status": "available", "box": None, "alternatives": []}
            ],
        },
        {"hfWeek": "2026-W39", "units": []},
    ],
    "promises": [
        {
            "id": PROMISE_ID,
            "voucherCode": "RX-9BNR6",
            "source": "unspecified",
            "expirationDateTime": "2026-09-09T23:59:59-0700",
            "units": [
                {
                    "box": None,
                    "benefits": [
                        {
                            "id": "398285ec-ba2a-4473-bdcb-eec97a2eb693",
                            "applicableTo": "premiumSurcharge",
                            "budget": {"type": "fixed", "value": 1000},
                        }
                    ],
                    "used": None,
                }
            ],
            "unlimited": True,
            "type": "premiumSurcharge",
            "campaign": {"type": "default"},
            "storeSlug": None,
            "attachmentDateTime": "2026-08-09T13:34:00Z",
        }
    ],
}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _client() -> HelloFreshClient:
    return HelloFreshClient(session=object(), access_token="token")  # type: ignore[arg-type]


def _weeks() -> list[HelloFreshWeek]:
    def week(week_id: str, delivery: date, skipped: bool = False) -> HelloFreshWeek:
        return HelloFreshWeek(
            week_id=week_id,
            display_name=week_id,
            subscription_id="6959884",
            delivery_date=delivery,
            is_skipped=skipped,
            raw={
                "id": week_id,
                "state": "RUNNING",
                "status": "RUNNING",
                "cutoffDate": "2026-09-09T23:59:59-0700",
                "product": {"handle": "US-CBU-3-2-0"},
            },
        )

    return [
        week("2026-W37", TODAY - timedelta(days=2)),  # delivered: not sent
        week("2026-W38", NEXT_WEEK),
        week("2026-W39", WEEK_AFTER),
    ]


def test_wallet_benefits_land_on_weeks_and_the_next_box() -> None:
    client = _client()
    calls: list[tuple] = []

    async def fake_request(method, path, params=None, json_payload=None, extra_headers=None, **_):
        calls.append((method, path, params, json_payload, extra_headers))
        return SimpleNamespace(status=200)

    async def fake_json(_response):
        return WALLET_RESPONSE

    client._async_api_request = fake_request  # type: ignore[method-assign]
    client._async_response_json = fake_json  # type: ignore[method-assign]
    data = HelloFreshAccountData(next_delivery_total_currency="USD")
    weeks = _weeks()
    subscription = HelloFreshSubscription(subscription_id="6959884", raw={})
    _run(client._async_enrich_wallet_benefits(data, [subscription], weeks))

    # The request mirrors the site: upcoming weeks only, with state, cutoff and box handle.
    assert len(calls) == 1
    method, path, params, body, headers = calls[0]
    assert (method, path) == ("POST", "/gw/customer-wallet/v2/benefit-distribution")
    assert params == {"externalPartnerBenefits": "true", "subscription": "6959884"}
    assert headers == {"x-requested-by": "upselling"}
    assert [d["delivery"]["hfWeek"] for d in body["deliveries"]] == ["2026-W38", "2026-W39"]
    assert body["deliveries"][0] == {
        "delivery": {
            "hfWeek": "2026-W38",
            "state": "RUNNING",
            "cutoffDateTime": "2026-09-09T23:59:59-0700",
        },
        "selections": {"mealboxHandle": "US-CBU-3-2-0"},
        "subscriptionId": 6959884,
    }

    # The promise is attached to the week it is available for, and nowhere else.
    by_id = {week.week_id: week for week in weeks}
    assert by_id["2026-W39"].benefits == []
    assert by_id["2026-W37"].benefits == []
    (benefit,) = by_id["2026-W38"].benefits
    assert benefit["label"] == "$10 off premium meals"
    assert benefit["status"] == "available"
    assert benefit["amount"] == 10.0
    assert benefit["amount_type"] == "fixed"
    assert benefit["applies_to"] == "premiumSurcharge"
    assert benefit["voucher_code"] == "RX-9BNR6"
    assert benefit["one_time"] is False
    assert benefit["expires_at"].startswith("2026-09-09T23:59:59")
    assert "weeks" not in benefit
    # The week summary (what get_weeks and the cards read) carries it.
    assert by_id["2026-W38"].as_summary_dict()["benefits"] == [benefit]

    # Account-level: every promise with its available weeks, and the next box's discount.
    assert [p["weeks"] for p in data.wallet_benefits] == [["2026-W38"]]
    assert data.next_box_discount == {**benefit, "week_id": "2026-W38"}

    # The debug trace counts things but never carries the code.
    (attempt,) = client._debug_trace["wallet_attempts"]
    assert attempt["promises"] == 1 and attempt["weeks_with_benefits"] == 1
    assert "RX-9BNR6" not in repr(attempt)


def test_wallet_skips_a_skipped_week_for_the_next_box_discount() -> None:
    client = _client()
    response = {
        "deliveries": [
            {"hfWeek": "2026-W38", "units": [{"promiseId": PROMISE_ID, "status": "available"}]},
            {"hfWeek": "2026-W39", "units": [{"promiseId": PROMISE_ID, "status": "available"}]},
        ],
        "promises": WALLET_RESPONSE["promises"],
    }

    async def fake_request(*_a, **_k):
        return SimpleNamespace(status=200)

    async def fake_json(_response):
        return response

    client._async_api_request = fake_request  # type: ignore[method-assign]
    client._async_response_json = fake_json  # type: ignore[method-assign]
    weeks = _weeks()
    weeks[1].is_skipped = True
    data = HelloFreshAccountData()
    _run(
        client._async_enrich_wallet_benefits(
            data, [HelloFreshSubscription(subscription_id="6959884", raw={})], weeks
        )
    )
    assert data.next_box_discount["week_id"] == "2026-W39"
    assert data.wallet_benefits[0]["weeks"] == ["2026-W38", "2026-W39"]


def test_wallet_failure_leaves_the_discount_unknown() -> None:
    client = _client()

    async def fake_request(*_a, **_k):
        raise HelloFreshError("wallet down")

    client._async_api_request = fake_request  # type: ignore[method-assign]
    data = HelloFreshAccountData()
    _run(
        client._async_enrich_wallet_benefits(
            data, [HelloFreshSubscription(subscription_id="6959884", raw={})], _weeks()
        )
    )
    assert data.next_box_discount is None
    assert data.wallet_benefits == []
    assert client._debug_trace["wallet_attempts"][0]["error"] == "wallet down"


def test_benefit_labels_read_like_the_site() -> None:
    label = HelloFreshClient._benefit_label
    assert label("premiumSurcharge", 10.0, "fixed", "USD") == "$10 off premium meals"
    assert label("premiumSurcharge", 7.5, "fixed", "EUR") == "€7.50 off premium meals"
    assert label("shipping", 15.0, "percent", "USD") == "15% off shipping"
    assert label("mealbox", 20.0, "fixed", "SEK") == "20 SEK off your box"
    assert label("addOnSurcharge", 5.0, "fixed", "GBP") == "£5 off add on surcharge"
    assert label("premiumSurcharge", None, None, "USD") == "Voucher for premium meals"
    assert label(None, 5.0, "fixed", "USD") == "$5 off"


def test_next_box_discount_sensor_value_and_attributes() -> None:
    empty = HelloFreshAccountData().finalize()
    assert sensor_native_value("next_box_discount", empty, "https://example") is None
    assert sensor_extra_state_attributes("next_box_discount", empty)["benefits"] == []

    benefit = {
        "promise_id": PROMISE_ID,
        "voucher_code": "RX-9BNR6",
        "applies_to": "premiumSurcharge",
        "amount": 10.0,
        "amount_type": "fixed",
        "currency": "USD",
        "label": "$10 off premium meals",
        "one_time": False,
        "expires_at": "2026-09-09T23:59:59-07:00",
        "status": "available",
        "week_id": "2026-W38",
    }
    data = HelloFreshAccountData(
        next_box_discount=benefit, wallet_benefits=[{**benefit, "weeks": ["2026-W38"]}]
    ).finalize()
    assert sensor_native_value("next_box_discount", data, "https://example") == 10.0
    attributes = sensor_extra_state_attributes("next_box_discount", data)
    assert attributes["label"] == "$10 off premium meals"
    assert attributes["week_id"] == "2026-W38"
    assert attributes["voucher_code"] == "RX-9BNR6"
    assert attributes["benefits"][0]["weeks"] == ["2026-W38"]
    # A shared diagnostics export never carries the code, even nested in the benefits list.
    redacted = async_redact_data({"attributes": attributes}, TO_REDACT)
    assert "RX-9BNR6" not in repr(redacted)


def test_realized_discounts_from_the_billing_ledger() -> None:
    """Order lines' couponMoneyValue become per-delivery discounts, on orders and in spending."""
    client = _client()
    items = [
        {
            "createdAt": "2026-08-12T00:00:00-0700",
            "grandTotal": 29.96,
            "couponCode": "RX-9BNR6",
            "orderLines": [
                {
                    "deliveryDate": "2026-08-17T12:00:00-0700",
                    "subscription": {"id": "6959884"},
                    "couponMoneyValue": 10,
                    "unitPrice": 39.96,
                    "paidPrice": 29.96,
                }
            ],
        },
        {
            "createdAt": "2026-08-12T00:00:00-0700",
            "grandTotal": 76.93,
            "couponCode": None,
            "orderLines": [
                {
                    "deliveryDate": "2026-08-17T12:00:00-0700",
                    "subscription": {"id": "6959884"},
                    "couponMoneyValue": 0,
                }
            ],
        },
        {
            "createdAt": "2026-08-05T00:00:00-0700",
            "grandTotal": 76.93,
            "orderLines": [
                {"deliveryDate": "2026-08-10T12:00:00-0700", "subscription": {"id": "6959884"}}
            ],
        },
    ]
    _, _, _, price_by_key, discount_by_key = client._accumulate_order_prices(items)
    assert discount_by_key == {("6959884", date(2026, 8, 17)): (10.0, "RX-9BNR6")}
    assert round(price_by_key[("6959884", date(2026, 8, 17))][0], 2) == 106.89

    dataset = HelloFreshClient._build_spending_dataset(price_by_key, discount_by_key)
    weeks = {w["delivery_date"]: w for w in dataset["weeks"]}
    assert weeks["2026-08-17"]["discount"] == 10.0
    assert weeks["2026-08-17"]["coupon_code"] == "RX-9BNR6"
    assert weeks["2026-08-10"]["discount"] == 0.0
    assert weeks["2026-08-10"]["coupon_code"] is None
    assert dataset["months"][0]["discount"] == 10.0
    assert dataset["total"]["discount"] == 10.0

    order = HelloFreshOrder(
        order_id="1",
        week_id="2026-W34",
        status="x",
        subscription_id="6959884",
        delivery_date=date(2026, 8, 17),
    )
    client._apply_prices_to_orders([order], price_by_key, discount_by_key)
    assert order.discount_amount == 10.0
    assert order.coupon_code == "RX-9BNR6"
    assert order.as_dict()["discount_amount"] == 10.0

    data = HelloFreshAccountData()
    client._compute_next_delivery_total(
        data, {"6959884": (date(2026, 8, 17), None)}, {}, price_by_key, discount_by_key
    )
    assert data.next_delivery_discount == 10.0
    assert (
        sensor_extra_state_attributes("next_box_total_price", data.finalize())["billed_discount"]
        == 10.0
    )
