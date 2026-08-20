"""Tests for Repairs issue creation and the data-quality suppression option."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from custom_components.hellofresh import issues
from custom_components.hellofresh.const import CONF_SHOW_DATA_QUALITY_ISSUES


def _hass_with_options(options: dict) -> SimpleNamespace:
    entry = SimpleNamespace(options=options)
    return SimpleNamespace(config_entries=SimpleNamespace(async_get_entry=lambda entry_id: entry))


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
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_get_entry=lambda entry_id: None))
    with patch.object(issues.ir, "async_create_issue") as create:
        issues.async_create_write_actions_issue(hass, "entry-1", "HelloFresh (US)")
    assert create.call_count == 1


def test_delete_data_quality_issues_covers_every_issue_key() -> None:
    """The bulk delete must address all four issue ids for the entry."""
    hass = _hass_with_options({})
    with patch.object(issues.ir, "async_delete_issue") as delete:
        issues.async_delete_data_quality_issues(hass, "entry-1")
    deleted = {call.args[2] for call in delete.call_args_list}
    assert deleted == {
        f"{key}_entry-1"
        for key in (
            issues.ISSUE_ACCOUNT_DATA_UNAVAILABLE,
            issues.ISSUE_ACCOUNT_MENU_FALLBACK,
            issues.ISSUE_PAYLOAD_SHAPE_CHANGED,
            issues.ISSUE_WRITE_ACTIONS_UNAVAILABLE,
        )
    }


def test_cleanup_stale_issues_removes_only_orphaned_entries() -> None:
    """Issues from removed config entries are swept; live entries and other domains stay."""
    live = f"{issues.ISSUE_ACCOUNT_MENU_FALLBACK}_LIVEENTRYID"
    orphan = f"{issues.ISSUE_ACCOUNT_MENU_FALLBACK}_DEADENTRYID"
    orphan_write = f"{issues.ISSUE_WRITE_ACTIONS_UNAVAILABLE}_DEADENTRYID"
    registry = SimpleNamespace(
        issues={
            (issues.DOMAIN, live): object(),
            (issues.DOMAIN, orphan): object(),
            (issues.DOMAIN, orphan_write): object(),
            ("other_domain", orphan): object(),
        }
    )
    hass = SimpleNamespace()
    with (
        patch.object(issues.ir, "async_get", return_value=registry),
        patch.object(issues.ir, "async_delete_issue") as delete,
    ):
        issues.async_cleanup_stale_issues(hass, {"LIVEENTRYID"})
    deleted = {(call.args[1], call.args[2]) for call in delete.call_args_list}
    assert deleted == {(issues.DOMAIN, orphan), (issues.DOMAIN, orphan_write)}


def test_write_actions_issue_delete_targets_same_id() -> None:
    """The new delete helper must address the exact id the create helper uses."""
    hass = _hass_with_options({})
    with patch.object(issues.ir, "async_create_issue") as create:
        issues.async_create_write_actions_issue(hass, "entry-1", "HelloFresh (US)")
    created_id = create.call_args.args[2]
    with patch.object(issues.ir, "async_delete_issue") as delete:
        issues.async_delete_write_actions_issue(hass, "entry-1")
    assert delete.call_args.args[2] == created_id
