"""Tests for full-catalog recipe text search (`async_search_catalog_recipes`).

The search rides the recipes-service /gw API (the recipe-detail family) — verified live —
so unlike the browse catalog it involves no build-id scraping. Row spellings differ from the
website catalog's (`ratingsCount` vs `aggregateRatingsCount`); both must parse.
"""

from __future__ import annotations

import asyncio

from custom_components.hellofresh.api import HelloFreshClient

SEARCH_PAYLOAD = {
    "total": 2,
    "skip": 0,
    "take": 50,
    "count": 2,
    "items": [
        {
            "id": "69ea8a4da9f41d816354c5eb",
            "name": "Paneer & Steak Tikka Masala",
            "headline": "with Sautéed Bell Pepper, Cilantro Rice",
            "slug": "paneer-and-steak-tikka-masala",
            "imagePath": "/image/paneer.jpg",
            "websiteUrl": "https://www.hellofresh.com/recipes/paneer-69ea8a4da9f41d816354c5eb",
            "averageRating": 3,
            "ratingsCount": 3,
            "prepTime": "PT25M",
        },
        {"id": "69ea8a4da9f41d816354c5eb", "name": "Duplicate Of The First"},  # deduped
        {
            "id": "5f3f2b4e6e2f2b0e6c000001",
            "name": "Paneer Tikka Bowls",
            "prepTime": "PT35M",
        },
        {"noise": True},  # not a recipe row
    ],
}


def _client_with_search(payload: object) -> tuple[HelloFreshClient, list[dict]]:
    client = HelloFreshClient(session=object())  # type: ignore[arg-type]
    requests: list[dict] = []

    class DummyResponse:
        status = 200

    async def fake_api_get(path, params=None, extra_headers=None):
        requests.append({"path": path, "params": params})
        return DummyResponse()

    async def fake_response_json(_response):
        return payload

    async def fake_favorites(recipe_ids):
        return {"5f3f2b4e6e2f2b0e6c000001"}

    client._async_api_get = fake_api_get  # type: ignore[method-assign]
    client._async_response_json = fake_response_json  # type: ignore[method-assign]
    client.async_get_favorite_recipe_ids = fake_favorites  # type: ignore[method-assign]
    return client, requests


def _run(coro):
    # Repo convention (see tests/test_api.py): a fresh loop per test, NOT closed —
    # Home Assistant's autouse verify_cleanup fixture reads the current loop at teardown
    # and raises on a closed one.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def test_search_hits_the_recipes_service_with_q() -> None:
    client, requests = _client_with_search(SEARCH_PAYLOAD)
    recipes = _run(client.async_search_catalog_recipes("paneer tikka", limit=50))
    assert requests == [
        {
            "path": "/gw/recipes/recipes/search",
            "params": {"country": "US", "locale": "en-US", "q": "paneer tikka", "take": "50"},
        }
    ]
    # Deduped and noise-dropped: 2 real recipes from 4 rows.
    assert [r.name for r in recipes] == ["Paneer & Steak Tikka Masala", "Paneer Tikka Bowls"]


def test_search_rows_parse_the_gw_spellings() -> None:
    client, _ = _client_with_search(SEARCH_PAYLOAD)
    first = _run(client.async_search_catalog_recipes("paneer", limit=50))[0]
    assert first.rating == 3
    assert first.ratings_count == 3  # `ratingsCount`, not the catalog's aggregate spelling
    assert first.prep_time_minutes == 25  # ISO-8601 PT25M
    assert first.image_url and first.image_url.endswith("/image/paneer.jpg")
    assert first.url and first.url.startswith("https://www.hellofresh.com/recipes/")


def test_search_results_are_favorite_flagged() -> None:
    client, _ = _client_with_search(SEARCH_PAYLOAD)
    recipes = _run(client.async_search_catalog_recipes("paneer", limit=50))
    assert [r.is_favorite for r in recipes] == [False, True]


def test_blank_query_makes_no_request() -> None:
    client, requests = _client_with_search(SEARCH_PAYLOAD)
    assert _run(client.async_search_catalog_recipes("   ")) == []
    assert requests == []


def test_malformed_payload_yields_no_recipes() -> None:
    client, _ = _client_with_search({"unexpected": True})
    assert _run(client.async_search_catalog_recipes("paneer")) == []
