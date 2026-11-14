"""Tests for the HelloFresh delivery calendar platform.

This platform shipped with **no test coverage at all**, and two defects were sitting in it:

1. ``async_get_events`` tested only whether an event *started* inside the requested window.
   Home Assistant's contract is **overlap**. A delivery is an all-day event starting at
   00:00 local, so any window that did not begin exactly at midnight dropped that day's
   delivery — a calendar card asking for ``Aug 20 08:00 → Aug 21`` returned nothing even
   though the box arrives on Aug 20.
2. Both ``async_get_events`` and the next-event sort built *naive* datetimes from all-day
   ``date`` values and compared them against Home Assistant's *aware* bounds, which raises
   ``TypeError: can't compare offset-naive and offset-aware datetimes``.

A later review found two more: the entity's ``event`` skipped today's delivery (so the
calendar was never on during delivery day), and skipped or paused weeks showed up as
deliveries.

The tests below exercise the entity's real methods against stub coordinator data.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from homeassistant.components.calendar import CalendarEvent
from homeassistant.const import STATE_OFF, STATE_ON

from custom_components.hellofresh.api import (
    HelloFreshAccountData,
    HelloFreshOrder,
    HelloFreshSubscription,
    HelloFreshWeek,
)
from custom_components.hellofresh.calendar import HelloFreshDeliveryCalendar
from custom_components.hellofresh.parsers import local_today

UTC = timezone.utc


def _order(order_id: str, delivery_date: date | None, **extra: object) -> SimpleNamespace:
    """Build a stub order with the fields _build_events reads."""
    return SimpleNamespace(
        order_id=order_id,
        delivery_date=delivery_date,
        week_id=f"2026-W{order_id}",
        subscription_id=None,
        status=extra.get("status"),
        tracking_status=extra.get("tracking_status"),
        tracking_number=extra.get("tracking_number"),
        slot_label=extra.get("slot_label"),
    )


def _calendar(orders: list, data: object | None = None) -> HelloFreshDeliveryCalendar:
    """Build the real entity around a stub coordinator, bypassing HA's entity plumbing.

    ``data`` is real account data for the tests that need weeks; the rest get a stub with
    just the orders and no weeks to look up.
    """
    if data is None:
        data = SimpleNamespace(orders=orders, weeks=[], get_week=lambda *_args: None)
    coordinator = SimpleNamespace(
        data=data,
        config_entry=SimpleNamespace(entry_id="entry1"),
    )
    cal = HelloFreshDeliveryCalendar.__new__(HelloFreshDeliveryCalendar)
    cal.coordinator = coordinator  # type: ignore[attr-defined]
    cal._events = cal._build_events()
    return cal


def _get_events(cal: HelloFreshDeliveryCalendar, start: datetime, end: datetime):
    """Drive the async API from a sync test without leaking an event loop."""
    # Not closed on purpose: HA's autouse `verify_cleanup` fixture touches the current event
    # loop at teardown, and a closed one raises there. Matches the rest of the suite.
    loop = asyncio.new_event_loop()
    return loop.run_until_complete(cal.async_get_events(None, start, end))


def test_delivery_is_returned_when_window_starts_after_midnight() -> None:
    """The reported class of bug: an all-day delivery dropped by a mid-day window.

    The box arrives on Aug 20. A window of Aug 20 08:00 → Aug 21 overlaps that day, so the
    event must be returned. Start-containment logic returned nothing here.
    """
    cal = _calendar([_order("1", date(2026, 8, 20))])
    events = _get_events(
        cal, datetime(2026, 8, 20, 8, tzinfo=UTC), datetime(2026, 8, 21, tzinfo=UTC)
    )
    assert len(events) == 1, "an all-day delivery overlapping the window must be returned"


def test_events_outside_the_window_are_excluded() -> None:
    """Overlap must not become 'return everything'."""
    cal = _calendar(
        [
            _order("1", date(2026, 8, 10)),
            _order("2", date(2026, 8, 20)),
            _order("3", date(2026, 9, 5)),
        ]
    )
    events = _get_events(cal, datetime(2026, 8, 19, tzinfo=UTC), datetime(2026, 8, 22, tzinfo=UTC))
    assert [e.summary for e in events] == ["HelloFresh delivery"]
    assert len(events) == 1


def test_window_boundaries_are_half_open() -> None:
    """A delivery on the window's end day belongs to the next window, not this one.

    All-day event Aug 20 spans [Aug 20 00:00, Aug 21 00:00). A window ending exactly at
    Aug 20 00:00 must not include it, or adjacent month views double-count.
    """
    cal = _calendar([_order("1", date(2026, 8, 20))])
    before = _get_events(cal, datetime(2026, 8, 19, tzinfo=UTC), datetime(2026, 8, 20, tzinfo=UTC))
    assert before == []
    touching = _get_events(
        cal, datetime(2026, 8, 20, tzinfo=UTC), datetime(2026, 8, 21, tzinfo=UTC)
    )
    assert len(touching) == 1


def test_get_events_does_not_raise_on_all_day_events() -> None:
    """Guards the naive/aware TypeError: all-day dates vs aware window bounds."""
    cal = _calendar([_order("1", date(2026, 8, 20))])
    # Must not raise.
    _get_events(cal, datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC))


def test_next_event_prefers_the_soonest_upcoming_delivery() -> None:
    """`event` drives the entity state and must sort all-day events without raising."""
    today = local_today()
    cal = _calendar(
        [
            _order("past", today - timedelta(days=14)),
            _order("soon", today + timedelta(days=3)),
            _order("later", today + timedelta(days=30)),
        ]
    )
    nxt = cal.event
    assert nxt is not None
    assert nxt.start == today + timedelta(days=3)


def test_next_event_is_none_without_orders() -> None:
    """An account with no dated orders has no calendar event."""
    assert _calendar([]).event is None


def test_todays_delivery_is_the_current_event_and_turns_the_calendar_on() -> None:
    """On delivery day the calendar is on and describes today's box, not next week's.

    An all-day delivery starts at local midnight, so one second later it no longer counted
    as "upcoming": ``event`` moved on to next week's delivery and the entity stayed off for
    the whole delivery day.
    """
    today = local_today()
    cal = _calendar(
        [
            _order("past", today - timedelta(days=7)),
            _order("today", today),
            _order("next", today + timedelta(days=7)),
        ]
    )
    assert cal.event is not None
    assert cal.event.start == today
    assert cal.state == STATE_ON


def test_no_event_once_every_delivery_is_behind_us() -> None:
    """With nothing left to deliver there is no event; the oldest order is not "next"."""
    today = local_today()
    cal = _calendar(
        [_order("old", today - timedelta(days=70)), _order("last", today - timedelta(days=1))]
    )
    assert cal.event is None
    assert cal.state == STATE_OFF


def _week_and_order(week_id: str, day: date, **week_fields: object) -> tuple:
    week = HelloFreshWeek(
        week_id=week_id,
        display_name=week_id,
        subscription_id="sub-1",
        delivery_date=day,
        **week_fields,
    )
    order = HelloFreshOrder(
        order_id=f"order-{week_id}",
        week_id=week_id,
        status=str(week_fields.get("status") or "RUNNING"),
        subscription_id="sub-1",
        delivery_date=day,
    )
    return week, order


def test_skipped_and_paused_weeks_are_not_deliveries() -> None:
    """HelloFresh lists an order for every week; only the ones that ship go on the calendar.

    A skipped week used to appear as an all-day "HelloFresh delivery: PAUSED" on a day no box
    arrives, so calendar-trigger automations fired for it and ``event`` could point at it.
    """
    today = local_today()
    skipped = _week_and_order("2026-W47", today + timedelta(days=4), is_skipped=True)
    paused = _week_and_order("2026-W48", today + timedelta(days=11), status="PAUSED")
    shipping = _week_and_order("2026-W49", today + timedelta(days=18), status="RUNNING")
    data = HelloFreshAccountData(
        weeks=[skipped[0], paused[0], shipping[0]],
        orders=[skipped[1], paused[1], shipping[1]],
        subscriptions=[HelloFreshSubscription(subscription_id="sub-1")],
    ).finalize()

    cal = _calendar([], data)

    assert [event.start for event in cal._events] == [today + timedelta(days=18)]
    assert cal.event is not None
    assert cal.event.summary == "HelloFresh delivery: RUNNING"


def test_a_skipped_week_only_hides_its_own_subscriptions_order() -> None:
    """Two subscriptions share ISO week ids: one skipping a week must not hide the other's box."""
    day = local_today() + timedelta(days=4)
    skipped = HelloFreshWeek(
        week_id="2026-W47",
        display_name="W47",
        subscription_id="sub-1",
        delivery_date=day,
        is_skipped=True,
    )
    shipping = HelloFreshWeek(
        week_id="2026-W47", display_name="W47", subscription_id="sub-2", delivery_date=day
    )
    orders = [
        HelloFreshOrder(
            order_id=f"order-{sub}",
            week_id="2026-W47",
            status="RUNNING",
            subscription_id=sub,
            delivery_date=day,
        )
        for sub in ("sub-1", "sub-2")
    ]
    data = HelloFreshAccountData(
        weeks=[skipped, shipping],
        orders=orders,
        subscriptions=[
            HelloFreshSubscription(subscription_id="sub-1"),
            HelloFreshSubscription(subscription_id="sub-2"),
        ],
    ).finalize()

    cal = _calendar([], data)

    assert [event.uid for event in cal._events] == ["entry1_order_order-sub-2"]


def test_another_subscriptions_skipped_week_never_hides_an_order() -> None:
    """An order whose own subscription has no week entry is not judged by another's week."""
    day = local_today() + timedelta(days=4)
    skipped = HelloFreshWeek(
        week_id="2026-W47",
        display_name="W47",
        subscription_id="sub-1",
        delivery_date=day,
        is_skipped=True,
    )
    order = HelloFreshOrder(
        order_id="order-sub-2",
        week_id="2026-W47",
        status="RUNNING",
        subscription_id="sub-2",
        delivery_date=day,
    )
    data = HelloFreshAccountData(
        weeks=[skipped],
        orders=[order],
        subscriptions=[
            HelloFreshSubscription(subscription_id="sub-1"),
            HelloFreshSubscription(subscription_id="sub-2"),
        ],
    ).finalize()

    assert [event.uid for event in _calendar([], data)._events] == ["entry1_order_order-sub-2"]


def test_orders_without_a_delivery_date_are_skipped() -> None:
    """A shell order with no date cannot be placed on a calendar."""
    cal = _calendar([_order("1", None), _order("2", date(2026, 8, 20))])
    assert len(cal._events) == 1


def test_event_carries_status_and_tracking_detail() -> None:
    """The description is the only place order metadata surfaces on the calendar."""
    cal = _calendar(
        [
            _order(
                "1",
                date(2026, 8, 20),
                status="Delivered",
                tracking_status="Delivered",
                tracking_number="1Z999",
                slot_label="8am - 8pm",
            )
        ]
    )
    event = cal._events[0]
    assert event.summary == "HelloFresh delivery: Delivered"
    assert "Tracking number: 1Z999" in event.description
    assert "Delivery slot: 8am - 8pm" in event.description


def test_all_day_event_spans_exactly_one_day() -> None:
    """An all-day delivery must not bleed into the following day on the card."""
    cal = _calendar([_order("1", date(2026, 8, 20))])
    event: CalendarEvent = cal._events[0]
    assert event.start == date(2026, 8, 20)
    assert event.end == date(2026, 8, 21)
