"""Tests for the coordinator's proactive token refresh and reauth escalation.

This path was largely untested (coordinator.py sat at 49%), which matters because it is the only
thing that turns a dead token into a Home Assistant reauth prompt. A client-side error that fails
to propagate here is invisible: polls keep "succeeding" with partial data and the user is never
asked to re-authenticate. That is exactly the shape of the history-endpoint bug fixed alongside
these tests.

Two behaviours are pinned:

* An unrecoverable auth failure must **stop the refresh timer** and start reauth. Leaving the
  timer running re-submitted the stored (stale) password every 2-10 minutes forever, which is how
  accounts get rate-limited or locked.
* A transient transport failure must do **neither** — it should be swallowed so a blip doesn't
  nag the user for credentials that are still valid.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

from custom_components.hellofresh.api import HelloFreshAuthError, HelloFreshError
from custom_components.hellofresh.coordinator import (
    DEFAULT_TOKEN_LIFETIME_SECONDS,
    MAX_TOKEN_REFRESH_INTERVAL,
    MIN_TOKEN_REFRESH_INTERVAL,
    HelloFreshDataUpdateCoordinator,
    _token_refresh_interval,
)


def _coordinator(refresh_error: Exception | None) -> tuple[object, dict]:
    """Build a coordinator with just enough wiring to exercise _async_refresh_token."""
    calls: dict[str, int] = {"reauth": 0, "stopped": 0, "refresh": 0}

    async def fake_ensure_fresh() -> None:
        calls["refresh"] += 1
        if refresh_error is not None:
            raise refresh_error

    coordinator = HelloFreshDataUpdateCoordinator.__new__(HelloFreshDataUpdateCoordinator)
    coordinator.client = SimpleNamespace(async_ensure_token_fresh=fake_ensure_fresh)
    coordinator.hass = object()
    coordinator.config_entry = SimpleNamespace(
        async_start_reauth=lambda _hass: calls.__setitem__("reauth", calls["reauth"] + 1)
    )
    # Stand in for the real timer canceller so "did we stop the timer?" is observable.
    coordinator.async_stop_token_refresh = lambda: calls.__setitem__(  # type: ignore[method-assign]
        "stopped", calls["stopped"] + 1
    )
    return coordinator, calls


def _run(coro) -> None:
    # Not closed on purpose: HA's autouse `verify_cleanup` fixture touches the current event
    # loop at teardown, and a closed one raises there. Matches the rest of the suite.
    loop = asyncio.new_event_loop()
    loop.run_until_complete(coro)


def test_auth_failure_stops_the_timer_and_requests_reauth() -> None:
    """A dead refresh token cannot heal itself: escalate to the user, stop retrying."""
    coordinator, calls = _coordinator(HelloFreshAuthError("refresh token expired"))
    _run(coordinator._async_refresh_token())

    assert calls["reauth"] == 1, "an unrecoverable auth failure must start reauth"
    assert calls["stopped"] == 1, (
        "the refresh timer must stop, or the stale password is re-submitted every few minutes"
    )


def test_transient_failure_neither_stops_the_timer_nor_nags_for_reauth() -> None:
    """Guards the over-correction: a network blip must not prompt for credentials."""
    coordinator, calls = _coordinator(HelloFreshError("connection reset"))
    _run(coordinator._async_refresh_token())

    assert calls["reauth"] == 0
    assert calls["stopped"] == 0


def test_successful_refresh_is_silent() -> None:
    """The common case leaves the timer running and asks the user for nothing."""
    coordinator, calls = _coordinator(None)
    _run(coordinator._async_refresh_token())

    assert calls == {"reauth": 0, "stopped": 0, "refresh": 1}


def test_refresh_interval_ticks_before_a_real_token_can_expire() -> None:
    """The interval must land a tick inside the client's refresh window.

    The client only refreshes once a token is past half its life, so at least one tick has to
    occur during that back half or the token expires between polls — the original bug that
    motivated a poll-independent timer.
    """
    for lifetime in (300, 600, DEFAULT_TOKEN_LIFETIME_SECONDS, 3600, 7200):
        interval = _token_refresh_interval(lifetime)
        window = lifetime / 2
        assert interval.total_seconds() <= window, (
            f"lifetime {lifetime}s: interval {interval} cannot tick inside its refresh window"
        )


def test_refresh_interval_is_clamped_both_ways() -> None:
    """Never hammer the auth endpoint, never drift past the ceiling."""
    assert _token_refresh_interval(1) == MIN_TOKEN_REFRESH_INTERVAL
    assert _token_refresh_interval(86400) == MAX_TOKEN_REFRESH_INTERVAL
    assert _token_refresh_interval(None) == timedelta(seconds=DEFAULT_TOKEN_LIFETIME_SECONDS * 0.25)


def test_unknown_lifetime_falls_back_to_the_documented_default() -> None:
    """A server that omits expires_in must not disable proactive refresh.

    Both None and 0 are falsy, so each takes the `or DEFAULT_TOKEN_LIFETIME_SECONDS` branch —
    a zero lifetime is treated as "unknown" rather than collapsing to the 2-minute floor.
    """
    default_interval = timedelta(seconds=DEFAULT_TOKEN_LIFETIME_SECONDS * 0.25)
    assert _token_refresh_interval(0) == default_interval
    assert _token_refresh_interval(None) == default_interval
    assert _token_refresh_interval(None) > timedelta(0)
