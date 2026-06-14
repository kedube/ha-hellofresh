"""Repairs issue helpers for HelloFresh."""

from __future__ import annotations

from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

ISSUE_ACCOUNT_DATA_UNAVAILABLE = "account_data_unavailable"
ISSUE_ACCOUNT_MENU_FALLBACK = "account_menu_fallback"
ISSUE_PAYLOAD_SHAPE_CHANGED = "payload_shape_changed"
ISSUE_WRITE_ACTIONS_UNAVAILABLE = "write_actions_unavailable"


def _issue_id(issue_key: str, entry_id: str) -> str:
    """Build a stable issue id for a config entry."""
    return f"{issue_key}_{entry_id}"


def async_create_account_data_issue(hass, entry_id: str, entry_title: str) -> None:
    """Create a warning when only public fallback data is available."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(ISSUE_ACCOUNT_DATA_UNAVAILABLE, entry_id),
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_ACCOUNT_DATA_UNAVAILABLE,
        translation_placeholders={"entry_title": entry_title},
        learn_more_url="https://github.com/kedube/ha-hellofresh#current-scope",
    )


def async_delete_account_data_issue(hass, entry_id: str) -> None:
    """Delete the fallback-only account data issue."""
    ir.async_delete_issue(hass, DOMAIN, _issue_id(ISSUE_ACCOUNT_DATA_UNAVAILABLE, entry_id))


def async_create_account_menu_fallback_issue(hass, entry_id: str, entry_title: str) -> None:
    """Create a warning when the account menu API is unavailable."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(ISSUE_ACCOUNT_MENU_FALLBACK, entry_id),
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_ACCOUNT_MENU_FALLBACK,
        translation_placeholders={"entry_title": entry_title},
        learn_more_url="https://github.com/kedube/ha-hellofresh#current-scope",
    )


def async_delete_account_menu_fallback_issue(hass, entry_id: str) -> None:
    """Delete the menu fallback issue."""
    ir.async_delete_issue(hass, DOMAIN, _issue_id(ISSUE_ACCOUNT_MENU_FALLBACK, entry_id))


def async_create_payload_shape_changed_issue(hass, entry_id: str, entry_title: str) -> None:
    """Create a warning when account payloads no longer match known shapes."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(ISSUE_PAYLOAD_SHAPE_CHANGED, entry_id),
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_PAYLOAD_SHAPE_CHANGED,
        translation_placeholders={"entry_title": entry_title},
        learn_more_url="https://github.com/kedube/ha-hellofresh/issues",
    )


def async_delete_payload_shape_changed_issue(hass, entry_id: str) -> None:
    """Delete the payload-shape issue."""
    ir.async_delete_issue(hass, DOMAIN, _issue_id(ISSUE_PAYLOAD_SHAPE_CHANGED, entry_id))


def async_create_write_actions_issue(hass, entry_id: str, entry_title: str) -> None:
    """Create a warning when a user triggers unsupported write actions."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(ISSUE_WRITE_ACTIONS_UNAVAILABLE, entry_id),
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_WRITE_ACTIONS_UNAVAILABLE,
        translation_placeholders={"entry_title": entry_title},
        learn_more_url="https://github.com/kedube/ha-hellofresh#current-scope",
    )
