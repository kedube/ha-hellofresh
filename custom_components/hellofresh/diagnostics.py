"""Diagnostics support for HelloFresh."""

from __future__ import annotations

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_USERNAME,
    DOMAIN,
)

TO_REDACT = {
    CONF_ACCESS_TOKEN,
    # Long-lived credential (~60 days): refreshes access tokens, so it must never leak
    # through a shared diagnostics export.
    CONF_REFRESH_TOKEN,
    # Account login credentials now live in entry.data — never expose them.
    CONF_USERNAME,
    CONF_PASSWORD,
    "account_id",
    "subscription_id",
    "delivery_address",
    "tracking_number",
    "tracking_url",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
) -> dict:
    """Return diagnostics for a config entry."""
    coordinator = hass.data[DOMAIN].get(config_entry.entry_id)
    data = coordinator.data if coordinator is not None and coordinator.data else None
    client = getattr(coordinator, "client", None)

    # Non-sensitive token timing — surfaced so a refresh-token rotation/persist problem is
    # diagnosable from a redacted export. These are timestamps/durations, never the tokens.
    token_health: dict[str, object] = {}
    if client is not None:
        token_expires_at = getattr(client, "token_expires_at", None)
        refresh_expires_at = getattr(client, "refresh_token_expires_at", None)
        token_health = {
            "access_token_expires_at": (token_expires_at.isoformat() if token_expires_at else None),
            "refresh_token_expires_at": (
                refresh_expires_at.isoformat() if refresh_expires_at else None
            ),
            "has_refresh_token": bool(getattr(client, "_refresh_token", None)),
            "has_credentials": bool(getattr(client, "_has_credentials", False)),
        }

    diagnostics = {
        "config_entry": {
            "entry_id": config_entry.entry_id,
            "title": config_entry.title,
            "data": dict(config_entry.data),
            "options": dict(config_entry.options),
        },
        "token_health": token_health,
        "runtime": {
            "account_id": getattr(data, "account_id", None),
            "subscription_id": getattr(data, "subscription_id", None),
            "locale": getattr(data, "locale", None),
            "account_data_available": getattr(data, "account_data_available", None),
            "capabilities": getattr(data, "capabilities", None).as_dict() if data else {},
            "debug_trace": getattr(data, "debug_trace", {}) if data else {},
            "subscription_count": len(getattr(data, "subscriptions", [])) if data else 0,
            "order_count": len(getattr(data, "orders", [])) if data else 0,
            "week_count": len(getattr(data, "weeks", [])) if data else 0,
            "public_menu_week_count": len(getattr(data, "public_menu_weeks", [])) if data else 0,
            "subscriptions": getattr(data, "serialized_subscriptions", []),
            "orders": getattr(data, "serialized_orders", []),
            "weeks": getattr(data, "serialized_weeks", []),
            "weeks_needing_selection": getattr(data, "serialized_weeks_needing_selection", []),
            "public_menu_weeks": getattr(data, "serialized_public_menu_weeks", []),
        },
    }

    return async_redact_data(diagnostics, TO_REDACT)
