"""Regression tests for the HelloFresh client's review fixes.

HelloFresh is faked behind the client's transport: the aiohttp-like session it is given (the
curl transport is off for the suite, see conftest.py). Each test therefore runs the client's
real request path — the token and bot checks, the status handling, the JSON decoding — and no
request leaves the test. All account data is made up.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import date, timedelta
import json
import logging
import time
from typing import Any
from zoneinfo import ZoneInfo

from freezegun import freeze_time
from homeassistant.util import dt as dt_util
import pytest

from custom_components.hellofresh import client as client_module
from custom_components.hellofresh import (
    client_billing,
    client_catalog,
    client_core,
    client_history,
    client_menus,
    client_pricing,
)
from custom_components.hellofresh.client import HelloFreshClient
from custom_components.hellofresh.models import (
    HelloFreshAccountData,
    HelloFreshAuthError,
    HelloFreshError,
    HelloFreshNoMenuError,
    HelloFreshNoSubscriptionError,
    HelloFreshNotImplementedError,
    HelloFreshOrder,
    HelloFreshRateLimitedError,
    HelloFreshRecipe,
    HelloFreshSubscription,
    HelloFreshWeek,
)
from custom_components.hellofresh.normalizers import HelloFreshPayloadNormalizer
from custom_components.hellofresh.parsers import local_today
from custom_components.hellofresh.tls_transport import AuthResponse

SUB = "1234567"
OTHER_SUB = "8642086"
CUSTOMER = 2000002
PLAN = "00000000-0000-4000-8000-000000000001"
CUSTOMER_UUID = "00000000-0000-4000-8000-000000000002"
APP, CAKE = 70185, 70200  # Market item indexes: salmon bites, a cake


# ---- a fake HelloFresh behind the transport -------------------------------------------------


class FakeResponse:
    """An aiohttp-like response: a JSON body, or text."""

    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self._body = body
        is_text = isinstance(body, str)
        self.headers = {"Content-Type": "text/html" if is_text else "application/json"}

    async def json(self, content_type=None):
        # Every read gets its own copy, as every real response is parsed afresh.
        return json.loads(self._body) if isinstance(self._body, str) else copy.deepcopy(self._body)

    async def text(self) -> str:
        return self._body if isinstance(self._body, str) else json.dumps(self._body)


class FakeHelloFresh:
    """HelloFresh behind the client's session: answers by method and path, records requests.

    A route's answer is ``(status, body)`` or a function of ``(params, json)`` returning one;
    anything unrouted answers 404, as a route HelloFresh doesn't have does. Every request
    yields to the event loop once, as a network round-trip would.
    """

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Any] = {}
        self.requests: list[dict[str, Any]] = []

    def route(self, method: str, path: str, answer: Any) -> None:
        self.routes[(method, path)] = answer

    async def request(self, method, url, params=None, json=None, headers=None):
        path = "/" + url.split("://", 1)[1].split("/", 1)[1]
        self.requests.append(
            {"method": method, "path": path, "params": dict(params or {}), "json": json}
        )
        await asyncio.sleep(0)
        answer = self.routes.get((method, path))
        if answer is None:
            status, body = 404, {"status": "NOT_FOUND"}
        elif callable(answer):
            status, body = answer(dict(params or {}), json)
        else:
            status, body = answer
        return FakeResponse(status, body)

    def paths(self, start: int = 0, *, method: str | None = None) -> list[str]:
        return [
            request["path"]
            for request in self.requests[start:]
            if method is None or request["method"] == method
        ]

    def writes(self, start: int = 0) -> list[dict[str, Any]]:
        return [request for request in self.requests[start:] if request["method"] != "GET"]


def make_client(server: FakeHelloFresh, **options: Any) -> HelloFreshClient:
    return HelloFreshClient(
        session=server,  # type: ignore[arg-type]
        access_token="token",
        token_issued_at=int(time.time()),
        token_expires_in=3600,
        **options,
    )


def iso_week(day: date) -> str:
    iso = day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


class Account:
    """One subscription's state on the fake HelloFresh, read and changed by the client.

    One week, ten days out, with a menu of four meals and a Market catalog of two items. The
    week's selection, add-ons, box and status are what the menu and weeks reads answer and
    what the cart and status writes change.
    """

    def __init__(self, server: FakeHelloFresh) -> None:
        self.server = server
        self.delivery = local_today() + timedelta(days=10)
        self.week_id = iso_week(self.delivery)
        self.selection: dict[int, int] = {11: 1, 18: 1, 20: 1}
        self.extras: dict[int, tuple[int, int]] = {APP: (1, 0)}  # oneOff, preselected
        self.box = "US-CBU-3-2-0"
        self.status = "RUNNING"
        self.delivery_time = "US-1-0800-2000"
        self.puts: list[dict[str, Any]] = []
        server.route("GET", "/gw/api/customers/me/subscriptions", self._subscriptions)
        server.route("GET", "/gw/api/customers/me/deliveries", self._weeks)
        server.route("GET", "/gw/my-deliveries/menu", self._menu)
        server.route("PUT", f"/gw/v1/carts/{self.week_id}", self._cart)
        server.route(
            "PATCH", f"/gw/api/subscriptions/{SUB}/delivery_dates/{self.week_id}", self._status
        )

    def subscription(self) -> dict[str, Any]:
        return {
            "id": int(SUB),
            "customerPlanId": PLAN,
            "customer": {"id": CUSTOMER, "uuid": CUSTOMER_UUID, "locale": "en-US"},
            "product": {"sku": "US-CBU-3-2-0"},
            "productType": {"productName": "Classic", "specs": {"meals": 3, "size": 2}},
            "preset": "quick",
            "deliveryTime": self.delivery_time,
            "shippingAddress": {"postcode": "02101"},
            "isActive": True,
        }

    def week(self) -> dict[str, Any]:
        meals = int(self.box.split("-")[2])
        return {
            "id": self.week_id,
            "subscriptionId": int(SUB),
            "deliveryDate": f"{self.delivery.isoformat()}T12:00:00-0700",
            "cutoffDate": f"{(self.delivery - timedelta(days=5)).isoformat()}T23:59:59-0700",
            "status": self.status,
            "state": self.status,
            "allowedActions": {"mealSwap": True, "pause": True, "oneOffChange": True},
            "product": {"handle": self.box, "specs": {"meals": meals, "size": 2}, "price": 6594},
            "deliveryOption": {"handle": "US-1-0800-2000", "deliveryName": "Mondays"},
        }

    def _subscriptions(self, _params, _body):
        return 200, {"items": [self.subscription()]}

    def _weeks(self, _params, _body):
        return 200, {"items": [self.week()]}

    def _addon(self, index: int, recipe_id: str, name: str, sku: str, maximum: int):
        one_off, preselected = self.extras.get(index, (0, 0))
        selection = None
        if one_off or preselected:
            selection = {
                "skipped": one_off == 0,
                "oneOffQuantity": one_off,
                "preselectedQuantity": preselected,
                "courses": [],
            }
        return {
            "index": index,
            "sku": sku,
            "maxQuantity": maximum,
            "recipe": {"id": recipe_id, "name": name},
            "selection": selection,
        }

    def _menu(self, params, _body):
        if params.get("week") != self.week_id:
            return 404, {"status": "NOT_FOUND"}
        return 200, {
            "week": self.week_id,
            "meals": [
                {
                    "index": index,
                    "recipe": {"id": f"recipe-{index}", "name": f"Meal {index}"},
                    "selection": {"quantity": self.selection.get(index, 0), "limit": 0},
                }
                for index in (11, 18, 20, 32)
            ],
            "addOns": {
                "groups": [
                    {
                        "groupType": "appetizer",
                        "sku": "US-AAB-0-0-0",
                        "addOns": [self._addon(APP, "m-app", "Salmon Bites", "US-AAB-0-0-0", 6)],
                    },
                    {
                        "groupType": "dessert",
                        "sku": "US-DES-0-0-0",
                        "addOns": [self._addon(CAKE, "m-des", "Bundt Cake", "US-DES-0-0-0", 4)],
                    },
                ]
            },
        }

    def _cart(self, params, body):
        self.puts.append({"params": params, **body})
        self.selection = {meal["index"]: meal["quantity"] for meal in body["meals"]}
        self.extras = {
            entry["index"]: (entry["oneOffQuantity"], entry["preselectedQuantity"])
            for group in body["extras"]
            for entry in group["selection"]
        }
        self.box = params["product-sku"]
        return 200, {"hasSeamlessDowngraded": False}

    def _status(self, _params, body):
        self.status = body["delivery"]["status"]
        # The stub week HelloFresh answers a status change with (see the API reference).
        return 201, {"count": 1, "items": [{"id": self.week_id, "allowedActions": {}}]}


async def polled() -> tuple[FakeHelloFresh, Account, HelloFreshClient, HelloFreshAccountData]:
    """A fake HelloFresh with one account, and a client that has polled it once."""
    server = FakeHelloFresh()
    account = Account(server)
    client = make_client(server)
    data = await client.async_get_account_data()
    return server, account, client, data


def selected(week: HelloFreshWeek) -> dict[str, int | None]:
    return {
        recipe.recipe_id: recipe.selected_quantity for recipe in week.recipes if recipe.is_selected
    }


# ---- 1. cart writes are built from the week as HelloFresh has it now ------------------------


async def test_a_market_write_right_after_a_meal_write_keeps_the_new_meals() -> None:
    server, account, client, _data = await polled()

    await client.async_select_meals(account.week_id, ["recipe-11", "recipe-32", "recipe-20"])
    await client.async_select_market_items(account.week_id, {"m-des": 1})

    second = account.puts[1]
    assert {meal["index"] for meal in second["meals"]} == {11, 32, 20}
    assert account.selection == {11: 1, 32: 1, 20: 1}


async def test_a_meal_write_does_not_put_back_an_add_on_removed_in_the_app() -> None:
    server, account, client, _data = await polled()
    account.extras = {}  # removed in the HelloFresh app after the poll

    await client.async_select_meals(account.week_id, ["recipe-11", "recipe-18", "recipe-20"])

    assert account.puts[0]["extras"] == []
    assert account.extras == {}


async def test_a_market_write_does_not_revert_meals_changed_in_the_app() -> None:
    server, account, client, _data = await polled()
    account.selection = {18: 1, 20: 1, 32: 1}  # changed in the HelloFresh app after the poll

    await client.async_select_market_items(account.week_id, {"m-app": 2})

    assert {meal["index"] for meal in account.puts[0]["meals"]} == {18, 20, 32}


async def test_two_writes_to_one_week_at_once_run_in_turn() -> None:
    server, account, client, _data = await polled()

    await asyncio.gather(
        client.async_select_meals(account.week_id, ["recipe-11", "recipe-32", "recipe-20"]),
        client.async_select_market_items(account.week_id, {"m-des": 2}),
    )

    # The second write read the week after the first had saved it, so both halves stand.
    assert account.selection == {11: 1, 32: 1, 20: 1}
    assert account.extras == {CAKE: (2, 0)}


async def test_a_write_publishes_a_fresh_snapshot_and_leaves_the_old_one_alone() -> None:
    server, account, client, data = await polled()
    assert client.last_account_data is data

    await client.async_select_meals(account.week_id, ["recipe-11", "recipe-32"], {"recipe-32": 3})

    fresh = client.last_account_data
    assert fresh is not data
    week = fresh.get_week(account.week_id)
    assert selected(week) == {"recipe-11": 1, "recipe-32": 3}
    # Four portions: the box grew to four meals, and the re-read week says so.
    assert account.box == "US-CBU-4-2-0"
    assert week.meals_required == 4
    assert week.meals_selected == 4
    # The previous snapshot (maybe still on screen) is untouched.
    assert selected(data.get_week(account.week_id)) == {
        "recipe-11": 1,
        "recipe-18": 1,
        "recipe-20": 1,
    }


async def test_a_failed_re_read_keeps_the_snapshot_and_the_write_stands() -> None:
    server, account, client, data = await polled()
    server.route("GET", "/gw/api/customers/me/deliveries", (503, {"error": "busy"}))

    downgraded = await client.async_select_meals(
        account.week_id, ["recipe-11", "recipe-32", "recipe-20"]
    )

    assert downgraded is False
    assert len(account.puts) == 1
    assert client.last_account_data is data


# ---- 2. only verified writes, and a clear error when one can't be built ----------------------


async def test_an_unknown_recipe_is_refused_by_name_and_nothing_is_written() -> None:
    server, account, client, _data = await polled()
    start = len(server.requests)

    with pytest.raises(HelloFreshError, match="recipe-TYPO") as raised:
        await client.async_select_meals(account.week_id, ["recipe-11", "recipe-18", "recipe-TYPO"])

    assert not isinstance(raised.value, HelloFreshNotImplementedError)
    # Reads only: the week's box as HelloFresh has it now, then its menu. Nothing is written.
    assert server.paths(start) == ["/gw/api/customers/me/deliveries", "/gw/my-deliveries/menu"]


async def test_a_week_without_a_menu_yet_writes_nothing_and_is_not_a_broken_account() -> None:
    """A week whose menu isn't out yet is an ordinary state, not a missing capability.

    Raised as HelloFreshNotImplementedError it put up the "write actions unavailable" repair
    for someone who only tried to choose meals too early.
    """
    server, account, client, _data = await polled()
    server.route("GET", "/gw/my-deliveries/menu", (404, {"status": "NOT_FOUND"}))
    start = len(server.requests)

    with pytest.raises(HelloFreshNoMenuError, match="no menu") as raised:
        await client.async_select_meals(account.week_id, ["recipe-11", "recipe-18", "recipe-20"])

    assert not isinstance(raised.value, HelloFreshNotImplementedError)
    assert server.writes(start) == []


async def test_a_failed_cart_write_is_raised_without_trying_other_endpoints() -> None:
    server, account, client, _data = await polled()
    server.route("PUT", f"/gw/v1/carts/{account.week_id}", (500, {"error": "boom"}))
    start = len(server.requests)

    with pytest.raises(HelloFreshError) as raised:
        await client.async_select_meals(account.week_id, ["recipe-11", "recipe-18", "recipe-20"])

    assert raised.value.status == 500
    assert [write["method"] for write in server.writes(start)] == ["PUT"]


async def test_skip_sends_the_verified_patch_and_publishes_the_skipped_week() -> None:
    server, account, client, data = await polled()
    start = len(server.requests)

    await client.async_skip_week(account.week_id)

    assert [(write["method"], write["path"]) for write in server.writes(start)] == [
        ("PATCH", f"/gw/api/subscriptions/{SUB}/delivery_dates/{account.week_id}")
    ]
    assert account.status == "PAUSED"
    week = client.last_account_data.get_week(account.week_id)
    assert week.is_skipped is True
    # A paused week's picks are phantom: cleared, as the poll clears them.
    assert selected(week) == {}
    assert data.get_week(account.week_id).is_skipped is False


async def test_a_rejected_skip_is_raised_without_trying_other_endpoints() -> None:
    server, account, client, _data = await polled()
    path = f"/gw/api/subscriptions/{SUB}/delivery_dates/{account.week_id}"
    server.route("PATCH", path, (500, {"error": "boom"}))
    start = len(server.requests)

    with pytest.raises(HelloFreshError) as raised:
        await client.async_skip_week(account.week_id)

    assert raised.value.status == 500
    assert [write["path"] for write in server.writes(start)] == [path]


async def test_a_rejected_weekday_change_is_not_retried_elsewhere() -> None:
    server, account, client, _data = await polled()
    server.route("PATCH", f"/gw/api/subscriptions/{SUB}", (502, {"error": "gateway"}))
    start = len(server.requests)

    with pytest.raises(HelloFreshError) as raised:
        await client.async_change_delivery_weekday("US-2-0800-2000")

    assert raised.value.status == 502
    assert [write["path"] for write in server.writes(start)] == [f"/gw/api/subscriptions/{SUB}"]


# ---- 16. an omitted quantity keeps the portions in the box ----------------------------------


async def test_a_meal_write_without_quantities_keeps_a_doubled_portion() -> None:
    server = FakeHelloFresh()
    account = Account(server)
    account.selection = {11: 2, 18: 1, 20: 1}
    account.box = "US-CBU-4-2-0"
    client = make_client(server)
    await client.async_get_account_data()

    await client.async_select_meals(account.week_id, ["recipe-11", "recipe-18", "recipe-32"])

    put = account.puts[0]
    assert {meal["index"]: meal["quantity"] for meal in put["meals"]} == {11: 2, 18: 1, 32: 1}
    assert put["params"]["product-sku"] == "US-CBU-4-2-0"


# ---- A. the snapshot after every write ------------------------------------------------------


async def test_adding_a_favorite_sets_the_heart_without_a_request() -> None:
    server, account, client, data = await polled()
    server.route(
        "POST",
        "/gw/cookbook/v1/internal-recipes",
        (201, {"id": "row-1", "bookmark_id": "recipe-32-en-US"}),
    )
    start = len(server.requests)

    await client.async_add_favorite("recipe-32")

    assert server.paths(start) == ["/gw/cookbook/v1/internal-recipes"]
    fresh = client.last_account_data
    assert fresh is not data
    hearts = {r.recipe_id: r.is_favorite for r in fresh.get_week(account.week_id).recipes}
    assert hearts["recipe-32"] is True
    old = {r.recipe_id: r.is_favorite for r in data.get_week(account.week_id).recipes}
    assert old["recipe-32"] is not True


async def test_removing_a_favorite_clears_the_heart() -> None:
    server, account, client, _data = await polled()
    server.route(
        "POST",
        "/gw/cookbook/v1/internal-recipes/search",
        (200, {"recipes": [{"id": "row-9", "bookmark_id": "recipe-11-en-US"}]}),
    )
    server.route("DELETE", "/gw/cookbook/v1/external-recipes/row-9", (204, {}))

    assert await client.async_remove_favorite("recipe-11") is True

    hearts = {
        r.recipe_id: r.is_favorite
        for r in client.last_account_data.get_week(account.week_id).recipes
    }
    assert hearts["recipe-11"] is False


async def test_a_weekday_change_shows_the_new_day_even_when_the_read_lags() -> None:
    server, account, client, data = await polled()
    server.route("PATCH", f"/gw/api/subscriptions/{SUB}", (200, account.subscription()))

    # HelloFresh's own read still has the old day for a second after the PATCH.
    await client.async_change_delivery_weekday("US-2-0800-2000")

    fresh = client.last_account_data
    assert fresh is not data
    assert fresh.subscriptions[0].raw["deliveryTime"] == "US-2-0800-2000"
    assert data.subscriptions[0].raw["deliveryTime"] == "US-1-0800-2000"


async def test_a_plan_change_re_reads_the_subscription_and_its_weeks() -> None:
    server, account, client, data = await polled()

    def change_plan(_params, body):
        account.box = body["productHandle"]
        return 204, {}

    server.route("PATCH", f"/gw/api/plans/{PLAN}", change_plan)

    await client.async_change_plan("US-CBU-4-2-0")

    week = client.last_account_data.get_week(account.week_id)
    assert week.meals_required == 4
    assert week.raw["product"]["handle"] == "US-CBU-4-2-0"
    # The menu isn't read again for a plan change; the week keeps the one it had.
    assert selected(week) == selected(data.get_week(account.week_id))
    assert data.get_week(account.week_id).meals_required == 3


# ---- 3. cached prices expire, only real totals are kept, writes empty the cache --------------


class Clock:
    """A monotonic clock a test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _plan_subscription() -> HelloFreshSubscription:
    return HelloFreshSubscription(
        subscription_id=SUB,
        account_id=str(CUSTOMER),
        locale="en-US",
        raw={
            "customerPlanId": PLAN,
            "product": {"sku": "US-CBU-3-2-0"},
            "deliveryOption": {"handle": "US-1-0800-2000"},
            "shippingAddress": {"postcode": "02101"},
        },
    )


async def test_a_cached_price_expires_after_six_hours(monkeypatch) -> None:
    clock = Clock()
    monkeypatch.setattr(client_pricing, "monotonic", clock)
    server = FakeHelloFresh()
    server.route("POST", "/gw/calculate", (200, {"grandTotal": 76.93, "currency": "USD"}))
    client = make_client(server)

    for _ in range(2):
        assert (await client._async_get_calculate_price(_plan_subscription()))[
            "grandTotal"
        ] == 76.93
    assert server.paths() == ["/gw/calculate"]

    clock.now += 6 * 3600
    await client._async_get_calculate_price(_plan_subscription())
    assert server.paths() == ["/gw/calculate", "/gw/calculate"]


async def test_a_price_answer_without_a_total_is_not_cached() -> None:
    server = FakeHelloFresh()
    server.route("POST", "/gw/calculate", (200, {"unrelated": True}))
    client = make_client(server)

    await client._async_get_calculate_price(_plan_subscription())
    await client._async_get_calculate_price(_plan_subscription())

    assert server.paths() == ["/gw/calculate", "/gw/calculate"]


async def test_a_write_empties_the_price_cache() -> None:
    server, account, client, _data = await polled()
    client._store_cart_price("earlier", {"grandTotal": 1.0})

    await client.async_skip_week(account.week_id)

    assert "earlier" not in client._cart_price_cache


# ---- 4. two subscriptions keep their own history and tracking --------------------------------


def _history_week(week_id: str, meal: str) -> dict[str, Any]:
    return {"week": week_id, "meals": [{"id": meal, "name": meal}], "addons": []}


async def test_two_subscriptions_keep_their_own_delivered_weeks() -> None:
    """A delivered W40 and skipped W41; B delivered both (the review's scenario)."""
    today = local_today()
    w40, w41 = iso_week(today - timedelta(weeks=3)), iso_week(today - timedelta(weeks=2))
    pages = {
        SUB: {"weeks": [_history_week(w40, "a-meal-40")], "nextWeek": None},
        OTHER_SUB: {
            "weeks": [_history_week(w41, "b-meal-41"), _history_week(w40, "b-meal-40")],
            "nextWeek": None,
        },
    }
    server = FakeHelloFresh()
    server.route(
        "GET",
        "/gw/my-deliveries/past-deliveries",
        lambda params, _body: (200, pages[params["subscription"]]),
    )
    client = make_client(server)
    subscriptions = [
        HelloFreshSubscription(subscription_id=SUB),
        HelloFreshSubscription(subscription_id=OTHER_SUB),
    ]

    weeks = await client._async_get_past_delivery_weeks(subscriptions)

    by_key = {(w.subscription_id, w.week_id): [r.recipe_id for r in w.recipes] for w in weeks}
    assert by_key == {
        (SUB, w40): ["a-meal-40"],
        (OTHER_SUB, w41): ["b-meal-41"],
        (OTHER_SUB, w40): ["b-meal-40"],
    }
    # A's skipped W41 gets no meals from B's history.
    skipped = HelloFreshWeek(
        week_id=w41, display_name=w41, subscription_id=SUB, delivery_date=today - timedelta(weeks=2)
    )
    client._merge_past_delivery_recipes_into_account_weeks([skipped], weeks)
    assert skipped.recipes == []


async def test_the_delivery_watch_lays_each_subscriptions_tracking_on_its_own_order() -> None:
    """Orders without an id of their own are named after their week, which both share."""
    client = make_client(FakeHelloFresh())
    week_id = iso_week(local_today())

    def order(subscription_id: str, url: str | None = None) -> HelloFreshOrder:
        return HelloFreshOrder(
            order_id=week_id,
            week_id=week_id,
            status="RUNNING",
            subscription_id=subscription_id,
            delivery_date=local_today(),
            tracking_url=url,
        )

    data = HelloFreshAccountData(
        weeks=[
            HelloFreshWeek(week_id=week_id, display_name="A", subscription_id=SUB),
            HelloFreshWeek(week_id=week_id, display_name="B", subscription_id=OTHER_SUB),
        ],
        orders=[order(SUB), order(OTHER_SUB)],
        subscriptions=[
            HelloFreshSubscription(subscription_id=SUB),
            HelloFreshSubscription(subscription_id=OTHER_SUB),
        ],
    ).finalize()

    async def fresh(subscription):
        url = f"https://track.example/{subscription.subscription_id}"
        return [], [order(subscription.subscription_id, url)]

    async def no_tracking(_subscriptions, _orders):
        return None

    client._async_get_upcoming_deliveries = fresh  # type: ignore[method-assign]
    client._async_enrich_order_tracking = no_tracking  # type: ignore[method-assign]

    await client.async_refresh_delivery_status(data)

    urls = {o.subscription_id: o.tracking_url for o in data.orders}
    assert urls == {
        SUB: f"https://track.example/{SUB}",
        OTHER_SUB: f"https://track.example/{OTHER_SUB}",
    }


# ---- 5. a refused request is told apart by its status ---------------------------------------


async def test_recipe_detail_names_an_unknown_recipe_and_keeps_other_failures() -> None:
    server = FakeHelloFresh()
    client = make_client(server)
    with pytest.raises(HelloFreshError, match="no recipe with id 0000000000000000000000aa"):
        await client.async_get_recipe_detail("0000000000000000000000aa")

    server.route("GET", "/gw/recipes/recipes/0000000000000000000000aa", (500, {"error": "boom"}))
    with pytest.raises(HelloFreshError) as raised:
        await client.async_get_recipe_detail("0000000000000000000000aa")
    assert raised.value.status == 500
    assert "no recipe" not in str(raised.value)


# ---- 6. removing a favorite needs the cookbook to answer ------------------------------------


async def test_removing_a_favorite_raises_when_the_cookbook_did_not_answer() -> None:
    server = FakeHelloFresh()
    server.route("POST", "/gw/cookbook/v1/internal-recipes/search", (503, {"error": "busy"}))
    client = make_client(server)

    with pytest.raises(HelloFreshError, match="not removed"):
        await client.async_remove_favorite("0000000000000000000000aa")

    assert server.paths(method="DELETE") == []


async def test_removing_a_recipe_that_is_not_a_favorite_is_a_no_op() -> None:
    server = FakeHelloFresh()
    server.route("POST", "/gw/cookbook/v1/internal-recipes/search", (200, {"recipes": []}))
    client = make_client(server)

    assert await client.async_remove_favorite("0000000000000000000000aa") is False
    assert server.paths(method="DELETE") == []


# ---- 7. the billing scan's "today" is Home Assistant's ---------------------------------------


@pytest.fixture
def pacific_time():
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(ZoneInfo("America/Los_Angeles"))
    yield
    dt_util.set_default_time_zone(previous)


def test_the_billing_scan_still_counts_mondays_box_on_monday_evening(pacific_time) -> None:
    """Monday 17:00 in California is Tuesday 01:00 UTC; the box delivered Monday still counts."""
    client = make_client(FakeHelloFresh())
    items = [
        {
            "orderNr": "30000000001",
            "createdAt": "2026-11-04T00:13:06-0800",
            "grandTotal": 76.93,
            "orderLines": [
                {"deliveryDate": "2026-11-09T00:00:00-0800", "subscription": {"id": SUB}}
            ],
        }
    ]
    with freeze_time("2026-11-10 01:00:00"):
        latest, future, order_nrs, prices, discounts = client._accumulate_order_prices(items)
        data = HelloFreshAccountData()
        client._compute_next_delivery_total(data, future, order_nrs, prices, discounts)

    assert future[SUB][0] == date(2026, 11, 9)
    assert data.next_delivery_total == 76.93


# ---- 8 and 13. one JSON read: auth ends the poll, a wrong body type doesn't crash it ---------


async def test_an_auth_failure_in_an_enrichment_ends_the_poll() -> None:
    server = FakeHelloFresh()
    Account(server)
    server.route("GET", f"/gw/payments/customers/{CUSTOMER_UUID}/balance", (401, {"error": "x"}))
    client = make_client(server)

    with pytest.raises(HelloFreshAuthError):
        await client.async_get_account_data()


async def test_an_auth_failure_on_a_week_menu_ends_the_poll_without_more_probes() -> None:
    server = FakeHelloFresh()
    Account(server)
    server.route("GET", "/gw/my-deliveries/menu", (401, {"error": "expired"}))
    client = make_client(server)

    with pytest.raises(HelloFreshAuthError):
        await client.async_get_account_data()

    probes = [
        path for path in server.paths() if path.startswith(("/gw/my-menu", "/gw/menus-service"))
    ]
    assert probes == []


async def test_a_list_where_an_object_belongs_does_not_crash_the_poll() -> None:
    server = FakeHelloFresh()
    account = Account(server)
    server.route("GET", "/gw/api/customers/me/info", (200, [1, 2, 3]))
    client = make_client(server)

    data = await client.async_get_account_data()

    assert data.boxes_received is None
    assert data.get_week(account.week_id) is not None


# ---- 9. the sold-out overlay is small, cached, and decoded off the loop ----------------------


def _editable(week_id: str, deadline_days: int) -> HelloFreshWeek:
    return HelloFreshWeek(
        week_id=week_id,
        display_name=week_id,
        subscription_id=SUB,
        delivery_date=local_today() + timedelta(days=deadline_days + 4),
        selection_deadline=dt_util.utcnow() + timedelta(days=deadline_days),
        allowed_actions={"mealSwap": True},
        recipes=[HelloFreshRecipe(recipe_id=f"r-{week_id}", name="Meal", course_index=1)],
    )


async def test_the_sold_out_overlay_asks_for_the_two_nearest_deadlines_once_an_hour(
    monkeypatch,
) -> None:
    clock = Clock()
    monkeypatch.setattr(client_menus, "monotonic", clock)
    server = FakeHelloFresh()
    server.route("GET", "/gw/menus-service/menus", (200, {"items": []}))
    client = make_client(server)
    weeks = [_editable("2026-W50", 9), _editable("2026-W48", 2), _editable("2026-W49", 5)]
    subscriptions = [HelloFreshSubscription(subscription_id=SUB, locale="en-US")]

    await client._async_apply_menu_availability(subscriptions, weeks)
    assert [r["params"]["weeks"] for r in server.requests] == ["2026-W48,2026-W49"]

    await client._async_apply_menu_availability(subscriptions, weeks)
    assert len(server.requests) == 1

    clock.now += 3600
    await client._async_apply_menu_availability(subscriptions, weeks)
    assert len(server.requests) == 2


async def test_the_regional_menu_fallback_never_asks_for_delivered_weeks() -> None:
    server = FakeHelloFresh()
    client = make_client(server)
    subscriptions = [HelloFreshSubscription(subscription_id=SUB, locale="en-US")]
    past = HelloFreshWeek(
        week_id="2026-W30", display_name="past", delivery_date=local_today() - timedelta(days=30)
    )
    upcoming = HelloFreshWeek(
        week_id="2099-W01", display_name="next", delivery_date=local_today() + timedelta(days=3)
    )

    await client._async_get_menus_service_weeks(subscriptions, [past, upcoming])
    assert server.requests[-1]["params"]["weeks"] == "2099-W01"

    count = len(server.requests)
    assert await client._async_get_menus_service_weeks(subscriptions, [past]) is None
    assert len(server.requests) == count


async def test_a_large_body_is_decoded_in_a_worker_thread(monkeypatch) -> None:
    offloaded: list[Any] = []
    to_thread = asyncio.to_thread

    async def spy(func, *args):
        offloaded.append(func)
        return await to_thread(func, *args)

    monkeypatch.setattr(client_core.asyncio, "to_thread", spy)
    client = make_client(FakeHelloFresh())
    headers = {"content-type": "application/json"}

    small = await client._async_response_json(AuthResponse(200, headers, '{"items": []}'))
    assert small == {"items": []} and offloaded == []

    big_body = json.dumps({"items": ["x" * 300_000]})
    big = await client._async_response_json(AuthResponse(200, headers, big_body))
    assert len(big["items"][0]) == 300_000
    assert offloaded == [json.loads]


# ---- 10. history is walked once a day, and independent reads overlap -------------------------


def _paged_history(server: FakeHelloFresh, pages: dict[str | None, dict[str, Any]]) -> None:
    server.route(
        "GET",
        "/gw/my-deliveries/past-deliveries",
        lambda params, _body: (200, pages[params.get("from")]),
    )


def _weeks_ago(count: int) -> str:
    return iso_week(local_today() - timedelta(weeks=count))


async def test_history_pages_read_once_are_reused_until_the_next_day(monkeypatch) -> None:
    clock = Clock()
    monkeypatch.setattr(client_history, "monotonic", clock)
    w1, w2, w3, w4, w5 = (_weeks_ago(n) for n in range(1, 6))
    server = FakeHelloFresh()
    _paged_history(
        server,
        {
            None: {"weeks": [_history_week(w1, "m1"), _history_week(w2, "m2")], "nextWeek": w3},
            w3: {"weeks": [_history_week(w3, "m3"), _history_week(w4, "m4")], "nextWeek": w5},
            w5: {"weeks": [_history_week(w5, "m5")], "nextWeek": None},
        },
    )
    client = make_client(server)
    subscriptions = [HelloFreshSubscription(subscription_id=SUB)]

    first = await client._async_get_past_delivery_weeks(subscriptions)
    assert len(server.requests) == 3

    second = await client._async_get_past_delivery_weeks(subscriptions)
    assert len(server.requests) == 4  # the first page only
    assert {w.week_id for w in second} == {w.week_id for w in first} == {w1, w2, w3, w4, w5}

    clock.now += 24 * 3600
    await client._async_get_past_delivery_weeks(subscriptions)
    assert len(server.requests) == 7


async def test_a_new_delivery_shifts_page_one_and_the_rest_is_reused(monkeypatch) -> None:
    w0, w1, w2, w3, w4 = (_weeks_ago(n) for n in range(0, 5))
    server = FakeHelloFresh()
    pages = {
        None: {"weeks": [_history_week(w1, "m1"), _history_week(w2, "m2")], "nextWeek": w3},
        w3: {"weeks": [_history_week(w3, "m3"), _history_week(w4, "m4")], "nextWeek": None},
    }
    _paged_history(server, pages)
    client = make_client(server)
    subscriptions = [HelloFreshSubscription(subscription_id=SUB)]
    await client._async_get_past_delivery_weeks(subscriptions)
    assert len(server.requests) == 2

    # A box arrives: page one now starts a week later and points at a week read before.
    pages[None] = {"weeks": [_history_week(w0, "m0"), _history_week(w1, "m1")], "nextWeek": w2}
    weeks = await client._async_get_past_delivery_weeks(subscriptions)

    assert len(server.requests) == 3
    assert {w.week_id for w in weeks} == {w0, w1, w2, w3, w4}


async def test_tracking_is_not_looked_up_for_a_box_delivered_days_ago() -> None:
    server = FakeHelloFresh()
    client = make_client(server)
    recent, old = "00000000-0000-4000-8000-00000000000a", "00000000-0000-4000-8000-00000000000b"

    def order(public_id: str, days_ago: int) -> HelloFreshOrder:
        return HelloFreshOrder(
            order_id=public_id[-1],
            week_id="2026-W40",
            status="DELIVERED",
            delivery_date=local_today() - timedelta(days=days_ago),
            tracking_url=f"https://www.hellofresh.com/delivery-tracking/{public_id}",
        )

    await client._async_enrich_order_tracking([], [order(recent, 1), order(old, 5)])

    assert server.paths() == [f"/gw/scm/tracking-ids/track/public-id/{recent}"]


async def test_the_cookbook_is_listed_while_the_menus_load() -> None:
    server = FakeHelloFresh()
    Account(server)
    client = make_client(server)

    await client.async_get_account_data()

    paths = server.paths()
    assert paths.index("/gw/cookbook/v1/external-recipes") < paths.index("/gw/my-deliveries/menu")


# ---- 11. probing: only 404/405 moves on, dead routes rest, rate limits stop the poll ---------


async def test_a_fallback_route_that_404s_rests_for_a_day(monkeypatch) -> None:
    clock = Clock()
    monkeypatch.setattr(client_core, "monotonic", clock)
    server = FakeHelloFresh()  # every weeks route answers 404
    client = make_client(server)
    subscription = HelloFreshSubscription(subscription_id=SUB)

    assert await client._async_get_upcoming_deliveries(subscription) == ([], [])
    assert len(server.requests) == 6

    client._shared_get_tasks = {}  # a new poll
    await client._async_get_upcoming_deliveries(subscription)
    # Only the verified route is asked again; the five fallbacks rest.
    assert server.paths(6) == ["/gw/api/customers/me/deliveries"]

    clock.now += 24 * 3600
    client._shared_get_tasks = {}
    await client._async_get_upcoming_deliveries(subscription)
    assert len(server.requests) == 13


async def test_a_server_error_ends_the_probing_and_fails_the_poll() -> None:
    server = FakeHelloFresh()
    server.route("GET", "/gw/api/customers/me/deliveries", (503, {"error": "busy"}))
    client = make_client(server)

    # Not ([], []): an account without weeks would be published as a good poll.
    with pytest.raises(HelloFreshError, match="upcoming deliveries unavailable") as raised:
        await client._async_get_upcoming_deliveries(HelloFreshSubscription(subscription_id=SUB))
    assert raised.value.status == 503
    assert len(server.requests) == 1


async def test_a_rate_limit_ends_the_poll() -> None:
    server = FakeHelloFresh()
    Account(server)
    server.route("GET", "/gw/api/customers/me/deliveries", (429, {"error": "slow down"}))
    client = make_client(server)

    with pytest.raises(HelloFreshRateLimitedError) as raised:
        await client.async_get_account_data()
    assert raised.value.status == 429


# ---- 12. the ISO week's year -----------------------------------------------------------------


async def test_the_fallback_names_the_iso_week_new_years_day_belongs_to(monkeypatch) -> None:
    monkeypatch.setattr(client_module, "local_today", lambda: date(2027, 1, 1))
    server = FakeHelloFresh()
    client = make_client(server)

    await client._async_get_upcoming_deliveries(HelloFreshSubscription(subscription_id=SUB))

    froms = [
        request["params"]["from"] for request in server.requests if "from" in request["params"]
    ]
    assert froms == ["2026-W53"]


# ---- 14. the catalog and the spending view don't download twice ------------------------------


def _catalog_page(marker: str) -> str:
    blob = {
        "props": {"pageProps": {"ssrPayload": {"collection": {"recipes": [], "marker": marker}}}}
    }
    return (
        '<html><script>{"buildId":"build-1"}</script>'
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(blob)}</script></html>'
    )


async def test_after_a_fresh_build_id_404s_the_data_route_rests_for_an_hour(monkeypatch) -> None:
    clock = Clock()
    monkeypatch.setattr(client_catalog, "monotonic", clock)
    server = FakeHelloFresh()
    server.route("GET", "/recipes", (200, _catalog_page("top")))
    server.route("GET", "/recipes/chicken-recipes", (200, _catalog_page("chicken")))
    client = make_client(server)

    first = await client._async_get_catalog_json("recipes.json")
    assert first is not None
    assert server.paths() == ["/recipes", "/_next/data/build-1/recipes.json", "/recipes"]

    await client._async_get_catalog_json("recipes/chicken-recipes.json", {"main-collection": "x"})
    assert server.paths(3) == ["/recipes/chicken-recipes"]

    clock.now += 3600
    await client._async_get_catalog_json("recipes/chicken-recipes.json", {"main-collection": "x"})
    assert "/_next/data/build-1/recipes/chicken-recipes.json" in server.paths(4)


async def test_catalog_pages_are_reused_for_an_hour(monkeypatch) -> None:
    clock = Clock()
    monkeypatch.setattr(client_catalog, "monotonic", clock)
    server = FakeHelloFresh()
    server.route("GET", "/recipes", (200, '{"buildId":"build-1"}'))
    rows = [{"id": "0000000000000000000000aa", "name": "Pasta", "imagePath": "/image/p.jpg"}]
    server.route(
        "GET",
        "/_next/data/build-1/recipes.json",
        (200, {"pageProps": {"ssrPayload": {"collection": {"recipes": rows}}}}),
    )
    client = make_client(server)

    for _ in range(2):
        recipes = await client.async_get_catalog_recipes(include_favorites=False)
        assert [recipe.name for recipe in recipes] == ["Pasta"]
    assert server.paths().count("/_next/data/build-1/recipes.json") == 1

    clock.now += 3600
    await client.async_get_catalog_recipes(include_favorites=False)
    assert server.paths().count("/_next/data/build-1/recipes.json") == 2


async def test_spending_reuses_the_polls_ledger_and_says_when_it_is_cut_short(monkeypatch) -> None:
    clock = Clock()
    monkeypatch.setattr(client_billing, "monotonic", clock)
    server = FakeHelloFresh()
    Account(server)
    ledger = {
        "count": 250,  # more orders than the one request returned
        "items": [
            {
                "orderNr": "30000000001",
                "createdAt": "2026-09-01T00:13:06-0700",
                "grandTotal": 76.93,
                "orderLines": [
                    {"deliveryDate": "2026-09-07T00:00:00-0700", "subscription": {"id": SUB}}
                ],
            }
        ],
    }
    server.route("GET", "/gw/api/customers/me/orders", (200, ledger))
    client = make_client(server)
    await client.async_get_account_data()
    assert server.paths().count("/gw/api/customers/me/orders") == 1

    spending = await client.async_get_spending()

    assert server.paths().count("/gw/api/customers/me/orders") == 1
    assert spending["truncated"] is True
    assert spending["total"]["amount"] == 76.93

    clock.now += 3600
    await client.async_get_spending()
    assert server.paths().count("/gw/api/customers/me/orders") == 2


# ---- 15. no account ids in logs or errors; nothing unchecked in a path -----------------------


async def test_no_subscription_id_reaches_a_log_line_or_an_error(caplog) -> None:
    server = FakeHelloFresh()
    server.route("GET", "/gw/api/customers/me/deliveries", (503, {"error": "busy"}))
    client = make_client(server)
    client._cached_subscriptions = [HelloFreshSubscription(subscription_id=SUB)]

    with (
        caplog.at_level(logging.DEBUG, logger="custom_components.hellofresh"),
        pytest.raises(HelloFreshError) as raised_deliveries,
    ):
        await client._async_get_upcoming_deliveries(HelloFreshSubscription(subscription_id=SUB))
    assert "unavailable" in str(raised_deliveries.value)
    assert SUB not in str(raised_deliveries.value)
    assert SUB not in caplog.text

    with pytest.raises(HelloFreshError) as raised:
        await client._async_resolve_subscription(OTHER_SUB)
    assert OTHER_SUB not in str(raised.value)

    week = HelloFreshWeek(week_id="2026-W50", display_name="W50", subscription_id=OTHER_SUB)
    with pytest.raises(HelloFreshNotImplementedError) as raised_week:
        await client._async_get_subscription_for_week(week)
    assert OTHER_SUB not in str(raised_week.value)


@pytest.mark.parametrize("recipe_id", ["../../gw/api/plans", "abc def", "x?y=1", "a/b", ""])
async def test_a_recipe_id_that_is_not_one_is_refused_before_any_request(recipe_id) -> None:
    server = FakeHelloFresh()
    client = make_client(server)

    with pytest.raises(HelloFreshError):
        await client.async_get_recipe_detail(recipe_id)
    assert server.requests == []


@pytest.mark.parametrize("collection", ["../secret", "Chicken", "chicken recipes", "a//b", "/x"])
async def test_a_collection_that_is_not_a_path_of_slugs_is_refused(collection) -> None:
    server = FakeHelloFresh()
    client = make_client(server)

    with pytest.raises(HelloFreshError, match="Invalid recipe collection"):
        await client.async_get_catalog_recipes(collection, include_favorites=False)
    with pytest.raises(HelloFreshError, match="Invalid recipe collection"):
        await client.async_get_catalog_page(collection, include_favorites=False)
    assert server.requests == []


async def test_the_cart_path_encodes_its_week() -> None:
    client = make_client(FakeHelloFresh())

    async def preference(_subscription):
        return "quick"

    client._async_get_subscription_plan_preference = preference  # type: ignore[method-assign]
    week = HelloFreshWeek(
        week_id="2026-W50/../x",
        display_name="odd",
        subscription_id=SUB,
        selection_deadline=dt_util.utcnow() + timedelta(days=3),
        raw={"product": {"handle": "US-CBU-3-2-0"}},
    )
    subscription = HelloFreshSubscription(subscription_id=SUB, account_id=str(CUSTOMER))

    request = await client._async_build_cart_update(week, subscription, [], [])

    assert request["path"] == "/gw/v1/carts/2026-W50%2F..%2Fx"


# ---- B. only an account without a subscription goes on to the menu-only result ---------------


async def test_a_failed_subscriptions_read_fails_the_poll() -> None:
    server = FakeHelloFresh()
    server.route("GET", "/gw/api/customers/me/subscriptions", (503, {"error": "busy"}))
    client = make_client(server)

    with pytest.raises(HelloFreshError) as raised:
        await client.async_get_account_data()
    assert raised.value.status == 503
    assert not isinstance(raised.value, HelloFreshNoSubscriptionError)


async def test_an_account_without_a_subscription_still_shows_the_menu() -> None:
    server = FakeHelloFresh()
    server.route("GET", "/gw/api/customers/me/subscriptions", (200, {"items": []}))
    client = make_client(server)

    async def public_menu():
        return {
            "weeks": [HelloFreshWeek(week_id="menu", display_name="Menu", source="public_menu")],
            "available_labels": ["Menu"],
        }

    client._async_get_public_menu_data = public_menu  # type: ignore[method-assign]

    data = await client.async_get_account_data()

    assert data.account_data_available is False
    assert [week.week_id for week in data.public_menu_weeks] == ["menu"]


# ---- C. a merged week keeps only the menu blocks read later -----------------------------------


def _full_menu(week_id: str) -> dict[str, Any]:
    return {
        "id": "menu-id",
        "week": week_id,
        "meals": [
            {
                "index": 1,
                "recipe": {"id": "r-1", "name": "Chicken", "nutrition": {"calories": 600}},
                "selection": {"quantity": 2, "limit": 0},
                "charge": {"label": "+$5", "unitAmount": 500},
            },
            {
                "index": 2,
                "recipe": {"id": "r-2", "name": "Beef", "nutrition": {"calories": 700}},
                "selection": {"quantity": 0, "limit": 0},
            },
            {
                "index": 3,
                "recipe": {"id": "r-3", "name": "Chicken"},
                "selection": {"quantity": 0, "limit": 0},
            },
        ],
        "modularity": [
            {"defaultCourseIndex": 1, "variations": [{"index": 3, "title": "2x Chicken"}]}
        ],
        "addOns": {
            "groups": [
                {
                    "groupType": "dessert",
                    "sku": "US-DES-0-0-0",
                    "addOns": [{"index": 70200, "recipe": {"id": "m-des", "name": "Cake"}}],
                }
            ]
        },
        "categories": {
            "categories": [{"name": "Dinners", "slug": "dinners", "items": [{"id": "r-1"}]}]
        },
        "filters": [
            {
                "name": "Protein",
                "type": "main-protein",
                "options": [{"name": "Beef", "type": "beef"}],
            }
        ],
        "sorting": [{"name": "Popular"}],
        "menuCollections": [{"handle": "x", "description": "editorial text"}],
    }


def test_a_merged_week_keeps_only_the_menu_blocks_read_later() -> None:
    normalizer = HelloFreshPayloadNormalizer()
    subscription = HelloFreshSubscription(subscription_id=SUB, meals_required=3)
    [menu] = normalizer._normalize_menu_weeks([_full_menu("2026-W50")], subscription)
    account = HelloFreshWeek(week_id="2026-W50", display_name="W50", subscription_id=SUB)

    [week] = normalizer._merge_menu_weeks_into_account_weeks([account], [menu])

    kept = week.raw["_menu_payload"]
    assert set(kept) == {"id", "week", "meals", "modularity", "addOns", "categories", "filters"}
    # Of the meals, only the one in the box, with what pricing reads from it.
    assert kept["meals"] == [
        {
            "index": 1,
            "selection": {"quantity": 2, "limit": 0},
            "charge": {"label": "+$5", "unitAmount": 500},
        }
    ]
    assert [recipe.recipe_id for recipe in week.recipes] == ["r-1", "r-2", "r-3"]
    # Everything read from the blocks later still is.
    normalizer._apply_variation_titles([week])
    normalizer._apply_market_items([week])
    normalizer._apply_menu_categories([week])
    normalizer._apply_menu_filters([week])
    assert {r.recipe_id: r.variation_title for r in week.recipes}["r-3"] == "2x Chicken"
    assert [item.name for item in week.market_items] == ["Cake"]
    assert week.menu_categories[0]["recipe_ids"] == ["r-1"]
    assert week.menu_filters[0]["slug"] == "main-protein"


def test_pricing_still_finds_the_meals_in_the_box_after_the_menu_is_trimmed() -> None:
    client = make_client(FakeHelloFresh())
    subscription = HelloFreshSubscription(
        subscription_id=SUB,
        account_id=str(CUSTOMER),
        locale="en-US",
        servings=2,
        raw={"customerPlanId": PLAN, "shippingAddress": {"postcode": "02101"}},
    )
    [menu] = client._normalize_menu_weeks([_full_menu("2026-W50")], subscription)
    account = HelloFreshWeek(
        week_id="2026-W50",
        display_name="W50",
        subscription_id=SUB,
        raw={
            "product": {"handle": "US-CBU-3-2-0", "price": 6594},
            "deliveryOption": {"handle": "US-1-0800-2000"},
        },
    )
    [week] = client._merge_menu_weeks_into_account_weeks([account], [menu])

    payload = client._build_cart_price_payload(subscription, week)

    meals = payload["products"][1]
    assert meals["quantityPerCourse"] == [{"index": 1, "quantity": 2}]
    assert meals["recipeIndexes"] == ["1", "1"]


# ---- D. a week that delivered only add-ons is history too -------------------------------------


async def test_a_history_week_that_delivered_only_add_ons_is_kept() -> None:
    w1, w2 = _weeks_ago(1), _weeks_ago(2)
    server = FakeHelloFresh()
    _paged_history(
        server,
        {
            None: {
                "weeks": [
                    {"week": w1, "meals": [], "addons": [{"id": "m-des", "name": "Cake"}]},
                    _history_week(w2, "m2"),
                ],
                "nextWeek": None,
            }
        },
    )
    client = make_client(server)

    weeks = await client._async_get_past_delivery_weeks(
        [HelloFreshSubscription(subscription_id=SUB)]
    )

    by_id = {week.week_id: week for week in weeks}
    assert set(by_id) == {w1, w2}
    assert [item.name for item in by_id[w1].market_items] == ["Cake"]
    assert by_id[w1].recipes == []
