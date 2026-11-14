"""The new delivery events and the record that keeps the once-a-week ones from repeating.

``box_delayed`` / ``box_early`` fire when a week's ``sub_status`` turns DELAYED / EARLY, with the
day the box now comes. They, ``selection_deadline_approaching`` and ``selection_locked`` fire at
most once per week, even across a restart: what fired is kept in a small Store per entry. The
timers that fire the deadline events at their moment are exercised end to end in
test_ha_lifecycle.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.hellofresh.api import HelloFreshAccountData, HelloFreshWeek
from custom_components.hellofresh.coordinator import (
    DELIVERY_EVENT_TYPES,
    EVENT_BOX_DELAYED,
    EVENT_BOX_EARLY,
    EVENT_SELECTION_DEADLINE_APPROACHING,
    EVENT_SELECTION_LOCKED,
    EVENT_WEEK_SKIPPED,
    FiredEvents,
    HelloFreshDataUpdateCoordinator,
    async_remove_fired_events,
    delivery_snapshot,
    delivery_transitions,
    fired_events_storage_key,
)
from custom_components.hellofresh.parsers import local_today

TODAY = local_today()


def _week(**overrides) -> HelloFreshWeek:
    defaults = {
        "week_id": "2026-W47",
        "display_name": "Nov 16 - Nov 22",
        "subscription_id": "sub-1",
        "delivery_date": TODAY + timedelta(days=2),
        "status": "RUNNING",
        "selection_deadline": datetime.now(UTC) + timedelta(days=7),
        "allowed_actions": {"mealSwap": True},
    }
    return HelloFreshWeek(**{**defaults, **overrides})


def _events(old: list[HelloFreshWeek], new: list[HelloFreshWeek]) -> list[tuple[str, dict]]:
    return delivery_transitions(
        delivery_snapshot(HelloFreshAccountData(weeks=old).finalize()),
        delivery_snapshot(HelloFreshAccountData(weeks=new).finalize()),
    )


def test_the_new_event_types_are_offered_by_the_entity() -> None:
    assert {
        EVENT_BOX_DELAYED,
        EVENT_BOX_EARLY,
        EVENT_SELECTION_DEADLINE_APPROACHING,
    } <= set(DELIVERY_EVENT_TYPES)


def test_a_box_hellofresh_moves_is_announced_with_the_day_it_now_comes() -> None:
    later = TODAY + timedelta(days=3)
    [(kind, attributes)] = _events(
        [_week()], [_week(sub_status="DELAYED", revised_delivery_date=later)]
    )
    assert kind == EVENT_BOX_DELAYED
    assert attributes == {
        "week_id": "2026-W47",
        "subscription_id": "sub-1",
        "display_name": "Nov 16 - Nov 22",
        "delivery_date": (TODAY + timedelta(days=2)).isoformat(),
        "revised_delivery_date": later.isoformat(),
    }

    earlier = TODAY + timedelta(days=1)
    assert _events([_week()], [_week(sub_status="early", revised_delivery_date=earlier)]) == [
        (
            EVENT_BOX_EARLY,
            {**attributes, "revised_delivery_date": earlier.isoformat()},
        )
    ]


def test_a_moved_box_is_news_when_it_moves_not_while_it_stays_moved() -> None:
    delayed = _week(sub_status="DELAYED")
    assert _events([delayed], [_week(sub_status="DELAYED")]) == []
    assert [kind for kind, _ in _events([delayed], [_week(sub_status="EARLY")])] == [
        EVENT_BOX_EARLY
    ]
    assert _events([delayed], [_week()]) == []


def test_a_delivered_skipped_or_newly_seen_box_is_not_announced_as_moved() -> None:
    delivered = _week(sub_status="DELAYED", status="DELIVERED", delivered_at=datetime.now(UTC))
    assert EVENT_BOX_DELAYED not in [kind for kind, _ in _events([_week()], [delivered])]
    skipped = _week(sub_status="DELAYED", is_skipped=True)
    assert EVENT_BOX_DELAYED not in [kind for kind, _ in _events([_week()], [skipped])]
    # A week first seen already delayed is not a change anyone saw happen.
    assert _events([], [_week(sub_status="DELAYED")]) == []


def test_a_lock_names_its_deadline() -> None:
    passed = datetime(2026, 1, 1, 23, 59, 59, tzinfo=UTC)
    [(kind, attributes)] = _events([_week()], [_week(selection_deadline=passed)])
    assert kind == EVENT_SELECTION_LOCKED
    assert attributes["deadline"] == passed.isoformat()


# ---- the record of what fired ----------------------------------------------------------------


async def test_what_fired_survives_a_restart_and_goes_with_its_week(hass, hass_storage) -> None:
    key = ("2026-W47", "sub-1")
    fired = FiredEvents(hass, "entry1")
    await fired.async_load()
    assert not fired.has_fired(key, EVENT_BOX_DELAYED)

    fired.mark(key, EVENT_BOX_DELAYED)
    fired.mark(key, EVENT_SELECTION_LOCKED)
    await fired.async_flush()
    stored = hass_storage[fired_events_storage_key("entry1")]["data"]
    assert stored == {"weeks": {"sub-1|2026-W47": ["box_delayed", "selection_locked"]}}

    restarted = FiredEvents(hass, "entry1")
    await restarted.async_load()
    assert restarted.has_fired(key, EVENT_BOX_DELAYED)
    assert restarted.has_fired(key, EVENT_SELECTION_LOCKED)
    assert not restarted.has_fired(key, EVENT_BOX_EARLY)
    assert not restarted.has_fired(("2026-W47", "sub-2"), EVENT_BOX_DELAYED)

    # The week left the data: forgotten.
    restarted.prune([("2026-W48", "sub-1")])
    assert not restarted.has_fired(key, EVENT_BOX_DELAYED)
    await restarted.async_flush()
    assert hass_storage[fired_events_storage_key("entry1")]["data"] == {"weeks": {}}

    await async_remove_fired_events(hass, "entry1")
    assert fired_events_storage_key("entry1") not in hass_storage


async def test_a_record_it_cannot_read_is_an_empty_one(hass, hass_storage) -> None:
    hass_storage[fired_events_storage_key("entry1")] = {
        "version": 1,
        "key": fired_events_storage_key("entry1"),
        "data": {"weeks": {"sub-1|2026-W47": ["box_delayed", "not_an_event"], "bad": "x"}},
    }
    fired = FiredEvents(hass, "entry1")
    await fired.async_load()
    assert fired.has_fired(("2026-W47", "sub-1"), EVENT_BOX_DELAYED)
    assert not fired.has_fired(("2026-W47", "sub-1"), "not_an_event")


async def test_an_unchanged_record_is_not_written(hass, monkeypatch) -> None:
    fired = FiredEvents(hass, "entry1")
    await fired.async_load()
    writes: list = []

    async def _save(data):
        writes.append(data)

    monkeypatch.setattr(fired._store, "async_save", _save)
    fired.prune([])  # nothing to forget
    await fired.async_flush()
    assert writes == []


async def test_a_once_a_week_event_is_queued_once_per_week(hass) -> None:
    coordinator = HelloFreshDataUpdateCoordinator.__new__(HelloFreshDataUpdateCoordinator)
    coordinator.delivery_events = []
    coordinator._event_serial = 0
    coordinator.fired_events = FiredEvents(hass, "entry1")
    week = {"week_id": "2026-W47", "subscription_id": "sub-1"}
    other = {"week_id": "2026-W48", "subscription_id": "sub-1"}

    assert coordinator._queue_event(EVENT_BOX_DELAYED, week) is True
    assert coordinator._queue_event(EVENT_BOX_DELAYED, week) is False
    assert coordinator._queue_event(EVENT_BOX_DELAYED, other) is True
    # A skip can be undone and done again: every one is news.
    assert coordinator._queue_event(EVENT_WEEK_SKIPPED, week) is True
    assert coordinator._queue_event(EVENT_WEEK_SKIPPED, week) is True

    assert [(serial, kind) for serial, kind, _ in coordinator.delivery_events] == [
        (1, EVENT_BOX_DELAYED),
        (2, EVENT_BOX_DELAYED),
        (3, EVENT_WEEK_SKIPPED),
        (4, EVENT_WEEK_SKIPPED),
    ]


async def test_a_delay_seen_by_two_polls_is_announced_once(hass) -> None:
    coordinator = HelloFreshDataUpdateCoordinator.__new__(HelloFreshDataUpdateCoordinator)
    coordinator.delivery_events = []
    coordinator._event_serial = 0
    coordinator.fired_events = FiredEvents(hass, "entry1")
    on_time = HelloFreshAccountData(weeks=[_week()]).finalize()
    delayed = HelloFreshAccountData(weeks=[_week(sub_status="DELAYED")]).finalize()

    coordinator._record_transitions(delivery_snapshot(on_time), delayed)
    # Back on time, then delayed again: HelloFresh moved the same box twice.
    coordinator._record_transitions(delivery_snapshot(delayed), on_time)
    coordinator._record_transitions(delivery_snapshot(on_time), delayed)

    assert [kind for _, kind, _ in coordinator.delivery_events] == [EVENT_BOX_DELAYED]


# ---- a deadline timer and the week it was set for --------------------------------------------


def _timed_coordinator(*weeks: HelloFreshWeek) -> HelloFreshDataUpdateCoordinator:
    coordinator = HelloFreshDataUpdateCoordinator.__new__(HelloFreshDataUpdateCoordinator)
    coordinator.delivery_events = []
    coordinator._event_serial = 0
    coordinator._deadline_timers = {}
    coordinator.data = HelloFreshAccountData(weeks=list(weeks)).finalize()
    return coordinator


DEADLINE = datetime(2026, 11, 18, 7, 59, 59, tzinfo=UTC)
KEY = ("2026-W47", "sub-1")


def test_a_timer_for_a_week_that_changed_since_fires_nothing() -> None:
    for week in (
        _week(selection_deadline=DEADLINE + timedelta(hours=3)),  # the deadline moved
        _week(selection_deadline=DEADLINE, is_skipped=True),
        _week(selection_deadline=DEADLINE, allowed_actions={"mealSwap": False}),
        _week(week_id="2026-W48", selection_deadline=DEADLINE),  # the week left the data
    ):
        coordinator = _timed_coordinator(week)
        coordinator._fire_deadline_event(KEY, EVENT_SELECTION_LOCKED, DEADLINE)
        assert coordinator.delivery_events == [], week

    coordinator = _timed_coordinator(_week(selection_deadline=DEADLINE))
    coordinator._fire_deadline_event(KEY, EVENT_SELECTION_LOCKED, DEADLINE)
    assert [kind for _, kind, _ in coordinator.delivery_events] == [EVENT_SELECTION_LOCKED]


def test_a_timer_with_nothing_due_does_nothing() -> None:
    coordinator = _timed_coordinator(_week())
    coordinator._deadline_timers = {
        (KEY, EVENT_SELECTION_LOCKED): (datetime.now(UTC) + timedelta(days=1), DEADLINE, None)
    }
    coordinator._async_deadline_reached(datetime.now(UTC))
    assert coordinator.delivery_events == []
    assert len(coordinator._deadline_timers) == 1
