"""To-do platform for HelloFresh."""

from __future__ import annotations

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HelloFresh to-do entities."""
    coordinator: HelloFreshDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([HelloFreshMealSelectionTodo(coordinator)])


class HelloFreshMealSelectionTodo(HelloFreshCoordinatorEntity, TodoListEntity):
    """Read-only to-do list of weeks that still need meal selection."""

    _attr_translation_key = "meal_selection"
    _attr_supported_features = TodoListEntityFeature.UPDATE_TODO_ITEM
    _attr_icon = "mdi:format-list-checks"

    def __init__(self, coordinator: HelloFreshDataUpdateCoordinator) -> None:
        """Initialize the to-do entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_meal_selection"

    @property
    def todo_items(self) -> list[TodoItem] | None:
        """Return the list of to-do items."""
        items: list[TodoItem] = []
        for week in self.coordinator.data.weeks_needing_selection:
            meals_selected = week.meals_selected or 0
            meals_required = week.meals_required or 0
            description = (
                f"Select meals for {week.display_name}. "
                f"Selected {meals_selected} of {meals_required} meals."
            )
            items.append(
                TodoItem(
                    uid=week.week_id,
                    summary=f"Pick HelloFresh meals for {week.display_name}",
                    status=TodoItemStatus.NEEDS_ACTION,
                    due=(
                        week.selection_deadline.date()
                        if week.selection_deadline is not None
                        else week.delivery_date
                    ),
                    description=description,
                )
            )
        return items

    async def async_update_todo_item(self, item: TodoItem) -> None:
        """Treat completion as confirming the current recipe selection."""
        if item.uid is None:
            raise HomeAssistantError("A HelloFresh week id is required to update this to-do item.")

        week = self.coordinator.data.get_week(item.uid)
        if week is None:
            raise HomeAssistantError(f"HelloFresh week not found: {item.uid}")

        if item.status != TodoItemStatus.COMPLETED:
            return

        recipe_ids = [recipe.recipe_id for recipe in week.recipes if recipe.is_selected]
        if week.meals_required is not None and len(recipe_ids) != week.meals_required:
            raise HomeAssistantError(
                "This HelloFresh week does not have enough selected recipes to mark it complete."
            )

        try:
            await self.coordinator.client.async_select_meals(week.week_id, recipe_ids)
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
