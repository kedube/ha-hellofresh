"""Lifecycle transition detection behind ``event.delivery_events``.

The coordinator reduces each poll to a plain snapshot per (week, subscription) and diffs
consecutive snapshots; only TRANSITIONS fire. A box already delivered when first seen is
history, a week leaving the window is nothing, and the very first poll after startup fires
nothing at all (there is no previous snapshot).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

from custom_components.hellofresh.api import (
    HelloFreshAccountData,
    HelloFreshOrder,
    HelloFreshRecipe,
    HelloFreshWeek,
)
from custom_components.hellofresh.coordinator import (
    DELIVERY_EVENT_TYPES,
    EVENT_BOX_DELIVERED,
    EVENT_BOX_SHIPPED,
    EVENT_DELIVERY_FAILED,
    EVENT_MENU_PUBLISHED,
    EVENT_SELECTION_LOCKED,
    EVENT_WEEK_SKIPPED,
    EVENT_WEEK_UNSKIPPED,
    delivery_in_progress,
    delivery_snapshot,
    delivery_transitions,
)
from custom_components.hellofresh.event import EVENTS, HelloFreshDeliveryEvent

TODAY = date(2026, 9, 9)


def _week(**overrides) -> HelloFreshWeek:
    defaults = {
        "week_id": "2026-W37",
        "display_name": "Sep 7 - Sep 13",
        "subscription_id": "sub-1",
        "delivery_date": TODAY,
        "status": "RUNNING",
        "selection_deadline": datetime.now(UTC) + timedelta(days=7),
        "allowed_actions": {"mealSwap": True},
        "recipes": [HelloFreshRecipe(recipe_id="r1", name="Pasta")],
    }
    return HelloFreshWeek(**{**defaults, **overrides})


def _data(weeks, orders=()) -> HelloFreshAccountData:
    return HelloFreshAccountData(weeks=list(weeks), orders=list(orders)).finalize()


def _events(old: HelloFreshAccountData, new: HelloFreshAccountData) -> list[str]:
    return [t for t, _ in delivery_transitions(delivery_snapshot(old), delivery_snapshot(new))]


def test_delivered_transition_fires_once_with_carrier_timestamp() -> None:
    before = _data([_week()])
    arrived = datetime(2026, 9, 9, 18, 20, tzinfo=UTC)
    after = _data([_week(status="DELIVERED", delivered_at=arrived)])
    events = delivery_transitions(delivery_snapshot(before), delivery_snapshot(after))
    assert [t for t, _ in events] == [EVENT_BOX_DELIVERED]
    attrs = events[0][1]
    assert attrs["week_id"] == "2026-W37"
    assert attrs["subscription_id"] == "sub-1"
    assert attrs["delivered_at"] == arrived.isoformat()
    # Already delivered on both sides: nothing more to say.
    assert _events(after, after) == []


def test_shipped_from_week_state_or_carrier_status() -> None:
    before = _data([_week()])
    on_the_way = _data([_week(status="ON_THE_WAY")])
    assert _events(before, on_the_way) == [EVENT_BOX_SHIPPED]

    order = HelloFreshOrder(
        order_id="o1",
        week_id="2026-W37",
        status="scheduled",
        subscription_id="sub-1",
        delivery_date=TODAY,
        tracking_status="out_for_delivery",
        carrier="Veho",
        tracking_number="TRACK1",
    )
    via_carrier = _data([_week()], [order])
    events = delivery_transitions(delivery_snapshot(before), delivery_snapshot(via_carrier))
    assert [t for t, _ in events] == [EVENT_BOX_SHIPPED]
    assert events[0][1]["carrier"] == "Veho"
    assert events[0][1]["tracking_number"] == "TRACK1"
    # Shipped stays shipped: no repeat on the next poll.
    assert _events(via_carrier, via_carrier) == []


def test_skip_unskip_lock_and_failure_transitions() -> None:
    open_week = _week()
    assert _events(_data([open_week]), _data([_week(is_skipped=True)])) == [EVENT_WEEK_SKIPPED]
    assert _events(_data([_week(is_skipped=True)]), _data([open_week])) == [EVENT_WEEK_UNSKIPPED]
    locked = _week(selection_deadline=datetime(2020, 1, 1, tzinfo=UTC))
    assert _events(_data([open_week]), _data([locked])) == [EVENT_SELECTION_LOCKED]
    assert _events(_data([open_week]), _data([_week(status="FAILED")])) == [EVENT_DELIVERY_FAILED]


def test_menu_published_for_new_week_and_for_week_gaining_recipes() -> None:
    base = _data([_week()])
    newcomer = _week(week_id="2026-W38", delivery_date=TODAY + timedelta(days=7))
    assert _events(base, _data([_week(), newcomer])) == [EVENT_MENU_PUBLISHED]
    bare = _week(week_id="2026-W38", delivery_date=TODAY + timedelta(days=7), recipes=[])
    assert _events(_data([_week(), bare]), _data([_week(), newcomer])) == [EVENT_MENU_PUBLISHED]
    # A newly seen week that is already delivered or skipped is not "published".
    old_box = _week(week_id="2026-W30", status="DELIVERED", delivered_at=datetime.now(UTC))
    assert _events(base, _data([_week(), old_box])) == []


def test_weeks_leaving_the_window_fire_nothing() -> None:
    assert _events(_data([_week(), _week(week_id="2026-W38")]), _data([_week()])) == []


def test_two_subscriptions_sharing_a_week_id_are_tracked_separately() -> None:
    before = _data([_week(), _week(subscription_id="sub-2")])
    after = _data(
        [
            _week(),
            _week(subscription_id="sub-2", status="DELIVERED", delivered_at=datetime.now(UTC)),
        ]
    )
    events = delivery_transitions(delivery_snapshot(before), delivery_snapshot(after))
    assert [(t, a["subscription_id"]) for t, a in events] == [(EVENT_BOX_DELIVERED, "sub-2")]


def test_delivery_in_progress_only_around_the_due_day() -> None:
    assert delivery_in_progress(_data([_week()]), TODAY) is True
    assert delivery_in_progress(_data([_week()]), TODAY + timedelta(days=1)) is True  # late box
    assert delivery_in_progress(_data([_week()]), TODAY - timedelta(days=1)) is False
    assert delivery_in_progress(_data([_week()]), TODAY + timedelta(days=2)) is False
    delivered = _week(status="DELIVERED", delivered_at=datetime.now(UTC))
    assert delivery_in_progress(_data([delivered]), TODAY) is False
    assert delivery_in_progress(_data([_week(is_skipped=True)]), TODAY) is False
    # A box already on the truck counts whatever the calendar says.
    early = _week(status="ON_THE_WAY", delivery_date=TODAY + timedelta(days=3))
    assert delivery_in_progress(_data([early]), TODAY) is True


def test_event_entity_fires_only_new_events_and_never_replays_history() -> None:
    coordinator = SimpleNamespace(
        data=_data([_week()]),
        config_entry=SimpleNamespace(entry_id="entry-1", title="HelloFresh"),
        delivery_events=[(1, EVENT_BOX_SHIPPED, {"week_id": "2026-W36"})],
        event_serial=1,
    )
    entity = HelloFreshDeliveryEvent(coordinator, EVENTS[0])
    writes: list[str | None] = []
    entity.async_write_ha_state = lambda: writes.append(entity.state_attributes.get("event_type"))  # type: ignore[method-assign]
    assert entity.entity_id == "event.hellofresh_delivery_events"
    assert entity.event_types == DELIVERY_EVENT_TYPES

    # The event recorded before the entity existed is history: nothing fires.
    entity._handle_coordinator_update()
    assert writes == []

    coordinator.delivery_events.extend(
        [
            (2, EVENT_BOX_DELIVERED, {"week_id": "2026-W37", "delivered_at": "x"}),
            (3, EVENT_MENU_PUBLISHED, {"week_id": "2026-W40"}),
        ]
    )
    entity._handle_coordinator_update()
    # Each transition reaches the state machine in order; the entity settles on the last.
    assert writes == [EVENT_BOX_DELIVERED, EVENT_MENU_PUBLISHED]
    assert entity.state_attributes["week_id"] == "2026-W40"
    # A listener update with nothing new fires nothing.
    entity._handle_coordinator_update()
    assert len(writes) == 2
