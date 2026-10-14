"""The Week tab's meal plan: storage, the rules for what can be planned, and its timing.

The plan is a set of dated meal slots. These tests pin what the card and the services rely
on: a meal can be planned only as often as it was ordered, Market pet food and donations
never can, each meal's start and end follow the options (and dinner can follow the recipe),
and the plan survives a reload and forgets days past the history window.
"""

from __future__ import annotations

from datetime import date, time, timedelta
from types import SimpleNamespace

from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
import pytest

from custom_components.hellofresh.calendar import HelloFreshMealsCalendar
from custom_components.hellofresh.const import (
    CONF_DINNER_START,
    CONF_DINNER_USE_COOKING_TIME,
    CONF_HISTORY_WEEKS,
)
from custom_components.hellofresh.meal_plan import (
    MealPlan,
    MealPlanSettings,
    async_meal_labels,
    async_remove_meal_plan,
    slot_title,
)
from custom_components.hellofresh.models import (
    HelloFreshMarketItem,
    HelloFreshRecipe,
    HelloFreshWeek,
)
from custom_components.hellofresh.sensor import HelloFreshMealSensor

TODAY = date(2026, 10, 14)  # a Wednesday; the box arrived on Monday the 12th


def _week(delivery: date, week_id: str = "2026-W42") -> HelloFreshWeek:
    return HelloFreshWeek(
        week_id=week_id,
        display_name="Classic Box",
        delivery_date=delivery,
        recipes=[
            HelloFreshRecipe(
                recipe_id="shrimp",
                name="Butter Shrimp",
                is_selected=True,
                selected_quantity=1,
                image_url="https://img/shrimp.jpg",
                prep_time_minutes=30,
                total_time_minutes=10,
            ),
            HelloFreshRecipe(
                recipe_id="couscous", name="Chicken Couscous", is_selected=True, selected_quantity=2
            ),
            HelloFreshRecipe(recipe_id="unchosen", name="Not In The Box"),
        ],
        market_items=[
            HelloFreshMarketItem(
                item_id="m2",
                recipe_id="gyoza-recipe",
                name="Gyoza",
                group_type="appetizer",
                is_selected=True,
                selected_quantity=1,
            ),
            HelloFreshMarketItem(
                item_id="m5", name="Lava Cakes", group_type="dessert", selected_quantity=2
            ),
            HelloFreshMarketItem(
                item_id="pet", name="Dog Treats", group_type="petstable", selected_quantity=1
            ),
            HelloFreshMarketItem(item_id="m9", name="Unbought", group_type="protein"),
        ],
    )


class _Client:
    """Recipe detail lookups for Market items: a time for the gyoza, nothing else."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def async_get_recipe_detail(self, recipe_id: str):
        self.calls.append(recipe_id)
        return SimpleNamespace(prep_time_minutes=15, total_time_minutes=None)


def _plan(hass, options: dict | None = None, weeks: list | None = None) -> MealPlan:
    entry = SimpleNamespace(entry_id="entry1", options=options or {})
    coordinator = SimpleNamespace(
        data=SimpleNamespace(weeks=weeks if weeks is not None else [_week(date(2026, 10, 12))]),
        client=_Client(),
        config_entry=SimpleNamespace(entry_id="entry1", title="HelloFresh"),
    )
    return MealPlan(hass, entry, coordinator)


@pytest.fixture(autouse=True)
async def _today(monkeypatch: pytest.MonkeyPatch, hass) -> None:
    """Pin "today" (pruning, sensors) to a fixed Wednesday afternoon in New York."""
    await hass.config.async_set_time_zone("America/New_York")
    now = dt_util.as_utc(
        dt_util.as_local(dt_util.utcnow()).replace(
            year=TODAY.year, month=TODAY.month, day=TODAY.day, hour=15, minute=0
        )
    )
    monkeypatch.setattr(dt_util, "now", lambda tz=None: dt_util.as_local(now))


# ---- settings -----------------------------------------------------------------------------------


def test_settings_default_to_the_agreed_times() -> None:
    settings = MealPlanSettings.from_options({}).as_dict()
    assert settings["meals"] == {
        "breakfast": {"start": "07:30", "minutes": 30},
        "lunch": {"start": "12:00", "minutes": 45},
        "dinner": {"start": "17:30", "minutes": 90, "use_cooking_time": False, "extra_minutes": 45},
    }
    assert settings["always_show"] == {"breakfast": False, "lunch": False}


def test_a_malformed_start_option_falls_back_to_the_default() -> None:
    settings = MealPlanSettings.from_options({CONF_DINNER_START: "teatime"})
    assert settings.dinner.start == time(17, 30)


# ---- planning -----------------------------------------------------------------------------------


async def test_a_planned_meal_is_snapshotted_and_timed(hass) -> None:
    plan = _plan(hass)
    await plan.async_set(TODAY, recipe_id="shrimp")

    day = plan.as_dict()["days"]["2026-10-14"]["dinner"]
    assert day["main"] == {
        "kind": "meal",
        "id": "shrimp",
        "week_id": "2026-W42",
        "name": "Butter Shrimp",
        "image_url": "https://img/shrimp.jpg",
        # The headline time the tile shows (the payload's swapped prep/total naming).
        "minutes": 30,
    }
    assert day["start"] == "2026-10-14T17:30:00-04:00"
    assert day["end"] == "2026-10-14T19:00:00-04:00"
    assert day["custom_start"] is False


async def test_a_meal_is_planned_no_more_often_than_it_was_ordered(hass) -> None:
    plan = _plan(hass)
    await plan.async_set(TODAY, recipe_id="shrimp")
    with pytest.raises(HomeAssistantError, match="already planned 1 time"):
        await plan.async_set(TODAY + timedelta(days=1), recipe_id="shrimp")
    # Replacing a slot with what it already holds is not a second use.
    await plan.async_set(TODAY, recipe_id="shrimp")
    # A doubled portion goes on two days.
    await plan.async_set(TODAY + timedelta(days=1), recipe_id="couscous")
    await plan.async_set(TODAY + timedelta(days=2), recipe_id="couscous")
    with pytest.raises(HomeAssistantError):
        await plan.async_set(TODAY + timedelta(days=3), recipe_id="couscous")


async def test_only_what_is_in_a_box_can_be_planned(hass) -> None:
    plan = _plan(hass)
    with pytest.raises(HomeAssistantError, match="isn't in any of your boxes"):
        await plan.async_set(TODAY, recipe_id="unchosen")
    with pytest.raises(HomeAssistantError, match="wasn't bought"):
        await plan.async_set(TODAY, market_item_id="m9")
    with pytest.raises(HomeAssistantError, match="isn't something to plan a meal around"):
        await plan.async_set(TODAY, market_item_id="pet")


async def test_dinner_courses_and_the_market_cooking_time(hass) -> None:
    plan = _plan(hass)
    await plan.async_set(TODAY, recipe_id="shrimp")
    await plan.async_set(TODAY, course="appetizer", market_item_id="m2")
    await plan.async_set(TODAY, course="dessert", market_item_id="m5")

    dinner = plan.as_dict()["days"]["2026-10-14"]["dinner"]
    assert dinner["appetizer"]["name"] == "Gyoza"
    assert dinner["appetizer"]["minutes"] == 15  # from the Market item's recipe detail
    assert dinner["dessert"]["minutes"] is None  # no recipe behind it
    assert plan.coordinator.client.calls == ["gyoza-recipe"]
    with pytest.raises(HomeAssistantError, match="Only dinner"):
        await plan.async_set(TODAY, meal="lunch", course="dessert", market_item_id="m5")


async def test_dinner_can_follow_the_recipe(hass) -> None:
    plan = _plan(hass, {CONF_DINNER_USE_COOKING_TIME: True})
    await plan.async_set(TODAY, recipe_id="shrimp")
    await plan.async_set(TODAY, course="appetizer", market_item_id="m2")
    await plan.async_set(TODAY + timedelta(days=1), text="Pizza night")

    days = plan.as_dict()["days"]
    # 30 min shrimp + 15 min gyoza + 45 min extra.
    assert days["2026-10-14"]["dinner"]["end"] == "2026-10-14T19:00:00-04:00"
    # A note has no cooking time: the fixed 90 minutes.
    assert days["2026-10-15"]["dinner"]["end"] == "2026-10-15T19:00:00-04:00"


async def test_a_day_can_start_at_its_own_time(hass) -> None:
    plan = _plan(hass)
    await plan.async_set(TODAY, text="  Pizza   night ", start_time=time(18, 30))
    dinner = plan.as_dict()["days"]["2026-10-14"]["dinner"]
    assert dinner["main"] == {"kind": "note", "name": "Pizza night"}
    assert dinner["start"] == "2026-10-14T18:30:00-04:00"
    assert dinner["custom_start"] is True
    # Moving it back to the default time stores no override.
    await plan.async_set(TODAY, start_time=time(17, 30))
    assert plan.as_dict()["days"]["2026-10-14"]["dinner"]["custom_start"] is False
    with pytest.raises(HomeAssistantError, match="before changing its time"):
        await plan.async_set(TODAY, meal="lunch", start_time=time(12, 30))


async def test_clearing_a_course_or_a_meal(hass) -> None:
    plan = _plan(hass)
    await plan.async_set(TODAY, recipe_id="shrimp")
    await plan.async_set(TODAY, course="dessert", market_item_id="m5")
    plan.clear(TODAY, course="main")
    # The dessert alone still makes a dinner, titled after it.
    assert slot_title(plan.slot(TODAY, "dinner")) == "Lava Cakes"
    plan.clear(TODAY)
    assert plan.as_dict()["days"] == {}


async def test_listeners_hear_every_change(hass) -> None:
    plan = _plan(hass)
    heard = []
    remove = plan.async_add_listener(lambda: heard.append(1))
    await plan.async_set(TODAY, recipe_id="shrimp")
    plan.clear(TODAY)
    remove()
    await plan.async_set(TODAY, recipe_id="shrimp")
    assert len(heard) == 2


# ---- storage ------------------------------------------------------------------------------------


async def test_the_plan_survives_a_reload_and_is_removed_with_the_entry(hass) -> None:
    plan = _plan(hass)
    await plan.async_set(TODAY, recipe_id="shrimp")
    await plan.async_flush()

    reloaded = _plan(hass)
    await reloaded.async_load()
    assert reloaded.as_dict()["days"] == plan.as_dict()["days"]

    await async_remove_meal_plan(hass, "entry1")
    gone = _plan(hass)
    await gone.async_load()
    assert gone.days == {}


async def test_days_past_the_history_window_are_forgotten(hass) -> None:
    plan = _plan(hass, {CONF_HISTORY_WEEKS: 1})
    plan.days = {
        "2026-09-01": {"dinner": {"main": {"kind": "note", "name": "Old"}}},
        "2026-10-13": {"dinner": {"main": {"kind": "note", "name": "Recent"}}},
    }
    await plan.async_set(TODAY, text="Tonight")
    assert sorted(plan.days) == ["2026-10-13", "2026-10-14"]


# ---- calendar and sensors -----------------------------------------------------------------------


async def test_calendar_events_are_timed_and_worded_in_the_language(
    hass, enable_custom_integrations
) -> None:
    plan = _plan(hass)
    await plan.async_set(TODAY, recipe_id="shrimp")
    await plan.async_set(TODAY, course="appetizer", market_item_id="m2")
    await plan.async_set(TODAY + timedelta(days=1), meal="breakfast", text="Pancakes")

    calendar = HelloFreshMealsCalendar.__new__(HelloFreshMealsCalendar)
    calendar.coordinator = plan.coordinator
    calendar._meal_plan = plan
    calendar._labels = await async_meal_labels(hass)
    events = calendar._build_events()

    assert [(e.summary, e.start.isoformat(), e.end.isoformat()) for e in events] == [
        ("Dinner: Butter Shrimp", "2026-10-14T17:30:00-04:00", "2026-10-14T19:00:00-04:00"),
        ("Breakfast: Pancakes", "2026-10-15T07:30:00-04:00", "2026-10-15T08:00:00-04:00"),
    ]
    assert events[0].description == "Appetizer: Gyoza\nCooking time: 30 min"
    assert events[0].uid == "entry1_meal_2026-10-14_dinner"

    stamp = calendar.extra_state_attributes["plan_updated"]
    plan.updated -= timedelta(minutes=1)
    await plan.async_set(TODAY + timedelta(days=2), text="Leftovers")
    assert calendar.extra_state_attributes["plan_updated"] > stamp

    hass.config.language = "de"
    calendar._labels = await async_meal_labels(hass)
    assert calendar._build_events()[0].summary == "Abendessen: Butter Shrimp"


async def test_tonights_dinner_sensor(hass) -> None:
    plan = _plan(hass)
    await plan.async_set(TODAY, recipe_id="shrimp")
    await plan.async_set(TODAY, course="dessert", market_item_id="m5")
    await plan.async_set(TODAY + timedelta(days=1), text="Leftovers")

    sensor = HelloFreshMealSensor.__new__(HelloFreshMealSensor)
    sensor._meal_plan = plan
    sensor._meal = "dinner"
    assert sensor.native_value == "Butter Shrimp"
    attrs = sensor.extra_state_attributes
    assert attrs["start"] == "2026-10-14T17:30:00-04:00"
    assert attrs["recipe_id"] == "shrimp"
    assert attrs["cooking_minutes"] == 30
    assert attrs["dessert"] == "Lava Cakes"
    assert attrs["appetizer"] is None
    assert attrs["tomorrow"] == "Leftovers"

    sensor._meal = "lunch"
    assert sensor.native_value == "None"
    assert sensor.extra_state_attributes == {"date": "2026-10-14", "tomorrow": None}
