"""A week's box is priced for the week's own number of people.

``boxSize`` is the number of people (docs/hellofresh-api.md, "Price a week's box"). A week resized
with change_week_servings, or in the app, has its own in ``product.specs.size``.
"""

from __future__ import annotations

from test_week_servings import _polled


async def test_a_four_person_weeks_box_is_priced_for_four_people() -> None:
    server, account, client, _data = await _polled()
    await client.async_change_week_servings(account.week_id, 4)
    start = len(server.requests)

    # The next poll prices the week's box again.
    await client.async_get_account_data()

    prices = [
        request["json"]
        for request in server.requests[start:]
        if request["path"] == f"/gw/v1/carts/{account.week_id}/price"
    ]
    assert prices, "the week's box was not priced"
    body = prices[-1]
    assert body["products"][0]["handle"] == "US-CBU-3-4-0"
    assert body["boxSize"] == 4
