"""Tests for the shared service dispatch layer in ``services.py``.

Every one of the integration's services goes through ``_target_coordinator`` to decide
*which* account it acts on, and through one wrapper that turns client exceptions into
user-facing errors. A mistake in either affects all services at once, and the failure modes are
quiet — acting on the wrong account, or swallowing a client error so a failed write looks like
it succeeded.

The handlers are reached by calling the real ``async_register_services`` against a stub hass
and capturing what it registers.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest

from custom_components.hellofresh.api import (
    HelloFreshAccountData,
    HelloFreshAuthError,
    HelloFreshError,
    HelloFreshOrder,
    HelloFreshRecipe,
    HelloFreshSubscription,
    HelloFreshWeek,
)
from custom_components.hellofresh.const import ATTR_CONFIG_ENTRY_ID, DOMAIN
from custom_components.hellofresh.coordinator import HelloFreshDataUpdateCoordinator
from custom_components.hellofresh.models import HelloFreshNotImplementedError
from custom_components.hellofresh.parsers import local_today
from custom_components.hellofresh.services import SERVICES, async_register_services

ISSUE_CREATE = "custom_components.hellofresh.coordinator.async_create_write_actions_issue"
ISSUE_DELETE = "custom_components.hellofresh.coordinator.async_delete_write_actions_issue"


class _Services:
    """Minimal stand-in for `hass.services` that records registrations."""

    def __init__(self) -> None:
        self.registered: dict[str, object] = {}

    def has_service(self, _domain: str, service: str) -> bool:
        return service in self.registered

    def async_register(self, _domain, service, handler, **_kw):
        self.registered[service] = handler


def _hass(coordinators: dict, *, states: dict | None = None, domains: dict | None = None):
    # Coordinators live on each entry's runtime_data (HA's per-entry storage), which is what
    # the service layer reads for entries that are loaded.
    entries = {
        entry_id: SimpleNamespace(
            entry_id=entry_id,
            title=f"HelloFresh {entry_id}",
            options={},
            domain=(domains or {}).get(entry_id, DOMAIN),
            state=(states or {}).get(entry_id, ConfigEntryState.LOADED),
            runtime_data=coordinator,
        )
        for entry_id, coordinator in coordinators.items()
    }
    return SimpleNamespace(
        services=_Services(),
        config_entries=SimpleNamespace(
            async_entries=lambda domain: [e for e in entries.values() if e.domain == domain],
            async_get_entry=entries.get,
        ),
    )


def _coordinator(
    entry_id: str, *, refresh_error: Exception | None = None, client: object | None = None
) -> SimpleNamespace:
    calls: list[str] = []

    async def async_request_refresh() -> None:
        calls.append(entry_id)

    async def async_after_write(*, full_refresh: bool = True) -> None:
        if full_refresh:
            calls.append(entry_id)

    async def async_refresh() -> None:
        # Like Home Assistant's coordinator: a failed refresh is recorded, not raised.
        calls.append(entry_id)
        coordinator.last_update_success = refresh_error is None
        coordinator.last_exception = refresh_error

    coordinator = SimpleNamespace(
        async_request_refresh=async_request_refresh,
        async_refresh=async_refresh,
        async_after_write=async_after_write,
        last_update_success=True,
        last_exception=None,
        calls=calls,
        client=client,
        config_entry=SimpleNamespace(
            entry_id=entry_id,
            title=f"HelloFresh {entry_id}",
            async_start_reauth=mock.Mock(),
        ),
    )
    return coordinator


def _register(hass) -> dict:
    async_register_services(hass)
    return hass.services.registered


def _call(handler, data: dict, hass) -> object:
    # Loops are deliberately left open: HA's autouse `verify_cleanup` fixture touches the
    # current event loop at teardown, and a closed one raises there.
    service_call = SimpleNamespace(data=data, hass=hass)
    loop = asyncio.new_event_loop()
    return loop.run_until_complete(handler(service_call))


# ---- which account a call acts on -----------------------------------------------------------


def test_single_account_needs_no_config_entry_id() -> None:
    """The common case: one account, so the service targets it implicitly."""
    coordinator = _coordinator("entry1")
    hass = _hass({"entry1": coordinator})
    handlers = _register(hass)

    _call(handlers["refresh_data"], {}, hass)

    assert coordinator.calls == ["entry1"]


def test_multiple_accounts_without_a_target_is_rejected() -> None:
    """Guessing which account to act on would silently write to the wrong one."""
    hass = _hass({"entry1": _coordinator("entry1"), "entry2": _coordinator("entry2")})
    handlers = _register(hass)

    with pytest.raises(ServiceValidationError, match="Specify config_entry_id"):
        _call(handlers["get_weeks"], {}, hass)


def test_explicit_config_entry_id_selects_that_account() -> None:
    """With several accounts, the caller's choice must be honoured exactly."""
    first, second = _coordinator("entry1"), _coordinator("entry2")
    hass = _hass({"entry1": first, "entry2": second})
    handlers = _register(hass)

    _call(handlers["refresh_data"], {ATTR_CONFIG_ENTRY_ID: "entry2"}, hass)

    assert second.calls == ["entry2"]
    assert first.calls == [], "the untargeted account must be left alone"


def test_unknown_config_entry_id_is_an_error_not_a_silent_noop() -> None:
    """A typo'd entry id must fail loudly rather than doing nothing."""
    hass = _hass({"entry1": _coordinator("entry1")})
    handlers = _register(hass)

    with pytest.raises(ServiceValidationError, match="config entry not found"):
        _call(handlers["refresh_data"], {ATTR_CONFIG_ENTRY_ID: "nope"}, hass)


def test_another_integrations_entry_id_is_refused() -> None:
    """An entry id resolves for ANY integration; its runtime_data is not ours to use."""
    foreign = _coordinator("other")
    hass = _hass(
        {"entry1": _coordinator("entry1"), "other": foreign}, domains={"other": "shopping_list"}
    )
    handlers = _register(hass)

    with pytest.raises(ServiceValidationError, match="config entry not found"):
        _call(handlers["refresh_data"], {ATTR_CONFIG_ENTRY_ID: "other"}, hass)
    assert foreign.calls == []


def test_no_loaded_account_reports_that_specifically() -> None:
    """ "Nothing loaded" and "which one?" are different problems; don't conflate them."""
    hass = _hass({})
    handlers = _register(hass)

    with pytest.raises(HomeAssistantError, match="No HelloFresh account is currently loaded"):
        _call(handlers["refresh_data"], {}, hass)


@pytest.mark.parametrize(
    "state",
    [
        ConfigEntryState.SETUP_ERROR,
        ConfigEntryState.SETUP_RETRY,
        ConfigEntryState.FAILED_UNLOAD,
        ConfigEntryState.NOT_LOADED,
    ],
)
def test_an_entry_that_is_not_loaded_is_never_acted_on(state: ConfigEntryState) -> None:
    """runtime_data outlives a failed setup or unload; only a LOADED entry counts.

    Home Assistant leaves runtime_data on an entry whose setup failed late or whose unload
    failed, so going by its presence let services write through a coordinator whose timers had
    already stopped.
    """
    zombie = _coordinator("entry1")
    hass = _hass({"entry1": zombie}, states={"entry1": state})
    handlers = _register(hass)

    with pytest.raises(HomeAssistantError, match="No HelloFresh account is currently loaded"):
        _call(handlers["refresh_data"], {}, hass)
    with pytest.raises(ServiceValidationError, match="not loaded"):
        _call(handlers["refresh_data"], {ATTR_CONFIG_ENTRY_ID: "entry1"}, hass)
    assert zombie.calls == []


def test_a_broken_account_does_not_make_the_working_one_ambiguous() -> None:
    """One loaded account plus one that failed to set up is still "the only account"."""
    working, broken = _coordinator("entry1"), _coordinator("entry2")
    hass = _hass(
        {"entry1": working, "entry2": broken}, states={"entry2": ConfigEntryState.SETUP_ERROR}
    )
    handlers = _register(hass)

    _call(handlers["refresh_data"], {}, hass)

    assert working.calls == ["entry1"]
    assert broken.calls == []


def test_refresh_data_with_several_accounts_still_needs_a_target() -> None:
    """`refresh_data` does NOT fan out — ambiguity is refused for every service alike."""
    first, second = _coordinator("entry1"), _coordinator("entry2")
    hass = _hass({"entry1": first, "entry2": second})
    handlers = _register(hass)

    with pytest.raises(ServiceValidationError, match="Specify config_entry_id"):
        _call(handlers["refresh_data"], {}, hass)

    assert first.calls == [] and second.calls == []


# ---- what a HelloFresh failure becomes ------------------------------------------------------


class _FailingClient:
    """A client whose every method raises ``error`` when awaited."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def __getattr__(self, _name: str):
        async def _raise(*_args, **_kwargs):
            raise self._error

        return _raise


def _failing_client(error: Exception) -> _FailingClient:
    return _FailingClient(error)


# One minimal valid call per service that reaches the client. Services that only read the
# coordinator's own data (get_weeks, get_account_summary, the meal plan) cannot fail this way.
CLIENT_CALLS: dict[str, dict] = {
    "get_food_profile": {},
    "set_food_profile": {"taste": {"cuisines": {"italian": 100}}},
    "get_delivery_options": {},
    "get_plans": {},
    "get_presets": {},
    "get_spending": {},
    "get_plan_options": {},
    "get_favorites": {},
    "add_favorite": {"recipe_id": "r1"},
    "remove_favorite": {"recipe_id": "r1"},
    "get_recipe_detail": {"recipe_id": "r1"},
    "get_recipe_collections": {},
    "get_catalog_recipes": {},
    "get_menu_courses": {"week_id": "2026-W47"},
    "preview_meal_price": {"week_id": "2026-W47", "recipe_ids": ["r1"]},
    "select_meals": {"week_id": "2026-W47", "recipe_ids": ["r1"]},
    "select_market_items": {"week_id": "2026-W47", "quantities": {"m1": 1}},
    "skip_week": {"week_id": "2026-W47"},
    "unskip_week": {"week_id": "2026-W47"},
    "reschedule_week": {"week_id": "2026-W47", "delivery_option": "US-1-0800-2000"},
    "change_plan": {"product_handle": "US-CBU-3-2-0"},
    "change_delivery_weekday": {"delivery_option": "US-1-0800-2000"},
    "change_week_servings": {"week_id": "2026-W47", "servings": 4},
}


@pytest.mark.parametrize("service", sorted(CLIENT_CALLS))
def test_every_service_reports_a_client_failure_as_a_home_assistant_error(service: str) -> None:
    """No service may let the client's own exception out.

    Fourteen read services and the favorite writes used to. Home Assistant then logged an
    "unexpected exception" traceback for what was only an outage and told the caller
    "unknown error"; scripts aborted with an unexpected error.
    """
    coordinator = _coordinator("entry1", client=_failing_client(HelloFreshError("upstream down")))
    hass = _hass({"entry1": coordinator})
    handlers = _register(hass)

    with mock.patch(ISSUE_CREATE) as raised, pytest.raises(HomeAssistantError) as excinfo:
        _call(handlers[service], CLIENT_CALLS[service], hass)

    assert "upstream down" in str(excinfo.value)
    # An outage is not a missing endpoint and not a rejected sign-in.
    raised.assert_not_called()
    coordinator.config_entry.async_start_reauth.assert_not_called()


def test_the_client_call_table_covers_every_service_that_reaches_hellofresh() -> None:
    """A new service must be added to the table above, or it escapes the check."""
    local_only = {
        "refresh_data",
        "get_weeks",
        "get_account_summary",
        "get_delivery_tracking",
        "get_meal_plan",
        "set_planned_meal",
        "clear_planned_meal",
    }
    assert set(CLIENT_CALLS) | local_only == {spec.name for spec in SERVICES}


@pytest.mark.parametrize("service", ["get_plans", "add_favorite", "skip_week"])
def test_a_rejected_sign_in_starts_reauth(service: str) -> None:
    """An auth failure in a service call must ask the user to sign in again.

    Nothing else would: the call simply failed, and the next scheduled poll could be hours
    away.
    """
    coordinator = _coordinator(
        "entry1", client=_failing_client(HelloFreshAuthError("token rejected"))
    )
    hass = _hass({"entry1": coordinator})
    handlers = _register(hass)

    with mock.patch(ISSUE_CREATE) as raised, pytest.raises(HomeAssistantError, match="rejected"):
        _call(handlers[service], CLIENT_CALLS[service], hass)

    coordinator.config_entry.async_start_reauth.assert_called_once_with(hass)
    raised.assert_not_called()


@pytest.mark.parametrize("service", ["skip_week", "add_favorite", "set_food_profile"])
def test_unsupported_action_raises_and_opens_a_repairs_issue(service: str) -> None:
    """An endpoint the integration can't build for this account is a Repairs case.

    It is distinct from a transient failure: retrying will never help, so the user needs the
    persistent issue as well as the immediate error. The favorite and food-profile writes used
    to skip the repair. The issue registry needs real Home Assistant infrastructure, so only
    the fact that it is invoked is asserted here.
    """
    coordinator = _coordinator(
        "entry1", client=_failing_client(HelloFreshNotImplementedError("no verified endpoint"))
    )
    hass = _hass({"entry1": coordinator})
    handlers = _register(hass)

    with (
        mock.patch(ISSUE_CREATE) as raised,
        pytest.raises(HomeAssistantError, match="no verified endpoint"),
    ):
        _call(handlers[service], CLIENT_CALLS[service], hass)

    raised.assert_called_once_with(hass, "entry1", "HelloFresh entry1")
    coordinator.config_entry.async_start_reauth.assert_not_called()


def test_a_failed_refresh_surfaces_as_a_home_assistant_error() -> None:
    """A failed refresh must reach the user, not vanish into the service call.

    Home Assistant's coordinator records the failure instead of raising it, so refresh_data
    reported success either way and the card's "Refresh now" claimed it had worked.
    """
    coordinator = _coordinator("entry1", refresh_error=HelloFreshError("upstream is down"))
    hass = _hass({"entry1": coordinator})
    handlers = _register(hass)

    with pytest.raises(HomeAssistantError, match="upstream is down"):
        _call(handlers["refresh_data"], {}, hass)


def test_a_successful_write_clears_the_repair_and_refreshes() -> None:
    """A write endpoint that just worked makes a "write actions unavailable" repair stale."""
    client = SimpleNamespace(async_skip_week=mock.AsyncMock())
    coordinator = _coordinator("entry1", client=client)
    hass = _hass({"entry1": coordinator})
    handlers = _register(hass)

    with mock.patch(ISSUE_DELETE) as cleared:
        _call(handlers["skip_week"], {"week_id": "2026-W47"}, hass)

    client.async_skip_week.assert_awaited_once_with("2026-W47")
    cleared.assert_called_once_with(hass, "entry1")
    assert coordinator.calls == ["entry1"]


# ---- input that is refused before anything is sent ------------------------------------------


def test_set_food_profile_needs_at_least_one_section() -> None:
    client = SimpleNamespace(async_update_food_profile=mock.AsyncMock())
    hass = _hass({"entry1": _coordinator("entry1", client=client)})
    handlers = _register(hass)

    with pytest.raises(ServiceValidationError, match="at least one of"):
        _call(handlers["set_food_profile"], {}, hass)
    client.async_update_food_profile.assert_not_awaited()


@pytest.mark.parametrize("interval", [2, 3, 4, 0])
def test_change_delivery_weekday_refuses_an_interval_it_cannot_apply(interval: int) -> None:
    """Only weekly can be requested: the call HelloFresh accepts carries no interval.

    ``delivery_interval: 2`` used to return success while the request sent only the delivery
    option, so boxes and charges stayed weekly and only a log line said so.
    """
    client = SimpleNamespace(async_change_delivery_weekday=mock.AsyncMock())
    coordinator = _coordinator("entry1", client=client)
    hass = _hass({"entry1": coordinator})
    handlers = _register(hass)

    with pytest.raises(ServiceValidationError, match="delivery_interval"):
        _call(
            handlers["change_delivery_weekday"],
            {"delivery_option": "US-3-0800-2000", "delivery_interval": interval},
            hass,
        )

    client.async_change_delivery_weekday.assert_not_awaited()
    assert coordinator.calls == []


@pytest.mark.parametrize("extra", [{}, {"delivery_interval": 1}])
def test_change_delivery_weekday_still_changes_the_day(extra: dict) -> None:
    client = SimpleNamespace(async_change_delivery_weekday=mock.AsyncMock())
    coordinator = _coordinator("entry1", client=client)
    hass = _hass({"entry1": coordinator})
    handlers = _register(hass)

    with mock.patch(ISSUE_DELETE):
        _call(
            handlers["change_delivery_weekday"],
            {"delivery_option": "US-3-0800-2000", "subscription_id": "sub-1", **extra},
            hass,
        )

    client.async_change_delivery_weekday.assert_awaited_once_with("US-3-0800-2000", 1, "sub-1")


def test_change_delivery_weekday_schema_defaults_the_interval_to_weekly() -> None:
    hass = _hass({"entry1": _coordinator("entry1")})
    schemas: dict = {}
    hass.services.async_register = lambda _d, name, _h, schema=None, **_kw: schemas.__setitem__(
        name, schema
    )
    async_register_services(hass)

    data = schemas["change_delivery_weekday"]({"delivery_option": "US-3-0800-2000"})
    assert data["delivery_interval"] == 1


# ---- get_weeks ------------------------------------------------------------------------------


def _weeks_coordinator(data: HelloFreshAccountData) -> HelloFreshDataUpdateCoordinator:
    """A real coordinator around ``data``, with just what get_weeks reads (no hass)."""
    coordinator = HelloFreshDataUpdateCoordinator.__new__(HelloFreshDataUpdateCoordinator)
    coordinator.data = data
    coordinator.client = SimpleNamespace(menu_grace_weeks=2)
    coordinator.config_entry = SimpleNamespace(entry_id="entry1", title="HelloFresh", options={})
    coordinator._weeks_response_cache = None
    return coordinator


def _menu_week(week_id: str, day, subscription_id: str = "sub-1", **fields) -> HelloFreshWeek:
    return HelloFreshWeek(
        week_id=week_id,
        display_name=week_id,
        subscription_id=subscription_id,
        delivery_date=day,
        recipes=[HelloFreshRecipe(recipe_id=f"{week_id}-r1", name="Soup", is_selected=True)],
        **fields,
    )


def _get_weeks(coordinator, data: dict | None = None) -> dict:
    hass = _hass({"entry1": coordinator})
    handlers = _register(hass)
    return _call(handlers["get_weeks"], data or {}, hass)


def test_get_weeks_refresh_contract_is_never_served_from_the_cache() -> None:
    """``delivery_in_progress`` follows the clock even when the weeks come from the cache.

    The whole response used to be cached until the next poll, so a card open across midnight
    of delivery day was not told to switch to the fast cadence until the data changed.
    """
    data = HelloFreshAccountData(
        weeks=[_menu_week("2026-W47", local_today() + timedelta(days=1))],
        subscriptions=[HelloFreshSubscription(subscription_id="sub-1")],
    ).finalize()
    coordinator = _weeks_coordinator(data)

    before = _get_weeks(coordinator)
    assert before["account"]["delivery_in_progress"] is False

    # The day turns: the same data object, so the weeks are still the cached ones.
    tomorrow = dt_util.now() + timedelta(days=1)
    with mock.patch("homeassistant.util.dt.now", return_value=tomorrow):
        after = _get_weeks(coordinator)

    assert after["account"]["delivery_in_progress"] is True
    assert after["weeks"][0]["recipes"] is before["weeks"][0]["recipes"], "weeks were rebuilt"


def test_get_weeks_reports_a_weeks_editable_state_as_of_now() -> None:
    """A week whose deadline passes after the poll is not reported editable from the cache."""
    week = _menu_week(
        "2026-W47",
        local_today() + timedelta(days=5),
        selection_deadline=datetime.now(UTC) + timedelta(hours=1),
        allowed_actions={"mealSwap": True},
        meals_required=3,
        meals_selected=0,
    )
    data = HelloFreshAccountData(
        weeks=[week], subscriptions=[HelloFreshSubscription(subscription_id="sub-1")]
    ).finalize()
    coordinator = _weeks_coordinator(data)

    open_week = _get_weeks(coordinator)["weeks"][0]
    assert open_week["is_editable"] is True
    assert open_week["needs_selection"] is True

    week.selection_deadline = datetime.now(UTC) - timedelta(minutes=1)  # the cutoff passes
    locked_week = _get_weeks(coordinator)["weeks"][0]
    assert locked_week["is_editable"] is False
    assert locked_week["needs_selection"] is False


def test_get_weeks_can_leave_the_menus_out() -> None:
    """``include_recipes: false`` returns the schedule without each week's menu."""
    day = local_today() + timedelta(days=5)
    data = HelloFreshAccountData(
        weeks=[_menu_week("2026-W47", day, allowed_actions={"mealSwap": True})],
        orders=[
            HelloFreshOrder(
                order_id="o1",
                week_id="2026-W47",
                status="scheduled",
                subscription_id="sub-1",
                delivery_date=day,
            )
        ],
        subscriptions=[HelloFreshSubscription(subscription_id="sub-1")],
    ).finalize()
    coordinator = _weeks_coordinator(data)

    full = _get_weeks(coordinator)["weeks"][0]
    light = _get_weeks(coordinator, {"include_recipes": False})["weeks"][0]

    assert [recipe["recipe_id"] for recipe in full["recipes"]] == ["2026-W47-r1"]
    for menu_key in ("recipes", "market_items", "menu_categories", "menu_filters"):
        assert menu_key in full
        assert menu_key not in light
    # Everything else is the same week: schedule, order, actions, and the account block.
    assert {key: value for key, value in full.items() if key in light} == light
    assert light["order"]["order_id"] == "o1"
    assert light["allowed_actions"] == {"mealSwap": True}
    assert "account" in _get_weeks(coordinator, {"include_recipes": False})


def test_get_weeks_pairs_each_week_with_its_own_subscriptions_order() -> None:
    """Two subscriptions share ISO week ids; each week must get its own order.

    Paired by week id alone, the second subscription's week showed the first one's tracking,
    status and price.
    """
    day = local_today() + timedelta(days=5)
    orders = [
        HelloFreshOrder(
            order_id=f"order-{sub}",
            week_id="2026-W47",
            status="scheduled",
            subscription_id=sub,
            delivery_date=day,
            tracking_number=tracking,
        )
        for sub, tracking in (("sub-1", "TRACK-ONE"), ("sub-2", None))
    ]
    data = HelloFreshAccountData(
        weeks=[_menu_week("2026-W47", day, "sub-1"), _menu_week("2026-W47", day, "sub-2")],
        orders=orders,
        subscriptions=[
            HelloFreshSubscription(subscription_id="sub-1"),
            HelloFreshSubscription(subscription_id="sub-2"),
        ],
    ).finalize()

    weeks = _get_weeks(_weeks_coordinator(data))["weeks"]

    assert {week["subscription_id"]: week["order"]["order_id"] for week in weeks} == {
        "sub-1": "order-sub-1",
        "sub-2": "order-sub-2",
    }


def test_get_weeks_filtered_to_a_week_id_returns_every_subscriptions_week() -> None:
    """The ``week_id`` filter narrows the full list; it does not pick one subscription.

    Two subscriptions share ISO week ids. The filter used to return only the primary
    subscription's week, so a card re-reading one week after a write could merge it over the
    other subscription's entry in its list.
    """
    early, late = local_today() + timedelta(days=3), local_today() + timedelta(days=5)
    data = HelloFreshAccountData(
        weeks=[
            _menu_week("2026-W47", late, "sub-1"),
            _menu_week("2026-W47", early, "sub-2"),
            _menu_week("2026-W48", late + timedelta(days=7), "sub-1"),
        ],
        orders=[
            HelloFreshOrder(
                order_id=f"order-{sub}",
                week_id="2026-W47",
                status="scheduled",
                subscription_id=sub,
                delivery_date=day,
            )
            for sub, day in (("sub-1", late), ("sub-2", early))
        ],
        subscriptions=[
            HelloFreshSubscription(subscription_id="sub-1"),
            HelloFreshSubscription(subscription_id="sub-2"),
        ],
    ).finalize()
    coordinator = _weeks_coordinator(data)

    full = [w for w in _get_weeks(coordinator)["weeks"] if w["week_id"] == "2026-W47"]
    one = _get_weeks(coordinator, {"week_id": "2026-W47"})["weeks"]

    # The same weeks, in the order the full list has them, each with its own order.
    assert [(w["subscription_id"], w["order"]["order_id"]) for w in one] == [
        (w["subscription_id"], w["order"]["order_id"]) for w in full
    ]
    assert {w["subscription_id"] for w in one} == {"sub-1", "sub-2"}
    assert _get_weeks(coordinator, {"week_id": "2026-W99"})["weeks"] == []


# ---- independent reads are sent together ----------------------------------------------------


class _Overlap:
    """Counts how many of a client's calls are in flight at once."""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0

    def method(self, result):
        async def _call(*_args, **_kwargs):
            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0)
            self.active -= 1
            return result

        return _call


def test_get_food_profile_reads_its_three_parts_together() -> None:
    overlap = _Overlap()
    part = SimpleNamespace(as_dict=lambda: {"ok": True})
    client = SimpleNamespace(
        async_get_food_profile=overlap.method(part),
        async_get_food_profile_options=overlap.method(part),
        async_get_food_profile_completion=overlap.method(None),
    )
    hass = _hass({"entry1": _coordinator("entry1", client=client)})
    handlers = _register(hass)

    response = _call(handlers["get_food_profile"], {}, hass)

    assert response == {"profile": {"ok": True}, "options": {"ok": True}, "completion": None}
    assert overlap.peak == 3, "the three requests ran one after another"


def test_get_favorites_reads_both_stores_together() -> None:
    overlap = _Overlap()
    favorite = SimpleNamespace(recipe_id="r1", as_dict=lambda: {"recipe_id": "r1"})
    client = SimpleNamespace(
        async_list_favorites=overlap.method([favorite]),
        async_get_cfs_favorites=overlap.method([]),
    )
    hass = _hass({"entry1": _coordinator("entry1", client=client)})
    handlers = _register(hass)

    response = _call(handlers["get_favorites"], {}, hass)

    assert response == {
        "favorites": [{"recipe_id": "r1"}],
        "favorite_recipe_ids": ["r1"],
        "total": 1,
        "secondary_favorites": [],
    }
    assert overlap.peak == 2, "the two requests ran one after another"


class _Unanswered:
    """A read HelloFresh never answers, noting whether it was cancelled."""

    def __init__(self) -> None:
        self.cancelled = False

    async def __call__(self, *_args, **_kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def _fails(*_args, **_kwargs):
    raise HelloFreshError("HelloFresh answered HTTP 503")


def test_get_food_profile_stops_its_other_reads_when_one_fails() -> None:
    options, completion = _Unanswered(), _Unanswered()
    client = SimpleNamespace(
        async_get_food_profile=_fails,
        async_get_food_profile_options=options,
        async_get_food_profile_completion=completion,
    )
    hass = _hass({"entry1": _coordinator("entry1", client=client)})
    handlers = _register(hass)

    with pytest.raises(HomeAssistantError):
        _call(handlers["get_food_profile"], {}, hass)

    assert options.cancelled, "the options read kept running after the call failed"
    assert completion.cancelled, "the completion read kept running after the call failed"


def test_get_favorites_stops_the_other_store_read_when_one_fails() -> None:
    secondary = _Unanswered()
    client = SimpleNamespace(async_list_favorites=_fails, async_get_cfs_favorites=secondary)
    hass = _hass({"entry1": _coordinator("entry1", client=client)})
    handlers = _register(hass)

    with pytest.raises(HomeAssistantError):
        _call(handlers["get_favorites"], {}, hass)

    assert secondary.cancelled, "the second store read kept running after the call failed"


def test_registration_is_idempotent() -> None:
    """Setting up a second account must not re-register (or duplicate) the services."""
    hass = _hass({"entry1": _coordinator("entry1")})
    first = dict(_register(hass))
    second = dict(_register(hass))

    assert first.keys() == second.keys()
    assert all(first[name] is second[name] for name in first)
