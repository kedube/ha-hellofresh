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

import json
import logging
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# All cards share the integration release version (manifest.json, bumped by the release
# workflow) as their cache-busting ?v= query, so every release automatically invalidates
# stale card JS without per-card manual bumps. The manifest is tiny and colocated, so a
# read at import keeps the URL constants module-level.
INTEGRATION_VERSION: str = json.loads(
    (Path(__file__).parent / "manifest.json").read_text(encoding="utf-8")
)["version"]

CARD_FILENAME = "hellofresh-meal-planner-card.js"
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
                _LOGGER.exception(
                    "HelloFresh could not migrate card resource %s", resource_url
                )
        if matches:
            continue
        try:
            await resources.async_create_item({"res_type": "module", "url": resource_url})
            _LOGGER.info("Registered HelloFresh card resource at %s", resource_url)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("HelloFresh could not auto-register card resource %s", resource_url)


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
    }
