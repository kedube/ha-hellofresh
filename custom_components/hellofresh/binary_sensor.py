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
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # On when HelloFresh reports the card on file as expiring or already expired — the most
    # common way a box silently fails to ship. Unavailable until the payments gateway has
    # answered; attributes carry only the card type/provider and expiry month.
    BinarySensorEntityDescription(
        key="payment_method_expiring",
        translation_key="payment_method_expiring",
        device_class=BinarySensorDeviceClass.PROBLEM,
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
    """Set up HelloFresh binary sensors."""
    coordinator: HelloFreshDataUpdateCoordinator = entry.runtime_data
    async_add_entities(HelloFreshBinarySensor(coordinator, description) for description in SENSORS)


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
    def available(self) -> bool:
        """Payment-method health is unavailable until the payments gateway has answered."""
        if self.entity_description.key == "payment_method_expiring":
            return super().available and self.coordinator.data.payment_method_expiring is not None
        return super().available

    @property
    def is_on(self) -> bool:
        """Return the state."""
        if self.entity_description.key == "payment_method_expiring":
            data = self.coordinator.data
            return bool(data.payment_method_expiring or data.payment_method_expired)
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
                "mdi:package-variant-closed-check" if is_on else "mdi:package-variant-closed-remove"
            )
        if self.entity_description.key == "payload_shape_changed":
            return "mdi:alert-octagon" if is_on else "mdi:check-circle-outline"
        if self.entity_description.key == "payment_method_expiring":
            return "mdi:credit-card-clock-outline" if is_on else "mdi:credit-card-check-outline"
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
        if self.entity_description.key == "payment_method_expiring":
            # The last four digits are the only part of the number HelloFresh returns; the
            # billing address in the same response is dropped at parse time.
            return {
                "expiring": data.payment_method_expiring,
                "expired": data.payment_method_expired,
                "card_type": data.payment_card_type,
                "card_provider": data.payment_card_provider,
                "card_brand": data.payment_card_brand,
                "card_last4": data.payment_card_last4,
                "card_expiry": data.payment_card_expiry,
            }
        return {
            "account_data_available": data.account_data_available,
            "capabilities": data.capabilities.as_dict(),
            "weeks_needing_selection": len(data.weeks_needing_selection),
            "order_count": len(data.orders),
            "subscription_count": data.subscription_count,
        }
