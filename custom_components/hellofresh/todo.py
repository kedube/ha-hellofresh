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

Within a week, amounts for the same ingredient are added up, converting between units of the
same measurement family where that is exact — 4 tablespoon plus 3 teaspoon is 5 tablespoon.
Amounts are never summed *across* weeks: each box is its own shopping trip. See
:func:`_format_amounts` for what does and does not get converted.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any, TypeGuard

from homeassistant.components.todo import (
    ENTITY_ID_FORMAT,
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity

from .coordinator import HelloFreshConfigEntry, HelloFreshDataUpdateCoordinator
from .entity import HelloFreshCoordinatorEntity
from .models import HelloFreshWeek
from .parsers import local_today

_LOGGER = logging.getLogger(__name__)

# How many upcoming delivery weeks the list covers: the box on its way, plus the one after it,
# so a single shopping trip can serve both.
_WEEKS_COVERED = 2

# Cap the recipes one list reads. A week holds a handful of selected meals, but a malformed
# payload could nominate far more; this keeps one bad poll from issuing dozens of round-trips.
_MAX_RECIPE_FETCHES = 24

# How many recipe details one list fetches at a time. Enough that a week's meals no longer
# queue up one behind another, few enough to stay a polite client.
_FETCH_CONCURRENCY = 3

# How long a list that could not read every recipe waits before asking for the missing ones
# again. Long enough not to hammer HelloFresh through an outage.
_RETRY_AFTER = timedelta(minutes=10)


# Coordinator-based: entities never poll on their own, so entity-update parallelism is
# irrelevant — declared 0 (unlimited) per the integration quality scale's convention.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HelloFreshConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one prep-list entity per covered delivery week."""
    coordinator = entry.runtime_data
    async_add_entities(HelloFreshPrepListTodo(coordinator, slot) for slot in range(_WEEKS_COVERED))


@dataclass
class _PrepTicksExtraData(ExtraStoredData):
    """Restore-state payload: the ticked-off (box key, ingredient_key) pairs."""

    ticks: list[list[str]]

    def as_dict(self) -> dict[str, Any]:
        return {"ticks": self.ticks}


def _ingredient_key(name: str) -> str:
    """Return a stable identity for an ingredient line.

    Check-off state is keyed on this rather than list position, so re-fetching the week (or
    the user swapping one meal) doesn't shuffle which items look completed. Case- and
    whitespace-insensitive because the same staple is not spelled consistently across recipes.
    """
    return " ".join(name.split()).casefold()


def _box_key(week: HelloFreshWeek) -> str:
    """Return the identity a week's prep list and its check-offs are kept under.

    A week id is shared by every subscription on the account: with two boxes in the same
    week, keying on the id alone let one box hide the other and would have mixed their
    check-offs. A week without a subscription id keeps its bare week id, which is also how
    every tick was saved before boxes were told apart.
    """
    subscription_id = getattr(week, "subscription_id", None)
    return f"{subscription_id}/{week.week_id}" if subscription_id else week.week_id


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


# Unit families for combining amounts. Each maps a canonical unit to its size in the family's
# base unit, smallest first. Only units that convert *exactly* and unambiguously are listed:
# 3 teaspoon = 1 tablespoon is a definition, so "4 tablespoon + 3 teaspoon" is honestly
# "5 tablespoon". Deliberately excluded:
#
#   * Weight <-> volume (grams to cups) — depends on what is being measured.
#   * Metric <-> imperial (ml to teaspoon) — 4.929 ml/tsp, so any total becomes a fraction
#     that is worse to read than the two amounts side by side.
#   * "clove", "piece", "bunch", "can", "pinch" — countable or vague, and not interconvertible.
#
# Anything not listed here still groups by its own name, so an unknown unit is never silently
# folded into the wrong family; it simply reports its own subtotal.
_UNIT_FAMILIES: tuple[dict[str, float], ...] = (
    # Volume, imperial. Base: teaspoon.
    {"teaspoon": 1.0, "tablespoon": 3.0, "fluid ounce": 6.0, "cup": 48.0},
    # Volume, metric. Base: millilitre.
    {"millilitre": 1.0, "centilitre": 10.0, "decilitre": 100.0, "litre": 1000.0},
    # Weight, metric. Base: gram.
    {"gram": 1.0, "kilogram": 1000.0},
    # Weight, imperial. Base: ounce.
    {"ounce": 1.0, "pound": 16.0},
)

# Spellings HelloFresh uses (or plausibly could) for the canonical names above. The payload is
# not consistent — both "tablespoon" and "tbsp" appear — so aliases are resolved before any
# comparison or conversion.
_UNIT_ALIASES: dict[str, str] = {
    "tsp": "teaspoon",
    "teaspoons": "teaspoon",
    "tbsp": "tablespoon",
    "tbs": "tablespoon",
    "tablespoons": "tablespoon",
    "fl oz": "fluid ounce",
    "fluid ounces": "fluid ounce",
    "cups": "cup",
    "ml": "millilitre",
    "milliliter": "millilitre",
    "milliliters": "millilitre",
    "millilitres": "millilitre",
    "cl": "centilitre",
    "dl": "decilitre",
    "l": "litre",
    "liter": "litre",
    "liters": "litre",
    "litres": "litre",
    "g": "gram",
    "grams": "gram",
    "gramme": "gram",
    "grammes": "gram",
    "kg": "kilogram",
    "kilograms": "kilogram",
    "oz": "ounce",
    "ounces": "ounce",
    "lb": "pound",
    "lbs": "pound",
    "pounds": "pound",
}


# Every canonical unit across all families, for recognizing a resolved candidate.
_KNOWN_UNITS: frozenset[str] = frozenset(unit for family in _UNIT_FAMILIES for unit in family)


def _unit_key(unit: Any) -> str:
    """Return a comparison key for a unit.

    Normalizes case, whitespace, a trailing period ("tbsp." -> "tbsp"), and known aliases, so
    "Tablespoon", "tbsp" and "TBSP." are all one unit.

    HelloFresh's real payloads spell units as **name plus a parenthetical abbreviation** —
    ``"tablespoon (tbsp)"``, ``"teaspoon (tsp)"``. The parenthetical is stripped and either
    half is allowed to resolve the alias, so the compound form lands on the same key as the
    bare one. Without this every such unit looked unrecognized and nothing ever combined.
    """
    text = " ".join(str(unit or "").split()).casefold().rstrip(".")
    if text in _UNIT_ALIASES:
        return _UNIT_ALIASES[text]

    # Split "name (abbrev)" and try the name, then the abbreviation.
    outer, _, rest = text.partition("(")
    candidates = [outer.strip().rstrip("."), rest.rstrip(")").strip().rstrip(".")]
    for candidate in candidates:
        if not candidate:
            continue
        resolved = _UNIT_ALIASES.get(candidate, candidate)
        if resolved in _KNOWN_UNITS:
            return resolved
    return text


def _unit_family(unit: str) -> dict[str, float] | None:
    """Return the conversion family ``unit`` belongs to, if any."""
    for family in _UNIT_FAMILIES:
        if unit in family:
            return family
    return None


# Fractions a cook can actually measure. A combined total is only promoted into a larger unit
# when it lands on one of these (within a whisker), so "4 tablespoon + 3 teaspoon" becomes
# "5 tablespoon" but "1 cup + 1 teaspoon" is NOT flattened into an unmeasurable "1.02 cup".
_MEASURABLE_FRACTIONS: tuple[float, ...] = (0.0, 0.25, 1 / 3, 0.5, 2 / 3, 0.75)


def _is_measurable(value: float) -> bool:
    """Return True when ``value`` sits on a fraction a measuring spoon can hit."""
    fraction = value - int(value)
    return any(abs(fraction - candidate) < 0.02 for candidate in _MEASURABLE_FRACTIONS)


def _combine_family(
    totals: dict[str, float], family: dict[str, float], display: dict[str, Any]
) -> list[tuple[float, str]]:
    """Fold one family's subtotals into as few readable amounts as possible.

    The result is expressed in the largest unit **the recipes themselves used**, never in a
    unit that only exists in the conversion table. That is what makes "4 tablespoon +
    3 teaspoon" read as "5 tablespoon": a cook measuring tablespoons wants the answer in
    tablespoons, and promoting to "2.5 fluid ounce" would be equal but useless.

    Combining only happens when the total lands on a measurable fraction. "1 cup + 1 teaspoon"
    is left as two amounts rather than becoming "1.02 cup", which no one can measure and which
    hides the teaspoon entirely.
    """
    base_total = sum(amount * family[unit] for unit, amount in totals.items())
    used = sorted(totals, key=lambda u: family[u], reverse=True)

    # Only ever combine into the largest unit the recipes used. Falling back to a smaller one
    # would turn "1 cup + 1 teaspoon" into "49 teaspoon" — arithmetically right, useless to a
    # cook, and further from the recipe than the two amounts it replaced.
    largest = used[0]
    scaled = base_total / family[largest]
    if scaled >= 1 and _is_measurable(scaled):
        return [(scaled, str(display.get(largest) or largest))]

    # A single unit always reports its own total, even at an awkward fraction: there is
    # nothing to split it into.
    if len(used) == 1:
        return [(totals[largest], str(display.get(largest) or largest))]

    # Nothing combines cleanly: keep each unit's own subtotal, largest first.
    return [(totals[unit], str(display.get(unit) or unit)) for unit in used]


def _format_amounts(amounts: list[tuple[Any, Any]]) -> str:
    """Render the total of one ingredient across a week's recipes.

    Amounts are summed, and units that belong to the same measurement family are converted so
    the total reads the way a cook would say it: 4 tablespoon plus 3 teaspoon is "5 tablespoon",
    not "4 tablespoon + 1 tablespoon". Aliases are resolved first, so "tbsp" and "tablespoon"
    are never treated as different units.

    Conversion is deliberately confined to families where it is exact and unambiguous (see
    :data:`_UNIT_FAMILIES`). Grams do not become cups, and millilitres do not become teaspoons:
    those need to know what is being measured, or produce a fraction less readable than the
    two amounts side by side. Unknown units keep their own subtotal, and non-numeric amounts
    (a range like "1-2") are passed through verbatim rather than guessed at.
    """
    # Sum per normalized unit first, preserving first-seen order for stable output.
    totals: dict[str, float] = {}
    display_units: dict[str, Any] = {}
    literals: list[str] = []
    for amount, unit in amounts:
        if amount is None and unit is None:
            continue
        number = _coerce_amount(amount)
        if number is None:
            if amount in (None, "") and unit not in (None, ""):
                # A unit with no amount means one of it: HelloFresh writes "teaspoon" for a
                # single teaspoon. Treating it as 1 makes it countable, so it both reads as
                # "1 teaspoon" on its own and adds up with the other recipes' teaspoons
                # instead of trailing behind them as a separate bare word.
                number = 1.0
            else:
                # No usable number and nothing to infer: keep whatever was there rather than
                # dropping the line (a range like "1-2" is still information).
                text = " ".join(str(p) for p in (amount, unit) if p not in (None, ""))
                if text and text not in literals:
                    literals.append(text)
                continue
        key = _unit_key(unit)
        totals[key] = totals.get(key, 0.0) + number
        display_units.setdefault(key, unit)

    # Fold each convertible family down to one amount; leave everything else as it is.
    grouped: list[tuple[float, str]] = []
    handled: set[str] = set()
    for key in list(totals):
        if key in handled:
            continue
        family = _unit_family(key)
        if family is None:
            grouped.append((totals[key], str(display_units.get(key) or "")))
            handled.add(key)
            continue
        members = {unit: totals[unit] for unit in totals if unit in family}
        handled.update(members)
        grouped.extend(_combine_family(members, family, display_units))

    rendered = [
        " ".join(part for part in (_trim_number(total), unit) if part).strip()
        for total, unit in grouped
    ]
    return " + ".join([*rendered, *literals])


class HelloFreshPrepListTodo(HelloFreshCoordinatorEntity, TodoListEntity, RestoreEntity):
    """Pantry staples to have on hand before one HelloFresh box arrives.

    One entity per covered delivery week (``slot`` 0 is the box on its way, 1 the one after
    it). Separate entities rather than a single combined list: HA's to-do card renders exactly
    one entity, so two weeks in one entity can only ever be one flat list. Two entities give
    two cards — a real section per week — and keep each week's totals, deadline, and check-offs
    genuinely independent.

    The slot is positional, so as boxes arrive the *same* entity keeps meaning "the next
    delivery" and its history stays coherent; the week it points at moves up.
    """

    _attr_supported_features = TodoListEntityFeature.UPDATE_TODO_ITEM

    def __init__(self, coordinator: HelloFreshDataUpdateCoordinator, slot: int) -> None:
        """Initialize the prep list for one week slot."""
        key = "prep_list" if slot == 0 else f"prep_list_week_{slot + 1}"
        super().__init__(coordinator, ENTITY_ID_FORMAT, key)
        self._slot = slot
        self._attr_translation_key = key
        self._items: list[TodoItem] = []
        # What the current items were built from: (week id, selected recipe ids, servings).
        # Empty until a build has read every recipe, so an incomplete one is tried again.
        self._built_for: tuple[Any, ...] = ()
        self._rebuild_task: asyncio.Task[None] | None = None
        self._rebuild_again = False
        self._cancel_retry: CALLBACK_TYPE | None = None

    @property
    def _completed(self) -> set[tuple[str, str]]:
        """Ticked-off items, keyed by (box key, ingredient_key); see ``_box_key``.

        Keying on the box (not the slot) AND sharing one set across both entities via
        the coordinator means a box landing — which shifts every week up one slot, onto the
        OTHER entity — carries the ticks with the week they belong to. A per-entity set
        stranded them on the old slot.
        """
        return self.coordinator.prep_completed

    @property
    def todo_items(self) -> list[TodoItem] | None:
        """Return this week's prep list."""
        return self._items

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose which delivery this list belongs to.

        The entity name is static ("Prep list" / "Prep list week 2") so it stays stable in the
        UI, but a dashboard heading or automation usually wants the actual date this box
        arrives. Kept as attributes rather than baked into the name.
        """
        week = self._target_week()
        if week is None:
            return {"week_id": None, "subscription_id": None, "delivery_date": None}
        return {
            "week_id": week.week_id,
            "subscription_id": getattr(week, "subscription_id", None),
            "delivery_date": week.delivery_date.isoformat() if week.delivery_date else None,
        }

    def _all_covered_weeks(self) -> list[HelloFreshWeek]:
        """Return the delivery weeks the prep lists cover, current box first.

        Skipped weeks ship nothing, so they are never covered. Weeks whose delivery date has
        passed are excluded on the same ``delivery_date >= today`` basis the account data uses
        for upcoming orders.
        """
        data = self.coordinator.data
        if data is None:
            return []

        today = local_today()
        anchor = data.next_delivery_week_obj or data.next_configurable_week

        # A TypeGuard, not a TypeIs: False also covers a real week that is skipped or past.
        def _eligible(week: HelloFreshWeek | None) -> TypeGuard[HelloFreshWeek]:
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
            if _eligible(week) and _box_key(week) not in {_box_key(w) for w in ordered}
        ]
        remaining.sort(key=lambda w: (w.delivery_date is None, w.delivery_date or today))
        ordered.extend(remaining)
        return ordered[:_WEEKS_COVERED]

    def _target_week(self) -> HelloFreshWeek | None:
        """Return the one delivery week this entity is responsible for."""
        weeks = self._all_covered_weeks()
        if self._slot < len(weeks):
            return weeks[self._slot]
        return None

    def _subscription_for(self, week: HelloFreshWeek) -> Any | None:
        """Return the subscription the week's box belongs to, else the account's first.

        Its serving count sets the amounts. Reading the first subscription's count for every
        week gave a second subscription's box the wrong amounts.
        """
        data = self.coordinator.data
        if data is None:
            return None
        subscription_id = getattr(week, "subscription_id", None)
        for subscription in getattr(data, "subscriptions", None) or []:
            if subscription_id and subscription.subscription_id == subscription_id:
                return subscription
        return data.primary_subscription

    def _adopt_legacy_ticks(self, covered_weeks: list[HelloFreshWeek]) -> None:
        """Move check-offs saved under a bare week id onto that week's box.

        Ticks were keyed by week id alone until boxes were told apart by subscription. A
        restored tick of that kind belongs to the first covered box of its week, which is the
        one it was ticked on; it is moved there before ticks for uncovered boxes are dropped.
        """
        box_for_week: dict[str, str] = {}
        for week in covered_weeks:
            box_for_week.setdefault(week.week_id, _box_key(week))
        for box, key in [entry for entry in self._completed if "/" not in entry[0]]:
            adopted = box_for_week.get(box)
            if adopted is not None and adopted != box:
                self._completed.discard((box, key))
                self._completed.add((adopted, key))

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

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        """Persist the shared tick set so check-offs survive a Home Assistant restart."""
        return _PrepTicksExtraData([list(entry) for entry in sorted(self._completed)])

    async def async_added_to_hass(self) -> None:
        """Restore saved ticks, then start building the list in the background.

        The build fetches one recipe per selected meal. Awaited here it held up the whole
        entry's setup, because Home Assistant waits for every entity to finish being added.
        """
        await super().async_added_to_hass()
        last = await self.async_get_last_extra_data()
        if last is not None:
            for entry in last.as_dict().get("ticks", []):
                if isinstance(entry, list | tuple) and len(entry) == 2:
                    # Union into the SHARED set: both entities persist the same set, so
                    # whichever restores first seeds it and the second is a no-op.
                    self._completed.add((str(entry[0]), str(entry[1])))
        self._schedule_rebuild()

    async def async_will_remove_from_hass(self) -> None:
        """Stop a rebuild still running, or still to come, when the entity goes away."""
        await super().async_will_remove_from_hass()
        self._cancel_pending_retry()
        if self._rebuild_task is not None:
            self._rebuild_task.cancel()
            self._rebuild_task = None

    @callback
    def _handle_coordinator_update(self) -> None:
        """React to a coordinator poll.

        ``CoordinatorEntity`` is a *synchronous* callback that only writes state, and
        ``should_poll`` is False for coordinator entities — so neither hook can await the
        per-recipe detail fetch this list needs. The rebuild is therefore scheduled as a task
        and writes state when it finishes. Without this the list would be built once at
        startup and then never change as meals are swapped or a new week became the anchor.
        """
        self._schedule_rebuild()

    @callback
    def _schedule_rebuild(self) -> None:
        """Rebuild in a task that belongs to the config entry.

        One rebuild runs at a time: a request that arrives while one is running makes it go
        round once more when it finishes, so two never write the list over each other. The
        entry cancels its background tasks when it unloads, so none outlives it either.
        """
        if self._rebuild_task is not None and not self._rebuild_task.done():
            self._rebuild_again = True
            return
        self._rebuild_task = self.coordinator.config_entry.async_create_background_task(
            self.hass,
            self._async_rebuild_and_write(),
            name=f"hellofresh prep list {self._slot + 1}",
        )

    async def _async_rebuild_and_write(self) -> None:
        """Rebuild off the event loop's critical path, then publish."""
        while True:
            self._rebuild_again = False
            complete = True
            try:
                complete = await self._async_rebuild()
            except Exception:  # noqa: BLE001 - a background task must not raise into the loop
                _LOGGER.exception("Prep list refresh failed")
            else:
                self.async_write_ha_state()
            if not self._rebuild_again:
                break
        self._cancel_pending_retry()
        if not complete:
            # Home Assistant updates the entities only when the account data changed, which
            # on a quiet week may not happen for days, so the missing recipes are asked for
            # again on a timer.
            self._cancel_retry = async_call_later(self.hass, _RETRY_AFTER, self._async_retry)

    @callback
    def _async_retry(self, _now: datetime) -> None:
        """Build again for the recipes the last build could not read."""
        self._cancel_retry = None
        self._schedule_rebuild()

    @callback
    def _cancel_pending_retry(self) -> None:
        if self._cancel_retry is not None:
            self._cancel_retry()
            self._cancel_retry = None

    async def _async_recipe_details(
        self, recipe_ids: list[str], servings: int | None
    ) -> list[Any | None]:
        """Return each recipe's detail, in order; None for one that could not be read.

        Details come from the coordinator's cache when a list has read them before (the same
        recipe at the same serving count never changes), so swapping one meal fetches one
        recipe. The rest are fetched a few at a time.
        """
        cache = self.coordinator.prep_recipe_details
        limit = asyncio.Semaphore(_FETCH_CONCURRENCY)

        async def _detail(recipe_id: str) -> Any | None:
            key = (recipe_id, servings)
            if key in cache:
                return cache[key]
            async with limit:
                try:
                    detail = await self.coordinator.client.async_get_recipe_detail(
                        recipe_id, servings=servings, include_favorite=False
                    )
                except Exception as err:  # noqa: BLE001 - one bad recipe must not blank the list
                    _LOGGER.debug("Prep list could not read recipe %s: %s", recipe_id, err)
                    return None
            cache[key] = detail
            return detail

        return list(await asyncio.gather(*(_detail(recipe_id) for recipe_id in recipe_ids)))

    async def _async_rebuild(self) -> bool:
        """Refresh this week's prep list from its selected meals.

        Recipe detail is fetched per selected meal (the week's own recipes carry ingredient
        *names* only, without amounts or the shipped flag), so this is skipped entirely unless
        the week, its selection or the serving count actually changed.

        Returns False when a recipe could not be read, so the list is missing its staples.
        """
        week = self._target_week()
        if week is None:
            self._items = []
            self._built_for = ()
            return True

        servings = None
        subscription = self._subscription_for(week)
        if subscription is not None:
            servings = subscription.servings

        recipe_ids = self._selected_recipe_ids(week)
        fingerprint = (_box_key(week), tuple(recipe_ids), servings)
        if fingerprint == self._built_for:
            return True

        # Ticks belong to a week, not a slot. Dropping weeks NEITHER entity still covers
        # keeps the shared set from growing without bound, while a week shifting from
        # slot 1 to slot 0 (its predecessor landed) arrives with its check-offs intact —
        # the shared set is exactly what makes that survive the entity handover.
        covered_weeks = self._all_covered_weeks()
        self._adopt_legacy_ticks(covered_weeks)
        covered_ids = {_box_key(w) for w in covered_weeks}
        self._completed.intersection_update(
            {entry for entry in self._completed if entry[0] in covered_ids}
        )
        # The detail cache is pruned the same way: only recipes a covered week still selects,
        # at the current serving count, are worth keeping.
        wanted = {rid for w in covered_weeks for rid in self._selected_recipe_ids(w)}
        cache = self.coordinator.prep_recipe_details
        for cache_key in [k for k in cache if k[0] not in wanted or k[1] != servings]:
            del cache[cache_key]

        if len(recipe_ids) > _MAX_RECIPE_FETCHES:
            _LOGGER.debug(
                "Prep list reads only the first %s of %s recipes (week %s)",
                _MAX_RECIPE_FETCHES,
                len(recipe_ids),
                week.week_id,
            )
        details = await self._async_recipe_details(recipe_ids[:_MAX_RECIPE_FETCHES], servings)

        # name -> (display name, [(amount, unit), ...])
        collected: dict[str, tuple[str, list[tuple[Any, Any]]]] = {}
        for detail in details:
            if detail is None:
                continue
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
                _display, amounts = collected.setdefault(key, (name.strip(), []))
                amounts.append((ingredient.get("amount"), ingredient.get("unit")))

        items: list[TodoItem] = []
        for key, (display, amounts) in sorted(collected.items(), key=lambda kv: kv[1][0].lower()):
            summary = display
            detail_text = _format_amounts(amounts)
            if detail_text:
                summary = f"{display} — {detail_text}"
            items.append(
                TodoItem(
                    uid=f"{_box_key(week)}:{key}",
                    summary=summary,
                    status=(
                        TodoItemStatus.COMPLETED
                        if (_box_key(week), key) in self._completed
                        else TodoItemStatus.NEEDS_ACTION
                    ),
                    # The deadline for having these on hand is the day that box arrives.
                    due=week.delivery_date,
                )
            )

        self._items = items
        # Only a build that read every recipe is final. With one missing (a timeout, a 503),
        # the list shows what it has and the next build tries again: the recipes already read
        # come from the cache, so the retry costs only the missing ones. Storing the
        # fingerprint anyway left that recipe's staples off the list until the selection
        # changed or Home Assistant restarted.
        complete = all(detail is not None for detail in details)
        self._built_for = fingerprint if complete else ()
        return complete

    async def async_update_todo_item(self, item: TodoItem) -> None:
        """Record a check-off (or un-check) for one ingredient in one week."""
        if item.uid is None:
            return
        # A box key never contains a colon (a subscription id, a slash and a week id), so the
        # first colon ends it, whatever the ingredient's own name holds.
        box, _, key = item.uid.partition(":")
        if not key:
            return
        if item.status == TodoItemStatus.COMPLETED:
            self._completed.add((box, key))
        else:
            self._completed.discard((box, key))
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
