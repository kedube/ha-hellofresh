"""A write after a poll the coordinator refused builds on the data that is shown.

The client keeps every poll's result as the snapshot its writes build on, including an
account-less one the coordinator refuses. A write after it (here a favourite, which has no
background refresh) used to publish that empty account on every entity, marked successful.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hellofresh.const import DOMAIN
from custom_components.hellofresh.coordinator import HelloFreshDataUpdateCoordinator
from test_client_fixes import Account, FakeHelloFresh, make_client


async def test_a_write_after_a_rejected_poll_keeps_the_good_data(hass: HomeAssistant) -> None:
    server = FakeHelloFresh()
    account = Account(server)
    client = make_client(server)
    entry = MockConfigEntry(domain=DOMAIN, title="HelloFresh", data={"country": "us"})
    entry.add_to_hass(hass)
    coordinator = HelloFreshDataUpdateCoordinator(
        hass, client, entry, update_interval=timedelta(minutes=180)
    )
    await coordinator.async_refresh()
    good = coordinator.data
    assert good.account_data_available and good.weeks

    # HelloFresh answers the subscriptions read with no subscription once.
    server.route("GET", "/gw/api/customers/me/subscriptions", (200, {"items": []}))
    await coordinator.async_refresh()
    assert coordinator.last_update_success is False
    assert coordinator.data is good  # the guard kept the good data
    server.route("GET", "/gw/api/customers/me/subscriptions", account._subscriptions)

    # The user hearts a recipe on the card (hellofresh.add_favorite: refresh=False).
    server.route("POST", "/gw/cookbook/v1/internal-recipes", (200, {}))
    await client.async_add_favorite("recipe-11")
    await coordinator.async_after_write(full_refresh=False)

    assert coordinator.data.account_data_available, "the rejected empty account is now shown"
    assert coordinator.data.weeks
