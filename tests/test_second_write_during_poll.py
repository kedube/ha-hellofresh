"""A second write that lands while the first write's background poll is still reading.

The running poll read the account before the second write. Publishing its result put the
change back, and when the poll outlasted the 10 s cooldown the debouncer dropped the second
write's refresh ("any call is good"), so it stayed back until the next scheduled poll. The
coordinator now keeps the write's snapshot and reads the account again after such a poll.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.hellofresh.api import (
    HelloFreshAccountData,
    HelloFreshSubscription,
    HelloFreshWeek,
)
from custom_components.hellofresh.const import DOMAIN
from custom_components.hellofresh.coordinator import HelloFreshDataUpdateCoordinator
from custom_components.hellofresh.parsers import local_today


def _account(*, skipped: bool) -> HelloFreshAccountData:
    return HelloFreshAccountData(
        account_data_available=True,
        weeks=[
            HelloFreshWeek(
                week_id="2026-W49",
                display_name="Week 49",
                subscription_id="sub-1",
                delivery_date=local_today() + timedelta(days=12),
                selection_deadline=datetime.now(UTC) + timedelta(days=7),
                status="PAUSED" if skipped else "RUNNING",
                is_skipped=skipped,
                allowed_actions={"mealSwap": True},
            )
        ],
        subscriptions=[HelloFreshSubscription(subscription_id="sub-1")],
    ).finalize()


class FakeClient:
    """The client surface the coordinator uses; each poll waits until the test answers it."""

    base_url = "https://www.hellofresh.example"

    def __init__(self) -> None:
        self.last_account_data: HelloFreshAccountData | None = None
        self.polls: list[asyncio.Future[HelloFreshAccountData]] = []

    def restore_account_data(self, data: HelloFreshAccountData | None) -> None:
        self.last_account_data = data

    async def async_get_account_data(self) -> HelloFreshAccountData:
        answer: asyncio.Future[HelloFreshAccountData] = asyncio.get_running_loop().create_future()
        self.polls.append(answer)
        if len(self.polls) > 1:
            # Any later poll reads the account as it is now: skipped.
            answer.set_result(_account(skipped=True))
        data = await answer
        self.last_account_data = data
        return data


async def _settle() -> None:
    """Let ready callbacks run without waiting on poll A's task (it is not answered yet)."""
    for _ in range(20):
        await asyncio.sleep(0)


async def test_a_second_write_during_the_first_writes_poll_is_not_lost(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="HelloFresh", data={"country": "us"})
    entry.add_to_hass(hass)
    client = FakeClient()
    coordinator = HelloFreshDataUpdateCoordinator(
        hass, client, entry, update_interval=timedelta(minutes=180)
    )
    coordinator.async_add_listener(lambda: None)  # an entity
    start = _account(skipped=False)
    client.last_account_data = start
    coordinator.async_set_updated_data(start)

    # Write 1 (say, saving meals): its snapshot is shown and a background poll starts.
    client.last_account_data = _account(skipped=False)
    await coordinator.async_after_write()
    await _settle()
    assert len(client.polls) == 1  # poll A, reading the account as it was after write 1

    # Write 2 (skipping the week) lands while poll A is still running.
    skipped = _account(skipped=True)
    client.last_account_data = skipped
    await coordinator.async_after_write()
    await _settle()
    assert coordinator.data is skipped

    # Poll A is slow (a full poll reads every week's menu): the cooldown passes first.
    freezer.tick(timedelta(seconds=11))
    async_fire_time_changed(hass)
    await _settle()

    # Poll A finishes with what it read before write 2; the skip stays on screen.
    client.polls[0].set_result(_account(skipped=False))
    await hass.async_block_till_done()
    assert coordinator.data.weeks[0].is_skipped is True, "poll A's older read replaced the skip"
    assert client.last_account_data.weeks[0].is_skipped is True

    # And the account is read again, so the skip is confirmed rather than assumed.
    for _ in range(3):
        freezer.tick(timedelta(seconds=11))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert len(client.polls) == 2, "write 2's refresh was dropped; the skip stays hidden"
    assert coordinator.data.weeks[0].is_skipped is True
    assert coordinator.last_update_success
    await coordinator.async_shutdown()


async def test_a_quick_poll_also_gets_the_follow_up(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Same story, but poll A answers inside the cooldown: write 2's refresh does run."""
    entry = MockConfigEntry(domain=DOMAIN, title="HelloFresh", data={"country": "us"})
    entry.add_to_hass(hass)
    client = FakeClient()
    coordinator = HelloFreshDataUpdateCoordinator(
        hass, client, entry, update_interval=timedelta(minutes=180)
    )
    coordinator.async_add_listener(lambda: None)
    start = _account(skipped=False)
    client.last_account_data = start
    coordinator.async_set_updated_data(start)
    client.last_account_data = _account(skipped=False)
    await coordinator.async_after_write()
    await _settle()
    client.last_account_data = _account(skipped=True)
    await coordinator.async_after_write()
    await _settle()
    client.polls[0].set_result(_account(skipped=False))
    await hass.async_block_till_done()
    for _ in range(3):
        freezer.tick(timedelta(seconds=11))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert len(client.polls) == 2
    assert coordinator.data.weeks[0].is_skipped is True
