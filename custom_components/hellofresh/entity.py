"""Entity helpers for HelloFresh."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HelloFreshDataUpdateCoordinator


class HelloFreshCoordinatorEntity(CoordinatorEntity[HelloFreshDataUpdateCoordinator]):
    """Base entity for HelloFresh."""

    _attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.config_entry.entry_id)},
            entry_type=DeviceEntryType.SERVICE,
            manufacturer="HelloFresh",
            model="Customer Account",
            name=self.coordinator.config_entry.title,
        )
