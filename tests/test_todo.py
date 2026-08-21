"""Tests for the HelloFresh prep-list todo platform.

The prep list is a *projection* of one delivery week's selected meals — the ingredients
HelloFresh does not ship — so the behaviors worth pinning are the ones where a naive
implementation quietly does the wrong thing:

1. An ingredient with **no** ``shipped`` key must not be treated as a pantry staple. The API
   field is tri-state; coercing a missing flag to False would tell the cook to go buy the
   chicken that is already in the box.
2. Amounts are **not** summed across recipes. ``amount``/``unit`` are free-form API values,
   so "1.5 tablespoon" + "2 tbsp" cannot be added without inventing precision.
3. Check-off state is keyed to ingredient identity, not list position, and is dropped when
   the anchor week rolls over — last week's ticks must not pre-complete a fresh box's list.
"""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

from homeassistant.components.todo import TodoItem, TodoItemStatus

from custom_components.hellofresh.todo import HelloFreshPrepListTodo, _format_amounts


def _run(coro):
    """Drive one coroutine on a private loop.

    Matches the suite's existing hass-free style (see ``tests/test_calendar.py``); the loop is
    intentionally left unclosed because a session-scoped fixture holds one open.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def _recipe(recipe_id: str, *, selected: bool = True) -> SimpleNamespace:
    return SimpleNamespace(recipe_id=recipe_id, is_selected=selected)


def _week(
    week_id: str, recipes: list, delivery: date | None = date(2026, 8, 25)
) -> SimpleNamespace:
    return SimpleNamespace(week_id=week_id, recipes=recipes, delivery_date=delivery)


def _detail(ingredients: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(ingredients=ingredients)


def _make_entity(week, details: dict[str, object], *, servings: int | None = 2):
    """Build a prep-list entity over stub coordinator data."""
    data = SimpleNamespace(
        next_delivery_week_obj=week,
        next_configurable_week=None,
        primary_subscription=SimpleNamespace(servings=servings),
    )

    calls: list[tuple[str, int | None]] = []

    async def fake_detail(recipe_id, *, servings=None, include_favorite=True):
        calls.append((recipe_id, servings))
        result = details[recipe_id]
        if isinstance(result, Exception):
            raise result
        return result

    coordinator = SimpleNamespace(
        data=data,
        client=SimpleNamespace(async_get_recipe_detail=fake_detail),
        config_entry=SimpleNamespace(entry_id="e1", title="HelloFresh (US)"),
    )
    entity = HelloFreshPrepListTodo.__new__(HelloFreshPrepListTodo)
    entity.coordinator = coordinator  # type: ignore[attr-defined]
    entity._completed = set()
    entity._completed_week = None
    entity._items = []
    entity._built_for = (None, ())
    entity.async_write_ha_state = lambda: None  # type: ignore[method-assign]
    entity._calls = calls  # type: ignore[attr-defined]
    return entity


def test_only_unshipped_ingredients_become_items() -> None:
    """Items are the things HelloFresh does not put in the box."""
    week = _week("2026-W35", [_recipe("r1")])
    entity = _make_entity(
        week,
        {
            "r1": _detail(
                [
                    {"name": "Chicken", "shipped": True, "amount": 1, "unit": "lb"},
                    {"name": "Olive Oil", "shipped": False, "amount": 2, "unit": "tbsp"},
                    {"name": "Salt", "shipped": False, "amount": None, "unit": None},
                ]
            )
        },
    )
    _run(entity._async_rebuild())

    summaries = [i.summary for i in entity.todo_items]
    assert summaries == ["Olive Oil — 2 tbsp", "Salt"]
    assert all("Chicken" not in s for s in summaries)


def test_missing_shipped_flag_is_not_treated_as_pantry_staple() -> None:
    """A missing `shipped` key is unknown, not "you supply it".

    Guards the tri-state in HelloFreshRecipeDetail: coercing None to False here would put
    every in-box ingredient on the shopping list for any region that omits the field.
    """
    week = _week("2026-W35", [_recipe("r1")])
    entity = _make_entity(
        week,
        {
            "r1": _detail(
                [
                    {"name": "Chicken", "shipped": None, "amount": 1, "unit": "lb"},
                    {"name": "Beef"},  # key absent entirely
                    {"name": "Salt", "shipped": False},
                ]
            )
        },
    )
    _run(entity._async_rebuild())

    assert [i.summary for i in entity.todo_items] == ["Salt"]


def test_amounts_are_listed_not_summed_across_recipes() -> None:
    """Two recipes needing the same staple keep both amounts verbatim."""
    week = _week("2026-W35", [_recipe("r1"), _recipe("r2")])
    entity = _make_entity(
        week,
        {
            "r1": _detail(
                [{"name": "Butter", "shipped": False, "amount": 1.5, "unit": "tablespoon"}]
            ),
            "r2": _detail([{"name": "butter", "shipped": False, "amount": 2, "unit": "tbsp"}]),
        },
    )
    _run(entity._async_rebuild())

    assert len(entity.todo_items) == 1
    # Case-insensitive grouping, but no arithmetic across mismatched units.
    assert entity.todo_items[0].summary == "Butter — 1.5 tablespoon + 2 tbsp"


def test_identical_amounts_collapse_to_a_multiple() -> None:
    """The same amount twice reads as "2 x", not a repeated string."""
    assert _format_amounts([(1, "tsp"), (1, "tsp")]) == "2 x 1 tsp"


def test_unselected_meals_are_ignored() -> None:
    """Only meals the week will actually ship contribute ingredients."""
    week = _week("2026-W35", [_recipe("r1"), _recipe("r2", selected=False)])
    entity = _make_entity(
        week,
        {"r1": _detail([{"name": "Salt", "shipped": False}])},
    )
    _run(entity._async_rebuild())

    assert [rid for rid, _ in entity._calls] == ["r1"]


def test_servings_follow_the_subscription() -> None:
    """Ingredient amounts are resolved for the household's serving count."""
    week = _week("2026-W35", [_recipe("r1")])
    entity = _make_entity(week, {"r1": _detail([])}, servings=4)
    _run(entity._async_rebuild())

    assert entity._calls == [("r1", 4)]


def test_one_failing_recipe_does_not_blank_the_list() -> None:
    """A bad recipe fetch is skipped; the rest of the week still produces a list."""
    week = _week("2026-W35", [_recipe("r1"), _recipe("r2")])
    entity = _make_entity(
        week,
        {
            "r1": RuntimeError("boom"),
            "r2": _detail([{"name": "Salt", "shipped": False}]),
        },
    )
    _run(entity._async_rebuild())

    assert [i.summary for i in entity.todo_items] == ["Salt"]


def test_check_off_survives_a_rebuild_of_the_same_week() -> None:
    """Completion is keyed to ingredient identity, not list position."""
    week = _week("2026-W35", [_recipe("r1")])
    details = {
        "r1": _detail(
            [
                {"name": "Salt", "shipped": False},
                {"name": "Olive Oil", "shipped": False},
            ]
        )
    }
    entity = _make_entity(week, details)
    _run(entity._async_rebuild())

    salt = next(i for i in entity.todo_items if i.summary == "Salt")
    _run(
        entity.async_update_todo_item(
            TodoItem(uid=salt.uid, summary=salt.summary, status=TodoItemStatus.COMPLETED)
        )
    )
    assert next(i for i in entity.todo_items if i.summary == "Salt").status is (
        TodoItemStatus.COMPLETED
    )

    # Force a genuine rebuild of the same week (selection unchanged).
    entity._built_for = (None, ())
    _run(entity._async_rebuild())

    assert next(i for i in entity.todo_items if i.summary == "Salt").status is (
        TodoItemStatus.COMPLETED
    )
    assert next(i for i in entity.todo_items if i.summary == "Olive Oil").status is (
        TodoItemStatus.NEEDS_ACTION
    )


def test_new_week_clears_last_weeks_check_offs() -> None:
    """A fresh box starts unticked — last week's purchases don't pre-complete it."""
    entity = _make_entity(
        _week("2026-W35", [_recipe("r1")]),
        {"r1": _detail([{"name": "Salt", "shipped": False}])},
    )
    _run(entity._async_rebuild())
    salt = entity.todo_items[0]
    _run(
        entity.async_update_todo_item(
            TodoItem(uid=salt.uid, summary=salt.summary, status=TodoItemStatus.COMPLETED)
        )
    )
    assert entity.todo_items[0].status is TodoItemStatus.COMPLETED

    # The next delivery becomes the anchor.
    entity.coordinator.data.next_delivery_week_obj = _week("2026-W36", [_recipe("r1")])
    _run(entity._async_rebuild())

    assert entity.todo_items[0].summary == "Salt"
    assert entity.todo_items[0].status is TodoItemStatus.NEEDS_ACTION


def test_items_are_due_on_the_delivery_date() -> None:
    """The deadline for having staples on hand is the day the box lands."""
    week = _week("2026-W35", [_recipe("r1")], delivery=date(2026, 8, 25))
    entity = _make_entity(week, {"r1": _detail([{"name": "Salt", "shipped": False}])})
    _run(entity._async_rebuild())

    assert entity.todo_items[0].due == date(2026, 8, 25)


def test_no_upcoming_week_yields_an_empty_list() -> None:
    """With nothing on its way there is nothing to prep."""
    entity = _make_entity(None, {})
    entity.coordinator.data.next_delivery_week_obj = None
    _run(entity._async_rebuild())

    assert entity.todo_items == []


def test_unchanged_selection_skips_refetching_recipes() -> None:
    """The per-meal detail fetch is skipped when nothing about the week changed."""
    week = _week("2026-W35", [_recipe("r1")])
    entity = _make_entity(week, {"r1": _detail([{"name": "Salt", "shipped": False}])})
    _run(entity._async_rebuild())
    assert len(entity._calls) == 1

    _run(entity._async_rebuild())
    assert len(entity._calls) == 1  # no second round-trip


def test_coordinator_update_triggers_a_rebuild() -> None:
    """A coordinator poll must actually refresh the list.

    Regression guard. ``CoordinatorEntity`` sets ``should_poll = False`` and its
    ``_handle_coordinator_update`` is a *synchronous* callback that only writes state, so an
    ``async_update`` method is never called and an un-overridden callback can never await the
    per-recipe fetch. The first version of this platform had exactly that shape: it built the
    list once at startup and then never changed it as meals were swapped.
    """
    week = _week("2026-W35", [_recipe("r1")])
    entity = _make_entity(week, {"r1": _detail([{"name": "Salt", "shipped": False}])})

    scheduled: list = []
    entity.hass = SimpleNamespace(  # type: ignore[attr-defined]
        async_create_task=lambda coro: scheduled.append(coro)
    )

    entity._handle_coordinator_update()

    assert scheduled, "a coordinator poll must schedule a rebuild"
    _run(scheduled[0])
    assert [i.summary for i in entity.todo_items] == ["Salt"]


def test_entity_does_not_rely_on_polling() -> None:
    """The platform must not depend on `async_update`, which never fires here."""
    entity = HelloFreshPrepListTodo.__new__(HelloFreshPrepListTodo)
    assert entity.should_poll is False
    # `async_update` exists on HA's Entity base; what matters is that this platform does not
    # define its own, since defining one would imply a polling contract that never runs.
    assert "async_update" not in HelloFreshPrepListTodo.__dict__
    # The coordinator callback is the real refresh path, so it must be overridden here.
    assert "_handle_coordinator_update" in HelloFreshPrepListTodo.__dict__
