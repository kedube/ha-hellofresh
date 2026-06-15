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

    def _pin_entity_id(self, entity_id_format: str, key: str) -> None:
        """Pin ``self.entity_id`` to ``<domain>.<title-slug>_<key>``.

        With ``has_entity_name`` the entity_id would otherwise derive from the
        (translated, user-facing) display name, so renaming a sensor would silently
        change its entity_id. Setting ``entity_id`` explicitly suggests a stable id
        based on the ``key`` instead, so ids stay constant across display-name changes
        and match the ids the README and example dashboard reference
        (e.g. ``sensor.hellofresh_us_<key>``). ``entity_id_format`` is the platform's
        ``ENTITY_ID_FORMAT`` (e.g. ``"sensor.{}"``).
        """
        object_id = f"{slugify(self.coordinator.config_entry.title)}_{key}"
        self.entity_id = entity_id_format.format(object_id)

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
