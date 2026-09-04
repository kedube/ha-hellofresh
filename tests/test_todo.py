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
from datetime import date, timedelta
from types import SimpleNamespace

from homeassistant.components.todo import TodoItem, TodoItemStatus

from custom_components.hellofresh.todo import (
    HelloFreshPrepListTodo,
    _format_amounts,
    _unit_key,
)

# Delivery dates are anchored to ``today`` so the ``delivery_date >= today`` filter in
# ``_all_covered_weeks`` stays deterministic regardless of when the suite runs — the
# original fixed literals (2026-08-25 et al.) started failing the day the anchor date
# passed. Same convention as ``tests/test_entities.py``.
TODAY = date.today()
DELIVERY_1 = TODAY + timedelta(days=2)
DELIVERY_2 = DELIVERY_1 + timedelta(days=7)
DELIVERY_3 = DELIVERY_1 + timedelta(days=14)


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
    delivery: date | None = DELIVERY_1,
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
        # The tick set lives on the coordinator so both slot entities SHARE it (the week
        # handover between slots is what makes ticks survive a box landing).
        prep_completed=set(),
    )
    entity = HelloFreshPrepListTodo.__new__(HelloFreshPrepListTodo)
    entity.coordinator = coordinator  # type: ignore[attr-defined]
    entity._slot = slot
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


def test_units_in_one_family_are_converted_and_combined() -> None:
    """3 teaspoon is 1 tablespoon, so 4 tablespoon + 3 teaspoon is 5 tablespoon."""
    assert _format_amounts([(4, "tablespoon"), (3, "teaspoon")]) == "5 tablespoon"
    assert _format_amounts([(1, "tablespoon"), (1.5, "teaspoon")]) == "1.5 tablespoon"
    assert _format_amounts([(500, "g"), (1, "kg")]) == "1.5 kg"
    assert _format_amounts([(8, "oz"), (1, "lb")]) == "1.5 lb"


def test_aliases_are_the_same_unit() -> None:
    """ "tbsp", "tbsp." and "tablespoon" must not be three different units."""
    assert _format_amounts([(2, "tbsp"), (2, "tablespoon")]) == "4 tbsp"
    # The first spelling seen is what gets displayed; only matching is normalized.
    assert _format_amounts([(1, "tbsp."), (1, "Tablespoon")]) == "2 tbsp."
    assert _format_amounts([(1, "tsp"), (2, "teaspoons")]) == "3 tsp"


def test_a_combined_total_must_be_measurable() -> None:
    """Combining stops when the result is a fraction no one can measure.

    "1 cup + 1 teaspoon" is 1.02 cup — arithmetically right, useless in a kitchen, and it
    hides the teaspoon entirely. Both amounts are kept instead.
    """
    assert _format_amounts([(1, "cup"), (1, "teaspoon")]) == "1 cup + 1 teaspoon"
    assert _format_amounts([(1, "pound"), (1, "ounce")]) == "1 pound + 1 ounce"


def test_combining_never_promotes_to_an_unused_unit() -> None:
    """The total stays in a unit the recipes actually used.

    4 tablespoon + 3 teaspoon is exactly 2.5 fluid ounce, but nobody wrote "fluid ounce", so
    reporting it that way would be equal and useless.
    """
    assert "fluid ounce" not in _format_amounts([(4, "tablespoon"), (3, "teaspoon")])
    # Nor does a failed combination fall back to the family's smallest unit.
    assert _format_amounts([(1, "cup"), (1, "teaspoon")]) != "49 teaspoon"


def test_different_families_are_never_converted() -> None:
    """Weight and volume cannot be converted without knowing the ingredient."""
    assert _format_amounts([(100, "g"), (1, "cup")]) == "100 g + 1 cup"
    # Metric and imperial volume are also left alone: 1 tsp is 4.929 ml, so any combined
    # total would be a fraction less readable than the two amounts.
    assert _format_amounts([(15, "ml"), (1, "tablespoon")]) == "15 ml + 1 tablespoon"


def test_unknown_units_keep_their_own_subtotal() -> None:
    """An unrecognized unit still adds up; it just never joins a family."""
    assert _format_amounts([(2, "clove"), (1, "clove")]) == "3 clove"
    assert _format_amounts([(1, "clove"), (1, "teaspoon")]) == "1 clove + 1 teaspoon"


def test_a_unit_with_no_amount_counts_as_one() -> None:
    """HelloFresh writes a bare "teaspoon" for a single teaspoon.

    Reading it as 1 makes it both render as "1 teaspoon" and add up with the other recipes'
    teaspoons, instead of trailing behind them as a stray bare word.
    """
    assert _format_amounts([(None, "teaspoon")]) == "1 teaspoon"
    assert _format_amounts([(None, "tsp")]) == "1 tsp"
    assert _format_amounts([(None, "teaspoon"), (2, "teaspoon")]) == "3 teaspoon"
    assert _format_amounts([(None, "teaspoon"), (None, "teaspoon")]) == "2 teaspoon"
    # It combines across the family too: 1 tablespoon + 3 teaspoon = 2 tablespoon.
    assert _format_amounts([(None, "tablespoon"), (3, "teaspoon")]) == "2 tablespoon"


def test_an_ingredient_with_no_amount_at_all_stays_bare() -> None:
    """ "Salt" with neither amount nor unit must not become "1"."""
    assert _format_amounts([(None, None)]) == ""


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
    week = _week("2026-W35", [_recipe("r1")], delivery=DELIVERY_1)
    entity = _make_entity(week, {"r1": _detail([{"name": "Salt", "shipped": False}])})
    _run(entity._async_rebuild())

    assert entity.todo_items[0].due == DELIVERY_1


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
    w1 = _week("2026-W35", [_recipe("r1")], DELIVERY_1)
    w2 = _week("2026-W36", [_recipe("r2")], DELIVERY_2)
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
    assert current.todo_items[0].due == DELIVERY_1
    assert following.todo_items[0].due == DELIVERY_2


def test_a_staple_needed_in_both_weeks_is_not_merged() -> None:
    """Each week reports its own total; amounts are never summed across weeks."""
    w1 = _week("2026-W35", [_recipe("r1")], DELIVERY_1)
    w2 = _week("2026-W36", [_recipe("r2")], DELIVERY_2)
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
    w1 = _week("2026-W35", [_recipe("r1")], DELIVERY_1)
    skipped = _week("2026-W36", [_recipe("r2")], DELIVERY_2, is_skipped=True)
    w3 = _week("2026-W37", [_recipe("r3")], DELIVERY_3)
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

    Ticks are keyed to the week id rather than the slot AND live in one set shared by both
    entities via the coordinator, so the week that was "following" arrives in the current
    slot — on the OTHER entity — with everything already bought still ticked. (A per-entity
    set was the shipped bug: the handover between entities stranded the ticks.)
    """
    w1 = _week("2026-W35", [_recipe("r1")], DELIVERY_1)
    w2 = _week("2026-W36", [_recipe("r2")], DELIVERY_2)
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
    # while the slot-0 entity picks W36 up — carrying the tick with the week through the
    # coordinator's shared set (no copying: the new entity reads the same set).
    current = _make_entity(w2, details, weeks=[w2], slot=0)
    current.coordinator.prep_completed = entity.coordinator.prep_completed
    _run(current._async_rebuild())

    assert [i.uid for i in current.todo_items] == ["2026-W36:pepper"]
    assert current.todo_items[0].status is TodoItemStatus.COMPLETED


def test_slot_beyond_the_covered_weeks_is_empty() -> None:
    """With only one box on its way, the following-week list is simply empty."""
    w1 = _week("2026-W35", [_recipe("r1")], DELIVERY_1)
    entity = _make_entity(
        w1, {"r1": _detail([{"name": "Salt", "shipped": False}])}, weeks=[w1], slot=1
    )
    _run(entity._async_rebuild())

    assert entity.todo_items == []


def test_attributes_name_the_delivery_the_list_belongs_to() -> None:
    """A dashboard heading needs the date, which the entity name does not carry."""
    w1 = _week("2026-W35", [_recipe("r1")], DELIVERY_1)
    w2 = _week("2026-W36", [_recipe("r2")], DELIVERY_2)
    details = {"r1": _detail([]), "r2": _detail([])}

    current = _make_entity(w1, details, weeks=[w1, w2], slot=0)
    following = _make_entity(w1, details, weeks=[w1, w2], slot=1)

    assert current.extra_state_attributes["delivery_date"] == DELIVERY_1.isoformat()
    assert following.extra_state_attributes["delivery_date"] == DELIVERY_2.isoformat()
    assert following.extra_state_attributes["week_id"] == "2026-W36"


def test_compound_unit_spellings_are_recognized() -> None:
    """HelloFresh writes units as "name (abbrev)", not as a bare name.

    Regression guard for the real payload format. The alias table originally matched only
    "tablespoon" or "tbsp", so the live ``"tablespoon (tbsp)"`` fell through as an unknown
    unit and nothing ever combined — a prep list showed
    "4 tablespoon (tbsp) + 3 teaspoon (tsp)" instead of "5 tablespoon (tbsp)".
    """
    assert (
        _format_amounts([(4, "tablespoon (tbsp)"), (3, "teaspoon (tsp)")]) == "5 tablespoon (tbsp)"
    )
    assert (
        _format_amounts([(1, "tablespoon (tbsp)"), (6, "teaspoon (tsp)")]) == "3 tablespoon (tbsp)"
    )
    # The compound and bare spellings are the same unit.
    assert _format_amounts([(2, "tablespoon (tbsp)"), (2, "tbsp")]) == "4 tablespoon (tbsp)"
    assert _format_amounts([(500, "gram (g)"), (1, "kilogram (kg)")]) == "1.5 kilogram (kg)"


def test_compound_unit_resolves_from_either_half() -> None:
    """Either the name or the abbreviation is enough to identify the unit."""
    assert _unit_key("tablespoon (tbsp)") == "tablespoon"
    assert _unit_key("Tablespoon (TBSP)") == "tablespoon"
    # "c" is not in the alias table, but the name half still resolves it.
    assert _unit_key("cup (c)") == "cup"
    # An unrecognized compound is left alone rather than being forced into a family.
    assert _unit_key("clove (whole)") == "clove (whole)"


def test_compound_units_still_respect_the_conversion_guards() -> None:
    """The guards must hold for the real spelling too, not just the bare one."""
    # Weight and volume never merge.
    assert _format_amounts([(100, "gram (g)"), (1, "cup (c)")]) == "100 gram (g) + 1 cup (c)"
    # An unmeasurable total stays split.
    assert (
        _format_amounts([(1, "cup (c)"), (1, "teaspoon (tsp)")]) == "1 cup (c) + 1 teaspoon (tsp)"
    )


def test_prune_keeps_the_sibling_entitys_week() -> None:
    """Rebuilding one slot must not throw away the OTHER slot's ticks.

    The shipped prune kept only the entity's own week; with the set now shared between
    both entities, that would have erased next week's check-offs on every rebuild of the
    current week. Only weeks NEITHER entity covers any more get dropped.
    """
    w1 = _week("2026-W35", [_recipe("r1")], DELIVERY_1)
    w2 = _week("2026-W36", [_recipe("r2")], DELIVERY_2)
    details = {"r1": _detail([{"name": "Salt", "shipped": False}]), "r2": _detail([])}

    entity = _make_entity(w1, details, weeks=[w1, w2], slot=0)
    entity.coordinator.prep_completed.update(
        {("2026-W35", "salt"), ("2026-W36", "pepper"), ("2026-W30", "stale-long-gone")}
    )
    _run(entity._async_rebuild())

    assert entity.coordinator.prep_completed == {
        ("2026-W35", "salt"),
        ("2026-W36", "pepper"),
    }


def test_restore_payload_round_trips_the_ticks() -> None:
    """The RestoreEntity extra data must serialize every tick and reload losslessly."""
    w1 = _week("2026-W35", [_recipe("r1")], DELIVERY_1)
    entity = _make_entity(w1, {"r1": _detail([])}, weeks=[w1], slot=0)
    entity.coordinator.prep_completed.update({("2026-W35", "salt"), ("2026-W36", "pepper")})

    stored = entity.extra_restore_state_data.as_dict()
    assert stored == {"ticks": [["2026-W35", "salt"], ["2026-W36", "pepper"]]}

    # A fresh session seeds its shared set from the stored payload (the same parse
    # async_added_to_hass performs).
    fresh = _make_entity(w1, {"r1": _detail([])}, weeks=[w1], slot=0)
    for entry in stored["ticks"]:
        assert isinstance(entry, list) and len(entry) == 2
        fresh.coordinator.prep_completed.add((str(entry[0]), str(entry[1])))
    assert fresh.coordinator.prep_completed == entity.coordinator.prep_completed
