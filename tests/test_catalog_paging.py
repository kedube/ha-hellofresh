"""Paging through the public recipe catalog: a category's list and the whole-catalog search.

A category (or the top-rated listing) is one document HelloFresh sends whole and the client keeps
for an hour, so a page is a slice of it and costs no request after the first. The search is
paged by HelloFresh: ``take`` results after the first ``skip``, with ``skip`` sent only from the
second page on. Either way the response says where the page stands: ``offset``, ``total``
(a non-negative whole number, or null) and ``has_more`` (a real boolean).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import voluptuous as vol

from custom_components.hellofresh.models import HelloFreshCatalogRecipe
from custom_components.hellofresh.services import SERVICES
from test_client_fixes import FakeHelloFresh, make_client

ROWS = [{"id": f"{n:024x}", "name": f"Recipe {n}"} for n in range(1, 8)]
CATEGORY = "/_next/data/build-1/recipes/chicken-recipes.json"
SEARCH = "/gw/recipes/recipes/search"


def _catalog_server(rows=ROWS) -> FakeHelloFresh:
    server = FakeHelloFresh()
    server.route("GET", "/recipes", (200, '{"buildId":"build-1"}'))
    payload = {"pageProps": {"ssrPayload": {"collection": {"recipes": rows}}}}
    server.route("GET", CATEGORY, (200, payload))
    server.route("GET", "/_next/data/build-1/recipes.json", (200, payload))
    return server


def _names(page: dict) -> list[str]:
    return [recipe.name for recipe in page["recipes"]]


# ---- a category ------------------------------------------------------------------------------


async def test_a_category_is_paged_from_the_list_read_once() -> None:
    server = _catalog_server()
    client = make_client(server)

    first = await client.async_get_catalog_page("chicken-recipes", limit=3, include_favorites=False)
    second = await client.async_get_catalog_page(
        "chicken-recipes", limit=3, offset=3, include_favorites=False
    )
    last = await client.async_get_catalog_page(
        "chicken-recipes", limit=3, offset=6, include_favorites=False
    )

    assert _names(first) == ["Recipe 1", "Recipe 2", "Recipe 3"]
    assert (first["offset"], first["total"], first["has_more"]) == (0, 7, True)
    assert _names(second) == ["Recipe 4", "Recipe 5", "Recipe 6"]
    assert (second["offset"], second["total"], second["has_more"]) == (3, 7, True)
    assert _names(last) == ["Recipe 7"]
    assert (last["offset"], last["total"], last["has_more"]) == (6, 7, False)
    assert server.paths().count(CATEGORY) == 1


async def test_the_top_rated_listing_pages_the_same_way() -> None:
    client = make_client(_catalog_server())

    page = await client.async_get_catalog_page(None, limit=5, offset=5, include_favorites=False)

    assert _names(page) == ["Recipe 6", "Recipe 7"]
    assert (page["total"], page["has_more"], page["subcollections"]) == (7, False, [])


async def test_a_page_past_the_end_is_empty_and_the_last() -> None:
    client = make_client(_catalog_server())
    page = await client.async_get_catalog_page(
        "chicken-recipes", limit=3, offset=50, include_favorites=False
    )
    assert (page["recipes"], page["total"], page["has_more"]) == ([], 7, False)


async def test_duplicates_count_once_in_the_total() -> None:
    client = make_client(_catalog_server([*ROWS[:3], ROWS[0], {"noise": True}]))
    page = await client.async_get_catalog_page("chicken-recipes", include_favorites=False)
    assert (len(page["recipes"]), page["total"], page["has_more"]) == (3, 3, False)


async def test_an_unreadable_category_has_no_total() -> None:
    server = FakeHelloFresh()  # nothing routed: the page cannot be read
    page = await make_client(server).async_get_catalog_page(
        "chicken-recipes", include_favorites=False
    )
    assert (page["recipes"], page["total"], page["has_more"]) == ([], None, False)


async def test_only_the_pages_recipes_are_looked_up_in_the_cookbook() -> None:
    client = make_client(_catalog_server())
    asked: list[list[str]] = []

    async def favorites(recipe_ids):
        asked.append(list(recipe_ids))
        return {recipe_ids[0]: object()}

    client.async_get_favorite_recipe_ids = favorites  # type: ignore[method-assign]

    page = await client.async_get_catalog_page("chicken-recipes", limit=2, offset=2)

    assert asked == [[ROWS[2]["id"], ROWS[3]["id"]]]
    assert [recipe.is_favorite for recipe in page["recipes"]] == [True, False]


async def test_the_older_list_call_still_returns_the_first_recipes() -> None:
    client = make_client(_catalog_server())
    recipes = await client.async_get_catalog_recipes(
        "chicken-recipes", limit=2, include_favorites=False
    )
    assert [recipe.name for recipe in recipes] == ["Recipe 1", "Recipe 2"]


# ---- the search ------------------------------------------------------------------------------


def _search_server(total, rows_by_skip: dict[int, list[dict]]) -> FakeHelloFresh:
    server = FakeHelloFresh()

    def answer(params, _body):
        rows = rows_by_skip.get(int(params.get("skip", 0)), [])
        return 200, {"items": rows, "count": len(rows), "take": int(params["take"]), "total": total}

    server.route("GET", SEARCH, answer)
    return server


async def test_the_search_is_paged_by_hellofresh_with_skip_from_the_second_page() -> None:
    server = _search_server(5, {0: ROWS[:3], 3: ROWS[3:5]})
    client = make_client(server)

    first = await client.async_search_catalog_page("paneer", limit=3, include_favorites=False)
    second = await client.async_search_catalog_page(
        "paneer", limit=3, offset=3, include_favorites=False
    )

    params = [request["params"] for request in server.requests if request["path"] == SEARCH]
    assert params == [
        {"country": "US", "locale": "en-US", "q": "paneer", "take": "3"},
        {"country": "US", "locale": "en-US", "q": "paneer", "take": "3", "skip": "3"},
    ]
    assert _names(first) == ["Recipe 1", "Recipe 2", "Recipe 3"]
    assert (first["offset"], first["total"], first["has_more"]) == (0, 5, True)
    assert _names(second) == ["Recipe 4", "Recipe 5"]
    assert (second["offset"], second["total"], second["has_more"]) == (3, 5, False)


@pytest.mark.parametrize(
    ("total", "rows", "expected_total", "has_more"),
    [
        (None, ROWS[:3], None, True),  # no total: a full page may have more after it
        (None, ROWS[:2], None, False),  # a short page is the last
        (-4, ROWS[:3], None, True),  # a total that cannot be one is no total
        (40, [], 40, False),  # an empty page ends the results whatever the total says
        ("12", ROWS[:3], 12, True),
    ],
)
async def test_a_search_page_without_a_usable_total_still_says_whether_more_follow(
    total, rows, expected_total, has_more
) -> None:
    client = make_client(_search_server(total, {0: rows}))
    page = await client.async_search_catalog_page("paneer", limit=3, include_favorites=False)
    assert page["total"] == expected_total
    assert page["has_more"] is has_more


async def test_a_blank_search_asks_nothing() -> None:
    server = FakeHelloFresh()
    page = await make_client(server).async_search_catalog_page("  ", offset=4)
    assert page == {"recipes": [], "offset": 4, "total": 0, "has_more": False}
    assert server.requests == []


# ---- the service -----------------------------------------------------------------------------


def _service():
    return next(spec for spec in SERVICES if spec.name == "get_catalog_recipes")


def _recipe(n: int) -> HelloFreshCatalogRecipe:
    return HelloFreshCatalogRecipe(recipe_id=f"{n:024x}", name=f"Recipe {n}")


def test_the_offset_defaults_to_zero_and_cannot_be_negative() -> None:
    schema = vol.Schema(_service().fields)
    assert schema({})["offset"] == 0
    assert schema({"offset": "20", "limit": 10}) == {"offset": 20, "limit": 10}
    with pytest.raises(vol.Invalid):
        schema({"offset": -1})


async def _respond(client, data: dict) -> dict:
    coordinator = SimpleNamespace(client=client)
    return await _service().handler(coordinator, SimpleNamespace(data=data))


async def test_the_service_passes_the_offset_and_reports_where_the_page_stands() -> None:
    calls: dict = {}

    async def category(collection, *, limit, offset):
        calls["category"] = (collection, limit, offset)
        return {
            "collection": collection,
            "recipes": [_recipe(4)],
            "subcollections": [],
            "offset": offset,
            "total": 7,
            "has_more": True,
        }

    async def search(query, *, limit, offset):
        calls["search"] = (query, limit, offset)
        return {"recipes": [_recipe(9)], "offset": offset, "total": None, "has_more": 1}

    client = SimpleNamespace(async_get_catalog_page=category, async_search_catalog_page=search)

    page = await _respond(client, {"collection": "chicken-recipes", "limit": 3, "offset": 3})
    found = await _respond(client, {"search": "paneer", "limit": 3, "offset": 6})

    assert calls == {"category": ("chicken-recipes", 3, 3), "search": ("paneer", 3, 6)}
    assert (page["offset"], page["total"], page["has_more"]) == (3, 7, True)
    assert page["recipes"][0]["name"] == "Recipe 4"
    assert found["search"] == "paneer"
    assert (found["offset"], found["total"]) == (6, None)
    # Whatever the client said, the card gets a real boolean.
    assert found["has_more"] is True
    for response in (page, found):
        assert {"collection", "recipes", "subcollections"} <= set(response)


async def test_the_service_never_reports_a_total_that_is_not_a_count() -> None:
    async def category(collection, *, limit, offset):
        return {
            "collection": None,
            "recipes": [],
            "subcollections": [],
            "offset": offset,
            "total": True,
            "has_more": None,
        }

    client = SimpleNamespace(async_get_catalog_page=category)
    page = await _respond(client, {"limit": 50, "offset": 0})
    assert page["total"] is None
    assert page["has_more"] is False
