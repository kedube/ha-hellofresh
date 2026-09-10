"""Box-size and delivery-day selects: option catalogs, current value, and writes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest import mock

from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.hellofresh import select as select_module
from custom_components.hellofresh.api import (
    HelloFreshAccountData,
    HelloFreshDeliveryOption,
    HelloFreshError,
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
        config_entry=SimpleNamespace(entry_id="entry-1", title="HelloFresh"),
        async_request_refresh=mock.AsyncMock(),
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
        "2 meals × 2 servings",
        "3 meals × 2 servings",
        "4 meals × 2 servings",
    ]
    assert entity.available is True
    assert entity.current_option == "3 meals × 2 servings"


def test_box_size_select_writes_the_handle_and_refreshes() -> None:
    client = SimpleNamespace(
        async_list_plan_options=mock.AsyncMock(return_value=PLAN_OPTIONS),
        async_change_plan=mock.AsyncMock(),
    )
    entity = _box_select(client)
    _run(entity._async_load_options())
    with mock.patch.object(select_module, "async_delete_write_actions_issue") as cleared:
        _run(entity.async_select_option("4 meals × 2 servings"))
    client.async_change_plan.assert_awaited_once_with("US-CBU-4-2-0", "sub-1")
    entity.coordinator.async_request_refresh.assert_awaited_once()
    cleared.assert_called_once()
    # Re-selecting the current plan is a no-op, not a write.
    _run(entity.async_select_option("3 meals × 2 servings"))
    client.async_change_plan.assert_awaited_once()


def test_box_size_select_surfaces_write_failures_as_repairs_issue() -> None:
    client = SimpleNamespace(
        async_list_plan_options=mock.AsyncMock(return_value=PLAN_OPTIONS),
        async_change_plan=mock.AsyncMock(side_effect=HelloFreshError("nope")),
    )
    entity = _box_select(client)
    _run(entity._async_load_options())
    with (
        mock.patch.object(select_module, "async_create_write_actions_issue") as raised,
        pytest.raises(HomeAssistantError),
    ):
        _run(entity.async_select_option("2 meals × 2 servings"))
    raised.assert_called_once()
    with pytest.raises(HomeAssistantError):
        _run(entity.async_select_option("not an option"))


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
    with mock.patch.object(select_module, "async_delete_write_actions_issue"):
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
