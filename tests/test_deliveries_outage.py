"""A 5xx or a timeout on the deliveries read fails the poll instead of emptying the account.

The deliveries read used to answer a server error with no weeks, and the poll then carried on
as a success: every week vanished for a whole scan interval, and the next good poll, diffing
against nothing, never announced what happened in between (a box delivered).
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hellofresh.const import DOMAIN
from custom_components.hellofresh.coordinator import (
    EVENT_BOX_DELIVERED,
    HelloFreshDataUpdateCoordinator,
)
from custom_components.hellofresh.parsers import local_today
from test_client_fixes import Account, FakeHelloFresh, iso_week, make_client

DELIVERIES = "/gw/api/customers/me/deliveries"


def _setup(hass: HomeAssistant, *, due_today: bool = False):
    server = FakeHelloFresh()
    account = Account(server)
    if due_today:
        account.delivery = local_today()
        account.week_id = iso_week(account.delivery)
    client = make_client(server)
    entry = MockConfigEntry(domain=DOMAIN, title="HelloFresh", data={"country": "us"})
    entry.add_to_hass(hass)
    coordinator = HelloFreshDataUpdateCoordinator(
        hass, client, entry, update_interval=timedelta(minutes=180)
    )
    return server, account, coordinator


async def test_a_deliveries_outage_keeps_the_last_good_weeks(hass: HomeAssistant) -> None:
    server, _account, coordinator = _setup(hass)
    await coordinator.async_refresh()
    good = coordinator.data
    assert coordinator.last_update_success and len(good.weeks) == 1

    server.route("GET", DELIVERIES, (503, {"error": "busy"}))
    await coordinator.async_refresh()

    # A failed update, which keeps the last good weeks.
    assert coordinator.data.weeks, "the account's weeks were replaced by an empty list"
    assert coordinator.last_update_success is False


async def test_a_box_delivered_during_the_outage_is_announced(hass: HomeAssistant) -> None:
    server, account, coordinator = _setup(hass, due_today=True)
    await coordinator.async_refresh()
    assert coordinator.data.weeks[0].delivery_date == local_today()

    server.route("GET", DELIVERIES, (503, {"error": "busy"}))
    await coordinator.async_refresh()

    account.status = "DELIVERED"  # the box arrives while the deliveries read is down
    server.route("GET", DELIVERIES, account._weeks)
    await coordinator.async_refresh()

    assert coordinator.data.weeks[0].status == "DELIVERED"
    assert [kind for _, kind, _ in coordinator.delivery_events] == [EVENT_BOX_DELIVERED]


async def test_without_an_outage_the_delivery_is_announced(hass: HomeAssistant) -> None:
    _server, account, coordinator = _setup(hass, due_today=True)
    await coordinator.async_refresh()
    account.status = "DELIVERED"
    await coordinator.async_refresh()
    assert [kind for _, kind, _ in coordinator.delivery_events] == [EVENT_BOX_DELIVERED]
