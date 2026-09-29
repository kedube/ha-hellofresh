"""The HelloFresh sidebar panel (``frontend.async_add_entry_panel`` / ``async_remove_entry_panel``).

Every set-up account whose "Show HelloFresh in the sidebar" option is on gets a sidebar entry
that opens the HelloFresh card full screen; turning the option off, or unloading the entry,
takes it away. One account gets a plain "HelloFresh" entry with no account pinned (so it shares
its stored view and week with a dashboard card); with several, each entry pins its account.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.hellofresh import frontend as frontend_module
from custom_components.hellofresh.const import CONF_SHOW_SIDEBAR_PANEL, DOMAIN
from custom_components.hellofresh.frontend import (
    INTEGRATION_VERSION,
    PANEL_ELEMENT,
    PANEL_ICON,
    PANEL_URL_PATH,
    WWW_URL_BASE,
    async_add_entry_panel,
    async_get_frontend_diagnostics,
    async_remove_entry_panel,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def _entry(entry_id: str, title: str = "HelloFresh (US)", **options) -> SimpleNamespace:
    return SimpleNamespace(entry_id=entry_id, title=title, options=options)


class _Sidebar:
    """Stands in for Home Assistant's panel registry, recording every change."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, fail: bool = False) -> None:
        self.panels: dict[str, dict] = {}
        self.log: list[tuple[str, str]] = []
        self.fail = fail

        async def register(hass, **kwargs):
            if self.fail:
                raise ValueError("Overwriting panel")
            self.panels[kwargs["frontend_url_path"]] = kwargs
            self.log.append(("add", kwargs["frontend_url_path"]))

        def remove(hass, url_path, *, warn_if_unknown=True):
            self.panels.pop(url_path, None)
            self.log.append(("remove", url_path))

        monkeypatch.setattr(frontend_module.panel_custom, "async_register_panel", register)
        monkeypatch.setattr(frontend_module.frontend, "async_remove_panel", remove)


def _hass(*entries: SimpleNamespace) -> SimpleNamespace:
    listed = list(entries)
    return SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_entries=lambda domain: listed if domain == DOMAIN else []
        ),
    )


def test_one_account_gets_a_plain_hellofresh_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    sidebar = _Sidebar(monkeypatch)
    entry = _entry("a")
    _run(async_add_entry_panel(_hass(entry), entry))

    assert list(sidebar.panels) == [PANEL_URL_PATH]
    panel = sidebar.panels[PANEL_URL_PATH]
    assert panel["sidebar_title"] == "HelloFresh"
    assert panel["sidebar_icon"] == PANEL_ICON == "mdi:silverware-variant"
    assert panel["webcomponent_name"] == PANEL_ELEMENT
    # Stamped like the cards, so an upgrade never serves a cached old panel.
    assert panel["module_url"] == f"{WWW_URL_BASE}/hellofresh-panel.js?v={INTEGRATION_VERSION}"
    assert panel["config"] == {}  # nothing pinned: it shares state with a dashboard card
    assert panel["require_admin"] is False


def test_the_panel_does_not_sit_under_the_static_path() -> None:
    """A panel at /hellofresh would send a page reload there to the static file handler that
    serves /hellofresh/<card>.js (a directory: an error) instead of Home Assistant's app."""
    assert f"/{PANEL_URL_PATH}" != WWW_URL_BASE
    assert not f"/{PANEL_URL_PATH}/".startswith(f"{WWW_URL_BASE}/")


def test_the_option_turns_the_entry_off_and_on(monkeypatch: pytest.MonkeyPatch) -> None:
    sidebar = _Sidebar(monkeypatch)
    off = _entry("a", **{CONF_SHOW_SIDEBAR_PANEL: False})
    hass = _hass(off)
    _run(async_add_entry_panel(hass, off))
    assert sidebar.panels == {}

    # Changing an option reloads the entry: unload, then set up with the new options.
    on = _entry("a", **{CONF_SHOW_SIDEBAR_PANEL: True})
    _run(async_remove_entry_panel(hass, off))
    _run(async_add_entry_panel(hass, on))
    assert list(sidebar.panels) == [PANEL_URL_PATH]

    _run(async_remove_entry_panel(hass, on))
    _run(async_add_entry_panel(hass, off))
    assert sidebar.panels == {}


def test_unloading_takes_the_entry_away(monkeypatch: pytest.MonkeyPatch) -> None:
    sidebar = _Sidebar(monkeypatch)
    entry = _entry("a")
    hass = _hass(entry)
    _run(async_add_entry_panel(hass, entry))
    _run(async_remove_entry_panel(hass, entry))
    assert sidebar.panels == {}
    assert sidebar.log == [("add", PANEL_URL_PATH), ("remove", PANEL_URL_PATH)]


def test_several_accounts_each_get_an_entry_pinned_to_their_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sidebar = _Sidebar(monkeypatch)
    first = _entry("a", "HelloFresh (US)")
    second = _entry("b", "HelloFresh (US)")
    third = _entry("c", "Mum's HelloFresh")
    hass = _hass(first, second, third)
    # Entries finish setting up in any order; addresses follow the config-entry order.
    for entry in (second, first, third):
        _run(async_add_entry_panel(hass, entry))

    assert {url: (p["sidebar_title"], p["config"]) for url, p in sidebar.panels.items()} == {
        PANEL_URL_PATH: ("HelloFresh (US)", {"config_entry_id": "a"}),
        f"{PANEL_URL_PATH}-2": ("HelloFresh (US) 2", {"config_entry_id": "b"}),
        f"{PANEL_URL_PATH}-3": ("Mum's HelloFresh", {"config_entry_id": "c"}),
    }


def test_a_second_account_turns_the_plain_entry_into_a_pinned_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sidebar = _Sidebar(monkeypatch)
    first, second = _entry("a", "Home"), _entry("b", "Cabin")
    hass = _hass(first, second)
    _run(async_add_entry_panel(hass, first))
    assert sidebar.panels[PANEL_URL_PATH]["config"] == {}

    _run(async_add_entry_panel(hass, second))
    assert sidebar.panels[PANEL_URL_PATH]["sidebar_title"] == "Home"
    assert sidebar.panels[PANEL_URL_PATH]["config"] == {"config_entry_id": "a"}
    assert sidebar.panels[f"{PANEL_URL_PATH}-2"]["config"] == {"config_entry_id": "b"}

    # Back to one account: back to the plain entry.
    _run(async_remove_entry_panel(hass, second))
    assert list(sidebar.panels) == [PANEL_URL_PATH]
    assert sidebar.panels[PANEL_URL_PATH]["sidebar_title"] == "HelloFresh"
    assert sidebar.panels[PANEL_URL_PATH]["config"] == {}


def test_an_account_without_the_panel_still_counts_as_another_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With two accounts the card must be told which one to show, even if only one of them
    has a sidebar entry."""
    sidebar = _Sidebar(monkeypatch)
    shown, hidden = _entry("a", "Home"), _entry("b", "Cabin", **{CONF_SHOW_SIDEBAR_PANEL: False})
    hass = _hass(shown, hidden)
    _run(async_add_entry_panel(hass, shown))
    _run(async_add_entry_panel(hass, hidden))
    assert list(sidebar.panels) == [PANEL_URL_PATH]
    assert sidebar.panels[PANEL_URL_PATH]["config"] == {"config_entry_id": "a"}


def test_a_sidebar_failure_never_fails_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    sidebar = _Sidebar(monkeypatch, fail=True)
    entry = _entry("a")
    hass = _hass(entry)
    _run(async_add_entry_panel(hass, entry))  # logged, not raised
    assert sidebar.panels == {}
    # Nothing was recorded as registered, so a later sync tries again.
    sidebar.fail = False
    _run(async_add_entry_panel(hass, entry))
    assert list(sidebar.panels) == [PANEL_URL_PATH]


def test_diagnostics_list_the_sidebar_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    _Sidebar(monkeypatch)
    entry = _entry("a")
    hass = _hass(entry)
    _run(async_add_entry_panel(hass, entry))
    assert async_get_frontend_diagnostics(hass)["sidebar_panels"] == [PANEL_URL_PATH]
