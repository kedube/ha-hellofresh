"""HelloFresh cookbook (favorites) operations.

Split out of ``client.py`` (which had grown past 4,900 lines) as a mixin: the methods run
on ``HelloFreshClient`` exactly as before — same ``self`` attributes, same call sites —
this module only gives the area its own file. ``token_manager.py`` and
``tls_transport.py`` set the precedent.
"""

from __future__ import annotations

from collections.abc import Sequence
import logging

from aiohttp import ClientError

from .const import api_country_code
from .models import (
    HelloFreshError,
    HelloFreshFavorite,
    HelloFreshWeek,
)
from .parsers import _seg, safe_error_summary

_LOGGER = logging.getLogger(__name__)


class FavoritesClientMixin:
    """Cookbook bookmark reads/writes; mixed into HelloFreshClient."""

    # ---- Cookbook (favorites) -------------------------------------------------
    #
    # HelloFresh bookmarks ("favorites") span two cookbook collections, all HAR-verified:
    #   POST   /gw/cookbook/v1/internal-recipes/search   -> which of these ids are bookmarked
    #   POST   /gw/cookbook/v1/internal-recipes          -> create a bookmark (HTTP 201)
    #   GET    /gw/cookbook/v1/external-recipes          -> list ALL bookmarks (paginated)
    #   DELETE /gw/cookbook/v1/external-recipes/{id}     -> remove one (HTTP 204)
    #
    # The naming is counter-intuitive and worth stating plainly: bookmarking a HelloFresh
    # recipe is a POST to *internal*-recipes, but the resulting row is listed and deleted
    # through *external*-recipes. The delete targets the server-assigned row id (the `id`
    # field), not the `bookmark_id`.
    #
    # Two ways to read favorites, with different costs:
    #   • search  — a FILTER: send candidate bookmark_ids, get back the subset that is
    #     bookmarked as bare {id, bookmark_id} pairs (no title/image). Right for decorating a
    #     week's menu, where the candidate ids are already in hand.
    #   • external-recipes — the full list, with title, headline, thumbnail, times, and
    #     nutrition, plus a `total_count`. Right for "show me my cookbook" with no candidates.

    _COOKBOOK_PATH = "/gw/cookbook/v1/internal-recipes"
    # Listing and deletion both live under the external-recipes collection (see above).
    _COOKBOOK_LIST_PATH = "/gw/cookbook/v1/external-recipes"
    # The website's cookbook page only ever renders a 3-item preview (limit=3), but the
    # endpoint reports the true `total_count` and pages the rest via an opaque `cursor` —
    # so the full cookbook IS reachable even though the UI caps at 3.
    _COOKBOOK_LIST_PAGE = 50
    # Backstop so a server that keeps returning has_more can't spin forever.
    _COOKBOOK_LIST_MAX_PAGES = 20
    # Bookmark ids are batched into one search call. HelloFresh's own web app sends ~40 ids per
    # request; this cap keeps us in the same range rather than posting a 400-id week in one go.
    _COOKBOOK_SEARCH_BATCH = 50

    def _cookbook_params(self) -> dict[str, str]:
        return {
            "country": api_country_code(self._country),
            "locale": self._locale_for_country(),
        }

    def _bookmark_id(self, recipe_id: str) -> str:
        """Build the ``<recipeId>-<locale>`` bookmark id HelloFresh keys cookbook rows by."""
        bare = str(recipe_id).split("-", 1)[0]
        return f"{bare}-{self._locale_for_country()}"

    async def async_get_favorite_recipe_ids(
        self, recipe_ids: Sequence[str]
    ) -> dict[str, HelloFreshFavorite]:
        """Return which of ``recipe_ids`` are bookmarked, keyed by bare recipe id.

        Because the endpoint only echoes ids back, the returned favorites carry no title or
        image — they answer "is this a favorite?", which is what a heart badge needs. An empty
        input short-circuits without a request. Failures are non-fatal: this decorates existing
        data, so a cookbook outage must not fail the whole poll.
        """
        favorites, _ = await self._async_search_favorites(recipe_ids)
        return favorites

    async def _async_search_favorites(
        self, recipe_ids: Sequence[str]
    ) -> tuple[dict[str, HelloFreshFavorite], bool]:
        """Batched cookbook lookup. Returns ``(favorites, any_batch_answered)``.

        The second element distinguishes "no bookmarks among these ids" from "every request
        failed" — an empty dict alone cannot tell those apart, and callers that write a
        definitive False need to know which one happened.
        """
        unique: list[str] = []
        seen: set[str] = set()
        for recipe_id in recipe_ids:
            bare = str(recipe_id).split("-", 1)[0]
            if bare and bare not in seen:
                seen.add(bare)
                unique.append(bare)
        if not unique:
            return {}, True

        found: dict[str, HelloFreshFavorite] = {}
        answered = False
        for start in range(0, len(unique), self._COOKBOOK_SEARCH_BATCH):
            batch = unique[start : start + self._COOKBOOK_SEARCH_BATCH]
            payload = {"bookmark_ids": [self._bookmark_id(rid) for rid in batch]}
            try:
                response = await self._async_api_request(
                    "POST",
                    f"{self._COOKBOOK_PATH}/search",
                    params=self._cookbook_params(),
                    json_payload=payload,
                )
                if response.status >= 400:
                    raise HelloFreshError(f"HTTP {response.status}")
                body = await self._async_response_json(response)
            except (HelloFreshError, ClientError) as err:
                # Favorites are decoration layered onto already-fetched data, so a cookbook
                # failure must never fail the poll — record it and leave the state unknown.
                self._record_debug_attempt(
                    "cookbook_attempts",
                    {
                        "path": f"{self._COOKBOOK_PATH}/search",
                        "count": len(batch),
                        "error": str(err),
                    },
                )
                continue

            answered = True
            rows = body.get("recipes") if isinstance(body, dict) else None
            for row in rows if isinstance(rows, list) else []:
                favorite = HelloFreshFavorite.from_api(row)
                if favorite is not None:
                    found[favorite.recipe_id] = favorite

        self._record_debug_attempt(
            "cookbook_attempts",
            {
                "path": f"{self._COOKBOOK_PATH}/search",
                "requested": len(unique),
                "found": len(found),
                "answered": answered,
            },
        )
        return found, answered

    async def _async_apply_favorites(self, weeks: Sequence[HelloFreshWeek]) -> None:
        """Flag each week's recipes with their cookbook bookmark state.

        Every distinct recipe id across all weeks is resolved in one batched pass, since the
        same dish recurs across weeks and as portion variants. Best-effort by design: on failure
        ``is_favorite`` stays None, which the card renders as "no heart" rather than "not a
        favorite", so a cookbook outage never shows a misleading empty heart.
        """
        if not self._enable_favorites:
            return
        recipe_ids = {
            recipe.recipe_id for week in weeks for recipe in week.recipes if recipe.recipe_id
        }
        if not recipe_ids:
            return
        try:
            favorites, answered = await self._async_search_favorites(sorted(recipe_ids))
        except HelloFreshError as err:  # pragma: no cover - defensive; the call already guards
            _LOGGER.debug("HelloFresh favorites lookup failed: %s", err)
            return
        if not answered:
            # Every batch failed, so "not in favorites" would be a guess rather than an answer.
            # Leave is_favorite as None so the card shows no heart at all.
            return
        for week in weeks:
            for recipe in week.recipes:
                bare = str(recipe.recipe_id).split("-", 1)[0]
                recipe.is_favorite = bare in favorites

    async def async_add_favorite(self, recipe_id: str) -> HelloFreshFavorite:
        """Bookmark a recipe. Returns the created favorite (rich: title, image, nutrition)."""
        payload = {
            "bookmark_id": self._bookmark_id(recipe_id),
            "bookmark_source": "hellofresh",
        }
        response = await self._async_api_request(
            "POST",
            self._COOKBOOK_PATH,
            params=self._cookbook_params(),
            json_payload=payload,
        )
        if response.status >= 400:
            details = await response.text()
            raise HelloFreshError(
                f"HelloFresh add-favorite failed (HTTP {response.status}): {details[:300]}"
            )
        body = await self._async_response_json(response)
        favorite = HelloFreshFavorite.from_api(body)
        if favorite is None:
            # A 2xx with an unparseable body still means the bookmark was created, so report
            # success with what we know rather than raising.
            favorite = HelloFreshFavorite(
                bookmark_id=payload["bookmark_id"],
                recipe_id=str(recipe_id).split("-", 1)[0],
            )
        _LOGGER.info("HelloFresh favorite added for recipe %s", favorite.recipe_id)
        return favorite

    async def async_list_favorites(self, limit: int | None = None) -> list[HelloFreshFavorite]:
        """Return every recipe bookmarked in the customer's cookbook, newest first.

        Unlike the search endpoint (a filter needing candidate ids), this is a genuine listing
        and returns full detail — title, headline, thumbnail, times, nutrition. Pages until
        ``has_more`` clears, or until ``limit`` rows have been collected.
        """
        favorites: list[HelloFreshFavorite] = []
        seen: set[str] = set()
        cursor: str | None = None
        total_count: int | None = None
        for _page in range(self._COOKBOOK_LIST_MAX_PAGES):
            page_size = self._COOKBOOK_LIST_PAGE
            if limit is not None:
                remaining = limit - len(favorites)
                if remaining <= 0:
                    break
                page_size = min(page_size, remaining)
            params = {**self._cookbook_params(), "limit": str(page_size)}
            # Paging is CURSOR-based, not offset-based: the response's `next_cursor` is an
            # opaque token (base64 of the next row's id) that must be echoed back verbatim.
            # An `offset` param is simply ignored, which would re-serve page one forever.
            if cursor:
                params["cursor"] = cursor
            response = await self._async_api_request("GET", self._COOKBOOK_LIST_PATH, params=params)
            if response.status >= 400:
                raise HelloFreshError(
                    f"HelloFresh cookbook listing failed (HTTP {response.status})"
                )
            payload = await self._async_response_json(response)
            rows = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(rows, list) or not rows:
                break
            new_rows = 0
            for row in rows:
                favorite = HelloFreshFavorite.from_api(row)
                # Belt and braces: if the cursor were ever ignored and page one re-served,
                # this stops duplicates accumulating up to the page cap.
                if favorite is not None and favorite.bookmark_id not in seen:
                    seen.add(favorite.bookmark_id)
                    favorites.append(favorite)
                    new_rows += 1
            pagination = payload.get("pagination") if isinstance(payload, dict) else None
            if isinstance(pagination, dict):
                if isinstance(pagination.get("total_count"), int):
                    total_count = pagination["total_count"]
                has_more = bool(pagination.get("has_more"))
                next_cursor = pagination.get("next_cursor")
            else:
                has_more, next_cursor = False, None
            # Stop unless the server both says there is more AND hands back a usable cursor;
            # without one there is no way to advance, and re-requesting would loop.
            if not has_more or not isinstance(next_cursor, str) or not next_cursor:
                break
            if not new_rows:
                # The page advanced but contributed nothing new — treat as exhausted rather
                # than burning the remaining page budget.
                break
            cursor = next_cursor

        self._record_debug_attempt(
            "cookbook_attempts",
            {
                "path": self._COOKBOOK_LIST_PATH,
                "listed": len(favorites),
                "total_count": total_count,
            },
        )
        if total_count is not None and limit is None and len(favorites) < total_count:
            # Surfaces a paging contract change instead of silently under-reporting.
            _LOGGER.warning(
                "HelloFresh cookbook listing returned %d of %d bookmarks; "
                "pagination may have changed",
                len(favorites),
                total_count,
            )
        return favorites

    async def async_remove_favorite(self, recipe_id: str) -> bool:
        """Remove a bookmark. Returns True when HelloFresh accepted the delete.

        The delete targets the server-assigned row id under the *external*-recipes collection
        (``DELETE /gw/cookbook/v1/external-recipes/{id}`` → HTTP 204), even though the bookmark
        was created under *internal*-recipes. That row id is not the bookmark id, so it has to
        be resolved first via the search endpoint.
        """
        bare = str(recipe_id).split("-", 1)[0]
        existing = await self.async_get_favorite_recipe_ids([bare])
        favorite = existing.get(bare)
        if favorite is None:
            # Already not a favorite: report a no-op rather than erroring, so callers can call
            # this idempotently without pre-checking.
            _LOGGER.debug("HelloFresh recipe %s is not favorited; nothing to remove", bare)
            return False
        if not favorite.favorite_id:
            raise HelloFreshError(
                f"HelloFresh returned no cookbook row id for recipe {bare}; cannot remove it."
            )

        response = await self._async_api_request(
            "DELETE",
            f"{self._COOKBOOK_LIST_PATH}/{_seg(favorite.favorite_id)}",
            params=self._cookbook_params(),
        )
        if response.status >= 400:
            details = await response.text()
            raise HelloFreshError(
                f"HelloFresh remove-favorite failed (HTTP {response.status}): "
                f"{safe_error_summary(details)}"
            )
        _LOGGER.info("HelloFresh favorite removed for recipe %s", bare)
        return True
