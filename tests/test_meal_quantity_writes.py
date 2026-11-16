"""A meal's count reaches the cart as the card shows it, and the preview prices that box.

select_meals keeps a meal left out of ``quantities`` at the count it has in the box (4.26), so
the card states every count: when it left out the 1s, lowering a doubled meal to one, or
undoing a doubling, saved it doubled again. The price preview reads a left-out meal the same way.
"""

from __future__ import annotations

import pytest

from test_client_fixes import Account, FakeHelloFresh, make_client
from test_unified_card_logic import NODE, _run


async def _doubled() -> tuple[Account, object]:
    server = FakeHelloFresh()
    account = Account(server)
    account.selection = {11: 2, 18: 1, 20: 1}  # meal 11 at two portions: a 4-meal box
    account.box = "US-CBU-4-2-0"
    client = make_client(server)
    await client.async_get_account_data()
    return account, client


@pytest.mark.skipif(NODE is None, reason="node is not installed")
async def test_the_card_lowering_a_doubled_meal_to_one_portion_is_saved() -> None:
    account, client = await _doubled()
    # The card's own payload for the user's new box: meal 11 lowered to 1 portion.
    write = _run(
        """
        const w = { recipes: [
          { recipe_id: "recipe-11", course_index: 11 }, { recipe_id: "recipe-18", course_index: 18 },
          { recipe_id: "recipe-20", course_index: 20 },
        ] };
        return L.buildMealWrite(w, new Map([[11, 1], [18, 1], [20, 1]]));
        """
    )
    assert write == {
        "recipe_ids": ["recipe-11", "recipe-18", "recipe-20"],
        "quantities": {"recipe-11": 1, "recipe-18": 1, "recipe-20": 1},
    }

    await client.async_select_meals(account.week_id, write["recipe_ids"], write["quantities"])

    put = account.puts[-1]
    assert {m["index"]: m["quantity"] for m in put["meals"]} == {11: 1, 18: 1, 20: 1}
    assert put["params"]["product-sku"] == "US-CBU-3-2-0"


async def test_the_preview_prices_the_box_the_same_call_would_save() -> None:
    account, client = await _doubled()
    priced: list[dict] = []

    async def fake_price(subscription, week, course_quantities=None):
        if course_quantities is not None:  # the preview (the re-read prices the saved box)
            priced.append(dict(course_quantities))
        return {"grandTotal": 1.0}

    client._async_get_cart_price_for_week = fake_price  # type: ignore[method-assign]
    ids = ["recipe-11", "recipe-18", "recipe-20"]

    await client.async_preview_meal_price(account.week_id, ids, {})
    await client.async_select_meals(account.week_id, ids, {})

    saved = {m["index"]: m["quantity"] for m in account.puts[-1]["meals"]}
    assert priced[0] == saved


@pytest.mark.skipif(NODE is None, reason="node is not installed")
async def test_undo_of_doubling_a_meal_puts_back_one_portion() -> None:
    server = FakeHelloFresh()
    account = Account(server)  # meals 11, 18, 20 at one portion each; salmon bites x1
    client = make_client(server)
    await client.async_get_account_data()
    # The user doubles meal 11 in the card and saves (the card sends {recipe-11: 2}).
    await client.async_select_meals(
        account.week_id, ["recipe-11", "recipe-18", "recipe-20"], {"recipe-11": 2}
    )
    assert account.selection == {11: 2, 18: 1, 20: 1}

    # Then presses Undo: the card's own undo write for the box as it was saved before.
    undo = _run(
        f"""
        const w = {{ week_id: "{account.week_id}", recipes: [
          {{ recipe_id: "recipe-11", course_index: 11 }}, {{ recipe_id: "recipe-18", course_index: 18 }},
          {{ recipe_id: "recipe-20", course_index: 20 }},
        ] }};
        const before = {{ meals: new Map([[11, 1], [18, 1], [20, 1]]), market: new Map([["m-app", 1]]) }};
        return L.boxUndoWrite(w, before, {{ meals: true }});
        """
    )
    data = undo["data"]
    await client.async_select_meals(
        data["week_id"], data["recipe_ids"], data.get("quantities"), data.get("market_quantities")
    )

    assert account.selection == {11: 1, 18: 1, 20: 1}
    assert account.box == "US-CBU-3-2-0"
