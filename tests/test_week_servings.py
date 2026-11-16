"""One week's box for another number of people, and what a week says about its own box.

``change_week_servings`` changes ONE week's box, as HelloFresh's Android app does: the box keeps
its meal count and its handle's people segment is replaced. It is priced first, refused when the
plan doesn't sell that size or the week can't change, and an endpoint the website doesn't offer
raises the write-actions repair. After it, the snapshot (and so the next get_weeks) already shows
the week's new ``servings``, even when HelloFresh's own read lags.

HelloFresh is faked behind the client's transport (see test_client_fixes.py); every account
detail is made up.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest

from custom_components.hellofresh.coordinator import async_client_error
from custom_components.hellofresh.models import (
    HelloFreshAccountData,
    HelloFreshError,
    HelloFreshInvalidRequestError,
    HelloFreshNotImplementedError,
    HelloFreshOrder,
    HelloFreshSubscription,
    HelloFreshWeek,
)
from custom_components.hellofresh.parsers import box_handle_parts, local_today
from custom_components.hellofresh.sensor_helpers import sensor_extra_state_attributes
from custom_components.hellofresh.services import SERVICES
from test_client_fixes import SUB, Account, FakeHelloFresh, make_client

ISSUE_CREATE = "custom_components.hellofresh.coordinator.async_create_write_actions_issue"


def _plan_options(*handles: str) -> dict:
    """A product_options answer offering ``handles`` (one family)."""
    products = []
    for handle in handles:
        _prefix, meals, people, _n = box_handle_parts(handle)
        products.append(
            {"handle": handle, "specs": {"meals": meals, "size": people}, "price": 1000 * meals}
        )
    return {
        "count": 1,
        "items": [{"handle": "classic-box-unified", "name": "Classic", "products": products}],
    }


# 2 to 4 meals, each for 2 or 4 people.
CLASSIC = _plan_options(*(f"US-CBU-{m}-{p}-0" for m in (2, 3, 4) for p in (2, 4)))


class ResizableAccount(Account):
    """The fake account with its plan's box sizes, box pricing and the one-week resize."""

    def __init__(self, server: FakeHelloFresh, options: dict = CLASSIC) -> None:
        super().__init__(server)
        self.resizes: list[dict] = []
        # Whether HelloFresh's next read of the week shows a resize (it can lag the write).
        self.shows_resize = True
        server.route("GET", f"/gw/api/subscriptions/{SUB}/product_options", (200, options))
        server.route("POST", "/gw/calculate", self._calculate)
        server.route("PUT", f"/gw/carts/{self.week_id}/size", self._resize)

    def week(self) -> dict:
        week = super().week()
        week["product"]["specs"]["size"] = int(self.box.split("-")[3])
        return week

    @staticmethod
    def _calculate(_params, body):
        _prefix, meals, people, _n = box_handle_parts(body["products"][0]["handle"])
        return 200, {"grandTotal": 10.0 * meals * people + 9.99, "currency": "USD"}

    def _resize(self, params, body):
        self.resizes.append({"params": params, **body})
        if self.shows_resize:
            self.box = body["productHandle"]
        return 200, {}


async def _polled(**options) -> tuple[FakeHelloFresh, ResizableAccount, object, object]:
    server = FakeHelloFresh()
    account = ResizableAccount(server, **options)
    client = make_client(server)
    data = await client.async_get_account_data()
    return server, account, client, data


# ---- the resize ------------------------------------------------------------------------------


async def test_one_weeks_box_is_priced_resized_and_shown_at_once() -> None:
    server, account, client, data = await _polled()
    assert data.get_week(account.week_id).servings == 2
    start = len(server.requests)

    result = await client.async_change_week_servings(account.week_id, 4)

    writes = server.writes(start)
    resize = [write for write in writes if write["method"] == "PUT"]
    assert resize == [
        {
            "method": "PUT",
            "path": f"/gw/carts/{account.week_id}/size",
            "params": {"subscription": SUB},
            "json": {"productHandle": "US-CBU-3-4-0", "hfMarketCode": "US"},
        }
    ]
    # Priced before it is changed, as the app does: the price is for the new box.
    first_price = next(write for write in writes if write["path"] == "/gw/calculate")
    assert first_price["json"]["products"][0]["handle"] == "US-CBU-3-4-0"
    assert writes.index(first_price) < writes.index(resize[0])
    assert result == {
        "week_id": account.week_id,
        "servings": 4,
        "product_handle": "US-CBU-3-4-0",
        "price": 129.99,
        "currency": "USD",
        "changed": True,
    }
    # The published snapshot already shows the week's new box; the old one is left alone.
    fresh = client.last_account_data
    assert fresh is not data
    week = fresh.get_week(account.week_id)
    assert week.servings == 4
    assert week.as_summary_dict()["servings"] == 4
    assert data.get_week(account.week_id).servings == 2


async def test_the_new_box_shows_even_while_hellofreshs_read_still_has_the_old_one() -> None:
    server, account, client, data = await _polled()
    account.shows_resize = False  # HelloFresh's own read lags the write

    await client.async_change_week_servings(account.week_id, 4)

    week = client.last_account_data.get_week(account.week_id)
    assert week.servings == 4
    assert week.raw["product"]["handle"] == "US-CBU-3-4-0"
    assert week.raw["product"]["specs"] == {"meals": 3, "size": 4}
    assert data.get_week(account.week_id).raw["product"]["handle"] == "US-CBU-3-2-0"


async def test_a_failed_re_read_still_shows_the_new_box() -> None:
    server, account, client, data = await _polled()
    original_resize = account._resize

    def resize_then_fail_reads(params, body):
        # The week can't be read again once the box has changed.
        server.route("GET", "/gw/api/customers/me/deliveries", (503, {"error": "busy"}))
        return original_resize(params, body)

    server.route("PUT", f"/gw/carts/{account.week_id}/size", resize_then_fail_reads)

    result = await client.async_change_week_servings(account.week_id, 4)

    assert result["changed"] is True
    assert client.last_account_data is not data
    assert client.last_account_data.get_week(account.week_id).servings == 4


async def test_the_same_number_of_people_writes_nothing() -> None:
    server, account, client, data = await _polled()
    start = len(server.requests)

    result = await client.async_change_week_servings(account.week_id, 2)

    assert result == {
        "week_id": account.week_id,
        "servings": 2,
        "product_handle": "US-CBU-3-2-0",
        "price": 69.99,
        "currency": "USD",
        "changed": False,
    }
    # At most the box is priced (the poll's price for it is reused); nothing is changed.
    assert {write["path"] for write in server.writes(start)} <= {"/gw/calculate"}
    assert account.resizes == []
    assert client.last_account_data is data


async def test_the_box_is_read_from_hellofresh_so_a_meal_change_since_keeps_its_count() -> None:
    """The week's box is read under its lock: a 4-meal change made in the app stays 4 meals."""
    _server, account, client, _data = await _polled()
    account.box = "US-CBU-4-2-0"  # changed in the HelloFresh app after the poll

    await client.async_change_week_servings(account.week_id, 4)

    assert account.resizes[0]["productHandle"] == "US-CBU-4-4-0"


async def test_a_meal_change_and_a_resize_of_one_week_run_in_turn() -> None:
    _server, account, client, _data = await _polled()

    await asyncio.gather(
        client.async_select_meals(
            account.week_id, ["recipe-11", "recipe-18", "recipe-20", "recipe-32"]
        ),
        client.async_change_week_servings(account.week_id, 4),
    )

    # The resize waited for the meal change, read its 4-meal box and kept the 4 meals.
    assert account.resizes[0]["productHandle"] == "US-CBU-4-4-0"
    assert account.box == "US-CBU-4-4-0"


# ---- refused before anything is written ------------------------------------------------------


@pytest.mark.parametrize(
    ("options", "servings", "offered"),
    [
        (CLASSIC, 3, "It offers 2, 4."),
        # Only the week's own box family counts.
        (_plan_options("US-VEG-3-4-0", "US-CBU-2-4-0"), 4, ""),
    ],
    ids=["size-not-sold", "other-family"],
)
async def test_a_size_the_plan_does_not_offer_is_refused(options, servings, offered) -> None:
    server, account, client, data = await _polled(options=options)
    start = len(server.requests)

    with pytest.raises(HelloFreshInvalidRequestError, match="doesn't offer a box of 3 meals") as e:
        await client.async_change_week_servings(account.week_id, servings)

    assert str(e.value).endswith(f"this plan.{(' ' + offered) if offered else ''}")
    assert [write for write in server.writes(start) if write["method"] == "PUT"] == []
    assert client.last_account_data is data


async def test_a_week_skipped_since_the_poll_is_refused() -> None:
    server, account, client, _data = await _polled()
    account.status = "PAUSED"  # skipped in the app after the poll
    start = len(server.requests)

    with pytest.raises(HelloFreshInvalidRequestError, match="is skipped"):
        await client.async_change_week_servings(account.week_id, 4)

    assert server.writes(start) == []


async def test_a_week_past_its_deadline_is_refused() -> None:
    server, account, client, _data = await _polled()
    account.delivery = local_today() + timedelta(days=3)  # its deadline was two days ago
    start = len(server.requests)

    with pytest.raises(HelloFreshInvalidRequestError, match="can no longer be changed"):
        await client.async_change_week_servings(account.week_id, 4)

    assert server.writes(start) == []


@pytest.mark.parametrize("servings", [0, -1, True, "4"])
async def test_a_number_of_people_that_is_not_one_is_refused(servings) -> None:
    server, account, client, _data = await _polled()
    start = len(server.requests)

    with pytest.raises(HelloFreshInvalidRequestError, match="whole number"):
        await client.async_change_week_servings(account.week_id, servings)

    assert server.requests[start:] == []


async def test_an_unknown_week_is_refused() -> None:
    server, _account, client, _data = await _polled()
    start = len(server.requests)

    with pytest.raises(HelloFreshInvalidRequestError, match="week not found"):
        await client.async_change_week_servings("2099-W01", 4)

    assert server.requests[start:] == []


async def test_a_box_handle_of_another_shape_cannot_be_resized() -> None:
    _server, account, client, _data = await _polled()
    account.box = "US-CBU-3-2"  # no recurrency segment: not a box handle

    with pytest.raises(HelloFreshNotImplementedError, match="can't be resized"):
        await client.async_change_week_servings(account.week_id, 4)


# ---- a route the website doesn't have --------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (404, {"status": "NOT_FOUND"}),
        (405, {"error": "Method Not Allowed"}),
        (400, {"message": "No route found for PUT /gw/carts/2026-W47/size"}),
        (400, {"error": "unknown endpoint"}),
    ],
    ids=["404", "405", "400-route", "400-endpoint"],
)
async def test_a_route_hellofresh_does_not_offer_is_a_missing_capability(status, body) -> None:
    _server, account, client, data = await _polled()
    account.server.route("PUT", f"/gw/carts/{account.week_id}/size", (status, body))

    with pytest.raises(HelloFreshNotImplementedError) as raised:
        await client.async_change_week_servings(account.week_id, 4)

    assert str(raised.value) == "HelloFresh doesn't offer changing one week's servings here"
    assert client.last_account_data is data


@pytest.mark.parametrize(
    ("status", "body"),
    [(400, {"error": "PRODUCT_NOT_AVAILABLE", "message": "size not available"}), (500, {})],
    ids=["400-request", "500"],
)
async def test_any_other_refusal_is_reported_as_it_is(status, body) -> None:
    _server, account, client, _data = await _polled()
    account.server.route("PUT", f"/gw/carts/{account.week_id}/size", (status, body))

    with pytest.raises(HelloFreshError) as raised:
        await client.async_change_week_servings(account.week_id, 4)

    assert not isinstance(raised.value, HelloFreshNotImplementedError)
    assert raised.value.status == status


# ---- the service -----------------------------------------------------------------------------


def _service():
    return next(spec for spec in SERVICES if spec.name == "change_week_servings")


def _call(client, data: dict):
    coordinator = SimpleNamespace(
        client=client,
        async_after_write=mock.AsyncMock(),
        config_entry=SimpleNamespace(entry_id="entry-1", title="HelloFresh"),
    )
    call = SimpleNamespace(data=data, hass=SimpleNamespace())
    return coordinator, _service().handler(coordinator, call)


async def test_the_service_answers_the_interface_and_shows_the_change() -> None:
    client = SimpleNamespace(
        async_change_week_servings=mock.AsyncMock(
            return_value={
                "week_id": "2026-W47",
                "servings": 4,
                "product_handle": "US-CBU-3-4-0",
                "price": 129.99,
                "currency": "USD",
                "changed": True,
            }
        )
    )
    coordinator, handler = _call(client, {"week_id": "2026-W47", "servings": 4})
    with mock.patch(
        "custom_components.hellofresh.services.async_write_succeeded"
    ) as write_succeeded:
        response = await handler

    assert response == {
        "week_id": "2026-W47",
        "servings": 4,
        "product_handle": "US-CBU-3-4-0",
        "price": 129.99,
        "currency": "USD",
    }
    client.async_change_week_servings.assert_awaited_once_with("2026-W47", 4)
    write_succeeded.assert_called_once()
    coordinator.async_after_write.assert_awaited_once_with()


async def test_the_service_changing_nothing_neither_refreshes_nor_clears_the_repair() -> None:
    client = SimpleNamespace(
        async_change_week_servings=mock.AsyncMock(
            return_value={
                "week_id": "2026-W47",
                "servings": 2,
                "product_handle": "US-CBU-3-2-0",
                "price": None,
                "currency": None,
                "changed": False,
            }
        )
    )
    coordinator, handler = _call(client, {"week_id": "2026-W47", "servings": 2})
    with mock.patch(
        "custom_components.hellofresh.services.async_write_succeeded"
    ) as write_succeeded:
        response = await handler

    assert set(response) == {"week_id", "servings", "product_handle", "price", "currency"}
    write_succeeded.assert_not_called()
    coordinator.async_after_write.assert_not_awaited()


def test_the_schema_takes_a_week_and_a_whole_number_of_people() -> None:
    import voluptuous as vol  # noqa: PLC0415

    schema = vol.Schema(_service().fields)
    assert schema({"week_id": "2026-W47", "servings": "4"}) == {
        "week_id": "2026-W47",
        "servings": 4,
    }
    for bad in ({"week_id": "2026-W47"}, {"week_id": "2026-W47", "servings": 0}, {"servings": 4}):
        with pytest.raises(vol.Invalid):
            schema(bad)


def test_a_refused_change_is_the_callers_input_not_an_outage() -> None:
    entry = SimpleNamespace(entry_id="entry-1", title="HelloFresh", async_start_reauth=mock.Mock())
    with mock.patch(ISSUE_CREATE) as repair:
        error = async_client_error(
            SimpleNamespace(), entry, HelloFreshInvalidRequestError("Week 2026-W47 is skipped")
        )
        missing = async_client_error(
            SimpleNamespace(), entry, HelloFreshNotImplementedError("not offered here")
        )

    assert isinstance(error, ServiceValidationError)
    assert str(error) == "Week 2026-W47 is skipped"
    assert isinstance(missing, HomeAssistantError)
    assert not isinstance(missing, ServiceValidationError)
    repair.assert_called_once()  # only for the missing endpoint
    entry.async_start_reauth.assert_not_called()


# ---- what a week says about its own box ------------------------------------------------------


SUBSCRIPTION = HelloFreshSubscription(subscription_id=SUB, meals_required=3, servings=2)


def _week(**raw) -> HelloFreshWeek:
    client = make_client(FakeHelloFresh())
    return client._week_from_raw_delivery(
        0, {"id": "2026-W47", "subscriptionId": SUB, **raw}, SUBSCRIPTION
    )


@pytest.mark.parametrize(
    ("product", "servings"),
    [
        ({"handle": "US-CBU-3-4-0", "specs": {"meals": 3, "size": 4}}, 4),
        ({"handle": "US-CBU-3-4-0"}, 4),  # no specs: the handle's people
        ({"sku": "GB-CBU-2-1-0"}, 1),
        ({"handle": "US-CHARGE-0-0-0"}, None),  # a charge handle is not a box
        ("not a box", None),
    ],
)
def test_a_weeks_servings_are_its_own_boxs_people(product, servings) -> None:
    assert _week(product=product).servings == servings


def test_a_week_without_a_box_of_its_own_has_no_servings() -> None:
    assert _week().servings is None


@pytest.fixture
def new_york_time():
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(ZoneInfo("America/New_York"))
    yield
    dt_util.set_default_time_zone(previous)


@pytest.mark.parametrize(
    ("sub_status", "tracking", "revised"),
    [
        # An evening delivery in New York is already the next day in UTC.
        ("DELAYED", {"delivery_date": "2026-11-20T02:30:00+0000"}, date(2026, 11, 19)),
        ("EARLY", {"delivery_date": "2026-11-17T12:00:00+0000"}, date(2026, 11, 17)),
        ("delayed", {"estimated_delivery_time": "2026-11-21T16:00:00Z"}, date(2026, 11, 21)),
        ("DELAYED", {"delivery_date": "2026-11-22"}, date(2026, 11, 22)),  # a bare date
        ("DELAYED", None, None),
        ("DELAYED", {"delivery_date": "soon"}, None),
        ("RATING", {"delivery_date": "2026-11-20T12:00:00+0000"}, None),
        (None, {"delivery_date": "2026-11-20T12:00:00+0000"}, None),
    ],
)
def test_a_moved_box_has_the_day_it_now_comes(new_york_time, sub_status, tracking, revised):
    week = _week(subStatus=sub_status, tracking=tracking, deliveryDate="2026-11-18T12:00:00-0500")
    assert week.revised_delivery_date == revised
    # The scheduled day stays the scheduled day.
    assert week.delivery_date == date(2026, 11, 18)
    assert week.as_summary_dict()["revised_delivery_date"] == (
        revised.isoformat() if revised else None
    )


@pytest.fixture
def auckland_time():
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(ZoneInfo("Pacific/Auckland"))
    yield
    dt_util.set_default_time_zone(previous)


@pytest.mark.parametrize(
    ("tracking", "revised"),
    [
        # HelloFresh's noon-UTC placeholder is 01:00 the next day in Auckland (UTC+13).
        ({"delivery_date": "2026-11-17T12:00:00+0000"}, date(2026, 11, 17)),
        ({"estimated_delivery_time": "2026-11-17T12:00:00Z"}, date(2026, 11, 17)),
        # Any other moment is still read where the household is.
        ({"delivery_date": "2026-11-17T11:30:00+0000"}, date(2026, 11, 18)),
        ({"delivery_date": "2026-11-17T12:00:00-0500"}, date(2026, 11, 18)),
    ],
)
def test_a_moved_box_in_new_zealand_keeps_its_day(auckland_time, tracking, revised):
    """A moved box's new day comes as noon UTC, HelloFresh's placeholder, not a moment.

    Read in Home Assistant's time zone, east of UTC+12 it was the next day, so every moved box
    in New Zealand came out a day late.
    """
    week = _week(subStatus="EARLY", tracking=tracking, deliveryDate="2026-11-18T12:00:00+1300")
    assert week.revised_delivery_date == revised


def test_the_next_delivery_sensor_names_the_day_a_moved_box_now_comes() -> None:
    day = local_today() + timedelta(days=2)
    week = HelloFreshWeek(
        week_id="2026-W47",
        display_name="Week 47",
        subscription_id=SUB,
        delivery_date=day,
        sub_status="DELAYED",
        revised_delivery_date=day + timedelta(days=1),
    )
    order = HelloFreshOrder(
        order_id="o1", week_id="2026-W47", status="RUNNING", subscription_id=SUB, delivery_date=day
    )
    data = HelloFreshAccountData(weeks=[week], orders=[order]).finalize()

    attributes = sensor_extra_state_attributes("next_delivery_date", data)

    assert attributes["revised_delivery_date"] == (day + timedelta(days=1)).isoformat()
    week.revised_delivery_date = None
    assert (
        sensor_extra_state_attributes("next_delivery_date", data)["revised_delivery_date"] is None
    )
    # Only the next-delivery sensor carries it.
    assert "revised_delivery_date" not in sensor_extra_state_attributes(
        "next_box_total_price", data
    )
