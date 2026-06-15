"""Unit tests for the HelloFresh API normalization layer."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from custom_components.hellofresh.api import (
    HelloFreshAccountData,
    HelloFreshAuthError,
    HelloFreshCapabilities,
    HelloFreshClient,
    HelloFreshError,
    HelloFreshNotImplementedError,
    HelloFreshOrder,
    HelloFreshRecipe,
    HelloFreshSubscription,
    HelloFreshWeek,
)
from custom_components.hellofresh.sensor_helpers import sensor_extra_state_attributes

# Home Assistant recorder drops state attributes larger than this many bytes.
_RECORDER_ATTR_CAP_BYTES = 16384


def test_week_needs_selection_respects_skip_and_counts() -> None:
    """Weeks should only need selection when active and incomplete."""
    assert (
        HelloFreshWeek(
            week_id="week-1",
            display_name="Week 1",
            meals_required=4,
            meals_selected=2,
        ).needs_selection
        is True
    )
    assert (
        HelloFreshWeek(
            week_id="week-2",
            display_name="Week 2",
            meals_required=4,
            meals_selected=2,
            is_skipped=True,
        ).needs_selection
        is False
    )


def _account_data_with_large_menu() -> HelloFreshAccountData:
    """Build account data whose week carries a full menu (well over the recorder cap)."""
    recipes = [
        HelloFreshRecipe(
            recipe_id=f"r{i}",
            name=f"Recipe number {i} with a fairly descriptive dish title",
            description="A long-ish description repeated to bulk up the recipe payload. " * 4,
            ingredients=[f"Ingredient {j} for recipe {i}" for j in range(15)],
            tags=["Vegetarian", "Quick", "Family Friendly", "Calorie Smart"],
            image_url=f"https://img.hellofresh.com/recipes/{i}/hero-image-large.jpg",
        )
        for i in range(40)
    ]
    week = HelloFreshWeek(
        week_id="2026-W25",
        display_name="Jun 17 - Jun 23",
        subscription_id="sub-1",
        delivery_date=date(2026, 6, 19),
        selection_deadline=datetime(2026, 6, 16, 18, 0),
        meals_required=3,
        meals_selected=1,
        slot_label="Fridays: 8AM - 8PM",
        recipes=recipes,
    )
    order = HelloFreshOrder(
        order_id="ord-1",
        week_id=week.week_id,
        status="scheduled",
        subscription_id="sub-1",
        delivery_date=week.delivery_date,
    )
    return HelloFreshAccountData(
        weeks=[week],
        orders=[order],
        subscriptions=[
            HelloFreshSubscription(subscription_id="sub-1", account_id="acct-1", meals_required=3)
        ],
        capabilities=HelloFreshCapabilities(supports_meal_selection=True),
    ).finalize()


def test_sensor_attributes_stay_under_recorder_cap_with_large_menu() -> None:
    """No sensor attribute payload may exceed the recorder's 16 KB cap.

    Regression: a week's full recipe catalog (from the authenticated menu API) embedded in
    single-week sensor attributes blew the cap and the recorder dropped the attributes.
    """
    import json

    data = _account_data_with_large_menu()

    # Sanity: the full week serialization really is over the cap, so the test is meaningful.
    assert len(json.dumps(data.weeks[0].as_dict()).encode()) > _RECORDER_ATTR_CAP_BYTES

    affected_keys = [
        "next_order_status",
        "next_box_total_price",
        "next_delivery_subscription",
        "next_delivery_slot",
        "next_delivery_date",
        "next_delivery_week",
        "next_selection_deadline",
        "selected_meal_count",
        "required_meal_count",
        "weeks_needing_selection",
        "last_delivery_date",
    ]
    for key in affected_keys:
        attributes = sensor_extra_state_attributes(key, data)
        if attributes is None:
            continue
        size = len(json.dumps(attributes, default=str).encode())
        assert size <= _RECORDER_ATTR_CAP_BYTES, f"{key} attributes are {size} bytes (over cap)"


def test_week_summary_dict_omits_recipes_but_full_dict_keeps_them() -> None:
    """as_summary_dict drops recipes/action lists; as_dict (diagnostics) keeps them."""
    data = _account_data_with_large_menu()
    week = data.weeks[0]

    summary = week.as_summary_dict()
    # The heavy lists are dropped to stay under the recorder cap.
    assert "recipes" not in summary
    assert "allowed_actions" not in summary
    # The small, bounded one-off delivery-date options ARE kept (useful, recorder-safe).
    assert "available_one_off_options" in summary
    # Scalar metadata the dashboard/automations actually read is preserved.
    assert summary["week_id"] == "2026-W25"
    assert summary["meals_required"] == 3
    assert summary["slot_label"] == "Fridays: 8AM - 8PM"

    full = week.as_dict()
    assert len(full["recipes"]) == 40
    # Full serialization (diagnostics + serialized_weeks) still carries the catalog.
    assert data.serialized_weeks[0]["recipes"]


def test_account_data_finalize_builds_serialized_views() -> None:
    """Serialized attribute payloads should be derived in one place."""
    today = date.today()
    current_week_id = f"{today.isocalendar().year}-W{today.isocalendar().week:02d}"
    last_week = today - timedelta(days=7)
    last_week_id = f"{last_week.isocalendar().year}-W{last_week.isocalendar().week:02d}"
    week = HelloFreshWeek(
        week_id=current_week_id,
        display_name="Jun 10 - Jun 16",
        subscription_id="sub-1",
        delivery_date=today,
        selection_deadline=datetime.combine(today, datetime.min.time()).replace(hour=18),
        meals_required=3,
        meals_selected=1,
        recipes=[
            HelloFreshRecipe(
                recipe_id="r1",
                name="Pasta",
                ingredients=["Pasta", "Mushrooms"],
                tags=["Vegetarian"],
            )
        ],
    )
    order = HelloFreshOrder(
        order_id="ord-1",
        week_id=week.week_id,
        status="scheduled",
        subscription_id="sub-1",
        delivery_date=week.delivery_date,
        tracking_number="TRACK123",
    )
    public_menu = HelloFreshWeek(
        week_id="public-current",
        display_name="Current Menu",
        recipes=[HelloFreshRecipe(recipe_id="m1", name="Tacos")],
        source="public_menu",
    )

    data = HelloFreshAccountData(
        weeks=[week],
        orders=[order],
        past_delivery_weeks=[
            HelloFreshWeek(
                week_id=last_week_id,
                display_name="Jun 03 - Jun 09",
                subscription_id="sub-1",
                delivery_date=last_week,
                status="delivered",
                source="past_deliveries",
            )
        ],
        public_menu_weeks=[public_menu],
        subscriptions=[
            HelloFreshSubscription(
                subscription_id="sub-1",
                account_id="acct-1",
                display_name="Classic Plan",
                meals_required=3,
            )
        ],
        capabilities=HelloFreshCapabilities(
            supports_meal_selection=True,
        ),
    ).finalize()

    assert data.serialized_orders[0]["order_id"] == "ord-1"
    assert data.serialized_orders[0]["tracking_number"] == "TRACK123"
    assert data.serialized_weeks_needing_selection[0]["week_id"] == current_week_id
    assert data.serialized_weeks_needing_selection[0]["subscription_id"] == "sub-1"
    assert data.serialized_public_menu_weeks[0]["source"] == "public_menu"
    assert data.serialized_past_delivery_weeks[0]["source"] == "past_deliveries"
    assert data.serialized_subscriptions[0]["display_name"] == "Classic Plan"
    assert data.next_selection_week is not None
    assert data.serialized_weeks_needing_selection[0]["week_id"] == current_week_id
    assert data.next_selection_week.week_id == current_week_id
    assert data.delivery_count_this_week == 1
    assert data.past_delivery_count == 1
    assert data.last_delivery_week is not None
    assert data.last_delivery_week.week_id == last_week_id


def test_next_order_skips_past_deliveries_and_picks_earliest_future() -> None:
    """next_order/upcoming_orders must resolve to future deliveries, not the oldest one.

    Regression: the deliveries endpoint returns a wide window (≈12 weeks back to 1 week
    ahead), so the order list contains many past orders. next_order must filter to
    delivery_date >= today and pick the earliest *future* order, not orders[0] (which is
    the oldest historical delivery).
    """
    today = date.today()
    orders = [
        HelloFreshOrder(
            order_id="past-old",
            week_id="w-old",
            status="delivered",
            subscription_id="sub-1",
            delivery_date=today - timedelta(weeks=10),
        ),
        HelloFreshOrder(
            order_id="past-recent",
            week_id="w-recent",
            status="delivered",
            subscription_id="sub-1",
            delivery_date=today - timedelta(days=3),
        ),
        HelloFreshOrder(
            order_id="future-next",
            week_id="w-next",
            status="scheduled",
            subscription_id="sub-1",
            delivery_date=today + timedelta(days=4),
        ),
        HelloFreshOrder(
            order_id="future-later",
            week_id="w-later",
            status="scheduled",
            subscription_id="sub-1",
            delivery_date=today + timedelta(days=11),
        ),
    ]
    data = HelloFreshAccountData(orders=orders).finalize()

    assert data.next_order is not None
    assert data.next_order.order_id == "future-next"
    assert data.next_order.delivery_date == today + timedelta(days=4)
    # upcoming_orders is future-only and sorted ascending.
    assert [o.order_id for o in data.upcoming_orders] == ["future-next", "future-later"]


def test_upcoming_orders_exclude_skipped_weeks() -> None:
    """A skipped upcoming week ships no box, so it drops out of upcoming_orders/next_order."""
    today = date.today()
    weeks = [
        HelloFreshWeek(
            week_id="w-next",
            display_name="Next",
            subscription_id="sub-1",
            delivery_date=today + timedelta(days=4),
            is_skipped=True,  # skipped: no delivery this week
        ),
        HelloFreshWeek(
            week_id="w-later",
            display_name="Later",
            subscription_id="sub-1",
            delivery_date=today + timedelta(days=11),
        ),
    ]
    orders = [
        HelloFreshOrder(
            order_id="skipped-next",
            week_id="w-next",
            status="skipped",
            subscription_id="sub-1",
            delivery_date=today + timedelta(days=4),
        ),
        HelloFreshOrder(
            order_id="future-later",
            week_id="w-later",
            status="scheduled",
            subscription_id="sub-1",
            delivery_date=today + timedelta(days=11),
        ),
    ]
    data = HelloFreshAccountData(weeks=weeks, orders=orders).finalize()

    # The skipped week is excluded; next real delivery is the later week.
    assert [o.order_id for o in data.upcoming_orders] == ["future-later"]
    assert data.next_order is not None
    assert data.next_order.order_id == "future-later"


def test_next_order_includes_todays_delivery() -> None:
    """A delivery scheduled for today still counts as upcoming."""
    today = date.today()
    data = HelloFreshAccountData(
        orders=[
            HelloFreshOrder(
                order_id="yesterday",
                week_id="w-y",
                status="delivered",
                subscription_id="sub-1",
                delivery_date=today - timedelta(days=1),
            ),
            HelloFreshOrder(
                order_id="today",
                week_id="w-t",
                status="scheduled",
                subscription_id="sub-1",
                delivery_date=today,
            ),
        ]
    ).finalize()

    assert data.next_order is not None
    assert data.next_order.order_id == "today"


def test_normalize_past_delivery_payload_extracts_recipe_history() -> None:
    """Delivered-history payloads should retain recipe summaries from the account API."""
    client = HelloFreshClient(session=object())  # type: ignore[arg-type]
    subscription = HelloFreshSubscription(
        subscription_id="sub-1",
        account_id="acct-1",
        locale="en-US",
        meals_required=3,
    )

    weeks = client._normalize_past_delivery_payload(
        {
            "data": [
                {
                    "week": "2026-W23",
                    "delivery_date": "2026-06-08T12:00:00Z",
                    "subscription_id": "sub-1",
                    "recipe_count": 3,
                    "recipes": [
                        {
                            "id": "recipe-1",
                            "name": "Creamy Mushroom Pasta",
                            "headline": "Fast and cozy",
                            "prep_time": 10,
                            "ingredients": [{"name": "Pasta"}, {"name": "Mushrooms"}],
                        }
                    ],
                }
            ]
        },
        [subscription],
    )

    assert len(weeks) == 1
    assert weeks[0].week_id == "2026-W23"
    assert weeks[0].source == "past_deliveries"
    assert weeks[0].delivery_date == date(2026, 6, 8)
    assert weeks[0].recipes[0].name == "Creamy Mushroom Pasta"
    assert weeks[0].recipes[0].ingredients == ["Pasta", "Mushrooms"]


def test_normalize_past_delivery_payload_preserves_holiday_and_one_off_metadata() -> None:
    """Past-delivery payloads should retain richer week metadata from the HAR."""
    client = HelloFreshClient(session=None)  # type: ignore[arg-type]
    weeks = client._normalize_past_delivery_payload(
        {
            "items": [
                {
                    "id": "2026-W22",
                    "deliveryDate": "2026-05-25T12:00:00-0700",
                    "subStatus": "RATING",
                    "state": "DELIVERED",
                    "actionable": False,
                    "prepaid": False,
                    "deliveryBlocked": False,
                    "holidayDelivery": "2026-05-24T12:00:00-0700",
                    "isHolidayShiftVisible": True,
                    "allowedActions": {
                        "updateDeliveryAddress": False,
                        "updateDeliveryWeekday": False,
                    },
                    "availableOneOffOptions": [
                        {"handle": "US-1-0800-2000", "deliveryDate": "2026-05-25"}
                    ],
                    "deliveryOption": {
                        "deliveryName": "Sundays: 8AM - 8PM",
                        "type": "PLAN",
                    },
                    "recipes": [{"id": "recipe-1", "name": "Pasta"}],
                }
            ]
        },
        [HelloFreshSubscription(subscription_id="sub-1")],
    )

    assert len(weeks) == 1
    assert weeks[0].holiday_delivery_date == date(2026, 5, 24)
    assert weeks[0].holiday_shift_visible is True
    assert weeks[0].delivery_state == "DELIVERED"
    assert weeks[0].sub_status == "RATING"
    assert weeks[0].allowed_actions["updateDeliveryAddress"] is False
    assert weeks[0].available_one_off_options == [
        {"handle": "US-1-0800-2000", "delivery_date": "2026-05-25"}
    ]


def test_account_data_loads_profile_metrics_and_past_delivery_history() -> None:
    """Account refresh should retain authenticated profile and history data from extra endpoints."""
    client = HelloFreshClient(
        session=object(),  # type: ignore[arg-type]
        access_token="token",
        enable_public_menu_fallback=False,
    )
    subscription = HelloFreshSubscription(
        subscription_id="sub-1",
        account_id="acct-1",
        locale="en-US",
        meals_required=3,
    )

    async def fake_get_subscriptions():
        return [subscription]

    async def fake_get_boxes_received():
        return 14

    async def fake_get_past_delivery_weeks(_subscriptions):
        return [
            HelloFreshWeek(
                week_id="2026-W23",
                display_name="Week 23",
                subscription_id="sub-1",
                delivery_date=date(2026, 6, 8),
                status="delivered",
                source="past_deliveries",
            )
        ]

    async def fake_get_upcoming_deliveries(_subscription):
        return ([], [])

    async def fake_get_account_menu_data(_subscriptions, _weeks):
        return None

    async def fake_enrich_tracking(*_args, **_kwargs):
        return None

    async def fake_enrich_subscription_payments(*_args, **_kwargs):
        return None

    client._async_get_subscriptions = fake_get_subscriptions  # type: ignore[method-assign]
    client._async_get_boxes_received = fake_get_boxes_received  # type: ignore[method-assign]
    client._async_get_past_delivery_weeks = fake_get_past_delivery_weeks  # type: ignore[method-assign]
    client._async_get_upcoming_deliveries = fake_get_upcoming_deliveries  # type: ignore[method-assign]
    client._async_get_account_menu_data = fake_get_account_menu_data  # type: ignore[method-assign]
    client._async_enrich_order_tracking = fake_enrich_tracking  # type: ignore[method-assign]
    client._async_enrich_subscription_payment_dates = fake_enrich_subscription_payments  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(client.async_get_account_data())

    assert result.boxes_received == 14
    assert result.past_delivery_count == 1
    assert result.last_delivery_week is not None
    assert result.last_delivery_week.week_id == "2026-W23"


def test_initial_account_payloads_are_fetched_concurrently() -> None:
    """Independent account payload calls should start before any one waits to finish."""
    client = HelloFreshClient(session=None)  # type: ignore[arg-type]
    subscriptions = [
        HelloFreshSubscription(subscription_id="sub-1"),
        HelloFreshSubscription(subscription_id="sub-2"),
    ]
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    release = asyncio.Event()
    started: set[str] = set()

    def mark_started(name: str) -> None:
        started.add(name)
        if started == {"boxes", "history", "delivery:sub-1", "delivery:sub-2"}:
            release.set()

    async def fake_get_boxes_received():
        mark_started("boxes")
        await release.wait()
        return 14

    async def fake_get_past_delivery_weeks(_subscriptions):
        mark_started("history")
        await release.wait()
        return [
            HelloFreshWeek(
                week_id="past-week",
                display_name="Past week",
                subscription_id="sub-1",
            )
        ]

    async def fake_get_upcoming_deliveries(subscription):
        mark_started(f"delivery:{subscription.subscription_id}")
        await release.wait()
        return (
            [
                HelloFreshWeek(
                    week_id=f"week-{subscription.subscription_id}",
                    display_name="Upcoming week",
                    subscription_id=subscription.subscription_id,
                )
            ],
            [
                HelloFreshOrder(
                    order_id=f"order-{subscription.subscription_id}",
                    week_id=f"week-{subscription.subscription_id}",
                    status="scheduled",
                    subscription_id=subscription.subscription_id,
                )
            ],
        )

    client._async_get_boxes_received = fake_get_boxes_received  # type: ignore[method-assign]
    client._async_get_past_delivery_weeks = fake_get_past_delivery_weeks  # type: ignore[method-assign]
    client._async_get_upcoming_deliveries = fake_get_upcoming_deliveries  # type: ignore[method-assign]

    result = loop.run_until_complete(
        asyncio.wait_for(client._async_get_initial_account_payloads(subscriptions), 1)
    )

    boxes_received, past_weeks, weeks, orders, payload_found = result
    assert boxes_received == 14
    assert len(past_weeks) == 1
    assert len(weeks) == 2
    assert len(orders) == 2
    assert payload_found is True


def test_normalize_weeks_payload_extracts_tracking_and_meals() -> None:
    """Delivery payloads should map into stable week and order models."""
    client = HelloFreshClient(session=None)  # type: ignore[arg-type]
    subscription = HelloFreshSubscription(
        subscription_id="sub-1",
        account_id="acct-1",
        locale="en-US",
        meals_required=4,
    )
    payload = {
        "items": [
            {
                "id": "week-123",
                "label": "Jun 10 - Jun 16",
                "deliveryDate": "2026-06-12",
                "selectionDeadline": "2026-06-09T18:00:00Z",
                "status": "packed",
                "mealsRequired": 3,
                "mealsSelected": 2,
                "meals": [
                    {
                        "id": "recipe-1",
                        "name": "Creamy Mushroom Pasta",
                        "selected": True,
                        "imageUrl": "https://example.com/recipe.jpg",
                        "ingredients": [{"name": "Mushrooms"}, {"name": "Pasta"}],
                        "tags": ["Veggie", "Quick"],
                        "nutrition": {"calories": "720"},
                    }
                ],
                "tracking": {
                    "trackingUrl": "https://carrier.example/track/123",
                    "trackingNumber": "TRACK123",
                    "trackingStatus": "in_transit",
                    "carrierName": "Carrier",
                },
                "price": "64.95",
                "currencyCode": "USD",
            }
        ]
    }

    weeks, orders = client._normalize_weeks_payload(payload, subscription=subscription)

    assert len(weeks) == 1
    assert weeks[0].needs_selection is True
    assert weeks[0].recipes[0].name == "Creamy Mushroom Pasta"
    assert weeks[0].recipes[0].ingredients == ["Mushrooms", "Pasta"]
    assert weeks[0].recipes[0].tags == ["Veggie", "Quick"]
    assert weeks[0].recipes[0].calories_kcal == 720.0
    assert weeks[0].subscription_id == "sub-1"
    assert len(orders) == 1
    assert orders[0].tracking_number == "TRACK123"
    assert orders[0].tracking_status == "in_transit"
    assert orders[0].total_price == 64.95
    assert orders[0].subscription_id == "sub-1"


def test_normalize_weeks_payload_preserves_action_and_schedule_metadata() -> None:
    """Upcoming-delivery payloads should keep holiday, one-off, and action flags."""
    client = HelloFreshClient(session=None)  # type: ignore[arg-type]
    subscription = HelloFreshSubscription(subscription_id="sub-1", locale="en-US")
    payload = {
        "items": [
            {
                "id": "2026-W25",
                "deliveryDate": "2026-06-15T12:00:00-0700",
                "cutoffDate": "2026-06-10T23:59:59-0700",
                "status": "RUNNING",
                "subStatus": "NULL",
                "state": "RUNNING",
                "actionable": True,
                "prepaid": False,
                "deliveryBlocked": False,
                "holidayDelivery": None,
                "holidayMessage": None,
                "isHolidayShiftVisible": False,
                "allowedActions": {
                    "mealSwap": True,
                    "updateDeliveryAddress": True,
                    "updateDeliveryWeekday": True,
                    "pause": True,
                    "oneOffChange": True,
                    "updatePaymentMethod": True,
                    "donate": False,
                },
                "availableOneOffOptions": [
                    {"handle": "US-1-0800-2000", "deliveryDate": "2026-06-15"},
                    {"handle": "US-2-0800-2000", "deliveryDate": "2026-06-16"},
                ],
                "deliveryOption": {
                    "deliveryName": "Mondays: 8AM - 8PM",
                    "type": "PLAN",
                },
            }
        ]
    }

    weeks, _ = client._normalize_weeks_payload(payload, subscription=subscription)

    assert len(weeks) == 1
    assert weeks[0].actionable is True
    assert weeks[0].sub_status == "NULL"
    assert weeks[0].delivery_state == "RUNNING"
    assert weeks[0].allowed_actions["pause"] is True
    assert weeks[0].allowed_actions["updatePaymentMethod"] is True
    assert weeks[0].available_one_off_options[1]["delivery_date"] == "2026-06-16"


def test_normalize_weeks_payload_accepts_snake_case_tracking_fields() -> None:
    """Deliveries may expose tracking fields with SCM-style snake_case names."""
    client = HelloFreshClient(session=None)  # type: ignore[arg-type]
    subscription = HelloFreshSubscription(
        subscription_id="sub-1",
        account_id="acct-1",
        locale="en-US",
    )
    payload = {
        "items": [
            {
                "id": "2026-W24",
                "deliveryDate": "2026-06-08T12:00:00-0700",
                "status": "DELIVERED",
                "tracking": {
                    "tracking_link": "https://www.hellofresh.com/delivery-tracking/6c11d560-8cc1-4190-bd71-dd8fa51f9d9a",
                    "tracking_code": "DUS1441132100520980",
                },
            }
        ]
    }

    _, orders = client._normalize_weeks_payload(payload, subscription=subscription)

    assert len(orders) == 1
    assert orders[0].tracking_url == (
        "https://www.hellofresh.com/delivery-tracking/6c11d560-8cc1-4190-bd71-dd8fa51f9d9a"
    )
    assert orders[0].tracking_number == "DUS1441132100520980"
    assert orders[0].carrier is None


def test_normalize_weeks_payload_extracts_nested_delivery_recipes_and_counts() -> None:
    """Delivery payloads may wrap recipes and counts in nested containers."""
    client = HelloFreshClient(session=None)  # type: ignore[arg-type]
    subscription = HelloFreshSubscription(
        subscription_id="sub-1",
        account_id="acct-1",
        locale="en-US",
    )
    payload = {
        "items": [
            {
                "deliveryWeek": "2026-W25",
                "deliveryDate": "2026-06-15",
                "deadline": "2026-06-10T23:59:59-07:00",
                "deliveryStatus": "RUNNING",
                "selection": {
                    "requiredMealCount": 2,
                    "selectedMealCount": 1,
                    "entries": {
                        "nodes": [
                            {"id": "recipe-1", "title": "Pasta", "selected": True},
                            {"id": "recipe-2", "name": "Tacos", "selected": False},
                        ]
                    },
                },
            }
        ]
    }

    weeks, orders = client._normalize_weeks_payload(payload, subscription=subscription)

    assert len(weeks) == 1
    assert weeks[0].week_id == "2026-W25"
    assert weeks[0].recipes[0].name == "Pasta"
    assert len(weeks[0].recipes) == 2
    assert weeks[0].meals_required == 2
    assert weeks[0].meals_selected == 1
    assert weeks[0].needs_selection is True
    assert len(orders) == 1
    assert orders[0].status == "RUNNING"


def test_normalize_weeks_payload_extracts_meals_required_from_product_specs() -> None:
    """Delivery payloads may expose meal counts under product specs."""
    client = HelloFreshClient(session=None)  # type: ignore[arg-type]
    subscription = HelloFreshSubscription(
        subscription_id="sub-1",
        account_id="acct-1",
        locale="en-US",
    )
    payload = {
        "items": [
            {
                "id": "2026-W25",
                "deliveryDate": "2026-06-15",
                "cutoffDate": "2026-06-10T23:59:59-07:00",
                "status": "RUNNING",
                "product": {
                    "displayName": "Classic Box",
                    "specs": {"meals": 3},
                },
                "deliveryOption": {
                    "deliveryName": "Mon 8:00 AM - 8:00 PM",
                    "type": "standard",
                    "priceInCents": 1299,
                },
            }
        ]
    }

    weeks, orders = client._normalize_weeks_payload(payload, subscription=subscription)

    assert len(weeks) == 1
    assert weeks[0].display_name == "Classic Box"
    assert weeks[0].menu_title == "Classic Box"
    assert weeks[0].meals_required == 3
    assert weeks[0].slot_label == "Mon 8:00 AM - 8:00 PM"
    assert weeks[0].shipping_method == "standard"
    assert len(orders) == 1
    assert orders[0].total_price == 12.99


def test_order_total_prefers_subtotal_plus_shipping_and_defaults_currency() -> None:
    """Split subtotal and shipping fields should be combined into the visible total."""
    client = HelloFreshClient(session=None, country="us")  # type: ignore[arg-type]
    subscription = HelloFreshSubscription(
        subscription_id="sub-1",
        account_id="acct-1",
        locale="en-US",
    )
    payload = {
        "items": [
            {
                "id": "2026-W25",
                "deliveryDate": "2026-06-15",
                "status": "RUNNING",
                "pricing": {
                    "subTotalInCents": 5999,
                    "shippingAmountInCents": 1099,
                },
                "deliveryOption": {
                    "priceInCents": 1099,
                },
            }
        ]
    }

    _, orders = client._normalize_weeks_payload(payload, subscription=subscription)

    assert len(orders) == 1
    assert orders[0].total_price == 70.98
    assert orders[0].currency == "USD"


def test_order_total_prefers_grand_total_over_shipping_only_cents() -> None:
    """A grand total should win over nested shipping-only price fields."""
    client = HelloFreshClient(session=None, country="us")  # type: ignore[arg-type]
    subscription = HelloFreshSubscription(
        subscription_id="sub-1",
        account_id="acct-1",
        locale="en-US",
    )
    payload = {
        "items": [
            {
                "id": "2026-W25",
                "deliveryDate": "2026-06-15",
                "status": "RUNNING",
                "grandTotal": "82.47",
                "deliveryOption": {
                    "priceInCents": 1299,
                },
            }
        ]
    }

    _, orders = client._normalize_weeks_payload(payload, subscription=subscription)

    assert len(orders) == 1
    assert orders[0].total_price == 82.47
    assert orders[0].currency == "USD"


def test_order_total_falls_back_to_product_price_plus_special_fee_for_future_delivery() -> None:
    """Upcoming deliveries may only expose box price and special fee on the product."""
    client = HelloFreshClient(session=None, country="us")  # type: ignore[arg-type]
    subscription = HelloFreshSubscription(
        subscription_id="sub-1",
        account_id="acct-1",
        locale="en-US",
    )
    payload = {
        "items": [
            {
                "id": "2026-W25",
                "deliveryDate": "2026-06-15",
                "status": "RUNNING",
                "product": {
                    "price": 6594,
                    "specialFee": 1099,
                    "shippingPrice": 0,
                },
                "deliveryOption": {
                    "priceInCents": 0,
                },
            }
        ]
    }

    _, orders = client._normalize_weeks_payload(payload, subscription=subscription)

    assert len(orders) == 1
    assert orders[0].total_price == 76.93
    assert orders[0].currency == "USD"


def test_normalize_menu_weeks_infers_selected_meals_from_selection_quantity() -> None:
    """Menu payloads should treat selection.quantity as selected state and count."""
    client = HelloFreshClient(session=None)  # type: ignore[arg-type]
    subscription = HelloFreshSubscription(
        subscription_id="sub-1",
        account_id="acct-1",
        locale="en-US",
        meals_required=3,
    )

    weeks = client._normalize_menu_weeks(
        [
            {
                "week": "2026-W25",
                "meals": [
                    {
                        "index": 11,
                        "selection": {"quantity": 1, "limit": 2},
                        "recipe": {"id": "recipe-1", "name": "Pasta"},
                    },
                    {
                        "index": 18,
                        "selection": {"quantity": 1, "limit": 2},
                        "recipe": {"id": "recipe-2", "name": "Tacos"},
                    },
                    {
                        "index": 20,
                        "selection": {"quantity": 0, "limit": 2},
                        "recipe": {"id": "recipe-3", "name": "Burger"},
                    },
                ],
            }
        ],
        subscription=subscription,
    )

    assert len(weeks) == 1
    assert weeks[0].meals_selected == 2
    assert [recipe.is_selected for recipe in weeks[0].recipes] == [True, True, False]


def test_normalize_menu_weeks_reads_menus_service_courses_container() -> None:
    """menus-service items wrap recipes in a ``courses`` list (each with a nested recipe)."""
    client = HelloFreshClient(session=None)  # type: ignore[arg-type]
    subscription = HelloFreshSubscription(subscription_id="sub-1", locale="en-US")

    # Mirrors the /gw/menus-service/menus item shape from the HAR capture.
    weeks = client._normalize_menu_weeks(
        [
            {
                "id": "menu-week-id",
                "week": "2026-W27",
                "courses": [
                    {"index": 1, "recipe": {"id": "r-1", "name": "Garlicky Chicken"}},
                    {"index": 2, "recipe": {"id": "r-2", "name": "Beef Tacos"}},
                ],
            }
        ],
        subscription=subscription,
    )

    assert len(weeks) == 1
    assert [recipe.name for recipe in weeks[0].recipes] == ["Garlicky Chicken", "Beef Tacos"]


def test_subscription_normalization_accepts_nested_plan_metadata() -> None:
    """Subscription payloads may expose plan metadata under renamed nested objects."""
    client = HelloFreshClient(session=None)  # type: ignore[arg-type]

    subscription = client._subscription_from_raw_subscription(
        {
            "id": "sub-1",
            "customer": {"id": "acct-1", "locale": "en-US"},
            "subscriptionPlan": {
                "displayName": "Family Plan",
                "recipesPerWeek": 4,
                "servings": 2,
            },
        }
    )

    assert subscription.display_name == "Family Plan"
    assert subscription.plan_name == "Family Plan"
    assert subscription.meals_required == 4
    assert subscription.servings == 2


def test_subscription_normalization_reads_product_type_specs() -> None:
    """Subscription payloads may expose meals and servings under productType specs."""
    client = HelloFreshClient(session=None)  # type: ignore[arg-type]

    subscription = client._subscription_from_raw_subscription(
        {
            "id": "6959884",
            "customer": {"id": "acct-1", "locale": "en-US"},
            "productType": {
                "productName": "Classic - 3 meals per week for 2 people",
                "specs": {
                    "meals": 3,
                    "size": 2,
                },
            },
        }
    )

    assert subscription.meals_required == 3
    assert subscription.servings == 2


def test_subscription_normalization_formats_delivery_address() -> None:
    """Subscription payloads should expose a compact delivery address string."""
    client = HelloFreshClient(session=None)  # type: ignore[arg-type]

    subscription = client._subscription_from_raw_subscription(
        {
            "id": "sub-1",
            "customer": {"id": "acct-1", "locale": "en-US"},
            "shippingAddress": {
                "address1": "62 Leonard St",
                "city": "Gloucester",
                "postcode": "01930",
                "region": {"code": "MA", "name": "Massachusetts"},
            },
        }
    )

    assert subscription.delivery_address == "62 Leonard St, Gloucester, MA, 01930"


def test_subscription_normalization_preserves_settings_metadata() -> None:
    """Subscription settings payloads should retain operational account metadata."""
    client = HelloFreshClient(session=None)  # type: ignore[arg-type]

    subscription = client._subscription_from_raw_subscription(
        {
            "id": "6959884",
            "status": "active",
            "customer": {
                "id": "acct-1",
                "locale": "en-US",
                "loyalty": {
                    "value": 335,
                    "boxesUntilNextFreebie": 2,
                },
            },
            "paymentMethod": "Credit Card",
            "paymentGateway": "Braintree",
            "couponCode": None,
            "preset": "quick",
            "deliveryWeekday": 1,
            "nextDelivery": "2026-06-15T00:00:00-0700",
            "nextDeliveryWeek": "2026-W25",
            "nextCutoffDate": "2026-06-10T23:59:59-0700",
            "nextModifiableDeliveryDate": "2026-06-15T00:00:00-0700",
            "nextModifiableDeliveryWeek": "2026-W25",
            "nextDeliveryTime": "US-1-0800-2000",
            "firstBoxDelivered": True,
        }
    )

    assert subscription.preset == "quick"
    assert subscription.delivery_weekday == 1
    assert subscription.next_delivery == date(2026, 6, 15)
    assert subscription.next_delivery_week == "2026-W25"
    assert subscription.next_cutoff_date is not None
    assert subscription.payment_method == "Credit Card"
    assert subscription.payment_gateway == "Braintree"
    assert subscription.first_box_delivered is True
    assert subscription.loyalty_boxes_received == 335
    assert subscription.loyalty_boxes_until_next_freebie == 2


def test_enrich_subscription_payment_dates_from_orders() -> None:
    """Subscription payment dates should come from real order creation timestamps."""
    client = HelloFreshClient(
        session=object(),  # type: ignore[arg-type]
        access_token="token",
        enable_public_menu_fallback=False,
    )
    subscription = HelloFreshSubscription(
        subscription_id="6959884",
        account_id="15259216",
        locale="en-US",
        next_cutoff_date=datetime(2026, 6, 10, 23, 59, 59, tzinfo=timezone(timedelta(hours=-7))),
        raw={"customer": {"uuid": "customer-uuid"}},
    )

    class DummyResponse:
        status = 200

    requests: list[dict[str, object | None]] = []

    async def fake_api_get(path: str, params=None, extra_headers=None):
        requests.append({"path": path, "params": params, "extra_headers": extra_headers})
        return DummyResponse()

    async def fake_response_json(_response):
        return {
            "items": [
                {
                    "createdAt": "2026-06-04T00:13:06-0700",
                    "orderLines": [
                        {
                            "deliveryDate": "2026-06-08T00:00:00-0700",
                            "subscription": {"id": "6959884"},
                        }
                    ],
                }
            ]
        }

    client._async_api_get = fake_api_get  # type: ignore[method-assign]
    client._async_response_json = fake_response_json  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(client._async_enrich_subscription_payment_dates([subscription]))

    assert requests == [
        {
            "path": "/gw/api/customers/me/orders",
            "params": {"country": "us", "locale": "en-US", "limit": 200},
            "extra_headers": None,
        }
    ]
    assert subscription.recent_payment_date == date(2026, 6, 4)
    assert subscription.next_payment_date == date(2026, 6, 11)


def test_summarize_payload_includes_nested_first_item_structure() -> None:
    """Payload diagnostics should expose nested keys for the first returned item."""
    client = HelloFreshClient(session=None)  # type: ignore[arg-type]

    summary = client._summarize_payload(
        {
            "items": [
                {
                    "deliveryWeek": "2026-W25",
                    "selection": {
                        "requiredMealCount": 2,
                        "entries": {
                            "nodes": [
                                {"id": "recipe-1", "name": "Pasta"},
                            ]
                        },
                    },
                }
            ]
        }
    )

    assert summary["items_count"] == 1
    first_item = summary["items_first_item"]
    assert isinstance(first_item, dict)
    assert first_item["deliveryWeek"] == "str"
    assert first_item["selection"]["type"] == "dict"
    assert "entries" in first_item["selection"]["keys"]
    assert "deliveryWeek" in summary["items_first_item_keys"]
    assert any(path.startswith("selection") for path in summary["items_interesting_paths"])


def test_account_data_exposes_skipped_week_and_capabilities() -> None:
    """Expanded account data should expose capability and skipped-week helpers."""
    data = HelloFreshAccountData(
        weeks=[
            HelloFreshWeek(
                week_id="week-1",
                display_name="Week 1",
                subscription_id="sub-1",
                delivery_date=date(2026, 6, 12),
                meals_required=3,
                meals_selected=1,
            ),
            HelloFreshWeek(
                week_id="week-2",
                display_name="Week 2",
                subscription_id="sub-1",
                delivery_date=date(2026, 6, 19),
                meals_required=3,
                meals_selected=3,
                is_skipped=True,
            ),
        ],
        subscriptions=[HelloFreshSubscription(subscription_id="sub-1")],
        capabilities=HelloFreshCapabilities(
            supports_meal_selection=True,
            using_public_menu_fallback=True,
        ),
    ).finalize()

    assert data.subscription_count == 1
    assert data.next_skipped_week is not None
    assert data.next_skipped_week.week_id == "week-2"
    assert data.capabilities.as_dict()["supports_write_actions"] is True


def test_account_data_finalize_prefers_latest_tracked_order() -> None:
    """Tracked shipment helpers should point at the most recent tracked order."""
    data = HelloFreshAccountData(
        orders=[
            HelloFreshOrder(
                order_id="old",
                week_id="2026-W21",
                status="delivered",
                delivery_date=date(2026, 5, 18),
                tracking_number="OLD",
            ),
            HelloFreshOrder(
                order_id="current",
                week_id="2026-W24",
                status="delivered",
                delivery_date=date(2026, 6, 8),
                tracking_number="NEW",
            ),
        ]
    ).finalize()

    assert data.tracked_order is not None
    assert data.tracked_order.order_id == "current"


def test_account_data_finalize_prefers_concrete_tracking_over_state_only_order() -> None:
    """A real tracked shipment should beat a later order with only generic status."""
    data = HelloFreshAccountData(
        orders=[
            HelloFreshOrder(
                order_id="delivered-box",
                week_id="2026-W24",
                status="delivered",
                delivery_date=date(2026, 6, 8),
                tracking_number="DUS1441132100520980",
                tracking_status="DELIVERED",
                carrier="DDASH",
            ),
            HelloFreshOrder(
                order_id="future-box",
                week_id="2026-W25",
                status="RUNNING",
                delivery_date=date(2026, 6, 15),
                tracking_status="RUNNING",
            ),
        ]
    ).finalize()

    assert data.tracked_order is not None
    assert data.tracked_order.order_id == "delivered-box"


def test_account_data_finalize_caches_serialized_weeks() -> None:
    """Finalize should cache whole-week serialization and indexed lookups."""
    week = HelloFreshWeek(
        week_id="week-1",
        display_name="Week 1",
        subscription_id="sub-1",
        delivery_date=date(2026, 6, 12),
        meals_required=3,
        meals_selected=1,
    )
    data = HelloFreshAccountData(weeks=[week]).finalize()

    assert data.serialized_weeks[0]["week_id"] == "week-1"
    assert data.get_week("week-1") is week


def test_account_menu_data_does_not_duplicate_single_payload_across_subscriptions() -> None:
    """Menu normalization should not fan out one payload across every subscription."""
    client = HelloFreshClient(session=None)  # type: ignore[arg-type]
    subscriptions = [
        HelloFreshSubscription(subscription_id="sub-1"),
        HelloFreshSubscription(subscription_id="sub-2"),
    ]

    class DummyResponse:
        """Minimal response object."""

    requests: list[dict[str, object]] = []

    async def fake_api_get(path: str, params=None):
        requests.append({"path": path, "params": params})
        return DummyResponse()

    async def fake_response_json(_response):
        subscription_id = requests[-1]["params"]["subscription"]  # type: ignore[index]
        return {
            "weeks": [
                {
                    "id": f"menu-{subscription_id}",
                    "label": f"Menu {subscription_id}",
                    "recipes": [{"id": f"recipe-{subscription_id}", "name": "Pasta"}],
                }
            ]
        }

    client._async_api_get = fake_api_get  # type: ignore[method-assign]
    client._async_response_json = fake_response_json  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(client._async_get_account_menu_data(subscriptions))

    assert result is not None
    weeks = result["weeks"]
    assert len(weeks) == 2
    assert weeks[0].subscription_id == "sub-1"
    assert weeks[1].subscription_id == "sub-2"
    assert [week.display_name for week in weeks] == ["Menu sub-1", "Menu sub-2"]


def test_upcoming_deliveries_uses_ranged_customer_deliveries_endpoint() -> None:
    """Deliveries loading should try the ranged customer deliveries endpoint from the HAR."""
    client = HelloFreshClient(session=object())  # type: ignore[arg-type]
    subscription = HelloFreshSubscription(subscription_id="sub-1", locale="en-US")

    class DummyResponse:
        """Minimal response object."""

        status = 200

    requests: list[dict[str, object | None]] = []

    async def fake_api_get(path: str, params=None, extra_headers=None):
        requests.append({"path": path, "params": params, "extra_headers": extra_headers})
        return DummyResponse()

    async def fake_response_json(_response):
        return {
            "items": [
                {
                    "id": "2026-W24",
                    "subscriptionId": "sub-1",
                    "deliveryDate": "2026-06-08T12:00:00-0700",
                    "status": "DELIVERED",
                    "tracking": {
                        "tracking_link": "https://www.hellofresh.com/delivery-tracking/6c11d560-8cc1-4190-bd71-dd8fa51f9d9a",
                        "tracking_code": "DUS1441132100520980",
                    },
                }
            ]
        }

    client._async_api_get = fake_api_get  # type: ignore[method-assign]
    client._async_response_json = fake_response_json  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    weeks, orders = loop.run_until_complete(client._async_get_upcoming_deliveries(subscription))

    assert requests[0]["path"] == "/gw/api/customers/me/deliveries"
    params = requests[0]["params"]
    assert isinstance(params, dict)
    assert "rangeStart" in params
    assert "rangeEnd" in params
    assert len(weeks) == 1
    assert len(orders) == 1
    assert orders[0].tracking_number == "DUS1441132100520980"


def test_account_menu_data_accepts_nested_menu_payload_shape() -> None:
    """Nested authenticated menu payloads should not force public fallback."""
    client = HelloFreshClient(session=None)  # type: ignore[arg-type]
    subscriptions = [HelloFreshSubscription(subscription_id="sub-1", meals_required=3)]

    class DummyResponse:
        """Minimal response object."""

    async def fake_api_get(path: str, params=None):
        return DummyResponse()

    async def fake_response_json(_response):
        return {
            "data": {
                "menus": [
                    {
                        "id": "menu-sub-1",
                        "label": "Menu sub-1",
                        "entries": [{"id": "recipe-sub-1", "name": "Pasta"}],
                    }
                ]
            }
        }

    client._async_api_get = fake_api_get  # type: ignore[method-assign]
    client._async_response_json = fake_response_json  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(client._async_get_account_menu_data(subscriptions))

    assert result is not None
    weeks = result["weeks"]
    assert len(weeks) == 1
    assert weeks[0].subscription_id == "sub-1"
    assert weeks[0].display_name == "Menu sub-1"
    assert weeks[0].recipes[0].name == "Pasta"


def test_account_menu_data_accepts_wrapped_recipe_collections() -> None:
    """Authenticated menu payloads may wrap recipes in container objects."""
    client = HelloFreshClient(session=None)  # type: ignore[arg-type]
    subscriptions = [HelloFreshSubscription(subscription_id="sub-1", meals_required=3)]

    class DummyResponse:
        """Minimal response object."""

    async def fake_api_get(path: str, params=None):
        return DummyResponse()

    async def fake_response_json(_response):
        return {
            "data": {
                "weeks": [
                    {
                        "id": "menu-sub-1",
                        "label": "Menu sub-1",
                        "recipes": {
                            "items": [
                                {
                                    "id": "recipe-sub-1",
                                    "name": "Pasta",
                                    "headline": "Creamy and quick",
                                }
                            ]
                        },
                    }
                ]
            }
        }

    client._async_api_get = fake_api_get  # type: ignore[method-assign]
    client._async_response_json = fake_response_json  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(client._async_get_account_menu_data(subscriptions))

    assert result is not None
    weeks = result["weeks"]
    assert len(weeks) == 1
    assert weeks[0].display_name == "Menu sub-1"
    assert weeks[0].recipes[0].name == "Pasta"


def test_account_menu_data_falls_back_to_subscription_scoped_menu_endpoint() -> None:
    """Menu loading should try newer subscription-scoped endpoint families."""
    client = HelloFreshClient(session=None)  # type: ignore[arg-type]
    subscriptions = [HelloFreshSubscription(subscription_id="sub-1", meals_required=3)]

    class DummyResponse:
        """Minimal response object."""

    requests: list[dict[str, object | None]] = []

    async def fake_api_get(path: str, params=None):
        requests.append({"path": path, "params": params})
        if path != "/gw/api/customers/me/subscriptions/sub-1/menu":
            raise HelloFreshError("unreachable")
        return DummyResponse()

    async def fake_response_json(_response):
        return {
            "weeks": [
                {
                    "id": "menu-sub-1",
                    "label": "Menu sub-1",
                    "recipes": [{"id": "recipe-sub-1", "name": "Pasta"}],
                }
            ]
        }

    client._async_api_get = fake_api_get  # type: ignore[method-assign]
    client._async_response_json = fake_response_json  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(client._async_get_account_menu_data(subscriptions))

    assert result is not None
    assert [request["path"] for request in requests[:4]] == [
        "/gw/my-menu/weeks",
        "/gw/my-menu",
        "/gw/api/customers/me/menu",
        "/gw/api/customers/me/subscriptions/sub-1/menu",
    ]
    weeks = result["weeks"]
    assert len(weeks) == 1
    assert weeks[0].recipes[0].name == "Pasta"


def test_past_delivery_history_tries_ranged_customer_deliveries_endpoint() -> None:
    """History loading should try the ranged customer deliveries endpoint from the HAR."""
    client = HelloFreshClient(session=object())  # type: ignore[arg-type]
    subscriptions = [HelloFreshSubscription(subscription_id="sub-1", meals_required=3)]

    class DummyResponse:
        """Minimal response object."""

        status = 200

    requests: list[dict[str, object | None]] = []

    async def fake_api_get(path: str, params=None, extra_headers=None):
        requests.append({"path": path, "params": params, "extra_headers": extra_headers})
        if path == "/gw/customer-complaints/users/me/deliveries":
            raise HelloFreshError("unavailable")
        return DummyResponse()

    async def fake_response_json(_response):
        return {
            "items": [
                {
                    "id": "2026-W24",
                    "deliveryDate": "2026-06-08T12:00:00-0700",
                    "subscriptionId": "sub-1",
                    "recipes": [{"id": "recipe-1", "name": "Pasta"}],
                }
            ]
        }

    client._async_api_get = fake_api_get  # type: ignore[method-assign]
    client._async_response_json = fake_response_json  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    weeks = loop.run_until_complete(client._async_get_past_delivery_weeks(subscriptions))

    assert len(weeks) == 1
    assert requests[1]["path"] == "/gw/api/customers/me/deliveries"
    params = requests[1]["params"]
    assert isinstance(params, dict)
    assert "rangeStart" in params
    assert "rangeEnd" in params


def test_account_menu_data_uses_authenticated_delivery_menu_endpoint() -> None:
    """The live delivery menu endpoint should be used when week metadata is available."""
    client = HelloFreshClient(session=None)  # type: ignore[arg-type]
    subscription = HelloFreshSubscription(
        subscription_id="6959884",
        account_id="acct-1",
        locale="en-US",
        servings=2,
        raw={
            "customerPlanId": "plan-123",
            "preset": "chefschoice",
            "shippingAddress": {"postcode": "01930"},
            "product": {"sku": "US-CBU-3-2-0"},
            "productType": {"specs": {"size": 2}},
            "deliveryOption": {"handle": "US-1-0800-2000"},
        },
    )
    account_week = HelloFreshWeek(
        week_id="2026-W25",
        display_name="Jun 15 - Jun 21",
        subscription_id="6959884",
        meals_required=3,
        meals_selected=1,
        raw={
            "deliveryOption": {"handle": "US-1-0800-2000"},
            "product": {"handle": "US-CBU-3-2-0"},
        },
    )

    class DummyResponse:
        """Minimal response object."""

        status = 200

    requests: list[dict[str, object | None]] = []

    async def fake_api_get(path: str, params=None):
        requests.append({"path": path, "params": params})
        if path == "/gw/api/subscriptions/6959884/product_options":
            return DummyResponse()
        if path != "/gw/my-deliveries/menu":
            raise HelloFreshError("unexpected endpoint")
        return DummyResponse()

    async def fake_response_json(_response):
        if requests[-1]["path"] == "/gw/api/subscriptions/6959884/product_options":
            return {
                "unifiedPreferences": {
                    "plans": {
                        "plan-123": {
                            "planPreference": "quick",
                        }
                    }
                }
            }
        return {
            "id": "menu-id",
            "week": "2026-W25",
            "meals": [
                {
                    "index": 13,
                    "selection": {"limit": 2},
                    "recipe": {
                        "id": "recipe-1",
                        "name": "Honey Garlic Shrimp Po'Boys",
                        "headline": "Sweet, savory, and crunchy",
                    },
                }
            ],
        }

    client._async_api_get = fake_api_get  # type: ignore[method-assign]
    client._async_response_json = fake_response_json  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(
        client._async_get_account_menu_data([subscription], [account_week])
    )

    assert result is not None
    assert requests[0]["path"] == "/gw/api/subscriptions/6959884/product_options"
    assert requests[1]["path"] == "/gw/my-deliveries/menu"
    assert requests[1]["params"] == {
        "customerPlanId": "plan-123",
        "delivery-option": "US-1-0800-2000",
        "exclude": "",
        "exclude-feedback": "true",
        "include-filters": "true",
        "include-future-feedback": "false",
        "locale": "en-US",
        "postcode": "01930",
        "preference": "quick",
        "product-sku": "US-CBU-3-2-0",
        "servings": "2",
        "subscription": "6959884",
        "week": "2026-W25",
    }
    weeks = result["weeks"]
    assert len(weeks) == 1
    assert weeks[0].display_name == "Jun 15 - Jun 21"
    assert weeks[0].recipes[0].name == "Honey Garlic Shrimp Po'Boys"
    assert weeks[0].recipes[0].is_selected is False


def test_delivery_menu_preference_falls_back_to_subscription_preset() -> None:
    """Missing product-options data should not block the authenticated menu request."""
    client = HelloFreshClient(session=None)  # type: ignore[arg-type]
    subscription = HelloFreshSubscription(
        subscription_id="6959884",
        account_id="acct-1",
        locale="en-US",
        servings=2,
        raw={
            "customerPlanId": "plan-123",
            "preset": "chefschoice",
            "shippingAddress": {"postcode": "01930"},
            "product": {"sku": "US-CBU-3-2-0"},
            "productType": {"specs": {"size": 2}},
            "deliveryOption": {"handle": "US-1-0800-2000"},
        },
    )
    account_week = HelloFreshWeek(
        week_id="2026-W25",
        display_name="Jun 15 - Jun 21",
        subscription_id="6959884",
        raw={
            "deliveryOption": {"handle": "US-1-0800-2000"},
            "product": {"handle": "US-CBU-3-2-0"},
        },
    )

    class DummyResponse:
        """Minimal response object."""

        status = 200

    requests: list[dict[str, object | None]] = []

    async def fake_api_get(path: str, params=None):
        requests.append({"path": path, "params": params})
        if path == "/gw/api/subscriptions/6959884/product_options":
            raise HelloFreshError("not available")
        return DummyResponse()

    async def fake_response_json(_response):
        return {"id": "menu-id", "week": "2026-W25", "meals": []}

    client._async_api_get = fake_api_get  # type: ignore[method-assign]
    client._async_response_json = fake_response_json  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(client._async_get_delivery_menu_week_data(subscription, account_week))

    assert requests[1]["path"] == "/gw/my-deliveries/menu"
    assert requests[1]["params"]["preference"] == "chefschoice"  # type: ignore[index]


def test_account_menu_candidate_detection_accepts_wrapped_recipe_collections() -> None:
    """Week candidate detection should keep wrapped recipe payloads reachable."""
    client = HelloFreshClient(session=None)  # type: ignore[arg-type]

    raw_weeks = client._extract_menu_week_candidates(
        {
            "data": {
                "weeks": [
                    {
                        "id": "menu-sub-1",
                        "label": "Menu sub-1",
                        "entries": {"nodes": [{"id": "recipe-sub-1", "title": "Pasta"}]},
                    }
                ]
            }
        }
    )

    assert len(raw_weeks) == 1
    assert raw_weeks[0]["id"] == "menu-sub-1"


def test_account_data_does_not_flag_public_menu_fallback_when_delivery_weeks_have_recipes() -> None:
    """Structured delivery recipes should suppress the menu fallback warning."""
    client = HelloFreshClient(
        session=None,  # type: ignore[arg-type]
        access_token="token",
    )
    subscription = HelloFreshSubscription(
        subscription_id="sub-1",
        account_id="acct-1",
        locale="en-US",
        meals_required=3,
    )

    async def fake_get_subscriptions():
        return [subscription]

    async def fake_get_account_menu_data(_subscriptions, _weeks):
        return None

    async def fake_get_public_menu_data():
        return {
            "weeks": [
                HelloFreshWeek(
                    week_id="public-current",
                    display_name="Public Menu",
                    recipes=[HelloFreshRecipe(recipe_id="public-1", name="Burger")],
                    source="public_menu",
                )
            ],
            "available_labels": ["Public Menu"],
        }

    async def fake_get_upcoming_deliveries(_subscription):
        return (
            [
                HelloFreshWeek(
                    week_id="week-1",
                    display_name="Week 1",
                    subscription_id="sub-1",
                    meals_required=3,
                    meals_selected=1,
                    recipes=[HelloFreshRecipe(recipe_id="recipe-1", name="Pasta")],
                )
            ],
            [],
        )

    client._async_get_subscriptions = fake_get_subscriptions  # type: ignore[method-assign]
    client._async_get_account_menu_data = fake_get_account_menu_data  # type: ignore[method-assign]
    client._async_get_public_menu_data = fake_get_public_menu_data  # type: ignore[method-assign]
    client._async_get_upcoming_deliveries = fake_get_upcoming_deliveries  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(client.async_get_account_data())

    assert result.capabilities.using_public_menu_fallback is False
    assert result.weeks[0].recipes[0].name == "Pasta"
    assert "Week 1" in result.available_menu_labels


def test_account_data_merges_authenticated_menu_catalog_into_delivery_week() -> None:
    """Authenticated delivery menu recipes should enrich the account week."""
    client = HelloFreshClient(
        session=None,  # type: ignore[arg-type]
        access_token="token",
        enable_public_menu_fallback=False,
    )
    subscription = HelloFreshSubscription(
        subscription_id="sub-1",
        account_id="acct-1",
        locale="en-US",
        meals_required=3,
    )

    async def fake_get_subscriptions():
        return [subscription]

    async def fake_get_upcoming_deliveries(_subscription):
        return (
            [
                HelloFreshWeek(
                    week_id="2026-W25",
                    display_name="Week 25",
                    subscription_id="sub-1",
                    meals_required=3,
                    meals_selected=1,
                    recipes=[HelloFreshRecipe(recipe_id="recipe-1", name="Selected Pasta")],
                )
            ],
            [],
        )

    async def fake_get_account_menu_data(_subscriptions, weeks):
        assert weeks[0].week_id == "2026-W25"
        return {
            "weeks": [
                HelloFreshWeek(
                    week_id="2026-W25",
                    display_name="Week 25",
                    subscription_id="sub-1",
                    recipes=[
                        HelloFreshRecipe(
                            recipe_id="recipe-1", name="Selected Pasta", is_selected=False
                        ),
                        HelloFreshRecipe(recipe_id="recipe-2", name="Burger", is_selected=False),
                    ],
                    source="account_menu_api",
                )
            ],
            "available_labels": ["Week 25"],
        }

    client._async_get_subscriptions = fake_get_subscriptions  # type: ignore[method-assign]
    client._async_get_upcoming_deliveries = fake_get_upcoming_deliveries  # type: ignore[method-assign]
    client._async_get_account_menu_data = fake_get_account_menu_data  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(client.async_get_account_data())

    assert result.capabilities.supports_account_menu_api is True
    assert [recipe.name for recipe in result.weeks[0].recipes] == ["Selected Pasta", "Burger"]
    assert result.weeks[0].recipes[0].is_selected is True
    assert result.weeks[0].recipes[1].is_selected is False


def test_account_data_collects_debug_trace_for_menu_and_delivery_attempts() -> None:
    """Debug trace should expose endpoint attempts for diagnostics."""
    client = HelloFreshClient(
        session=None,  # type: ignore[arg-type]
        access_token="token",
    )
    subscription = HelloFreshSubscription(
        subscription_id="sub-1",
        account_id="acct-1",
        locale="en-US",
        meals_required=3,
    )

    async def fake_get_subscriptions():
        return [subscription]

    async def fake_get_account_menu_data(_subscriptions, _weeks):
        client._record_debug_attempt(  # type: ignore[attr-defined]
            "menu_attempts",
            {
                "subscription_id": "sub-1",
                "path": "/gw/my-menu",
                "status": 200,
                "payload_summary": {"top_level_keys": ["data"]},
                "recognized_week_count": 0,
            },
        )
        return None

    async def fake_get_public_menu_data():
        return {
            "weeks": [
                HelloFreshWeek(
                    week_id="public-current",
                    display_name="Public Menu",
                    recipes=[HelloFreshRecipe(recipe_id="public-1", name="Burger")],
                    source="public_menu",
                )
            ],
            "available_labels": ["Public Menu"],
        }

    async def fake_get_upcoming_deliveries(_subscription):
        client._record_debug_attempt(  # type: ignore[attr-defined]
            "delivery_attempts",
            {
                "subscription_id": "sub-1",
                "path": "/gw/my-deliveries/upcoming-deliveries",
                "status": 200,
                "payload_summary": {"top_level_keys": ["items"]},
                "recognized_week_count": 1,
            },
        )
        return (
            [
                HelloFreshWeek(
                    week_id="week-1",
                    display_name="Week 1",
                    subscription_id="sub-1",
                    meals_required=3,
                    meals_selected=1,
                    recipes=[HelloFreshRecipe(recipe_id="recipe-1", name="Pasta")],
                )
            ],
            [],
        )

    client._async_get_subscriptions = fake_get_subscriptions  # type: ignore[method-assign]
    client._async_get_account_menu_data = fake_get_account_menu_data  # type: ignore[method-assign]
    client._async_get_public_menu_data = fake_get_public_menu_data  # type: ignore[method-assign]
    client._async_get_upcoming_deliveries = fake_get_upcoming_deliveries  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(client.async_get_account_data())

    assert result.debug_trace["menu_attempts"][0]["path"] == "/gw/my-menu"
    assert result.debug_trace["delivery_attempts"][0]["recognized_week_count"] == 1


def test_account_data_enriches_order_price_from_cart_endpoint() -> None:
    """Cart pricing should override partial delivery totals when menu metadata is available."""
    client = HelloFreshClient(
        session=None,  # type: ignore[arg-type]
        access_token="token",
        country="us",
    )
    subscription = HelloFreshSubscription(
        subscription_id="6959884",
        account_id="15259216",
        locale="en-US",
        servings=2,
        meals_required=3,
        raw={
            "customerPlanId": "plan-123",
            "shippingAddress": {
                "address1": "62 Leonard St",
                "postcode": "01930",
                "region": "MA",
            },
        },
    )

    async def fake_get_subscriptions():
        return [subscription]

    async def fake_get_upcoming_deliveries(_subscription):
        return (
            [
                HelloFreshWeek(
                    week_id="2026-W25",
                    display_name="Week 25",
                    subscription_id="6959884",
                    delivery_date=date(2026, 6, 15),
                    meals_required=3,
                    meals_selected=3,
                    raw={
                        "product": {
                            "handle": "US-CBU-3-2-0",
                            "price": 6594,
                        },
                        "deliveryOption": {"handle": "US-1-0800-2000"},
                    },
                )
            ],
            [
                HelloFreshOrder(
                    order_id="2026-W25",
                    week_id="2026-W25",
                    status="scheduled",
                    subscription_id="6959884",
                    delivery_date=date(2026, 6, 15),
                    total_price=76.93,
                    currency="USD",
                )
            ],
        )

    async def fake_get_account_menu_data(_subscriptions, _weeks):
        return {
            "weeks": [
                HelloFreshWeek(
                    week_id="2026-W25",
                    display_name="Week 25",
                    subscription_id="6959884",
                    source="account_menu_api",
                    raw={
                        "meals": [
                            {
                                "index": 68,
                                "selection": {"quantity": 1},
                                "charge": {"handle": "US-CHARGE-0-0-0"},
                                "recipe": {"id": "recipe-68", "name": "Meal 68"},
                            },
                            {
                                "index": 80,
                                "selection": {"quantity": 1},
                                "charge": {"handle": "US-CHARGE-0-0-0"},
                                "recipe": {"id": "recipe-80", "name": "Meal 80"},
                            },
                            {
                                "index": 55,
                                "selection": {"quantity": 1},
                                "charge": {"handle": "US-CHARGE-0-0-0"},
                                "recipe": {"id": "recipe-55", "name": "Meal 55"},
                            },
                        ]
                    },
                    recipes=[
                        HelloFreshRecipe(recipe_id="recipe-68", name="Meal 68", is_selected=False),
                        HelloFreshRecipe(recipe_id="recipe-80", name="Meal 80", is_selected=False),
                        HelloFreshRecipe(recipe_id="recipe-55", name="Meal 55", is_selected=False),
                    ],
                )
            ],
            "available_labels": ["Week 25"],
        }

    class DummyResponse:
        """Minimal response object."""

        status = 200

    pricing_requests: list[dict[str, object | None]] = []

    async def fake_api_request(method: str, path: str, params=None, json_payload=None):
        pricing_requests.append(
            {
                "method": method,
                "path": path,
                "params": params,
                "json_payload": json_payload,
            }
        )
        return DummyResponse()

    async def fake_response_json(_response):
        return {
            "grandTotal": 97.5,
            "subTotal": 96.5,
            "shippingAmount": 10.99,
            "discountAmount": 9.99,
            "currencyCode": "USD",
        }

    client._async_get_subscriptions = fake_get_subscriptions  # type: ignore[method-assign]
    client._async_get_upcoming_deliveries = fake_get_upcoming_deliveries  # type: ignore[method-assign]
    client._async_get_account_menu_data = fake_get_account_menu_data  # type: ignore[method-assign]
    client._async_api_request = fake_api_request  # type: ignore[method-assign]
    client._async_response_json = fake_response_json  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(client.async_get_account_data())

    assert result.next_order is not None
    assert result.next_order.total_price == 97.5
    assert result.next_order.currency == "USD"
    assert pricing_requests[0]["path"] == "/gw/v1/carts/2026-W25/price"
    assert pricing_requests[0]["params"] == {"isFutureWeek": "true"}
    assert pricing_requests[0]["json_payload"] == {
        "boxSize": 2,
        "isFirstOrder": False,
        "customerID": 15259216,
        "isRecurring": True,
        "subscriptionID": 6959884,
        "planID": "plan-123",
        "products": [
            {
                "handle": "US-CBU-3-2-0",
                "deliveryOption": "US-1-0800-2000",
                "hfWeek": "2026-W25",
                "unitPrice": 65.94,
            },
            {
                "boxSku": "US-CBU-3-2-0",
                "handle": "US-CHARGE-0-0-0",
                "hfWeek": "2026-W25",
                "quantityPerCourse": [
                    {"index": 68, "quantity": 1},
                    {"index": 80, "quantity": 1},
                    {"index": 55, "quantity": 1},
                ],
                "recipeIndexes": ["68", "80", "55"],
            },
        ],
        "shippingAddress": {
            "address1": "62 Leonard St",
            "postcode": "01930",
            "region": "MA",
        },
        "locale": "en-US",
        "country": "US",
    }


def test_account_data_backfills_next_selection_week_from_subscription_metadata() -> None:
    """Subscription next-delivery metadata should keep selection sensors usable."""
    client = HelloFreshClient(
        session=None,  # type: ignore[arg-type]
        access_token="token",
    )
    subscription = HelloFreshSubscription(
        subscription_id="6959884",
        account_id="acct-1",
        locale="en-US",
        meals_required=3,
        raw={
            "id": "6959884",
            "isActive": True,
            "nextDelivery": "2026-06-15T00:00:00-0700",
            "nextDeliveryWeek": "2026-W25",
            "nextModifiableDeliveryDate": "2026-06-15T00:00:00-0700",
            "nextModifiableDeliveryWeek": "2026-W25",
            "nextCutoffDate": "2026-06-10T23:59:59-0700",
            "nextDeliveryOption": {
                "deliveryName": "Mondays: 8AM - 8PM",
                "type": "PLAN",
            },
            "productType": {
                "productName": "Classic - 3 meals per week for 2 people",
                "specs": {"meals": 3},
            },
        },
    )

    async def fake_get_subscriptions():
        return [subscription]

    async def fake_get_upcoming_deliveries(_subscription):
        return ([], [])

    async def fake_get_account_menu_data(_subscriptions, weeks):
        assert len(weeks) == 1
        assert weeks[0].week_id == "2026-W25"
        return {
            "weeks": [
                HelloFreshWeek(
                    week_id="2026-W25",
                    display_name="Week 25",
                    subscription_id="6959884",
                    meals_required=3,
                    meals_selected=1,
                    source="account_menu_api",
                    recipes=[
                        HelloFreshRecipe(recipe_id="recipe-1", name="Pasta", is_selected=True)
                    ],
                )
            ],
            "available_labels": ["Week 25"],
        }

    client._async_get_subscriptions = fake_get_subscriptions  # type: ignore[method-assign]
    client._async_get_upcoming_deliveries = fake_get_upcoming_deliveries  # type: ignore[method-assign]
    client._async_get_account_menu_data = fake_get_account_menu_data  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(client.async_get_account_data())

    assert result.next_selection_week is not None
    assert result.next_selection_week.week_id == "2026-W25"
    assert result.next_selection_week.selection_deadline is not None
    assert result.next_selection_week.selection_progress == "1/3"
    assert result.next_selection_week.slot_label == "Mondays: 8AM - 8PM"


def test_account_data_derives_write_capabilities_from_allowed_actions() -> None:
    """Allowed action flags should be reflected in runtime capabilities."""
    client = HelloFreshClient(
        session=None,  # type: ignore[arg-type]
        access_token="token",
        enable_public_menu_fallback=False,
    )
    subscription = HelloFreshSubscription(subscription_id="sub-1", locale="en-US", meals_required=3)
    actionable_week = HelloFreshWeek(
        week_id="2026-W25",
        display_name="Week 25",
        subscription_id="sub-1",
        delivery_date=date(2026, 6, 15),
        meals_required=3,
        meals_selected=1,
        allowed_actions={
            "updateDeliveryAddress": True,
            "updateDeliveryWeekday": True,
            "pause": True,
            "oneOffChange": True,
            "updatePaymentMethod": True,
            "donate": False,
        },
    )

    async def fake_get_subscriptions():
        return [subscription]

    async def fake_get_boxes_received():
        return None

    async def fake_get_past_delivery_weeks(_subscriptions):
        return []

    async def fake_get_upcoming_deliveries(_subscription):
        return ([actionable_week], [])

    async def fake_get_account_menu_data(_subscriptions, _weeks):
        return None

    async def fake_enrich_tracking(*_args, **_kwargs):
        return None

    client._async_get_subscriptions = fake_get_subscriptions  # type: ignore[method-assign]
    client._async_get_boxes_received = fake_get_boxes_received  # type: ignore[method-assign]
    client._async_get_past_delivery_weeks = fake_get_past_delivery_weeks  # type: ignore[method-assign]
    client._async_get_upcoming_deliveries = fake_get_upcoming_deliveries  # type: ignore[method-assign]
    client._async_get_account_menu_data = fake_get_account_menu_data  # type: ignore[method-assign]
    client._async_enrich_order_tracking = fake_enrich_tracking  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(client.async_get_account_data())

    capabilities = result.capabilities.as_dict()
    assert capabilities["supports_update_delivery_address"] is True
    assert capabilities["supports_update_delivery_weekday"] is True
    assert capabilities["supports_pause"] is True
    assert capabilities["supports_one_off_change"] is True
    assert capabilities["supports_update_payment_method"] is True
    assert capabilities["supports_donation"] is False


def test_account_data_enriches_tracking_from_scm_public_tracking_endpoint() -> None:
    """Orders with HelloFresh tracking links should be enriched from SCM tracking data."""
    client = HelloFreshClient(
        session=None,  # type: ignore[arg-type]
        access_token="token",
        enable_public_menu_fallback=False,
    )
    subscription = HelloFreshSubscription(
        subscription_id="6959884",
        account_id="acct-1",
        locale="en-US",
    )
    tracked_week = HelloFreshWeek(
        week_id="2026-W24",
        display_name="Week 24",
        subscription_id="6959884",
        delivery_date=date(2026, 6, 8),
        status="DELIVERED",
    )
    tracked_order = HelloFreshOrder(
        order_id="401686221",
        week_id="2026-W24",
        status="DELIVERED",
        subscription_id="6959884",
        delivery_date=date(2026, 6, 8),
        tracking_url="https://www.hellofresh.com/delivery-tracking/6c11d560-8cc1-4190-bd71-dd8fa51f9d9a",
        tracking_number="DUS1441132100520980",
    )

    async def fake_get_subscriptions():
        return [subscription]

    async def fake_get_upcoming_deliveries(_subscription):
        return ([tracked_week], [tracked_order])

    async def fake_get_account_menu_data(_subscriptions, _weeks):
        return None

    class DummyResponse:
        """Minimal response object."""

        status = 200

    tracking_requests: list[dict[str, object | None]] = []

    async def fake_api_get(path: str, params=None, extra_headers=None):
        tracking_requests.append(
            {
                "path": path,
                "params": params,
                "extra_headers": extra_headers,
            }
        )
        return DummyResponse()

    async def fake_response_json(_response):
        return {
            "boxes": [
                {
                    "external_id": "H4182317000",
                    "carrier": "DDASH",
                    "delivery_date": "2026-06-08T12:00:00Z",
                    "tracking_code": "DUS1441132100520980",
                    "public_url": "https://www.doordash.com/orders/drive?trackingNumber=DUS1441132100520980",
                    "carrier_tracking_url": "https://www.doordash.com/orders/drive?trackingNumber=DUS1441132100520980",
                    "hf_tracking_url": "https://www.hellofresh.com/delivery-tracking/6c11d560-8cc1-4190-bd71-dd8fa51f9d9a",
                    "internal_status": "delivered",
                    "last_status": {
                        "status": "delivered",
                        "internal_status": "delivered",
                    },
                }
            ]
        }

    client._async_get_subscriptions = fake_get_subscriptions  # type: ignore[method-assign]
    client._async_get_upcoming_deliveries = fake_get_upcoming_deliveries  # type: ignore[method-assign]
    client._async_get_account_menu_data = fake_get_account_menu_data  # type: ignore[method-assign]
    client._async_api_get = fake_api_get  # type: ignore[method-assign]
    client._async_response_json = fake_response_json  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(client.async_get_account_data())

    assert tracking_requests == [
        {
            "path": "/gw/scm/tracking-ids/track/public-id/6c11d560-8cc1-4190-bd71-dd8fa51f9d9a",
            "params": {"country": "US", "locale": "en-US"},
            "extra_headers": {"x-requested-by": "shipping-and-tracking"},
        }
    ]
    assert result.tracked_order is not None
    assert result.tracked_order.tracking_number == "DUS1441132100520980"
    assert result.tracked_order.tracking_status == "delivered"
    assert result.tracked_order.carrier == "DoorDash"
    assert result.tracked_order.tracking_url == (
        "https://www.doordash.com/orders/drive?trackingNumber=DUS1441132100520980"
    )


def test_async_select_meals_uses_cart_update_endpoint_from_menu_payload() -> None:
    """Meal selection should use the cart update API with menu indexes from the authenticated menu."""
    client = HelloFreshClient(
        session=None,  # type: ignore[arg-type]
        access_token="token",
        enable_public_menu_fallback=False,
    )
    subscription = HelloFreshSubscription(
        subscription_id="6959884",
        account_id="15259216",
        locale="en-US",
        servings=2,
        raw={
            "customerPlanId": "plan-123",
            "product": {"sku": "US-CBU-3-2-0"},
        },
    )
    week = HelloFreshWeek(
        week_id="2026-W26",
        display_name="Week 26",
        subscription_id="6959884",
        selection_deadline=datetime(2026, 6, 17, 23, 59, 59, tzinfo=timezone(timedelta(hours=-7))),
        meals_required=3,
        meals_selected=0,
        recipes=[
            HelloFreshRecipe(recipe_id="recipe-11", name="Meal 11", is_selected=False),
            HelloFreshRecipe(recipe_id="recipe-18", name="Meal 18", is_selected=False),
            HelloFreshRecipe(recipe_id="recipe-20", name="Meal 20", is_selected=False),
        ],
        raw={
            "product": {"handle": "US-CBU-3-2-0"},
            "_menu_payload": {
                "week": "2026-W26",
                "meals": [
                    {
                        "index": 11,
                        "selection": {"limit": 2},
                        "recipe": {"id": "recipe-11", "name": "Meal 11"},
                    },
                    {
                        "index": 18,
                        "selection": {"limit": 2},
                        "recipe": {"id": "recipe-18", "name": "Meal 18"},
                    },
                    {
                        "index": 20,
                        "selection": {"limit": 2},
                        "recipe": {"id": "recipe-20", "name": "Meal 20"},
                    },
                ],
            },
        },
    )
    client._last_account_data = HelloFreshAccountData(weeks=[week]).finalize()

    requests: list[dict[str, object | None]] = []

    async def fake_get_subscriptions():
        return [subscription]

    async def fake_get_subscription_plan_preference(_subscription):
        return "quick"

    async def fake_api_request(
        method: str,
        path: str,
        params=None,
        json_payload=None,
        extra_headers=None,
        _allow_refresh_retry=True,
    ):
        requests.append(
            {
                "method": method,
                "path": path,
                "params": params,
                "json_payload": json_payload,
                "extra_headers": extra_headers,
            }
        )

        class DummyResponse:
            status = 200

        return DummyResponse()

    client._async_get_subscriptions = fake_get_subscriptions  # type: ignore[method-assign]
    client._async_get_subscription_plan_preference = fake_get_subscription_plan_preference  # type: ignore[method-assign]
    client._async_api_request = fake_api_request  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(
        client.async_select_meals("2026-W26", ["recipe-11", "recipe-18", "recipe-20"])
    )

    assert requests == [
        {
            "method": "PUT",
            "path": "/gw/v1/carts/2026-W26",
            "params": {
                "customer": "15259216",
                "cutoff_time": "2026-06-17T23:59:59-07:00",
                "ignore_addons": "false",
                "preference": "quick",
                "product-sku": "US-CBU-3-2-0",
                "subscription": "6959884",
                "update_quantity": "true",
                "week": "2026-W26",
            },
            "json_payload": {
                "extras": [],
                "meals": [
                    {"index": 11, "quantity": 1},
                    {"index": 18, "quantity": 1},
                    {"index": 20, "quantity": 1},
                ],
            },
            "extra_headers": {"x-requested-by": "shopping-experience-web"},
        }
    ]


def test_scm_tracking_prefers_external_status_label() -> None:
    """SCM tracking should prefer the customer-facing status over internal labels."""
    from custom_components.hellofresh.parsers import extract_scm_tracking_details

    details = extract_scm_tracking_details(
        {
            "carrier": "DDASH",
            "tracking_code": "TRACK123",
            "last_status": {
                "status": "in_transit",
                "internal_status": "transit",
            },
        }
    )

    assert details["tracking_status"] == "in_transit"
    assert details["carrier"] == "DoorDash"


def test_api_request_refreshes_expiring_access_token_before_request() -> None:
    """Expiring access tokens should be renewed via the refresh token automatically."""
    requests: list[dict[str, object | None]] = []

    class DummyResponse:
        """Minimal response object."""

        def __init__(self, status: int, payload: dict[str, object]) -> None:
            self.status = status
            self._payload = payload

        async def json(self, content_type=None):
            return self._payload

        async def text(self):
            return str(self._payload)

    class DummySession:
        """Minimal session object."""

        async def post(self, url: str, params=None, json=None, headers=None):
            requests.append({"method": "POST", "url": url, "json": json, "headers": headers})
            return DummyResponse(
                200,
                {
                    "access_token": "fresh-token",
                    "expires_in": 1800,
                    "refresh_token": "refresh-456",
                },
            )

        async def request(self, method: str, url: str, params=None, json=None, headers=None):
            requests.append(
                {
                    "method": method,
                    "url": url,
                    "params": params,
                    "json": json,
                    "headers": headers,
                }
            )
            return DummyResponse(200, {"ok": True})

    client = HelloFreshClient(
        session=DummySession(),  # type: ignore[arg-type]
        country="us",
        access_token="stale-token",
        refresh_token="refresh-123",
        token_issued_at=int(datetime.now(timezone.utc).timestamp()) - 1790,
        token_expires_in=1800,
        refresh_expires_in=5184000,
        username="user@example.com",
        password="pw",
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    response = loop.run_until_complete(client._async_api_get("/gw/api/customers/me/subscriptions"))

    assert response.status == 200
    assert requests[0]["url"] == "https://www.hellofresh.com/gw/refresh"
    assert requests[0]["json"] == {"refresh_token": "refresh-123"}
    assert requests[1]["headers"]["Authorization"] == "Bearer fresh-token"  # type: ignore[index]


def test_api_request_retries_after_401_with_refreshed_token() -> None:
    """A 401 should trigger one refresh-and-retry attempt when refresh metadata exists."""
    requests: list[dict[str, object | None]] = []

    class DummyResponse:
        """Minimal response object."""

        def __init__(self, status: int, payload: dict[str, object]) -> None:
            self.status = status
            self._payload = payload

        async def json(self, content_type=None):
            return self._payload

        async def text(self):
            return str(self._payload)

    class DummySession:
        """Minimal session object."""

        def __init__(self) -> None:
            self.request_calls = 0

        async def post(self, url: str, params=None, json=None, headers=None):
            requests.append({"method": "POST", "url": url, "json": json, "headers": headers})
            return DummyResponse(200, {"access_token": "fresh-token", "expires_in": 1800})

        async def request(self, method: str, url: str, params=None, json=None, headers=None):
            self.request_calls += 1
            requests.append(
                {
                    "method": method,
                    "url": url,
                    "params": params,
                    "json": json,
                    "headers": headers,
                }
            )
            if self.request_calls == 1:
                return DummyResponse(401, {"error": "expired"})
            return DummyResponse(200, {"ok": True})

    client = HelloFreshClient(
        session=DummySession(),  # type: ignore[arg-type]
        country="us",
        access_token="expired-token",
        refresh_token="refresh-123",
        username="user@example.com",
        password="pw",
        token_issued_at=int(__import__("time").time()),  # now, so expiry is far in the future
        token_expires_in=86400,  # 24 h — proactive refresh won't fire
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    response = loop.run_until_complete(client._async_api_get("/gw/api/customers/me/subscriptions"))

    assert response.status == 200
    assert requests[0]["method"] == "GET"
    assert requests[1]["url"] == "https://www.hellofresh.com/gw/refresh"
    assert requests[2]["headers"]["Authorization"] == "Bearer fresh-token"  # type: ignore[index]


def test_concurrent_401s_rotate_refresh_token_only_once() -> None:
    """Concurrent 401s must trigger a single refresh, not one rotation per request.

    Regression: Auth0 rotates (and invalidates) the refresh token on every use. The
    coordinator fetches many endpoints concurrently, so when the access token expires
    several requests 401 at once. Without a re-check inside the refresh lock, each one
    forced its own rotation, burning the refresh token the previous waiter had just
    obtained — which killed auth after a few hours instead of lasting ~60 days.
    """
    refresh_calls = 0
    rotations: list[str] = []

    class DummyResponse:
        def __init__(self, status: int, payload: dict[str, object]) -> None:
            self.status = status
            self._payload = payload

        async def json(self, content_type=None):
            return self._payload

        async def text(self):
            return str(self._payload)

    class DummySession:
        def __init__(self) -> None:
            self.access_token = "expired-token"

        async def post(self, url: str, params=None, json=None, headers=None):
            nonlocal refresh_calls
            # Yield so any racing waiters reach the lock before this rotation completes.
            await asyncio.sleep(0)
            # A stale (already-rotated) refresh token would be rejected by Auth0.
            if json is not None and json.get("refresh_token") != "refresh-current":
                return DummyResponse(403, {"error": "invalid_grant"})
            refresh_calls += 1
            self.access_token = f"fresh-token-{refresh_calls}"
            rotations.append("refresh-current")
            return DummyResponse(
                200,
                {
                    "access_token": self.access_token,
                    "refresh_token": "refresh-current",  # rotation returns a token (same value here)
                    "expires_in": 1800,
                },
            )

        async def request(self, method: str, url: str, params=None, json=None, headers=None):
            # Yield so all five gathered requests get their 401 before any refresh runs,
            # forcing real contention on the refresh lock.
            await asyncio.sleep(0)
            auth = (headers or {}).get("Authorization", "")
            if auth == "Bearer expired-token":
                return DummyResponse(401, {"error": "expired"})
            return DummyResponse(200, {"ok": True})

    client = HelloFreshClient(
        session=DummySession(),  # type: ignore[arg-type]
        country="us",
        access_token="expired-token",
        refresh_token="refresh-current",
        username="user@example.com",
        password="pw",
        token_issued_at=int(__import__("time").time()),
        token_expires_in=86400,  # proactive refresh won't fire; force the reactive 401 path
    )

    async def _hammer() -> list[object]:
        return await asyncio.gather(*(client._async_api_get(f"/gw/endpoint/{i}") for i in range(5)))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    results = loop.run_until_complete(_hammer())

    assert all(r.status == 200 for r in results)
    # The crux: five concurrent 401s, but exactly one refresh-token rotation.
    assert refresh_calls == 1


def test_reboot_uses_valid_access_token_when_proactive_refresh_fails() -> None:
    """A still-valid stored access token must survive a reboot even if refresh fails.

    On startup the proactive (half-life) refresh fires for any token older than half its
    life. If the stored refresh token was already rotated in a prior session, that refresh
    returns 403 — but the stored access token is still valid and should keep working
    instead of failing setup with a reauth prompt.
    """
    api_calls = 0

    class DummyResponse:
        def __init__(self, status: int, payload: dict[str, object]) -> None:
            self.status = status
            self._payload = payload

        async def json(self, content_type=None):
            return self._payload

        async def text(self):
            return str(self._payload)

    class DummySession:
        async def post(self, url: str, params=None, json=None, headers=None):
            # Refresh token was rotated in a previous session -> Auth0 rejects it.
            return DummyResponse(403, {"error": "invalid_grant"})

        async def request(self, method: str, url: str, params=None, json=None, headers=None):
            nonlocal api_calls
            api_calls += 1
            # The stored access token is still valid -> the API accepts it.
            assert (headers or {}).get("Authorization") == "Bearer stored-valid-token"
            return DummyResponse(200, {"ok": True})

    now = int(datetime.now(timezone.utc).timestamp())
    client = HelloFreshClient(
        session=DummySession(),  # type: ignore[arg-type]
        country="us",
        access_token="stored-valid-token",
        refresh_token="rotated-away-token",
        username="user@example.com",
        password="pw",
        # Issued 20 min ago on a 30-min token: past half-life (proactive refresh fires)
        # but still ~10 min of real life left.
        token_issued_at=now - 1200,
        token_expires_in=1800,
        refresh_expires_in=5184000,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    response = loop.run_until_complete(client._async_api_get("/gw/api/customers/me/subscriptions"))

    assert response.status == 200
    assert api_calls == 1
    # The still-valid stored token is retained, not discarded.
    assert client._access_token == "stored-valid-token"


def test_async_ensure_token_fresh_refreshes_expiring_token() -> None:
    """The public token-refresh helper renews a token that is near expiry."""
    posts: list[dict[str, object | None]] = []

    class DummyResponse:
        def __init__(self, status: int, payload: dict[str, object]) -> None:
            self.status = status
            self._payload = payload

        async def json(self, content_type=None):
            return self._payload

        async def text(self):
            return str(self._payload)

    class DummySession:
        async def post(self, url: str, params=None, json=None, headers=None):
            posts.append({"url": url, "json": json})
            return DummyResponse(200, {"access_token": "renewed-token", "expires_in": 1800})

    client = HelloFreshClient(
        session=DummySession(),  # type: ignore[arg-type]
        country="us",
        access_token="stale-token",
        refresh_token="refresh-123",
        token_issued_at=int(datetime.now(timezone.utc).timestamp()) - 1790,
        token_expires_in=1800,
        refresh_expires_in=5184000,
        username="user@example.com",
        password="pw",
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(client.async_ensure_token_fresh())

    assert posts and posts[0]["url"] == "https://www.hellofresh.com/gw/refresh"
    assert client._access_token == "renewed-token"


class _AuthFlowResponse:
    """Minimal aiohttp-like response for the /gw auth endpoints."""

    def __init__(
        self,
        status: int,
        payload: dict | None = None,
        *,
        text: str | None = None,
        headers: dict | None = None,
    ) -> None:
        self.status = status
        self._payload = payload if payload is not None else {}
        self._text = text
        self.headers = headers or {}

    async def json(self, content_type=None):
        return self._payload

    async def text(self):
        return self._text if self._text is not None else str(self._payload)


def test_login_runs_app_token_then_login_when_no_refresh_token() -> None:
    """With credentials but no refresh token, the client logs in via /gw/auth/token + /gw/login."""
    posts: list[dict] = []

    class DummySession:
        async def post(self, url: str, params=None, json=None, headers=None):
            posts.append({"url": url, "params": params, "json": json})
            if url.endswith("/gw/auth/token"):
                return _AuthFlowResponse(200, {"access_token": "app-token"})
            return _AuthFlowResponse(
                200,
                {
                    "access_token": "user-token",
                    "refresh_token": "R-new",
                    "expires_in": 1800,
                    "refresh_expires_in": 5184000,
                    "token_type": "Bearer",
                },
            )

        async def request(self, method, url, params=None, json=None, headers=None):
            return _AuthFlowResponse(200, {"ok": True})

    client = HelloFreshClient(
        session=DummySession(),  # type: ignore[arg-type]
        country="us",
        username="user@example.com",
        password="pw",
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(client._async_login(force=True))

    # App token is fetched first, then credentials are POSTed to /gw/login.
    assert posts[0]["url"] == "https://www.hellofresh.com/gw/auth/token"
    assert posts[0]["params"] == {"grant_type": "client_credentials", "client_id": "senf"}
    assert posts[1]["url"] == "https://www.hellofresh.com/gw/login"
    assert posts[1]["json"] == {"username": "user@example.com", "password": "pw"}
    assert client._access_token == "user-token"
    assert client._refresh_token == "R-new"


def test_refresh_falls_back_to_login_when_refresh_token_rejected() -> None:
    """A rejected /gw/refresh must fall through to a full login when credentials exist."""
    posts: list[str] = []

    class DummySession:
        async def post(self, url: str, params=None, json=None, headers=None):
            posts.append(url)
            if url.endswith("/gw/refresh"):
                return _AuthFlowResponse(403, {"error": "invalid_grant"})
            if url.endswith("/gw/auth/token"):
                return _AuthFlowResponse(200, {"access_token": "app-token"})
            return _AuthFlowResponse(
                200, {"access_token": "user-token", "refresh_token": "R-new", "expires_in": 1800}
            )

    client = HelloFreshClient(
        session=DummySession(),  # type: ignore[arg-type]
        country="us",
        access_token="stale",
        refresh_token="R-dead",
        token_issued_at=int(datetime.now(timezone.utc).timestamp()),
        token_expires_in=1800,
        refresh_expires_in=5184000,
        username="user@example.com",
        password="pw",
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(client._async_refresh_access_token(force=True))

    assert posts[0].endswith("/gw/refresh")  # tried refresh first
    assert posts[1].endswith("/gw/auth/token")  # then logged in
    assert posts[2].endswith("/gw/login")
    assert client._access_token == "user-token"
    assert client._refresh_token == "R-new"


def test_refresh_without_credentials_raises_when_token_rejected() -> None:
    """A rejected refresh with no credentials configured surfaces an auth error (no login)."""

    class DummySession:
        async def post(self, url: str, params=None, json=None, headers=None):
            return _AuthFlowResponse(403, {"error": "invalid_grant"})

    client = HelloFreshClient(
        session=DummySession(),  # type: ignore[arg-type]
        country="us",
        access_token="stale",
        refresh_token="R-dead",
        token_issued_at=int(datetime.now(timezone.utc).timestamp()),
        token_expires_in=1800,
        refresh_expires_in=5184000,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    with pytest.raises(HelloFreshAuthError):
        loop.run_until_complete(client._async_refresh_access_token(force=True))


_BOT_BLOCK_HTML = (
    "<!DOCTYPE html>\n<!--[if lt IE 7]> <html class=\"no-js ie6 oldie\" lang=\"en-US\"> "
    "<![endif]-->\n<html class=\"no-js\" lang=\"en-US\"><head><title>Access denied</title>"
)


def test_login_bot_block_raises_transient_error_not_auth_error() -> None:
    """An HTML 403 on /gw/login is a WAF block, not bad credentials.

    It must surface as a (transient, retriable) HelloFreshError so Home Assistant does not
    prompt the user to re-enter correct credentials. The login must NOT be retried as a
    different exception path.
    """
    posts: list[str] = []

    class DummySession:
        async def post(self, url: str, params=None, json=None, headers=None):
            posts.append(url)
            if url.endswith("/gw/auth/token"):
                return _AuthFlowResponse(200, {"access_token": "app-token"})
            # /gw/login is blocked by edge bot protection with an HTML challenge page.
            return _AuthFlowResponse(
                403,
                text=_BOT_BLOCK_HTML,
                headers={"Content-Type": "text/html; charset=UTF-8"},
            )

    client = HelloFreshClient(
        session=DummySession(),  # type: ignore[arg-type]
        country="us",
        username="user@example.com",
        password="pw",
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    with pytest.raises(HelloFreshError) as excinfo:
        loop.run_until_complete(client._async_login(force=True))

    assert not isinstance(excinfo.value, HelloFreshAuthError)
    assert "bot protection" in str(excinfo.value)
    assert posts[-1].endswith("/gw/login")


def test_refresh_bot_block_does_not_fall_back_to_login() -> None:
    """An HTML 403 on /gw/refresh must not escalate into a login against the same WAF.

    The refresh raises a transient HelloFreshError; the refresh-then-login orchestration
    only falls back to login on a real HelloFreshAuthError, so no /gw/login is attempted
    and the existing refresh token is kept.
    """
    posts: list[str] = []

    class DummySession:
        async def post(self, url: str, params=None, json=None, headers=None):
            posts.append(url)
            return _AuthFlowResponse(
                403,
                text=_BOT_BLOCK_HTML,
                headers={"Content-Type": "text/html"},
            )

    client = HelloFreshClient(
        session=DummySession(),  # type: ignore[arg-type]
        country="us",
        access_token="stale",
        refresh_token="R-live",
        token_issued_at=int(datetime.now(timezone.utc).timestamp()),
        token_expires_in=1800,
        refresh_expires_in=5184000,
        username="user@example.com",
        password="pw",
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    with pytest.raises(HelloFreshError) as excinfo:
        loop.run_until_complete(client._async_refresh_access_token(force=True))

    assert not isinstance(excinfo.value, HelloFreshAuthError)
    assert posts == [posts[0]] and posts[0].endswith("/gw/refresh")  # no login fallback
    assert client._refresh_token == "R-live"  # refresh token preserved


def test_upcoming_deliveries_prefers_last_successful_endpoint() -> None:
    """After one endpoint succeeds, the next poll should try it first (sticky probing)."""
    client = HelloFreshClient(session=object(), access_token="t")  # type: ignore[arg-type]
    subscription = HelloFreshSubscription(subscription_id="sub-1", locale="en-US")

    # The first two candidate paths 404; the third (upcoming-deliveries + from) returns weeks.
    winning_params_key = "from,subscription"

    class DummyResponse:
        status = 200

    calls: list[str] = []

    async def fake_api_get(path: str, params=None, extra_headers=None):
        param_keys = ",".join(sorted(params)) if params else ""
        calls.append(f"{path}?{param_keys}")
        if path == "/gw/my-deliveries/upcoming-deliveries" and param_keys == winning_params_key:
            return DummyResponse()
        raise HelloFreshError("HTTP 404")

    async def fake_response_json(_response):
        return {"weeks": [{"id": "2026-W25", "deliveryDate": "2026-06-19"}]}

    client._async_api_get = fake_api_get  # type: ignore[method-assign]
    client._async_response_json = fake_response_json  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    weeks_1, _ = loop.run_until_complete(client._async_get_upcoming_deliveries(subscription))
    assert weeks_1  # found via the third candidate
    first_poll_calls = len(calls)
    assert first_poll_calls >= 3  # it had to probe past the earlier candidates

    calls.clear()
    weeks_2, _ = loop.run_until_complete(client._async_get_upcoming_deliveries(subscription))
    assert weeks_2
    # Second poll hits the remembered winner on the very first request — no wasted probes.
    assert calls[0] == f"/gw/my-deliveries/upcoming-deliveries?{winning_params_key}"
    assert len(calls) == 1


def test_cart_price_is_cached_for_identical_request() -> None:
    """An unchanged cart-pricing request must not be re-POSTed on the next poll."""
    client = HelloFreshClient(session=object(), access_token="t")  # type: ignore[arg-type]
    subscription = HelloFreshSubscription(subscription_id="sub-1", account_id="42", locale="en-US")
    week = HelloFreshWeek(week_id="2026-W25", display_name="W25", subscription_id="sub-1")

    post_count = 0

    async def fake_build(_sub, _week):
        return {"boxSize": 2, "products": [{"handle": "X"}]}

    async def fake_api_request(method, path, params=None, json_payload=None, extra_headers=None):
        nonlocal post_count
        post_count += 1
        return object()

    async def fake_response_json(_response):
        return {"grandTotal": 97.5, "currency": "USD"}

    client._build_cart_price_payload = lambda _s, _w: {  # type: ignore[method-assign]
        "boxSize": 2,
        "products": [{"handle": "X"}],
    }
    client._async_api_request = fake_api_request  # type: ignore[method-assign]
    client._async_response_json = fake_response_json  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    first = loop.run_until_complete(client._async_get_cart_price_for_week(subscription, week))
    second = loop.run_until_complete(client._async_get_cart_price_for_week(subscription, week))

    assert first == second == {"grandTotal": 97.5, "currency": "USD"}
    assert post_count == 1  # second call served from cache, no second POST


def test_cart_price_cache_is_fifo_bounded() -> None:
    """The pricing cache must not grow without bound over the client's lifetime."""
    from custom_components.hellofresh.client import _CART_PRICE_CACHE_MAX

    client = HelloFreshClient(session=object(), access_token="t")  # type: ignore[arg-type]
    for i in range(_CART_PRICE_CACHE_MAX + 10):
        client._store_cart_price(f"key-{i}", {"grandTotal": i})

    assert len(client._cart_price_cache) == _CART_PRICE_CACHE_MAX
    # Oldest keys were evicted; the most recent ones remain.
    assert "key-0" not in client._cart_price_cache
    assert f"key-{_CART_PRICE_CACHE_MAX + 9}" in client._cart_price_cache


def test_order_price_falls_back_to_calculate_when_cart_price_has_no_total() -> None:
    """When the cart-price endpoint yields no total, /gw/calculate supplies it."""
    client = HelloFreshClient(session=object(), access_token="t")  # type: ignore[arg-type]
    subscription = HelloFreshSubscription(
        subscription_id="6959884",
        account_id="15259216",
        locale="en-US",
        raw={"customerPlanId": "plan-1", "sku": "US-CBU-3-2-0", "postcode": "01930"},
    )
    week = HelloFreshWeek(
        week_id="2026-W27",
        display_name="W27",
        subscription_id="6959884",
        raw={"deliveryOption": {"handle": "US-1-0800-2000"}},
    )
    order = HelloFreshOrder(order_id="o-1", week_id="2026-W27", status="scheduled")

    calls: list[str] = []

    async def fake_api_request(method, path, params=None, json_payload=None, extra_headers=None):
        calls.append(path)
        return object()

    async def fake_response_json(_response):
        # Cart-price endpoint answers without a recognizable total; calculate supplies one.
        if calls[-1].endswith("/price"):
            return {"unrelated": True}
        return {"grandTotal": 88.25, "currency": "USD"}

    client._async_api_request = fake_api_request  # type: ignore[method-assign]
    client._async_response_json = fake_response_json  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(client._async_apply_order_price(subscription, week, order))

    assert any(p == "/gw/calculate" for p in calls)
    assert order.total_price == 88.25
    assert order.currency == "USD"


def test_mutation_remembers_winning_endpoint_combo() -> None:
    """A write action that succeeds should be retried first next time (sticky writes)."""
    client = HelloFreshClient(session=object(), access_token="t")  # type: ignore[arg-type]
    week = HelloFreshWeek(week_id="2026-W25", display_name="W25", subscription_id="sub-1")

    winning_path = "/gw/api/customers/me/subscriptions/sub-1/weeks/2026-W25/skip"
    attempts: list[str] = []

    async def fake_api_request(method, path, params=None, json_payload=None, extra_headers=None):
        attempts.append(f"{method} {path}")
        if path == winning_path:
            return object()
        raise HelloFreshError("HTTP 404")

    client._async_api_request = fake_api_request  # type: ignore[method-assign]

    path_templates = [
        "/gw/my-deliveries/weeks/{week_id}/skip",
        "/gw/my-menu/weeks/{week_id}/skip",
        "/gw/api/customers/me/subscriptions/{subscription_id}/weeks/{week_id}/skip",
    ]
    payload_variants = [{"weekId": "2026-W25", "skip": True}]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    loop.run_until_complete(
        client._async_try_mutation_candidates(
            path_templates, week, payload_variants, category="skip"
        )
    )
    assert any(a.endswith(winning_path) for a in attempts)
    first_attempt_count = len(attempts)
    assert first_attempt_count > 1  # had to probe past the failing candidates

    attempts.clear()
    loop.run_until_complete(
        client._async_try_mutation_candidates(
            path_templates, week, payload_variants, category="skip"
        )
    )
    # Second call hits the remembered winning combo first — no wasted probes.
    assert attempts == [f"POST {winning_path}"]


def _client_with_known_week(week: HelloFreshWeek) -> HelloFreshClient:
    """Return a client whose loaded account data contains ``week``."""
    client = HelloFreshClient(session=object(), access_token="t")  # type: ignore[arg-type]
    client._last_account_data = HelloFreshAccountData(weeks=[week]).finalize()
    return client


def test_skip_week_uses_verified_delivery_status_patch() -> None:
    """Skip should PATCH delivery_dates with status PAUSED (the HAR-verified shape)."""
    week = HelloFreshWeek(
        week_id="2026-W30",
        display_name="W30",
        subscription_id="6959884",
        raw={
            "cutoffDate": "2026-07-15T23:59:59-0700",
            "deliveryDate": "2026-07-20T12:00:00-0700",
        },
    )
    client = _client_with_known_week(week)

    captured: dict[str, object] = {}

    async def fake_api_request(method, path, params=None, json_payload=None, extra_headers=None):
        captured.update(method=method, path=path, params=params, json_payload=json_payload)
        return object()

    client._async_api_request = fake_api_request  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(client.async_skip_week("2026-W30"))

    assert captured["method"] == "PATCH"
    assert captured["path"] == "/gw/api/subscriptions/6959884/delivery_dates/2026-W30"
    assert captured["json_payload"] == {
        "delivery": {
            "cutoffDate": "2026-07-15T23:59:59-0700",
            "deliveryDate": "2026-07-20T12:00:00-0700",
            "status": "PAUSED",
            "subscriptionId": "6959884",
            "id": "2026-W30",
        }
    }


def test_unskip_week_sets_status_running() -> None:
    """Unskip should PATCH the same endpoint with status RUNNING."""
    week = HelloFreshWeek(
        week_id="2026-W30",
        display_name="W30",
        subscription_id="6959884",
        is_skipped=True,
        raw={
            "cutoffDate": "2026-07-15T23:59:59-0700",
            "deliveryDate": "2026-07-20T12:00:00-0700",
        },
    )
    client = _client_with_known_week(week)

    captured: dict[str, object] = {}

    async def fake_api_request(method, path, params=None, json_payload=None, extra_headers=None):
        captured.update(method=method, json_payload=json_payload)
        return object()

    client._async_api_request = fake_api_request  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(client.async_unskip_week("2026-W30"))

    assert captured["method"] == "PATCH"
    assert captured["json_payload"]["delivery"]["status"] == "RUNNING"  # type: ignore[index]


def test_skip_week_falls_back_to_guessed_paths_without_dates() -> None:
    """When a week lacks cutoff/delivery dates, skip falls back to the guessed endpoints."""
    week = HelloFreshWeek(week_id="2026-W30", display_name="W30", subscription_id="6959884")
    client = _client_with_known_week(week)

    paths: list[str] = []

    async def fake_api_request(method, path, params=None, json_payload=None, extra_headers=None):
        paths.append(path)
        # Accept the first guessed skip path so the fallback resolves.
        if path.endswith("/skip"):
            return object()
        raise HelloFreshError("HTTP 404")

    client._async_api_request = fake_api_request  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(client.async_skip_week("2026-W30"))

    # The verified PATCH is skipped (no dates), so only the guessed /skip paths are tried.
    assert paths and all(p.endswith("/skip") for p in paths)
    assert not any("delivery_dates" in p for p in paths)


def test_reschedule_week_posts_oneoff_with_verified_body() -> None:
    """Reschedule should POST /oneoff with the HAR-verified body shape."""
    week = HelloFreshWeek(
        week_id="2026-W26",
        display_name="W26",
        subscription_id="6959884",
        allowed_actions={"oneOffChange": True},
    )
    client = _client_with_known_week(week)
    captured: dict[str, object] = {}

    async def fake_api_request(method, path, params=None, json_payload=None, extra_headers=None):
        captured.update(method=method, path=path, params=params, json_payload=json_payload)
        return object()

    client._async_api_request = fake_api_request  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(
        client.async_change_one_off_delivery("2026-W26", "US-2-0800-2000")
    )

    assert captured["method"] == "POST"
    assert captured["path"] == "/gw/api/subscriptions/6959884/oneoff"
    assert captured["json_payload"] == {
        "id": "6959884",
        "delivery_option": "US-2-0800-2000",
        "week": "2026-W26",
        "source": "reschedule-delivery-feature",
    }


def test_reschedule_week_blocked_when_capability_absent() -> None:
    """Reschedule must refuse when the week disallows one-off changes."""
    week = HelloFreshWeek(
        week_id="2026-W26",
        display_name="W26",
        subscription_id="6959884",
        allowed_actions={"oneOffChange": False},
    )
    client = _client_with_known_week(week)

    async def fake_api_request(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("no request should be sent when capability is absent")

    client._async_api_request = fake_api_request  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    with pytest.raises(HelloFreshNotImplementedError):
        loop.run_until_complete(
            client.async_change_one_off_delivery("2026-W26", "US-2-0800-2000")
        )


def test_change_delivery_weekday_posts_plan_details() -> None:
    """Weekday change should POST changePlanDeliveryDetails for the plan."""
    client = HelloFreshClient(session=object(), access_token="t")  # type: ignore[arg-type]
    client._cached_subscriptions = [
        HelloFreshSubscription(
            subscription_id="6959884",
            account_id="15259216",
            raw={"customerPlanId": "plan-1"},
        )
    ]
    captured: dict[str, object] = {}

    async def fake_api_request(method, path, params=None, json_payload=None, extra_headers=None):
        captured.update(method=method, path=path, json_payload=json_payload)
        return object()

    client._async_api_request = fake_api_request  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(
        client.async_change_delivery_weekday("US-1-0800-2000", 1)
    )

    assert captured["method"] == "POST"
    assert captured["path"] == "/gw/api/plans/plan-1/changePlanDeliveryDetails"
    assert captured["json_payload"] == {
        "deliveryOption": "US-1-0800-2000",
        "deliveryInterval": 1,
    }


def test_authenticated_requests_send_feature_headers() -> None:
    """Authenticated reads should carry the HAR-observed market/feature headers."""
    client = HelloFreshClient(
        session=_HeaderCapturingSession(),  # type: ignore[arg-type]
        access_token="tok",
        token_issued_at=int(datetime.now(timezone.utc).timestamp()),
        token_expires_in=1800,
    )
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(client._async_api_get("/gw/api/customers/me/subscriptions"))

    sent = client._session.last_headers  # type: ignore[attr-defined]
    assert sent["X-Market-API-Version"] == "2"
    assert sent["X-Food-Categorization"] == "v1"
    assert sent["x-sort-variations-by-quantity"] == "true"
    assert "Mozilla/5.0" in sent["User-Agent"]


class _HeaderCapturingSession:
    """Session stub that records the headers of the last request."""

    def __init__(self) -> None:
        self.last_headers: dict[str, str] = {}

    async def request(self, method, url, params=None, json=None, headers=None):
        self.last_headers = dict(headers or {})

        class _Resp:
            status = 200

        return _Resp()


def test_async_ensure_token_fresh_skips_when_token_has_life() -> None:
    """A token that is comfortably valid must not be refreshed by the timer helper."""
    posts: list[object] = []

    class DummySession:
        async def post(self, url: str, params=None, json=None, headers=None):
            posts.append(url)
            raise AssertionError("refresh should not be attempted for a healthy token")

    client = HelloFreshClient(
        session=DummySession(),  # type: ignore[arg-type]
        country="us",
        access_token="healthy-token",
        refresh_token="refresh-123",
        token_issued_at=int(datetime.now(timezone.utc).timestamp()),
        token_expires_in=1800,
        refresh_expires_in=5184000,
        username="user@example.com",
        password="pw",
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(client.async_ensure_token_fresh())

    assert posts == []
    assert client._access_token == "healthy-token"


def test_diagnostics_redacts_all_credential_fields() -> None:
    """Long-lived credentials and account login details must be in the redaction set."""
    from custom_components.hellofresh.const import (
        CONF_ACCESS_TOKEN,
        CONF_PASSWORD,
        CONF_REFRESH_TOKEN,
        CONF_USERNAME,
    )
    from custom_components.hellofresh.diagnostics import TO_REDACT

    for key in (
        CONF_ACCESS_TOKEN,
        CONF_REFRESH_TOKEN,
        CONF_USERNAME,
        CONF_PASSWORD,
    ):
        assert key in TO_REDACT


def test_token_lifetime_seconds_exposes_configured_lifetime() -> None:
    """The client exposes the access token lifetime for the refresh-timer cadence."""
    client = HelloFreshClient(
        session=object(),  # type: ignore[arg-type]
        country="us",
        token_expires_in=1800,
    )
    assert client.token_lifetime_seconds == 1800


def test_refresh_token_expiry_anchors_to_refresh_issue_time_not_access_token() -> None:
    """The 60-day refresh-token clock must not slide on every access-token refresh.

    Bug: _refresh_token_expired used the access token's issued_at (reset every ~15 min),
    so the deadline slid forward forever and never fired. It must anchor to when the
    refresh token itself was issued.
    """
    now = int(datetime.now(timezone.utc).timestamp())
    client = HelloFreshClient(
        session=object(),  # type: ignore[arg-type]
        country="us",
        access_token="A",
        refresh_token="R",
        token_issued_at=now,
        token_expires_in=1800,
        refresh_expires_in=5184000,  # 60 days
        refresh_token_issued_at=now - 5184000 - 10,  # issued just over 60 days ago
    )
    # Access token is fresh, but the refresh token is past its own 60-day life.
    assert client._refresh_token_expired() is True

    # A refresh token issued recently is NOT expired, even with an old access-token time.
    client2 = HelloFreshClient(
        session=object(),  # type: ignore[arg-type]
        country="us",
        access_token="A",
        refresh_token="R",
        token_issued_at=now - 3600,  # access token "issued" an hour ago
        token_expires_in=1800,
        refresh_expires_in=5184000,
        refresh_token_issued_at=now - 86400,  # refresh token issued 1 day ago
    )
    assert client2._refresh_token_expired() is False


def test_refresh_token_issued_at_defaults_to_login_issued_at() -> None:
    """Legacy entries without a separate refresh_token_issued_at fall back to issued_at."""
    now = int(datetime.now(timezone.utc).timestamp())
    client = HelloFreshClient(
        session=object(),  # type: ignore[arg-type]
        country="us",
        access_token="A",
        refresh_token="R",
        token_issued_at=now - 100,
        token_expires_in=1800,
        refresh_expires_in=5184000,
        # refresh_token_issued_at omitted -> should adopt token_issued_at
    )
    assert client._refresh_token_issued_at == now - 100
    expiry = client.refresh_token_expires_at
    assert expiry is not None
    assert int(expiry.timestamp()) == (now - 100) + 5184000


def test_rotation_resets_refresh_token_clock_and_persists_it() -> None:
    """When Auth0 returns a new refresh token, its 60-day clock resets to now and persists."""
    persisted: list[dict] = []

    class DummyResponse:
        def __init__(self, status: int, payload: dict) -> None:
            self.status = status
            self._payload = payload

        async def json(self, content_type=None):
            return self._payload

        async def text(self):
            return str(self._payload)

    class DummySession:
        async def post(self, url: str, params=None, json=None, headers=None):
            return DummyResponse(
                200,
                {
                    "access_token": "A_new",
                    "refresh_token": "R_new",  # rotation
                    "expires_in": 1800,
                    # Note: Auth0 commonly omits refresh_expires_in on refresh.
                },
            )

    old_issue = int(datetime.now(timezone.utc).timestamp()) - 40 * 86400
    client = HelloFreshClient(
        session=DummySession(),  # type: ignore[arg-type]
        country="us",
        access_token="A_old",
        refresh_token="R_old",
        token_issued_at=old_issue,
        token_expires_in=1800,
        refresh_expires_in=5184000,
        refresh_token_issued_at=old_issue,
        username="user@example.com",
        password="pw",
        token_refresh_callback=persisted.append,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(client._async_refresh_access_token(force=True))

    now = int(datetime.now(timezone.utc).timestamp())
    # Refresh-token clock reset to ~now (was 40 days ago).
    assert client._refresh_token_issued_at is not None
    assert abs(client._refresh_token_issued_at - now) <= 2
    assert client._refresh_token == "R_new"
    # Persisted payload carries the new anchor so it survives a reboot.
    assert persisted
    assert abs(persisted[-1]["refresh_token_issued_at"] - now) <= 2


def test_rotation_does_not_swallow_server_refresh_expires_in() -> None:
    """An explicit refresh_expires_in from the server must replace the stored value.

    Bug: ``coerce_int(...) or self._refresh_expires_in`` discarded a returned 0 or any
    smaller value. Use an explicit None check instead.
    """

    class DummyResponse:
        def __init__(self, status: int, payload: dict) -> None:
            self.status = status
            self._payload = payload

        async def json(self, content_type=None):
            return self._payload

        async def text(self):
            return str(self._payload)

    class DummySession:
        async def post(self, url: str, params=None, json=None, headers=None):
            return DummyResponse(
                200,
                {
                    "access_token": "A_new",
                    "expires_in": 1800,
                    "refresh_expires_in": 1000000,  # server shortens the RT lifetime
                },
            )

    client = HelloFreshClient(
        session=DummySession(),  # type: ignore[arg-type]
        country="us",
        access_token="A",
        refresh_token="R",
        token_issued_at=int(datetime.now(timezone.utc).timestamp()),
        token_expires_in=1800,
        refresh_expires_in=5184000,
        username="user@example.com",
        password="pw",
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(client._async_refresh_access_token(force=True))

    assert client._refresh_expires_in == 1000000


def test_token_timing_falls_back_to_jwt_claims_for_bare_token() -> None:
    """A bare access token's iat/exp claims should populate expiry timing."""
    import base64
    import json

    def _make_jwt(claims: dict) -> str:
        def _segment(data: dict) -> str:
            return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

        header = _segment({"alg": "RS256", "typ": "JWT"})
        return f"{header}.{_segment(claims)}.signature"

    issued_at = int(datetime.now(timezone.utc).timestamp())
    expires_at = issued_at + 1800
    client = HelloFreshClient(
        session=object(),  # type: ignore[arg-type]
        country="us",
        access_token=_make_jwt({"iat": issued_at, "exp": expires_at}),
    )

    assert client.token_lifetime_seconds == 1800
    token_expiry = client.token_expires_at
    assert token_expiry is not None
    assert int(token_expiry.timestamp()) == expires_at
    # No refresh token was supplied, so the refresh-token expiry stays unknown.
    assert client.refresh_token_expires_at is None


def test_explicit_token_timing_takes_precedence_over_jwt() -> None:
    """Explicit issued-at/expires-in should not be overridden by JWT claims."""
    import base64
    import json

    def _make_jwt(claims: dict) -> str:
        def _segment(data: dict) -> str:
            return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

        header = _segment({"alg": "RS256", "typ": "JWT"})
        return f"{header}.{_segment(claims)}.signature"

    client = HelloFreshClient(
        session=object(),  # type: ignore[arg-type]
        country="us",
        access_token=_make_jwt({"iat": 1, "exp": 2}),
        token_issued_at=1781271373,
        token_expires_in=1800,
    )
    assert client.token_lifetime_seconds == 1800


def test_token_refresh_interval_derives_cadence_from_lifetime() -> None:
    """The proactive refresh cadence sits well below the token lifetime, within bounds."""
    from datetime import timedelta

    from custom_components.hellofresh.coordinator import (
        MAX_TOKEN_REFRESH_INTERVAL,
        MIN_TOKEN_REFRESH_INTERVAL,
        _token_refresh_interval,
    )

    # 30-minute token -> refresh every ~7.5 min (a quarter of its life).
    assert _token_refresh_interval(1800) == timedelta(minutes=7, seconds=30)
    # A very short token clamps up to the 2-minute floor.
    assert _token_refresh_interval(120) == MIN_TOKEN_REFRESH_INTERVAL
    # A long token clamps down to the 10-minute ceiling.
    assert _token_refresh_interval(86400) == MAX_TOKEN_REFRESH_INTERVAL
    # Missing lifetime falls back to the default and still yields a valid interval.
    assert MIN_TOKEN_REFRESH_INTERVAL <= _token_refresh_interval(None) <= MAX_TOKEN_REFRESH_INTERVAL


def test_token_refresh_timer_never_lets_token_expire() -> None:
    """Regression: the timer must tick inside the refresh window before any expiry.

    The original 2/3-of-lifetime interval (20 min for a 30-min token) stepped over the
    narrow pre-expiry window, leaving the token dead for ~10 min each cycle. This
    simulates the real interaction between _token_refresh_interval (coordinator) and
    _token_expiring_soon (client) and asserts the token is never past expiry at a tick.
    """
    from custom_components.hellofresh.client import (
        _TOKEN_MIN_REMAINING_BEFORE_REFRESH,
        _TOKEN_REFRESH_AT_LIFETIME_FRACTION,
    )
    from custom_components.hellofresh.coordinator import _token_refresh_interval

    for lifetime in (600, 1800, 3600, 7200):
        interval = _token_refresh_interval(lifetime).total_seconds()

        def refresh_at(issued: float, life: int = lifetime) -> float:
            return min(
                issued + life * _TOKEN_REFRESH_AT_LIFETIME_FRACTION,
                issued + life - _TOKEN_MIN_REMAINING_BEFORE_REFRESH,
            )

        issued = 0.0
        for tick in range(1, 60):
            now = tick * interval
            assert now <= issued + lifetime, (
                f"token expired before refresh: lifetime={lifetime}, interval={interval}, "
                f"now={now}, expires={issued + lifetime}"
            )
            if now >= refresh_at(issued):
                issued = now


def test_async_unload_entry_clears_token_only_flag() -> None:
    """Unloading an entry must drop its coordinator and any pending token-only flag.

    Without the discard, an entry removed while a token-only refresh write was pending
    would leave its id in the TOKEN_ONLY_UPDATE_KEY set forever (slow per-entry leak).
    """
    from types import SimpleNamespace

    from custom_components.hellofresh import TOKEN_ONLY_UPDATE_KEY, async_unload_entry
    from custom_components.hellofresh.const import DOMAIN

    entry_id = "entry-xyz"

    async def _unload_platforms(_entry, _platforms) -> bool:
        return True

    hass = SimpleNamespace(
        data={
            DOMAIN: {entry_id: object()},
            TOKEN_ONLY_UPDATE_KEY: {entry_id, "other-entry"},
        },
        config_entries=SimpleNamespace(async_unload_platforms=_unload_platforms),
    )
    entry = SimpleNamespace(entry_id=entry_id)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(async_unload_entry(hass, entry))  # type: ignore[arg-type]

    assert result is True
    assert entry_id not in hass.data[DOMAIN]
    assert entry_id not in hass.data[TOKEN_ONLY_UPDATE_KEY]
    # Unrelated entries in the set are untouched.
    assert "other-entry" in hass.data[TOKEN_ONLY_UPDATE_KEY]


def test_async_unload_entry_handles_absent_token_only_set() -> None:
    """Unload must not fail when no token-only flag set exists yet."""
    from types import SimpleNamespace

    from custom_components.hellofresh import async_unload_entry
    from custom_components.hellofresh.const import DOMAIN

    entry_id = "entry-abc"

    async def _unload_platforms(_entry, _platforms) -> bool:
        return True

    hass = SimpleNamespace(
        data={DOMAIN: {entry_id: object()}},
        config_entries=SimpleNamespace(async_unload_platforms=_unload_platforms),
    )
    entry = SimpleNamespace(entry_id=entry_id)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    assert loop.run_until_complete(async_unload_entry(hass, entry)) is True  # type: ignore[arg-type]
    assert entry_id not in hass.data[DOMAIN]


class _FakeEntry:
    """Minimal stand-in for a HA ConfigEntry."""

    def __init__(self, data: dict, options: dict | None = None) -> None:
        self.entry_id = "entry-1"
        self.data = data
        self.options = options or {}


class _FakeConfigEntries:
    """Records async_update_entry calls and applies them to the fake entry."""

    def __init__(self, entry=None) -> None:
        self.update_calls: list[dict] = []
        self._entry = entry

    def async_update_entry(self, entry, data=None, options=None) -> bool:
        call: dict = {}
        if data is not None:
            entry.data = data
            call["data"] = data
        if options is not None:
            entry.options = options
            call["options"] = options
        self.update_calls.append(call)
        return True

    def async_get_known_entry(self, _entry_id):
        return self._entry


def _fake_hass_for_entry(entry=None):
    from types import SimpleNamespace

    config_entries = _FakeConfigEntries(entry)
    hass = SimpleNamespace(data={}, config_entries=config_entries)
    return hass, config_entries


def test_heal_moves_legacy_credentials_from_options_into_data() -> None:
    """Entries that stored credentials in options must heal into data-only on load."""
    from custom_components.hellofresh import _heal_credential_storage
    from custom_components.hellofresh.const import (
        CONF_ACCESS_TOKEN,
        CONF_COUNTRY,
        CONF_REFRESH_TOKEN,
        CONF_SCAN_INTERVAL_MINUTES,
    )

    # data is missing the refresh token; options carries the real credentials (legacy state).
    entry = _FakeEntry(
        data={CONF_COUNTRY: "us", CONF_ACCESS_TOKEN: "A0"},
        options={
            CONF_ACCESS_TOKEN: "A0",
            CONF_REFRESH_TOKEN: "R0",
            CONF_SCAN_INTERVAL_MINUTES: 180,
        },
    )
    hass, _ = _fake_hass_for_entry()

    _heal_credential_storage(hass, entry)  # type: ignore[arg-type]

    # Refresh token is now in data; credentials are gone from options; preferences remain.
    assert entry.data[CONF_REFRESH_TOKEN] == "R0"
    assert CONF_REFRESH_TOKEN not in entry.options
    assert CONF_ACCESS_TOKEN not in entry.options
    assert entry.options[CONF_SCAN_INTERVAL_MINUTES] == 180


def test_heal_prefers_fresher_data_token_over_stale_options_token() -> None:
    """When both stores have a token, data wins (runtime refresh keeps data current)."""
    from custom_components.hellofresh import _heal_credential_storage
    from custom_components.hellofresh.const import (
        CONF_ACCESS_TOKEN,
        CONF_COUNTRY,
        CONF_REFRESH_TOKEN,
    )

    entry = _FakeEntry(
        data={CONF_COUNTRY: "us", CONF_ACCESS_TOKEN: "A_fresh", CONF_REFRESH_TOKEN: "R_fresh"},
        options={CONF_ACCESS_TOKEN: "A_stale", CONF_REFRESH_TOKEN: "R_stale"},
    )
    hass, _ = _fake_hass_for_entry()

    _heal_credential_storage(hass, entry)  # type: ignore[arg-type]

    assert entry.data[CONF_REFRESH_TOKEN] == "R_fresh"
    assert entry.data[CONF_ACCESS_TOKEN] == "A_fresh"
    assert CONF_REFRESH_TOKEN not in entry.options


def test_heal_is_noop_when_options_has_no_credentials() -> None:
    """A clean entry (no creds in options) must not be rewritten."""
    from custom_components.hellofresh import _heal_credential_storage
    from custom_components.hellofresh.const import (
        CONF_ACCESS_TOKEN,
        CONF_COUNTRY,
        CONF_SCAN_INTERVAL_MINUTES,
    )

    entry = _FakeEntry(
        data={CONF_COUNTRY: "us", CONF_ACCESS_TOKEN: "A0"},
        options={CONF_SCAN_INTERVAL_MINUTES: 180},
    )
    hass, config_entries = _fake_hass_for_entry()

    _heal_credential_storage(hass, entry)  # type: ignore[arg-type]

    assert config_entries.update_calls == []  # no rewrite


# ---------------------------------------------------------------------------
# Billing logic: _accumulate_order_prices
# ---------------------------------------------------------------------------


def _make_client() -> HelloFreshClient:
    """Return a minimal HelloFreshClient for unit-testing pure methods."""

    class _NullSession:
        pass

    return HelloFreshClient(session=_NullSession(), country="us")  # type: ignore[arg-type]


def _billing_item(
    subscription_id: str,
    delivery_date: str,
    grand_total: float,
    currency: str = "USD",
    created_at: str = "2026-06-11T00:00:00Z",
    order_nr: str = "28192254942",
) -> dict:
    return {
        "orderNr": order_nr,
        "grandTotal": grand_total,
        "currency": currency,
        "createdAt": created_at,
        "orderLines": [
            {
                "deliveryDate": delivery_date,
                "subscription": {"id": subscription_id},
            }
        ],
    }


def test_accumulate_order_prices_sums_multiple_charges_same_date() -> None:
    """Three charges for the same (subscription, delivery_date) must be summed, not deduped."""
    client = _make_client()
    items = [
        _billing_item("sub-1", "2026-06-15", 4.59),
        _billing_item("sub-1", "2026-06-15", 15.98),
        _billing_item("sub-1", "2026-06-15", 76.93),
    ]
    _, _, _, price_by_key = client._accumulate_order_prices(items)

    assert ("sub-1", date(2026, 6, 15)) in price_by_key
    total, currency = price_by_key[("sub-1", date(2026, 6, 15))]
    assert round(total, 2) == 97.50
    assert currency == "USD"


def test_accumulate_order_prices_separates_different_dates() -> None:
    """Charges for different delivery dates accumulate independently."""
    client = _make_client()
    items = [
        _billing_item("sub-1", "2026-06-08", 80.00, created_at="2026-06-01T00:00:00Z"),
        _billing_item("sub-1", "2026-06-15", 97.50),
    ]
    _, _, _, price_by_key = client._accumulate_order_prices(items)

    assert round(price_by_key[("sub-1", date(2026, 6, 8))][0], 2) == 80.00
    assert round(price_by_key[("sub-1", date(2026, 6, 15))][0], 2) == 97.50


def test_accumulate_order_prices_future_vs_past_tracking() -> None:
    """Already-charged orders update latest_by_subscription; future deliveries populate future_by_subscription.

    The recent-charge accumulator keys off ``createdAt`` (the actual charge), not the
    delivery date: HelloFresh bills a box days before it ships, so the most recently billed
    order is a real recent payment even while its delivery is still upcoming.
    """
    client = _make_client()
    today = datetime.now(timezone.utc).date()
    older_charge = today - timedelta(days=14)
    recent_charge = today - timedelta(days=3)
    future_delivery = today + timedelta(days=4)
    items = [
        _billing_item(
            "sub-1",
            (today - timedelta(days=10)).isoformat(),
            80.00,
            created_at=f"{older_charge.isoformat()}T00:00:00Z",
        ),
        # Charged 3 days ago for a box that hasn't been delivered yet — the real last charge.
        _billing_item(
            "sub-1",
            future_delivery.isoformat(),
            97.50,
            created_at=f"{recent_charge.isoformat()}T00:00:00Z",
        ),
    ]
    latest, future, _, _ = client._accumulate_order_prices(items)

    assert "sub-1" in latest
    assert latest["sub-1"].date() == recent_charge  # most recent CHARGE, not last delivery
    assert "sub-1" in future
    assert future["sub-1"][0] == future_delivery


def test_accumulate_order_prices_next_order_nr_is_earliest_future() -> None:
    """next_order_nr_by_subscription should point to the nearest upcoming delivery."""
    client = _make_client()
    items = [
        _billing_item("sub-1", "2026-06-22", 90.00, order_nr="99999999999"),
        _billing_item("sub-1", "2026-06-15", 97.50, order_nr="28192254942"),
    ]
    _, _, next_order_nr, _ = client._accumulate_order_prices(items)

    assert next_order_nr.get("sub-1") == "28192254942"


# ---------------------------------------------------------------------------
# Billing logic: _compute_next_delivery_total
# ---------------------------------------------------------------------------


def test_compute_next_delivery_total_sums_across_subscriptions() -> None:
    """next_delivery_total should sum all charges whose delivery date equals the earliest future date."""
    client = _make_client()
    future_by_subscription = {
        "sub-1": (date(2026, 6, 15), datetime(2026, 6, 11, tzinfo=timezone.utc)),
    }
    next_order_nr = {"sub-1": "28192254942"}
    price_by_key = {
        ("sub-1", date(2026, 6, 15)): (97.50, "USD"),
        ("sub-1", date(2026, 6, 22)): (85.00, "USD"),
    }
    data = HelloFreshAccountData().finalize()
    client._compute_next_delivery_total(data, future_by_subscription, next_order_nr, price_by_key)

    assert data.next_delivery_total == 97.50
    assert data.next_delivery_total_currency == "USD"
    assert data.recent_order_id == "28192254942"


def test_compute_next_delivery_total_empty_future() -> None:
    """With no future deliveries, data fields remain None."""
    client = _make_client()
    data = HelloFreshAccountData().finalize()
    client._compute_next_delivery_total(data, {}, {}, {})

    assert data.next_delivery_total is None
    assert data.recent_order_id is None


# ---------------------------------------------------------------------------
# Billing logic: recent_payment_date only uses past deliveries
# ---------------------------------------------------------------------------


def test_recent_payment_date_is_most_recent_actual_charge() -> None:
    """recent_payment_date is the latest order ALREADY CHARGED, even if its box is upcoming.

    Regression: filtering on delivery date left this ~a week behind the customer's real last
    charge, because the upcoming box (billed days ago) was skipped in favour of the prior one.
    """
    client = _make_client()
    today = datetime.now(timezone.utc).date()
    older_charge = today - timedelta(days=14)
    recent_charge = today - timedelta(days=3)
    items = [
        _billing_item(
            "sub-1",
            (today - timedelta(days=10)).isoformat(),
            80.00,
            created_at=f"{older_charge.isoformat()}T00:00:00Z",
        ),
        _billing_item(
            "sub-1",
            (today + timedelta(days=4)).isoformat(),  # box not yet delivered
            97.50,
            created_at=f"{recent_charge.isoformat()}T00:00:00Z",  # but already billed
        ),
    ]
    latest, future, _, _ = client._accumulate_order_prices(items)

    subscriptions = [HelloFreshSubscription(subscription_id="sub-1")]
    client._apply_recent_payment_dates(subscriptions, latest, future)

    assert subscriptions[0].recent_payment_date == recent_charge


def test_recent_payment_date_none_when_charge_is_still_in_the_future() -> None:
    """A charge dated in the future (not yet billed) does not count as a recent payment."""
    client = _make_client()
    today = datetime.now(timezone.utc).date()
    future_charge = today + timedelta(days=2)
    items = [
        _billing_item(
            "sub-1",
            (today + timedelta(days=7)).isoformat(),
            97.50,
            created_at=f"{future_charge.isoformat()}T00:00:00Z",
        ),
    ]
    latest, future, _, _ = client._accumulate_order_prices(items)

    subscriptions = [HelloFreshSubscription(subscription_id="sub-1")]
    client._apply_recent_payment_dates(subscriptions, latest, future)

    assert subscriptions[0].recent_payment_date is None


def test_enrich_account_credit_reads_balance_amount_and_currency() -> None:
    """The credit balance endpoint populates account_credit and its currency."""
    client = _make_client()
    captured: dict[str, object] = {}

    async def fake_api_get(path, params=None, **_kwargs):
        captured["path"] = path
        captured["params"] = params
        return object()

    async def fake_response_json(_response):
        return {
            "amount": 12.5,
            "cash": 12.5,
            "bonus": 0,
            "currencyCode": "USD",
            "restrictedAmount": 0,
        }

    client._async_api_get = fake_api_get  # type: ignore[method-assign]
    client._async_response_json = fake_response_json  # type: ignore[method-assign]

    data = HelloFreshAccountData()
    subscription = HelloFreshSubscription(subscription_id="sub-1", raw={"uuid": "cust-uuid-1"})

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(client._async_enrich_account_credit(data, [subscription]))

    assert data.account_credit == 12.5
    assert data.account_credit_currency == "USD"
    assert captured["path"] == "/gw/payments/customers/cust-uuid-1/balance"
    assert captured["params"] == {"business_unit": "US", "country": "US"}


def test_enrich_account_credit_skips_without_customer_uuid() -> None:
    """No credit is recorded when the subscription payload has no customer UUID."""
    client = _make_client()

    async def fail_api_get(*_args, **_kwargs):
        raise AssertionError("balance endpoint should not be called without a UUID")

    client._async_api_get = fail_api_get  # type: ignore[method-assign]

    data = HelloFreshAccountData()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(
        client._async_enrich_account_credit(data, [HelloFreshSubscription(subscription_id="s")])
    )

    assert data.account_credit is None
    assert data.account_credit_currency is None


def test_get_week_returns_full_recipe_detail_for_dashboard() -> None:
    """The get_weeks service reads per-week recipes via get_week + as_dict.

    Recipes are kept out of sensor attributes, so the dashboard relies on this
    serialization carrying recipe names and is_selected for the chosen week.
    """
    data = HelloFreshAccountData(
        weeks=[
            HelloFreshWeek(
                week_id="2026-W25",
                display_name="Week 25",
                subscription_id="sub-1",
                delivery_date=date(2026, 6, 16),
                meals_required=2,
                meals_selected=1,
                recipes=[
                    HelloFreshRecipe(recipe_id="r1", name="Pasta", is_selected=True),
                    HelloFreshRecipe(recipe_id="r2", name="Tacos", is_selected=False),
                ],
            ),
            HelloFreshWeek(
                week_id="2026-W26",
                display_name="Week 26",
                subscription_id="sub-1",
                delivery_date=date(2026, 6, 23),
            ),
        ],
    ).finalize()

    week = data.get_week("2026-W25")
    assert week is not None
    payload = week.as_dict()
    assert payload["week_id"] == "2026-W25"
    assert payload["meals_selected"] == 1
    selected = [r["name"] for r in payload["recipes"] if r["is_selected"]]
    assert selected == ["Pasta"]
    # Unknown week resolves to None (service returns an empty list in that case).
    assert data.get_week("2026-W99") is None
