"""Button platform for HelloFresh."""

from __future__ import annotations

from homeassistant.components.button import (
    ENTITY_ID_FORMAT,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import HelloFreshDataUpdateCoordinator
from .entity import HelloFreshCoordinatorEntity

BUTTONS: tuple[ButtonEntityDescription, ...] = (
    ButtonEntityDescription(
        key="refresh_data",
        translation_key="refresh_data",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=True,
        icon="mdi:refresh",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HelloFresh buttons."""
    coordinator: HelloFreshDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(HelloFreshButton(coordinator, description) for description in BUTTONS)


class HelloFreshButton(HelloFreshCoordinatorEntity, ButtonEntity):
    """HelloFresh button."""

    def __init__(
        self,
        coordinator: HelloFreshDataUpdateCoordinator,
        description: ButtonEntityDescription,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{description.key}"
        self._pin_entity_id(ENTITY_ID_FORMAT, description.key)

    async def async_press(self) -> None:
        """Handle the button press."""
        if self.entity_description.key == "refresh_data":
            await self.coordinator.async_request_refresh()
