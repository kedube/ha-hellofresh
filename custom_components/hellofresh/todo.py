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

from dataclasses import dataclass
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
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity

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


# Coordinator-based: entities never poll on their own, so entity-update parallelism is
# irrelevant — declared 0 (unlimited) per the integration quality scale's convention.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one prep-list entity per covered delivery week."""
    coordinator: HelloFreshDataUpdateCoordinator = entry.runtime_data
    async_add_entities(HelloFreshPrepListTodo(coordinator, slot) for slot in range(_WEEKS_COVERED))


@dataclass
class _PrepTicksExtraData(ExtraStoredData):
    """Restore-state payload: the ticked-off (week_id, ingredient_key) pairs."""

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
        super().__init__(coordinator)
        self._slot = slot
        key = "prep_list" if slot == 0 else f"prep_list_week_{slot + 1}"
        self._attr_translation_key = key
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{key}"
        self._pin_entity_id(ENTITY_ID_FORMAT, key)
        self._items: list[TodoItem] = []
        self._built_for: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def _completed(self) -> set[tuple[str, str]]:
        """Ticked-off items, keyed by (week_id, ingredient_key).

        Keying on the week id (not the slot) AND sharing one set across both entities via
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
            return {"week_id": None, "delivery_date": None}
        return {
            "week_id": week.week_id,
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

    def _target_week(self) -> HelloFreshWeek | None:
        """Return the one delivery week this entity is responsible for."""
        weeks = self._all_covered_weeks()
        if self._slot < len(weeks):
            return weeks[self._slot]
        return None

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
        """Restore saved ticks, then build the list once the entity is live."""
        await super().async_added_to_hass()
        last = await self.async_get_last_extra_data()
        if last is not None:
            for entry in last.as_dict().get("ticks", []):
                if isinstance(entry, list | tuple) and len(entry) == 2:
                    # Union into the SHARED set: both entities persist the same set, so
                    # whichever restores first seeds it and the second is a no-op.
                    self._completed.add((str(entry[0]), str(entry[1])))
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
        """Refresh this week's prep list from its selected meals.

        Recipe detail is fetched per selected meal (the week's own recipes carry ingredient
        *names* only, without amounts or the shipped flag), so this is skipped entirely unless
        the week or its selection actually changed.
        """
        week = self._target_week()
        if week is None:
            self._items = []
            self._built_for = ()
            return

        recipe_ids = self._selected_recipe_ids(week)
        fingerprint = ((week.week_id, tuple(recipe_ids)),)
        if fingerprint == self._built_for:
            return

        # Ticks belong to a week, not a slot. Dropping weeks NEITHER entity still covers
        # keeps the shared set from growing without bound, while a week shifting from
        # slot 1 to slot 0 (its predecessor landed) arrives with its check-offs intact —
        # the shared set is exactly what makes that survive the entity handover.
        covered_ids = {w.week_id for w in self._all_covered_weeks()}
        self._completed.intersection_update(
            {entry for entry in self._completed if entry[0] in covered_ids}
        )

        servings = None
        subscription = self.coordinator.data.primary_subscription if self.coordinator.data else None
        if subscription is not None:
            servings = subscription.servings

        # name -> (display name, [(amount, unit), ...])
        collected: dict[str, tuple[str, list[tuple[Any, Any]]]] = {}
        fetched = 0
        for recipe_id in recipe_ids:
            if fetched >= _MAX_RECIPE_FETCHES:
                _LOGGER.debug("Prep list stopped after %s recipes (week %s)", fetched, week.week_id)
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
                    uid=f"{week.week_id}:{key}",
                    summary=summary,
                    status=(
                        TodoItemStatus.COMPLETED
                        if (week.week_id, key) in self._completed
                        else TodoItemStatus.NEEDS_ACTION
                    ),
                    # The deadline for having these on hand is the day that box arrives.
                    due=week.delivery_date,
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
