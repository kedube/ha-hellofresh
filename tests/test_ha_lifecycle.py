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

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, PropertyMock, patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.frontend import DATA_PANELS
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, section
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.hellofresh.api import (
    HelloFreshAccountData,
    HelloFreshAuthError,
    HelloFreshError,
    HelloFreshInvalidRequestError,
    HelloFreshOrder,
    HelloFreshRecipe,
    HelloFreshSubscription,
    HelloFreshWeek,
)
from custom_components.hellofresh.client import HelloFreshClient
from custom_components.hellofresh.config_flow import HelloFreshConfigFlow
from custom_components.hellofresh.const import DOMAIN
from custom_components.hellofresh.parsers import local_today

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
    with (
        patch.object(
            HelloFreshClient, "async_get_account_data", AsyncMock(return_value=_account_data())
        ) as fetch,
        patch.object(HelloFreshClient, "async_ensure_token_fresh", AsyncMock()),
        patch.object(HelloFreshClient, "async_list_plan_options", AsyncMock(return_value=[])),
        patch.object(HelloFreshClient, "async_get_delivery_options", AsyncMock(return_value=[])),
        patch.object(
            HelloFreshClient,
            "async_get_recipe_detail",
            AsyncMock(return_value=SimpleNamespace(ingredients=[])),
        ),
        patch.object(HelloFreshConfigFlow, "_async_validate", side_effect=_validate),
    ):
        yield fetch


def _account_data() -> HelloFreshAccountData:
    """One subscription with two upcoming weeks, each with a menu and a selected meal."""
    week_delivery = local_today() + timedelta(days=5)
    return HelloFreshAccountData(
        account_data_available=True,
        weeks=[
            HelloFreshWeek(
                week_id="2026-W46",
                display_name="Week 46",
                subscription_id="sub-1",
                delivery_date=week_delivery,
                selection_deadline=datetime.now(UTC) + timedelta(days=2),
                meals_required=3,
                meals_selected=1,
                recipes=[HelloFreshRecipe(recipe_id="r1", name="Soup", is_selected=True)],
            ),
            HelloFreshWeek(
                week_id="2026-W47",
                display_name="Week 47",
                subscription_id="sub-1",
                delivery_date=week_delivery + timedelta(days=7),
                selection_deadline=datetime.now(UTC) + timedelta(days=9),
                meals_required=3,
                meals_selected=1,
                recipes=[HelloFreshRecipe(recipe_id="r2", name="Stew", is_selected=True)],
            ),
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


async def _load(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="HelloFresh (US)",
        unique_id="us::acct-1",
        data={"country": "us", "username": "cook@example.com", "password": "made-up"},
        options=options or {},
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


# ---- unload ----------------------------------------------------------------------------------

# What Home Assistant logs when an integration unloads a platform it never set up for the
# entry. Older versions raised instead, and the entry ended in "Unload failed".
UNLOAD_OF_A_PLATFORM_NEVER_SET_UP = "Ignored unload request"


def _unload_complaints(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if UNLOAD_OF_A_PLATFORM_NEVER_SET_UP in record.getMessage()
        or "Error unloading entry" in record.getMessage()
    ]


async def test_unload_leaves_another_integrations_todo_platform_alone(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """With prep lists off, unload must not ask for a to-do platform it never set up.

    Any other to-do integration (Shopping list, Local To-do) loads the ``todo`` component.
    Unload used to ask for it regardless; Home Assistant then failed the unload and left the
    entry in "Unload failed", its timers still running, until a restart.
    """
    assert await async_setup_component(hass, "todo", {})
    await hass.async_block_till_done()
    entry = await _load(hass, {"enable_prep_lists": False})
    assert entry.state is ConfigEntryState.LOADED
    assert "todo" not in entry.runtime_data.platforms

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert _unload_complaints(caplog) == []


async def test_turning_prep_lists_off_unloads_them_and_later_reloads_stay_clean(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The reload that follows the option change unloads what was set up, not what is wanted.

    By the time that unload runs the option already reads off, so the to-do entities are
    removed only because setup remembered it had forwarded them. Every later reload then
    leaves the to-do platform out.
    """
    entry = await _load(hass)  # prep lists are on by default
    registry = er.async_get(hass)
    assert [
        e for e in er.async_entries_for_config_entry(registry, entry.entry_id) if e.domain == "todo"
    ]
    assert hass.states.get("todo.hellofresh_us_prep_list") is not None

    await _save_options(hass, entry, enable_prep_lists=False)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.options["enable_prep_lists"] is False
    assert "todo" not in entry.runtime_data.platforms
    prep_list = hass.states.get("todo.hellofresh_us_prep_list")
    assert prep_list is None or prep_list.state == "unavailable"

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert _unload_complaints(caplog) == []


# ---- polls that return nothing ---------------------------------------------------------------


async def test_a_poll_without_account_data_keeps_the_last_good_data(
    hass: HomeAssistant, account_fetch: AsyncMock
) -> None:
    """A failed first request must not replace real data with an empty account.

    The client answers a failed subscriptions request with an account-less result rather
    than an error. Taken as a successful poll it emptied every entity for a whole scan
    interval, and on recovery every upcoming week looked newly published.
    """
    entry = await _load(hass)
    coordinator = entry.runtime_data
    good = coordinator.data
    assert hass.states.get("sensor.hellofresh_us_upcoming_delivery_count").state == "1"

    account_fetch.return_value = HelloFreshAccountData().finalize()
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    assert coordinator.data is good
    # An ordinary failed update: the entities say so instead of reporting an empty account.
    assert hass.states.get("sensor.hellofresh_us_upcoming_delivery_count").state == "unavailable"

    account_fetch.return_value = _account_data()
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    assert hass.states.get("sensor.hellofresh_us_upcoming_delivery_count").state == "1"
    assert hass.states.get("sensor.hellofresh_us_subscription_status").state == "Active"
    # Nothing changed between the two good polls, so nothing is announced.
    assert coordinator.delivery_events == []


async def test_setup_is_retried_when_the_first_poll_returns_nothing(
    hass: HomeAssistant, account_fetch: AsyncMock
) -> None:
    """Home Assistant started before the network was ready: retry, do not set up empty."""
    account_fetch.return_value = HelloFreshAccountData().finalize()
    entry = await _load(hass)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_accepts_a_menu_without_an_account(
    hass: HomeAssistant, account_fetch: AsyncMock
) -> None:
    """The public-menu fallback is a real result: an account-less poll that carries a menu."""
    menu = HelloFreshWeek(
        week_id="2026-W46",
        display_name="Week 46",
        source="public_menu",
        recipes=[HelloFreshRecipe(recipe_id="r1", name="Soup")],
    )
    account_fetch.return_value = HelloFreshAccountData(public_menu_weeks=[menu]).finalize()
    entry = await _load(hass)

    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.hellofresh_us_public_menu_recipe_count").state == "1"

    # The account arrives later. With no earlier weeks to compare against, its weeks are
    # not all announced as newly published.
    account_fetch.return_value = _account_data()
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert entry.runtime_data.delivery_events == []


# ---- setup does not wait for the extra requests ----------------------------------------------


async def test_setup_does_not_wait_for_recipe_details_or_option_catalogs(
    hass: HomeAssistant,
) -> None:
    """The prep lists and the two selects fetch in the background.

    Awaited while the entities were being added, these requests (one per selected meal, plus
    two catalogs) were part of every start: the entry was not set up until all had answered.
    """
    answer = asyncio.Event()

    async def _slow(*_args, **_kwargs):
        await answer.wait()
        return []

    async def _slow_detail(*_args, **_kwargs):
        await answer.wait()
        return SimpleNamespace(
            ingredients=[{"name": "Butter", "shipped": False, "amount": 2, "unit": "tablespoon"}]
        )

    with (
        patch.object(HelloFreshClient, "async_list_plan_options", _slow),
        patch.object(HelloFreshClient, "async_get_delivery_options", _slow),
        patch.object(HelloFreshClient, "async_get_recipe_detail", _slow_detail),
    ):
        # HelloFresh has not answered any of them, and setup finishes anyway.
        entry = await asyncio.wait_for(_load(hass), timeout=5)
        assert entry.state is ConfigEntryState.LOADED
        assert hass.states.get("todo.hellofresh_us_prep_list").state == "0"

        answer.set()
        await hass.async_block_till_done(wait_background_tasks=True)

    assert hass.states.get("todo.hellofresh_us_prep_list").state == "1"


async def test_unload_cancels_requests_still_running_in_the_background(
    hass: HomeAssistant,
) -> None:
    """A slow recipe fetch must not outlive the entry it belongs to."""
    never = asyncio.Event()
    started = 0

    async def _stuck_detail(*_args, **_kwargs):
        nonlocal started
        started += 1
        await never.wait()

    with patch.object(HelloFreshClient, "async_get_recipe_detail", _stuck_detail):
        entry = await _load(hass)
        assert started
        assert await asyncio.wait_for(hass.config_entries.async_unload(entry.entry_id), timeout=5)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED


# ---- the client's sessions -------------------------------------------------------------------


async def test_the_clients_sessions_are_closed_on_unload(hass: HomeAssistant) -> None:
    with patch.object(HelloFreshClient, "async_close", AsyncMock()) as close:
        entry = await _load(hass)
        close.assert_not_awaited()
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    close.assert_awaited_once()


async def test_the_clients_sessions_are_closed_when_setup_fails_late(
    hass: HomeAssistant,
) -> None:
    """A failure after the first poll must not leave the pooled sessions open.

    Only a failed first poll and a successful unload used to close them.
    """
    with (
        patch.object(HelloFreshClient, "async_close", AsyncMock()) as close,
        patch(
            "custom_components.hellofresh.MealPlan.async_load",
            AsyncMock(side_effect=RuntimeError("storage unreadable")),
        ),
    ):
        entry = await _load(hass)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    close.assert_awaited_once()


# ---- token sensors ---------------------------------------------------------------------------


async def test_token_sensor_updates_when_the_token_rotates(hass: HomeAssistant) -> None:
    """The client's token callback reaches the "minutes remaining" sensor.

    The sensor was only rewritten when a poll returned changed account data, so it could
    read the same value for hours while the token rotated every half hour.
    """
    captured: dict = {}
    real_client = HelloFreshClient

    def _client(**kwargs):
        captured.update(kwargs)
        return real_client(**kwargs)

    sensor = "sensor.hellofresh_us_access_token_minutes_remaining"
    with patch("custom_components.hellofresh.HelloFreshClient", side_effect=_client):
        entry = await _load(hass)
    assert hass.states.get(sensor).state == "unknown"  # the stub client holds no token

    expires_at = datetime.now(UTC) + timedelta(minutes=30, seconds=30)
    with patch.object(HelloFreshClient, "token_expires_at", PropertyMock(return_value=expires_at)):
        captured["token_refresh_callback"](
            {"access_token": "rotated-access", "refresh_token": "rotated-refresh"}
        )
        await hass.async_block_till_done()

    assert entry.data["refresh_token"] == "rotated-refresh"
    assert hass.states.get(sensor).state == "30"
    assert hass.states.get(sensor).attributes["expires_at"] == expires_at.isoformat()


# ---- services, through Home Assistant's own service layer ------------------------------------


async def test_a_failing_read_service_is_a_handled_error(hass: HomeAssistant) -> None:
    """Home Assistant gets a HomeAssistantError, not the client's own exception."""
    await _load(hass)
    with (
        patch.object(
            HelloFreshClient, "async_get_plans", AsyncMock(side_effect=HelloFreshError("boom"))
        ),
        pytest.raises(HomeAssistantError, match="boom"),
    ):
        await hass.services.async_call(DOMAIN, "get_plans", {}, blocking=True, return_response=True)


async def test_an_interval_the_weekday_change_cannot_apply_is_refused(
    hass: HomeAssistant,
) -> None:
    await _load(hass)
    with (
        patch.object(HelloFreshClient, "async_change_delivery_weekday", AsyncMock()) as change,
        pytest.raises(ServiceValidationError, match="delivery_interval"),
    ):
        await hass.services.async_call(
            DOMAIN,
            "change_delivery_weekday",
            {"delivery_option": "US-3-0800-2000", "delivery_interval": 2},
            blocking=True,
        )
    change.assert_not_awaited()


async def test_get_weeks_with_and_without_the_menus(hass: HomeAssistant) -> None:
    await _load(hass)

    full = await hass.services.async_call(
        DOMAIN, "get_weeks", {}, blocking=True, return_response=True
    )
    light = await hass.services.async_call(
        DOMAIN, "get_weeks", {"include_recipes": False}, blocking=True, return_response=True
    )

    assert [week["week_id"] for week in full["weeks"]] == ["2026-W46", "2026-W47"]
    assert full["weeks"][0]["recipes"][0]["recipe_id"] == "r1"
    assert full["weeks"][0]["order"]["order_id"] == "ord-1"
    assert [week["week_id"] for week in light["weeks"]] == ["2026-W46", "2026-W47"]
    assert all("recipes" not in week for week in light["weeks"])
    assert light["weeks"][0]["order"]["order_id"] == "ord-1"
    assert light["account"] == full["account"]


async def test_services_refuse_an_entry_whose_setup_failed(
    hass: HomeAssistant, account_fetch: AsyncMock
) -> None:
    account_fetch.side_effect = HelloFreshError("HelloFresh is down")
    entry = await _load(hass)
    assert entry.state is ConfigEntryState.SETUP_RETRY

    with pytest.raises(HomeAssistantError, match="No HelloFresh account is currently loaded"):
        await hass.services.async_call(DOMAIN, "refresh_data", {}, blocking=True)
    with pytest.raises(ServiceValidationError, match="not loaded"):
        await hass.services.async_call(
            DOMAIN, "refresh_data", {"config_entry_id": entry.entry_id}, blocking=True
        )


# ---- one week's box for another number of people ---------------------------------------------


async def test_the_get_weeks_after_a_resize_already_shows_the_new_servings(
    hass: HomeAssistant,
) -> None:
    """The card re-reads the one week after change_week_servings and must see the new box.

    The client publishes a snapshot with the week resized; the service hands it to the
    coordinator before it answers, so no poll is needed (the one it starts is held back here).
    """
    entry = await _load(hass)
    coordinator = entry.runtime_data

    async def _resize(self, week_id, servings):
        weeks = [
            replace(week, servings=servings) if week.week_id == week_id else week
            for week in coordinator.data.weeks
        ]
        self._last_account_data = replace(coordinator.data, weeks=weeks).finalize()
        return {
            "week_id": week_id,
            "servings": servings,
            "product_handle": "US-CBU-3-4-0",
            "price": 129.99,
            "currency": "USD",
            "changed": True,
        }

    with (
        patch.object(HelloFreshClient, "async_change_week_servings", _resize),
        patch.object(coordinator, "async_request_refresh", AsyncMock()),
    ):
        response = await hass.services.async_call(
            DOMAIN,
            "change_week_servings",
            {"week_id": "2026-W46", "servings": 4},
            blocking=True,
            return_response=True,
        )
        weeks = await hass.services.async_call(
            DOMAIN,
            "get_weeks",
            {"week_id": "2026-W46", "include_recipes": False},
            blocking=True,
            return_response=True,
        )

    assert response == {
        "week_id": "2026-W46",
        "servings": 4,
        "product_handle": "US-CBU-3-4-0",
        "price": 129.99,
        "currency": "USD",
    }
    assert [week["servings"] for week in weeks["weeks"]] == [4]
    assert "revised_delivery_date" in weeks["weeks"][0]


async def test_a_resize_that_cannot_work_is_refused_as_the_callers_input(
    hass: HomeAssistant,
) -> None:
    await _load(hass)
    with (
        patch.object(
            HelloFreshClient,
            "async_change_week_servings",
            AsyncMock(side_effect=HelloFreshInvalidRequestError("Week 2026-W46 is skipped")),
        ),
        pytest.raises(ServiceValidationError, match="is skipped"),
    ):
        await hass.services.async_call(
            DOMAIN,
            "change_week_servings",
            {"week_id": "2026-W46", "servings": 4},
            blocking=True,
            return_response=True,
        )


# ---- the payment warning ---------------------------------------------------------------------


def _with_failed_payment() -> HelloFreshAccountData:
    """The usual account, with the charge for its next box (Week 46) failed."""
    data = _account_data()
    failure = {
        "subscription_id": "sub-1",
        "delivery_date": data.weeks[0].delivery_date,
        "status": "failed",
        "amount": 97.5,
        "currency": "USD",
    }
    return replace(data, payment_failures=[failure]).finalize()


def _payment_issue(hass: HomeAssistant, entry: MockConfigEntry):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"payment_problem_{entry.entry_id}")


async def test_a_payment_problem_is_a_warning_while_it_lasts(
    hass: HomeAssistant, account_fetch: AsyncMock
) -> None:
    account_fetch.return_value = _with_failed_payment()
    # Not a data-quality warning: turning those off does not hide it.
    entry = await _load(hass, {"show_data_quality_issues": False})

    issue = _payment_issue(hass, entry)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_placeholders == {
        "entry_title": "HelloFresh (US)",
        "date": account_fetch.return_value.weeks[0].delivery_date.isoformat(),
        "amount": "97.50 USD",
    }
    assert issue.learn_more_url == "https://www.hellofresh.com"
    assert hass.states.get("binary_sensor.hellofresh_us_payment_problem").state == "on"
    summary = await hass.services.async_call(
        DOMAIN, "get_account_summary", {}, blocking=True, return_response=True
    )
    assert summary["payment_problem"] is True
    assert [week["week_id"] for week in summary["payment_problem_weeks"]] == ["2026-W46"]

    # The charge went through: the warning and the sensor clear on the next poll.
    account_fetch.return_value = _account_data()
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert _payment_issue(hass, entry) is None
    assert hass.states.get("binary_sensor.hellofresh_us_payment_problem").state == "off"

    # Back again; the warning goes when the entry unloads, and with the entry.
    account_fetch.return_value = _with_failed_payment()
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert _payment_issue(hass, entry) is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert _payment_issue(hass, entry) is None

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _payment_issue(hass, entry) is not None
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert _payment_issue(hass, entry) is None


async def test_the_credit_parts_are_sensors_in_the_credits_currency(
    hass: HomeAssistant, account_fetch: AsyncMock
) -> None:
    account_fetch.return_value = replace(
        _account_data(),
        account_credit=25.0,
        account_credit_currency="USD",
        account_credit_cash=15.0,
        account_credit_bonus=10.0,
        account_credit_restricted=3.0,
    ).finalize()
    await _load(hass)

    cash = hass.states.get("sensor.hellofresh_us_account_credit_cash")
    bonus = hass.states.get("sensor.hellofresh_us_account_credit_bonus")
    assert (cash.state, cash.attributes["unit_of_measurement"]) == ("15.0", "USD")
    assert (bonus.state, bonus.attributes["unit_of_measurement"]) == ("10.0", "USD")
    credit = hass.states.get("sensor.hellofresh_us_account_credit")
    assert credit.attributes["restricted_amount"] == 3.0
    assert hass.states.get("sensor.hellofresh_us_loyalty_boxes_until_free_box").state == "unknown"


# ---- the selection-deadline events -----------------------------------------------------------

EVENTS = "event.hellofresh_us_delivery_events"
START = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def _open_week(deadline: datetime) -> HelloFreshAccountData:
    """One week still open for changes, with one of its three meals chosen."""
    return HelloFreshAccountData(
        account_data_available=True,
        weeks=[
            HelloFreshWeek(
                week_id="2026-W48",
                display_name="Week 48",
                subscription_id="sub-1",
                delivery_date=(deadline + timedelta(days=5)).date(),
                selection_deadline=deadline,
                meals_required=3,
                meals_selected=1,
                allowed_actions={"mealSwap": True},
                recipes=[HelloFreshRecipe(recipe_id="r1", name="Soup", is_selected=True)],
            )
        ],
        subscriptions=[
            HelloFreshSubscription(
                subscription_id="sub-1", display_name="Classic Plan", status="ACTIVE", servings=2
            )
        ],
    ).finalize()


async def _at(hass: HomeAssistant, freezer: FrozenDateTimeFactory, moment: datetime) -> None:
    freezer.move_to(moment)
    async_fire_time_changed(hass, moment)
    await hass.async_block_till_done()


def _fired(entry: MockConfigEntry) -> list[str]:
    return [kind for _serial, kind, _attributes in entry.runtime_data.delivery_events]


async def test_the_deadline_events_fire_at_their_moment_and_only_once(
    hass: HomeAssistant, account_fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(START)
    deadline = START + timedelta(hours=30)
    account_fetch.side_effect = lambda: _open_week(deadline)
    entry = await _load(hass)
    assert _fired(entry) == []
    assert hass.states.get("binary_sensor.hellofresh_us_needs_meal_selection").state == "on"

    await _at(hass, freezer, deadline - timedelta(hours=24))
    notice = hass.states.get(EVENTS).attributes
    assert notice["event_type"] == "selection_deadline_approaching"
    assert (notice["week_id"], notice["subscription_id"]) == ("2026-W48", "sub-1")
    assert notice["deadline"] == deadline.isoformat()
    assert notice["needs_selection"] is True
    assert (notice["meals_selected"], notice["meals_required"]) == (1, 3)

    await _at(hass, freezer, deadline)
    locked = hass.states.get(EVENTS).attributes
    assert locked["event_type"] == "selection_locked"
    assert locked["deadline"] == deadline.isoformat()
    # What the clock decides moved with the deadline, not with the next poll.
    assert hass.states.get("binary_sensor.hellofresh_us_needs_meal_selection").state == "off"

    # Polls after the deadline see the week locked as well, and say nothing more.
    await entry.runtime_data.async_refresh()
    await _at(hass, freezer, deadline + timedelta(hours=4))
    assert _fired(entry) == ["selection_deadline_approaching", "selection_locked"]


async def test_a_reload_does_not_announce_a_week_again_when_its_deadline_moves(
    hass: HomeAssistant, account_fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(START)
    deadline = START + timedelta(hours=30)
    account_fetch.side_effect = lambda: _open_week(deadline)
    entry = await _load(hass)
    await _at(hass, freezer, deadline - timedelta(hours=24))
    assert _fired(entry) == ["selection_deadline_approaching"]

    # HelloFresh gives the week three more hours, and Home Assistant restarts.
    later = deadline + timedelta(hours=3)
    account_fetch.side_effect = lambda: _open_week(later)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    # Its new 24-hour mark is still ahead, but the notice was given.
    await _at(hass, freezer, later - timedelta(hours=24))
    assert _fired(entry) == []
    await _at(hass, freezer, later)
    assert _fired(entry) == ["selection_locked"]


async def test_a_moment_that_passed_while_home_assistant_was_down_is_not_announced(
    hass: HomeAssistant, account_fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(START)
    deadline = START + timedelta(hours=30)
    account_fetch.side_effect = lambda: _open_week(deadline)
    entry = await _load(hass)
    coordinator = entry.runtime_data
    assert len(coordinator._deadline_timers) == 2

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert coordinator._deadline_timers == {}

    # Down through the 24-hour mark: nothing is said about it afterwards.
    await _at(hass, freezer, deadline - timedelta(hours=20))
    assert coordinator.delivery_events == []
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await _at(hass, freezer, deadline - timedelta(minutes=1))
    assert _fired(entry) == []
    # The deadline itself was still ahead when it came back up.
    await _at(hass, freezer, deadline)
    assert _fired(entry) == ["selection_locked"]

    # Down through a deadline as well: nothing when it comes back.
    second = later_deadline = deadline + timedelta(days=7)
    account_fetch.side_effect = lambda: _open_week(later_deadline)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    await _at(hass, freezer, second + timedelta(hours=1))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert _fired(entry) == []


async def test_what_fired_is_kept_on_unload_and_removed_with_the_entry(
    hass: HomeAssistant,
    hass_storage: dict,
    account_fetch: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(START)
    deadline = START + timedelta(hours=30)
    account_fetch.side_effect = lambda: _open_week(deadline)
    entry = await _load(hass)
    await _at(hass, freezer, deadline - timedelta(hours=24))

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    key = f"{DOMAIN}.delivery_events.{entry.entry_id}"
    assert hass_storage[key]["data"] == {
        "weeks": {"sub-1|2026-W48": ["selection_deadline_approaching"]}
    }

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage
