"""Switch platform for HelloFresh."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import (
    ENTITY_ID_FORMAT,
    SwitchEntity,
    SwitchEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import HelloFreshError
from .coordinator import (
    HelloFreshConfigEntry,
    HelloFreshDataUpdateCoordinator,
    async_client_error,
    async_write_succeeded,
)
from .entity import HelloFreshCoordinatorEntity

SWITCHES: tuple[SwitchEntityDescription, ...] = (
    SwitchEntityDescription(
        key="skip_next_modifiable_week",
        translation_key="skip_next_modifiable_week",
        icon="mdi:calendar-remove-outline",
    ),
)


# Coordinator-based: entities never poll on their own, so entity-update parallelism is
# irrelevant — declared 0 (unlimited) per the integration quality scale's convention.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HelloFreshConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up HelloFresh switches."""
    coordinator = entry.runtime_data
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
        super().__init__(coordinator, ENTITY_ID_FORMAT, description.key, description)

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
        except HelloFreshError as err:
            # The rule the services use: a rejected sign-in starts reauth, an action
            # HelloFresh has no endpoint for raises the repair, and anything else (a timeout,
            # a 5xx) is only reported. Unexpected exceptions propagate as real bugs.
            raise async_client_error(
                self.coordinator.hass, self.coordinator.config_entry, err
            ) from err
        async_write_succeeded(self.coordinator.hass, self.coordinator.config_entry)
        await self.coordinator.async_after_write()
