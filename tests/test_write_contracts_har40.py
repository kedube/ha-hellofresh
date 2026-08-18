"""Contract tests for the two write paths verified by HAR capture 40.

Before this capture, skip/unskip and one-off reschedule were the integration's least-evidenced
writes: skip/unskip had been confirmed against a capture that was no longer on disk, and
`POST …/oneoff` had never been observed at all. Capture 40 exercises both, so the exact request
shape is now pinned here against verbatim capture data.

Also recorded: **both endpoints return a stub week**, not a usable one. The response's
`allowedActions` are all false/null and `cutoffDate` is null; `/oneoff` additionally returns no
`status` and no `deliveryDate`. Feeding either back into state would mark the week permanently
uneditable, so the client must discard the response and let the coordinator re-poll. That is
asserted here too, because it is the kind of "improvement" someone could plausibly add later.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

import pytest

from custom_components.hellofresh.client import HelloFreshClient
from custom_components.hellofresh.models import HelloFreshWeek

UTC = timezone.utc

# Verbatim from HAR 40: the week the capture skipped, unskipped and rescheduled.
WEEK_ID = "2026-W39"
SUBSCRIPTION_ID = "6959884"
RAW_CUTOFF = "2026-09-16T23:59:59-0700"
RAW_DELIVERY = "2026-09-21T12:00:00-0700"

# Verbatim stub response body both writes return (trimmed to the fields that matter).
STUB_RESPONSE = {
    "count": 1,
    "items": [
        {
            "actionable": None,
            "allowedActions": {
                "donate": False,
                "mealSwap": False,
                "oneOffChange": None,
                "pause": False,
                "updateDeliveryAddress": False,
                "updateDeliveryWeekday": False,
                "updatePaymentMethod": None,
            },
            "availableOneOffOptions": None,
            "cutoffDate": None,
            "deliveryBlocked": False,
            "deliveryDate": RAW_DELIVERY,
            "id": WEEK_ID,
            "status": "PAUSED",
        }
    ],
}


def _week(*, status: str = "RUNNING", one_off: bool = True) -> HelloFreshWeek:
    return HelloFreshWeek(
        week_id=WEEK_ID,
        display_name="Week of Sep 21",
        subscription_id=SUBSCRIPTION_ID,
        delivery_date=date(2026, 9, 21),
        selection_deadline=datetime(2026, 9, 17, 6, 59, 59, tzinfo=UTC),
        status=status,
        allowed_actions={"mealSwap": True, "oneOffChange": one_off},
        raw={"cutoffDate": RAW_CUTOFF, "deliveryDate": RAW_DELIVERY, "id": WEEK_ID},
    )


def _client(week: HelloFreshWeek) -> tuple[HelloFreshClient, list[dict]]:
    """A client whose transport records requests instead of sending them."""
    client = HelloFreshClient(session=object())  # type: ignore[arg-type]
    sent: list[dict] = []

    async def fake_request(method, path, *, params=None, json_payload=None, **_kw):
        sent.append(
            {"method": method, "path": path, "params": params, "json": json_payload}
        )

        class _Resp:
            status = 201

        return _Resp()

    async def fake_json(_response):
        return STUB_RESPONSE

    client._async_api_request = fake_request  # type: ignore[method-assign]
    client._async_response_json = fake_json  # type: ignore[method-assign]
    client._last_account_data = None
    client._get_known_week_or_raise = lambda _week_id: week  # type: ignore[method-assign]
    return client, sent


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


@pytest.mark.parametrize(
    ("action", "expected_status"),
    [("async_skip_week", "PAUSED"), ("async_unskip_week", "RUNNING")],
)
def test_skip_unskip_uses_the_captured_delivery_status_patch(
    action: str, expected_status: str
) -> None:
    """HAR 40: PATCH …/delivery_dates/{week} with the status inside a `delivery` envelope."""
    week = _week(status="RUNNING" if action == "async_skip_week" else "PAUSED")
    client, sent = _client(week)

    _run(getattr(client, action)(WEEK_ID))

    assert len(sent) == 1, "the verified PATCH must succeed without touching any fallback path"
    request = sent[0]
    assert request["method"] == "PATCH"
    assert request["path"] == f"/gw/api/subscriptions/{SUBSCRIPTION_ID}/delivery_dates/{WEEK_ID}"
    assert request["params"] == {"country": "US", "locale": "en-US"}
    assert request["json"] == {
        "delivery": {
            "cutoffDate": RAW_CUTOFF,
            "deliveryDate": RAW_DELIVERY,
            "status": expected_status,
            "subscriptionId": SUBSCRIPTION_ID,
            "id": WEEK_ID,
        }
    }


def test_skip_preserves_the_servers_own_timestamp_format() -> None:
    """The body echoes the raw `-0700` strings, not a re-serialized ISO form.

    HelloFresh's own request sends exactly what it sent us; normalizing to `+00:00` would be an
    untested deviation on a write path.
    """
    client, sent = _client(_week())
    _run(client.async_skip_week(WEEK_ID))
    body = sent[0]["json"]["delivery"]
    assert body["cutoffDate"] == RAW_CUTOFF
    assert body["deliveryDate"] == RAW_DELIVERY
    assert body["cutoffDate"].endswith("-0700")


def test_one_off_reschedule_matches_the_captured_body() -> None:
    """HAR 40: POST …/oneoff with {id, delivery_option, week, source}."""
    client, sent = _client(_week())

    _run(client.async_change_one_off_delivery(WEEK_ID, "US-2-0800-2000"))

    assert len(sent) == 1
    request = sent[0]
    assert request["method"] == "POST"
    assert request["path"] == f"/gw/api/subscriptions/{SUBSCRIPTION_ID}/oneoff"
    assert request["params"] == {"country": "US", "locale": "en-US"}
    assert request["json"] == {
        "id": SUBSCRIPTION_ID,
        "delivery_option": "US-2-0800-2000",
        "week": WEEK_ID,
        # The captured web app sends this literal; it is not decorative.
        "source": "reschedule-delivery-feature",
    }


def test_one_off_reschedule_is_gated_on_the_advertised_capability() -> None:
    """A week HelloFresh won't reschedule must fail locally, not burn a rejected request."""
    from custom_components.hellofresh.models import HelloFreshNotImplementedError

    client, sent = _client(_week(one_off=False))
    with pytest.raises(HelloFreshNotImplementedError):
        _run(client.async_change_one_off_delivery(WEEK_ID, "US-2-0800-2000"))
    assert sent == []


def test_write_responses_are_not_merged_into_week_state() -> None:
    """Both endpoints return a STUB week; adopting it would corrupt state.

    HAR 40 shows `allowedActions` all false/null and `cutoffDate` null on the PATCH response, and
    `/oneoff` returning no status or delivery date at all. Trusting either would leave the week
    looking permanently locked until the next poll, so the response must be discarded.
    """
    week = _week()
    before = (dict(week.allowed_actions), week.status, week.selection_deadline)

    client, _ = _client(week)
    _run(client.async_skip_week(WEEK_ID))
    _run(client.async_change_one_off_delivery(WEEK_ID, "US-2-0800-2000"))

    assert (dict(week.allowed_actions), week.status, week.selection_deadline) == before, (
        "a write's stub response must not overwrite the week the client holds"
    )
