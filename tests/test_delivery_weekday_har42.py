"""Contract tests for the recurring delivery-weekday change, verified by HAR capture 42.

This was the integration's **last unverified write path**. Until capture 42 it was implemented
against an inferred ``POST /gw/api/plans/{planId}/changePlanDeliveryDetails`` that appears in none
of the 16 retained captures. Capture 42 records the real web app performing the change twice
(Tuesday → Monday, then back) and it uses a completely different endpoint:

    PATCH /gw/api/subscriptions/6959884?country=us&locale=en-US
    {"subscription": {"id": "6959884", "deliveryTime": "US-2-0800-2000"}}

Two non-obvious properties of the captured exchange are pinned here, because both are the kind of
thing a later "improvement" would plausibly get wrong:

1. The 200 response echoes the **pre-change** ``deliveryTime``. In the capture, the PATCH sending
   ``US-2`` returns ``US-1``, and a ``GET`` one second later returns ``US-2``. The write commits
   immediately; only the response body is stale. Adopting it would roll state back.
2. ``deliveryInterval`` is not part of the request at all -- the web app sends only the handle.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.hellofresh.client import HelloFreshClient
from custom_components.hellofresh.models import (
    HelloFreshAuthError,
    HelloFreshError,
    HelloFreshNotImplementedError,
    HelloFreshSubscription,
)

SUBSCRIPTION_ID = "6959884"
# Verbatim from HAR 42: the two handles the capture toggles between.
TUESDAY = "US-2-0800-2000"
MONDAY = "US-1-0800-2000"


def _subscription(*, plan_id: str | None = "1e989989-eb15-49b3-94e2-a089bc0e2082"):
    raw = {"customerPlanId": plan_id} if plan_id else {}
    return HelloFreshSubscription(
        subscription_id=SUBSCRIPTION_ID,
        display_name="Classic Plan",
        status="ACTIVE",
        locale="en-US",
        raw=raw,
    )


def _client(*, fail_patch: bool = False, auth_fail: bool = False):
    """A client whose transport records requests instead of sending them."""
    client = HelloFreshClient(session=object())  # type: ignore[arg-type]
    sent: list[dict] = []

    async def fake_request(method, path, *, params=None, json_payload=None, **_kw):
        sent.append({"method": method, "path": path, "params": params, "json": json_payload})
        if method == "PATCH" and auth_fail:
            raise HelloFreshAuthError("token expired")
        if method == "PATCH" and fail_patch:
            raise HelloFreshError("rejected")
        return SimpleNamespace(status=200)

    client._async_api_request = fake_request  # type: ignore[method-assign]
    client._cached_subscriptions = [_subscription()]
    client._country = "us"
    return client, sent


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def test_weekday_change_matches_the_captured_patch() -> None:
    """The verified path: PATCH the subscription with a `subscription` envelope."""
    client, sent = _client()

    _run(client.async_change_delivery_weekday(TUESDAY))

    assert len(sent) == 1, "the verified PATCH must succeed without touching the fallback"
    request = sent[0]
    assert request["method"] == "PATCH"
    assert request["path"] == f"/gw/api/subscriptions/{SUBSCRIPTION_ID}"
    assert request["json"] == {"subscription": {"id": SUBSCRIPTION_ID, "deliveryTime": TUESDAY}}


def test_country_param_is_lowercase_as_captured() -> None:
    """HAR 42 sends `country=us`, not `country=US`.

    Every other endpoint in the integration sends the uppercase ISO code, so uppercasing this one
    "for consistency" is a plausible future edit -- and an untested deviation on a write path.
    """
    client, sent = _client()
    _run(client.async_change_delivery_weekday(MONDAY))
    assert sent[0]["params"] == {"country": "us", "locale": "en-US"}


def test_handle_is_passed_through_unaltered() -> None:
    """Handles come from `delivery_dates_options`; they are never constructed locally."""
    client, sent = _client()
    _run(client.async_change_delivery_weekday(f"  {MONDAY}  "))
    assert sent[0]["json"]["subscription"]["deliveryTime"] == MONDAY


def test_a_blank_handle_fails_before_any_request() -> None:
    client, sent = _client()
    with pytest.raises(HelloFreshError):
        _run(client.async_change_delivery_weekday("   "))
    assert sent == []


def test_the_stale_response_body_is_not_adopted() -> None:
    """The 200 echoes the OLD deliveryTime; the client must not read it back.

    In the capture, PATCH(US-2) returned US-1 and the very next GET returned US-2. A future change
    that "saves a poll" by trusting this response would silently revert the user's weekday in the
    UI until the next refresh.
    """
    client, sent = _client()
    captured_stale_body = {"deliveryTime": MONDAY, "deliveryInterval": 1}

    async def fake_json(_response):  # pragma: no cover - must never be called
        return captured_stale_body

    client._async_response_json = fake_json  # type: ignore[method-assign]
    called: list[str] = []
    client._async_response_json = lambda *_a, **_kw: called.append("read")  # type: ignore[method-assign]

    _run(client.async_change_delivery_weekday(TUESDAY))

    assert called == [], "the write response must be discarded, not parsed into state"


def test_delivery_interval_warns_rather_than_silently_dropping(caplog) -> None:
    """The captured request has no interval field, so a non-default value cannot be honoured."""
    client, sent = _client()
    _run(client.async_change_delivery_weekday(TUESDAY, delivery_interval=2))

    assert "deliveryInterval" not in str(sent[0]["json"])
    assert any(
        "delivery_interval" in record.message and record.levelname == "WARNING"
        for record in caplog.records
    ), "a caller asking for an interval must be told it cannot be applied"


def test_default_interval_does_not_warn(caplog) -> None:
    client, _ = _client()
    _run(client.async_change_delivery_weekday(TUESDAY))
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_rejected_patch_falls_back_to_the_legacy_plans_endpoint() -> None:
    """No capture proves the plans endpoint is dead, so it is retained as a fallback."""
    client, sent = _client(fail_patch=True)

    _run(client.async_change_delivery_weekday(TUESDAY, delivery_interval=2))

    assert [request["method"] for request in sent] == ["PATCH", "POST"]
    fallback = sent[1]
    assert fallback["path"] == (
        "/gw/api/plans/1e989989-eb15-49b3-94e2-a089bc0e2082/changePlanDeliveryDetails"
    )
    # The fallback is the one path that can carry an interval.
    assert fallback["json"] == {"deliveryOption": TUESDAY, "deliveryInterval": 2}


def test_auth_failure_is_not_swallowed_by_the_fallback() -> None:
    """An expired token must surface as reauth, not be retried against a second endpoint.

    Falling back on auth errors would turn a recoverable reauth into a confusing second failure.
    """
    client, sent = _client(auth_fail=True)
    with pytest.raises(HelloFreshAuthError):
        _run(client.async_change_delivery_weekday(TUESDAY))
    assert [request["method"] for request in sent] == ["PATCH"]


def test_fallback_without_a_plan_id_raises_not_implemented() -> None:
    client, _ = _client(fail_patch=True)
    client._cached_subscriptions = [_subscription(plan_id=None)]
    with pytest.raises(HelloFreshNotImplementedError):
        _run(client.async_change_delivery_weekday(TUESDAY))


def test_not_implemented_is_not_retried_against_the_legacy_endpoint() -> None:
    """A "this account can't do that" error must surface, not trigger the fallback.

    ``HelloFreshNotImplementedError`` subclasses ``HelloFreshError``, so the broad ``except``
    that drives the legacy fallback used to swallow it. Two things went wrong: a never-captured
    endpoint was called in response to HelloFresh explicitly saying the capability is absent, and
    -- worse -- the service layer raises a Repairs issue from this exact type, so suppressing it
    hid a user-facing signal about an unsupported account.
    """
    client = HelloFreshClient(session=object())  # type: ignore[arg-type]
    calls: list[str] = []

    async def fake_request(method, path, *, params=None, json_payload=None, **_kw):
        calls.append(f"{method} {path}")
        if method == "PATCH":
            raise HelloFreshNotImplementedError("account cannot change delivery weekday")
        return SimpleNamespace(status=200)

    client._async_api_request = fake_request  # type: ignore[method-assign]
    client._cached_subscriptions = [_subscription()]
    client._country = "us"

    with pytest.raises(HelloFreshNotImplementedError):
        _run(client.async_change_delivery_weekday(TUESDAY))

    assert calls == [f"PATCH /gw/api/subscriptions/{SUBSCRIPTION_ID}"], (
        "the legacy plans endpoint must not be called after an explicit NotImplemented"
    )
