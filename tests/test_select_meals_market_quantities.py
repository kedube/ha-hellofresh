"""select_meals can carry the week's Market add-ons in the same cart write.

The unified card saves a box's meals and extras with one button, and ``market_quantities``
puts both halves in one PUT. Every cart write reads the week's menu again first and builds
both halves from what HelloFresh answers, so the half it doesn't change is the one in the box
now, not the one the last poll saw.

These pin the contract: one write with both halves, omission keeps the extras in the box, an
empty map clears them, a bad item fails before anything is written, and a week HelloFresh has
no menu for refuses rather than guessing.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from homeassistant.config_entries import ConfigEntryState
import pytest
import voluptuous as vol

from custom_components.hellofresh.api import (
    HelloFreshAccountData,
    HelloFreshClient,
    HelloFreshError,
    HelloFreshMarketItem,
    HelloFreshRecipe,
    HelloFreshSubscription,
    HelloFreshWeek,
)
from custom_components.hellofresh.const import DOMAIN
from custom_components.hellofresh.models import HelloFreshNoMenuError
from custom_components.hellofresh.services import async_register_services


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


# The week's menu as HelloFresh answers it: three meals, none in the box yet, and a Market
# catalog with the salmon bites in the box (one) and a cake that isn't.
MENU = {
    "week": "2026-W26",
    "meals": [
        {
            "index": i,
            "selection": {"limit": 2},
            "recipe": {"id": f"recipe-{i}", "name": f"Meal {i}"},
        }
        for i in (11, 18, 20)
    ],
    "addOns": {
        "groups": [
            {
                "groupType": "appetizer",
                "sku": "US-AAB-0-0-0",
                "addOns": [
                    {
                        "index": 70185,
                        "sku": "US-AAB-0-0-0",
                        "maxQuantity": 6,
                        "recipe": {"id": "m-app", "name": "Salmon Bites"},
                        "selection": {"oneOffQuantity": 1, "preselectedQuantity": 0},
                    }
                ],
            },
            {
                "groupType": "dessert",
                "sku": "US-DES-0-0-0",
                "addOns": [
                    {
                        "index": 70200,
                        "sku": "US-DES-0-0-0",
                        "maxQuantity": 4,
                        "recipe": {"id": "m-des", "name": "Bundt Cake"},
                        "selection": None,
                    }
                ],
            },
        ]
    },
}


def _client(*, menu_payload: bool = True):
    """A client whose one known week has a menu, a Market catalog and one extra already chosen.

    Without ``menu_payload`` HelloFresh has no menu for the week (it answers 404).
    """
    client = HelloFreshClient(
        session=None,  # type: ignore[arg-type]
        access_token="token",
        enable_public_menu_fallback=False,
    )
    subscription = HelloFreshSubscription(
        subscription_id="1000001",
        account_id="20000001",
        locale="en-US",
        servings=2,
        raw={
            "customerPlanId": "plan-123",
            "product": {"sku": "US-CBU-3-2-0"},
            "shippingAddress": {"postcode": "01103"},
        },
    )
    raw: dict = {"product": {"handle": "US-CBU-3-2-0"}}
    if menu_payload:
        raw["_menu_payload"] = {"week": "2026-W26"}
    week = HelloFreshWeek(
        week_id="2026-W26",
        display_name="Week 26",
        subscription_id="1000001",
        selection_deadline=datetime(2026, 6, 17, 23, 59, 59, tzinfo=timezone(timedelta(hours=-7))),
        meals_required=3,
        recipes=[
            HelloFreshRecipe(recipe_id=f"recipe-{i}", name=f"Meal {i}", course_index=i)
            for i in (11, 18, 20)
        ],
        market_items=[
            HelloFreshMarketItem(
                item_id="m-app",
                name="Salmon Bites",
                index=70185,
                sku="US-AAB-0-0-0",
                group_type="appetizer",
                max_quantity=6,
                is_selected=True,
                selected_quantity=1,
            ),
            HelloFreshMarketItem(
                item_id="m-des",
                name="Bundt Cake",
                index=70200,
                sku="US-DES-0-0-0",
                group_type="dessert",
                max_quantity=4,
            ),
        ],
        raw=raw,
    )
    client._last_account_data = HelloFreshAccountData(weeks=[week]).finalize()
    requests: list[dict] = []

    async def fake_subs():
        return [subscription]

    async def fake_pref(_s):
        return "quick"

    class JsonResponse:
        status = 200

        def __init__(self, body: dict) -> None:
            self._body = body

        async def json(self, content_type=None):
            return self._body

    async def fake_req(
        method, path, params=None, json_payload=None, extra_headers=None, _allow_refresh_retry=True
    ):
        if method == "GET":
            # The week's menu, read again before every write; any other read isn't served.
            if path == "/gw/my-deliveries/menu" and menu_payload:
                return JsonResponse(MENU)
            raise HelloFreshError(f"HelloFresh request failed for {path}: HTTP 404", status=404)
        requests.append({"method": method, "path": path, "json_payload": json_payload})
        return JsonResponse({"hasSeamlessDowngraded": False})

    client._async_get_subscriptions = fake_subs  # type: ignore[method-assign]
    client._async_get_subscription_plan_preference = fake_pref  # type: ignore[method-assign]
    client._async_api_request = fake_req  # type: ignore[method-assign]
    return client, requests


MEALS = ["recipe-11", "recipe-18", "recipe-20"]


def test_meals_and_market_go_out_in_one_cart_write() -> None:
    client, requests = _client()
    _run(client.async_select_meals("2026-W26", MEALS, {"recipe-18": 2}, {"m-des": 2}))

    assert len(requests) == 1, "meals and extras must share ONE cart PUT"
    payload = requests[0]["json_payload"]
    assert payload["meals"] == [
        {"index": 11, "quantity": 1},
        {"index": 18, "quantity": 2},
        {"index": 20, "quantity": 1},
    ]
    # The requested extras REPLACE the old ones: the salmon bites (not requested) are gone.
    assert payload["extras"] == [
        {
            "groupType": "dessert",
            "sku": "US-DES-0-0-0",
            "selection": [
                {"index": 70200, "oneOffQuantity": 2, "preselectedQuantity": 0, "courses": []}
            ],
        }
    ]


def test_without_market_quantities_the_current_extras_are_kept() -> None:
    """The pre-existing behaviour, unchanged: a meal-only save carries the chosen extras over."""
    client, requests = _client()
    _run(client.async_select_meals("2026-W26", MEALS))

    extras = requests[0]["json_payload"]["extras"]
    assert [group["sku"] for group in extras] == ["US-AAB-0-0-0"]


def test_an_empty_market_map_clears_the_extras() -> None:
    client, requests = _client()
    _run(client.async_select_meals("2026-W26", MEALS, None, {}))

    assert requests[0]["json_payload"]["extras"] == []


def test_market_items_resolve_by_sku_and_index_like_the_market_write() -> None:
    client, requests = _client()
    _run(client.async_select_meals("2026-W26", MEALS, None, {"US-AAB-0-0-0": 3, "70200": 1}))

    selection = {
        group["sku"]: group["selection"][0]["oneOffQuantity"]
        for group in requests[0]["json_payload"]["extras"]
    }
    assert selection == {"US-AAB-0-0-0": 3, "US-DES-0-0-0": 1}


@pytest.mark.parametrize(
    ("market", "match"),
    [
        ({"nope": 1}, "not in week 2026-W26's catalog"),
        ({"m-des": 5}, "allows at most 4"),
    ],
)
def test_a_bad_add_on_fails_before_anything_is_written(market, match) -> None:
    """Half a save is worse than none: the meals must not be written if the extras can't be."""
    client, requests = _client()
    with pytest.raises(HelloFreshError, match=match):
        _run(client.async_select_meals("2026-W26", MEALS, None, market))
    assert requests == []


def test_a_week_without_a_menu_refuses_rather_than_guessing() -> None:
    """No menu, no cart write: there is no other write path to fall back to.

    Meal-only endpoints nobody had seen HelloFresh accept used to be tried in turn here.
    """
    client, requests = _client(menu_payload=False)
    with pytest.raises(HelloFreshNoMenuError, match="no menu for week 2026-W26"):
        _run(client.async_select_meals("2026-W26", MEALS, None, {"m-des": 1}))
    assert requests == []


# ---- service layer --------------------------------------------------------------------------


class _Services:
    def __init__(self) -> None:
        self.handlers: dict = {}
        self.schemas: dict = {}

    def has_service(self, _domain, service) -> bool:
        return service in self.handlers

    def async_register(self, _domain, service, handler, schema=None, **_kw):
        self.handlers[service] = handler
        self.schemas[service] = schema


def _service_hass(calls: list):
    async def async_select_meals(week_id, recipe_ids, quantities=None, market_quantities=None):
        calls.append((week_id, recipe_ids, quantities, market_quantities))
        return False

    async def async_request_refresh():
        return None

    async def async_after_write(*, full_refresh: bool = True):
        return None

    coordinator = SimpleNamespace(
        client=SimpleNamespace(async_select_meals=async_select_meals),
        async_request_refresh=async_request_refresh,
        async_after_write=async_after_write,
        config_entry=SimpleNamespace(entry_id="entry1", title="HelloFresh"),
    )
    entry = SimpleNamespace(
        entry_id="entry1",
        title="HelloFresh",
        options={},
        domain=DOMAIN,
        state=ConfigEntryState.LOADED,
        runtime_data=coordinator,
    )
    return SimpleNamespace(
        services=_Services(),
        config_entries=SimpleNamespace(
            async_entries=lambda _d: [entry], async_get_entry={"entry1": entry}.get
        ),
    )


def _register(hass):
    async_register_services(hass)
    return hass.services


def test_schema_accepts_market_quantities_and_coerces_them() -> None:
    services = _register(_service_hass([]))
    schema = services.schemas["select_meals"]
    data = schema(
        {"week_id": "2026-W26", "recipe_ids": MEALS, "market_quantities": {"m1": "2", "m2": 0}}
    )
    assert data["market_quantities"] == {"m1": 2, "m2": 0}
    with pytest.raises(vol.Invalid):
        schema({"week_id": "2026-W26", "recipe_ids": MEALS, "market_quantities": {"m1": -1}})


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({}, None),  # absent -> keep the week's extras
        ({"market_quantities": {}}, {}),  # present but empty -> clear them
        ({"market_quantities": {"m1": 2}}, {"m1": 2}),
    ],
)
def test_handler_distinguishes_absent_from_empty(monkeypatch, extra, expected) -> None:
    monkeypatch.setattr(
        "custom_components.hellofresh.coordinator.async_delete_write_actions_issue",
        lambda *_a, **_k: None,
    )
    calls: list = []
    hass = _service_hass(calls)
    services = _register(hass)
    call = SimpleNamespace(data={"week_id": "2026-W26", "recipe_ids": MEALS, **extra}, hass=hass)
    response = asyncio.new_event_loop().run_until_complete(services.handlers["select_meals"](call))

    assert response == {"downgraded": False}
    assert calls == [("2026-W26", MEALS, {}, expected)]
