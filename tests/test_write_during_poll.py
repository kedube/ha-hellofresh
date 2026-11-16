"""A write that lands while an account poll is in flight is not undone by that poll.

Real HelloFreshDataUpdateCoordinator and Home Assistant debouncer; the client is a fake
(no network). The poll's read of the week happened before the write reached HelloFresh, and
the poll outlasts the write's refresh cooldown, which used to drop the write's own refresh.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.hellofresh.api import (
    HelloFreshAccountData,
    HelloFreshRecipe,
    HelloFreshSubscription,
    HelloFreshWeek,
)
from custom_components.hellofresh.const import DOMAIN
from custom_components.hellofresh.coordinator import HelloFreshDataUpdateCoordinator
from custom_components.hellofresh.parsers import local_today


def _data(selected: str) -> HelloFreshAccountData:
    return HelloFreshAccountData(
        account_data_available=True,
        weeks=[
            HelloFreshWeek(
                week_id="2026-W48",
                display_name="Week 48",
                subscription_id="sub-1",
                delivery_date=local_today() + timedelta(days=9),
                recipes=[
                    HelloFreshRecipe(recipe_id=rid, name=rid, is_selected=rid == selected)
                    for rid in ("r1", "r2")
                ],
            )
        ],
        subscriptions=[HelloFreshSubscription(subscription_id="sub-1", display_name="Plan")],
    ).finalize()


def _selected(data: HelloFreshAccountData) -> list[str]:
    return [r.recipe_id for r in data.weeks[0].recipes if r.is_selected]


class _Client:
    base_url = "https://www.hellofresh.com"

    def __init__(self) -> None:
        self.last_account_data = None
        self.fetches = 0
        self.gate = asyncio.Event()
        # What HelloFresh holds: r1 until the write, r2 after it.
        self.server_selected = "r1"

    async def async_get_account_data(self) -> HelloFreshAccountData:
        self.fetches += 1
        read = _data(self.server_selected)  # the week is read at the start of the poll
        if self.fetches == 2:
            await self.gate.wait()  # ...and the rest of the poll is still running
        self.last_account_data = read
        return read

    def restore_account_data(self, data: HelloFreshAccountData | None) -> None:
        self.last_account_data = data


async def test_a_write_during_a_poll_is_not_undone_by_it(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"country": "us"}, options={})
    entry.add_to_hass(hass)
    client = _Client()
    coordinator = HelloFreshDataUpdateCoordinator(hass, client, entry, timedelta(minutes=180))

    await coordinator.async_refresh()
    assert _selected(coordinator.data) == ["r1"]

    # The scheduled poll starts (it holds the refresh debouncer's lock while it runs).
    poll = hass.async_create_task(coordinator._handle_refresh_interval())
    for _ in range(5):
        await asyncio.sleep(0)
    assert client.fetches == 2

    # select_meals lands meanwhile: HelloFresh accepts r2, the client publishes its re-read,
    # and services._async_write calls async_after_write (show now, refresh behind).
    client.server_selected = "r2"
    client.last_account_data = _data("r2")
    await coordinator.async_after_write()
    assert _selected(coordinator.data) == ["r2"]

    # The poll is still running past the refresh cooldown (10 s), then finishes.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    for _ in range(5):
        await asyncio.sleep(0)
    client.gate.set()
    await poll
    # Plenty of time for any follow-up refresh.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=60))
    await hass.async_block_till_done()

    # The account holds r2; what the entities and get_weeks show should too.
    assert _selected(coordinator.data) == ["r2"], (
        f"entities show {_selected(coordinator.data)} after {client.fetches} fetches"
    )
    assert coordinator.last_update_success
    # The poll that predated the write was set aside and the account read again.
    assert client.fetches == 3
    assert _selected(client.last_account_data) == ["r2"]
    await coordinator.async_shutdown()
