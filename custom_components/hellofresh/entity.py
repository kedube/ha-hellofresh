"""Entity helpers for HelloFresh."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import DOMAIN
from .coordinator import HelloFreshDataUpdateCoordinator
from .tracey import HelloFreshTraceyCoordinator


class HelloFreshCoordinatorEntity[
    CoordinatorT: HelloFreshDataUpdateCoordinator | HelloFreshTraceyCoordinator = (
        HelloFreshDataUpdateCoordinator
    )
](CoordinatorEntity[CoordinatorT]):
    """Base entity for HelloFresh.

    Driven by the account coordinator, unless a subclass names the live-tracking one
    (``HelloFreshCoordinatorEntity[HelloFreshTraceyCoordinator]``), so that ``coordinator.data``
    has the right type. Both coordinators carry the config entry this base reads.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CoordinatorT,
        entity_id_format: str,
        key: str,
        description: EntityDescription | None = None,
    ) -> None:
        """Identify the entity by ``key``: its unique id and its pinned entity id.

        ``entity_id_format`` is the platform's ``ENTITY_ID_FORMAT`` (e.g. ``"sensor.{}"``).
        ``description``, when given, becomes the entity description.
        """
        super().__init__(coordinator)
        if description is not None:
            self.entity_description = description
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{key}"
        self._pin_entity_id(entity_id_format, key)

    def _pin_entity_id(self, entity_id_format: str, key: str) -> None:
        """Pin ``self.entity_id`` to ``<domain>.<title-slug>_<key>``.

        With ``has_entity_name`` the entity_id would otherwise derive from the
        (translated, user-facing) display name, so renaming a sensor would silently
        change its entity_id. Setting ``entity_id`` explicitly suggests a stable id
        based on the ``key`` instead, so ids stay constant across display-name changes
        and match the ids the README and docs reference
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
