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
    week_id: str,
    recipes: list,
    delivery: date | None = date(2026, 8, 25),
    *,
    is_skipped: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        week_id=week_id,
        recipes=recipes,
        delivery_date=delivery,
        is_skipped=is_skipped,
        display_name=week_id,
    )


def _detail(ingredients: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(ingredients=ingredients)


def _make_entity(
    week,
    details: dict[str, object],
    *,
    servings: int | None = 2,
    weeks: list | None = None,
    slot: int = 0,
):
    """Build a prep-list entity over stub coordinator data.

    ``weeks`` is the account's full week list the second covered week is drawn from; it
    defaults to just the anchor so single-week tests read unchanged.
    """
    data = SimpleNamespace(
        next_delivery_week_obj=week,
        next_configurable_week=None,
        primary_subscription=SimpleNamespace(servings=servings),
        weeks=weeks if weeks is not None else ([week] if week is not None else []),
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
    entity._slot = slot
    entity._completed = set()
    entity._items = []
    entity._built_for = ()
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


def test_amounts_are_summed_across_recipes() -> None:
    """Two recipes needing the same staple report one combined amount."""
    week = _week("2026-W35", [_recipe("r1"), _recipe("r2")])
    entity = _make_entity(
        week,
        {
            "r1": _detail(
                [{"name": "Butter", "shipped": False, "amount": 1.5, "unit": "tablespoon"}]
            ),
            "r2": _detail(
                [{"name": "butter", "shipped": False, "amount": 2, "unit": "tablespoon"}]
            ),
        },
    )
    _run(entity._async_rebuild())

    assert len(entity.todo_items) == 1
    # Ingredient names group case-insensitively, and the shared unit is summed.
    assert entity.todo_items[0].summary == "Butter — 3.5 tablespoon"


def test_amounts_in_the_same_unit_are_summed() -> None:
    """Two recipes wanting 2 tablespoon each need 4 tablespoon, not "2 + 2"."""
    assert _format_amounts([(2, "tablespoon"), (2, "tablespoon")]) == "4 tablespoon"
    assert _format_amounts([(1, "tsp"), (2, "tsp")]) == "3 tsp"
    # Fractions combine, and the total is rendered without trailing zeros.
    assert _format_amounts([(0.5, "cup"), (0.25, "cup")]) == "0.75 cup"
    assert _format_amounts([(1.5, "tablespoon"), (1.5, "tablespoon")]) == "3 tablespoon"


def test_unit_matching_ignores_case() -> None:
    """A unit spelled "Tablespoon" and "tablespoon" is one unit."""
    assert _format_amounts([(2, "Tablespoon"), (3, "tablespoon")]) == "5 Tablespoon"


def test_unitless_amounts_sum() -> None:
    """Countable staples (eggs) carry no unit and still add up."""
    assert _format_amounts([(2, None), (1, None)]) == "3"


def test_different_units_are_not_converted() -> None:
    """Distinct units are summed separately and joined — never converted.

    Converting tbsp->tablespoon (or cup->ounce) needs a unit table this integration has no
    business owning, and a wrong conversion is worse than none.
    """
    assert _format_amounts([(2, "tablespoon"), (2, "tbsp")]) == "2 tablespoon + 2 tbsp"


def test_non_numeric_amounts_are_kept_verbatim() -> None:
    """A range like "1-2" cannot be summed, and must not be dropped either."""
    assert _format_amounts([("1-2", "tsp"), (1, "tsp")]) == "1 tsp + 1-2 tsp"


def test_numeric_strings_are_summed() -> None:
    """Amounts arrive as strings on some recipes; those still add up."""
    assert _format_amounts([("2", "tsp"), (1, "tsp")]) == "3 tsp"


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
    entity._built_for = ()
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


def test_each_entity_covers_exactly_one_week() -> None:
    """Slot 0 is the box on its way; slot 1 is the one after it.

    They are separate entities precisely so a dashboard can give each week its own card and
    heading — HA's to-do card renders one entity, so a combined list could only ever be flat.
    """
    w1 = _week("2026-W35", [_recipe("r1")], date(2026, 8, 25))
    w2 = _week("2026-W36", [_recipe("r2")], date(2026, 9, 1))
    details = {
        "r1": _detail([{"name": "Butter", "shipped": False, "amount": 2, "unit": "tbsp"}]),
        "r2": _detail([{"name": "Salt", "shipped": False}]),
    }

    current = _make_entity(w1, details, weeks=[w1, w2], slot=0)
    _run(current._async_rebuild())
    following = _make_entity(w1, details, weeks=[w1, w2], slot=1)
    _run(following._async_rebuild())

    assert [i.uid for i in current.todo_items] == ["2026-W35:butter"]
    assert [i.uid for i in following.todo_items] == ["2026-W36:salt"]
    # Neither list leaks the other week's items.
    assert current.todo_items[0].due == date(2026, 8, 25)
    assert following.todo_items[0].due == date(2026, 9, 1)


def test_a_staple_needed_in_both_weeks_is_not_merged() -> None:
    """Each week reports its own total; amounts are never summed across weeks."""
    w1 = _week("2026-W35", [_recipe("r1")], date(2026, 8, 25))
    w2 = _week("2026-W36", [_recipe("r2")], date(2026, 9, 1))
    details = {
        "r1": _detail([{"name": "Butter", "shipped": False, "amount": 2, "unit": "tablespoon"}]),
        "r2": _detail([{"name": "Butter", "shipped": False, "amount": 1, "unit": "tablespoon"}]),
    }

    current = _make_entity(w1, details, weeks=[w1, w2], slot=0)
    _run(current._async_rebuild())
    following = _make_entity(w1, details, weeks=[w1, w2], slot=1)
    _run(following._async_rebuild())

    assert [i.summary for i in current.todo_items] == ["Butter — 2 tablespoon"]
    assert [i.summary for i in following.todo_items] == ["Butter — 1 tablespoon"]


def test_skipped_weeks_are_never_covered() -> None:
    """A skipped box ships nothing, so coverage moves past it."""
    w1 = _week("2026-W35", [_recipe("r1")], date(2026, 8, 25))
    skipped = _week("2026-W36", [_recipe("r2")], date(2026, 9, 1), is_skipped=True)
    w3 = _week("2026-W37", [_recipe("r3")], date(2026, 9, 8))
    details = {
        "r1": _detail([{"name": "Salt", "shipped": False}]),
        "r3": _detail([{"name": "Pepper", "shipped": False}]),
    }

    following = _make_entity(w1, details, weeks=[w1, skipped, w3], slot=1)
    _run(following._async_rebuild())

    # Slot 1 lands on W37, not the skipped W36.
    assert [i.uid for i in following.todo_items] == ["2026-W37:pepper"]


def test_a_week_keeps_its_ticks_when_it_shifts_slot() -> None:
    """When the current box lands, next week's check-offs move up with it.

    Ticks are keyed to the week id rather than the slot, so the week that was "following"
    arrives in the current slot with everything already bought still ticked.
    """
    w1 = _week("2026-W35", [_recipe("r1")], date(2026, 8, 25))
    w2 = _week("2026-W36", [_recipe("r2")], date(2026, 9, 1))
    details = {
        "r1": _detail([{"name": "Salt", "shipped": False}]),
        "r2": _detail([{"name": "Pepper", "shipped": False}]),
    }

    entity = _make_entity(w1, details, weeks=[w1, w2], slot=1)
    _run(entity._async_rebuild())
    pepper = entity.todo_items[0]
    _run(
        entity.async_update_todo_item(
            TodoItem(uid=pepper.uid, summary=pepper.summary, status=TodoItemStatus.COMPLETED)
        )
    )
    assert entity.todo_items[0].status is TodoItemStatus.COMPLETED

    # W35 lands. W36 shifts into slot 0, so this same entity now points at slot 1 = nothing,
    # while the slot-0 entity picks W36 up — carrying the tick with the week.
    current = _make_entity(w2, details, weeks=[w2], slot=0)
    current._completed = set(entity._completed)
    _run(current._async_rebuild())

    assert [i.uid for i in current.todo_items] == ["2026-W36:pepper"]
    assert current.todo_items[0].status is TodoItemStatus.COMPLETED


def test_slot_beyond_the_covered_weeks_is_empty() -> None:
    """With only one box on its way, the following-week list is simply empty."""
    w1 = _week("2026-W35", [_recipe("r1")], date(2026, 8, 25))
    entity = _make_entity(
        w1, {"r1": _detail([{"name": "Salt", "shipped": False}])}, weeks=[w1], slot=1
    )
    _run(entity._async_rebuild())

    assert entity.todo_items == []


def test_attributes_name_the_delivery_the_list_belongs_to() -> None:
    """A dashboard heading needs the date, which the entity name does not carry."""
    w1 = _week("2026-W35", [_recipe("r1")], date(2026, 8, 25))
    w2 = _week("2026-W36", [_recipe("r2")], date(2026, 9, 1))
    details = {"r1": _detail([]), "r2": _detail([])}

    current = _make_entity(w1, details, weeks=[w1, w2], slot=0)
    following = _make_entity(w1, details, weeks=[w1, w2], slot=1)

    assert current.extra_state_attributes["delivery_date"] == "2026-08-25"
    assert following.extra_state_attributes["delivery_date"] == "2026-09-01"
    assert following.extra_state_attributes["week_id"] == "2026-W36"
