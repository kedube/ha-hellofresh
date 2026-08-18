"""Contract tests for the plan (box size) change, verified by HAR capture 43.

Capture 43 records the web app's "change plan" screen, which exposed two endpoints the
integration did not implement at all:

    GET   /gw/api/subscriptions/{id}/product_options?country=US&locale=en-US   -> catalog
    PATCH /gw/api/plans/{planId}?country=US&locale=en-US                       -> 204 No Content
          {"productHandle": "US-CBU-3-2-0"}

The capture changes the box from 3 meals/2 people to 3 meals/3 people and back, and a
``GET /gw/api/customers/me/subscriptions`` immediately after shows ``product.sku`` reflecting the
change -- so the PATCH commits and is readable on the next poll.

Two things here are easy to get wrong and are pinned below:

1. This PATCH sends **uppercase** ``country=US``, while the delivery-weekday PATCH on
   ``/gw/api/subscriptions/{id}`` (capture 42) sends **lowercase** ``country=us``. Both are as
   captured. Normalizing them to match each other would be an untested deviation on a billing-
   affecting write.
2. The response is ``204 No Content``. There is no body to parse, so nothing may be merged into
   state -- the coordinator re-polls.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.hellofresh.client import HelloFreshClient
from custom_components.hellofresh.models import (
    HelloFreshError,
    HelloFreshNotImplementedError,
    HelloFreshSubscription,
)

SUBSCRIPTION_ID = "6959884"
PLAN_ID = "1e989989-eb15-49b3-94e2-a089bc0e2082"

# Verbatim slice of the capture-43 product_options body (the API returns largest-first and is
# not monotonic, which is why ordering is asserted below).
CATALOG = {
    "items": [
        {
            "handle": "classic-box-unified",
            "name": "Classic Box",
            "products": [
                {
                    "handle": "US-CBU-4-6-0",
                    "name": "Classic - 4 meals per week for 6 people",
                    "specs": {"meals": 4, "size": 6, "recurrency": 0},
                    "price": 23976,
                },
                {
                    "handle": "US-CBU-2-2-0",
                    "name": "Classic - 2 meals per week for 2 people",
                    "specs": {"meals": 2, "size": 2, "recurrency": 0},
                    "price": 4996,
                },
                {
                    "handle": "US-CBU-3-2-0",
                    "name": "Classic - 3 meals per week for 2 people",
                    "specs": {"meals": 3, "size": 2, "recurrency": 0},
                    "price": 6594,
                },
            ],
        }
    ]
}


def _subscription(*, plan_id: str | None = PLAN_ID) -> HelloFreshSubscription:
    return HelloFreshSubscription(
        subscription_id=SUBSCRIPTION_ID,
        display_name="Classic Plan",
        status="ACTIVE",
        locale="en-US",
        raw={"customerPlanId": plan_id} if plan_id else {},
    )


def _client(*, payload=None):
    client = HelloFreshClient(session=object())  # type: ignore[arg-type]
    sent: list[dict] = []

    async def fake_request(method, path, *, params=None, json_payload=None, **_kw):
        sent.append({"method": method, "path": path, "params": params, "json": json_payload})
        return SimpleNamespace(status=204 if method == "PATCH" else 200)

    async def fake_json(_response):
        return payload if payload is not None else CATALOG

    client._async_api_request = fake_request  # type: ignore[method-assign]
    client._async_response_json = fake_json  # type: ignore[method-assign]
    client._cached_subscriptions = [_subscription()]
    client._country = "us"
    return client, sent


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


# --- the catalog read ---------------------------------------------------------------


def test_plan_options_requests_the_captured_endpoint() -> None:
    client, sent = _client()
    _run(client.async_list_plan_options())
    assert sent[0]["method"] == "GET"
    assert sent[0]["path"] == f"/gw/api/subscriptions/{SUBSCRIPTION_ID}/product_options"
    assert sent[0]["params"] == {"country": "US", "locale": "en-US"}


def test_plan_options_flattens_products_with_specs_and_price() -> None:
    client, _ = _client()
    options = _run(client.async_list_plan_options())

    by_handle = {option["handle"]: option for option in options}
    assert set(by_handle) == {"US-CBU-4-6-0", "US-CBU-2-2-0", "US-CBU-3-2-0"}

    current = by_handle["US-CBU-3-2-0"]
    assert current["meals"] == 3
    assert current["servings"] == 2
    assert current["family_handle"] == "classic-box-unified"
    assert current["name"] == "Classic - 3 meals per week for 2 people"


def test_prices_are_converted_from_cents() -> None:
    """The API reports integer cents; 6594 is $65.94, not $6594."""
    client, _ = _client()
    options = {o["handle"]: o for o in _run(client.async_list_plan_options())}
    assert options["US-CBU-3-2-0"]["price_cents"] == 6594
    assert options["US-CBU-3-2-0"]["price"] == 65.94


def test_options_are_sorted_smallest_box_first() -> None:
    """The API's own order starts at the largest box and is not monotonic."""
    client, _ = _client()
    options = _run(client.async_list_plan_options())
    assert [o["handle"] for o in options] == [
        "US-CBU-2-2-0",
        "US-CBU-3-2-0",
        "US-CBU-4-6-0",
    ]


def test_malformed_catalog_entries_are_skipped_not_fatal() -> None:
    """A product with no handle can't be switched to, so it is dropped rather than raising."""
    client, _ = _client(
        payload={
            "items": [
                {"handle": "f", "products": [{"name": "no handle"}, "junk"]},
                "not-a-family",
            ]
        }
    )
    assert _run(client.async_list_plan_options()) == []


# --- the plan change ----------------------------------------------------------------


def test_change_plan_matches_the_captured_patch() -> None:
    client, sent = _client()
    _run(client.async_change_plan("US-CBU-3-3-0"))

    assert len(sent) == 1
    request = sent[0]
    assert request["method"] == "PATCH"
    assert request["path"] == f"/gw/api/plans/{PLAN_ID}"
    assert request["json"] == {"productHandle": "US-CBU-3-3-0"}


def test_change_plan_sends_uppercase_country_unlike_the_weekday_patch() -> None:
    """Capture 43 sends `country=US` here; capture 42 sends `country=us` for the weekday change.

    Making these two consistent with each other is a plausible future edit and would deviate from
    both captures on a write that changes billing.
    """
    client, sent = _client()
    _run(client.async_change_plan("US-CBU-3-3-0"))
    assert sent[0]["params"] == {"country": "US", "locale": "en-US"}


def test_change_plan_does_not_parse_the_204_response() -> None:
    """204 No Content: there is no body, so nothing may be read back into state."""
    client, _ = _client()
    read: list[str] = []
    client._async_response_json = lambda *_a, **_kw: read.append("read")  # type: ignore[method-assign]
    _run(client.async_change_plan("US-CBU-3-3-0"))
    assert read == []


def test_change_plan_trims_and_requires_a_handle() -> None:
    client, sent = _client()
    _run(client.async_change_plan("  US-CBU-3-3-0  "))
    assert sent[0]["json"] == {"productHandle": "US-CBU-3-3-0"}

    with pytest.raises(HelloFreshError):
        _run(client.async_change_plan("   "))
    assert len(sent) == 1, "a blank handle must not reach the network"


def test_change_plan_without_a_plan_id_raises_not_implemented() -> None:
    client, sent = _client()
    client._cached_subscriptions = [_subscription(plan_id=None)]
    with pytest.raises(HelloFreshNotImplementedError):
        _run(client.async_change_plan("US-CBU-3-3-0"))
    assert sent == []


def test_plan_change_is_independent_of_the_weekday_change() -> None:
    """The capture pairs them, but they are separate requests; neither implies the other.

    Coupling them would make a box-size change silently rewrite the delivery day.
    """
    client, sent = _client()
    _run(client.async_change_plan("US-CBU-3-3-0"))
    assert [r["path"] for r in sent] == [f"/gw/api/plans/{PLAN_ID}"]
    assert not any("deliveryTime" in str(r["json"]) for r in sent)
