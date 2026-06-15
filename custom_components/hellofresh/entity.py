"""Entity helpers for HelloFresh."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import DOMAIN
from .coordinator import HelloFreshDataUpdateCoordinator


class HelloFreshCoordinatorEntity(CoordinatorEntity[HelloFreshDataUpdateCoordinator]):
    """Base entity for HelloFresh."""

    _attr_has_entity_name = True

    def _stable_object_id(self, key: str) -> str:
        """Return a name-independent suggested object id: ``<title-slug>_<key>``.

        With ``has_entity_name`` the entity_id would otherwise derive from the
        (translated, user-facing) display name, so renaming a sensor would silently
        change its entity_id. Pinning the object id to the stable ``key`` keeps
        entity_ids constant across display-name changes and matches the IDs the
        README and example dashboard reference (e.g. ``sensor.hellofresh_us_<key>``).
        """
        return f"{slugify(self.coordinator.config_entry.title)}_{key}"

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
