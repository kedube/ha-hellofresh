"""Server-side menu filters: the ``menu_filters`` block and ``get_menu_courses``."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.hellofresh.api import HelloFreshClient, HelloFreshError, HelloFreshWeek

# Verbatim shape from the 2026-09 capture's /gw/my-deliveries/menu `filters` block.
FILTERS_BLOCK = [
    {
        "name": "Cuisine type",
        "type": "cuisine",
        "choice": "MULTI-OR",
        "options": [
            {"name": "Classic American", "type": "regional-specialty", "default": False},
            {"name": "Mediterranean", "type": "mediterranean", "default": False},
        ],
    },
    {
        "name": "Ingredients to avoid",
        "type": "exclude-allergens",
        "choice": "MULTI-AND",
        "options": [
            {"name": "Nuts", "type": "nuts", "default": False},
            {"name": "", "type": "broken", "default": False},
        ],
    },
    {"name": "Empty group", "type": "empty", "choice": "SINGLE", "options": []},
    "garbage",
]


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _client() -> HelloFreshClient:
    return HelloFreshClient(session=object(), access_token="token")  # type: ignore[arg-type]


def test_menu_filters_parse_to_slugged_groups_and_serialize_on_the_week() -> None:
    client = _client()
    parsed = client._build_menu_filters({"filters": FILTERS_BLOCK})
    assert parsed == [
        {
            "name": "Cuisine type",
            "slug": "cuisine",
            "choice": "MULTI-OR",
            "options": [
                {"name": "Classic American", "slug": "regional-specialty", "default": False},
                {"name": "Mediterranean", "slug": "mediterranean", "default": False},
            ],
        },
        {
            "name": "Ingredients to avoid",
            "slug": "exclude-allergens",
            "choice": "MULTI-AND",
            "options": [{"name": "Nuts", "slug": "nuts", "default": False}],
        },
    ]
    # Nested under the stashed menu payload works too, and the week carries it to the card.
    week = HelloFreshWeek(
        week_id="2026-W37", display_name="W37", raw={"_menu_payload": {"filters": FILTERS_BLOCK}}
    )
    client._apply_menu_filters([week])
    assert [group["slug"] for group in week.menu_filters] == ["cuisine", "exclude-allergens"]
    assert week.as_dict()["menu_filters"] == week.menu_filters
    # History weeks have no menu payload: nothing attached, nothing broken.
    bare = HelloFreshWeek(week_id="2026-W30", display_name="W30")
    client._apply_menu_filters([bare])
    assert bare.menu_filters == []


def test_get_menu_courses_builds_the_captured_query_and_returns_recipe_ids() -> None:
    client = _client()
    calls: list[tuple[str, dict]] = []

    async def fake_get(path, params=None, **_kwargs):
        calls.append((path, dict(params or {})))
        return SimpleNamespace(status=200)

    async def fake_json(_response):
        return {
            "courses": [
                {"index": 1, "recipeFamily": "classic-menu", "recipeId": "abc", "parent": None},
                {"index": 2, "recipeFamily": "classic-menu", "recipeId": "def", "parent": None},
                {"index": 3, "recipeFamily": "classic-menu", "recipeId": "abc", "parent": None},
                "junk",
            ]
        }

    client._async_api_get = fake_get  # type: ignore[method-assign]
    client._async_response_json = fake_json  # type: ignore[method-assign]

    ids = _run(
        client.async_get_menu_courses(
            "2026-W37",
            {
                "dish-type": ["family-style", "salad-and-bowls"],
                "total-cooking-time": "cooking-time-30",
                "cuisine": [],
                "Diet": "Vegetarian, high-protein ",
            },
        )
    )
    assert ids == ["abc", "def"]
    assert calls == [
        (
            "/gw/my-deliveries/courses",
            {
                "country": "US",
                "locale": "en-US",
                "week": "2026-W37",
                # Multiple values are comma-joined, exactly as the site sends them.
                "dish-type": "family-style,salad-and-bowls",
                "total-cooking-time": "cooking-time-30",
                "diet": "vegetarian,high-protein",
            },
        )
    ]


@pytest.mark.parametrize(
    ("week_id", "filters"),
    [
        ("2026-37", {}),
        ("", {}),
        ("2026-W37", {"cuisine": ["med iterranean"]}),
        ("2026-W37", {"cuisine=x": ["a"]}),
        ("2026-W37", {"week": ["2026-W01"]}),
        ("2026-W37", {"cuisine": 5}),
    ],
)
def test_get_menu_courses_rejects_malformed_input_before_any_request(week_id, filters) -> None:
    client = _client()

    async def fail(*_args, **_kwargs):
        raise AssertionError("no request expected")

    client._async_api_get = fail  # type: ignore[method-assign]
    with pytest.raises(HelloFreshError):
        _run(client.async_get_menu_courses(week_id, filters))
