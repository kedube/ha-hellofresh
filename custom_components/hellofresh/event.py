"""Event platform for HelloFresh: delivery lifecycle transitions."""

from __future__ import annotations

from homeassistant.components.event import (
    ENTITY_ID_FORMAT,
    EventEntity,
    EventEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import DELIVERY_EVENT_TYPES, HelloFreshDataUpdateCoordinator
from .entity import HelloFreshCoordinatorEntity

EVENTS: tuple[EventEntityDescription, ...] = (
    EventEntityDescription(
        key="delivery_events",
        translation_key="delivery_events",
        event_types=DELIVERY_EVENT_TYPES,
        icon="mdi:package-variant-closed",
    ),
)


# Coordinator-based: entities never poll on their own, so entity-update parallelism is
# irrelevant — declared 0 (unlimited) per the integration quality scale's convention.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the HelloFresh delivery event entity."""
    coordinator: HelloFreshDataUpdateCoordinator = entry.runtime_data
    async_add_entities(HelloFreshDeliveryEvent(coordinator, description) for description in EVENTS)


class HelloFreshDeliveryEvent(HelloFreshCoordinatorEntity, EventEntity):
    """Fires once per lifecycle transition the coordinator detects between polls.

    Event types: ``box_shipped``, ``box_delivered``, ``delivery_failed``, ``week_skipped``,
    ``week_unskipped``, ``selection_locked`` and ``menu_published``, each carrying the week id,
    subscription id, display name and delivery date (plus carrier details where relevant).
    Automations trigger on the entity's event type instead of diffing sensor states. The
    coordinator's delivery-day watch makes ``box_shipped`` / ``box_delivered`` land within
    minutes of the carrier reporting them.
    """

    def __init__(
        self,
        coordinator: HelloFreshDataUpdateCoordinator,
        description: EventEntityDescription,
    ) -> None:
        """Initialize the event entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{description.key}"
        self._pin_entity_id(ENTITY_ID_FORMAT, description.key)
        # Events recorded before this entity existed are history, not news: start consuming
        # from the coordinator's current serial.
        self._consumed_serial = coordinator.event_serial

    @callback
    def _handle_coordinator_update(self) -> None:
        """Fire every event recorded since the last one this entity emitted."""
        for serial, event_type, attributes in self.coordinator.delivery_events:
            if serial <= self._consumed_serial:
                continue
            self._consumed_serial = serial
            self._trigger_event(event_type, attributes)
            # Write after each event so back-to-back transitions each reach the state
            # machine (an event entity's state is only ever its latest event).
            self.async_write_ha_state()
