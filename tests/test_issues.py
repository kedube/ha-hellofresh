"""Tests for Repairs issue creation and the data-quality suppression option."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from custom_components.hellofresh import issues
from custom_components.hellofresh.const import CONF_SHOW_DATA_QUALITY_ISSUES


def _hass_with_options(options: dict) -> SimpleNamespace:
    entry = SimpleNamespace(options=options)
    return SimpleNamespace(
        config_entries=SimpleNamespace(async_get_entry=lambda entry_id: entry)
    )


def test_write_actions_issue_raised_by_default() -> None:
    hass = _hass_with_options({})
    with patch.object(issues.ir, "async_create_issue") as create:
        issues.async_create_write_actions_issue(hass, "entry-1", "HelloFresh (US)")
    assert create.call_count == 1


def test_write_actions_issue_suppressed_when_option_off() -> None:
    """Turning off show_data_quality_issues silences the write-actions warning."""
    hass = _hass_with_options({CONF_SHOW_DATA_QUALITY_ISSUES: False})
    with patch.object(issues.ir, "async_create_issue") as create:
        issues.async_create_write_actions_issue(hass, "entry-1", "HelloFresh (US)")
    create.assert_not_called()


def test_write_actions_issue_raised_when_entry_lookup_fails() -> None:
    """A missing config entry must not swallow the warning."""
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_get_entry=lambda entry_id: None)
    )
    with patch.object(issues.ir, "async_create_issue") as create:
        issues.async_create_write_actions_issue(hass, "entry-1", "HelloFresh (US)")
    assert create.call_count == 1


def test_write_actions_issue_delete_targets_same_id() -> None:
    """The new delete helper must address the exact id the create helper uses."""
    hass = _hass_with_options({})
    with patch.object(issues.ir, "async_create_issue") as create:
        issues.async_create_write_actions_issue(hass, "entry-1", "HelloFresh (US)")
    created_id = create.call_args.args[2]
    with patch.object(issues.ir, "async_delete_issue") as delete:
        issues.async_delete_write_actions_issue(hass, "entry-1")
    assert delete.call_args.args[2] == created_id
