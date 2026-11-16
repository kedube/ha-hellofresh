"""A cart write is built on the week's box as HelloFresh has it now, not the last poll's.

The box (meal count and number of people) used to come from the week as it was before the
write's lock: a resize just before, or a change made in the app since the poll, was written
back over, and a menu read with the wrong box answers with the plan box's default picks.
"""

from __future__ import annotations

import asyncio

from test_client_fixes import Account, FakeHelloFresh, make_client
from test_week_servings import _polled


async def test_a_resize_then_a_meal_change_at_once_keeps_the_new_number_of_people() -> None:
    """Mirror of test_a_meal_change_and_a_resize_of_one_week_run_in_turn, other order."""
    _server, account, client, _data = await _polled()

    await asyncio.gather(
        client.async_change_week_servings(account.week_id, 4),
        client.async_select_meals(
            account.week_id, ["recipe-11", "recipe-18", "recipe-20", "recipe-32"]
        ),
    )

    assert account.resizes[0]["productHandle"] == "US-CBU-3-4-0"
    # The meal change ran second; it must keep the 4-person box and add the 4th meal.
    assert account.puts[-1]["params"]["product-sku"] == "US-CBU-4-4-0"
    assert account.box == "US-CBU-4-4-0"


async def test_a_resize_that_hellofreshs_read_does_not_show_yet_is_kept_by_the_next_write() -> None:
    """HelloFresh's read can lag a resize; the box written while the write waited wins then."""
    _server, account, client, _data = await _polled()
    account.shows_resize = False

    await asyncio.gather(
        client.async_change_week_servings(account.week_id, 4),
        client.async_select_meals(
            account.week_id, ["recipe-11", "recipe-18", "recipe-20", "recipe-32"]
        ),
    )

    assert account.resizes[0]["productHandle"] == "US-CBU-3-4-0"
    assert account.puts[-1]["params"]["product-sku"] == "US-CBU-4-4-0"


async def test_a_meal_change_keeps_a_people_change_made_in_the_app_since_the_poll() -> None:
    """The cart write is built from the week as HelloFresh has it now."""
    _server, account, client, _data = await _polled()
    account.box = "US-CBU-3-4-0"  # the week changed to 4 people in the HelloFresh app

    await client.async_select_meals(account.week_id, ["recipe-11", "recipe-18", "recipe-32"])

    assert account.puts[-1]["params"]["product-sku"] == "US-CBU-3-4-0"


class SkuServedAccount(Account):
    """The week menu as documented: asked with another box than the week's own, it answers
    with that box's default picks, not the customer's (docs/hellofresh-api.md, Week menu)."""

    DEFAULT_PICKS = {11: 1, 18: 1, 20: 1}

    def _menu(self, params, body):
        status, menu = super()._menu(params, body)
        if status == 200 and params.get("product-sku") != self.box:
            for meal in menu["meals"]:
                meal["selection"]["quantity"] = self.DEFAULT_PICKS.get(meal["index"], 0)
        return status, menu


async def test_a_market_write_keeps_a_fourth_meal_added_in_the_app_since_the_poll() -> None:
    server = FakeHelloFresh()
    account = SkuServedAccount(server)
    client = make_client(server)
    await client.async_get_account_data()
    # In the HelloFresh app, after the poll: a 4th meal, so the week now has a 4-meal box.
    account.selection = {11: 1, 18: 1, 20: 1, 32: 1}
    account.box = "US-CBU-4-2-0"

    await client.async_select_market_items(account.week_id, {"m-des": 1})

    assert account.selection == {11: 1, 18: 1, 20: 1, 32: 1}
    assert account.box == "US-CBU-4-2-0"
