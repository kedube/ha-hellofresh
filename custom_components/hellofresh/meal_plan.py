"""The Week tab's meal plan: which day each meal in a box (and each Market item) gets eaten.

A plan is a set of dated meal slots — breakfast, lunch, dinner — each holding a main course and,
for dinner, an optional appetizer and dessert. A course is one of the week's selected meals, a
Market item bought that week, or a free-text note ("Pizza night"). It lives in Home Assistant's
storage, one file per account, keyed by **date** rather than by delivery week, so moving a
delivery to another day never loses a plan.

Each planned course keeps a snapshot of what it was (name, image, cooking time) when it was
planned. The plan therefore survives the menu changing underneath it — a meal swapped out on
the Menu still shows what was planned, and the card flags it as no longer in the box. The
snapshot is also all the calendar and sensors need, so they never wait on a poll.

Each meal in a box can be planned as many times as it was ordered (a double portion twice), and
each Market item as many times as it was bought. The plan never writes to HelloFresh.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ALWAYS_SHOW_BREAKFAST,
    CONF_ALWAYS_SHOW_LUNCH,
    CONF_BREAKFAST_MINUTES,
    CONF_BREAKFAST_START,
    CONF_DINNER_EXTRA_MINUTES,
    CONF_DINNER_MINUTES,
    CONF_DINNER_START,
    CONF_DINNER_USE_COOKING_TIME,
    CONF_HISTORY_WEEKS,
    CONF_LUNCH_MINUTES,
    CONF_LUNCH_START,
    DEFAULT_BREAKFAST_MINUTES,
    DEFAULT_BREAKFAST_START,
    DEFAULT_DINNER_EXTRA_MINUTES,
    DEFAULT_DINNER_MINUTES,
    DEFAULT_DINNER_START,
    DEFAULT_HISTORY_WEEKS,
    DEFAULT_LUNCH_MINUTES,
    DEFAULT_LUNCH_START,
    DOMAIN,
)

if TYPE_CHECKING:
    from .models import HelloFreshMarketItem, HelloFreshRecipe, HelloFreshWeek

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1

MEALS = ("breakfast", "lunch", "dinner")
COURSES = ("main", "appetizer", "dessert")
# Only dinner has more than one course.
DINNER_ONLY_COURSES = frozenset({"appetizer", "dessert"})
# Market groups that are not food for the household: pet food and charity donations.
EXCLUDED_MARKET_GROUPS = frozenset({"petstable", "donation"})

# The entities the plan adds, by unique-id key: the Meals calendar and one sensor per meal.
MEAL_CALENDAR_KEY = "meals"
MEAL_SENSOR_KEYS = {
    "breakfast": "todays_breakfast",
    "lunch": "todays_lunch",
    "dinner": "tonights_dinner",
}
MEAL_ENTITY_KEYS = (MEAL_CALENDAR_KEY, *MEAL_SENSOR_KEYS.values())

KIND_MEAL = "meal"
KIND_MARKET = "market"
KIND_NOTE = "note"

# A free-text note is a short label for a day, not a recipe.
MAX_NOTE_LENGTH = 80

_SAVE_DELAY_SECONDS = 1


# The calendar's wording, read from the card's own words (strings.json config_panel.card) in
# Home Assistant's language, so events read "Abendessen: …" for a German household.
_LABEL_KEYS = {
    "breakfast": "weekplan.meal.breakfast",
    "lunch": "weekplan.meal.lunch",
    "dinner": "weekplan.meal.dinner",
    "appetizer": "weekplan.course.appetizer",
    "dessert": "weekplan.course.dessert",
    "event": "weekplan.calendar.event",
    "course": "weekplan.calendar.course",
    "cooking": "weekplan.calendar.cooking",
}
_ENGLISH_LABELS = {
    "breakfast": "Breakfast",
    "lunch": "Lunch",
    "dinner": "Dinner",
    "appetizer": "Appetizer",
    "dessert": "Dessert",
    "event": "{meal}: {name}",
    "course": "{course}: {name}",
    "cooking": "Cooking time: {minutes} min",
}


async def async_meal_labels(hass: HomeAssistant) -> dict[str, str]:
    """The calendar's wording in Home Assistant's language, English where a word is missing."""
    prefix = f"component.{DOMAIN}.config_panel.card."
    try:
        strings = await async_get_translations(hass, hass.config.language, "config_panel", [DOMAIN])
    except Exception as err:  # noqa: BLE001 - English wording is a fine fallback
        _LOGGER.debug("Meal calendar wording unavailable: %s", err)
        strings = {}
    labels: dict[str, str] = {}
    for name, key in _LABEL_KEYS.items():
        value = strings.get(prefix + key)
        labels[name] = value if isinstance(value, str) and value else _ENGLISH_LABELS[name]
    return labels


def storage_key(entry_id: str) -> str:
    """The storage file for one account's plan."""
    return f"{DOMAIN}.meal_plan.{entry_id}"


def _parse_time(value: object, fallback: str) -> time:
    """An option's "HH:MM[:SS]" as a time; the fallback when it is missing or malformed."""
    for candidate in (value, fallback):
        if isinstance(candidate, str):
            try:
                return time.fromisoformat(candidate)
            except ValueError:
                continue
    return time(0, 0)


def _hhmm(value: time) -> str:
    return value.strftime("%H:%M")


@dataclass(frozen=True, slots=True)
class MealTiming:
    """When one meal's calendar event starts and, by default, how long it lasts."""

    start: time
    minutes: int


@dataclass(frozen=True, slots=True)
class MealPlanSettings:
    """The Week tab's options, read once per entry setup (an options change reloads)."""

    breakfast: MealTiming
    lunch: MealTiming
    dinner: MealTiming
    always_show_breakfast: bool = False
    always_show_lunch: bool = False
    dinner_use_cooking_time: bool = False
    dinner_extra_minutes: int = DEFAULT_DINNER_EXTRA_MINUTES
    history_weeks: int = DEFAULT_HISTORY_WEEKS

    @classmethod
    def from_options(cls, options: dict[str, Any]) -> MealPlanSettings:
        def timing(start_key: str, start_default: str, minutes_key: str, minutes_default: int):
            return MealTiming(
                start=_parse_time(options.get(start_key), start_default),
                minutes=int(options.get(minutes_key, minutes_default)),
            )

        return cls(
            breakfast=timing(
                CONF_BREAKFAST_START,
                DEFAULT_BREAKFAST_START,
                CONF_BREAKFAST_MINUTES,
                DEFAULT_BREAKFAST_MINUTES,
            ),
            lunch=timing(
                CONF_LUNCH_START, DEFAULT_LUNCH_START, CONF_LUNCH_MINUTES, DEFAULT_LUNCH_MINUTES
            ),
            dinner=timing(
                CONF_DINNER_START,
                DEFAULT_DINNER_START,
                CONF_DINNER_MINUTES,
                DEFAULT_DINNER_MINUTES,
            ),
            always_show_breakfast=bool(options.get(CONF_ALWAYS_SHOW_BREAKFAST, False)),
            always_show_lunch=bool(options.get(CONF_ALWAYS_SHOW_LUNCH, False)),
            dinner_use_cooking_time=bool(options.get(CONF_DINNER_USE_COOKING_TIME, False)),
            dinner_extra_minutes=int(
                options.get(CONF_DINNER_EXTRA_MINUTES, DEFAULT_DINNER_EXTRA_MINUTES)
            ),
            history_weeks=int(options.get(CONF_HISTORY_WEEKS, DEFAULT_HISTORY_WEEKS)),
        )

    def timing(self, meal: str) -> MealTiming:
        return getattr(self, meal)

    def as_dict(self) -> dict[str, Any]:
        return {
            "always_show": {
                "breakfast": self.always_show_breakfast,
                "lunch": self.always_show_lunch,
            },
            "meals": {
                "breakfast": {
                    "start": _hhmm(self.breakfast.start),
                    "minutes": self.breakfast.minutes,
                },
                "lunch": {"start": _hhmm(self.lunch.start), "minutes": self.lunch.minutes},
                "dinner": {
                    "start": _hhmm(self.dinner.start),
                    "minutes": self.dinner.minutes,
                    "use_cooking_time": self.dinner_use_cooking_time,
                    "extra_minutes": self.dinner_extra_minutes,
                },
            },
        }


# ---- pure helpers ---------------------------------------------------------------------------


def headline_minutes(item: object) -> int | None:
    """The cooking time HelloFresh shows on a meal's tile.

    The menu payload swaps its time names: ``prep_time_minutes`` carries the headline time the
    website displays, ``total_time_minutes`` the smaller hands-on figure (see normalizers.py).
    """
    for attr in ("prep_time_minutes", "total_time_minutes"):
        value = getattr(item, attr, None)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return None


def recipe_quantity(recipe: HelloFreshRecipe) -> int:
    """How many times a selected meal was ordered (0 when it is not in the box)."""
    if not recipe.is_selected:
        return 0
    quantity = recipe.selected_quantity
    return quantity if isinstance(quantity, int) and quantity > 0 else 1


def market_quantity(item: HelloFreshMarketItem) -> int:
    """How many of a Market item were bought (0 when none were)."""
    quantity = item.selected_quantity
    if isinstance(quantity, int) and quantity > 0:
        return quantity
    return 1 if item.is_selected else 0


def week_window(week: HelloFreshWeek) -> tuple[date, date] | None:
    """The seven days a week's box covers, starting on its delivery day."""
    if week.delivery_date is None:
        return None
    return week.delivery_date, week.delivery_date + timedelta(days=6)


def _candidate_weeks(weeks: Iterable[HelloFreshWeek], day: date) -> list[HelloFreshWeek]:
    """Weeks an item planned for ``day`` most plausibly came from, best first.

    The week whose seven days cover ``day``, then earlier boxes (newest first) — food is eaten
    after it arrives — then later ones.
    """
    dated = [week for week in weeks if week.delivery_date is not None]

    def rank(week: HelloFreshWeek) -> tuple[int, int]:
        start, end = week_window(week)  # type: ignore[misc]
        if start <= day <= end:
            return (0, 0)
        if start < day:
            return (1, (day - start).days)
        return (2, (start - day).days)

    return sorted(dated, key=rank)


def _note_snapshot(text: str) -> dict[str, Any]:
    clean = " ".join(text.split())
    if not clean:
        raise HomeAssistantError("Write something for the note, such as Leftovers.")
    return {"kind": KIND_NOTE, "name": clean[:MAX_NOTE_LENGTH]}


def _same_item(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return (
        a.get("kind") == b.get("kind")
        and a.get("kind") != KIND_NOTE
        and a.get("id") == b.get("id")
        and a.get("week_id") == b.get("week_id")
    )


def iter_courses(days: dict[str, Any]) -> Iterable[tuple[str, str, str, dict[str, Any]]]:
    """Every planned course as (date, meal, course, item)."""
    for day, meals in days.items():
        if not isinstance(meals, dict):
            continue
        for meal, slot in meals.items():
            if meal not in MEALS or not isinstance(slot, dict):
                continue
            for course in COURSES:
                item = slot.get(course)
                if isinstance(item, dict):
                    yield day, meal, course, item


def planned_count(
    days: dict[str, Any], item: dict[str, Any], *, skip: tuple[str, str, str] | None = None
) -> int:
    """How many courses already use ``item`` (the same meal or Market item from the same box)."""
    return sum(
        1
        for day, meal, course, planned in iter_courses(days)
        if (day, meal, course) != skip and _same_item(planned, item)
    )


def slot_title(slot: dict[str, Any]) -> str | None:
    """What a meal slot is called: its main, else whatever courses it has."""
    main = slot.get("main")
    if isinstance(main, dict) and main.get("name"):
        return str(main["name"])
    names = [
        str(slot[course]["name"])
        for course in COURSES
        if isinstance(slot.get(course), dict) and slot[course].get("name")
    ]
    return ", ".join(names) or None


def slot_minutes(settings: MealPlanSettings, meal: str, slot: dict[str, Any]) -> int:
    """How long a meal's calendar event lasts.

    Dinner can follow the recipe: the main's HelloFresh cooking time, plus the appetizer's when
    it has one (made one after the other), plus the extra time for eating and clean-up. Without
    a cooking time — a note, or a Market item HelloFresh gives no time for — and for breakfast
    and lunch, the meal's fixed length applies.
    """
    timing = settings.timing(meal)
    if meal != "dinner" or not settings.dinner_use_cooking_time:
        return timing.minutes
    main = slot.get("main")
    cooking = main.get("minutes") if isinstance(main, dict) else None
    if not isinstance(cooking, int) or cooking <= 0:
        return timing.minutes
    appetizer = slot.get("appetizer")
    extra = appetizer.get("minutes") if isinstance(appetizer, dict) else None
    if isinstance(extra, int) and extra > 0:
        cooking += extra
    return cooking + settings.dinner_extra_minutes


def slot_window(
    settings: MealPlanSettings, day: date, meal: str, slot: dict[str, Any]
) -> tuple[datetime, datetime]:
    """A meal's start and end, in Home Assistant's time zone."""
    start_time = settings.timing(meal).start
    override = slot.get("start")
    if isinstance(override, str):
        start_time = _parse_time(override, _hhmm(start_time))
    start = datetime.combine(day, start_time, tzinfo=dt_util.get_default_time_zone())
    return start, start + timedelta(minutes=slot_minutes(settings, meal, slot))


# ---- the plan -------------------------------------------------------------------------------


class MealPlan:
    """One account's meal plan: stored days, the options that time them, and change listeners."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator: Any) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.settings = MealPlanSettings.from_options(dict(entry.options))
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, storage_key(entry.entry_id)
        )
        self.days: dict[str, dict[str, Any]] = {}
        # When the plan last changed: the Meals calendar exposes it so an open card can notice a
        # change made elsewhere (another device, an automation) and re-read the plan.
        self.updated: datetime = dt_util.utcnow()
        self._listeners: list[CALLBACK_TYPE] = []
        # Market items' cooking times, looked up once per recipe from its detail.
        self._minutes_cache: dict[str, int | None] = {}

    async def async_load(self) -> None:
        stored = await self._store.async_load()
        days = stored.get("days") if isinstance(stored, dict) else None
        self.days = days if isinstance(days, dict) else {}
        if self._prune():
            self._schedule_save()

    @callback
    def async_add_listener(self, update: CALLBACK_TYPE) -> Callable[[], None]:
        """Call ``update`` whenever the plan changes; returns the function that stops it."""
        self._listeners.append(update)

        def remove() -> None:
            if update in self._listeners:
                self._listeners.remove(update)

        return remove

    def _schedule_save(self) -> None:
        self._store.async_delay_save(lambda: {"days": self.days}, _SAVE_DELAY_SECONDS)

    def _changed(self) -> None:
        self.updated = dt_util.utcnow()
        self._prune()
        self._schedule_save()
        for update in list(self._listeners):
            update()

    def _prune(self) -> bool:
        """Drop days older than the history window, as the rest of the integration forgets."""
        cutoff = (dt_util.now().date() - timedelta(weeks=self.settings.history_weeks)).isoformat()
        stale = [day for day in self.days if day < cutoff]
        for day in stale:
            del self.days[day]
        return bool(stale)

    async def async_flush(self) -> None:
        """Write any pending change now (the entry is unloading)."""
        await self._store.async_save({"days": self.days})

    # ---- reading ----------------------------------------------------------------------------

    def slot(self, day: date | str, meal: str) -> dict[str, Any] | None:
        key = day.isoformat() if isinstance(day, date) else day
        meals = self.days.get(key)
        slot = meals.get(meal) if isinstance(meals, dict) else None
        return slot if isinstance(slot, dict) and slot_title(slot) else None

    def window(self, day: date, meal: str, slot: dict[str, Any]) -> tuple[datetime, datetime]:
        return slot_window(self.settings, day, meal, slot)

    def as_dict(self) -> dict[str, Any]:
        """The plan for the card and the get_meal_plan service: settings, and every day with
        each meal's computed start and end so no consumer re-implements the timing rules."""
        days: dict[str, Any] = {}
        for key in sorted(self.days):
            try:
                day = date.fromisoformat(key)
            except ValueError:
                continue
            meals: dict[str, Any] = {}
            for meal in MEALS:
                slot = self.slot(key, meal)
                if slot is None:
                    continue
                start, end = self.window(day, meal, slot)
                meals[meal] = {
                    **{course: slot[course] for course in COURSES if course in slot},
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "custom_start": isinstance(slot.get("start"), str),
                }
            if meals:
                days[key] = meals
        return {"enabled": True, **self.settings.as_dict(), "days": days}

    # ---- writing ----------------------------------------------------------------------------

    def _weeks(self) -> list[HelloFreshWeek]:
        data = getattr(self.coordinator, "data", None)
        return list(getattr(data, "weeks", None) or [])

    def _resolve_recipe(
        self, recipe_id: str, day: date, week_id: str | None
    ) -> tuple[HelloFreshWeek, HelloFreshRecipe, int]:
        weeks = self._weeks()
        if week_id is not None:
            weeks = [week for week in weeks if week.week_id == week_id]
        for week in _candidate_weeks(weeks, day):
            for recipe in week.recipes:
                if recipe.recipe_id == recipe_id and recipe_quantity(recipe) > 0:
                    return week, recipe, recipe_quantity(recipe)
        raise HomeAssistantError(
            f"Meal {recipe_id} isn't in any of your boxes, so it can't be planned."
        )

    def _resolve_market_item(
        self, item_id: str, day: date, week_id: str | None
    ) -> tuple[HelloFreshWeek, HelloFreshMarketItem, int]:
        weeks = self._weeks()
        if week_id is not None:
            weeks = [week for week in weeks if week.week_id == week_id]
        for week in _candidate_weeks(weeks, day):
            for item in week.market_items:
                if item.item_id != item_id or market_quantity(item) <= 0:
                    continue
                if (item.group_type or "") in EXCLUDED_MARKET_GROUPS:
                    raise HomeAssistantError(f"{item.name} isn't something to plan a meal around.")
                return week, item, market_quantity(item)
        raise HomeAssistantError(
            f"Market item {item_id} wasn't bought in any of your boxes, so it can't be planned."
        )

    async def _market_minutes(self, item: HelloFreshMarketItem) -> int | None:
        """A Market item's cooking time, from its recipe detail (looked up once, best effort).

        Market items carry no time in the menu payload; many are recipes HelloFresh times like
        any meal, while a plain protein add-on has none — the meal's fixed length then applies.
        """
        recipe_id = item.recipe_id
        if not recipe_id:
            return None
        if recipe_id not in self._minutes_cache:
            minutes = None
            client = getattr(self.coordinator, "client", None)
            if client is not None:
                try:
                    detail = await client.async_get_recipe_detail(recipe_id)
                    minutes = headline_minutes(detail)
                except Exception as err:  # noqa: BLE001 - a missing time only shortens the event
                    _LOGGER.debug("No cooking time for Market item %s: %s", recipe_id, err)
            self._minutes_cache[recipe_id] = minutes
        return self._minutes_cache[recipe_id]

    async def async_set(
        self,
        day: date,
        meal: str = "dinner",
        course: str = "main",
        *,
        recipe_id: str | None = None,
        market_item_id: str | None = None,
        text: str | None = None,
        week_id: str | None = None,
        start_time: time | None = None,
    ) -> None:
        """Plan one course of one meal, and/or move that meal's start time for the day."""
        if meal not in MEALS:
            raise HomeAssistantError(f"Unknown meal {meal}; use breakfast, lunch or dinner.")
        if course not in COURSES:
            raise HomeAssistantError(f"Unknown course {course}; use main, appetizer or dessert.")
        if course in DINNER_ONLY_COURSES and meal != "dinner":
            raise HomeAssistantError("Only dinner has an appetizer and a dessert.")
        given = [value for value in (recipe_id, market_item_id, text) if value is not None]
        if len(given) > 1:
            raise HomeAssistantError("Give one of recipe_id, market_item_id or text, not several.")

        key = day.isoformat()
        item: dict[str, Any] | None = None
        quantity = 0
        if recipe_id is not None:
            week, recipe, quantity = self._resolve_recipe(recipe_id, day, week_id)
            item = {
                "kind": KIND_MEAL,
                "id": recipe.recipe_id,
                "week_id": week.week_id,
                "name": recipe.name,
                "image_url": recipe.image_url,
                "minutes": headline_minutes(recipe),
            }
        elif market_item_id is not None:
            week, market_item, quantity = self._resolve_market_item(market_item_id, day, week_id)
            item = {
                "kind": KIND_MARKET,
                "id": market_item.item_id,
                "week_id": week.week_id,
                "name": market_item.name,
                "image_url": market_item.image_url,
                "minutes": await self._market_minutes(market_item),
                "group": market_item.group_type,
            }
        elif text is not None:
            item = _note_snapshot(text)

        existing = self.slot(key, meal)
        if item is None and start_time is None:
            raise HomeAssistantError("Give a meal, a Market item, a note or a start time.")
        if item is None and existing is None:
            raise HomeAssistantError("Plan something for this meal before changing its time.")

        if item is not None and item["kind"] != KIND_NOTE:
            used = planned_count(self.days, item, skip=(key, meal, course))
            if used >= quantity:
                raise HomeAssistantError(
                    f"{item['name']} is already planned {used} time(s), as many as your box has."
                )

        slot = dict(self.days.get(key, {}).get(meal) or {})
        if item is not None:
            slot[course] = item
        if start_time is not None:
            if start_time.replace(second=0, microsecond=0) == self.settings.timing(meal).start:
                slot.pop("start", None)
            else:
                slot["start"] = _hhmm(start_time)
        self.days.setdefault(key, {})[meal] = slot
        self._changed()

    def clear(self, day: date, meal: str = "dinner", course: str | None = None) -> None:
        """Clear one course, or the whole meal when no course is given."""
        if meal not in MEALS:
            raise HomeAssistantError(f"Unknown meal {meal}; use breakfast, lunch or dinner.")
        key = day.isoformat()
        meals = self.days.get(key)
        if not isinstance(meals, dict) or meal not in meals:
            return
        if course is None:
            del meals[meal]
        else:
            slot = meals[meal]
            slot.pop(course, None)
            if not slot_title(slot):
                del meals[meal]
        if not meals:
            del self.days[key]
        self._changed()


async def async_remove_meal_plan(hass: HomeAssistant, entry_id: str) -> None:
    """Delete an account's stored plan (the integration entry was removed)."""
    await Store(hass, STORAGE_VERSION, storage_key(entry_id)).async_remove()
