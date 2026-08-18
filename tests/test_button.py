"""Unit tests for the HelloFresh button platform.

``button.py`` ships in ``PLATFORMS`` and creates a user-visible entity, but no test
imported it -- it sat at 0% coverage while every other platform was exercised. The
button's whole contract is small (one entity, one press action, a pinned entity_id),
which is exactly why an untested regression here would be easy to miss: nothing else
in the suite would go red.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from custom_components.hellofresh.button import (
    BUTTONS,
    HelloFreshButton,
    async_setup_entry,
)
from custom_components.hellofresh.const import DOMAIN


def _coordinator() -> SimpleNamespace:
    """A minimal coordinator stand-in matching the entity base class's needs."""
    return SimpleNamespace(
        data=SimpleNamespace(),
        config_entry=SimpleNamespace(entry_id="entry-1", title="HelloFresh US"),
        last_update_success=True,
        async_add_listener=lambda *_a, **_kw: (lambda: None),
    )


def _button(key: str = "refresh_data") -> HelloFreshButton:
    description = next(item for item in BUTTONS if item.key == key)
    return HelloFreshButton(_coordinator(), description)


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def test_refresh_button_is_the_declared_button_set() -> None:
    """One diagnostic refresh button, enabled by default."""
    assert [description.key for description in BUTTONS] == ["refresh_data"]
    description = BUTTONS[0]
    assert description.entity_registry_enabled_default is True
    assert description.translation_key == "refresh_data"


def test_entity_id_is_pinned_to_the_key_not_the_display_name() -> None:
    """Same stability contract the sensors and switches hold.

    With ``has_entity_name`` the id would otherwise derive from the translated display
    name, so a rename would silently move the entity out from under any dashboard or
    automation referencing it.
    """
    assert _button().entity_id == "button.hellofresh_us_refresh_data"


def test_unique_id_is_scoped_to_the_config_entry() -> None:
    """Two accounts must not collide on a single registry entry."""
    assert _button().unique_id == "entry-1_refresh_data"


def test_press_requests_a_coordinator_refresh() -> None:
    """The press must go through ``async_request_refresh``.

    Calling ``async_refresh`` instead would bypass the coordinator's debounce, so a
    button-mashing user would fan out concurrent polls against HelloFresh.
    """
    button = _button()
    calls: list[str] = []

    async def _request_refresh() -> None:
        calls.append("refresh")

    button.coordinator.async_request_refresh = _request_refresh
    _run(button.async_press())
    assert calls == ["refresh"]


def test_press_ignores_unknown_keys() -> None:
    """A future button description must not accidentally trigger a refresh.

    ``async_press`` dispatches on ``description.key``; if that guard were dropped, every
    new button added to ``BUTTONS`` would silently inherit refresh behaviour.
    """
    from homeassistant.components.button import ButtonEntityDescription

    button = HelloFreshButton(_coordinator(), ButtonEntityDescription(key="not_refresh"))
    calls: list[str] = []

    async def _request_refresh() -> None:
        calls.append("refresh")

    button.coordinator.async_request_refresh = _request_refresh
    _run(button.async_press())
    assert calls == []


def test_setup_entry_adds_one_button_per_description() -> None:
    """Setup reads the coordinator from ``hass.data`` under the entry id."""
    coordinator = _coordinator()
    hass = SimpleNamespace(data={DOMAIN: {"entry-1": coordinator}})
    entry = SimpleNamespace(entry_id="entry-1", title="HelloFresh US")
    added: list[HelloFreshButton] = []

    _run(async_setup_entry(hass, entry, lambda entities: added.extend(entities)))

    assert [button.entity_description.key for button in added] == ["refresh_data"]
    assert added[0].coordinator is coordinator
