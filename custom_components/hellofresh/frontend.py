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
CARD_VERSION = "0.4.0"
# The integration's www/ directory is served at /hellofresh/, so every asset in it
# (the card JS, the logo PNG, …) gets a stable URL without per-file registration.
WWW_URL_BASE = f"/{DOMAIN}"
CARD_URL_PATH = f"{WWW_URL_BASE}/{CARD_FILENAME}"
CARD_RESOURCE_URL = f"{CARD_URL_PATH}?v={CARD_VERSION}"
# Public URL of the bundled HelloFresh logo, usable in picture/markdown cards.
LOGO_URL_PATH = f"{WWW_URL_BASE}/hellofresh-logo.png"

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

    await _async_register_lovelace_resource(hass)


async def _async_register_lovelace_resource(hass: HomeAssistant) -> None:
    """Add the card URL to the Lovelace resource list when in storage mode.

    In YAML-mode Lovelace the resources are user-managed, so we can only log guidance. In
    storage mode we add the resource through the resources collection if it isn't present.
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
        _LOGGER.debug("Could not load Lovelace resources; card must be added manually")
        return

    # YAML-mode resource stores don't support mutation (no store attribute).
    if getattr(resources, "store", None) is None:
        _LOGGER.info(
            "HelloFresh meal-planner card is served at %s. Add it under Settings > "
            "Dashboards > Resources (or your YAML `resources:`) as a JavaScript module.",
            CARD_RESOURCE_URL,
        )
        return

    already = any(
        isinstance(item, dict) and str(item.get("url", "")).startswith(CARD_URL_PATH)
        for item in resources.async_items()
    )
    if already:
        return

    try:
        await resources.async_create_item({"res_type": "module", "url": CARD_RESOURCE_URL})
        _LOGGER.info("Registered HelloFresh meal-planner card resource at %s", CARD_RESOURCE_URL)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("HelloFresh could not auto-register the card Lovelace resource")
