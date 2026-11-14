"""The credit balance's parts, the loyalty counter and the plan's box sizes.

The balance answers ``amount``, ``cash``, ``bonus``, ``restrictedAmount`` and
``cancellableCredits`` in minor units; the cash and bonus parts get sensors of their own in the
credit's currency, the other two ride on the credit sensor. The loyalty programme's "boxes until
a free box" comes from the subscription. All account details are made up.
"""

from __future__ import annotations

from types import SimpleNamespace

from homeassistant.components.sensor import SensorDeviceClass

from custom_components.hellofresh.models import HelloFreshAccountData, HelloFreshSubscription
from custom_components.hellofresh.sensor import SENSORS, HelloFreshSensor
from custom_components.hellofresh.sensor_helpers import sensor_extra_state_attributes
from custom_components.hellofresh.services import _SUMMARY_KEYS, _get_account_summary
from test_client_fixes import CUSTOMER_UUID, SUB, Account, FakeHelloFresh, make_client

BALANCE = f"/gw/payments/customers/{CUSTOMER_UUID}/balance"


async def _poll_with_balance(balance: dict) -> HelloFreshAccountData:
    server = FakeHelloFresh()
    Account(server)
    server.route("GET", BALANCE, (200, balance))
    return await make_client(server).async_get_account_data()


async def test_the_balance_is_read_with_its_parts_in_major_units() -> None:
    data = await _poll_with_balance(
        {
            "amount": 2500,
            "cash": 1500,
            "bonus": 1000,
            "currencyCode": "USD",
            "restrictedAmount": 300,
            "cancellableCredits": 200,
        }
    )

    assert data.account_credit == 25.0
    assert data.account_credit_currency == "USD"
    assert (data.account_credit_cash, data.account_credit_bonus) == (15.0, 10.0)
    assert (data.account_credit_restricted, data.account_credit_cancellable) == (3.0, 2.0)


async def test_a_balance_without_some_parts_still_gives_the_others() -> None:
    """HelloFresh's app models restrictedAmount as optional and has no cancellableCredits."""
    data = await _poll_with_balance({"amount": 500, "cash": 0, "bonus": 500, "currencyCode": "EUR"})

    assert (data.account_credit_cash, data.account_credit_bonus) == (0.0, 5.0)
    assert data.account_credit_restricted is None
    assert data.account_credit_cancellable is None


async def test_a_balance_without_an_amount_is_not_read_at_all() -> None:
    data = await _poll_with_balance({"cash": 1500, "currencyCode": "USD"})
    assert data.account_credit is None
    assert data.account_credit_cash is None


# ---- the sensors -----------------------------------------------------------------------------


def _sensor(key: str, data: HelloFreshAccountData) -> HelloFreshSensor:
    description = next(item for item in SENSORS if item.key == key)
    coordinator = SimpleNamespace(
        data=data,
        config_entry=SimpleNamespace(entry_id="entry-1", title="HelloFresh"),
        client=SimpleNamespace(base_url="https://www.hellofresh.com"),
        last_update_success=True,
    )
    return HelloFreshSensor(coordinator, description)


def _credit_data(**fields) -> HelloFreshAccountData:
    return HelloFreshAccountData(
        account_credit=25.0, account_credit_currency="CAD", **fields
    ).finalize()


def test_cash_and_bonus_are_money_in_the_credits_currency() -> None:
    data = _credit_data(account_credit_cash=15.0, account_credit_bonus=10.0)
    for key, value in (("account_credit_cash", 15.0), ("account_credit_bonus", 10.0)):
        sensor = _sensor(key, data)
        assert sensor.entity_id == f"sensor.hellofresh_{key}"
        assert sensor.native_value == value
        assert sensor.device_class is SensorDeviceClass.MONETARY
        assert sensor.native_unit_of_measurement == "CAD"
        assert sensor.suggested_display_precision == 2
        # Like the credit itself: a balance, not a total or a measurement.
        assert sensor.state_class is None


def test_unknown_parts_read_unknown() -> None:
    data = HelloFreshAccountData().finalize()
    assert _sensor("account_credit_cash", data).native_value is None
    assert _sensor("account_credit_bonus", data).native_unit_of_measurement is None


def test_the_credit_sensor_carries_the_restricted_and_cancellable_parts() -> None:
    data = _credit_data(account_credit_restricted=3.0, account_credit_cancellable=2.0)
    assert sensor_extra_state_attributes("account_credit", data) == {
        "currency": "CAD",
        "restricted_amount": 3.0,
        "cancellable_credits": 2.0,
    }
    assert _sensor("account_credit", data).extra_state_attributes["restricted_amount"] == 3.0


def test_the_loyalty_counter_comes_from_the_subscription() -> None:
    client = make_client(FakeHelloFresh())
    subscription = client._subscription_from_raw_subscription(
        {"id": int(SUB), "customer": {"id": 1, "loyalty": {"boxesUntilNextFreebie": 3}}}
    )
    data = HelloFreshAccountData(subscriptions=[subscription]).finalize()

    sensor = _sensor("loyalty_boxes_until_free_box", data)
    assert sensor.native_value == 3
    assert sensor.native_unit_of_measurement is None

    nothing = HelloFreshAccountData(
        subscriptions=[HelloFreshSubscription(subscription_id=SUB)]
    ).finalize()
    assert _sensor("loyalty_boxes_until_free_box", nothing).native_value is None


async def test_the_account_summary_mirrors_the_new_sensors() -> None:
    assert {"account_credit_cash", "account_credit_bonus", "loyalty_boxes_until_free_box"} <= set(
        _SUMMARY_KEYS
    )
    data = HelloFreshAccountData(
        account_credit=25.0,
        account_credit_currency="USD",
        account_credit_cash=15.0,
        account_credit_bonus=10.0,
        subscriptions=[
            HelloFreshSubscription(subscription_id=SUB, loyalty_boxes_until_next_freebie=4)
        ],
    ).finalize()
    coordinator = SimpleNamespace(
        data=data,
        config_entry=SimpleNamespace(entry_id="entry-1", title="HelloFresh", options={}),
        client=SimpleNamespace(base_url="https://www.hellofresh.com"),
        delivery_in_progress=lambda: False,
    )

    summary = await _get_account_summary(coordinator, SimpleNamespace(data={}))

    assert summary["account_credit"] == 25.0
    assert summary["account_credit_cash"] == 15.0
    assert summary["account_credit_bonus"] == 10.0
    assert summary["account_credit_currency"] == "USD"
    assert summary["loyalty_boxes_until_free_box"] == 4


# ---- the plan's box sizes --------------------------------------------------------------------


async def test_box_sizes_count_meals_and_people_in_whole_numbers() -> None:
    server = FakeHelloFresh()
    Account(server)
    server.route(
        "GET",
        f"/gw/api/subscriptions/{SUB}/product_options",
        (
            200,
            {
                "items": [
                    {
                        "handle": "classic-box-unified",
                        "name": "Classic",
                        "products": [
                            {"handle": "US-CBU-3-2-0", "specs": {"meals": "3", "size": "2"}},
                            {"handle": "US-CBU-0-0-0", "specs": {"meals": 0, "size": -1}},
                            {"handle": "US-CBU-4-2-0", "specs": None, "price": 8000},
                        ],
                    }
                ]
            },
        ),
    )
    client = make_client(server)
    await client.async_get_account_data()

    options = {option["handle"]: option for option in await client.async_list_plan_options()}

    assert (options["US-CBU-3-2-0"]["meals"], options["US-CBU-3-2-0"]["servings"]) == (3, 2)
    assert (options["US-CBU-0-0-0"]["meals"], options["US-CBU-0-0-0"]["servings"]) == (None, None)
    assert options["US-CBU-4-2-0"]["meals"] is None
    assert options["US-CBU-4-2-0"]["price"] == 80.0
    assert options["US-CBU-3-2-0"]["family_handle"] == "classic-box-unified"
    assert options["US-CBU-3-2-0"]["family_name"] == "Classic"
