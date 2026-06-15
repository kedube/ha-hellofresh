"""Binary sensor platform for HelloFresh."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    ENTITY_ID_FORMAT,
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import HelloFreshDataUpdateCoordinator
from .entity import HelloFreshCoordinatorEntity

SENSORS: tuple[BinarySensorEntityDescription, ...] = (
    BinarySensorEntityDescription(
        key="needs_meal_selection",
        translation_key="needs_meal_selection",
    ),
    BinarySensorEntityDescription(
        key="write_actions_available",
        translation_key="write_actions_available",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BinarySensorEntityDescription(
        key="tracked_shipment_available",
        translation_key="tracked_shipment_available",
    ),
    BinarySensorEntityDescription(
        key="payload_shape_changed",
        translation_key="payload_shape_changed",
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HelloFresh binary sensors."""
    coordinator: HelloFreshDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        HelloFreshBinarySensor(coordinator, description) for description in SENSORS
    )


class HelloFreshBinarySensor(HelloFreshCoordinatorEntity, BinarySensorEntity):
    """HelloFresh binary sensor."""

    def __init__(
        self,
        coordinator: HelloFreshDataUpdateCoordinator,
        description: BinarySensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{description.key}"
        self._pin_entity_id(ENTITY_ID_FORMAT, description.key)

    @property
    def is_on(self) -> bool:
        """Return the state."""
        if self.entity_description.key == "write_actions_available":
            return self.coordinator.data.capabilities.supports_write_actions
        if self.entity_description.key == "tracked_shipment_available":
            return self.coordinator.data.tracked_order is not None
        if self.entity_description.key == "payload_shape_changed":
            return self.coordinator.data.capabilities.payload_shape_changed
        return bool(self.coordinator.data.weeks_needing_selection)

    @property
    def icon(self) -> str | None:
        """Return an icon that matches the entity purpose and current state."""
        is_on = self.is_on

        if self.entity_description.key == "needs_meal_selection":
            return "mdi:silverware-fork-knife" if is_on else "mdi:silverware-clean"
        if self.entity_description.key == "write_actions_available":
            return "mdi:pencil-box-outline" if is_on else "mdi:pencil-off-outline"
        if self.entity_description.key == "tracked_shipment_available":
            return (
                "mdi:package-variant-closed-check"
                if is_on
                else "mdi:package-variant-closed-remove"
            )
        if self.entity_description.key == "payload_shape_changed":
            return "mdi:alert-octagon" if is_on else "mdi:check-circle-outline"
        return None

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Return lightweight diagnostic attributes.

        Deliberately scalar/counts only. Earlier this dumped full serialized
        subscriptions, weeks, orders, and public_menu on *every* binary sensor,
        which routinely blew past the recorder's 16 KB attribute cap (those payloads
        include per-week recipe lists). Nothing consumes the full blobs here — the
        dashboard reads the `weeks` list off sensor.next_selection_deadline instead —
        so we expose only small summary fields.
        """
        data = self.coordinator.data
        return {
            "account_data_available": data.account_data_available,
            "capabilities": data.capabilities.as_dict(),
            "weeks_needing_selection": len(data.weeks_needing_selection),
            "order_count": len(data.orders),
            "subscription_count": data.subscription_count,
        }
