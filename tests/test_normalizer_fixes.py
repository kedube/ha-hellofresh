"""Regression tests for the data layer: payload normalizers, models and parsers.

Each test pins one behaviour that was wrong: a Market item's index overwritten by a meal's, a
double portion that shrank to one the day after delivery, a complete box that still "needed
selection", a week's lifecycle state read as the carrier's status, and the rest. The payloads
are shaped as docs/hellofresh-api.md documents them; ids and addresses are made up.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta, timezone
import logging
from typing import Any

import pytest

from custom_components.hellofresh.api import (
    HelloFreshAccountData,
    HelloFreshClient,
    HelloFreshError,
    HelloFreshMarketItem,
    HelloFreshOrder,
    HelloFreshRecipe,
    HelloFreshSubscription,
    HelloFreshWeek,
)
from custom_components.hellofresh.meal_plan import market_quantity, recipe_quantity
from custom_components.hellofresh.models import (
    HelloFreshCatalogRecipe,
    HelloFreshDeliveryOption,
    HelloFreshFavorite,
    HelloFreshRecipeCollection,
    HelloFreshRecipeDetail,
    WeekLookup,
    same_box,
)
from custom_components.hellofresh.parsers import (
    as_utc,
    coerce_int,
    dict_path,
    extract_scm_tracking_details,
    extract_tracking_details,
    http_url_or_none,
    local_today,
    parse_datetime,
)
from custom_components.hellofresh.sensor_helpers import sensor_native_value

SUBSCRIPTION_ID = "1234567"
OTHER_SUBSCRIPTION_ID = "8642086"


def _client(**options: Any) -> HelloFreshClient:
    return HelloFreshClient(session=None, **options)  # type: ignore[arg-type]


def _subscription(subscription_id: str = SUBSCRIPTION_ID, **fields: Any) -> HelloFreshSubscription:
    return HelloFreshSubscription(subscription_id=subscription_id, locale="en-US", **fields)


def _iso_week(day: date) -> str:
    iso = day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


# ---- 1. A Market item keeps its own index -----------------------------------------------------


def _pairing_item(index: int, name: str, courses: list[int]) -> dict[str, Any]:
    return {
        "index": index,
        "recipe": {"id": f"recipe-{index}", "name": name},
        "priceCatalog": {"basePrice": 499},
        "selection": {
            "skipped": False,
            "oneOffQuantity": 1,
            "preselectedQuantity": 0,
            "courses": [{"index": course} for course in courses],
        },
    }


def _market(raw_items: list[dict[str, Any]]) -> list[HelloFreshMarketItem]:
    groups = [{"groupType": "modularity", "sku": "US-AMO-0-0-0", "addOns": raw_items}]
    return _client()._build_market_items({"addOns": {"groups": groups}})


def test_a_pairing_add_on_keeps_its_own_index() -> None:
    """The meals a pairing add-on is linked to are its courses, never its own index.

    The cart orders an add-on by its index, so one overwritten by a meal's course index would
    be re-sent under that meal's index on every save of the week.
    """
    (item,) = _market([_pairing_item(10089, "Pairing A", [12, 37])])
    assert item.index == 10089
    assert item.courses == [12, 37]
    assert item.item_id == "recipe-10089"


def test_two_pairing_add_ons_linked_to_the_same_course_are_both_kept() -> None:
    """Both took the course's index, and the second was dropped as a duplicate of the first."""
    items = _market(
        [_pairing_item(10089, "Pairing A", [12]), _pairing_item(10090, "Pairing B", [12])]
    )
    assert [(item.name, item.index, item.courses) for item in items] == [
        ("Pairing A", 10089, [12]),
        ("Pairing B", 10090, [12]),
    ]


# ---- 2. Double portions and Market quantities survive delivery ---------------------------------


def _delivered_week(days_ago: int, *, grace_weeks: int | None = None) -> HelloFreshWeek:
    """One week run through the normalizers and merges in the client's order.

    The box: chicken twice, beef once and two garlic breads from the Market. The deliveries
    week lists no meals, the menu has the quantities, and past-deliveries lists what came
    without any quantity, as the API does.
    """
    client = _client(menu_grace_weeks=grace_weeks)
    subscription = _subscription(meals_required=3)
    delivery = local_today() - timedelta(days=days_ago)
    week_id = _iso_week(delivery)
    weeks, _orders = client._normalize_weeks_payload(
        {
            "items": [
                {
                    "id": week_id,
                    "subscriptionId": SUBSCRIPTION_ID,
                    "deliveryDate": delivery.isoformat(),
                    "status": "DELIVERED",
                    "product": {"handle": "US-CBU-3-2-0", "specs": {"meals": 3, "size": 2}},
                }
            ]
        },
        subscription=subscription,
    )
    menu_weeks = client._normalize_menu_weeks(
        [
            {
                "week": week_id,
                "meals": [
                    {
                        "index": 1,
                        "selection": {"quantity": 2, "limit": 0},
                        "recipe": {"id": "chicken", "name": "Chicken"},
                    },
                    {
                        "index": 2,
                        "selection": {"quantity": 1, "limit": 0},
                        "recipe": {"id": "beef", "name": "Beef"},
                    },
                    {
                        "index": 3,
                        "selection": {"quantity": 0, "limit": 0},
                        "recipe": {"id": "pasta", "name": "Pasta"},
                    },
                ],
                "addOns": {
                    "groups": [
                        {
                            "groupType": "sides",
                            "sku": "US-ASD-0-0-0",
                            "addOns": [
                                {
                                    "index": 10003,
                                    "recipe": {"id": "bread", "name": "Garlic Bread"},
                                    "priceCatalog": {"basePrice": 499},
                                    "selection": {
                                        "skipped": False,
                                        "oneOffQuantity": 2,
                                        "preselectedQuantity": 0,
                                        "courses": [],
                                    },
                                }
                            ],
                        }
                    ]
                },
            }
        ],
        subscription=subscription,
    )
    history = client._normalize_past_delivery_payload(
        {
            "weeks": [
                {
                    "week": week_id,
                    "menuId": "menu-1",
                    "meals": [
                        {"id": "chicken", "name": "Chicken", "recipe": {}},
                        {"id": "beef", "name": "Beef", "recipe": {}},
                    ],
                    "addons": [{"id": "bread", "name": "Garlic Bread"}],
                }
            ]
        },
        [subscription],
    )
    weeks = client._merge_menu_weeks_into_account_weeks(account_weeks=weeks, menu_weeks=menu_weeks)
    weeks = client._merge_past_delivery_recipes_into_account_weeks(
        account_weeks=weeks, past_delivery_weeks=history
    )
    client._merge_past_delivery_market_items(account_weeks=weeks, past_delivery_weeks=history)
    client._apply_market_items(weeks)
    (week,) = weeks
    return week


@pytest.mark.parametrize(
    ("days_ago", "grace_weeks"),
    [
        (0, None),  # delivery day: the menu's own quantities
        (1, None),  # the day after, the catalog still browsable
        (1, 0),  # the day after with no grace window: only what was delivered is kept
    ],
)
def test_a_double_portion_and_two_add_ons_are_still_two_after_delivery(
    days_ago: int, grace_weeks: int | None
) -> None:
    """The meal plan can still place the second portion once the box has arrived.

    History lists each dish once, so the day after delivery the second chicken and the second
    garlic bread were gone and the plan refused them as "already planned 1 time(s)".
    """
    week = _delivered_week(days_ago, grace_weeks=grace_weeks)
    chicken = next(recipe for recipe in week.recipes if recipe.recipe_id == "chicken")
    beef = next(recipe for recipe in week.recipes if recipe.recipe_id == "beef")
    bread = next(item for item in week.market_items if item.name == "Garlic Bread")
    assert (recipe_quantity(chicken), recipe_quantity(beef)) == (2, 1)
    assert market_quantity(bread) == 2
    # Three portions fill the 3-meal box.
    assert (week.meals_selected, week.meals_required) == (3, 3)
    assert week.selection_progress == "3/3"


# ---- 3. meals_selected counts portions --------------------------------------------------------


def test_one_doubled_meal_is_a_complete_two_meal_box() -> None:
    """HelloFresh counts a meal at quantity 2 as two meals, so the box is full at 2/2."""
    client = _client()
    (week,) = client._normalize_menu_weeks(
        [
            {
                "week": "2026-W48",
                "meals": [
                    {
                        "index": 75,
                        "selection": {"quantity": 2, "limit": 0},
                        "recipe": {"id": "chicken", "name": "Chicken"},
                    },
                    {
                        "index": 76,
                        "selection": {"quantity": 0, "limit": 0},
                        "recipe": {"id": "beef", "name": "Beef"},
                    },
                ],
            }
        ],
        subscription=_subscription(meals_required=2),
    )
    week.allowed_actions = {"mealSwap": True}
    week.selection_deadline = datetime.now(UTC) + timedelta(days=2)
    week.delivery_date = local_today() + timedelta(days=7)
    assert week.meals_selected == 2
    assert week.selection_progress == "2/2"
    assert week.needs_selection is False


def test_a_deliveries_week_listing_its_meals_counts_portions_too() -> None:
    weeks, _orders = _client()._normalize_weeks_payload(
        {
            "items": [
                {
                    "id": "2026-W48",
                    "meals": [
                        {"id": "chicken", "name": "Chicken", "selection": {"quantity": 2}},
                        {"id": "beef", "name": "Beef", "selection": {"quantity": 1}},
                    ],
                }
            ]
        },
        subscription=_subscription(),
    )
    assert weeks[0].meals_selected == 3


def test_a_menu_week_that_states_zero_selected_meals_reads_zero() -> None:
    """A stated count of 0 is an answer, not a missing value to fill in from the recipes."""
    (week,) = _client()._normalize_menu_weeks(
        [
            {
                "week": "2026-W48",
                "mealsSelected": 0,
                "meals": [
                    {"index": 1, "selection": {"quantity": 1}, "recipe": {"id": "a", "name": "A"}},
                    {"index": 2, "selection": {"quantity": 1}, "recipe": {"id": "b", "name": "B"}},
                ],
            }
        ],
        subscription=_subscription(meals_required=2),
    )
    assert week.meals_selected == 0


# ---- 4. A week's lifecycle state is not the carrier's status ------------------------------------


def test_the_weeks_own_state_is_not_read_as_a_tracking_status() -> None:
    running = extract_tracking_details({"status": "RUNNING", "state": "RUNNING"})
    assert running["tracking_status"] is None
    assert extract_tracking_details({"state": "DELIVERED"})["tracking_status"] is None
    # A field that names a carrier status is still read, wherever it sits.
    assert extract_tracking_details({"trackingStatus": "in_transit"})["tracking_status"] == (
        "in_transit"
    )
    # So is a state inside a node about the shipment itself.
    assert (
        extract_tracking_details({"state": "RUNNING", "tracking": {"state": "in_transit"}})[
            "tracking_status"
        ]
        == "in_transit"
    )


def test_a_week_marked_delivered_early_cannot_vouch_for_itself() -> None:
    """DELIVERED with the arrival still ahead and no carrier lookup: the box is on its way.

    The week's own state used to stand in for the carrier's status, so "delivered" confirmed
    itself, and Last delivery day reported a time that had not come yet.
    """
    client = _client()
    subscription = _subscription()
    today = local_today()
    arrival = datetime.now(UTC) + timedelta(hours=3)
    weeks, orders = client._normalize_weeks_payload(
        {
            "items": [
                {
                    "id": _iso_week(today),
                    "subscriptionId": SUBSCRIPTION_ID,
                    "deliveryDate": today.isoformat(),
                    "status": "DELIVERED",
                    "state": "DELIVERED",
                    "tracking": {
                        "tracking_id": "",
                        # Not a HelloFresh tracking page, so there is no carrier lookup.
                        "tracking_link": "https://carrier.example/track/0001",
                        "tracking_code": "TRACK0001",
                        "delivery_date": arrival.strftime("%Y-%m-%dT%H:%M:%S+0000"),
                    },
                }
            ]
        },
        subscription=subscription,
    )
    assert orders[0].tracking_status is None

    data = HelloFreshAccountData(
        weeks=weeks, orders=orders, subscriptions=[subscription]
    ).finalize()
    assert data.weeks[0].status == "ON_THE_WAY"
    assert data.weeks[0].delivered_at is None
    assert data.orders[0].status == "ON_THE_WAY"
    assert data.last_delivery_week is None


def test_weeks_without_tracking_are_not_the_tracked_shipment() -> None:
    """Six scheduled weeks and no shipment: there is nothing to track, not a box "Running"."""
    client = _client()
    subscription = _subscription()
    today = local_today()
    items = [
        {
            "id": _iso_week(today + timedelta(weeks=ahead)),
            "subscriptionId": SUBSCRIPTION_ID,
            "deliveryDate": (today + timedelta(weeks=ahead)).isoformat(),
            "status": "RUNNING",
            "state": "RUNNING",
            "tracking": None,
        }
        for ahead in range(1, 7)
    ]
    weeks, orders = client._normalize_weeks_payload({"items": items}, subscription=subscription)
    data = HelloFreshAccountData(
        weeks=weeks, orders=orders, subscriptions=[subscription]
    ).finalize()
    assert data.tracked_order is None
    assert sensor_native_value("shipment_tracking_status", data, "https://example.com") is None

    # The week that does have a shipment is the tracked one, however far out the others are.
    orders[0].tracking_number = "TRACK0001"
    orders[0].tracking_status = "in_transit"
    assert data.finalize().tracked_order is orders[0]


def test_an_order_with_only_a_status_is_not_a_tracked_shipment() -> None:
    order = HelloFreshOrder(
        order_id="o1", week_id="2026-W47", status="RUNNING", tracking_status="in_transit"
    )
    assert HelloFreshAccountData(orders=[order]).finalize().tracked_order is None
    order.tracking_url = "https://carrier.example/track/0001"
    assert HelloFreshAccountData(orders=[order]).finalize().tracked_order is order


# ---- 6. Delivered add-ons stay out of the meal list --------------------------------------------


def test_history_meals_that_are_the_weeks_add_ons_are_not_meals() -> None:
    """History can list a bought add-on among the meals; its own ``addons`` say which."""
    (week,) = _client()._normalize_past_delivery_payload(
        {
            "weeks": [
                {
                    "week": "2026-W25",
                    "meals": [
                        {"id": "steak", "name": "Cantina Steak"},
                        {"id": "chicken", "name": "Chicken"},
                        {"id": "gyoza", "name": "Pork & Shiitake Gyoza"},
                    ],
                    "addons": [{"id": "gyoza", "name": "Pork & Shiitake Gyoza"}],
                }
            ]
        },
        [_subscription()],
    )
    assert [recipe.name for recipe in week.recipes] == ["Cantina Steak", "Chicken"]
    assert (week.meals_selected, week.meals_required) == (2, 2)
    assert [item.name for item in week.market_items] == ["Pork & Shiitake Gyoza"]


# ---- 7. Fields are read from their documented place --------------------------------------------


def test_a_delivered_weeks_shipping_method_is_not_a_meal_tags_type() -> None:
    """Past-deliveries meals carry tags shaped {name, type}; none of that describes the box."""
    (week,) = _client()._normalize_past_delivery_payload(
        {
            "weeks": [
                {
                    "week": "2026-W25",
                    "meals": [
                        {
                            "id": "chicken",
                            "name": "Chicken",
                            "tags": [
                                {
                                    "name": "Calorie Smart",
                                    "type": "calorie-smart",
                                    "displayLabel": True,
                                    "size": "large",
                                }
                            ],
                        }
                    ],
                }
            ]
        },
        [_subscription()],
    )
    assert week.shipping_method is None
    assert week.box_size is None
    assert week.slot_label is None


def test_a_weeks_delivery_fields_come_from_its_delivery_option() -> None:
    weeks, _orders = _client()._normalize_weeks_payload(
        {
            "items": [
                {
                    "id": "2026-W47",
                    # A node that comes first and also has a `type` and a `name`.
                    "promotion": {"type": "voucher", "name": "Welcome back"},
                    "product": {"handle": "US-CBU-3-2-0", "displayName": "Classic Box"},
                    "deliveryOption": {
                        "handle": "US-1-0800-2000",
                        "deliveryName": "Mondays: 8AM - 8PM",
                        "type": "standard",
                    },
                }
            ]
        },
        subscription=_subscription(),
    )
    week = weeks[0]
    assert week.shipping_method == "standard"
    assert week.slot_label == "Mondays: 8AM - 8PM"
    assert (week.display_name, week.menu_title) == ("Classic Box", "Classic Box")


@pytest.mark.parametrize(
    ("product", "expected"),
    [
        ({"price": 5000, "shippingPrice": 1299}, 62.99),
        ({"price": 5000, "specialFee": None, "shippingPrice": 1299}, 62.99),
        # A zero fee is a real zero: it must not stop the shipping price being added.
        ({"price": 5000, "specialFee": 0, "shippingPrice": 1299}, 62.99),
        ({"price": 5000, "specialFee": 299, "shippingPrice": 1299}, 65.98),
    ],
)
def test_a_box_total_adds_each_fee_to_the_price(product: dict[str, Any], expected: float) -> None:
    _weeks, orders = _client(country="us")._normalize_weeks_payload(
        {
            "items": [
                {
                    "id": "2026-W47",
                    # Ahead of `product`, where a free delivery option's 0 was taken for the fee.
                    "deliveryOption": {"handle": "US-1-0800-2000", "priceInCents": 0},
                    "product": product,
                }
            ]
        },
        subscription=_subscription(),
    )
    assert orders[0].total_price == expected


def _documented_subscription() -> dict[str, Any]:
    """A subscription with the fields docs/hellofresh-api.md lists, in a plausible order."""
    return {
        "id": SUBSCRIPTION_ID,
        "customerPlanId": "00000000-0000-4000-8000-000000000001",
        "isActive": True,
        "pausedAt": None,
        "canceledAt": None,
        "customer": {
            "id": "2000002",
            "uuid": "00000000-0000-4000-8000-000000000002",
            "locale": "en-US",
        },
        "shippingAddress": {
            "address1": "100 Main St",
            "city": "Anytown",
            "postcode": "01103",
            "region": {"code": "MA", "name": "Massachusetts"},
            "state": "MA",
        },
        "product": {"sku": "US-CBU-3-2-0"},
        "productType": {
            "productName": "Classic - 3 meals per week for 2 people",
            "specs": {"meals": 3, "size": 2},
            "family": {"handle": "classic-box-unified"},
        },
        "preset": "quick",
        "deliveryTime": "US-1-0800-2000",
        "nextDeliveryOption": {"deliveryName": "Mondays: 8AM - 8PM"},
    }


def test_a_subscription_is_named_after_its_plan_not_its_address() -> None:
    """The first `name` in the payload was the delivery address's region."""
    subscription = _client()._subscription_from_raw_subscription(_documented_subscription())
    assert subscription.plan_name == "Classic - 3 meals per week for 2 people"
    assert subscription.display_name == "Classic - 3 meals per week for 2 people"
    assert (subscription.meals_required, subscription.servings) == (3, 2)
    # A `state` further down is the address's, not the subscription's status.
    assert subscription.status == "active"


# ---- 8. Unexpected shapes cost one entry, not the refresh --------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"customer": None},
        {"customer": "2000002"},
        {"plan": "US-CBU-3-2-0"},
        {"productType": None},
    ],
)
def test_a_subscription_with_a_field_of_another_shape_still_loads(
    overrides: dict[str, Any],
) -> None:
    raw = {**_documented_subscription(), **overrides}
    subscription = _client()._subscription_from_raw_subscription(raw)
    assert subscription.subscription_id == SUBSCRIPTION_ID
    if "customer" in overrides:
        assert subscription.account_id is None
        assert subscription.loyalty_boxes_until_next_freebie is None
    if "plan" in overrides:
        assert subscription.meals_required == 3


def test_coerce_int_takes_infinity_for_no_number() -> None:
    assert coerce_int(float("inf")) is None
    assert coerce_int(float("-inf")) is None
    assert coerce_int(float("nan")) is None


def test_a_menu_with_an_infinite_number_still_loads() -> None:
    """Python's JSON parser accepts a bare Infinity, and int() of it raises OverflowError."""
    (week,) = _client()._normalize_menu_weeks(
        [
            {
                "week": "2026-W48",
                "meals": [
                    {
                        "index": float("inf"),
                        "selection": {"quantity": 1},
                        "recipe": {"id": "a", "name": "A"},
                    },
                    {"index": 2, "selection": {"quantity": 1}, "recipe": {"id": "b", "name": "B"}},
                ],
            }
        ],
        subscription=_subscription(),
    )
    assert [(recipe.name, recipe.course_index) for recipe in week.recipes] == [
        ("A", None),
        ("B", 2),
    ]


def test_a_meal_whose_name_is_not_text_does_not_break_the_merges() -> None:
    (week,) = _client()._normalize_menu_weeks(
        [
            {
                "week": "2026-W48",
                "meals": [
                    {"index": 1, "recipe": {"id": "a", "name": {"en": "A"}}},
                    {"index": 2, "recipe": {"id": "b", "name": "B"}},
                ],
            }
        ],
        subscription=_subscription(),
    )
    assert [recipe.name for recipe in week.recipes] == ["Meal", "B"]


def test_a_menu_meal_that_cannot_be_parsed_costs_only_that_meal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One bad meal is left out and noted; its week, the other weeks and the refresh go on."""
    client = _client()
    client._reset_debug_trace()
    build = client._recipe_from_raw_meal

    def flaky(raw_meal: dict[str, Any], **options: Any) -> HelloFreshRecipe:
        if raw_meal.get("index") == 2:
            raise ValueError("unexpected shape")
        return build(raw_meal, **options)

    client._recipe_from_raw_meal = flaky  # type: ignore[method-assign]
    meals = [{"index": i, "recipe": {"id": f"r{i}", "name": f"Meal {i}"}} for i in (1, 2, 3)]
    with caplog.at_level(logging.WARNING):
        weeks = client._normalize_menu_weeks(
            [{"week": "2026-W48", "meals": meals}, {"week": "2026-W49", "meals": meals[:1]}],
            subscription=_subscription(),
        )
    assert [[recipe.name for recipe in week.recipes] for week in weeks] == [
        ["Meal 1", "Meal 3"],
        ["Meal 1"],
    ]
    assert "1 meal(s) in HelloFresh menu week 2026-W48" in caplog.text
    assert client._debug_trace["skipped_entries"] == [
        {"entry": "1 meal(s) in HelloFresh menu week 2026-W48", "error": "ValueError"}
    ]


def test_a_menu_week_that_cannot_be_parsed_costs_only_that_week() -> None:
    """One menu week of an unexpected shape is left out; the weeks around it still load."""
    client = _client()
    client._reset_debug_trace()
    extract = client._extract_menu_week_recipe_candidates

    def flaky(node: Any) -> list[dict[str, Any]]:
        if isinstance(node, dict) and node.get("week") == "2026-W49":
            raise TypeError("unexpected shape")
        return extract(node)

    client._extract_menu_week_recipe_candidates = flaky  # type: ignore[method-assign]
    meal = {"index": 1, "recipe": {"id": "a", "name": "A"}}
    weeks = client._normalize_menu_weeks(
        [{"week": week_id, "meals": [meal]} for week_id in ("2026-W48", "2026-W49", "2026-W50")],
        subscription=_subscription(),
    )
    assert [week.week_id for week in weeks] == ["2026-W48", "2026-W50"]
    assert client._debug_trace["skipped_entries"] == [
        {"entry": "HelloFresh menu week entry 1", "error": "TypeError"}
    ]


def test_a_delivered_week_that_cannot_be_parsed_costs_only_that_week() -> None:
    """A week whose add-on has a number where its tags should be; the next week still lands."""
    weeks = _client()._normalize_past_delivery_payload(
        {
            "weeks": [
                {
                    "week": "2026-W25",
                    "meals": [{"id": "a", "name": "A"}],
                    "addons": [{"id": "x", "name": "Gyoza", "tags": 5}],
                },
                {"week": "2026-W24", "meals": [{"id": "b", "name": "B"}]},
            ]
        },
        [_subscription()],
    )
    assert [week.week_id for week in weeks] == ["2026-W24"]


def test_a_delivery_option_priced_as_text_serializes() -> None:
    option = HelloFreshDeliveryOption.from_api({"handle": "US-1-0800-2000", "priceInCents": "0"})
    assert option is not None
    assert (option.price_cents, option.as_dict()["price"]) == (0, 0.0)
    # Built directly with a text price, it still serializes.
    direct = HelloFreshDeliveryOption(handle="US-2-0800-2000", price_cents="1299")  # type: ignore[arg-type]
    assert direct.as_dict()["price"] == 12.99
    assert HelloFreshDeliveryOption(handle="US-3-0800-2000").as_dict()["price"] is None


# ---- 9 and 10. Skipped weeks and shared week ids -----------------------------------------------


def test_a_skipped_week_is_not_a_delivery_this_week() -> None:
    today = local_today()
    week = HelloFreshWeek(
        week_id=_iso_week(today),
        display_name="This week",
        subscription_id=SUBSCRIPTION_ID,
        delivery_date=today,
        status="PAUSED",
        is_skipped=True,
    )
    order = HelloFreshOrder(
        order_id="o1",
        week_id=week.week_id,
        status="PAUSED",
        subscription_id=SUBSCRIPTION_ID,
        delivery_date=today,
    )
    data = HelloFreshAccountData(
        weeks=[week], orders=[order], subscriptions=[_subscription()]
    ).finalize()
    assert data.delivery_count_this_week == 0
    assert data.upcoming_orders == []


def _two_subscriptions_sharing_a_week() -> HelloFreshAccountData:
    """The primary subscription skipped the week; the second one's box is coming."""
    delivery = local_today() + timedelta(days=3)
    week_id = _iso_week(delivery)

    def week(subscription_id: str, *, skipped: bool) -> HelloFreshWeek:
        return HelloFreshWeek(
            week_id=week_id,
            display_name=f"Week of {subscription_id}",
            subscription_id=subscription_id,
            delivery_date=delivery,
            status="PAUSED" if skipped else "RUNNING",
            is_skipped=skipped,
        )

    def order(subscription_id: str, status: str) -> HelloFreshOrder:
        return HelloFreshOrder(
            order_id=f"order-{subscription_id}",
            week_id=week_id,
            status=status,
            subscription_id=subscription_id,
            delivery_date=delivery,
        )

    return HelloFreshAccountData(
        weeks=[week(SUBSCRIPTION_ID, skipped=True), week(OTHER_SUBSCRIPTION_ID, skipped=False)],
        orders=[order(SUBSCRIPTION_ID, "PAUSED"), order(OTHER_SUBSCRIPTION_ID, "RUNNING")],
        subscriptions=[_subscription(), _subscription(OTHER_SUBSCRIPTION_ID)],
    ).finalize()


def test_one_subscription_skipping_a_week_does_not_hide_the_others_box() -> None:
    data = _two_subscriptions_sharing_a_week()
    assert [order.subscription_id for order in data.upcoming_orders] == [OTHER_SUBSCRIPTION_ID]
    assert data.next_order is not None
    assert data.next_order.order_id == f"order-{OTHER_SUBSCRIPTION_ID}"


def test_weeks_and_orders_can_be_looked_up_per_subscription() -> None:
    data = _two_subscriptions_sharing_a_week()
    week_id = data.weeks[0].week_id

    # With only the week id, both lookups answer for the primary subscription, as before.
    primary_week = data.get_week(week_id)
    assert primary_week is not None and primary_week.subscription_id == SUBSCRIPTION_ID
    primary_order = data.get_order_for_week(week_id)
    assert primary_order is not None and primary_order.subscription_id == SUBSCRIPTION_ID

    other_week = data.get_week(week_id, OTHER_SUBSCRIPTION_ID)
    assert other_week is not None and other_week.is_skipped is False
    other_order = data.get_order_for_week(week_id, OTHER_SUBSCRIPTION_ID)
    assert other_order is not None and other_order.order_id == f"order-{OTHER_SUBSCRIPTION_ID}"

    assert data.get_week(week_id, "no-such-subscription") is None
    assert data.get_order_for_week(week_id, "no-such-subscription") is None


def test_an_order_without_a_subscription_id_still_finds_its_week() -> None:
    order = HelloFreshOrder(order_id="o1", week_id="2026-W47", status="RUNNING")
    week = HelloFreshWeek(week_id="2026-W47", display_name="W47", subscription_id=SUBSCRIPTION_ID)
    data = HelloFreshAccountData(weeks=[week], orders=[order]).finalize()
    assert data.get_order_for_week("2026-W47") is order
    assert data.get_order_for_week("2026-W47", SUBSCRIPTION_ID) is order


_HISTORY_ROWS = {"weeks": [{"week": "2026-W25", "meals": [{"id": "a", "name": "A"}]}]}


def test_history_rows_belong_to_the_subscription_they_were_requested_for() -> None:
    """past-deliveries rows name no subscription; they are not all the first one's."""
    first, second = _subscription(), _subscription(OTHER_SUBSCRIPTION_ID)
    (week,) = _client()._normalize_past_delivery_payload(
        _HISTORY_ROWS, [first, second], requested_subscription=second
    )
    assert week.subscription_id == OTHER_SUBSCRIPTION_ID
    # Without one (an account-wide endpoint) the first subscription still stands in.
    (week,) = _client()._normalize_past_delivery_payload(_HISTORY_ROWS, [first, second])
    assert week.subscription_id == SUBSCRIPTION_ID


def test_both_subscriptions_keep_their_history_for_a_shared_week() -> None:
    """The second subscription's week was dropped as "already filled" by the first's."""
    client = HelloFreshClient(session=object())  # type: ignore[arg-type]
    subscriptions = [_subscription(), _subscription(OTHER_SUBSCRIPTION_ID)]
    requests: list[dict[str, Any]] = []

    class DummyResponse:
        status = 200

    async def fake_api_get(path: str, params: Any = None, extra_headers: Any = None) -> Any:
        if path != "/gw/my-deliveries/past-deliveries":
            raise HelloFreshError("unavailable")
        requests.append(params)
        return DummyResponse()

    async def fake_response_json(_response: Any) -> dict[str, Any]:
        subscription_id = requests[-1]["subscription"]
        meal = {"id": f"meal-{subscription_id}", "name": f"Dinner for {subscription_id}"}
        return {"weeks": [{"week": "2026-W25", "meals": [meal]}], "nextWeek": None}

    client._async_api_get = fake_api_get  # type: ignore[method-assign]
    client._async_response_json = fake_response_json  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    try:
        weeks = loop.run_until_complete(client._async_get_past_delivery_weeks(subscriptions))
    finally:
        loop.close()

    assert {(week.subscription_id, week.recipes[0].name) for week in weeks} == {
        (SUBSCRIPTION_ID, f"Dinner for {SUBSCRIPTION_ID}"),
        (OTHER_SUBSCRIPTION_ID, f"Dinner for {OTHER_SUBSCRIPTION_ID}"),
    }


# ---- 11. The debug trace does not make two polls differ ------------------------------------------


def test_the_debug_trace_takes_no_part_in_equality() -> None:
    """Concurrent menu fetches finish in any order; identical data must still compare equal."""
    first = HelloFreshAccountData(debug_trace={"menu_attempts": [{"week": "a"}, {"week": "b"}]})
    second = HelloFreshAccountData(debug_trace={"menu_attempts": [{"week": "b"}, {"week": "a"}]})
    assert first == second
    assert HelloFreshAccountData(account_id="1") != HelloFreshAccountData(account_id="2")


# ---- 13. A history row's own subscription id is used --------------------------------------------


def test_a_history_row_with_its_own_subscription_id_needs_no_default() -> None:
    (week,) = _client()._normalize_past_delivery_payload(
        {
            "weeks": [
                {
                    "week": "2026-W25",
                    "subscription_id": SUBSCRIPTION_ID,
                    "meals": [{"id": "a", "name": "A"}],
                }
            ]
        },
        [],
    )
    assert week.subscription_id == SUBSCRIPTION_ID
    # No id on the row and no subscription to fall back on: nothing to key the week by.
    assert _client()._normalize_past_delivery_payload(_HISTORY_ROWS, []) == []


# ---- 14. Only http(s) links leave the data layer ------------------------------------------------


def test_recipe_detail_links_are_http_or_nothing() -> None:
    raw = {
        "id": "0123456789abcdef01234567",
        "name": "Chicken",
        "websiteUrl": "javascript:alert(1)",
        "canonicalLink": "https://www.example.com/recipes/chicken",
        "cardLink": "data:text/html,x",
        "videoLink": {"oops": 1},
    }
    detail = HelloFreshRecipeDetail.from_api(raw)
    assert detail.url == "https://www.example.com/recipes/chicken"
    assert detail.card_url is None
    assert detail.video_url is None

    detail = HelloFreshRecipeDetail.from_api(
        {
            **raw,
            "cardLink": "https://www.example.com/card.pdf",
            "videoLink": "https://www.example.com/clip.mp4",
        }
    )
    assert detail.card_url == "https://www.example.com/card.pdf"
    assert detail.video_url == "https://www.example.com/clip.mp4"


def test_catalog_and_cookbook_links_are_http_or_nothing() -> None:
    base = "https://img.example.com/hellofresh_s3"
    recipe = HelloFreshCatalogRecipe.from_api(
        {"recipeId": "abc", "name": "N", "imagePath": "javascript:alert(1)", "websiteUrl": "x"},
        image_base=base,
    )
    assert (recipe.image_url, recipe.url) == (None, None)
    recipe = HelloFreshCatalogRecipe.from_api(
        {
            "recipeId": "abc",
            "name": "N",
            "imagePath": "/image/n.jpg",
            "websiteUrl": "https://www.example.com/recipes/n",
        },
        image_base=base,
    )
    assert recipe.image_url == f"{base}/image/n.jpg"
    assert recipe.url == "https://www.example.com/recipes/n"

    collection = HelloFreshRecipeCollection.from_api(
        {"slug": "chicken-recipes", "name": "Chicken", "thumbnail": "javascript:alert(1)"},
        image_base=base,
    )
    assert collection.thumbnail_url is None
    collection = HelloFreshRecipeCollection.from_api(
        {"slug": "chicken-recipes", "name": "Chicken", "thumbnail": "/image/c.jpg"},
        image_base=base,
    )
    assert collection.thumbnail_url == f"{base}/image/c.jpg"

    favorite = HelloFreshFavorite.from_api(
        {
            "bookmark_id": "0123456789abcdef01234567-en-US",
            "thumbnail_url": "data:image/svg+xml,x",
            "url": "javascript:alert(1)",
        }
    )
    assert (favorite.image_url, favorite.url) == (None, None)
    favorite = HelloFreshFavorite.from_api(
        {
            "bookmark_id": "0123456789abcdef01234567-en-US",
            "thumbnail_url": "https://media.example.com/w_640/image/n.jpg",
            "url": "https://www.example.com/recipes/n",
        }
    )
    assert favorite.image_url == "https://media.example.com/w_640/image/n.jpg"
    assert favorite.url == "https://www.example.com/recipes/n"


def test_a_meals_video_and_a_boxs_tracking_link_are_http_or_nothing() -> None:
    client = _client()
    recipe = client._recipe_from_raw_meal(
        {"recipe": {"id": "a", "name": "A", "videoLink": "javascript:alert(1)"}}
    )
    assert recipe.video_url is None
    recipe = client._recipe_from_raw_meal(
        {"recipe": {"id": "a", "name": "A", "videoLink": " https://www.example.com/a.mp4 "}}
    )
    assert recipe.video_url == "https://www.example.com/a.mp4"

    details = extract_tracking_details(
        {
            "tracking": {
                "trackingUrl": "javascript:alert(1)",
                "tracking_link": "https://www.example.com/delivery-tracking/abc",
            }
        }
    )
    # The value that is no link does not hide the real one.
    assert details["tracking_url"] == "https://www.example.com/delivery-tracking/abc"
    local_file = extract_tracking_details({"tracking": {"tracking_link": "file:///etc/hosts"}})
    assert local_file["tracking_url"] is None
    box = {"carrier_tracking_url": "javascript:alert(1)", "hf_tracking_url": "https://x.example/t"}
    assert extract_scm_tracking_details(box)["tracking_url"] == "https://x.example/t"


# ---- 15. Smaller corrections --------------------------------------------------------------------


def test_the_next_selection_week_is_the_one_whose_deadline_comes_first() -> None:
    """Deadlines carry their market's offset; as text the later one sorted first."""
    day = datetime.now(UTC).date() + timedelta(days=5)
    pacific = timezone(timedelta(hours=-8))

    def week(week_id: str, deadline: datetime) -> HelloFreshWeek:
        return HelloFreshWeek(
            week_id=week_id,
            display_name=week_id,
            subscription_id=SUBSCRIPTION_ID,
            delivery_date=day + timedelta(days=7),
            selection_deadline=deadline,
            meals_required=3,
            meals_selected=0,
            allowed_actions={"mealSwap": True},
        )

    # 23:00 at UTC-8 is 07:00 UTC the next day, five hours after the other deadline.
    later = week("later", datetime(day.year, day.month, day.day, 23, 0, tzinfo=pacific))
    earlier = week(
        "earlier", datetime(day.year, day.month, day.day, 2, 0, tzinfo=UTC) + timedelta(days=1)
    )
    assert later.selection_deadline.isoformat() < earlier.selection_deadline.isoformat()

    data = HelloFreshAccountData(weeks=[later, earlier]).finalize()
    assert data.next_selection_week is earlier
    # A deadline without an offset is UTC, and still compares.
    earlier.selection_deadline = earlier.selection_deadline.replace(tzinfo=None)
    assert HelloFreshAccountData(weeks=[later, earlier]).finalize().next_selection_week is earlier


def test_projecting_a_selection_moves_the_quantity_with_the_flag() -> None:
    """The deliveries week names the chosen meals; the menu's flags AND counts follow it."""
    client = _client()
    account_week = HelloFreshWeek(
        week_id="2026-W47",
        display_name="W47",
        subscription_id=SUBSCRIPTION_ID,
        recipes=[HelloFreshRecipe(recipe_id="beef", name="Beef", is_selected=True)],
    )
    menu_week = HelloFreshWeek(
        week_id="2026-W47",
        display_name="W47",
        subscription_id=SUBSCRIPTION_ID,
        recipes=[
            HelloFreshRecipe(
                recipe_id="chicken", name="Chicken", is_selected=True, selected_quantity=1
            ),
            HelloFreshRecipe(recipe_id="beef", name="Beef"),
            HelloFreshRecipe(recipe_id="pasta", name="Pasta"),
        ],
    )
    (merged,) = client._merge_menu_weeks_into_account_weeks(
        account_weeks=[account_week], menu_weeks=[menu_week]
    )
    assert [(r.name, r.is_selected, r.selected_quantity) for r in merged.recipes] == [
        ("Chicken", False, None),
        ("Beef", True, 1),
        ("Pasta", False, None),
    ]


def test_a_market_item_serializes_its_courses_and_has_a_currency() -> None:
    client = _client(country="uk")
    priced_meal = {
        "index": 1,
        "recipe": {"id": "a", "name": "A"},
        "itemPrice": {
            "group": "classic",
            "pricePerUnit": {"currencyCode": "USD", "units": 10, "nanos": 990000000},
        },
    }
    addons = {
        "groups": [
            {
                "groupType": "modularity",
                "sku": "US-AMO-0-0-0",
                "addOns": [_pairing_item(10089, "Garlic Bread", [1])],
            }
        ]
    }
    (priced,) = client._normalize_menu_weeks(
        [{"week": "2026-W47", "meals": [priced_meal], "addOns": addons}],
        subscription=_subscription(),
    )
    (unpriced,) = client._normalize_menu_weeks(
        [
            {
                "week": "2026-W48",
                "meals": [{"index": 1, "recipe": {"id": "a", "name": "A"}}],
                "addOns": addons,
            }
        ],
        subscription=_subscription(),
    )
    client._apply_market_items([priced, unpriced])

    item = priced.market_items[0].as_dict()
    assert item["courses"] == [1]
    assert (item["price_cents"], item["currency"]) == (499, "USD")
    # No meal price to take the currency from: the market's own.
    assert unpriced.market_items[0].currency == "GBP"
    # A history item has no price, so it has no currency either.
    assert HelloFreshMarketItem(item_id="x", name="Gyoza").as_dict()["currency"] is None


# ---- 16. The shared helpers the duplicated code now goes through --------------------------------


def test_week_lookup_pairs_records_by_week_and_subscription() -> None:
    def week(subscription_id: str | None, name: str) -> HelloFreshWeek:
        return HelloFreshWeek(
            week_id="2026-W25", display_name=name, subscription_id=subscription_id
        )

    mine, theirs = week(SUBSCRIPTION_ID, "mine"), week(OTHER_SUBSCRIPTION_ID, "theirs")
    lookup = WeekLookup([mine, theirs])
    assert lookup.find("2026-W25", SUBSCRIPTION_ID) is mine
    assert lookup.find("2026-W25", OTHER_SUBSCRIPTION_ID) is theirs
    # Two records that both name a subscription must agree; another week never matches.
    assert lookup.find("2026-W25", "someone-else") is None
    assert lookup.find("2026-W26", SUBSCRIPTION_ID) is None

    # A record without a subscription id matches on the week alone, in either direction.
    unattributed = week(None, "unattributed")
    assert WeekLookup([unattributed]).find("2026-W25", SUBSCRIPTION_ID) is unattributed
    assert WeekLookup([mine]).find("2026-W25", None) is mine
    # But the week of the same subscription is preferred to an unattributed one.
    assert WeekLookup([mine, unattributed]).find("2026-W25", SUBSCRIPTION_ID) is mine

    assert same_box("2026-W25", None, "2026-W25", SUBSCRIPTION_ID)
    assert not same_box("2026-W25", SUBSCRIPTION_ID, "2026-W25", OTHER_SUBSCRIPTION_ID)
    assert not same_box("2026-W25", None, "2026-W26", None)


def test_parse_datetime_reads_a_naive_timestamp_as_utc_only_when_asked() -> None:
    assert parse_datetime("2026-08-17T22:53:06Z") == datetime(2026, 8, 17, 22, 53, 6, tzinfo=UTC)
    assert parse_datetime(" 2026-08-17T22:53:06+0000 ") == datetime(
        2026, 8, 17, 22, 53, 6, tzinfo=UTC
    )
    naive = parse_datetime("2026-08-17T22:53:06")
    assert naive is not None and naive.tzinfo is None
    aware = parse_datetime("2026-08-17T22:53:06", naive_as_utc=True)
    assert aware == datetime(2026, 8, 17, 22, 53, 6, tzinfo=UTC)
    assert as_utc(naive) == aware
    # An offset the timestamp does carry is kept, not replaced.
    pacific = parse_datetime("2026-07-20T12:00:00-0700", naive_as_utc=True)
    assert pacific is not None and pacific.utcoffset() == timedelta(hours=-7)
    for junk in (None, "", "   ", "soon", 5):
        assert parse_datetime(junk, naive_as_utc=True) is None


def test_dict_path_stops_where_the_path_does() -> None:
    week = {"deliveryOption": {"type": "standard"}, "product": "US-CBU-3-2-0", "tracking": None}
    assert dict_path(week, "deliveryOption", "type") == "standard"
    assert dict_path(week, "deliveryOption", "handle") is None
    assert dict_path(week, "product", "specs", "meals") is None
    assert dict_path(week, "tracking", "tracking_link") is None
    assert dict_path(None, "anything") is None


def test_http_url_or_none_keeps_only_web_links() -> None:
    assert http_url_or_none(" https://www.example.com/a?b=1#c ") == (
        "https://www.example.com/a?b=1#c"
    )
    assert http_url_or_none("HTTP://www.example.com") == "HTTP://www.example.com"
    for junk in (
        None,
        5,
        {"url": "https://www.example.com"},
        "",
        "/image/a.jpg",
        "www.example.com/a",
        "javascript:alert(1)",
        "data:text/html,x",
        "file:///etc/hosts",
        "https://",
        "http://[",
    ):
        assert http_url_or_none(junk) is None
