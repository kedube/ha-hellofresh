"""Box-size and delivery-day selects: option catalogs, current value, and writes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest import mock

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest

from custom_components.hellofresh import coordinator as coordinator_module
from custom_components.hellofresh.api import (
    HelloFreshAccountData,
    HelloFreshAuthError,
    HelloFreshDeliveryOption,
    HelloFreshError,
    HelloFreshNotImplementedError,
    HelloFreshSubscription,
)
from custom_components.hellofresh.select import (
    SELECTS,
    HelloFreshBoxSizeSelect,
    HelloFreshDeliveryDaySelect,
)

PLAN_OPTIONS = [
    {"handle": "US-CBU-2-2-0", "meals": 2, "servings": 2, "price": 45.94},
    {"handle": "US-CBU-3-2-0", "meals": 3, "servings": 2, "price": 65.94},
    {"handle": "US-CBU-4-2-0", "meals": 4, "servings": 2, "price": 85.94},
]
DELIVERY_OPTIONS = [
    HelloFreshDeliveryOption(
        handle="US-1-0800-2000", delivery_name="Mondays: 8AM - 8PM", is_default=True
    ),
    HelloFreshDeliveryOption(handle="US-3-0800-2000", delivery_name="Wednesdays: 8AM - 8PM"),
]


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _coordinator(client) -> SimpleNamespace:
    data = HelloFreshAccountData(
        subscriptions=[
            HelloFreshSubscription(
                subscription_id="sub-1",
                meals_required=3,
                servings=2,
                raw={"deliveryTime": "US-1-0800-2000", "product": {"sku": "US-CBU-3-2-0"}},
            )
        ]
    ).finalize()
    return SimpleNamespace(
        data=data,
        client=client,
        hass=None,
        last_update_success=True,
        config_entry=SimpleNamespace(
            entry_id="entry-1", title="HelloFresh", async_start_reauth=mock.Mock()
        ),
        async_request_refresh=mock.AsyncMock(),
        async_after_write=mock.AsyncMock(),
        async_add_listener=lambda *_args, **_kwargs: lambda: None,
    )


def _box_select(client) -> HelloFreshBoxSizeSelect:
    entity = HelloFreshBoxSizeSelect(
        _coordinator(client), next(d for d in SELECTS if d.key == "box_size")
    )
    entity.async_write_ha_state = lambda: None  # type: ignore[method-assign]
    return entity


def _day_select(client) -> HelloFreshDeliveryDaySelect:
    entity = HelloFreshDeliveryDaySelect(
        _coordinator(client), next(d for d in SELECTS if d.key == "delivery_day")
    )
    entity.async_write_ha_state = lambda: None  # type: ignore[method-assign]
    return entity


def test_box_size_select_loads_catalog_and_reports_current_plan() -> None:
    client = SimpleNamespace(
        async_list_plan_options=mock.AsyncMock(return_value=PLAN_OPTIONS),
        async_change_plan=mock.AsyncMock(),
    )
    entity = _box_select(client)
    assert entity.entity_id == "select.hellofresh_box_size"
    assert entity.available is False  # catalog not loaded yet

    _run(entity._async_load_options())
    assert entity.options == [
        "2 meals for 2 people",
        "3 meals for 2 people",
        "4 meals for 2 people",
    ]
    assert entity.available is True
    assert entity.current_option == "3 meals for 2 people"


def test_box_size_options_read_like_hellofresh() -> None:
    """Options say "people", as HelloFresh's website does ("3 meals for 2 people"), and one
    person is a person."""
    client = SimpleNamespace(
        async_list_plan_options=mock.AsyncMock(
            return_value=[
                {"handle": "XX-CBU-2-1-0", "meals": 2, "servings": 1},
                {"handle": "XX-CBU-3-4-0", "meals": 3, "servings": 4},
            ]
        ),
        async_change_plan=mock.AsyncMock(),
    )
    entity = _box_select(client)
    _run(entity._async_load_options())
    assert entity.options == ["2 meals for 1 person", "3 meals for 4 people"]


def test_box_size_without_a_plan_sku_matches_by_meals_and_people() -> None:
    client = SimpleNamespace(
        async_list_plan_options=mock.AsyncMock(return_value=PLAN_OPTIONS),
        async_change_plan=mock.AsyncMock(),
    )
    entity = _box_select(client)
    entity.coordinator.data.subscriptions[0].raw = {}
    _run(entity._async_load_options())
    assert entity.current_option == "3 meals for 2 people"
    entity.coordinator.data.subscriptions[0].servings = None
    assert entity.current_option is None


def test_box_size_select_writes_the_handle_and_refreshes() -> None:
    client = SimpleNamespace(
        async_list_plan_options=mock.AsyncMock(return_value=PLAN_OPTIONS),
        async_change_plan=mock.AsyncMock(),
    )
    entity = _box_select(client)
    _run(entity._async_load_options())
    with mock.patch.object(coordinator_module, "async_delete_write_actions_issue") as cleared:
        _run(entity.async_select_option("4 meals for 2 people"))
    client.async_change_plan.assert_awaited_once_with("US-CBU-4-2-0", "sub-1")
    entity.coordinator.async_after_write.assert_awaited_once()
    cleared.assert_called_once()
    # Re-selecting the current plan is a no-op, not a write.
    _run(entity.async_select_option("3 meals for 2 people"))
    client.async_change_plan.assert_awaited_once()


def _failing_box_select(error: Exception) -> HelloFreshBoxSizeSelect:
    client = SimpleNamespace(
        async_list_plan_options=mock.AsyncMock(return_value=PLAN_OPTIONS),
        async_change_plan=mock.AsyncMock(side_effect=error),
    )
    entity = _box_select(client)
    _run(entity._async_load_options())
    return entity


def test_box_size_select_raises_the_repair_when_the_action_is_unsupported() -> None:
    """HelloFresh having no endpoint for the change is permanent: that is what the repair is for."""
    entity = _failing_box_select(HelloFreshNotImplementedError("no verified endpoint"))
    with (
        mock.patch.object(coordinator_module, "async_create_write_actions_issue") as raised,
        pytest.raises(HomeAssistantError, match="no verified endpoint"),
    ):
        _run(entity.async_select_option("2 meals for 2 people"))
    raised.assert_called_once_with(None, "entry-1", "HelloFresh")
    entity.coordinator.async_after_write.assert_not_awaited()


def test_box_size_select_reports_a_passing_failure_without_a_repair() -> None:
    """A timeout is reported and nothing more.

    Any failure used to raise the "write actions unavailable" repair, so one slow request
    left a warning in Repairs saying the account could not be changed at all.
    """
    entity = _failing_box_select(HelloFreshError("HelloFresh did not answer"))
    with (
        mock.patch.object(coordinator_module, "async_create_write_actions_issue") as raised,
        pytest.raises(HomeAssistantError, match="did not answer"),
    ):
        _run(entity.async_select_option("2 meals for 2 people"))
    raised.assert_not_called()
    entity.coordinator.config_entry.async_start_reauth.assert_not_called()


def test_box_size_select_starts_reauth_when_the_sign_in_is_rejected() -> None:
    entity = _failing_box_select(HelloFreshAuthError("token rejected"))
    with (
        mock.patch.object(coordinator_module, "async_create_write_actions_issue") as raised,
        pytest.raises(HomeAssistantError, match="token rejected"),
    ):
        _run(entity.async_select_option("2 meals for 2 people"))
    entity.coordinator.config_entry.async_start_reauth.assert_called_once()
    raised.assert_not_called()


def test_an_unknown_option_is_a_validation_error() -> None:
    entity = _failing_box_select(HelloFreshError("never reached"))
    with pytest.raises(ServiceValidationError, match="Unknown HelloFresh option"):
        _run(entity.async_select_option("not an option"))
    entity.coordinator.client.async_change_plan.assert_not_awaited()


def test_delivery_day_select_uses_the_subscription_handle_and_writes_it() -> None:
    client = SimpleNamespace(
        async_get_delivery_options=mock.AsyncMock(return_value=DELIVERY_OPTIONS),
        async_change_delivery_weekday=mock.AsyncMock(),
    )
    entity = _day_select(client)
    assert entity.entity_id == "select.hellofresh_delivery_day"
    _run(entity._async_load_options())
    assert entity.options == ["Mondays: 8AM - 8PM", "Wednesdays: 8AM - 8PM"]
    assert entity.current_option == "Mondays: 8AM - 8PM"
    with mock.patch.object(coordinator_module, "async_delete_write_actions_issue"):
        _run(entity.async_select_option("Wednesdays: 8AM - 8PM"))
    client.async_change_delivery_weekday.assert_awaited_once_with("US-3-0800-2000", 1, "sub-1")


def test_catalog_failure_keeps_the_select_unavailable_without_raising() -> None:
    client = SimpleNamespace(
        async_get_delivery_options=mock.AsyncMock(side_effect=HelloFreshError("offline")),
    )
    entity = _day_select(client)
    _run(entity._async_load_options())
    assert entity.options == []
    assert entity.available is False


def test_the_catalog_loads_in_the_background_so_setup_does_not_wait_for_it() -> None:
    """Adding the entity starts the catalog load and returns; it does not await HelloFresh.

    Home Assistant waits for every entity to finish being added before the entry counts as
    set up, so an awaited load here made each start wait on two extra requests.
    """
    client = SimpleNamespace(async_list_plan_options=mock.AsyncMock(return_value=PLAN_OPTIONS))
    entity = _box_select(client)
    entity.hass = SimpleNamespace()  # type: ignore[assignment]
    scheduled: list = []
    entity.coordinator.config_entry.async_create_background_task = lambda _hass, coro, name: (
        scheduled.append(coro) or mock.Mock(done=lambda: False)
    )

    _run(entity.async_added_to_hass())

    client.async_list_plan_options.assert_not_awaited()
    assert len(scheduled) == 1
    assert entity.available is False

    # A coordinator update while that load is still running does not start a second one.
    entity._handle_coordinator_update()
    assert len(scheduled) == 1

    _run(scheduled[0])
    assert entity.available is True
