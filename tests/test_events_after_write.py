"""A change made in Home Assistant fires its lifecycle events too.

A write publishes its snapshot at once and the account is re-read behind it. That poll compares
against the write's snapshot, so it sees no change: the write itself records the transitions
it caused (a skip from the switch, the service or the card fires ``week_skipped``).
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hellofresh.const import DOMAIN
from custom_components.hellofresh.coordinator import (
    EVENT_WEEK_SKIPPED,
    HelloFreshDataUpdateCoordinator,
)
from test_client_fixes import Account, FakeHelloFresh, make_client


async def test_a_skip_made_in_home_assistant_is_announced(hass: HomeAssistant) -> None:
    server = FakeHelloFresh()
    account = Account(server)
    client = make_client(server)
    entry = MockConfigEntry(domain=DOMAIN, title="HelloFresh", data={"country": "us"})
    entry.add_to_hass(hass)
    coordinator = HelloFreshDataUpdateCoordinator(
        hass, client, entry, update_interval=timedelta(minutes=180)
    )
    await coordinator.async_refresh()
    assert coordinator.data.get_week(account.week_id).is_skipped is False

    # What the skip switch / hellofresh.skip_week do.
    await client.async_skip_week(account.week_id)
    await coordinator.async_after_write(full_refresh=False)
    assert coordinator.data.get_week(account.week_id).is_skipped is True
    # The rest of the account is re-read behind it (the background refresh).
    await coordinator.async_refresh()

    assert [kind for _, kind, _ in coordinator.delivery_events] == [EVENT_WEEK_SKIPPED]


async def test_a_skip_seen_only_by_the_poll_is_announced(hass: HomeAssistant) -> None:
    server = FakeHelloFresh()
    account = Account(server)
    client = make_client(server)
    entry = MockConfigEntry(domain=DOMAIN, title="HelloFresh", data={"country": "us"})
    entry.add_to_hass(hass)
    coordinator = HelloFreshDataUpdateCoordinator(
        hass, client, entry, update_interval=timedelta(minutes=180)
    )
    await coordinator.async_refresh()
    account.status = "PAUSED"  # skipped in the HelloFresh app
    await coordinator.async_refresh()
    assert [kind for _, kind, _ in coordinator.delivery_events] == [EVENT_WEEK_SKIPPED]
