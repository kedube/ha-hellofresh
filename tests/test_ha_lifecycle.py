"""The integration's lifecycle through Home Assistant's real config-entry machinery.

Every other test drives the integration with fakes. These load it the way Home Assistant does:
manifest dependencies (frontend, http, panel_custom), every platform, the entity and device
registries, services, the options, reconfigure and reauth flows, and unload. Only HelloFresh
itself is stubbed: the account fetch and the config flow's credential check.

A Home Assistant release that changes a contract the integration relies on fails here, and so
does any deprecation it reports against the integration -- such as the update listener that
2026.12 stops accepting next to the flows' own reloads.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.components.frontend import DATA_PANELS
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, section
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hellofresh.api import (
    HelloFreshAccountData,
    HelloFreshAuthError,
    HelloFreshOrder,
    HelloFreshSubscription,
    HelloFreshWeek,
)
from custom_components.hellofresh.client import HelloFreshClient
from custom_components.hellofresh.config_flow import HelloFreshConfigFlow
from custom_components.hellofresh.const import DOMAIN

# What Home Assistant's frame helper logs when it reports an integration for deprecated use.
DEPRECATION_MARKERS = ("Detected that custom integration", "will stop working in Home Assistant")


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load custom_components/hellofresh."""


@pytest.fixture(autouse=True)
def account_fetch():
    """Stub every HelloFresh request setup makes; the fetch's await count counts (re)loads.

    tests/conftest.py already keeps requests on aiohttp, which pytest-socket blocks; these
    stubs keep setup from trying at all (token refresh and the two selects' options).
    """
    week_delivery = date.today() + timedelta(days=5)
    data = HelloFreshAccountData(
        weeks=[
            HelloFreshWeek(
                week_id="2026-W46",
                display_name="Week 46",
                subscription_id="sub-1",
                delivery_date=week_delivery,
                selection_deadline=datetime.now(UTC) + timedelta(days=2),
                meals_required=3,
                meals_selected=1,
            )
        ],
        orders=[
            HelloFreshOrder(
                order_id="ord-1",
                week_id="2026-W46",
                status="scheduled",
                subscription_id="sub-1",
                delivery_date=week_delivery,
                total_price=97.5,
                currency="USD",
            )
        ],
        subscriptions=[
            HelloFreshSubscription(
                subscription_id="sub-1",
                display_name="Classic Plan",
                plan_name="Classic Box",
                status="ACTIVE",
                servings=2,
            )
        ],
    ).finalize()
    with (
        patch.object(
            HelloFreshClient, "async_get_account_data", AsyncMock(return_value=data)
        ) as fetch,
        patch.object(HelloFreshClient, "async_ensure_token_fresh", AsyncMock()),
        patch.object(HelloFreshClient, "async_list_plan_options", AsyncMock(return_value=[])),
        patch.object(HelloFreshClient, "async_get_delivery_options", AsyncMock(return_value=[])),
        patch.object(HelloFreshConfigFlow, "_async_validate", side_effect=_validate),
    ):
        yield fetch


async def _validate(username, password, country, errors, **kwargs):
    """The config flow's credential check, succeeding for the entry's own account."""
    tokens = kwargs.get("tokens")
    if tokens is not None:
        tokens.update({"access_token": "fresh-access", "refresh_token": "fresh-refresh"})
    return {"account_id": "acct-1"}


@pytest.fixture(autouse=True)
def _no_deprecations(caplog: pytest.LogCaptureFixture):
    """Fail any test during which Home Assistant reports the integration for deprecated use."""
    yield
    reports = [
        record.getMessage()
        for record in caplog.records
        if any(marker in record.getMessage() for marker in DEPRECATION_MARKERS)
    ]
    assert not reports, "\n".join(reports)


async def _load(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="HelloFresh (US)",
        unique_id="us::acct-1",
        data={"country": "us", "username": "cook@example.com", "password": "made-up"},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _form_defaults(schema) -> dict:
    """An options form submitted exactly as rendered: every field at its default."""
    values: dict = {}
    for marker, validator in schema.schema.items():
        if isinstance(validator, section):
            values[str(marker)] = _form_defaults(validator.schema)
        elif callable(default := getattr(marker, "default", None)) and default() is not None:
            values[str(marker)] = default()
    return values


async def _save_options(hass: HomeAssistant, entry: MockConfigEntry, **changes) -> None:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**_form_defaults(result["data_schema"]), **changes}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


async def test_loads_entities_services_and_panel_then_unloads(hass: HomeAssistant) -> None:
    entry = await _load(hass)
    assert entry.state is ConfigEntryState.LOADED

    assert dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    enabled = [e.entity_id for e in entities if e.disabled_by is None]
    assert enabled
    assert [entity_id for entity_id in enabled if hass.states.get(entity_id) is None] == []

    summary = await hass.services.async_call(
        DOMAIN, "get_account_summary", {}, blocking=True, return_response=True
    )
    assert summary
    assert "hellofresh-app" in hass.data[DATA_PANELS]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert "hellofresh-app" not in hass.data[DATA_PANELS]


async def test_changed_options_reload_once_and_unchanged_ones_not_at_all(
    hass: HomeAssistant, account_fetch: AsyncMock
) -> None:
    entry = await _load(hass)
    await _save_options(hass, entry)  # the first save stores the defaults: a change
    loads = account_fetch.await_count

    await _save_options(hass, entry)
    assert account_fetch.await_count == loads

    await _save_options(hass, entry, scan_interval_minutes=240)
    assert account_fetch.await_count == loads + 1
    assert entry.state is ConfigEntryState.LOADED


async def test_reconfigure_reloads_a_loaded_entry_once(
    hass: HomeAssistant, account_fetch: AsyncMock
) -> None:
    entry = await _load(hass)
    loads = account_fetch.await_count

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"country": "us", "username": "cook@example.com", "password": "new-password"},
    )
    await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    assert entry.data["password"] == "new-password"
    assert entry.state is ConfigEntryState.LOADED
    assert account_fetch.await_count == loads + 1


async def test_reauth_reloads_a_loaded_entry_once(
    hass: HomeAssistant, account_fetch: AsyncMock
) -> None:
    """Reauth raised at runtime, when a poll hit an auth error, finds the entry loaded."""
    entry = await _load(hass)
    loads = account_fetch.await_count

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "cook@example.com", "password": "new-password"}
    )
    await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    assert entry.state is ConfigEntryState.LOADED
    assert account_fetch.await_count == loads + 1


async def test_reauth_brings_back_an_entry_whose_setup_failed(
    hass: HomeAssistant, account_fetch: AsyncMock
) -> None:
    account_fetch.side_effect = HelloFreshAuthError("token rejected")
    entry = await _load(hass)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    [flow] = [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"]["source"] == SOURCE_REAUTH
    ]

    account_fetch.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"username": "cook@example.com", "password": "new-password"}
    )
    await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    assert entry.state is ConfigEntryState.LOADED
