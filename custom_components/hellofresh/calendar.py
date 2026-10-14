"""Calendar platform for HelloFresh."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from homeassistant.components.calendar import (
    ENTITY_ID_FORMAT,
    CalendarEntity,
    CalendarEvent,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import HelloFreshDataUpdateCoordinator
from .entity import HelloFreshCoordinatorEntity
from .meal_plan import MEAL_CALENDAR_KEY, MEALS, MealPlan, async_meal_labels, slot_title


def _get_datetime_local(value: datetime | date) -> datetime:
    """Return ``value`` as a local, timezone-aware datetime.

    All-day events carry a bare ``date``, which cannot be compared against the aware
    ``datetime`` bounds Home Assistant passes to ``async_get_events``; a bare date is
    anchored to the start of the local day. Mirrors the private helper of the same name in
    ``homeassistant.components.calendar`` — reimplemented rather than imported, since a
    private symbol there is free to move between releases.
    """
    if isinstance(value, datetime):
        return dt_util.as_local(value)
    return dt_util.start_of_local_day(value)


# Coordinator-based: entities never poll on their own, so entity-update parallelism is
# irrelevant — declared 0 (unlimited) per the integration quality scale's convention.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HelloFresh calendar entities."""
    coordinator: HelloFreshDataUpdateCoordinator = entry.runtime_data
    entities: list[CalendarEntity] = [HelloFreshDeliveryCalendar(coordinator)]
    meal_plan = getattr(coordinator, "meal_plan", None)
    if meal_plan is not None:
        entities.append(HelloFreshMealsCalendar(coordinator, meal_plan))
    async_add_entities(entities)


class HelloFreshDeliveryCalendar(HelloFreshCoordinatorEntity, CalendarEntity):
    """Expose HelloFresh deliveries and selection deadlines as a calendar."""

    _attr_translation_key = "delivery_schedule"
    _attr_icon = "mdi:truck-delivery-outline"
    # A calendar entity's state is just on/off (event active now / not), which is noise in the
    # main entities list. Categorize it as diagnostic so it's tucked under the device's Diagnostic
    # section while staying fully usable on a Calendar dashboard card and in calendar automations.
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HelloFreshDataUpdateCoordinator) -> None:
        """Initialize the calendar."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_delivery_schedule"
        self._pin_entity_id(ENTITY_ID_FORMAT, "delivery_schedule")
        self._events = self._build_events()

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming event."""
        if not self._events:
            return None

        now = dt_util.now()
        future_events = [item for item in self._events if self._event_sort_key(item, now) >= now]
        return min(future_events or self._events, key=lambda item: self._event_sort_key(item, now))

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return calendar events overlapping a datetime range.

        Home Assistant's contract here is **overlap**, not "starts inside the window". An
        earlier version tested only the event's start against the range, which silently
        dropped a delivery whenever the requested window did not begin at midnight: a
        delivery is an all-day event starting at 00:00, so a card asking for
        ``Aug 20 08:00 → Aug 21`` matched nothing even though the box arrives that very day.
        Comparing end > start_date and start < end_date includes any event that overlaps.

        Deliveries are all-day ``date`` values while the requested range is always an aware
        ``datetime``, so both sides go through ``_get_datetime_local`` (the same helper HA's
        own calendar component uses) to anchor a bare date to the local day. Mixing a naive
        ``datetime.combine`` result with an aware bound previously raised TypeError.
        """
        results: list[CalendarEvent] = []
        window_start = dt_util.as_local(start_date)
        window_end = dt_util.as_local(end_date)
        for event in self._events:
            event_start = _get_datetime_local(event.start)
            event_end = _get_datetime_local(event.end)
            if event_end > window_start and event_start < window_end:
                results.append(event)
        return results

    def _handle_coordinator_update(self) -> None:
        """Push event updates to calendar subscribers."""
        self._events = self._build_events()
        self.async_update_event_listeners()
        super()._handle_coordinator_update()

    @staticmethod
    def _event_sort_key(event: CalendarEvent, fallback: datetime) -> datetime:
        """Normalize calendar event start values for sorting.

        Every branch must return an *aware* datetime, or sorting a mix of all-day and timed
        events raises TypeError.
        """
        start = event.start
        if isinstance(start, (date, datetime)):
            return _get_datetime_local(start)
        return fallback

    def _build_events(self) -> list[CalendarEvent]:
        """Build all calendar events from coordinator data."""
        events: list[CalendarEvent] = []
        data = self.coordinator.data

        for order in data.orders:
            if order.delivery_date is None:
                continue

            summary = "HelloFresh delivery"
            if order.status:
                summary = f"{summary}: {order.status}"

            description_parts = [
                f"Order ID: {order.order_id}",
                f"Week ID: {order.week_id}",
            ]
            if order.subscription_id:
                description_parts.append(f"Subscription ID: {order.subscription_id}")
            if order.tracking_status:
                description_parts.append(f"Tracking: {order.tracking_status}")
            if order.tracking_number:
                description_parts.append(f"Tracking number: {order.tracking_number}")
            if order.slot_label:
                description_parts.append(f"Delivery slot: {order.slot_label}")

            events.append(
                CalendarEvent(
                    summary=summary,
                    start=order.delivery_date,
                    end=order.delivery_date + timedelta(days=1),
                    description="\n".join(description_parts),
                    uid=f"{self.coordinator.config_entry.entry_id}_order_{order.order_id}",
                )
            )

        return events


class HelloFreshMealsCalendar(HelloFreshCoordinatorEntity, CalendarEntity):
    """The Week tab's plan as timed events: one per planned meal.

    Built from the stored plan alone (each course keeps a snapshot), so it changes the moment
    the plan does and never waits on a poll. Read-only here — the plan is edited in the card's
    Week tab or with the set/clear services. Titles are in Home Assistant's language, from the
    card's own words.
    """

    _attr_translation_key = "meals"
    _attr_icon = "mdi:silverware-variant"

    def __init__(self, coordinator: HelloFreshDataUpdateCoordinator, meal_plan: MealPlan) -> None:
        """Initialize the calendar."""
        super().__init__(coordinator)
        self._meal_plan = meal_plan
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{MEAL_CALENDAR_KEY}"
        self._pin_entity_id(ENTITY_ID_FORMAT, MEAL_CALENDAR_KEY)
        self._labels: dict[str, str] = {}
        self._events: list[CalendarEvent] = []

    @property
    def available(self) -> bool:
        """The plan is local: it stays readable while HelloFresh is down."""
        return True

    async def async_added_to_hass(self) -> None:
        """Load the event wording, build the events and follow the plan."""
        await super().async_added_to_hass()
        self._labels = await async_meal_labels(self.hass)
        self._events = self._build_events()
        self.async_on_remove(self._meal_plan.async_add_listener(self._plan_changed))

    @callback
    def _plan_changed(self) -> None:
        self._events = self._build_events()
        self.async_update_event_listeners()
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """When the plan last changed, so an open card can re-read it."""
        return {"plan_updated": self._meal_plan.updated.isoformat()}

    @property
    def event(self) -> CalendarEvent | None:
        """Return the meal under way, else the next one."""
        now = dt_util.now()
        ahead = [event for event in self._events if _get_datetime_local(event.end) > now]
        return min(ahead, key=lambda event: _get_datetime_local(event.start)) if ahead else None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return the planned meals overlapping a datetime range."""
        window_start = dt_util.as_local(start_date)
        window_end = dt_util.as_local(end_date)
        return [
            event
            for event in self._events
            if _get_datetime_local(event.end) > window_start
            and _get_datetime_local(event.start) < window_end
        ]

    def _build_events(self) -> list[CalendarEvent]:
        labels = self._labels
        events: list[CalendarEvent] = []
        for key in sorted(self._meal_plan.days):
            try:
                day = date.fromisoformat(key)
            except ValueError:
                continue
            for meal in MEALS:
                slot = self._meal_plan.slot(key, meal)
                if slot is None:
                    continue
                start, end = self._meal_plan.window(day, meal, slot)
                summary = labels["event"].format(meal=labels[meal], name=slot_title(slot))
                lines = [
                    labels["course"].format(course=labels[course], name=slot[course]["name"])
                    for course in ("appetizer", "dessert")
                    if isinstance(slot.get(course), dict) and slot.get("main")
                ]
                main = slot.get("main")
                minutes = main.get("minutes") if isinstance(main, dict) else None
                if isinstance(minutes, int) and minutes > 0:
                    lines.append(labels["cooking"].format(minutes=minutes))
                events.append(
                    CalendarEvent(
                        summary=summary,
                        start=start,
                        end=end,
                        description="\n".join(lines) or None,
                        uid=f"{self.coordinator.config_entry.entry_id}_meal_{key}_{meal}",
                    )
                )
        return events
