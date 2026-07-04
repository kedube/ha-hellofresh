"""Frontend resource registration for the HelloFresh meal-planner Lovelace card.

The integration ships a hand-written Lovelace card (``www/hellofresh-meal-planner-card.js``)
that reads per-week recipes on demand from the response-returning ``hellofresh.get_weeks``
service. To make it usable without the user manually adding a resource, the integration:

  1. serves the file from a stable URL via a static path, and
  2. registers that URL as a Lovelace module resource (storage mode) / appends it to the
     YAML-mode resource list, once per Home Assistant start.

Registration is best-effort: a failure here never blocks integration setup, since the
sensors/calendar/services work without the card.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CARD_FILENAME = "hellofresh-meal-planner-card.js"
# Bump when the card file changes so HA/browsers cache-bust the resource URL.
CARD_VERSION = "0.28.1"
MARKET_CARD_FILENAME = "hellofresh-market-card.js"
MARKET_CARD_VERSION = "0.9.2"
FOOD_PROFILE_CARD_FILENAME = "hellofresh-food-profile-card.js"
FOOD_PROFILE_CARD_VERSION = "0.4.1"
SCHEDULE_CARD_FILENAME = "hellofresh-schedule-card.js"
SCHEDULE_CARD_VERSION = "0.1.0"
# The integration's www/ directory is served at /hellofresh/, so every asset in it
# (the card JS, the logo PNG, …) gets a stable URL without per-file registration.
WWW_URL_BASE = f"/{DOMAIN}"
CARD_URL_PATH = f"{WWW_URL_BASE}/{CARD_FILENAME}"
CARD_RESOURCE_URL = f"{CARD_URL_PATH}?v={CARD_VERSION}"
MARKET_CARD_URL_PATH = f"{WWW_URL_BASE}/{MARKET_CARD_FILENAME}"
MARKET_CARD_RESOURCE_URL = f"{MARKET_CARD_URL_PATH}?v={MARKET_CARD_VERSION}"
FOOD_PROFILE_CARD_URL_PATH = f"{WWW_URL_BASE}/{FOOD_PROFILE_CARD_FILENAME}"
FOOD_PROFILE_CARD_RESOURCE_URL = f"{FOOD_PROFILE_CARD_URL_PATH}?v={FOOD_PROFILE_CARD_VERSION}"
SCHEDULE_CARD_URL_PATH = f"{WWW_URL_BASE}/{SCHEDULE_CARD_FILENAME}"
SCHEDULE_CARD_RESOURCE_URL = f"{SCHEDULE_CARD_URL_PATH}?v={SCHEDULE_CARD_VERSION}"
# Public URL of the bundled HelloFresh logo, usable in picture/markdown cards.
LOGO_URL_PATH = f"{WWW_URL_BASE}/hellofresh-logo.png"

# Cards the integration ships and auto-registers: (filename, url_path, resource_url).
_CARDS = (
    (CARD_FILENAME, CARD_URL_PATH, CARD_RESOURCE_URL),
    (MARKET_CARD_FILENAME, MARKET_CARD_URL_PATH, MARKET_CARD_RESOURCE_URL),
    (FOOD_PROFILE_CARD_FILENAME, FOOD_PROFILE_CARD_URL_PATH, FOOD_PROFILE_CARD_RESOURCE_URL),
    (SCHEDULE_CARD_FILENAME, SCHEDULE_CARD_URL_PATH, SCHEDULE_CARD_RESOURCE_URL),
)

_REGISTERED_KEY = f"{DOMAIN}_frontend_registered"


async def async_register_meal_planner_card(hass: HomeAssistant) -> None:
    """Serve the integration's www/ assets and register the card (idempotent)."""
    if hass.data.get(_REGISTERED_KEY):
        return
    hass.data[_REGISTERED_KEY] = True

    www_dir = Path(__file__).parent / "www"
    if not (www_dir / CARD_FILENAME).is_file():
        _LOGGER.warning("HelloFresh meal-planner card file not found in %s", www_dir)
        return

    try:
        # Serve the whole www/ directory so the card JS and logo image are both reachable
        # under /hellofresh/ (e.g. /hellofresh/hellofresh-logo.png) from a single mount.
        await hass.http.async_register_static_paths(
            [StaticPathConfig(WWW_URL_BASE, str(www_dir), cache_headers=False)]
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
        str(item.get("url", "")) for item in resources.async_items() if isinstance(item, dict)
    ]
    for _filename, url_path, resource_url in _CARDS:
        if any(url.startswith(url_path) for url in existing):
            continue
        try:
            await resources.async_create_item({"res_type": "module", "url": resource_url})
            _LOGGER.info("Registered HelloFresh card resource at %s", resource_url)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("HelloFresh could not auto-register card resource %s", resource_url)
