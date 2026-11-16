"""The delivery-day watch: a light shipment-state refresh between full polls.

``async_refresh_delivery_status`` re-reads only the ranged deliveries payload and the
carrier lookup, overlays the lifecycle fields onto the live data object in place, and
re-finalizes so every derived view (``last_delivery_week``, ``tracked_order``) follows.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from custom_components.hellofresh.api import (
    HelloFreshAccountData,
    HelloFreshAuthError,
    HelloFreshClient,
    HelloFreshError,
    HelloFreshOrder,
    HelloFreshSubscription,
    HelloFreshWeek,
)
from custom_components.hellofresh.coordinator import (
    EVENT_BOX_DELIVERED,
    HelloFreshDataUpdateCoordinator,
)
from custom_components.hellofresh.parsers import local_today

TODAY = local_today()
ARRIVED = datetime(2026, 9, 9, 18, 20, tzinfo=UTC)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _client() -> HelloFreshClient:
    return HelloFreshClient(session=object(), access_token="token")  # type: ignore[arg-type]


def _data() -> HelloFreshAccountData:
    return HelloFreshAccountData(
        weeks=[
            HelloFreshWeek(
                week_id="2026-W37",
                display_name="This week",
                subscription_id="sub-1",
                delivery_date=TODAY,
                status="RUNNING",
                raw={"tracking": {"status": "scheduled"}},
            )
        ],
        orders=[
            HelloFreshOrder(
                order_id="o1",
                week_id="2026-W37",
                status="scheduled",
                subscription_id="sub-1",
                delivery_date=TODAY,
            )
        ],
        subscriptions=[HelloFreshSubscription(subscription_id="sub-1", account_id="acct-1")],
    ).finalize()


def test_refresh_overlays_delivery_state_and_refinalizes() -> None:
    client = _client()
    data = _data()

    async def fake_upcoming(_subscription):
        return (
            [
                HelloFreshWeek(
                    week_id="2026-W37",
                    display_name="This week",
                    subscription_id="sub-1",
                    delivery_date=TODAY,
                    status="DELIVERED",
                    delivered_at=ARRIVED,
                    delivery_state="DELIVERED",
                    raw={"tracking": {"status": "delivered", "delivery_date": ARRIVED.isoformat()}},
                )
            ],
            [
                HelloFreshOrder(
                    order_id="o1",
                    week_id="2026-W37",
                    status="delivered",
                    subscription_id="sub-1",
                    delivery_date=TODAY,
                    tracking_url="https://track.example/abc",
                )
            ],
        )

    async def fake_tracking(_subscriptions, _orders):
        return None

    client._async_get_upcoming_deliveries = fake_upcoming  # type: ignore[method-assign]
    client._async_enrich_order_tracking = fake_tracking  # type: ignore[method-assign]

    assert data.last_delivery_week is None
    assert _run(client.async_refresh_delivery_status(data)) is True

    week = data.weeks[0]
    assert week.status == "DELIVERED"
    assert week.delivered_at == ARRIVED
    assert week.raw["tracking"]["status"] == "delivered"
    assert data.orders[0].status == "delivered"
    assert data.orders[0].tracking_url == "https://track.example/abc"
    # finalize() ran: the newly delivered box is now the last delivery.
    assert data.last_delivery_week is week
    # Nothing changed on a second pass.
    assert _run(client.async_refresh_delivery_status(data)) is False


def test_refresh_does_nothing_without_subscriptions() -> None:
    client = _client()
    data = HelloFreshAccountData().finalize()
    assert _run(client.async_refresh_delivery_status(data)) is False


def _coordinator(data: HelloFreshAccountData, client) -> HelloFreshDataUpdateCoordinator:
    """A coordinator with just the attributes the watch tick touches (no hass)."""
    coordinator = HelloFreshDataUpdateCoordinator.__new__(HelloFreshDataUpdateCoordinator)
    coordinator.data = data
    coordinator.client = client
    coordinator.hass = None
    coordinator.config_entry = SimpleNamespace(
        entry_id="entry-1", title="HelloFresh", async_start_reauth=mock.Mock()
    )
    coordinator._refresh_lock = asyncio.Lock()
    coordinator._writes = 0
    coordinator._last_full_refresh = None
    coordinator._cancel_delivery_watch = None
    coordinator._delivery_watch_interval = timedelta(minutes=15)
    coordinator.delivery_events = []
    coordinator._event_serial = 0
    coordinator._weeks_response_cache = (data, {"cached": True})
    coordinator.async_update_listeners = mock.Mock()  # type: ignore[method-assign]
    return coordinator


def test_watch_tick_is_a_no_op_when_no_delivery_is_due() -> None:
    quiet = HelloFreshAccountData(
        weeks=[
            HelloFreshWeek(
                week_id="2026-W40",
                display_name="Later",
                subscription_id="sub-1",
                delivery_date=TODAY + timedelta(days=10),
            )
        ],
        subscriptions=[HelloFreshSubscription(subscription_id="sub-1")],
    ).finalize()
    client = SimpleNamespace(async_refresh_delivery_status=mock.AsyncMock(return_value=True))
    coordinator = _coordinator(quiet, client)
    _run(coordinator._async_delivery_watch_tick())
    client.async_refresh_delivery_status.assert_not_called()
    coordinator.async_update_listeners.assert_not_called()


def test_watch_tick_records_transitions_and_notifies_on_change() -> None:
    data = _data()

    async def fake_refresh(live: HelloFreshAccountData) -> bool:
        live.weeks[0].status = "DELIVERED"
        live.weeks[0].delivered_at = ARRIVED
        live.finalize()
        return True

    client = SimpleNamespace(async_refresh_delivery_status=fake_refresh)
    coordinator = _coordinator(data, client)
    _run(coordinator._async_delivery_watch_tick())

    assert [(serial, kind) for serial, kind, _ in coordinator.delivery_events] == [
        (1, EVENT_BOX_DELIVERED)
    ]
    assert coordinator.event_serial == 1
    # The identity-keyed get_weeks cache must not serve the pre-delivery response.
    assert coordinator._weeks_response_cache is None
    coordinator.async_update_listeners.assert_called_once()


def test_watch_tick_skips_while_a_full_poll_runs_or_just_ran() -> None:
    data = _data()
    client = SimpleNamespace(async_refresh_delivery_status=mock.AsyncMock(return_value=True))
    coordinator = _coordinator(data, client)

    async def _tick_during_a_poll() -> None:
        async with coordinator._refresh_lock:  # what a full poll holds while it runs
            await coordinator._async_delivery_watch_tick()

    _run(_tick_during_a_poll())
    client.async_refresh_delivery_status.assert_not_called()

    coordinator._last_full_refresh = datetime.now(UTC)
    _run(coordinator._async_delivery_watch_tick())
    client.async_refresh_delivery_status.assert_not_called()


def test_watch_stops_itself_and_asks_for_reauth_when_the_sign_in_is_rejected() -> None:
    """A rejected sign-in cannot heal by itself, so the watch must not keep asking.

    It used to start reauth and carry on ticking, repeating the rejected request every
    15 minutes for as long as a box was due.
    """
    client = SimpleNamespace(
        async_refresh_delivery_status=mock.AsyncMock(side_effect=HelloFreshAuthError("expired"))
    )
    coordinator = _coordinator(_data(), client)
    cancel = mock.Mock()
    coordinator._cancel_delivery_watch = cancel

    _run(coordinator._async_delivery_watch_tick())

    cancel.assert_called_once_with()
    assert coordinator._cancel_delivery_watch is None
    coordinator.config_entry.async_start_reauth.assert_called_once_with(None)
    coordinator.async_update_listeners.assert_not_called()


def test_watch_keeps_running_through_a_passing_failure() -> None:
    client = SimpleNamespace(
        async_refresh_delivery_status=mock.AsyncMock(side_effect=HelloFreshError("timeout"))
    )
    coordinator = _coordinator(_data(), client)
    cancel = mock.Mock()
    coordinator._cancel_delivery_watch = cancel

    _run(coordinator._async_delivery_watch_tick())

    cancel.assert_not_called()
    coordinator.config_entry.async_start_reauth.assert_not_called()


def test_a_poll_waits_for_a_running_tick_so_an_event_is_recorded_once() -> None:
    """A poll that starts in the middle of a tick must not record the same delivery again.

    The tick changes the data in place. A poll that took its "before" snapshot ahead of that
    change, and finished after it, saw the box go from running to delivered as well: two
    ``box_delivered`` events for one box.
    """
    data = _data()
    tick_is_fetching = asyncio.Event()
    carrier_answers = asyncio.Event()

    async def slow_refresh(live: HelloFreshAccountData) -> bool:
        tick_is_fetching.set()
        await carrier_answers.wait()
        live.weeks[0].status = "DELIVERED"
        live.weeks[0].delivered_at = ARRIVED
        live.finalize()
        return True

    async def full_poll() -> HelloFreshAccountData:
        fresh = _data()
        fresh.weeks[0].status = "DELIVERED"
        fresh.weeks[0].delivered_at = ARRIVED
        return fresh.finalize()

    client = SimpleNamespace(
        async_refresh_delivery_status=slow_refresh, async_get_account_data=full_poll
    )
    coordinator = _coordinator(data, client)
    coordinator.config_entry.options = {"show_data_quality_issues": False}

    async def scenario() -> None:
        tick = asyncio.ensure_future(coordinator._async_delivery_watch_tick())
        await tick_is_fetching.wait()
        poll = asyncio.ensure_future(coordinator._async_update_data())
        await asyncio.sleep(0)  # the poll has started and is waiting its turn
        carrier_answers.set()
        await asyncio.gather(tick, poll)

    with (
        mock.patch("custom_components.hellofresh.coordinator.async_delete_account_data_issue"),
        mock.patch(
            "custom_components.hellofresh.coordinator.async_delete_account_menu_fallback_issue"
        ),
        mock.patch(
            "custom_components.hellofresh.coordinator.async_delete_payload_shape_changed_issue"
        ),
        mock.patch("custom_components.hellofresh.coordinator.async_delete_write_actions_issue"),
    ):
        _run(scenario())

    assert [kind for _, kind, _ in coordinator.delivery_events] == [EVENT_BOX_DELIVERED]


def test_watch_interval_follows_the_option_and_zero_disables_it() -> None:
    coordinator = HelloFreshDataUpdateCoordinator.__new__(HelloFreshDataUpdateCoordinator)
    coordinator.config_entry = SimpleNamespace(options={})
    assert coordinator.delivery_watch_interval == timedelta(minutes=15)
    coordinator.config_entry = SimpleNamespace(options={"delivery_watch_interval_minutes": 5})
    assert coordinator.delivery_watch_interval == timedelta(minutes=5)
    coordinator.config_entry = SimpleNamespace(options={"delivery_watch_interval_minutes": 0})
    assert coordinator.delivery_watch_interval is None
    # A 0 option never arms the timer.
    coordinator._cancel_delivery_watch = None
    coordinator.hass = None
    coordinator.async_start_delivery_watch()
    assert coordinator._cancel_delivery_watch is None
