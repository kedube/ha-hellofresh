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
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import HelloFreshDataUpdateCoordinator
from .entity import HelloFreshCoordinatorEntity


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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HelloFresh calendar entities."""
    coordinator: HelloFreshDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([HelloFreshDeliveryCalendar(coordinator)])


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
        future_events = [
            item for item in self._events if self._event_sort_key(item, now) >= now
        ]
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
