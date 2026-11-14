"""The public recipe catalog: its categories, listings and search, and recipe detail."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
import json
import re
from time import monotonic
from typing import Any

from .client_core import (
    _HTTP_BAD_REQUEST,
    _HTTP_NOT_FOUND,
    _LOGGER,
    _ClientCore,
    _dict_at,
    _refused_with,
)
from .const import RECIPE_IMAGE_BASE, api_country_code
from .models import (
    HelloFreshCatalogRecipe,
    HelloFreshError,
    HelloFreshRecipeCollection,
    HelloFreshRecipeDetail,
)
from .parsers import _seg, coerce_int

# The public recipe catalog changes slowly, and each page costs a full website request.
_CATALOG_TTL = 3600.0
_CATALOG_CACHE_MAX = 32
# How long the catalog skips the Next.js data route after it 404ed on a freshly scraped build
# id: such a 404 means the route is down, not that the id is stale.
_NEXT_DATA_PAUSE = 3600.0

# Recipe ids are 24 hexadecimal characters; a meal's id can carry a "-<suffix>" variant. Only
# letters, digits and dashes reach the request path.
_RECIPE_ID_RE = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")
# A catalog category: a slug, or a nested path of slugs ("noodle-recipes/ramen-noodles").
_COLLECTION_RE = re.compile(r"^[a-z0-9-]+(?:/[a-z0-9-]+)*$")


class CatalogClientMixin(_ClientCore):
    """Recipe catalog reads; mixed into HelloFreshClient."""

    # ---- Recipe catalog (public browse content) -------------------------------
    #
    # The "All Recipes" browse catalog (~10k recipes) is served by the Next.js site rather than
    # a /gw/ API: https://www.hellofresh.com/_next/data/<buildId>/recipes.json. The build id
    # changes on every HelloFresh web deploy, so it is scraped from the page HTML on first use,
    # cached, and re-scraped once when a request 404s (the signature of a stale id).

    # Catalog rows carry a bare `imagePath` ("/image/foo.jpg") with no host. Verified against
    # the live site (the HAR captures never re-fetched these images, so they were browser-cached
    # and absent from the export): the site renders them from Cloudinary as
    #   https://img.hellofresh.com/<transform>/hellofresh_s3/image/<file>.jpg
    # Note the `hellofresh_s3` segment, which is part of the path and not optional. The
    # transform is what keeps the grid affordable — untransformed, this same asset is 1.7 MB
    # versus 73 KB at w_640 — so a default width is baked in here rather than left to the card.
    _CATALOG_IMAGE_BASE = RECIPE_IMAGE_BASE
    _BUILD_ID_RE = re.compile(r'"buildId"\s*:\s*"([A-Za-z0-9._-]{1,64})"')

    async def _async_get_build_id(self, *, force_refresh: bool = False) -> str | None:
        """Return the site's current Next.js build id, scraping and caching it on demand."""
        if self._build_id is not None and not force_refresh:
            return self._build_id
        try:
            response = await self._async_api_request("GET", "/recipes", authenticated=False)
            if response.status >= 400:
                raise HelloFreshError(f"HTTP {response.status}")
            html = await response.text()
        except HelloFreshError as err:
            self._record_debug_attempt("catalog_attempts", {"path": "/recipes", "error": str(err)})
            return None
        match = self._BUILD_ID_RE.search(html)
        if match is None:
            self._record_debug_attempt(
                "catalog_attempts", {"path": "/recipes", "error": "buildId not found in page"}
            )
            return None
        self._build_id = match.group(1)
        self._record_debug_attempt("catalog_attempts", {"build_id": self._build_id})
        return self._build_id

    # The same SSR payload the `_next/data` JSON returns is also embedded in the rendered page
    # inside <script id="__NEXT_DATA__">. It is the fallback when the JSON URL is unavailable.
    _NEXT_DATA_RE = re.compile(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

    async def _async_get_catalog_json(self, page: str, params: dict[str, str] | None = None) -> Any:
        """Fetch one catalog document's SSR payload.

        Tries the `_next/data` JSON URL first (small and precise), refreshing a stale build id
        once on 404, then falls back to scraping the same payload out of the rendered page's
        ``__NEXT_DATA__`` blob.

        The fallback is not paranoia: HelloFresh has served **404 for every `_next/data`
        catalog URL on every build id** while the corresponding pages render perfectly, which
        made the whole All Recipes card report "No recipes found". A cached 200 can linger for
        hours after the origin starts refusing (an observed response had `age: 38586`), so a
        URL that works once is not evidence the route is healthy. The page HTML carries the
        identical payload, so the card keeps working through it.
        """
        payload = await self._async_get_catalog_data_url(page, params)
        if payload is not None:
            return payload
        return await self._async_get_catalog_from_html(page, params)

    async def _async_get_catalog_document(
        self, page: str, params: dict[str, str] | None = None
    ) -> Any:
        """Return the parts of a catalog document the catalog reads, cached for an hour.

        The catalog changes slowly and each document costs a website request (a whole page
        when the data route is down), so browsing back and forth is served from here.
        """
        key = (page, json.dumps(params or {}, sort_keys=True))
        cached = self._catalog_cache.get(key)
        if cached is not None and monotonic() - cached[0] < _CATALOG_TTL:
            self._catalog_cache.move_to_end(key)
            return cached[1]
        payload = await self._async_get_catalog_json(page, params)
        if payload is None:
            return None
        essentials = self._catalog_essentials(payload)
        self._catalog_cache[key] = (monotonic(), essentials)
        self._catalog_cache.move_to_end(key)
        while len(self._catalog_cache) > _CATALOG_CACHE_MAX:
            self._catalog_cache.popitem(last=False)
        return essentials

    @staticmethod
    def _catalog_essentials(payload: Any) -> Any:
        """Reduce a catalog document to what the catalog reads from it, for the cache.

        That is the category (its sub-categories and its recipe list), the page's recipe
        queries and the top-rated list; the rest of a page's server-side state is dropped.
        """
        ssr = CatalogClientMixin._nested_get(payload, "pageProps", "ssrPayload")
        if not isinstance(ssr, dict):
            return payload
        collection = _dict_at(ssr, "collection")
        queries = CatalogClientMixin._nested_get(ssr, "dehydratedState", "queries")
        kept_queries: list[dict[str, Any]] = []
        for query in queries if isinstance(queries, list) else []:
            if not isinstance(query, dict):
                continue
            query_key = query.get("queryKey")
            name = query_key[0] if isinstance(query_key, list) and query_key else query_key
            data = CatalogClientMixin._nested_get(query, "state", "data")
            if not (isinstance(name, str) and name.startswith("foodContentHubRecipe")):
                continue
            if not isinstance(data, dict):
                continue
            kept_queries.append(
                {
                    "queryKey": query_key,
                    "state": {"data": {key: data[key] for key in ("pages", "docs") if key in data}},
                }
            )
        essentials: dict[str, Any] = {
            "collection": {
                "children": collection.get("children"),
                "recipes": collection.get("recipes"),
            },
            "dehydratedState": {"queries": kept_queries},
        }
        if "bestRatedRecipes" in ssr:
            essentials["bestRatedRecipes"] = ssr["bestRatedRecipes"]
        return {"pageProps": {"ssrPayload": essentials}}

    async def _async_get_catalog_data_url(
        self, page: str, params: dict[str, str] | None = None
    ) -> Any:
        """Fetch one Next.js data document, refreshing a stale build id once on 404.

        A 404 on a build id the site has only just handed out says the data route itself is
        down (HelloFresh has served that for every build id while the pages worked). The route
        is then left alone for ``_NEXT_DATA_PAUSE`` and the catalog reads the pages instead,
        rather than scraping the build id again for every document.
        """
        if monotonic() < self._next_data_paused_until:
            return None
        for attempt in (0, 1):
            fresh_id = attempt == 1 or self._build_id is None
            build_id = await self._async_get_build_id(force_refresh=attempt == 1)
            if build_id is None:
                return None
            path = f"/_next/data/{build_id}/{page}"
            try:
                # Public website data document: no credentials needed, none sent.
                response = await self._async_api_request(
                    "GET", path, params=params, authenticated=False
                )
            except HelloFreshError as err:
                self._record_debug_attempt("catalog_attempts", {"path": path, "error": str(err)})
                return None
            if response.status == _HTTP_NOT_FOUND and not fresh_id:
                # Usually a deploy rotating the build id: drop the cache and retry once.
                _LOGGER.debug("HelloFresh catalog 404 for %s; refreshing build id", path)
                continue
            if response.status >= _HTTP_BAD_REQUEST:
                if response.status == _HTTP_NOT_FOUND:
                    self._next_data_paused_until = monotonic() + _NEXT_DATA_PAUSE
                self._record_debug_attempt(
                    "catalog_attempts",
                    {"path": path, "status": response.status, "fresh_build_id": fresh_id},
                )
                return None
            return await self._async_response_json(response)
        return None

    async def _async_get_catalog_from_html(
        self, page: str, params: dict[str, str] | None = None
    ) -> Any:
        """Scrape a catalog page's SSR payload out of its rendered HTML.

        ``page`` is the data-URL form (``recipes.json`` / ``recipes/<slug>.json``); the
        corresponding page path is the same with the ``.json`` suffix dropped.

        Returns the payload in the SAME shape the data URL yields, so callers need no special
        casing: the HTML blob nests the payload under ``props``, which is unwrapped here.
        """
        path = "/" + page[: -len(".json")] if page.endswith(".json") else "/" + page
        try:
            # Public website page: no credentials needed, none sent.
            response = await self._async_api_request(
                "GET", path, params=params, authenticated=False
            )
            if response.status >= 400:
                raise HelloFreshError(f"HTTP {response.status}")
            html = await response.text()
        except HelloFreshError as err:
            self._record_debug_attempt(
                "catalog_attempts", {"path": path, "fallback": "html", "error": str(err)}
            )
            return None

        match = self._NEXT_DATA_RE.search(html)
        if match is None:
            self._record_debug_attempt(
                "catalog_attempts", {"path": path, "fallback": "html", "error": "no __NEXT_DATA__"}
            )
            return None
        try:
            blob = await self._async_json_loads(match.group(1))
        except ValueError as err:
            self._record_debug_attempt(
                "catalog_attempts", {"path": path, "fallback": "html", "error": str(err)}
            )
            return None

        # The data URL returns {pageProps: ...}; the HTML blob wraps that under `props`.
        props = blob.get("props") if isinstance(blob, dict) else None
        payload = props if isinstance(props, dict) else blob
        self._record_debug_attempt(
            "catalog_attempts", {"path": path, "fallback": "html", "status": response.status}
        )
        return payload

    async def async_get_recipe_collections(self) -> list[HelloFreshRecipeCollection]:
        """Return the browsable recipe categories (Chicken, Carb Smart, Hall of Fame, ...)."""
        payload = await self._async_get_catalog_document("recipes.json")
        collection = self._nested_get(payload, "pageProps", "ssrPayload", "collection")
        children = collection.get("children") if isinstance(collection, dict) else None
        docs = children.get("docs") if isinstance(children, dict) else None
        out: list[HelloFreshRecipeCollection] = []
        for row in docs if isinstance(docs, list) else []:
            parsed = HelloFreshRecipeCollection.from_api(row, image_base=self._CATALOG_IMAGE_BASE)
            if parsed is not None:
                out.append(parsed)
        return out

    # Full recipe detail comes from a plain /gw/ API — NOT the website's Next.js data URLs —
    # so unlike the browse catalog it needs no build-id scraping and cannot break on a deploy.
    # HAR-observed being called from the My Menu page for a single recipe id.
    _RECIPE_DETAIL_PATH = "/gw/recipes/recipes"

    @staticmethod
    def _bare_recipe_id(recipe_id: Any) -> str:
        """Return a recipe id without a ``-<suffix>``, refusing anything that isn't one.

        The id goes into a request path, so only letters, digits and dashes are accepted.
        """
        text = str(recipe_id or "").strip()
        if not text:
            raise HelloFreshError("A recipe id is required")
        if not _RECIPE_ID_RE.fullmatch(text):
            raise HelloFreshError(f"Invalid recipe id {text!r}")
        return text.split("-", 1)[0]

    async def async_get_recipe_detail(
        self, recipe_id: str, *, servings: int | None = None, include_favorite: bool = True
    ) -> HelloFreshRecipeDetail:
        """Return one recipe's full detail: steps, ingredients, utensils, allergens, nutrition.

        ``servings`` picks which ``yields`` entry the ingredient amounts are resolved against
        (2, 4, ...); it falls back to the smallest the recipe offers. Works for any recipe id —
        a meal on a delivery week or a row from the browse catalog — since both share the same
        24-hex id namespace.
        """
        bare = self._bare_recipe_id(recipe_id)
        params = {
            "country": api_country_code(self._country),
            "locale": self._locale_for_country(),
        }
        try:
            payload = await self._async_get_json(
                f"{self._RECIPE_DETAIL_PATH}/{_seg(bare)}", params, trace_category="recipe_attempts"
            )
        except HelloFreshError as err:
            if _refused_with(err, _HTTP_NOT_FOUND):
                raise HelloFreshError(
                    f"HelloFresh has no recipe with id {bare}", status=_HTTP_NOT_FOUND
                ) from err
            raise
        detail = HelloFreshRecipeDetail.from_api(
            payload, servings=servings, image_base=self._CATALOG_IMAGE_BASE
        )
        if detail is None:
            raise HelloFreshError(f"HelloFresh returned an unreadable recipe payload for {bare}")
        if include_favorite:
            # Best-effort: the heart is decoration on top of the recipe, so a cookbook outage
            # leaves it unknown rather than failing the whole lookup.
            found, answered = await self._async_search_favorites([bare])
            if answered:
                detail.is_favorite = bare in found
        return detail

    async def async_get_catalog_recipes(
        self,
        collection: str | None = None,
        *,
        limit: int = 50,
        include_favorites: bool = True,
    ) -> list[HelloFreshCatalogRecipe]:
        """Return recipes from the browse catalog, optionally within one collection.

        ``collection`` is a slug from :meth:`async_get_recipe_collections` (e.g.
        ``chicken-recipes``); omit it for the top-level "best rated" listing. When
        ``include_favorites`` is set, each recipe is flagged with its bookmark state in one
        extra batched request.
        """
        page, params = self._catalog_page_params(self._checked_collection(collection))
        payload = await self._async_get_catalog_document(page, params)
        recipes = self._parse_catalog_recipes(
            self._extract_catalog_rows(payload, collection=collection)
        )[:limit]
        await self._flag_catalog_favorites(recipes, include_favorites)
        return recipes

    @staticmethod
    def _checked_collection(collection: str | None) -> str | None:
        """Return a catalog category as given, refusing one that isn't a path of slugs.

        It becomes part of a website path, so only lowercase slugs joined by "/" pass.
        """
        if collection is None or collection == "":
            return None
        if not isinstance(collection, str) or not _COLLECTION_RE.fullmatch(collection):
            raise HelloFreshError(f"Invalid recipe collection {collection!r}")
        return collection

    @staticmethod
    def _catalog_page_params(collection: str | None) -> tuple[str, dict[str, str] | None]:
        """Return the (data-URL page, params) pair for a catalog listing."""
        if collection:
            return f"recipes/{collection}.json", {"main-collection": collection}
        return "recipes.json", None

    def _parse_catalog_recipes(self, rows: Iterable[Any]) -> list[HelloFreshCatalogRecipe]:
        """Parse catalog rows into recipes, in order, without duplicates or non-recipe rows."""
        recipes: list[HelloFreshCatalogRecipe] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            parsed = HelloFreshCatalogRecipe.from_api(row, image_base=self._CATALOG_IMAGE_BASE)
            if parsed is None or parsed.recipe_id in seen:
                continue
            seen.add(parsed.recipe_id)
            recipes.append(parsed)
        return recipes

    async def _flag_catalog_favorites(
        self, recipes: Sequence[HelloFreshCatalogRecipe], include_favorites: bool
    ) -> None:
        """Mark each recipe's bookmark state, in one batched request for the whole page."""
        if include_favorites and recipes:
            favorites = await self.async_get_favorite_recipe_ids([r.recipe_id for r in recipes])
            for recipe in recipes:
                recipe.is_favorite = recipe.recipe_id in favorites

    # Full-catalog text search on the recipes service — the SAME plain /gw API family as
    # recipe detail, so unlike the browse catalog it needs no build-id scraping and cannot
    # break on a website deploy. Verified live (2026-09): GET with country/locale/q/take
    # returns {items, count, skip, take, total}, each item a full recipe-detail-shaped row;
    # a non-matching query returns an empty items list.
    _RECIPE_SEARCH_PATH = "/gw/recipes/recipes/search"

    async def async_search_catalog_recipes(
        self,
        query: str,
        *,
        limit: int = 50,
        include_favorites: bool = True,
    ) -> list[HelloFreshCatalogRecipe]:
        """Search the whole public recipe catalog (~10k recipes) by free text.

        Unlike :meth:`async_get_catalog_recipes`, which lists one browse category, this
        spans every category at once — it is the API behind the website's own recipe
        search. When ``include_favorites`` is set, each result is flagged with its bookmark
        state in one extra batched request, exactly as the category listings are.
        """
        page = await self.async_search_catalog_page(
            query, limit=limit, include_favorites=include_favorites
        )
        return page["recipes"]

    async def async_search_catalog_page(
        self,
        query: str,
        *,
        limit: int = 50,
        offset: int = 0,
        include_favorites: bool = True,
    ) -> dict[str, Any]:
        """Return one page of a whole-catalog text search, and where it stands in the results.

        The search answers ``{items, count, skip, take, total}`` and is paged like HelloFresh's
        other collections: ``take`` results after the first ``skip``. ``skip`` is sent only
        from the second page on, so the first page is the request the website makes. Returns
        ``{recipes, offset, total, has_more}``; ``total`` is the number of matches HelloFresh
        reports (None when it reports none) and ``has_more`` whether results follow this page.
        """
        offset = max(0, int(offset or 0))
        text = str(query or "").strip()
        if not text:
            return {"recipes": [], "offset": offset, "total": 0, "has_more": False}
        take = max(1, min(int(limit or 50), 250))
        params = {
            "country": api_country_code(self._country),
            "locale": self._locale_for_country(),
            "q": text,
            "take": str(take),
        }
        if offset:
            params["skip"] = str(offset)
        payload = await self._async_get_json(
            self._RECIPE_SEARCH_PATH, params, trace_category="catalog_attempts"
        )
        rows = payload.get("items")
        rows = rows if isinstance(rows, list) else []
        recipes = self._parse_catalog_recipes(rows)[:limit]
        await self._flag_catalog_favorites(recipes, include_favorites)
        total = coerce_int(payload.get("total"))
        if total is not None and total < 0:
            total = None
        # Counted in rows, not recipes: a duplicate or unreadable row still takes its place
        # in HelloFresh's paging. A page that came back empty ends the results whatever the
        # total says, so a caller can never page forever.
        if not rows:
            has_more = False
        elif total is not None:
            has_more = offset + len(rows) < total
        else:
            has_more = len(rows) >= take
        return {"recipes": recipes, "offset": offset, "total": total, "has_more": has_more}

    async def async_get_catalog_page(
        self,
        collection: str | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
        include_favorites: bool = True,
    ) -> dict[str, Any]:
        """Return one catalog page: its recipes plus any sub-categories it offers.

        Categories can nest — Noodle Recipes has Ramen / Udon / Rice / Soba / Yakisoba, Chicken
        Recipes has eight — and those children are **not** in the top-level category list, so
        without this they are unreachable from the card. They are returned alongside the
        recipes because the page already carries them: surfacing them costs no extra request.

        The recipes are ``limit`` of the category's list from ``offset`` on. The whole list is
        one document (kept for an hour, see ``_async_get_catalog_document``), so paging through
        it costs no request after the first; ``total`` is its length (None when the document
        could not be read) and ``has_more`` whether recipes follow this page.
        """
        offset = max(0, int(offset or 0))
        page, params = self._catalog_page_params(self._checked_collection(collection))
        payload = await self._async_get_catalog_document(page, params)
        listing = self._parse_catalog_recipes(
            self._extract_catalog_rows(payload, collection=collection)
        )
        recipes = listing[offset : offset + limit]
        await self._flag_catalog_favorites(recipes, include_favorites)
        return {
            "collection": collection,
            "recipes": recipes,
            # Only a real category has sub-categories to refine by. The ROOT listing
            # ("Top rated", no collection) reads recipes.json, whose
            # `collection.children.docs` are the top-level categories themselves — the exact
            # rows `async_get_recipe_collections` returns — so reporting them here made the
            # card draw a "Refine" row that duplicated its own main filter list (pick
            # "Top rated" and "Chinese Recipes" appeared in both).
            "subcollections": self._extract_subcollections(payload) if collection else [],
            "offset": offset,
            "total": len(listing) if payload is not None else None,
            "has_more": offset + len(recipes) < len(listing),
        }

    def _extract_subcollections(self, payload: Any) -> list[HelloFreshRecipeCollection]:
        """Return the child categories of the collection this page represents."""
        docs = self._nested_get(
            payload, "pageProps", "ssrPayload", "collection", "children", "docs"
        )
        out: list[HelloFreshRecipeCollection] = []
        for row in docs if isinstance(docs, list) else []:
            parsed = HelloFreshRecipeCollection.from_api(row, image_base=self._CATALOG_IMAGE_BASE)
            if parsed is not None:
                out.append(parsed)
        return out

    @staticmethod
    def _extract_catalog_rows(
        payload: Any, *, collection: str | None = None
    ) -> list[dict[str, Any]]:
        """Pull recipe rows out of a catalog page's react-query cache.

        The listing lives in ``dehydratedState.queries`` under a ``foodContentHubRecipe.*`` key,
        whose data is ``{"pages": [...]}`` — despite the name, ``pages`` is the flat recipe list,
        not pagination.

        **A category page carries several such queries, and only some belong to the category.**
        A page for e.g. ``flatbread-recipes`` holds:

        * one or more queries whose ``where`` clause has **``recipeCollectionTags.id``** — these
          are the category's own recipes (the actual flatbreads);
        * a generic "best rated overall" query, distinguished by having **no** collection tag
          (it filters on ``ratingsCount >= 100`` instead), which is the SAME top-20 list on
          every category page.

        Concatenating all of them let the generic list dominate — it is the largest and comes
        last — so every category rendered the same top-rated recipes and "Indian Recipes"
        showed no Indian food. When a collection is requested, the untagged query is therefore
        skipped; without one (the top-level listing) it is exactly what we want.
        """
        rows: list[dict[str, Any]] = []
        ssr = CatalogClientMixin._nested_get(payload, "pageProps", "ssrPayload")
        if not isinstance(ssr, dict):
            return rows

        # `collection.recipes` is the category's OWN canonical listing and by far the richest
        # source — 30 rows for Noodle where the query cache holds 10, 17 vs 8 for Indian. The
        # react-query entries below are the page's small "Quick & Easy" / "Most Recent" rails,
        # which overlap it but do contribute a few extras, so both are merged (deduped by
        # recipe id in async_get_catalog_recipes). Reading only the rails is what made a
        # category show ~10 recipes when the website listed 30.
        collection_rows = CatalogClientMixin._nested_get(ssr, "collection", "recipes")
        if isinstance(collection_rows, list):
            rows.extend(row for row in collection_rows if isinstance(row, dict))

        queries = CatalogClientMixin._nested_get(ssr, "dehydratedState", "queries")
        for query in queries if isinstance(queries, list) else []:
            if not isinstance(query, dict):
                continue
            key = query.get("queryKey")
            # Match on the query NAME only. Testing the serialized key was subtly wrong: the
            # category queries carry `recipeCollectionTags` inside their `where` clause, so a
            # blanket "Collection" test threw away the very rows we want and left only the
            # generic list — which is what made every category show the same recipes.
            name = key[0] if isinstance(key, list) and key else key
            if not isinstance(name, str) or not name.startswith("foodContentHubRecipe."):
                continue
            if name.startswith("foodContentHubRecipeCollection"):
                # Collection *metadata* (the category's own title/description), not recipes.
                continue
            if collection and not CatalogClientMixin._query_is_collection_scoped(key):
                # Generic cross-site listing on a category page — not this category's recipes.
                continue
            data = CatalogClientMixin._nested_get(query, "state", "data")
            if not isinstance(data, dict):
                continue
            for candidate in (data.get("pages"), data.get("docs")):
                if isinstance(candidate, list):
                    rows.extend(row for row in candidate if isinstance(row, dict))

        if not rows and not collection:
            # Only meaningful for the top-level listing: `bestRatedRecipes` IS the generic list,
            # so falling back to it for a category would reintroduce the wrong-recipes bug.
            fallback = ssr.get("bestRatedRecipes")
            pages = fallback.get("pages") if isinstance(fallback, dict) else None
            if isinstance(pages, list):
                rows.extend(row for row in pages if isinstance(row, dict))
        return rows

    @staticmethod
    def _query_is_collection_scoped(query_key: Any) -> bool:
        """Return True when a react-query key filters on a recipe-collection tag.

        The key is ``[name, {"searchParams": {"where": {...}}}]``; a category-scoped query
        constrains ``recipeCollectionTags.id``. Matched textually against the ``where`` clause
        so a nested/renamed operator (``equals``, ``in``, …) still counts.
        """
        if not isinstance(query_key, list) or len(query_key) < 2:
            return False
        params = query_key[1]
        if not isinstance(params, dict):
            return False
        where = CatalogClientMixin._nested_get(params, "searchParams", "where")
        if not isinstance(where, dict):
            return False
        return "recipeCollectionTags" in json.dumps(where, default=str)

    @staticmethod
    def _nested_get(node: Any, *keys: str) -> Any:
        """Walk a chain of dict keys, returning None if any level is missing or not a dict."""
        current = node
        for key in keys:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current
