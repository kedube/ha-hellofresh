"""What the coordinator shows after a write.

Every write used to wait for a whole account poll before the service call returned, and a
second write inside the refresh cooldown got no refresh at all, so it answered with the state
from before the first one. The client now keeps a snapshot it updates from a fresh read of
what it wrote; the coordinator publishes that at once and re-reads the rest in the
background. ``tests/test_client_fixes.py`` covers the client's side of that contract.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest import mock

from custom_components.hellofresh.coordinator import HelloFreshDataUpdateCoordinator

after_write = HelloFreshDataUpdateCoordinator.async_after_write


def _coordinator(*, data: object, snapshot: object) -> SimpleNamespace:
    tasks: list[object] = []

    def _background(_hass, coro, *, name):
        tasks.append(name)
        coro.close()  # the background refresh itself is Home Assistant's to run

    return SimpleNamespace(
        hass=object(),
        data=data,
        client=SimpleNamespace(last_account_data=snapshot),
        async_set_updated_data=mock.Mock(),
        async_request_refresh=mock.AsyncMock(),
        config_entry=SimpleNamespace(async_create_background_task=_background),
        background=tasks,
    )


def _run(coro) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(coro)


def test_a_write_with_a_fresh_snapshot_is_shown_at_once_and_refreshed_behind() -> None:
    old, fresh = object(), object()
    coordinator = _coordinator(data=old, snapshot=fresh)

    _run(after_write(coordinator))

    coordinator.async_set_updated_data.assert_called_once_with(fresh)
    coordinator.async_request_refresh.assert_not_awaited()
    assert coordinator.background == ["hellofresh refresh after a write"]


def test_a_write_the_client_could_not_re_read_waits_for_the_poll() -> None:
    """With no fresh snapshot there is nothing to show yet, so the write waits as it used to."""
    data = object()
    coordinator = _coordinator(data=data, snapshot=data)

    _run(after_write(coordinator))

    coordinator.async_set_updated_data.assert_not_called()
    coordinator.async_request_refresh.assert_awaited_once()
    assert coordinator.background == []


def test_a_favourite_is_shown_without_a_poll() -> None:
    """A heart changes nothing a poll reads, so its snapshot is all there is to show."""
    old, fresh = object(), object()
    coordinator = _coordinator(data=old, snapshot=fresh)

    _run(after_write(coordinator, full_refresh=False))

    coordinator.async_set_updated_data.assert_called_once_with(fresh)
    coordinator.async_request_refresh.assert_not_awaited()
    assert coordinator.background == []


def test_a_write_the_poll_does_not_read_changes_nothing() -> None:
    data = object()
    coordinator = _coordinator(data=data, snapshot=data)

    _run(after_write(coordinator, full_refresh=False))

    coordinator.async_set_updated_data.assert_not_called()
    coordinator.async_request_refresh.assert_not_awaited()


def test_before_the_first_poll_a_write_waits_for_one() -> None:
    coordinator = _coordinator(data=None, snapshot=None)

    _run(after_write(coordinator))

    coordinator.async_set_updated_data.assert_not_called()
    coordinator.async_request_refresh.assert_awaited_once()
