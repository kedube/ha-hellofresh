"""Select platform for HelloFresh: recurring box size and delivery day."""

from __future__ import annotations

import logging

from homeassistant.components.select import (
    ENTITY_ID_FORMAT,
    SelectEntity,
    SelectEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import HelloFreshAuthError, HelloFreshError, HelloFreshSubscription
from .coordinator import HelloFreshDataUpdateCoordinator
from .entity import HelloFreshCoordinatorEntity
from .issues import async_create_write_actions_issue, async_delete_write_actions_issue

_LOGGER = logging.getLogger(__name__)

SELECTS: tuple[SelectEntityDescription, ...] = (
    SelectEntityDescription(
        key="box_size",
        translation_key="box_size",
        icon="mdi:package-variant",
    ),
    SelectEntityDescription(
        key="delivery_day",
        translation_key="delivery_day",
        icon="mdi:calendar-clock",
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
    """Set up HelloFresh select entities."""
    coordinator: HelloFreshDataUpdateCoordinator = entry.runtime_data
    entities: list[SelectEntity] = []
    for description in SELECTS:
        if description.key == "box_size":
            entities.append(HelloFreshBoxSizeSelect(coordinator, description))
        else:
            entities.append(HelloFreshDeliveryDaySelect(coordinator, description))
    async_add_entities(entities)


class HelloFreshSelect(HelloFreshCoordinatorEntity, SelectEntity):
    """Base for the two recurring-plan selects.

    Both act on the primary subscription and write through the same client calls as the
    ``change_plan`` / ``change_delivery_weekday`` services — RECURRING changes affecting every
    future box (and, for the box size, what is billed). The option catalog is not part of the
    regular poll, so it is fetched when the entity is added and again after each write; until
    it has loaded the entity is unavailable rather than offering an empty list.
    """

    def __init__(
        self,
        coordinator: HelloFreshDataUpdateCoordinator,
        description: SelectEntityDescription,
    ) -> None:
        """Initialize the select."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{description.key}"
        self._pin_entity_id(ENTITY_ID_FORMAT, description.key)
        self._attr_options = []
        # Display label -> HelloFresh handle, in the API's order.
        self._handles_by_label: dict[str, str] = {}
        self._options_loaded = False

    @property
    def _subscription(self) -> HelloFreshSubscription | None:
        subscriptions = self.coordinator.data.subscriptions if self.coordinator.data else []
        return subscriptions[0] if subscriptions else None

    @property
    def available(self) -> bool:
        """Available once the option catalog has loaded for a known subscription."""
        return super().available and self._subscription is not None and bool(self._attr_options)

    async def async_added_to_hass(self) -> None:
        """Load the option catalog once the entity is live."""
        await super().async_added_to_hass()
        await self._async_load_options()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Retry the catalog load on later polls if the first attempt failed."""
        if not self._options_loaded and self.hass is not None:
            self.hass.async_create_task(self._async_load_options())
        super()._handle_coordinator_update()

    async def _async_load_options(self) -> None:
        """Fetch the option catalog; a failure leaves the entity unavailable, not broken."""
        subscription = self._subscription
        if subscription is None:
            return
        try:
            labelled = await self._async_fetch_options(subscription)
        except HelloFreshAuthError:
            # Reauth is the coordinator's job; don't compete with it from an entity.
            return
        except HelloFreshError as err:
            _LOGGER.debug("HelloFresh %s options unavailable: %s", self.entity_description.key, err)
            return
        handles: dict[str, str] = {}
        for label, handle in labelled:
            if label and handle and label not in handles:
                handles[label] = handle
        self._handles_by_label = handles
        self._attr_options = list(handles)
        self._options_loaded = True
        self.async_write_ha_state()

    async def _async_fetch_options(
        self, subscription: HelloFreshSubscription
    ) -> list[tuple[str, str]]:
        """Return (label, handle) pairs for the subscription."""
        raise NotImplementedError

    def _current_handle(self, subscription: HelloFreshSubscription) -> str | None:
        """Return the handle the subscription currently uses."""
        raise NotImplementedError

    async def _async_write(self, handle: str, subscription: HelloFreshSubscription) -> None:
        """Apply the chosen handle."""
        raise NotImplementedError

    @property
    def current_option(self) -> str | None:
        """Return the label matching the subscription's current handle."""
        subscription = self._subscription
        if subscription is None:
            return None
        handle = self._current_handle(subscription)
        if handle is None:
            return None
        for label, candidate in self._handles_by_label.items():
            if candidate == handle:
                return label
        return None

    async def async_select_option(self, option: str) -> None:
        """Write the chosen option and refresh."""
        handle = self._handles_by_label.get(option)
        subscription = self._subscription
        if handle is None or subscription is None:
            raise HomeAssistantError(f"Unknown HelloFresh option: {option}")
        if handle == self._current_handle(subscription):
            return
        try:
            await self._async_write(handle, subscription)
            # A write just succeeded — clear any stale "write actions unavailable" warning.
            async_delete_write_actions_issue(
                self.coordinator.hass, self.coordinator.config_entry.entry_id
            )
        except HelloFreshError as err:
            async_create_write_actions_issue(
                self.coordinator.hass,
                self.coordinator.config_entry.entry_id,
                self.coordinator.config_entry.title,
            )
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()
        await self._async_load_options()


class HelloFreshBoxSizeSelect(HelloFreshSelect):
    """Recurring box size (meals per week × servings), from the plan's product catalog."""

    async def _async_fetch_options(
        self, subscription: HelloFreshSubscription
    ) -> list[tuple[str, str]]:
        options = await self.coordinator.client.async_list_plan_options(
            subscription.subscription_id
        )
        labelled: list[tuple[str, str]] = []
        for option in options:
            handle = option.get("handle")
            meals = option.get("meals")
            servings = option.get("servings")
            if isinstance(meals, int) and isinstance(servings, int):
                label = f"{meals} meals × {servings} servings"
            else:
                label = str(option.get("name") or handle or "")
            if isinstance(handle, str) and handle:
                labelled.append((label, handle))
        return labelled

    def _current_handle(self, subscription: HelloFreshSubscription) -> str | None:
        raw = subscription.raw if isinstance(subscription.raw, dict) else {}
        product = raw.get("product")
        sku = product.get("sku") if isinstance(product, dict) else None
        if isinstance(sku, str) and sku:
            return sku
        maintained = raw.get("maintainedSku")
        if isinstance(maintained, str) and maintained:
            return maintained
        # Fall back to the option whose meal/serving counts match the subscription.
        wanted = f"{subscription.meals_required} meals × {subscription.servings} servings"
        return self._handles_by_label.get(wanted)

    async def _async_write(self, handle: str, subscription: HelloFreshSubscription) -> None:
        await self.coordinator.client.async_change_plan(handle, subscription.subscription_id)


class HelloFreshDeliveryDaySelect(HelloFreshSelect):
    """Recurring delivery day/slot, from the plan's delivery-date options."""

    async def _async_fetch_options(
        self, subscription: HelloFreshSubscription
    ) -> list[tuple[str, str]]:
        options = await self.coordinator.client.async_get_delivery_options(subscription)
        return [(option.delivery_name or option.handle, option.handle) for option in options]

    def _current_handle(self, subscription: HelloFreshSubscription) -> str | None:
        raw = subscription.raw if isinstance(subscription.raw, dict) else {}
        for key in ("deliveryTime", "nextDeliveryTime"):
            handle = raw.get(key)
            if isinstance(handle, str) and handle:
                return handle
        if isinstance(subscription.next_delivery_time, str) and subscription.next_delivery_time:
            return subscription.next_delivery_time
        return None

    async def _async_write(self, handle: str, subscription: HelloFreshSubscription) -> None:
        await self.coordinator.client.async_change_delivery_weekday(
            handle, 1, subscription.subscription_id
        )
