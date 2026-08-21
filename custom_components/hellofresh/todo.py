"""Todo platform for HelloFresh: the pantry-prep list for an upcoming delivery.

This is **not** a general shopping list, and deliberately not a user-authored one. It is a
projection of one delivery week: the ingredients its selected meals need that HelloFresh does
*not* put in the box (`shipped == False` — salt, oil, butter, eggs), so they can be on hand
before the box lands rather than discovered mid-recipe.

Scoping it to a single week is what makes it tractable. A rolling "missing ingredients" list
would have to answer whether last week's butter still counts, and would have to sum amounts
across weeks whose units are free-form strings from the API ("1.5 tablespoon" vs "2 tbsp").
Anchored to one delivery there is nothing to carry over: the list is built for that week,
checked off in the days before it arrives, and replaced wholesale when the next week becomes
the anchor.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.todo import (
    ENTITY_ID_FORMAT,
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import HelloFreshDataUpdateCoordinator
from .entity import HelloFreshCoordinatorEntity
from .models import HelloFreshWeek

_LOGGER = logging.getLogger(__name__)

# Cap the per-refresh recipe-detail fetches. A week holds a handful of selected meals, but a
# malformed payload could nominate far more; this keeps one bad poll from issuing dozens of
# round-trips.
_MAX_RECIPE_FETCHES = 12


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the HelloFresh prep-list todo entity."""
    coordinator: HelloFreshDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([HelloFreshPrepListTodo(coordinator)])


def _ingredient_key(name: str) -> str:
    """Return a stable identity for an ingredient line.

    Check-off state is keyed on this rather than list position, so re-fetching the week (or
    the user swapping one meal) doesn't shuffle which items look completed. Case- and
    whitespace-insensitive because the same staple is not spelled consistently across recipes.
    """
    return " ".join(name.split()).casefold()


def _format_amounts(amounts: list[tuple[Any, Any]]) -> str:
    """Render the per-recipe amounts for one ingredient.

    Amounts are **not** summed. ``amount``/``unit`` are free-form API values, and two recipes
    can express the same staple in different units ("1.5 tablespoon" vs "2 tbsp"), so adding
    them would invent a precision the data does not have. Identical amounts are collapsed to
    an "N x" multiple; anything else is listed side by side and left to the cook.
    """
    rendered: list[str] = []
    for amount, unit in amounts:
        if amount is None and unit is None:
            continue
        parts = [str(amount).rstrip("0").rstrip(".") if isinstance(amount, float) else amount]
        if unit:
            parts.append(str(unit))
        text = " ".join(str(p) for p in parts if p not in (None, ""))
        if text:
            rendered.append(text)
    if not rendered:
        return ""
    if len(set(rendered)) == 1 and len(rendered) > 1:
        return f"{len(rendered)} x {rendered[0]}"
    return " + ".join(rendered)


class HelloFreshPrepListTodo(HelloFreshCoordinatorEntity, TodoListEntity):
    """Pantry staples to have on hand before the next HelloFresh box arrives."""

    _attr_translation_key = "prep_list"
    # Read-mostly on purpose. The list is derived from the week's selected meals, so items
    # appear and vanish as the selection changes; letting the user add or delete rows would
    # put their edits in a fight with every refresh. Check-off is the one write that makes
    # sense, and it lives outside the projection.
    _attr_supported_features = TodoListEntityFeature.UPDATE_TODO_ITEM

    def __init__(self, coordinator: HelloFreshDataUpdateCoordinator) -> None:
        """Initialize the prep list."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_prep_list"
        self._pin_entity_id(ENTITY_ID_FORMAT, "prep_list")
        # Ingredient keys the user has ticked off, plus the week they belong to. Completion is
        # per-week: when the anchor week rolls over, the previous week's ticks are dropped
        # rather than carried onto a fresh box's list.
        self._completed: set[str] = set()
        self._completed_week: str | None = None
        self._items: list[TodoItem] = []
        self._built_for: tuple[str | None, tuple[str, ...]] = (None, ())

    @property
    def todo_items(self) -> list[TodoItem] | None:
        """Return the current prep list."""
        return self._items

    def _anchor_week(self) -> HelloFreshWeek | None:
        """Return the delivery week the list is built for.

        Prefers the subscription's own next-delivery handle (the box actually on its way) and
        falls back to the next configurable week, which is what the cards treat as "upcoming"
        when the handle is missing.
        """
        data = self.coordinator.data
        if data is None:
            return None
        return data.next_delivery_week_obj or data.next_configurable_week

    def _selected_recipe_ids(self, week: HelloFreshWeek) -> list[str]:
        """Return the recipe ids this week will actually ship."""
        ids: list[str] = []
        for recipe in week.recipes:
            if not recipe.is_selected:
                continue
            recipe_id = getattr(recipe, "recipe_id", None)
            if isinstance(recipe_id, str) and recipe_id and recipe_id not in ids:
                ids.append(recipe_id)
        return ids

    async def async_added_to_hass(self) -> None:
        """Build the list once the entity is live."""
        await super().async_added_to_hass()
        await self._async_rebuild()
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """React to a coordinator poll.

        ``CoordinatorEntity`` is a *synchronous* callback that only writes state, and
        ``should_poll`` is False for coordinator entities — so neither hook can await the
        per-recipe detail fetch this list needs. The rebuild is therefore scheduled as a task
        and writes state when it finishes. Without this the list would be built once at
        startup and then never change as meals are swapped or a new week became the anchor.
        """
        self.hass.async_create_task(self._async_rebuild_and_write())

    async def _async_rebuild_and_write(self) -> None:
        """Rebuild off the event loop's critical path, then publish."""
        try:
            await self._async_rebuild()
        except Exception:  # noqa: BLE001 - a background task must not raise into the loop
            _LOGGER.exception("Prep list refresh failed")
            return
        self.async_write_ha_state()

    async def _async_rebuild(self) -> None:
        """Refresh the prep list from the anchor week's selected meals.

        Recipe detail is fetched per selected meal (the week's own recipes carry ingredient
        *names* only, without amounts or the shipped flag), so this is skipped entirely unless
        the anchor week or its selection actually changed.
        """
        week = self._anchor_week()
        if week is None:
            self._items = []
            self._built_for = (None, ())
            return

        recipe_ids = self._selected_recipe_ids(week)
        fingerprint = (week.week_id, tuple(recipe_ids))
        if fingerprint == self._built_for:
            return

        # A new anchor week means the previous week's box has landed; its ticks no longer apply.
        if self._completed_week != week.week_id:
            self._completed = set()
            self._completed_week = week.week_id

        servings = None
        subscription = self.coordinator.data.primary_subscription if self.coordinator.data else None
        if subscription is not None:
            servings = subscription.servings

        # name -> (display name, [(amount, unit), ...])
        collected: dict[str, tuple[str, list[tuple[Any, Any]]]] = {}
        fetched = 0
        for recipe_id in recipe_ids:
            if fetched >= _MAX_RECIPE_FETCHES:
                _LOGGER.debug(
                    "Prep list stopped after %s recipes for week %s", fetched, week.week_id
                )
                break
            try:
                detail = await self.coordinator.client.async_get_recipe_detail(
                    recipe_id, servings=servings, include_favorite=False
                )
            except Exception as err:  # noqa: BLE001 - one bad recipe must not blank the list
                _LOGGER.debug("Prep list could not read recipe %s: %s", recipe_id, err)
                continue
            fetched += 1
            for ingredient in detail.ingredients:
                # Tri-state: only an explicit False means "you supply this". A missing flag
                # (None) is unknown, and is treated as in-box — telling someone to buy an
                # ingredient HelloFresh is already shipping is the worse error.
                if ingredient.get("shipped") is not False:
                    continue
                name = ingredient.get("name")
                if not isinstance(name, str) or not name.strip():
                    continue
                key = _ingredient_key(name)
                display, amounts = collected.setdefault(key, (name.strip(), []))
                amounts.append((ingredient.get("amount"), ingredient.get("unit")))

        items: list[TodoItem] = []
        for key, (display, amounts) in sorted(collected.items(), key=lambda kv: kv[1][0].lower()):
            summary = display
            detail_text = _format_amounts(amounts)
            if detail_text:
                summary = f"{display} — {detail_text}"
            items.append(
                TodoItem(
                    uid=key,
                    summary=summary,
                    status=(
                        TodoItemStatus.COMPLETED
                        if key in self._completed
                        else TodoItemStatus.NEEDS_ACTION
                    ),
                    # The whole list is due the day the box arrives: that is the deadline for
                    # having these on hand.
                    due=week.delivery_date,
                )
            )

        self._items = items
        self._built_for = fingerprint

    async def async_update_todo_item(self, item: TodoItem) -> None:
        """Record a check-off (or un-check) for one ingredient."""
        if item.uid is None:
            return
        if item.status == TodoItemStatus.COMPLETED:
            self._completed.add(item.uid)
        else:
            self._completed.discard(item.uid)
        for index, existing in enumerate(self._items):
            if existing.uid == item.uid:
                self._items[index] = TodoItem(
                    uid=existing.uid,
                    summary=existing.summary,
                    status=item.status or existing.status,
                    due=existing.due,
                )
                break
        self.async_write_ha_state()
