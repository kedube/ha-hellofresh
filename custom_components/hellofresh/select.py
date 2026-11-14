"""Select platform for HelloFresh: recurring box size and delivery day."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.select import (
    ENTITY_ID_FORMAT,
    SelectEntity,
    SelectEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import HelloFreshAuthError, HelloFreshError, HelloFreshSubscription
from .coordinator import (
    HelloFreshConfigEntry,
    HelloFreshDataUpdateCoordinator,
    async_client_error,
    async_write_succeeded,
)
from .entity import HelloFreshCoordinatorEntity

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
    entry: HelloFreshConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up HelloFresh select entities."""
    coordinator = entry.runtime_data
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
    regular poll, so it is fetched in the background when the entity is added and again after
    each write; until it has loaded the entity is unavailable rather than offering an empty
    list.
    """

    def __init__(
        self,
        coordinator: HelloFreshDataUpdateCoordinator,
        description: SelectEntityDescription,
    ) -> None:
        """Initialize the select."""
        super().__init__(coordinator, ENTITY_ID_FORMAT, description.key, description)
        self._attr_options = []
        # Display label -> HelloFresh handle, in the API's order.
        self._handles_by_label: dict[str, str] = {}
        self._options_loaded = False
        self._load_task: asyncio.Task[None] | None = None

    @property
    def _subscription(self) -> HelloFreshSubscription | None:
        subscriptions = self.coordinator.data.subscriptions if self.coordinator.data else []
        return subscriptions[0] if subscriptions else None

    @property
    def available(self) -> bool:
        """Available once the option catalog has loaded for a known subscription."""
        return super().available and self._subscription is not None and bool(self._attr_options)

    async def async_added_to_hass(self) -> None:
        """Start loading the option catalog once the entity is live.

        Not awaited: Home Assistant waits for every entity to finish being added before the
        entry counts as set up, and this is a request to HelloFresh.
        """
        await super().async_added_to_hass()
        self._schedule_options_load()

    async def async_will_remove_from_hass(self) -> None:
        """Stop a catalog load still running when the entity goes away."""
        await super().async_will_remove_from_hass()
        if self._load_task is not None:
            self._load_task.cancel()
            self._load_task = None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Retry the catalog load on later polls if the first attempt failed."""
        if not self._options_loaded and self.hass is not None:
            self._schedule_options_load()
        super()._handle_coordinator_update()

    @callback
    def _schedule_options_load(self) -> None:
        """Load the catalog in a task that belongs to the config entry, one at a time.

        The entry cancels its background tasks when it unloads, so a slow load cannot outlive
        it, and a second request while one is running is dropped rather than doubled.
        """
        if self._load_task is not None and not self._load_task.done():
            return
        self._load_task = self.coordinator.config_entry.async_create_background_task(
            self.hass,
            self._async_load_options(),
            name=f"hellofresh {self.entity_description.key} options",
        )

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
            raise ServiceValidationError(f"Unknown HelloFresh option: {option}")
        if handle == self._current_handle(subscription):
            return
        try:
            await self._async_write(handle, subscription)
        except HelloFreshError as err:
            # The rule the services use: a rejected sign-in starts reauth, an action HelloFresh
            # has no endpoint for raises the repair, and anything else (a timeout, a 5xx) is
            # only reported. A timeout here used to raise the repair as well.
            raise async_client_error(
                self.coordinator.hass, self.coordinator.config_entry, err
            ) from err
        async_write_succeeded(self.coordinator.hass, self.coordinator.config_entry)
        await self.coordinator.async_after_write()
        await self._async_load_options()


def _box_size_label(meals: int, people: int) -> str:
    """A box size the way HelloFresh words it: ``3 meals for 2 people``."""
    meal_word = "meal" if meals == 1 else "meals"
    people_word = "person" if people == 1 else "people"
    return f"{meals} {meal_word} for {people} {people_word}"


class HelloFreshBoxSizeSelect(HelloFreshSelect):
    """Recurring box size (meals per week, number of people), from the plan's product catalog."""

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
            people = option.get("servings")
            if isinstance(meals, int) and isinstance(people, int):
                label = _box_size_label(meals, people)
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
        # Fall back to the option whose meal and people counts match the subscription.
        if isinstance(subscription.meals_required, int) and isinstance(subscription.servings, int):
            return self._handles_by_label.get(
                _box_size_label(subscription.meals_required, subscription.servings)
            )
        return None

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
