"""Sensor platform for HelloFresh."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import HelloFreshDataUpdateCoordinator
from .entity import HelloFreshCoordinatorEntity
from .sensor_helpers import (
    sensor_extra_state_attributes,
    sensor_icon,
    sensor_native_value,
    token_days_remaining,
    token_minutes_remaining,
    token_seconds_remaining,
)

_MONETARY_DEVICE_CLASS = getattr(SensorDeviceClass, "MONETARY", None)

SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="next_delivery_date",
        translation_key="next_delivery_date",
        device_class=SensorDeviceClass.DATE,
        icon="mdi:calendar-check-outline",
    ),
    SensorEntityDescription(
        key="next_order_status",
        translation_key="next_order_status",
        icon="mdi:package-variant-closed-check",
    ),
    SensorEntityDescription(
        key="shipment_tracking_status",
        translation_key="shipment_tracking_status",
        icon="mdi:truck-delivery-outline",
    ),
    SensorEntityDescription(
        key="shipment_tracking_number",
        translation_key="shipment_tracking_number",
        icon="mdi:barcode",
    ),
    SensorEntityDescription(
        key="weeks_needing_selection",
        translation_key="weeks_needing_selection",
        icon="mdi:calendar-alert-outline",
    ),
    SensorEntityDescription(
        key="next_selection_deadline",
        translation_key="next_selection_deadline",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-alert-outline",
    ),
    SensorEntityDescription(
        key="selected_meal_count",
        translation_key="selected_meal_count",
        icon="mdi:silverware-fork-knife",
    ),
    SensorEntityDescription(
        key="required_meal_count",
        translation_key="required_meal_count",
        icon="mdi:numeric",
    ),
    SensorEntityDescription(
        key="delivery_count_this_week",
        translation_key="delivery_count_this_week",
        icon="mdi:truck-fast-outline",
    ),
    SensorEntityDescription(
        key="next_box_total_price",
        translation_key="next_box_total_price",
        device_class=_MONETARY_DEVICE_CLASS,
        icon="mdi:cash",
    ),
    SensorEntityDescription(
        key="selected_plan",
        translation_key="selected_plan",
        icon="mdi:food-variant",
    ),
    SensorEntityDescription(
        key="public_menu_recipe_count",
        translation_key="public_menu_recipe_count",
        icon="mdi:chef-hat",
    ),
    SensorEntityDescription(
        key="subscription_count",
        translation_key="subscription_count",
        icon="mdi:account-multiple-outline",
    ),
    SensorEntityDescription(
        key="number_of_people",
        translation_key="number_of_people",
        icon="mdi:account-group-outline",
    ),
    SensorEntityDescription(
        key="delivery_address",
        translation_key="delivery_address",
        icon="mdi:map-marker-outline",
    ),
    SensorEntityDescription(
        key="recent_payment_date",
        translation_key="recent_payment_date",
        device_class=SensorDeviceClass.DATE,
        icon="mdi:credit-card-check-outline",
    ),
    SensorEntityDescription(
        key="next_payment_date",
        translation_key="next_payment_date",
        device_class=SensorDeviceClass.DATE,
        icon="mdi:credit-card-clock-outline",
    ),
    SensorEntityDescription(
        key="upcoming_delivery_count",
        translation_key="upcoming_delivery_count",
        icon="mdi:truck-fast-outline",
    ),
    SensorEntityDescription(
        key="next_delivery_subscription",
        translation_key="next_delivery_subscription",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:account-box-outline",
    ),
    SensorEntityDescription(
        key="next_delivery_slot",
        translation_key="next_delivery_slot",
        icon="mdi:clock-time-four-outline",
    ),
    SensorEntityDescription(
        key="tracked_shipment_carrier",
        translation_key="tracked_shipment_carrier",
        icon="mdi:truck-outline",
    ),
    SensorEntityDescription(
        key="skipped_week_count",
        translation_key="skipped_week_count",
        icon="mdi:calendar-remove-outline",
    ),
    SensorEntityDescription(
        key="next_skipped_week",
        translation_key="next_skipped_week",
        icon="mdi:calendar-arrow-right",
    ),
    SensorEntityDescription(
        key="boxes_received",
        translation_key="boxes_received",
        icon="mdi:package-check",
    ),
    SensorEntityDescription(
        key="last_delivery_date",
        translation_key="last_delivery_date",
        device_class=SensorDeviceClass.DATE,
        icon="mdi:calendar-end-outline",
    ),
    SensorEntityDescription(
        key="next_delivery_week",
        translation_key="next_delivery_week",
        # ISO week label (e.g. "2026-W25"), not a date — no DATE device class.
        icon="mdi:calendar-week-outline",
    ),
    SensorEntityDescription(
        key="api_base_url",
        translation_key="api_base_url",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:api",
    ),
    SensorEntityDescription(
        key="account_id",
        translation_key="account_id",
        icon="mdi:account-circle-outline",
    ),
    SensorEntityDescription(
        key="recent_order_id",
        translation_key="recent_order_id",
        icon="mdi:receipt-text-outline",
    ),
    SensorEntityDescription(
        key="next_box_coupon",
        translation_key="next_box_coupon",
        icon="mdi:ticket-percent-outline",
    ),
    SensorEntityDescription(
        key="next_delivery_tracking_url",
        translation_key="next_delivery_tracking_url",
        icon="mdi:link-variant",
    ),
    SensorEntityDescription(
        key="next_holiday_delivery_date",
        translation_key="next_holiday_delivery_date",
        device_class=SensorDeviceClass.DATE,
        icon="mdi:calendar-star",
    ),
    SensorEntityDescription(
        key="next_holiday_message",
        translation_key="next_holiday_message",
        icon="mdi:calendar-text-outline",
    ),
    SensorEntityDescription(
        key="next_delivery_blocked",
        translation_key="next_delivery_blocked",
        icon="mdi:calendar-lock-outline",
    ),
    SensorEntityDescription(
        key="access_token_minutes_remaining",
        translation_key="access_token_minutes_remaining",
        native_unit_of_measurement="min",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:key-outline",
    ),
    SensorEntityDescription(
        key="refresh_token_days_remaining",
        translation_key="refresh_token_days_remaining",
        native_unit_of_measurement="d",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:key-chain-variant",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HelloFresh sensors."""
    coordinator: HelloFreshDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(HelloFreshSensor(coordinator, description) for description in SENSORS)


class HelloFreshSensor(HelloFreshCoordinatorEntity, SensorEntity):
    """HelloFresh sensor."""

    def __init__(
        self,
        coordinator: HelloFreshDataUpdateCoordinator,
        description: SensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{description.key}"

    @property
    def native_value(self):
        """Return the sensor value."""
        if self.entity_description.key == "access_token_minutes_remaining":
            return token_minutes_remaining(self.coordinator.client.token_expires_at)
        if self.entity_description.key == "refresh_token_days_remaining":
            return token_days_remaining(self.coordinator.client.refresh_token_expires_at)
        return sensor_native_value(
            self.entity_description.key,
            self.coordinator.data,
            self.coordinator.client.base_url,
        )

    @property
    def icon(self) -> str | None:
        """Return an icon that reflects the current sensor state when useful."""
        return sensor_icon(
            self.entity_description.key,
            self.coordinator.data,
            self.entity_description.icon,
        )

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit of measurement."""
        if self.entity_description.key == "next_box_total_price":
            return self.coordinator.data.next_delivery_total_currency
        return self.entity_description.native_unit_of_measurement

    @property
    def suggested_display_precision(self) -> int | None:
        """Return the suggested display precision where supported by Home Assistant."""
        if self.entity_description.key == "next_box_total_price":
            return 2
        return None

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Return extra attributes."""
        if self.entity_description.key == "access_token_minutes_remaining":
            expires_at = self.coordinator.client.token_expires_at
            return {
                "expires_at": expires_at.isoformat() if expires_at else None,
                "seconds_remaining": token_seconds_remaining(expires_at),
            }
        if self.entity_description.key == "refresh_token_days_remaining":
            expires_at = self.coordinator.client.refresh_token_expires_at
            return {
                "expires_at": expires_at.isoformat() if expires_at else None,
                "seconds_remaining": token_seconds_remaining(expires_at),
            }
        return sensor_extra_state_attributes(
            self.entity_description.key,
            self.coordinator.data,
        )
