"""Calendar platform for HelloFresh."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import HelloFreshDataUpdateCoordinator
from .entity import HelloFreshCoordinatorEntity


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

    def __init__(self, coordinator: HelloFreshDataUpdateCoordinator) -> None:
        """Initialize the calendar."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_delivery_schedule"
        self._events = self._build_events()

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming event."""
        if not self._events:
            return None

        now = datetime.now().astimezone()
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
        """Return calendar events within a datetime range."""
        results: list[CalendarEvent] = []
        for event in self._events:
            event_start = event.start
            if isinstance(event_start, date) and not isinstance(event_start, datetime):
                start_dt = datetime.combine(event_start, time.min, tzinfo=start_date.tzinfo)
            else:
                start_dt = event_start

            if start_dt >= end_date or start_dt < start_date:
                continue
            results.append(event)
        return results

    def _handle_coordinator_update(self) -> None:
        """Push event updates to calendar subscribers."""
        self._events = self._build_events()
        self.async_update_event_listeners()
        super()._handle_coordinator_update()

    @staticmethod
    def _event_sort_key(event: CalendarEvent, fallback: datetime) -> datetime:
        """Normalize calendar event start values for sorting."""
        start = event.start
        if isinstance(start, date) and not isinstance(start, datetime):
            return datetime.combine(start, time.min).astimezone()
        if isinstance(start, datetime):
            return start if start.tzinfo else start.astimezone()
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
