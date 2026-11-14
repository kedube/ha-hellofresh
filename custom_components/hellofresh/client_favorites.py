"""HelloFresh cookbook (favorites) operations, and the read of the separate ``cfs`` store.

Split out of ``client.py`` (which had grown past 4,900 lines) as a mixin on
``client_core._ClientCore``: the methods run on ``HelloFreshClient`` exactly as before — same
``self`` attributes, same call sites — this module only gives the area its own file.
``token_manager.py`` and ``tls_transport.py`` set the precedent.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
import json
import logging
from typing import Any

from .client_core import _ClientCore
from .const import api_country_code
from .models import (
    HelloFreshAuthError,
    HelloFreshBotBlockedError,
    HelloFreshError,
    HelloFreshFavorite,
    HelloFreshRateLimitedError,
    HelloFreshWeek,
)
from .parsers import _seg

_LOGGER = logging.getLogger(__name__)

# A rate limit on the cookbook search stops the batches not yet sent (see
# _async_search_favorites); these two end the call: reauth, or the shared bot cooldown.
_SEARCH_RAISES = (HelloFreshAuthError, HelloFreshBotBlockedError)
_HTTP_TOO_MANY_REQUESTS = 429


class FavoritesClientMixin(_ClientCore):
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
    #     bookmarked as bare {id, bookmark_id} pairs (no title/image). Right for a few
    #     candidates: one recipe, a catalog page.
    #   • external-recipes — the full list, with title, headline, thumbnail, times, and
    #     nutrition, plus a `total_count`. Right for "show me my cookbook", and for the hearts
    #     on every menu: a page per 50 bookmarks, where searching for every recipe on every
    #     published menu took about 60 requests a poll and HelloFresh answered with 429s.

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
        input short-circuits without a request. A cookbook outage is non-fatal (this decorates
        existing data) and leaves the result empty; a rejected sign-in or a bot block is raised.
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
        # Once HelloFresh answers 429, the batches not yet sent are dropped: more requests only
        # add to the burst it is refusing.
        rate_limited = False

        async def _search_batch(batch: list[str]) -> tuple[list[Any], bool]:
            """Look up one batch. Returns ``(rows, answered)``.

            ``answered`` is returned explicitly rather than inferred from ``rows``: a batch
            that succeeds with no bookmarks and a batch that failed outright both yield an
            empty list, and the caller's False-vs-unknown decision depends on telling them
            apart. (It is also why this cannot lean on ``_async_gather_bounded``'s own
            error swallowing, which collapses a failure to a bare ``[]``.)
            """
            nonlocal rate_limited
            if rate_limited:
                return [], False
            payload = {"bookmark_ids": [self._bookmark_id(rid) for rid in batch]}
            try:
                response = await self._async_api_request(
                    "POST",
                    f"{self._COOKBOOK_PATH}/search",
                    params=self._cookbook_params(),
                    json_payload=payload,
                )
                body = await self._async_response_json(response)
            except _SEARCH_RAISES:
                raise
            except HelloFreshError as err:
                # Favorites are decoration layered onto already-fetched data, so a cookbook
                # failure leaves the state unknown rather than failing the call.
                if (
                    isinstance(err, HelloFreshRateLimitedError)
                    or err.status == _HTTP_TOO_MANY_REQUESTS
                ):
                    rate_limited = True
                self._record_debug_attempt(
                    "cookbook_attempts",
                    {
                        "path": f"{self._COOKBOOK_PATH}/search",
                        "count": len(batch),
                        "error": str(err),
                    },
                )
                return [], False

            rows = body.get("recipes") if isinstance(body, dict) else None
            return (rows if isinstance(rows, list) else []), True

        # A few hundred distinct recipe ids at 50 per request is 6-9 round-trips; awaiting
        # them in sequence made this the last serialized network loop in the poll. Bounded
        # so a large cookbook doesn't burst connections at HelloFresh.
        batches = [
            unique[start : start + self._COOKBOOK_SEARCH_BATCH]
            for start in range(0, len(unique), self._COOKBOOK_SEARCH_BATCH)
        ]
        results = await self._async_gather_bounded([_search_batch(batch) for batch in batches])

        found: dict[str, HelloFreshFavorite] = {}
        answered = False
        for result in results:
            # _async_gather_bounded degrades an unexpected HelloFreshError to a bare [];
            # only a real (rows, answered) pair counts as an answer.
            if not isinstance(result, tuple):
                continue
            rows, batch_answered = result
            answered = answered or batch_answered
            for row in rows:
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
                "rate_limited": rate_limited,
            },
        )
        return found, answered

    async def _async_apply_favorites(self, weeks: Sequence[HelloFreshWeek]) -> None:
        """Flag each week's recipes with their cookbook bookmark state.

        The cookbook is listed once (a request per 50 rows) and each recipe looked up in it.
        Searching for every recipe on every published menu instead took about 60 requests a
        poll, and HelloFresh refused a third of them with 429. Best-effort by design: on failure
        ``is_favorite`` stays None, which the card renders as "no heart" rather than "not a
        favorite", so a cookbook outage never shows a misleading empty heart; a listing that
        stopped early leaves the recipes it didn't find unknown too.
        """
        if not any(recipe.recipe_id for week in weeks for recipe in week.recipes):
            return
        cookbook = await self._async_list_cookbook_for_poll()
        if cookbook is not None:
            self._apply_cookbook_favorites(weeks, *cookbook)

    async def _async_list_cookbook_for_poll(
        self,
    ) -> tuple[list[HelloFreshFavorite], bool] | None:
        """List the cookbook for the poll's hearts: ``(favorites, complete)``, or None.

        None when favorites are switched off or the cookbook couldn't be read (the hearts then
        stay unknown). A rejected sign-in, a bot block or a rate limit is raised: the poll
        stops on those. Needs no menu, so the poll runs it beside the menu stage.
        """
        if not self._enable_favorites:
            return None
        try:
            return await self._async_list_cookbook()
        except (HelloFreshAuthError, HelloFreshBotBlockedError, HelloFreshRateLimitedError):
            raise
        except HelloFreshError as err:
            _LOGGER.debug("HelloFresh favorites lookup failed: %s", err)
            self._record_debug_attempt(
                "cookbook_attempts", {"path": self._COOKBOOK_LIST_PATH, "error": str(err)}
            )
            return None

    @staticmethod
    def _apply_cookbook_favorites(
        weeks: Sequence[HelloFreshWeek], favorites: Sequence[HelloFreshFavorite], complete: bool
    ) -> None:
        """Flag each recipe in the cookbook; with a complete listing, the rest as not."""
        saved = {favorite.recipe_id for favorite in favorites}
        for week in weeks:
            for recipe in week.recipes:
                bare = str(recipe.recipe_id).split("-", 1)[0]
                if bare in saved:
                    recipe.is_favorite = True
                elif complete:
                    recipe.is_favorite = False

    async def async_add_favorite(self, recipe_id: str) -> HelloFreshFavorite:
        """Bookmark a recipe. Returns the created favorite (rich: title, image, nutrition).

        The recipe's heart is set in the account snapshot too (see ``last_account_data``),
        with no request.
        """
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
        try:
            body = await self._async_response_json(response)
        except HelloFreshError:
            body = None
        favorite = HelloFreshFavorite.from_api(body) if isinstance(body, dict) else None
        if favorite is None:
            # A 2xx with an unparseable body still means the bookmark was created, so report
            # success with what we know rather than raising.
            favorite = HelloFreshFavorite(
                bookmark_id=payload["bookmark_id"],
                recipe_id=str(recipe_id).split("-", 1)[0],
            )
        _LOGGER.info("HelloFresh favorite added for recipe %s", favorite.recipe_id)
        self._publish_favorite(favorite.recipe_id, True)
        return favorite

    def _publish_favorite(self, recipe_id: str, is_favorite: bool) -> None:
        """Store a snapshot with the recipe's heart set, after a cookbook write.

        Only the weeks holding the recipe are copied; the previous snapshot is left as it was.
        """
        data = self._last_account_data
        if data is None:
            return
        bare = str(recipe_id).split("-", 1)[0]

        def _matches(recipe_id: str) -> bool:
            return str(recipe_id).split("-", 1)[0] == bare

        weeks = [
            replace(
                week,
                recipes=[
                    replace(recipe, is_favorite=is_favorite)
                    if _matches(recipe.recipe_id)
                    else recipe
                    for recipe in week.recipes
                ],
            )
            if any(_matches(recipe.recipe_id) for recipe in week.recipes)
            else week
            for week in data.weeks
        ]
        self._publish_snapshot(weeks=weeks)

    async def async_list_favorites(self, limit: int | None = None) -> list[HelloFreshFavorite]:
        """Return every recipe bookmarked in the customer's cookbook, newest first.

        Unlike the search endpoint (a filter needing candidate ids), this is a genuine listing
        and returns full detail — title, headline, thumbnail, times, nutrition. Pages until
        ``has_more`` clears, or until ``limit`` rows have been collected.
        """
        favorites, _complete = await self._async_list_cookbook(limit)
        return favorites

    async def _async_list_cookbook(
        self, limit: int | None = None
    ) -> tuple[list[HelloFreshFavorite], bool]:
        """List the cookbook. Returns ``(favorites, complete)``.

        ``complete`` is True only when paging reached the end of the cookbook: a ``limit``,
        the page cap, a missing cursor or a replayed page all leave rows unread.
        """
        complete = False
        favorites: list[HelloFreshFavorite] = []
        seen: set[str] = set()
        seen_rows: set[str] = set()
        # Rows that are not HelloFresh bookmarks: the cookbook also stores recipes imported from
        # other sites and social media (HelloFresh's app imports them), which have no
        # bookmark id.
        other_rows = 0
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
            payload = await self._async_response_json(response)
            rows = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                break
            if not rows:
                complete = True
                break
            new_rows = 0
            for row in rows:
                # Progress is counted in raw rows, not parsed bookmarks: a page holding only
                # imported recipes still advances. Belt and braces: if the cursor were ever
                # ignored and page one re-served, this stops duplicates up to the page cap.
                row_key = self._cookbook_row_key(row)
                if row_key in seen_rows:
                    continue
                seen_rows.add(row_key)
                new_rows += 1
                favorite = HelloFreshFavorite.from_api(row)
                if favorite is None:
                    other_rows += 1
                elif favorite.bookmark_id not in seen:
                    seen.add(favorite.bookmark_id)
                    favorites.append(favorite)
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
            if not has_more:
                complete = True
                break
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            if not new_rows:
                # The page advanced but contributed nothing new — treat as exhausted rather
                # than burning the remaining page budget.
                break
            cursor = next_cursor

        if total_count is not None and limit is None and len(favorites) + other_rows < total_count:
            # Surfaces a paging contract change instead of silently under-reporting.
            complete = False
            _LOGGER.warning(
                "HelloFresh cookbook listing returned %d of %d bookmarks; "
                "pagination may have changed",
                len(favorites),
                total_count,
            )
        self._record_debug_attempt(
            "cookbook_attempts",
            {
                "path": self._COOKBOOK_LIST_PATH,
                "listed": len(favorites),
                "other_rows": other_rows,
                "total_count": total_count,
                "complete": complete,
            },
        )
        return favorites, complete

    @staticmethod
    def _cookbook_row_key(row: Any) -> str:
        """Return a stable identity for a raw cookbook listing row."""
        if isinstance(row, dict):
            for key in ("id", "bookmark_id"):
                value = row.get(key)
                if isinstance(value, (str, int)) and str(value):
                    return f"{key}:{value}"
        return json.dumps(row, sort_keys=True, default=str)

    async def async_remove_favorite(self, recipe_id: str) -> bool:
        """Remove a bookmark. Returns True when HelloFresh accepted the delete.

        The delete targets the server-assigned row id under the *external*-recipes collection
        (``DELETE /gw/cookbook/v1/external-recipes/{id}`` → HTTP 204), even though the bookmark
        was created under *internal*-recipes. That row id is not the bookmark id, so it has to
        be resolved first via the search endpoint. Returns False when HelloFresh answered that
        the recipe isn't bookmarked; raises HelloFreshError when it didn't answer at all (a
        failed lookup must not read as "nothing to remove" and leave the bookmark in place).
        """
        bare = str(recipe_id).split("-", 1)[0]
        existing, answered = await self._async_search_favorites([bare])
        if not answered:
            raise HelloFreshError(
                f"HelloFresh's cookbook could not be read, so recipe {bare} was not removed"
            )
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

        await self._async_api_request(
            "DELETE",
            f"{self._COOKBOOK_LIST_PATH}/{_seg(favorite.favorite_id)}",
            params=self._cookbook_params(),
        )
        _LOGGER.info("HelloFresh favorite removed for recipe %s", bare)
        self._publish_favorite(bare, False)
        return True

    # A SECOND, separate favorites service HelloFresh also runs (`cfs`), backing its
    # /recipes/favorites page. It is NOT the cookbook: different service, different paging
    # (skip/take/total rather than a cursor), and in captured traffic it reported total=0 while
    # the cookbook held six bookmarks — so the two stores are not synchronized. HelloFresh's
    # Android app keeps its favorite "heart" here (it writes PUT/DELETE
    # `cfs/v2/favorites/recipe/{id}`), with rows `{object_id: <recipe id>}` under `items` (or
    # `data`/`result`). Read-only here; it never overrides cookbook state.
    _CFS_FAVORITES_PATH = "/gw/cfs/v2/favorites/recipe"

    async def async_get_cfs_favorites(
        self, recipe_ids: Sequence[str] | None = None
    ) -> dict[str, Any]:
        """Read HelloFresh's secondary ("cfs") favorites store.

        Returns ``{"total", "count", "items", "available"}``. ``available`` is False when the
        endpoint could not be read at all, which is distinct from an empty-but-working store.
        Because no populated response has ever been captured, ``items`` are passed through as
        raw dicts rather than being coerced into a model that might not match.
        """
        params = {
            "country": api_country_code(self._country),
            "locale": self._locale_for_country(),
            # The website always sends `ids`, empty when it is not filtering to specific
            # recipes; mirroring that keeps the request shape identical to a real one. Several
            # ids are joined with "|", as HelloFresh's Android app sends them.
            "ids": "|".join(str(r).split("-", 1)[0] for r in recipe_ids) if recipe_ids else "",
        }
        payload = await self._async_get_json(
            self._CFS_FAVORITES_PATH, params, trace_category="cookbook_attempts", optional=True
        )
        if payload is None:
            return {"total": 0, "count": 0, "items": [], "recipe_ids": [], "available": False}

        items = next(
            (
                value
                for key in ("items", "data", "result")
                if isinstance(value := payload.get(key), list)
            ),
            None,
        )
        rows = [item for item in items if isinstance(item, dict)] if items else []
        return {
            "total": payload.get("total"),
            "count": payload.get("count"),
            "items": rows,
            "recipe_ids": [
                str(row["object_id"]) for row in rows if isinstance(row.get("object_id"), str)
            ],
            "available": True,
        }
