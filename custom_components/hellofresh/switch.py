"""Switch platform for HelloFresh."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import (
    ENTITY_ID_FORMAT,
    SwitchEntity,
    SwitchEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import HelloFreshError
from .const import DOMAIN
from .coordinator import HelloFreshDataUpdateCoordinator
from .entity import HelloFreshCoordinatorEntity
from .issues import async_create_write_actions_issue

SWITCHES: tuple[SwitchEntityDescription, ...] = (
    SwitchEntityDescription(
        key="skip_next_modifiable_week",
        translation_key="skip_next_modifiable_week",
        icon="mdi:calendar-remove-outline",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HelloFresh switches."""
    coordinator: HelloFreshDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(HelloFreshSwitch(coordinator, description) for description in SWITCHES)


class HelloFreshSwitch(HelloFreshCoordinatorEntity, SwitchEntity):
    """Switch that skips or ships the next modifiable HelloFresh delivery week.

    ``on`` skips the week (no box ships); ``off`` restores it so the box ships. The
    target is the subscription's next *modifiable* delivery week, so toggling never
    affects a week that has already been locked in or delivered.
    """

    def __init__(
        self,
        coordinator: HelloFreshDataUpdateCoordinator,
        description: SwitchEntityDescription,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{description.key}"
        self._pin_entity_id(ENTITY_ID_FORMAT, description.key)

    @property
    def available(self) -> bool:
        """Available only while a modifiable week is known."""
        return super().available and self.coordinator.data.next_modifiable_week is not None

    @property
    def is_on(self) -> bool | None:
        """Return True when the next modifiable week is currently skipped."""
        week = self.coordinator.data.next_modifiable_week
        if week is None:
            return None
        return week.is_skipped

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Skip the next modifiable delivery week."""
        await self._set_skipped(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Restore the next modifiable delivery week so the box ships."""
        await self._set_skipped(False)

    async def _set_skipped(self, skipped: bool) -> None:
        """Skip or unskip the next modifiable week and refresh."""
        week = self.coordinator.data.next_modifiable_week
        if week is None:
            raise HomeAssistantError(
                "No modifiable HelloFresh delivery week is currently available."
            )
        if week.is_skipped == skipped:
            return
        try:
            if skipped:
                await self.coordinator.client.async_skip_week(week.week_id)
            else:
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
