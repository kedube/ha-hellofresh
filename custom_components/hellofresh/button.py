"""Button platform for HelloFresh."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import HelloFreshError
from .const import DOMAIN
from .coordinator import HelloFreshDataUpdateCoordinator
from .entity import HelloFreshCoordinatorEntity
from .issues import async_create_write_actions_issue

BUTTONS: tuple[ButtonEntityDescription, ...] = (
    ButtonEntityDescription(
        key="refresh_data",
        translation_key="refresh_data",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=True,
        icon="mdi:refresh",
    ),
    ButtonEntityDescription(
        key="confirm_next_selection",
        translation_key="confirm_next_selection",
        entity_registry_enabled_default=True,
        icon="mdi:check-bold",
    ),
    ButtonEntityDescription(
        key="skip_next_selection_week",
        translation_key="skip_next_selection_week",
        entity_registry_enabled_default=True,
        icon="mdi:calendar-remove-outline",
    ),
    ButtonEntityDescription(
        key="restore_next_skipped_week",
        translation_key="restore_next_skipped_week",
        entity_registry_enabled_default=True,
        icon="mdi:calendar-check-outline",
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

    async def async_press(self) -> None:
        """Handle the button press."""
        try:
            if self.entity_description.key == "refresh_data":
                await self.coordinator.async_request_refresh()
                return

            if self.entity_description.key == "confirm_next_selection":
                week = self.coordinator.data.next_selection_week
                if week is None:
                    raise HomeAssistantError("No HelloFresh week currently needs meal selection.")
                recipe_ids = [recipe.recipe_id for recipe in week.recipes if recipe.is_selected]
                if week.meals_required is not None and len(recipe_ids) != week.meals_required:
                    raise HomeAssistantError(
                        "The next HelloFresh week does not have enough selected recipes to confirm."
                    )
                await self.coordinator.client.async_select_meals(week.week_id, recipe_ids)
                await self.coordinator.async_request_refresh()
                return

            if self.entity_description.key == "skip_next_selection_week":
                week = self.coordinator.data.next_selection_week
                if week is None:
                    raise HomeAssistantError("No HelloFresh week is available to skip.")
                await self.coordinator.client.async_skip_week(week.week_id)
                await self.coordinator.async_request_refresh()
                return

            if self.entity_description.key == "restore_next_skipped_week":
                week = self.coordinator.data.next_skipped_week
                if week is None:
                    raise HomeAssistantError("No skipped HelloFresh week is available to restore.")
                await self.coordinator.client.async_unskip_week(week.week_id)
                await self.coordinator.async_request_refresh()
        except HelloFreshError as err:
            # A known integration/write failure: raise a Repairs issue and surface a
            # clean error. Unexpected exceptions are left to propagate as real bugs.
            async_create_write_actions_issue(
                self.coordinator.hass,
                self.coordinator.config_entry.entry_id,
                self.coordinator.config_entry.title,
            )
            raise HomeAssistantError(str(err)) from err
