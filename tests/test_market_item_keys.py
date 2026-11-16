"""A Market key orders the add-on it names, not a sibling sharing its group's SKU.

Every add-on in a Market group carries the group's SKU (docs/hellofresh-api.md, Market catalog),
and an add-on with no recipe id takes that SKU as its item id.
"""

from __future__ import annotations

from test_client_fixes import Account, FakeHelloFresh, make_client

PROTEIN_SKU = "US-APR-0-0-0"


class ProteinAccount(Account):
    """The fake account with a protein group of two add-ons sharing the group SKU."""

    def _menu(self, params, body):
        status, menu = super()._menu(params, body)
        if status != 200:
            return status, menu
        trout = self._addon(10089, "m-trout", "Steelhead Trout", PROTEIN_SKU, 6)
        # A second protein the catalog gives without a recipe id: its item_id becomes its SKU.
        salmon = self._addon(10090, "unused", "Salmon Fillet", PROTEIN_SKU, 6)
        salmon["recipe"] = {"name": "Salmon Fillet"}
        menu["addOns"]["groups"].append(
            {"groupType": "protein", "sku": PROTEIN_SKU, "addOns": [trout, salmon]}
        )
        return status, menu


async def test_an_add_on_is_ordered_by_its_own_item_id() -> None:
    server = FakeHelloFresh()
    account = ProteinAccount(server)
    client = make_client(server)
    data = await client.async_get_account_data()
    salmon = next(i for i in data.get_week(account.week_id).market_items if i.index == 10090)
    assert salmon.item_id == PROTEIN_SKU  # what get_weeks and the card key it by

    await client.async_select_market_items(account.week_id, {salmon.item_id: 1})

    ordered = {
        entry["index"]
        for group in account.puts[-1]["extras"]
        for entry in group["selection"]
        if entry["oneOffQuantity"] > 0
    }
    assert ordered == {10090}  # the salmon, not the trout listed before it
