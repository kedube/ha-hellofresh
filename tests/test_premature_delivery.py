"""A box HelloFresh calls delivered before it arrives (issue #18).

A UK box was out for delivery with DPD at 08:40, but the card read "Delivered 20 Oct, 13:00":
HelloFresh had already marked the week DELIVERED, and its ``tracking.delivery_date`` was still
the scheduled-noon placeholder (12:00 UTC, 13:00 BST). The carrier's own status, or an
arrival time still in the future, takes "delivered" back until the box really comes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

from custom_components.hellofresh.api import (
    HelloFreshAccountData,
    HelloFreshClient,
    HelloFreshOrder,
    HelloFreshSubscription,
    HelloFreshWeek,
)
from custom_components.hellofresh.coordinator import (
    EVENT_BOX_DELIVERED,
    delivery_in_progress,
    delivery_snapshot,
    delivery_transitions,
)

TODAY = date.today()
NOW = datetime.now(UTC)


def _week(**overrides) -> HelloFreshWeek:
    defaults = {
        "week_id": "2026-W43",
        "display_name": "Oct 19 - Oct 25",
        "subscription_id": "sub-1",
        "delivery_date": TODAY,
        "status": "DELIVERED",
        "delivery_state": "DELIVERED",
        "sub_status": "COOK_IT",
    }
    return HelloFreshWeek(**{**defaults, **overrides})


def _order(**overrides) -> HelloFreshOrder:
    defaults = {
        "order_id": "o1",
        "week_id": "2026-W43",
        "status": "DELIVERED",
        "subscription_id": "sub-1",
        "delivery_date": TODAY,
        "tracking_url": "https://track.example/abc",
        "carrier": "DPD",
    }
    return HelloFreshOrder(**{**defaults, **overrides})


def _data(week: HelloFreshWeek, order: HelloFreshOrder) -> HelloFreshAccountData:
    return HelloFreshAccountData(
        weeks=[week],
        orders=[order],
        subscriptions=[HelloFreshSubscription(subscription_id="sub-1")],
    ).finalize()


def test_a_live_state_outranks_a_cook_it_sub_status() -> None:
    client = HelloFreshClient(session=None)  # type: ignore[arg-type]
    subscription = HelloFreshSubscription(subscription_id="sub-1", locale="en-GB")
    payload = {
        "items": [
            {
                "id": "2026-W43",
                "status": "RUNNING",
                "state": "ON_THE_WAY",
                "subStatus": "COOK_IT",
                "tracking": {"delivery_date": "2026-10-20T12:00:00+0000"},
            }
        ]
    }
    weeks, orders = client._normalize_weeks_payload(payload, subscription=subscription)
    assert weeks[0].status == "ON_THE_WAY"
    assert weeks[0].delivered_at is None
    assert orders[0].status == "ON_THE_WAY"


def test_the_carrier_takes_back_an_early_delivered() -> None:
    # The placeholder may already be past (a box still out after 13:00): the carrier decides.
    data = _data(
        _week(delivered_at=NOW - timedelta(hours=1)),
        _order(tracking_status="out_for_delivery"),
    )
    week = data.weeks[0]
    assert week.status == "ON_THE_WAY"
    assert week.delivered_at is None
    assert data.orders[0].status == "ON_THE_WAY"
    assert data.last_delivery_week is None
    # The raw lifecycle fields stay as HelloFresh sent them, for diagnostics.
    assert (week.delivery_state, week.sub_status) == ("DELIVERED", "COOK_IT")


def test_a_box_the_carrier_has_not_picked_up_is_preparing() -> None:
    data = _data(_week(), _order(tracking_status="pre_transit"))
    assert data.weeks[0].status == "PREPARING"
    assert data.orders[0].status == "PREPARING"


def test_an_arrival_time_in_the_future_is_a_placeholder() -> None:
    # No carrier status (its lookup failed), but nothing can have arrived at 13:00 at 08:40.
    data = _data(_week(delivered_at=NOW + timedelta(hours=4)), _order())
    assert data.weeks[0].status == "ON_THE_WAY"
    assert data.weeks[0].delivered_at is None


def test_a_box_the_carrier_delivered_stays_delivered() -> None:
    arrived = NOW - timedelta(minutes=5)
    data = _data(_week(delivered_at=arrived), _order(tracking_status="delivered"))
    assert data.weeks[0].status == "DELIVERED"
    assert data.weeks[0].delivered_at == arrived
    assert data.last_delivery_week is data.weeks[0]


def test_a_carrier_stuck_short_of_delivered_is_ignored_after_a_day() -> None:
    # A carrier that never posts its last scan can't hold an old box back.
    arrived = NOW - timedelta(days=3)
    data = _data(
        _week(delivery_date=TODAY - timedelta(days=3), delivered_at=arrived),
        _order(delivery_date=TODAY - timedelta(days=3), tracking_status="out_for_delivery"),
    )
    assert data.weeks[0].status == "DELIVERED"
    assert data.weeks[0].delivered_at == arrived


def test_box_delivered_fires_when_the_carrier_delivers_not_before() -> None:
    placeholder = NOW - timedelta(hours=1)
    out = _data(_week(delivered_at=placeholder), _order(tracking_status="out_for_delivery"))
    # The delivery-day fast poll keeps running while the box is on the road.
    assert delivery_in_progress(out, TODAY)

    still_out = _data(_week(delivered_at=placeholder), _order(tracking_status="out_for_delivery"))
    assert delivery_transitions(delivery_snapshot(out), delivery_snapshot(still_out)) == []

    arrived = NOW - timedelta(minutes=5)
    delivered = _data(_week(delivered_at=arrived), _order(tracking_status="delivered"))
    events = delivery_transitions(delivery_snapshot(still_out), delivery_snapshot(delivered))
    assert [kind for kind, _ in events] == [EVENT_BOX_DELIVERED]
    assert events[0][1]["delivered_at"] == arrived.isoformat()


def test_the_fast_poll_does_not_report_an_early_delivered_as_a_change_each_tick() -> None:
    client = HelloFreshClient(session=object(), access_token="token")  # type: ignore[arg-type]
    data = _data(
        _week(delivered_at=NOW - timedelta(hours=1)),
        _order(tracking_status="out_for_delivery"),
    )

    async def fake_upcoming(_subscription):
        # Every re-read says DELIVERED with the placeholder, as the deliveries payload did.
        return (
            [_week(delivered_at=NOW - timedelta(hours=1))],
            [_order(tracking_url=None, carrier=None)],
        )

    async def fake_tracking(_subscriptions, orders):
        for order in orders:
            order.tracking_status = "out_for_delivery"

    client._async_get_upcoming_deliveries = fake_upcoming  # type: ignore[method-assign]
    client._async_enrich_order_tracking = fake_tracking  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    try:
        assert loop.run_until_complete(client.async_refresh_delivery_status(data)) is False
    finally:
        loop.close()
    assert data.weeks[0].status == "ON_THE_WAY"
    assert data.orders[0].status == "ON_THE_WAY"
