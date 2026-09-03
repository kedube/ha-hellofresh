"""Tests for the shared service dispatch layer in ``__init__.py``.

Every one of the integration's 22 services goes through ``_get_target_coordinators`` to decide
*which* account it acts on, and the write services go through ``_for_each_coordinator`` to turn
client exceptions into user-facing errors. Those two closures are the highest-leverage untested
code in the package (``__init__.py`` sat at 17%): a mistake in either affects all services at
once, and the failure modes are quiet — acting on the wrong account, or swallowing a client
error so a failed write looks like it succeeded.

The handlers are closures over ``hass``, so they are reached by calling the real
``_async_register_services`` against a stub hass and capturing what it registers.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.hellofresh import _async_register_services
from custom_components.hellofresh.api import HelloFreshError
from custom_components.hellofresh.const import ATTR_CONFIG_ENTRY_ID, DOMAIN
from custom_components.hellofresh.models import HelloFreshNotImplementedError


class _Services:
    """Minimal stand-in for `hass.services` that records registrations."""

    def __init__(self) -> None:
        self.registered: dict[str, object] = {}

    def has_service(self, _domain: str, service: str) -> bool:
        return service in self.registered

    def async_register(self, _domain, service, handler, **_kw):
        self.registered[service] = handler


def _hass(coordinators: dict) -> SimpleNamespace:
    # `async_get_entry` is reached via the Repairs path when a write action is unsupported.
    # Coordinators live on each entry's runtime_data (HA's per-entry storage), which is
    # what `_get_target_coordinators` reads.
    entries = {
        entry_id: SimpleNamespace(
            entry_id=entry_id,
            title=f"HelloFresh {entry_id}",
            options={},
            domain=DOMAIN,
            runtime_data=coordinator,
        )
        for entry_id, coordinator in coordinators.items()
    }
    return SimpleNamespace(
        services=_Services(),
        config_entries=SimpleNamespace(
            async_entries=lambda _d: list(entries.values()),
            async_get_entry=entries.get,
        ),
    )


def _coordinator(entry_id: str, *, refresh_error: Exception | None = None) -> SimpleNamespace:
    calls: list[str] = []

    async def async_request_refresh() -> None:
        calls.append(entry_id)
        if refresh_error is not None:
            raise refresh_error

    return SimpleNamespace(
        async_request_refresh=async_request_refresh,
        calls=calls,
        config_entry=SimpleNamespace(entry_id=entry_id, title=f"HelloFresh {entry_id}"),
    )


def _register(hass) -> dict:
    # Loops are deliberately left open here and in _call: HA's autouse `verify_cleanup` fixture
    # touches the current event loop at teardown, and a closed one raises there.
    loop = asyncio.new_event_loop()
    loop.run_until_complete(_async_register_services(hass))
    return hass.services.registered


def _call(handler, data: dict, hass) -> object:
    service_call = SimpleNamespace(data=data, hass=hass)
    loop = asyncio.new_event_loop()
    return loop.run_until_complete(handler(service_call))


def test_single_account_needs_no_config_entry_id() -> None:
    """The common case: one account, so the service targets it implicitly."""
    coordinator = _coordinator("entry1")
    hass = _hass({"entry1": coordinator})
    handlers = _register(hass)

    _call(handlers["refresh_data"], {}, hass)

    assert coordinator.calls == ["entry1"]


def test_multiple_accounts_without_a_target_is_rejected() -> None:
    """Guessing which account to act on would silently write to the wrong one."""
    hass = _hass({"entry1": _coordinator("entry1"), "entry2": _coordinator("entry2")})
    handlers = _register(hass)

    with pytest.raises(HomeAssistantError, match="Specify config_entry_id"):
        _call(handlers["get_weeks"], {}, hass)


def test_explicit_config_entry_id_selects_that_account() -> None:
    """With several accounts, the caller's choice must be honoured exactly."""
    first, second = _coordinator("entry1"), _coordinator("entry2")
    hass = _hass({"entry1": first, "entry2": second})
    handlers = _register(hass)

    _call(handlers["refresh_data"], {ATTR_CONFIG_ENTRY_ID: "entry2"}, hass)

    assert second.calls == ["entry2"]
    assert first.calls == [], "the untargeted account must be left alone"


def test_unknown_config_entry_id_is_an_error_not_a_silent_noop() -> None:
    """A typo'd entry id must fail loudly rather than doing nothing."""
    hass = _hass({"entry1": _coordinator("entry1")})
    handlers = _register(hass)

    with pytest.raises(HomeAssistantError, match="config entry not found"):
        _call(handlers["refresh_data"], {ATTR_CONFIG_ENTRY_ID: "nope"}, hass)


def test_no_loaded_account_reports_that_specifically() -> None:
    """ "Nothing loaded" and "which one?" are different problems; don't conflate them."""
    hass = _hass({})
    handlers = _register(hass)

    with pytest.raises(HomeAssistantError, match="No HelloFresh account is currently loaded"):
        _call(handlers["refresh_data"], {}, hass)


def test_refresh_data_with_several_accounts_still_needs_a_target() -> None:
    """`refresh_data` does NOT fan out — ambiguity is refused for every service alike.

    `_for_each_coordinator` loops, which reads like a fan-out, but the list it loops over comes
    from `_get_target_coordinators`, which refuses to guess when several accounts are loaded.
    So the loop only ever has more than one element if a future caller passes one.
    """
    first, second = _coordinator("entry1"), _coordinator("entry2")
    hass = _hass({"entry1": first, "entry2": second})
    handlers = _register(hass)

    with pytest.raises(HomeAssistantError, match="Specify config_entry_id"):
        _call(handlers["refresh_data"], {}, hass)

    assert first.calls == [] and second.calls == []


def test_client_errors_surface_as_home_assistant_errors() -> None:
    """A failed write must reach the user, not vanish into the service call."""
    coordinator = _coordinator("entry1", refresh_error=HelloFreshError("upstream is down"))
    hass = _hass({"entry1": coordinator})
    handlers = _register(hass)

    with pytest.raises(HomeAssistantError, match="upstream is down"):
        _call(handlers["refresh_data"], {}, hass)


def test_unsupported_action_raises_and_opens_a_repairs_issue(monkeypatch) -> None:
    """An endpoint the integration can't build for this account is a Repairs case.

    It is distinct from a transient failure: retrying will never help, so the user needs the
    persistent issue as well as the immediate error. The issue registry needs real Home
    Assistant infrastructure, so only the fact that it is invoked is asserted here.
    """
    raised: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "custom_components.hellofresh.async_create_write_actions_issue",
        lambda _hass, entry_id, entry_title: raised.append((entry_id, entry_title)),
    )

    coordinator = _coordinator(
        "entry1", refresh_error=HelloFreshNotImplementedError("no verified endpoint")
    )
    hass = _hass({"entry1": coordinator})
    handlers = _register(hass)

    with pytest.raises(HomeAssistantError, match="no verified endpoint"):
        _call(handlers["refresh_data"], {}, hass)

    assert raised == [("entry1", "HelloFresh entry1")]


def test_registration_is_idempotent() -> None:
    """Setting up a second account must not re-register (or duplicate) the services."""
    hass = _hass({"entry1": _coordinator("entry1")})
    first = dict(_register(hass))
    second = dict(_register(hass))

    assert first.keys() == second.keys()
    assert len(second) == len(first)
