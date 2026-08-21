"""Todo platform for HelloFresh: the pantry-prep list for an upcoming delivery.

This is **not** a general shopping list, and deliberately not a user-authored one. It is a
projection of the next two delivery weeks: the ingredients their selected meals need that
HelloFresh does *not* put in the box (`shipped == False` — salt, oil, butter, eggs), so they
can be on hand before each box lands rather than discovered mid-recipe. Covering the current
box and the one after it lets a single shopping trip serve both.

Keeping the weeks *separate* is what makes it tractable. A rolling "missing ingredients" list
would have to answer whether last week's butter still counts, and whether a staple bought two
weeks ago is still in the cupboard. Here every item belongs to exactly one delivery: it is
grouped, totalled, and due against that box, and it disappears once that box has landed —
while the following week's items, and anything already ticked on them, carry straight over.

Within a week, amounts for the same ingredient **are** added up per unit — two recipes wanting
2 tablespoon of butter ask for 4 tablespoon, which is what you need to buy. Amounts are never
summed *across* weeks: each box is its own shopping trip. Only across *differing* units are
they left separate; see :func:`_format_amounts`.
"""

from __future__ import annotations

from datetime import date
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

# How many upcoming delivery weeks the list covers: the box on its way, plus the one after it,
# so a single shopping trip can serve both.
_WEEKS_COVERED = 2

# Cap the recipe-detail fetches per refresh, across all covered weeks. Each week holds a
# handful of selected meals, but a malformed payload could nominate far more; this keeps one
# bad poll from issuing dozens of round-trips.
_MAX_RECIPE_FETCHES = 24


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


def _week_label(week: HelloFreshWeek) -> str:
    """Return a human label naming the box an item belongs to.

    Carried in each item's ``description`` so the week is still identifiable in to-do views
    that don't surface due dates.
    """
    if week.delivery_date is not None:
        return f"Delivery {week.delivery_date.isoformat()}"
    return week.display_name or week.week_id


def _coerce_amount(value: Any) -> float | None:
    """Return ``value`` as a number, or None when it isn't one.

    Amounts arrive as ints, floats, or numeric strings depending on the recipe. Anything that
    isn't cleanly numeric (a range like "1-2", a word like "some") must not be summed.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _trim_number(value: float) -> str:
    """Render a summed amount without trailing zeros (3.0 -> "3", 1.50 -> "1.5")."""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def _unit_key(unit: Any) -> str:
    """Return a comparison key for a unit, so "Tablespoon" and "tablespoon" match."""
    return " ".join(str(unit or "").split()).casefold()


def _format_amounts(amounts: list[tuple[Any, Any]]) -> str:
    """Render the total of one ingredient across the week's recipes.

    Amounts **are** summed, but only within a single unit: two recipes each wanting
    ``2 tablespoon`` become ``4 tablespoon`` rather than "2 tablespoon + 2 tablespoon", which
    is what you actually need to have on hand.

    What is deliberately *not* attempted is unit conversion. ``unit`` is whatever HelloFresh
    put in the payload, and while it is consistent per ingredient in practice, nothing
    guarantees that a week won't mix ``tbsp`` with ``tablespoon`` or ``cup`` with ``ounce``.
    Converting between them needs a unit table this integration has no business owning, and a
    wrong conversion is worse than no conversion — so distinct units are summed separately
    and joined, leaving the last step to the cook. Non-numeric amounts are likewise kept
    verbatim rather than guessed at.
    """
    # Preserve first-seen unit order so the output is stable across rebuilds.
    totals: dict[str, list[Any]] = {}
    literals: list[str] = []
    for amount, unit in amounts:
        if amount is None and unit is None:
            continue
        number = _coerce_amount(amount)
        if number is None:
            # No usable number: keep whatever was there rather than dropping the line.
            text = " ".join(str(p) for p in (amount, unit) if p not in (None, ""))
            if text and text not in literals:
                literals.append(text)
            continue
        key = _unit_key(unit)
        entry = totals.setdefault(key, [0.0, unit])
        entry[0] += number

    rendered = [
        " ".join(part for part in (_trim_number(total), str(unit or "")) if part).strip()
        for total, unit in totals.values()
    ]
    return " + ".join([*rendered, *literals])


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
        # Ticked-off items, keyed by (week_id, ingredient_key). Keying on the week means a
        # box landing discards only its own check-offs, while the week that was "next" keeps
        # everything already ticked as it becomes the current box.
        self._completed: set[tuple[str, str]] = set()
        self._items: list[TodoItem] = []
        self._built_for: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def todo_items(self) -> list[TodoItem] | None:
        """Return the current prep list."""
        return self._items

    def _target_weeks(self) -> list[HelloFreshWeek]:
        """Return the delivery weeks the list covers: the current box and the one after it.

        "Current" is the next box actually on its way (the subscription's own next-delivery
        handle), which stays the anchor right up until it lands — that is the box you are
        shopping for. "Next" is the following non-skipped delivery, included so a single
        shopping trip can cover both.

        Skipped weeks ship nothing, so they are never targets. Weeks whose delivery date has
        passed are excluded on the same ``delivery_date >= today`` basis the account data uses
        for upcoming orders.
        """
        data = self.coordinator.data
        if data is None:
            return []

        today = date.today()
        anchor = data.next_delivery_week_obj or data.next_configurable_week

        def _eligible(week: HelloFreshWeek | None) -> bool:
            return (
                week is not None
                and not week.is_skipped
                and (week.delivery_date is None or week.delivery_date >= today)
            )

        ordered: list[HelloFreshWeek] = []
        if _eligible(anchor):
            ordered.append(anchor)

        # Everything else that still ships, earliest first. Undated weeks sort last: without a
        # delivery date there is nothing to shop toward.
        remaining = [
            week
            for week in data.weeks
            if _eligible(week) and week.week_id not in {w.week_id for w in ordered}
        ]
        remaining.sort(key=lambda w: (w.delivery_date is None, w.delivery_date or today))
        ordered.extend(remaining)
        return ordered[:_WEEKS_COVERED]

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
        """Refresh the prep list from each covered week's selected meals.

        Recipe detail is fetched per selected meal (the week's own recipes carry ingredient
        *names* only, without amounts or the shipped flag), so this is skipped entirely unless
        one of the covered weeks or its selection actually changed.
        """
        weeks = self._target_weeks()
        if not weeks:
            self._items = []
            self._built_for = ()
            return

        per_week = [(week, self._selected_recipe_ids(week)) for week in weeks]
        fingerprint = tuple((week.week_id, tuple(ids)) for week, ids in per_week)
        if fingerprint == self._built_for:
            return

        # Ticks are keyed by week, so a week dropping off the list (its box landed) discards
        # only its own check-offs; the week that was "next" keeps everything already ticked.
        live_weeks = {week.week_id for week, _ in per_week}
        self._completed = {key for key in self._completed if key[0] in live_weeks}

        servings = None
        subscription = self.coordinator.data.primary_subscription if self.coordinator.data else None
        if subscription is not None:
            servings = subscription.servings

        items: list[TodoItem] = []
        fetched = 0
        for week, recipe_ids in per_week:
            # Ingredients are grouped WITHIN a week, never across weeks: each box has its own
            # deadline, and merging two weeks' butter into one line would lose which shop it
            # belongs to.
            collected: dict[str, tuple[str, list[tuple[Any, Any]]]] = {}
            for recipe_id in recipe_ids:
                if fetched >= _MAX_RECIPE_FETCHES:
                    _LOGGER.debug(
                        "Prep list stopped after %s recipes (week %s)", fetched, week.week_id
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

            if not collected:
                continue

            for key, (display, amounts) in sorted(
                collected.items(), key=lambda kv: kv[1][0].lower()
            ):
                summary = display
                detail_text = _format_amounts(amounts)
                if detail_text:
                    summary = f"{display} — {detail_text}"
                uid = f"{week.week_id}:{key}"
                items.append(
                    TodoItem(
                        uid=uid,
                        summary=summary,
                        status=(
                            TodoItemStatus.COMPLETED
                            if (week.week_id, key) in self._completed
                            else TodoItemStatus.NEEDS_ACTION
                        ),
                        # Each week carries its own deadline: the day that box arrives. HA's
                        # to-do card groups and sorts by due date, which is what separates the
                        # two weeks visually without inventing section headers.
                        due=week.delivery_date,
                        description=_week_label(week),
                    )
                )

        self._items = items
        self._built_for = fingerprint

    async def async_update_todo_item(self, item: TodoItem) -> None:
        """Record a check-off (or un-check) for one ingredient in one week."""
        if item.uid is None:
            return
        week_id, _, key = item.uid.partition(":")
        if not key:
            return
        if item.status == TodoItemStatus.COMPLETED:
            self._completed.add((week_id, key))
        else:
            self._completed.discard((week_id, key))
        for index, existing in enumerate(self._items):
            if existing.uid == item.uid:
                self._items[index] = TodoItem(
                    uid=existing.uid,
                    summary=existing.summary,
                    status=item.status or existing.status,
                    due=existing.due,
                    description=existing.description,
                )
                break
        self.async_write_ha_state()
