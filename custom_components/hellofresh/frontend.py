"""Frontend resource registration for the HelloFresh Lovelace cards.

The integration ships hand-written Lovelace cards — the unified ``www/hellofresh-card.js`` and
the classic per-feature cards (meal planner, Market, …) — that read their data on demand from
the integration's response-returning services such as ``hellofresh.get_weeks``. To make them
usable without the user manually adding a resource, the integration:

  1. serves the file from a stable URL via a static path, and
  2. registers that URL as a Lovelace module resource (storage mode) / appends it to the
     YAML-mode resource list, once per Home Assistant start.

Registration is best-effort: a failure here never blocks integration setup, since the
sensors/calendar/services work without the card.

It also puts a **HelloFresh** entry in the sidebar: a custom panel (``www/hellofresh-panel.js``)
that shows the HelloFresh card full screen, one per account whose "Show HelloFresh in the
sidebar" option is on, so the whole experience needs no dashboard at all.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_SHOW_SIDEBAR_PANEL, DEFAULT_SHOW_SIDEBAR_PANEL, DOMAIN

_LOGGER = logging.getLogger(__name__)

# All cards share the integration release version (manifest.json, bumped by the release
# workflow) as their cache-busting ?v= query, so every release automatically invalidates
# stale card JS without per-card manual bumps. The manifest is tiny and colocated, so a
# read at import keeps the URL constants module-level.
INTEGRATION_VERSION: str = json.loads(
    (Path(__file__).parent / "manifest.json").read_text(encoding="utf-8")
)["version"]

CARD_FILENAME = "hellofresh-meal-planner-card.js"
# The unified card (Overview / Menu / Market / Recipes / Account in one). Its view and logic
# modules (hellofresh-card-*.js) are imports of this file, not resources of their own.
UNIFIED_CARD_FILENAME = "hellofresh-card.js"
MARKET_CARD_FILENAME = "hellofresh-market-card.js"
FOOD_PROFILE_CARD_FILENAME = "hellofresh-food-profile-card.js"
SCHEDULE_CARD_FILENAME = "hellofresh-schedule-card.js"
SUBSCRIPTION_CARD_FILENAME = "hellofresh-subscription-card.js"
COST_CARD_FILENAME = "hellofresh-cost-card.js"
RECIPES_CARD_FILENAME = "hellofresh-recipes-card.js"
# The integration's www/ directory is served at /hellofresh/, so every asset in it
# (the card JS, the logo PNG, …) gets a stable URL without per-file registration.
WWW_URL_BASE = f"/{DOMAIN}"
# Public URL of the bundled HelloFresh logo, usable in picture/markdown cards.
LOGO_URL_PATH = f"{WWW_URL_BASE}/hellofresh-logo.png"

# Cards the integration ships and auto-registers: (filename, url_path, resource_url).
_CARDS = tuple(
    (
        filename,
        f"{WWW_URL_BASE}/{filename}",
        f"{WWW_URL_BASE}/{filename}?v={INTEGRATION_VERSION}",
    )
    for filename in (
        UNIFIED_CARD_FILENAME,
        CARD_FILENAME,
        MARKET_CARD_FILENAME,
        FOOD_PROFILE_CARD_FILENAME,
        SCHEDULE_CARD_FILENAME,
        SUBSCRIPTION_CARD_FILENAME,
        COST_CARD_FILENAME,
        RECIPES_CARD_FILENAME,
    )
)

_REGISTERED_KEY = f"{DOMAIN}_frontend_registered"

# The sidebar panel: a web component hosting the HelloFresh card full screen. It lives BESIDE,
# not under, the static /hellofresh/ path: a panel at /hellofresh would send a page reload
# there to the static file handler (a directory) instead of Home Assistant's app.
PANEL_FILENAME = "hellofresh-panel.js"
PANEL_ELEMENT = "hellofresh-panel"
PANEL_URL_PATH = "hellofresh-app"
PANEL_TITLE = "HelloFresh"
PANEL_ICON = "mdi:silverware-variant"
PANEL_MODULE_URL = f"{WWW_URL_BASE}/{PANEL_FILENAME}?v={INTEGRATION_VERSION}"
# Entries that are set up (entry id -> entry), and the panels we registered (url -> spec).
_PANEL_ENTRIES_KEY = f"{DOMAIN}_panel_entries"
_PANELS_KEY = f"{DOMAIN}_panels"


async def async_register_meal_planner_card(hass: HomeAssistant) -> None:
    """Serve the integration's www/ assets and register the card (idempotent)."""
    if hass.data.get(_REGISTERED_KEY):
        return
    hass.data[_REGISTERED_KEY] = True

    www_dir = Path(__file__).parent / "www"
    # `Path.is_file()` hits the disk, so it must not run on the event loop — this is a
    # coroutine, and HA blocks (and warns about) synchronous I/O here.
    card_present = await hass.async_add_executor_job((www_dir / CARD_FILENAME).is_file)
    if not card_present:
        _LOGGER.warning("HelloFresh meal-planner card file not found in %s", www_dir)
        return

    try:
        # Serve the whole www/ directory so the card JS and logo image are both reachable
        # under /hellofresh/ (e.g. /hellofresh/hellofresh-logo.png) from a single mount.
        # cache_headers=True is what makes the ?v= scheme work: browsers may cache each
        # file indefinitely because every upgrade registers a NEW URL (the ?v= stamp
        # changes), so staleness is busted by the URL, not by refetching on every load.
        await hass.http.async_register_static_paths(
            [StaticPathConfig(WWW_URL_BASE, str(www_dir), cache_headers=True)]
        )
    except Exception:  # noqa: BLE001 - static serving is best-effort, never block setup
        _LOGGER.exception("HelloFresh could not serve the frontend assets")
        hass.data[_REGISTERED_KEY] = False
        return

    await _async_register_lovelace_resources(hass)


async def _async_register_lovelace_resources(hass: HomeAssistant) -> None:
    """Add each card URL to the Lovelace resource list when in storage mode.

    In YAML-mode Lovelace the resources are user-managed, so we can only log guidance. In
    storage mode we add each resource through the resources collection if not already present.
    """
    lovelace = hass.data.get("lovelace")
    resources = getattr(lovelace, "resources", None)
    if resources is None:
        _LOGGER.debug("Lovelace resources unavailable; skipping auto-registration")
        return

    try:
        if not resources.loaded:
            await resources.async_load()
            resources.loaded = True
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Could not load Lovelace resources; cards must be added manually")
        return

    # YAML-mode resource stores don't support mutation (no store attribute).
    if getattr(resources, "store", None) is None:
        urls = ", ".join(resource_url for _, _, resource_url in _CARDS)
        _LOGGER.info(
            "HelloFresh cards are served at %s. Add them under Settings > Dashboards > "
            "Resources (or your YAML `resources:`) as JavaScript modules.",
            urls,
        )
        return

    existing = [
        (str(item.get("url", "")), item.get("id"))
        for item in resources.async_items()
        if isinstance(item, dict)
    ]
    for _filename, url_path, resource_url in _CARDS:
        matches = [(url, item_id) for url, item_id in existing if url.startswith(url_path)]
        # Migrate EVERY stale entry for this card, not just the first, and even when the
        # current URL is already present. Duplicate entries (e.g. a manually-added one from
        # a pre-auto-registration install alongside ours) made the browser load the module
        # twice — the second customElements.define throws, and if the stale URL loaded
        # first, the OLD card code kept winning despite the release.
        stale = [(url, item_id) for url, item_id in matches if url != resource_url]
        current_count = len(matches) - len(stale)
        for index, (stale_url, item_id) in enumerate(stale):
            try:
                if current_count == 0 and index == 0:
                    # No entry carries the new URL yet — repoint the first stale one.
                    await resources.async_update_item(item_id, {"url": resource_url})
                    _LOGGER.info(
                        "Updated HelloFresh card resource %s -> %s", stale_url, resource_url
                    )
                else:
                    # The new URL is already registered — any other entry is a duplicate.
                    await resources.async_delete_item(item_id)
                    _LOGGER.info(
                        "Removed duplicate HelloFresh card resource %s (current: %s)",
                        stale_url,
                        resource_url,
                    )
            except Exception:  # noqa: BLE001
                _LOGGER.exception("HelloFresh could not migrate card resource %s", resource_url)
        if matches:
            continue
        try:
            await resources.async_create_item({"res_type": "module", "url": resource_url})
            _LOGGER.info("Registered HelloFresh card resource at %s", resource_url)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("HelloFresh could not auto-register card resource %s", resource_url)


async def async_add_entry_panel(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Note a set-up account and bring the sidebar in line (called at entry setup)."""
    hass.data.setdefault(_PANEL_ENTRIES_KEY, {})[entry.entry_id] = entry
    await _async_sync_panels_safely(hass)


async def async_remove_entry_panel(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Forget an unloaded account and bring the sidebar in line (called at entry unload)."""
    hass.data.setdefault(_PANEL_ENTRIES_KEY, {}).pop(entry.entry_id, None)
    await _async_sync_panels_safely(hass)


async def _async_sync_panels_safely(hass: HomeAssistant) -> None:
    try:
        await _async_sync_panels(hass)
    except Exception:  # noqa: BLE001 - the sidebar must never fail an entry's setup or unload
        _LOGGER.exception("HelloFresh could not update its sidebar panel")


def _wanted_panels(
    entries: list[ConfigEntry],
) -> dict[str, tuple[str, tuple[tuple[str, str], ...]]]:
    """The sidebar panels these set-up entries call for: url path -> (title, config items).

    One account gets plain "HelloFresh" at /hellofresh-app and a card with no account pinned,
    so it shares its stored view and week with any HelloFresh card on a dashboard. With several
    accounts each panel is titled after its entry and pins its account.
    """
    several = len(entries) > 1
    wanted: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {}
    titles: set[str] = set()
    showing = [
        entry
        for entry in entries
        if entry.options.get(CONF_SHOW_SIDEBAR_PANEL, DEFAULT_SHOW_SIDEBAR_PANEL)
    ]
    for number, entry in enumerate(showing, start=1):
        url_path = PANEL_URL_PATH if number == 1 else f"{PANEL_URL_PATH}-{number}"
        title = (entry.title or PANEL_TITLE) if several else PANEL_TITLE
        if title in titles:
            title = f"{title} {number}"
        titles.add(title)
        config = (("config_entry_id", entry.entry_id),) if several else ()
        wanted[url_path] = (title, config)
    return wanted


async def _async_sync_panels(hass: HomeAssistant) -> None:
    """Register the panels the set-up entries want and remove the ones they no longer do."""
    active: dict[str, ConfigEntry] = hass.data.setdefault(_PANEL_ENTRIES_KEY, {})
    # Config-entry order, not setup order (entries set up concurrently), keeps each account's
    # sidebar address stable across restarts.
    listed = getattr(hass.config_entries, "async_entries", None)
    order = [entry.entry_id for entry in listed(DOMAIN)] if callable(listed) else []
    entries = sorted(
        active.values(),
        key=lambda entry: order.index(entry.entry_id) if entry.entry_id in order else len(order),
    )
    wanted = _wanted_panels(entries)
    registered: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = hass.data.setdefault(
        _PANELS_KEY, {}
    )
    for url_path, spec in list(registered.items()):
        if wanted.get(url_path) == spec:
            continue
        try:
            frontend.async_remove_panel(hass, url_path, warn_if_unknown=False)
        except Exception:  # noqa: BLE001 - the sidebar is best-effort
            _LOGGER.debug("Could not remove the HelloFresh sidebar panel %s", url_path)
        del registered[url_path]
    for url_path, spec in wanted.items():
        if url_path in registered:
            continue
        title, config = spec
        try:
            await panel_custom.async_register_panel(
                hass,
                frontend_url_path=url_path,
                webcomponent_name=PANEL_ELEMENT,
                sidebar_title=title,
                sidebar_icon=PANEL_ICON,
                module_url=PANEL_MODULE_URL,
                config=dict(config),
                require_admin=False,
            )
        except Exception:  # noqa: BLE001 - the sidebar is best-effort
            _LOGGER.exception("HelloFresh could not add its sidebar panel at /%s", url_path)
            continue
        registered[url_path] = spec
        _LOGGER.debug("Added the HelloFresh sidebar panel at /%s", url_path)


def async_get_frontend_diagnostics(hass: HomeAssistant) -> dict[str, object]:
    """Return card-version info for the diagnostics export.

    Compares the resource URLs this build expects (all stamped with the manifest version)
    against what Lovelace actually has registered, so a stale ?v= — a user running an old
    cached card — is visible straight from a diagnostics download.
    """
    registered: list[str] = []
    lovelace = hass.data.get("lovelace")
    resources = getattr(lovelace, "resources", None)
    if resources is not None:
        try:
            registered = [
                str(item.get("url", ""))
                for item in resources.async_items()
                if isinstance(item, dict)
                and str(item.get("url", "")).startswith(f"{WWW_URL_BASE}/")
            ]
        except Exception:  # noqa: BLE001 - diagnostics must never fail the export
            _LOGGER.debug("Could not read Lovelace resources for diagnostics")
    return {
        "integration_version": INTEGRATION_VERSION,
        "expected_resources": {
            filename: resource_url for filename, _url_path, resource_url in _CARDS
        },
        "registered_resources": registered,
        "sidebar_panels": sorted(hass.data.get(_PANELS_KEY, {})),
    }
