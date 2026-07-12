"""HelloFreshClient — HTTP orchestration for the HelloFresh integration."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
import hashlib
import json
import logging
import re
from typing import Any

from aiohttp import ClientError, ClientResponse, ClientSession
from bs4 import BeautifulSoup

from .const import COUNTRY_BASE_URLS, DEFAULT_COUNTRY, api_country_code, api_locale
from .models import (
    HelloFreshAccountData,
    HelloFreshAuthError,
    HelloFreshDeliveryOption,
    HelloFreshError,
    HelloFreshFoodProfile,
    HelloFreshFoodProfileOptions,
    HelloFreshMarketItem,
    HelloFreshNotImplementedError,
    HelloFreshOrder,
    HelloFreshRecipe,
    HelloFreshSubscription,
    HelloFreshWeek,
)
from .normalizers import HelloFreshPayloadNormalizer
from .parsers import (
    coerce_float,
    coerce_int,
    extract_menu_labels,
    extract_scm_tracking_details,
    extract_tracking_public_id,
    looks_like_recipe_heading,
    parse_date,
    parse_datetime,
    slugify,
)
from .tls_transport import AuthResponse, async_request
from .token_manager import (
    _BROWSER_CLIENT_HINTS,
    _BROWSER_FETCH_HEADERS,
    _BROWSER_USER_AGENT,
    _TOKEN_MIN_REMAINING_BEFORE_REFRESH,  # noqa: F401 - re-exported for back-compat imports
    _TOKEN_REFRESH_AT_LIFETIME_FRACTION,  # noqa: F401 - re-exported for back-compat imports
    TokenManager,
    _looks_like_bot_block,  # noqa: F401 - re-exported for back-compat imports
    _response_content_type,  # noqa: F401 - re-exported for back-compat imports
    _token_fingerprint,  # noqa: F401 - re-exported (used by __init__.py and tests)
)

_LOGGER = logging.getLogger(__name__)

# HTTP status thresholds used when interpreting HelloFresh responses.
_HTTP_BAD_REQUEST = 400
_AUTH_FAILURE_STATUSES = frozenset({401, 403})

# Upper bound on the cart-price response cache. Only a handful of weeks are priced at once,
# but the cache key includes the (changing) week id and meal selection, so without a cap it
# would grow slowly forever over the lifetime of a long-running client. FIFO-evicted.
_CART_PRICE_CACHE_MAX = 32

_DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": _BROWSER_USER_AGENT,
    "Priority": "u=1, i",
    **_BROWSER_FETCH_HEADERS,
    **_BROWSER_CLIENT_HINTS,
}

# Feature/versioning headers the logged-in web app sends on its authenticated account and
# menu XHRs (observed in HAR captures). They pin the API/categorization variant the server
# replies with, so sending them makes the integration's traffic match the browser's and
# guards against payload-shape drift from an un-negotiated default. They are merged into
# every authenticated request; per-endpoint ``extra_headers`` (e.g. ``x-requested-by``)
# still override and add to them.
_FEATURE_HEADERS = {
    "X-Market-API-Version": "2",
    "X-Food-Categorization": "v1",
    "x-sort-variations-by-quantity": "true",
}


class HelloFreshClient(HelloFreshPayloadNormalizer):
    """Client wrapper for HelloFresh account access."""

    def __init__(
        self,
        session: ClientSession,
        country: str = DEFAULT_COUNTRY,
        access_token: str | None = None,
        refresh_token: str | None = None,
        token_issued_at: int | str | None = None,
        token_expires_in: int | str | None = None,
        refresh_expires_in: int | str | None = None,
        refresh_token_issued_at: int | str | None = None,
        token_type: str | None = None,
        username: str | None = None,
        password: str | None = None,
        enable_public_menu_fallback: bool = True,
        history_weeks: int | None = None,
        menu_grace_weeks: int | None = None,
        token_refresh_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._country = country
        # How many weeks of past history to fetch/browse. None -> the normalizer default.
        self._history_lookback_weeks = history_weeks
        # How many weeks a just-delivered week keeps its full menu. None -> normalizer default.
        self._menu_grace_weeks_option = menu_grace_weeks
        self._base_url = COUNTRY_BASE_URLS.get(country, COUNTRY_BASE_URLS[DEFAULT_COUNTRY])
        # All access/refresh token state and the /gw login/refresh calls live in the
        # TokenManager (composition, not inheritance). The client reads the current token /
        # auth header from it per request and asks it to refresh proactively and reactively.
        self._tokens = TokenManager(
            session=session,
            country=country,
            access_token=access_token,
            refresh_token=refresh_token,
            token_issued_at=token_issued_at,
            token_expires_in=token_expires_in,
            refresh_expires_in=refresh_expires_in,
            refresh_token_issued_at=refresh_token_issued_at,
            token_type=token_type,
            username=username,
            password=password,
            token_refresh_callback=token_refresh_callback,
        )
        self._cached_subscriptions: list[HelloFreshSubscription] | None = None
        self._subscription_preferences: dict[str, str | None] = {}
        self._enable_public_menu_fallback = enable_public_menu_fallback
        self._last_account_data: HelloFreshAccountData | None = None
        self._debug_trace: dict[str, list[dict[str, Any]]] = {}
        # Remembers which probed endpoint last produced a usable payload, keyed by
        # (category, subscription_id). The integration was built to probe a list of
        # candidate endpoints in order because the HAR captures didn't confirm which one
        # works per account; once one succeeds, recording it lets the next poll try the
        # winner first (and skip the doomed 404/403 probes that precede it). This persists
        # ACROSS polls — it is intentionally not cleared by the per-poll debug-trace reset.
        self._preferred_endpoints: dict[tuple[str, str], str] = {}
        # Caches the last cart-pricing response per identical request body. The exact box
        # total only changes when the priced cart changes (selection, week, address, box
        # size) — all of which are part of the request payload — so an unchanged payload
        # always yields the same total. Keyed by a hash of (path, params, json_payload),
        # this skips re-POSTing the same pricing request on every poll. Persists across polls;
        # FIFO-bounded to _CART_PRICE_CACHE_MAX so it can't grow without limit over time.
        self._cart_price_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def _order_candidates_by_preference(
        self,
        category: str,
        subscription_id: str | None,
        candidates: Sequence[tuple[str, dict[str, Any] | None]],
    ) -> list[tuple[str, dict[str, Any] | None]]:
        """Return ``candidates`` with the last-successful one moved to the front.

        Identity is the candidate's ``(path, sorted-param-keys)`` so a remembered winner is
        matched even though param *values* (week ids, ranges) differ between polls. Falling
        back to the original order keeps the full probe list available if the site drifts.
        """
        preferred_key = self._preferred_endpoints.get((category, subscription_id or ""))
        if preferred_key is None:
            return list(candidates)
        preferred = [c for c in candidates if self._candidate_key(c) == preferred_key]
        if not preferred:
            return list(candidates)
        rest = [c for c in candidates if self._candidate_key(c) != preferred_key]
        return preferred + rest

    @staticmethod
    def _candidate_key(candidate: tuple[str, dict[str, Any] | None]) -> str:
        """Return a stable identity for a probe candidate, ignoring param values."""
        path, params = candidate
        param_keys = ",".join(sorted(params)) if params else ""
        return f"{path}?{param_keys}"

    def _remember_preferred_endpoint(
        self,
        category: str,
        subscription_id: str | None,
        candidate: tuple[str, dict[str, Any] | None],
    ) -> None:
        """Record the candidate that just produced a usable payload."""
        self._preferred_endpoints[(category, subscription_id or "")] = self._candidate_key(
            candidate
        )

    async def async_validate_credentials(self) -> dict[str, Any]:
        """Validate configured bearer token against a real account endpoint."""
        subscription = await self._async_get_primary_subscription()
        return subscription.as_dict()

    async def async_ensure_token_fresh(self) -> None:
        """Proactively refresh the access token when it is near expiry.

        Safe to call on a timer independent of data polling. Does nothing when no
        refresh token is configured or the current token still has comfortable life.
        """
        await self._tokens.async_ensure_fresh()

    @property
    def token_lifetime_seconds(self) -> int | None:
        """Return the access token's configured lifetime in seconds, if known."""
        return self._tokens.token_lifetime_seconds

    @property
    def token_expires_at(self) -> datetime | None:
        """Return the access token's UTC expiry time, if known."""
        return self._tokens.token_expires_at

    @property
    def refresh_token_expires_at(self) -> datetime | None:
        """Return the refresh token's UTC expiry time, if known."""
        return self._tokens.refresh_token_expires_at

    async def async_get_account_data(self) -> HelloFreshAccountData:
        """Fetch account orders, weeks, and menu data."""
        data = HelloFreshAccountData()
        self._reset_debug_trace()
        self._cached_subscriptions = None
        self._subscription_preferences = {}
        public_menu_loaded = False

        try:
            subscriptions = await self._async_get_subscriptions()
        except HelloFreshAuthError:
            raise
        except HelloFreshError as err:
            _LOGGER.warning(
                "HelloFresh account data unavailable, keeping menu fallbacks only: %s", err
            )
            subscriptions = []

        if subscriptions:
            public_menu_loaded = await self._async_load_account_data(data, subscriptions)
        elif self._enable_public_menu_fallback:
            public_menu_loaded = await self._async_load_public_menu_fallback(data)

        self._finalize_capabilities(data, public_menu_loaded)

        data.debug_trace = self._debug_trace
        self._last_account_data = data.finalize()
        self._warn_partial_data(self._last_account_data)
        return self._last_account_data

    async def _async_load_account_data(
        self,
        data: HelloFreshAccountData,
        subscriptions: Sequence[HelloFreshSubscription],
    ) -> bool:
        """Populate ``data`` from authenticated account payloads. Returns public-menu-loaded."""
        primary = subscriptions[0]
        data.account_id = primary.account_id
        data.subscription_id = primary.subscription_id
        data.locale = primary.locale
        data.account_data_available = True
        data.subscriptions = subscriptions
        (
            data.boxes_received,
            data.past_delivery_weeks,
            all_weeks,
            all_orders,
            account_payload_found,
        ) = await self._async_get_initial_account_payloads(subscriptions)

        await self._async_enrich_subscription_payment_dates(subscriptions, all_orders, data)
        await self._async_enrich_account_credit(data, subscriptions)
        await self._async_enrich_selected_plan_price(data, subscriptions)

        all_weeks = self._backfill_account_weeks_from_subscriptions(
            subscriptions=subscriptions,
            weeks=all_weeks,
        )

        public_menu_loaded = False
        menu_result = await self._async_get_account_menu_data(subscriptions, all_weeks)
        if menu_result is not None:
            data.public_menu_weeks = menu_result["weeks"]
            data.available_menu_labels = menu_result["available_labels"]
            data.capabilities.supports_account_menu_api = True
            all_weeks = self._merge_menu_weeks_into_account_weeks(
                account_weeks=all_weeks,
                menu_weeks=menu_result["weeks"],
            )
            await self._async_enrich_order_prices(
                subscriptions=subscriptions,
                weeks=all_weeks,
                orders=all_orders,
            )
        elif self._enable_public_menu_fallback:
            public_menu_loaded = await self._async_load_public_menu_fallback(data)

        # Past delivery weeks carry the meals that were ACTUALLY delivered (from the
        # past-deliveries endpoint, keyed by week). The planning-menu endpoint above does not
        # serve real history — for a past week it returns nothing usable or a substitute menu
        # we reject — so the delivered recipes are the authoritative source for those weeks.
        all_weeks = self._merge_past_delivery_recipes_into_account_weeks(
            account_weeks=all_weeks,
            past_delivery_weeks=data.past_delivery_weeks,
        )

        await self._async_enrich_order_tracking(
            subscriptions=subscriptions,
            orders=all_orders,
        )

        # Universal pass: resolve each recipe's variant modifier ("2x Bacon", ...) from its
        # week's modularity block, regardless of which path built the recipes. Done here, after
        # the merge, so every assembled week is covered (not just the menu-API path).
        self._apply_variation_titles(all_weeks)
        # Likewise attach each week's Market add-on catalog (appetizers, sides, desserts, ...).
        self._apply_market_items(all_weeks)
        # Universal pass: a PAUSED week never shipped. Any "selected" meals on it are just the
        # system's auto-fill placeholders, so clear the selection (the catalog still shows for
        # browsing). Done last so it overrides whatever any merge path marked.
        self._clear_paused_week_selection(all_weeks)

        data.weeks = all_weeks
        data.orders = all_orders

        self._reconcile_menu_fallback_with_recipes(data)

        # Only flag a changed payload shape when we genuinely ended up with NO usable
        # account weeks. The primary deliveries probe returning nothing is not enough on
        # its own: weeks are also recovered from the subscription backfill and the
        # authenticated menu API, and when either yields weeks the data is fine. Flagging
        # on `account_payload_found` alone produced a false "data shape changed" banner on
        # accounts whose deliveries endpoint returns only past weeks but whose upcoming
        # week is filled in from the subscription/menu payloads.
        usable_account_weeks = any(week.source != "public_menu" for week in all_weeks)
        if not account_payload_found and not usable_account_weeks and data.public_menu_weeks:
            _LOGGER.info(
                "HelloFresh menu data loaded, but no verified upcoming-deliveries payload was "
                "available for this account token."
            )
            data.capabilities.payload_shape_changed = True
            data.capabilities.notes.append(
                "Authenticated account payloads were reachable, but no recognizable delivery list was found."
            )

        return public_menu_loaded

    async def _async_load_public_menu_fallback(self, data: HelloFreshAccountData) -> bool:
        """Load public-menu data into ``data``. Returns True when it succeeded."""
        try:
            public_menu = await self._async_get_public_menu_data()
        except HelloFreshError as err:
            _LOGGER.warning("HelloFresh public menu unavailable: %s", err)
            return False
        data.public_menu_weeks = public_menu["weeks"]
        data.available_menu_labels = public_menu["available_labels"]
        data.capabilities.using_public_menu_fallback = True
        return True

    @staticmethod
    def _reconcile_menu_fallback_with_recipes(data: HelloFreshAccountData) -> None:
        """Drop the public-menu fallback flag when authenticated weeks already have recipes."""
        authenticated_recipe_weeks = [week for week in data.weeks if week.recipes]
        if not (authenticated_recipe_weeks and data.capabilities.using_public_menu_fallback):
            return
        data.capabilities.using_public_menu_fallback = False
        data.capabilities.notes.append(
            "Authenticated delivery payloads already include structured recipe data, "
            "so the public menu fallback is not required for normal account use."
        )
        for week in authenticated_recipe_weeks:
            if week.display_name and week.display_name not in data.available_menu_labels:
                data.available_menu_labels.append(week.display_name)

    @staticmethod
    def _finalize_capabilities(data: HelloFreshAccountData, public_menu_loaded: bool) -> None:
        """Derive write-capability flags from the resolved weeks (or note the fallback)."""
        if data.weeks:
            weeks = data.weeks
            caps = data.capabilities
            caps.supports_meal_selection = any(
                week.meals_required and len(week.recipes) >= week.meals_required for week in weeks
            )
            caps.supports_update_delivery_address = any(
                week.allowed_actions.get("updateDeliveryAddress", False) for week in weeks
            )
            caps.supports_update_delivery_weekday = any(
                week.allowed_actions.get("updateDeliveryWeekday", False) for week in weeks
            )
            caps.supports_pause = any(week.allowed_actions.get("pause", False) for week in weeks)
            caps.supports_one_off_change = any(
                week.allowed_actions.get("oneOffChange", False) for week in weeks
            )
            caps.supports_update_payment_method = any(
                week.allowed_actions.get("updatePaymentMethod", False) for week in weeks
            )
            caps.supports_donation = any(
                week.allowed_actions.get("donate", False) for week in weeks
            )
        elif public_menu_loaded:
            data.capabilities.notes.append(
                "The integration is currently relying on public menu data because no account weeks were available."
            )

    async def _async_get_initial_account_payloads(
        self,
        subscriptions: Sequence[HelloFreshSubscription],
    ) -> tuple[
        int | None,
        list[HelloFreshWeek],
        list[HelloFreshWeek],
        list[HelloFreshOrder],
        bool,
    ]:
        """Fetch independent account payloads concurrently after subscriptions load."""
        delivery_task = asyncio.gather(
            *(self._async_get_upcoming_deliveries(subscription) for subscription in subscriptions)
        )
        boxes_received, past_delivery_weeks, delivery_results = await asyncio.gather(
            self._async_get_boxes_received(),
            self._async_get_past_delivery_weeks(subscriptions),
            delivery_task,
        )

        all_weeks: list[HelloFreshWeek] = []
        all_orders: list[HelloFreshOrder] = []
        account_payload_found = False
        for weeks, orders in delivery_results:
            if weeks:
                account_payload_found = True
            all_weeks.extend(weeks)
            all_orders.extend(orders)

        return (
            boxes_received,
            past_delivery_weeks,
            all_weeks,
            all_orders,
            account_payload_found,
        )

    async def async_select_meals(
        self,
        week_id: str,
        recipe_ids: list[str],
        quantities: dict[str, int] | None = None,
    ) -> bool:
        """Submit meal choices for a week.

        ``recipe_ids`` is the set of chosen recipes. ``quantities`` optionally maps a recipe id
        to how many servings of that meal to order (matching HelloFresh's per-recipe quantity
        stepper); recipes absent from the map default to a quantity of 1. The included-meal
        minimum is checked against TOTAL servings (sum of quantities), mirroring HelloFresh's
        box math where a meal at quantity 2 fills two of the box's slots.

        Returns ``True`` when HelloFresh applied a **seamless downgrade** — it accepted the
        write but silently shrank the box to fit (the cart response's ``hasSeamlessDowngraded``
        flag), so the saved selection is smaller than requested. Returns ``False`` otherwise
        (including when the selection was already up to date or a fallback path was used).
        """
        week = self._get_known_week_or_raise(week_id)
        deduplicated_recipe_ids = list(dict.fromkeys(recipe_ids))
        if not deduplicated_recipe_ids:
            raise HelloFreshError("At least one recipe_id is required")
        quantities = quantities or {}
        for recipe_id, quantity in quantities.items():
            if not isinstance(quantity, int) or quantity < 1:
                raise HelloFreshError(
                    f"Quantity for recipe {recipe_id} must be a positive integer, got {quantity!r}"
                )

        def _qty(recipe_id: str) -> int:
            return quantities.get(recipe_id, 1)

        # A week can be resized to a different box for that delivery: picking more or fewer
        # DISTINCT meals than the base plan swaps the box SKU up or down (see
        # _sku_for_meal_count), and HelloFresh reprices it. The only floor is the smallest box
        # HelloFresh sells — MIN_MEALS_PER_WEEK distinct meals — below which no valid box exists.
        # The count that resizes the box is distinct meals, NOT total servings (a meal at
        # quantity 2 fills extra servings of the same box slot, it doesn't add a meal).
        distinct_meals = len(deduplicated_recipe_ids)
        if distinct_meals < self.MIN_MEALS_PER_WEEK:
            raise HelloFreshError(
                f"Week {week_id} needs at least {self.MIN_MEALS_PER_WEEK} meals, "
                f"got {distinct_meals}"
            )

        selected = {
            recipe.recipe_id: self._existing_recipe_quantity(recipe)
            for recipe in week.recipes
            if recipe.is_selected
        }
        requested = {recipe_id: _qty(recipe_id) for recipe_id in deduplicated_recipe_ids}
        if selected and selected == requested:
            _LOGGER.info("HelloFresh meal selection for week %s is already up to date", week_id)
            return False

        if week.subscription_id is None:
            raise HelloFreshNotImplementedError(
                f"Week {week_id} does not expose a subscription id, so meal selection cannot be submitted safely."
            )

        subscription = await self._async_get_subscription_for_week(week)
        cart_update = await self._async_build_cart_selection_update(
            subscription=subscription,
            week=week,
            recipe_ids=deduplicated_recipe_ids,
            quantities=requested,
        )
        if cart_update is not None:
            response = await self._async_api_request(
                "PUT",
                cart_update["path"],
                params=cart_update["params"],
                json_payload=cart_update["json_payload"],
                extra_headers={"x-requested-by": "shopping-experience-web"},
            )
            _LOGGER.info("HelloFresh meal selection succeeded using %s", cart_update["path"])
            return await self._cart_response_downgraded(response, week_id)

        payload_variants = [
            {"weekId": week_id, "recipes": deduplicated_recipe_ids},
            {"week": week_id, "recipeIds": deduplicated_recipe_ids},
            {
                "subscriptionId": week.subscription_id,
                "weekId": week_id,
                "selectedRecipeIds": deduplicated_recipe_ids,
            },
        ]
        path_templates = [
            "/gw/my-menu/weeks/{week_id}/selection",
            "/gw/my-menu/weeks/{week_id}/recipes",
            "/gw/my-menu/{week_id}/selection",
            "/gw/api/customers/me/subscriptions/{subscription_id}/weeks/{week_id}/selection",
        ]
        await self._async_try_mutation_candidates(
            path_templates, week, payload_variants, category="select"
        )
        # Fallback paths don't return the cart's downgrade flag; assume no downgrade.
        return False

    async def _cart_response_downgraded(
        self,
        response: ClientResponse | AuthResponse,
        week_id: str,
    ) -> bool:
        """Return True when a cart write response signals a seamless (silent) box downgrade.

        The ``PUT /gw/v1/carts/{week}`` response is ``{"hasSeamlessDowngraded": bool}``: when
        ``true``, HelloFresh accepted the write but shrank the box to fit rather than rejecting
        it, so the saved selection is smaller than requested. Best-effort — an unreadable or
        unexpected body is treated as no downgrade rather than raising on a successful write.
        """
        json_method = getattr(response, "json", None)
        if not callable(json_method):
            # A transport (or test double) that doesn't expose a readable JSON body — a
            # successful write shouldn't be treated as a downgrade just because we can't read it.
            return False
        try:
            payload = await self._async_response_json(response)
        except HelloFreshError:
            return False
        downgraded = bool(isinstance(payload, dict) and payload.get("hasSeamlessDowngraded"))
        if downgraded:
            _LOGGER.warning(
                "HelloFresh seamlessly downgraded the box for week %s: the saved selection is "
                "smaller than requested",
                week_id,
            )
        return downgraded

    async def async_select_market_items(
        self,
        week_id: str,
        quantities: dict[str, int],
    ) -> bool:
        """Set the week's HelloFresh Market add-on (extras) selection.

        ``quantities`` maps a market item's id/sku/index (as returned by get_weeks) to the
        desired quantity; a quantity of 0 (or omitting an item) removes it. The week's meal
        selection is preserved. Writes the cart's ``extras`` array via PUT /gw/v1/carts/{week}.

        Returns ``True`` if the cart response signals a seamless box downgrade (see
        :meth:`async_select_meals`); market writes preserve the meal count, so this is rare.
        """
        week = self._get_known_week_or_raise(week_id)
        if not week.market_items:
            raise HelloFreshError(f"Week {week_id} has no market catalog to select from")
        if week.subscription_id is None:
            raise HelloFreshNotImplementedError(
                f"Week {week_id} does not expose a subscription id, so market selection cannot be submitted safely."
            )

        # Resolve requested quantities against the catalog by id/sku/index, clamped to maxQuantity.
        by_key: dict[str, HelloFreshMarketItem] = {}
        for item in week.market_items:
            for key in (item.item_id, item.sku, str(item.index)):
                if key:
                    by_key.setdefault(str(key), item)

        # Each requested quantity is the desired TOTAL servings. HelloFresh splits a selection
        # into a recurring (preselected) part and a this-week (one-off) part; we keep any existing
        # recurring portion and apply the rest as one-off, matching how the web app writes changes.
        resolved: list[tuple[HelloFreshMarketItem, int, int]] = []
        for raw_key, raw_qty in quantities.items():
            quantity = coerce_int(raw_qty) or 0
            if quantity <= 0:
                continue
            item = by_key.get(str(raw_key))
            if item is None or item.index is None:
                raise HelloFreshError(
                    f"Market item {raw_key!r} is not in week {week_id}'s catalog"
                )
            if item.max_quantity is not None and quantity > item.max_quantity:
                raise HelloFreshError(
                    f"Market item {item.name!r} allows at most {item.max_quantity}, got {quantity}"
                )
            preselected = min(item.preselected_quantity or 0, quantity)
            one_off = quantity - preselected
            resolved.append((item, one_off, preselected))

        extras = self._build_cart_extras(resolved)
        subscription = await self._async_get_subscription_for_week(week)
        meals = self._build_cart_existing_meals(week)  # preserve the meal selection
        cart_update = await self._async_build_cart_update(week, subscription, meals, extras)
        if cart_update is None:
            raise HelloFreshNotImplementedError(
                f"Week {week_id} is missing the cart metadata needed to submit market selections."
            )
        response = await self._async_api_request(
            "PUT",
            cart_update["path"],
            params=cart_update["params"],
            json_payload=cart_update["json_payload"],
            extra_headers={"x-requested-by": "shopping-experience-web"},
        )
        _LOGGER.info("HelloFresh market selection succeeded using %s", cart_update["path"])
        return await self._cart_response_downgraded(response, week_id)

    # ---- Food profile (auto-preselection preferences) ------------------------
    #
    # HelloFresh drives its automatic meal pre-selection from a per-customer "food profile"
    # (taste exclusions, dietary preference, liked/disliked cuisines/proteins/etc., household
    # size, goals). The web app reads it from
    #   GET  /gw/profile-service/v2/customers/me/profile
    # the full catalog of choices from
    #   GET  /gw/profile-service/v2/profile/options
    # and saves changes with
    #   PATCH /gw/profile-service/v2/customers/me/profile   (HAR-verified, US).
    # All three take the brand/exclusion/regionCode query params the web app sends.

    _PROFILE_OPTIONS_PATH = "/gw/profile-service/v2/profile/options"
    _PROFILE_PATH = "/gw/profile-service/v2/customers/me/profile"

    def _profile_params(self, *, source: str | None = None) -> dict[str, str]:
        params = {
            "brand": "BRAND_HELLOFRESH",
            "exclusion": "v2",
            "regionCode": api_country_code(self._country),
        }
        if source:
            params["source"] = source
        return params

    async def async_get_food_profile_options(self) -> HelloFreshFoodProfileOptions:
        """Return the full catalog of selectable food-profile options."""
        response = await self._async_api_get(
            self._PROFILE_OPTIONS_PATH, params=self._profile_params()
        )
        if response.status >= 400:
            raise HelloFreshError(
                f"HelloFresh food-profile options request failed (HTTP {response.status})"
            )
        payload = await self._async_response_json(response)
        return HelloFreshFoodProfileOptions.from_api(payload)

    async def async_get_food_profile(self) -> HelloFreshFoodProfile:
        """Return the customer's current food profile (auto-preselection preferences)."""
        response = await self._async_api_get(
            self._PROFILE_PATH, params=self._profile_params()
        )
        if response.status >= 400:
            raise HelloFreshError(
                f"HelloFresh food-profile request failed (HTTP {response.status})"
            )
        payload = await self._async_response_json(response)
        return HelloFreshFoodProfile.from_api(payload)

    async def async_update_food_profile(
        self, changes: dict[str, Any]
    ) -> HelloFreshFoodProfile:
        """Update the customer's food profile and return the saved result.

        ``changes`` is a partial ``{taste, household, goals}`` mapping (only the sections you
        want to change). Weighted taste fields may be given as a list of liked slugs or a
        ``{slug: weight}`` map; both are normalized to the canonical +/-100 form.
        """
        patch = HelloFreshFoodProfile.build_patch(changes)
        if not patch:
            raise HelloFreshError("No food-profile changes were provided")
        response = await self._async_api_request(
            "PATCH",
            self._PROFILE_PATH,
            params=self._profile_params(source="food-profile"),
            json_payload=patch,
        )
        if response.status >= 400:
            details = await response.text()
            raise HelloFreshError(
                f"HelloFresh food-profile update failed (HTTP {response.status}): {details[:300]}"
            )
        payload = await self._async_response_json(response)
        _LOGGER.info("HelloFresh food-profile update succeeded")
        return HelloFreshFoodProfile.from_api(payload)

    # ---- Reference catalogs (read-only) --------------------------------------
    #
    # These back optional read-only services. None are needed for the core poll; they expose
    # richer detail the web app fetches on demand.

    async def async_get_delivery_options(
        self,
        subscription: HelloFreshSubscription | None = None,
        num_deliveries: int = 7,
    ) -> list[HelloFreshDeliveryOption]:
        """Return the plan's selectable delivery days (``/gw/api/delivery_dates_options``).

        Richer than a week's ``availableOneOffOptions``: each option carries the weekday name,
        number, price, and default flag — enough for a full delivery-day picker. The plan
        ``family`` and delivery ``zip`` are read from the (primary) subscription payload.
        """
        if subscription is None:
            subscriptions = self._cached_subscriptions or await self._async_get_subscriptions()
            subscription = subscriptions[0] if subscriptions else None
        if subscription is None:
            return []

        family = self._find_first_nested_value(
            subscription.raw.get("productType"), ("family",)
        )
        family_handle = (
            family.get("handle") if isinstance(family, dict) else None
        ) or self._find_first_nested_value(subscription.raw, ("family",))
        postcode = self._find_first_nested_value(
            subscription.raw.get("shippingAddress"), ("postcode",)
        )
        if not family_handle or not postcode:
            return []

        params = {
            "country": api_country_code(self._country),
            "locale": subscription.locale or self._locale_for_country(),
            "family": str(family_handle),
            "numDeliveries": str(num_deliveries),
            "zip": str(postcode),
        }
        response = await self._async_api_get("/gw/api/delivery_dates_options", params=params)
        if response.status >= _HTTP_BAD_REQUEST:
            raise HelloFreshError(
                f"HelloFresh delivery-options request failed (HTTP {response.status})"
            )
        payload = await self._async_response_json(response)
        return self._parse_delivery_options(payload)

    @staticmethod
    def _parse_delivery_options(payload: Any) -> list[HelloFreshDeliveryOption]:
        """Dedupe the delivery-day options across items by handle, keeping the richest entry."""
        items = payload.get("items") if isinstance(payload, dict) else None
        by_handle: dict[str, HelloFreshDeliveryOption] = {}
        for item in items or []:
            if not isinstance(item, dict):
                continue
            for raw_option in item.get("deliveryOptions") or []:
                option = HelloFreshDeliveryOption.from_api(raw_option)
                if option is None:
                    continue
                # First occurrence wins, but a later one flagged default upgrades the flag.
                existing = by_handle.get(option.handle)
                if existing is None:
                    by_handle[option.handle] = option
                elif option.is_default and not existing.is_default:
                    existing.is_default = True
        return sorted(by_handle.values(), key=lambda o: (o.delivery_day or 99, o.handle))

    async def async_get_plans(self) -> list[dict[str, Any]]:
        """Return the customer's plan catalog (``GET /gw/api/plans``).

        The list of the account's plans with their product handle, price, and status. Returned
        as decoded dicts — this is reference data, not part of the normalized account model.
        """
        response = await self._async_api_get(
            "/gw/api/plans", params={"includeCanceled": "false"}
        )
        if response.status >= _HTTP_BAD_REQUEST:
            raise HelloFreshError(f"HelloFresh plans request failed (HTTP {response.status})")
        payload = await self._async_response_json(response)
        if isinstance(payload, list):
            return [p for p in payload if isinstance(p, dict)]
        items = payload.get("items") if isinstance(payload, dict) else None
        return [p for p in (items or []) if isinstance(p, dict)]

    async def async_get_presets(self, take: int = 150) -> list[dict[str, Any]]:
        """Return the region's menu presets (``GET /gw/menus-service/presets``).

        The selectable plan presets (Chef's Choice, Veggie, Quick & Easy, …) with their handle,
        display name, and description — the human-readable names behind a plan's ``preset`` slug.
        """
        params = {
            "country": api_country_code(self._country),
            "locale": self._locale_for_country(),
            "take": str(take),
        }
        response = await self._async_api_get("/gw/menus-service/presets", params=params)
        if response.status >= _HTTP_BAD_REQUEST:
            raise HelloFreshError(f"HelloFresh presets request failed (HTTP {response.status})")
        payload = await self._async_response_json(response)
        items = payload.get("items") if isinstance(payload, dict) else None
        return [p for p in (items or []) if isinstance(p, dict)]

    @staticmethod
    def _existing_recipe_quantity(recipe: HelloFreshRecipe) -> int:
        """Servings currently selected for a recipe (defaults to 1 when selected but unknown)."""
        if recipe.selected_quantity and recipe.selected_quantity > 0:
            return recipe.selected_quantity
        return 1

    async def _async_get_subscription_for_week(
        self,
        week: HelloFreshWeek,
    ) -> HelloFreshSubscription:
        """Return the subscription that owns a known week."""
        if week.subscription_id is None:
            raise HelloFreshNotImplementedError(
                f"Week {week.week_id} does not expose a subscription id, so the matching subscription cannot be loaded."
            )

        subscriptions = self._cached_subscriptions or await self._async_get_subscriptions()
        for subscription in subscriptions:
            if subscription.subscription_id == week.subscription_id:
                return subscription

        raise HelloFreshNotImplementedError(
            f"Week {week.week_id} refers to subscription {week.subscription_id}, but that subscription is not available."
        )

    async def _async_build_cart_selection_update(
        self,
        subscription: HelloFreshSubscription,
        week: HelloFreshWeek,
        recipe_ids: Sequence[str],
        quantities: dict[str, int] | None = None,
    ) -> dict[str, Any] | None:
        """Build the cart update request that HelloFresh currently uses for meal selection."""
        menu_payload = week.raw.get("_menu_payload")
        if not isinstance(menu_payload, dict):
            return None

        selected_meals = self._build_cart_selected_meals(menu_payload, recipe_ids, quantities)
        if selected_meals is None:
            return None

        # Preserve any currently-selected market items so a meal write doesn't clear extras.
        extras = self._build_cart_existing_extras(week)
        return await self._async_build_cart_update(week, subscription, selected_meals, extras)

    # HelloFresh box SKUs encode the plan as ``<PREFIX>-<MEALS>-<SERVINGS>-<N>`` (e.g.
    # ``US-CBU-3-2-0`` is 3 meals × 2 servings). Selecting a different number of DISTINCT meals
    # than the base plan resizes the box for that week: the cart PUT's ``product-sku`` must carry
    # the matching meal-count digit or the endpoint rejects the write with MEAL_SIZE_MISMATCH.
    # This mirrors the web app, which swaps the digit up (``US-CBU-3-2-0`` → ``US-CBU-4-2-0`` for
    # a 4th meal) OR down (``US-CBU-2-2-0`` for 2 meals) to match the selection; the servings
    # digit is unchanged. Both directions are HAR-confirmed against a 3-meal plan.
    _BOX_SKU_RE = re.compile(r"^(?P<prefix>.+-)(?P<meals>\d+)(?P<rest>-\d+-\d+)$")

    # Smallest box HelloFresh accepts. 2 distinct meals is the known minimum (and the smallest
    # box the HAR proves works); fewer has no valid box SKU to resize to.
    MIN_MEALS_PER_WEEK = 2

    @classmethod
    def _sku_for_meal_count(cls, product_sku: str, meal_count: int) -> str:
        """Return ``product_sku`` with its meal-count segment set to ``meal_count``.

        Resizes the box in either direction (up or down) to match the number of distinct meals
        selected, and only rewrites SKUs matching the known ``<prefix>-<meals>-<servings>-<n>``
        box shape; anything else is returned unchanged so we never corrupt an unexpected format.
        """
        match = cls._BOX_SKU_RE.match(product_sku)
        if match is None:
            return product_sku
        current_meals = int(match.group("meals"))
        # A zero meal-count marks an add-on/charge SKU (e.g. ``US-CHARGE-0-0-0``), not a meal
        # box — never rewrite those.
        if current_meals <= 0 or meal_count == current_meals:
            return product_sku
        # Never resize below the smallest real box. A sub-minimum count means "no meal info to
        # size by" (e.g. a market-only write on a week with no confirmed meals, where the meal
        # list is empty) — keep the base plan's SKU rather than emitting an invalid box.
        if meal_count < cls.MIN_MEALS_PER_WEEK:
            return product_sku
        return f"{match.group('prefix')}{meal_count}{match.group('rest')}"

    async def _async_build_cart_update(
        self,
        week: HelloFreshWeek,
        subscription: HelloFreshSubscription,
        meals: list[dict[str, int]],
        extras: list[dict[str, int]],
    ) -> dict[str, Any] | None:
        """Assemble the shared ``PUT /gw/v1/carts/{week}`` request for meals + extras."""
        customer_id = coerce_int(subscription.account_id)
        cutoff_time = self._format_cart_cutoff_time(
            week.selection_deadline
            or parse_datetime(
                week.raw.get("cutoffDate")
                or week.raw.get("selectionDeadline")
                or week.raw.get("deadline")
            )
        )
        preference = await self._async_get_subscription_plan_preference(subscription)
        product_sku = self._find_first_nested_value(
            subscription.raw, ("sku",)
        ) or self._find_first_nested_value(
            week.raw.get("product"),
            ("sku", "handle"),
        )

        if not all((customer_id, cutoff_time, preference, product_sku, week.subscription_id)):
            return None

        # Upgrade the box SKU when more distinct meals are selected than the base plan holds, so
        # HelloFresh accepts the larger selection (and prices the extra meals) instead of failing
        # with MEAL_SIZE_MISMATCH. The SKU's meal digit tracks DISTINCT meals, not total servings.
        product_sku = self._sku_for_meal_count(str(product_sku), len(meals))

        return {
            "path": f"/gw/v1/carts/{week.week_id}",
            "params": {
                "customer": str(customer_id),
                "cutoff_time": cutoff_time,
                "ignore_addons": "false",
                "preference": str(preference),
                "product-sku": str(product_sku),
                "subscription": str(week.subscription_id),
                "update_quantity": "true",
                "week": week.week_id,
            },
            "json_payload": {
                "extras": extras,
                "meals": meals,
            },
        }

    @staticmethod
    def _build_cart_extras(
        resolved: Sequence[tuple[HelloFreshMarketItem, int, int]],
    ) -> list[dict[str, Any]]:
        """Build the cart ``extras`` payload from (item, one_off_qty, preselected_qty) tuples.

        HAR-confirmed shape: extras are grouped by ``groupType``+``sku``, each group carrying a
        ``selection`` list of ``{index, oneOffQuantity, preselectedQuantity, courses}``::

            [{"groupType": "protein", "sku": "US-APR-0-0-0",
              "selection": [{"index": 10089, "oneOffQuantity": 2,
                             "preselectedQuantity": 0, "courses": []}]}]
        """
        groups: dict[tuple[str | None, str | None], dict[str, Any]] = {}
        for item, one_off, preselected in resolved:
            if item.index is None or (one_off <= 0 and preselected <= 0):
                continue
            key = (item.group_type, item.sku)
            group = groups.get(key)
            if group is None:
                group = {"groupType": item.group_type, "sku": item.sku, "selection": []}
                groups[key] = group
            group["selection"].append(
                {
                    "index": item.index,
                    "oneOffQuantity": one_off,
                    "preselectedQuantity": preselected,
                    "courses": [],
                }
            )
        return list(groups.values())

    @staticmethod
    def _build_cart_existing_extras(week: HelloFreshWeek) -> list[dict[str, Any]]:
        """Cart ``extras`` payload for the week's currently-selected market items (unchanged).

        Used when writing a MEAL change so the existing market selection is preserved.
        """
        resolved: list[tuple[HelloFreshMarketItem, int, int]] = []
        for item in week.market_items:
            if item.is_selected and item.index is not None:
                total = item.selected_quantity or 1
                preselected = min(item.preselected_quantity or 0, total)
                resolved.append((item, total - preselected, preselected))
        return HelloFreshClient._build_cart_extras(resolved)

    @staticmethod
    def _build_cart_existing_meals(week: HelloFreshWeek) -> list[dict[str, int]]:
        """Cart ``meals`` entries for the week's currently-selected recipes."""
        meals: list[dict[str, int]] = []
        for recipe in week.recipes:
            if recipe.is_selected and recipe.course_index is not None:
                meals.append(
                    {
                        "index": recipe.course_index,
                        "quantity": HelloFreshClient._existing_recipe_quantity(recipe),
                    }
                )
        return meals

    def _build_cart_selected_meals(
        self,
        menu_payload: dict[str, Any],
        recipe_ids: Sequence[str],
        quantities: dict[str, int] | None = None,
    ) -> list[dict[str, int]] | None:
        """Translate normalized recipe ids into the cart meal-index payload.

        When ``quantities`` provides a per-recipe serving count it wins; otherwise the count
        falls back to the meal's existing selection quantity (or 1), preserving prior behavior.
        """
        quantities = quantities or {}
        meals_by_recipe_id: dict[str, dict[str, Any]] = {}
        for raw_meal in self._extract_menu_week_recipe_candidates(menu_payload):
            if not isinstance(raw_meal, dict):
                continue
            recipe_id = self._extract_recipe_id_from_raw_meal(raw_meal)
            meals_by_recipe_id[recipe_id] = raw_meal

        selected_meals: list[dict[str, int]] = []
        for recipe_id in recipe_ids:
            raw_meal = meals_by_recipe_id.get(recipe_id)
            if raw_meal is None:
                return None

            index = coerce_int(raw_meal.get("index"))
            if index is None:
                return None

            quantity = quantities.get(recipe_id) or (
                coerce_int(
                    (raw_meal.get("selection") or {}).get("quantity") or raw_meal.get("quantity")
                )
                or 1
            )
            selected_meals.append({"index": index, "quantity": quantity})

        return selected_meals

    @staticmethod
    def _format_cart_cutoff_time(value: datetime | None) -> str | None:
        """Format the menu cutoff timestamp for the cart update endpoint."""
        if value is None:
            return None
        if value.tzinfo is None:
            return value.strftime("%Y-%m-%dT%H:%M:%S")
        return value.isoformat()

    async def async_skip_week(self, week_id: str) -> None:
        """Skip a delivery week (set its delivery status to PAUSED)."""
        week = self._get_known_week_or_raise(week_id)
        if week.is_skipped:
            _LOGGER.info("HelloFresh week %s is already skipped", week_id)
            return
        if await self._async_patch_delivery_status(week, "PAUSED"):
            return
        # HAR-verified PATCH couldn't be built or was rejected; fall back to guessed paths.
        payload_variants = [
            {"weekId": week_id, "skip": True},
            {"week": week_id, "status": "skipped"},
            {"subscriptionId": week.subscription_id, "weekId": week_id, "action": "skip"},
        ]
        path_templates = [
            "/gw/my-deliveries/weeks/{week_id}/skip",
            "/gw/my-menu/weeks/{week_id}/skip",
            "/gw/api/customers/me/subscriptions/{subscription_id}/weeks/{week_id}/skip",
        ]
        await self._async_try_mutation_candidates(
            path_templates, week, payload_variants, category="skip"
        )

    async def async_unskip_week(self, week_id: str) -> None:
        """Undo a skipped delivery week (set its delivery status back to RUNNING)."""
        week = self._get_known_week_or_raise(week_id)
        if not week.is_skipped:
            _LOGGER.info("HelloFresh week %s is already active", week_id)
            return
        if await self._async_patch_delivery_status(week, "RUNNING"):
            return
        # HAR-verified PATCH couldn't be built or was rejected; fall back to guessed paths.
        payload_variants = [
            {"weekId": week_id, "skip": False},
            {"week": week_id, "status": "active"},
            {"subscriptionId": week.subscription_id, "weekId": week_id, "action": "unskip"},
        ]
        path_templates = [
            "/gw/my-deliveries/weeks/{week_id}/unskip",
            "/gw/my-menu/weeks/{week_id}/unskip",
            "/gw/api/customers/me/subscriptions/{subscription_id}/weeks/{week_id}/unskip",
        ]
        await self._async_try_mutation_candidates(
            path_templates, week, payload_variants, category="unskip"
        )

    async def _async_patch_delivery_status(self, week: HelloFreshWeek, status: str) -> bool:
        """Skip/unskip a week via the HAR-verified delivery-status PATCH.

        Observed in a capture as
        ``PATCH /gw/api/subscriptions/{subscription_id}/delivery_dates/{week_id}`` with body
        ``{"delivery": {"cutoffDate", "deliveryDate", "status", "subscriptionId", "id"}}``,
        where ``status`` is ``PAUSED`` (skip) or ``RUNNING`` (unskip). Returns True on
        success; returns False (so the caller can fall back) when the required date fields
        aren't available. A rejection still propagates as ``HelloFreshError``.
        """
        if week.subscription_id is None:
            return False

        # Prefer the exact server-formatted timestamps from the raw payload; fall back to the
        # normalized fields. The endpoint echoes these back, so the format should round-trip.
        cutoff_date = week.raw.get("cutoffDate") or (
            week.selection_deadline.isoformat() if week.selection_deadline else None
        )
        delivery_date = week.raw.get("deliveryDate") or (
            week.delivery_date.isoformat() if week.delivery_date else None
        )
        if not cutoff_date or not delivery_date:
            return False

        path = f"/gw/api/subscriptions/{week.subscription_id}/delivery_dates/{week.week_id}"
        params = {"country": api_country_code(self._country), "locale": self._locale_for_country()}
        json_payload = {
            "delivery": {
                "cutoffDate": cutoff_date,
                "deliveryDate": delivery_date,
                "status": status,
                "subscriptionId": week.subscription_id,
                "id": week.week_id,
            }
        }
        await self._async_api_request("PATCH", path, params=params, json_payload=json_payload)
        _LOGGER.info(
            "HelloFresh week %s delivery status set to %s via %s", week.week_id, status, path
        )
        return True

    def _locale_for_country(self) -> str:
        """Return the locale of the primary subscription, defaulting to en-<CC>."""
        for subscription in self._cached_subscriptions or []:
            if subscription.locale:
                return subscription.locale
        return api_locale(self._country)

    async def async_change_one_off_delivery(self, week_id: str, delivery_option: str) -> None:
        """Reschedule a single week's delivery to a different delivery option (slot/day).

        HAR-verified: ``POST /gw/api/subscriptions/{subscription_id}/oneoff`` with body
        ``{"id", "delivery_option", "week", "source"}``. This is a one-off change affecting
        only the given week, not the recurring plan.
        """
        week = self._get_known_week_or_raise(week_id)
        if week.subscription_id is None:
            raise HelloFreshNotImplementedError(
                f"Week {week_id} does not expose a subscription id, so it cannot be rescheduled."
            )
        if not delivery_option or not delivery_option.strip():
            raise HelloFreshError("A delivery_option handle is required to reschedule a week")
        # Gate on the capability the account actually advertises for this week.
        if week.allowed_actions and not week.allowed_actions.get("oneOffChange", False):
            raise HelloFreshNotImplementedError(
                f"Week {week_id} does not allow a one-off delivery change for this account."
            )

        path = f"/gw/api/subscriptions/{week.subscription_id}/oneoff"
        params = {"country": api_country_code(self._country), "locale": self._locale_for_country()}
        json_payload = {
            "id": week.subscription_id,
            "delivery_option": delivery_option.strip(),
            "week": week_id,
            "source": "reschedule-delivery-feature",
        }
        await self._async_api_request("POST", path, params=params, json_payload=json_payload)
        _LOGGER.info(
            "HelloFresh week %s rescheduled to delivery option %s", week_id, delivery_option
        )

    async def async_change_delivery_weekday(
        self,
        delivery_option: str,
        delivery_interval: int = 1,
        subscription_id: str | None = None,
    ) -> None:
        """Change the recurring delivery option/interval for a subscription's plan.

        HAR-verified: ``POST /gw/api/plans/{planId}/changePlanDeliveryDetails`` with body
        ``{"deliveryOption", "deliveryInterval"}``. This affects **all** future deliveries
        for the plan, not a single week.
        """
        if not delivery_option or not delivery_option.strip():
            raise HelloFreshError("A delivery_option handle is required")

        subscription = await self._async_resolve_subscription(subscription_id)
        plan_id = subscription.raw.get("customerPlanId")
        if not plan_id:
            raise HelloFreshNotImplementedError(
                "The subscription does not expose a customerPlanId, so its delivery weekday "
                "cannot be changed."
            )

        path = f"/gw/api/plans/{plan_id}/changePlanDeliveryDetails"
        params = {"country": api_country_code(self._country)}
        json_payload = {
            "deliveryOption": delivery_option.strip(),
            "deliveryInterval": delivery_interval,
        }
        await self._async_api_request("POST", path, params=params, json_payload=json_payload)
        _LOGGER.info(
            "HelloFresh plan %s delivery details changed to %s (interval %s)",
            plan_id,
            delivery_option,
            delivery_interval,
        )

    async def _async_resolve_subscription(
        self,
        subscription_id: str | None,
    ) -> HelloFreshSubscription:
        """Return the named subscription, or the primary one when no id is given."""
        subscriptions = self._cached_subscriptions or await self._async_get_subscriptions()
        if subscription_id is None:
            return subscriptions[0]
        for subscription in subscriptions:
            if subscription.subscription_id == subscription_id:
                return subscription
        raise HelloFreshError(f"HelloFresh subscription not found: {subscription_id}")

    def _get_known_week_or_raise(self, week_id: str) -> HelloFreshWeek:
        """Return a known account week or raise a user-facing error."""
        if self._last_account_data is None:
            raise HelloFreshError("No HelloFresh account data loaded yet")

        week = self._last_account_data.get_week(week_id)
        if week is not None:
            return week

        raise HelloFreshError(f"HelloFresh week not found: {week_id}")

    async def _async_get_primary_subscription(self) -> HelloFreshSubscription:
        """Return the first subscription for auth validation."""
        subscriptions = await self._async_get_subscriptions()
        if not subscriptions:
            raise HelloFreshAuthError("No HelloFresh subscriptions returned for this token")
        return subscriptions[0]

    async def _async_get_subscriptions(self) -> list[HelloFreshSubscription]:
        """Fetch subscription and locale details for the current account."""
        if self._cached_subscriptions is not None:
            return self._cached_subscriptions

        response = await self._async_api_get("/gw/api/customers/me/subscriptions")
        payload = await self._async_response_json(response)
        items = payload.get("items") or []
        if not items:
            raise HelloFreshAuthError("No HelloFresh subscriptions returned for this token")

        subscriptions = [self._subscription_from_raw_subscription(item) for item in items]
        self._cached_subscriptions = subscriptions
        return subscriptions

    async def _async_get_boxes_received(self) -> int | None:
        """Fetch account-level box history metrics from authenticated profile endpoints."""
        if self._session is None:
            return None

        locale = next(
            (s.locale for s in (self._cached_subscriptions or []) if s.locale),
            api_locale(self._country),
        )
        candidate_paths = (
            "/gw/api/customers/me/info",
            "/gw/customer-attributes-service/attributes",
        )

        boxes_received: int | None = None
        for path in candidate_paths:
            try:
                params = (
                    {"country": api_country_code(self._country), "locale": locale}
                    if path == "/gw/api/customers/me/info"
                    else None
                )
                extra_headers = (
                    {"x-requested-by": "client-platform"}
                    if path == "/gw/api/customers/me/info"
                    else None
                )
                response = await self._async_api_get(
                    path, params=params, extra_headers=extra_headers
                )
                payload = await self._async_response_json(response)
            except HelloFreshError as err:
                self._record_debug_attempt(
                    "profile_attempts",
                    {
                        "path": path,
                        "error": str(err),
                    },
                )
                continue

            if boxes_received is None:
                boxes_received = coerce_int(
                    payload.get("boxesReceived")
                    or payload.get("boxes_received")
                    or self._find_first_nested_value(
                        payload,
                        ("boxesReceived", "boxes_received", "deliveredBoxes"),
                    )
                )
            self._record_debug_attempt(
                "profile_attempts",
                {
                    "path": path,
                    "status": self._response_status(response),
                    "payload_summary": self._summarize_payload(payload),
                    "boxes_received": boxes_received,
                },
            )
            if boxes_received is not None:
                break

        return boxes_received

    # ------------------------------------------------------------------
    # Partial-data warning
    # ------------------------------------------------------------------

    def _warn_partial_data(self, data: HelloFreshAccountData) -> None:
        """Emit a structured warning when account data is incomplete after a successful fetch."""
        missing: list[str] = []
        if not data.subscriptions:
            missing.append("subscriptions")
        if not data.weeks and not data.current_public_menu:
            missing.append("weeks/menu")
        if data.next_order is None and data.upcoming_orders:
            missing.append("next_order resolution")
        if missing:
            _LOGGER.warning(
                "HelloFresh account data is partial after fetch — missing: %s",
                ", ".join(missing),
            )

    # ------------------------------------------------------------------
    # Payment-date enrichment helpers
    # ------------------------------------------------------------------

    def _accumulate_order_prices(
        self,
        items: list[Any],
    ) -> tuple[
        dict[str, datetime],  # latest_by_subscription
        dict[str, tuple[date, datetime]],  # future_by_subscription
        dict[str, str],  # next_order_nr_by_subscription
        dict[tuple[str, date], tuple[float, str | None]],  # price_by_key
    ]:
        """Scan raw billing items and return per-subscription price/date accumulators."""
        today = datetime.now(UTC).date()
        latest_by_subscription: dict[str, datetime] = {}
        future_by_subscription: dict[str, tuple[date, datetime]] = {}
        next_order_nr_by_subscription: dict[str, str] = {}
        price_by_key: dict[tuple[str, date], tuple[float, str | None]] = {}

        for item in items:
            if not isinstance(item, dict):
                continue
            created_at = parse_datetime(item.get("createdAt"))
            if created_at is None:
                continue

            order_lines = item.get("orderLines")
            if not isinstance(order_lines, list) or not order_lines:
                continue
            first_line = order_lines[0] if isinstance(order_lines[0], dict) else {}
            raw_sub_id = (
                (first_line.get("subscription") or {}).get("id")
                if isinstance(first_line.get("subscription"), dict)
                else None
            )
            subscription_id = str(raw_sub_id) if raw_sub_id is not None else None
            if not subscription_id:
                continue

            delivery_date = parse_date(first_line.get("deliveryDate"))

            # "recent payment" = the most recent order that has actually been CHARGED, i.e.
            # whose createdAt is in the past. HelloFresh bills a box several days before its
            # delivery date, so an upcoming box can already be a real, recent charge. Filtering
            # on delivery_date (the previous behaviour) skipped that charge and reported the
            # PRIOR box instead, leaving the date ~a week behind the customer's last charge.
            if created_at.date() <= today:
                latest_existing = latest_by_subscription.get(subscription_id)
                if latest_existing is None or created_at > latest_existing:
                    latest_by_subscription[subscription_id] = created_at

            if delivery_date is None:
                continue

            grand_total = coerce_float(
                item.get("grandTotal") or item.get("totalPrice") or item.get("total")
            )
            if grand_total is not None:
                currency = self._extract_currency_code(item)
                price_key = (subscription_id, delivery_date)
                existing = price_by_key.get(price_key)
                price_by_key[price_key] = (
                    (existing[0] if existing else 0.0) + grand_total,
                    existing[1] if existing else currency,
                )

            if delivery_date < today:
                continue
            future_existing = future_by_subscription.get(subscription_id)
            if future_existing is None or delivery_date < future_existing[0]:
                future_by_subscription[subscription_id] = (delivery_date, created_at)
                raw_order_nr = item.get("orderNr") or item.get("id")
                if raw_order_nr is not None:
                    next_order_nr_by_subscription[subscription_id] = str(raw_order_nr)

        return (
            latest_by_subscription,
            future_by_subscription,
            next_order_nr_by_subscription,
            price_by_key,
        )

    def _apply_recent_payment_dates(
        self,
        subscriptions: Sequence[HelloFreshSubscription],
        latest_by_subscription: dict[str, datetime],
        future_by_subscription: dict[str, tuple[date, datetime]],
    ) -> None:
        """Write recent_payment_date and next_payment_date back onto each subscription."""
        for subscription in subscriptions:
            latest = latest_by_subscription.get(subscription.subscription_id)
            if latest is not None:
                subscription.recent_payment_date = latest.date()
            future = future_by_subscription.get(subscription.subscription_id)
            if future is not None:
                subscription.next_payment_date = future[0]

    def _compute_next_delivery_total(
        self,
        data: HelloFreshAccountData,
        future_by_subscription: dict[str, tuple[date, datetime]],
        next_order_nr_by_subscription: dict[str, str],
        price_by_key: dict[tuple[str, date], tuple[float, str | None]],
    ) -> None:
        """Sum all billing charges for the earliest upcoming delivery date and write to data."""
        if not future_by_subscription:
            return
        next_sub = min(future_by_subscription, key=lambda s: future_by_subscription[s][0])
        data.recent_order_id = next_order_nr_by_subscription.get(next_sub)

        next_date = min(d for d, _ in future_by_subscription.values())
        total = sum(v[0] for k, v in price_by_key.items() if k[1] == next_date)
        currency = next(
            (v[1] for k, v in price_by_key.items() if k[1] == next_date and v[1]),
            None,
        )
        data.next_delivery_total = round(total, 2) if total else None
        data.next_delivery_total_currency = currency

    def _apply_prices_to_orders(
        self,
        orders: Sequence[HelloFreshOrder],
        price_by_key: dict[tuple[str, date], tuple[float, str | None]],
    ) -> None:
        """Overlay accumulated billing totals onto matching order objects."""
        for order in orders:
            if order.subscription_id is None or order.delivery_date is None:
                continue
            price_entry = price_by_key.get((order.subscription_id, order.delivery_date))
            if price_entry is None:
                continue
            grand_total, currency = price_entry
            rounded_total = round(grand_total, 2)
            order.total_price = rounded_total
            # Preserve the summed billing total separately: a later cart/calculate estimate may
            # overwrite total_price, but billed_total_price stays the authoritative charge sum
            # (matching the next_box_total_price sensor).
            order.billed_total_price = rounded_total
            if currency:
                order.currency = currency
                order.billed_total_currency = currency

    async def _async_enrich_subscription_payment_dates(
        self,
        subscriptions: Sequence[HelloFreshSubscription],
        orders: Sequence[HelloFreshOrder] | None = None,
        data: HelloFreshAccountData | None = None,
    ) -> None:
        """Overlay recent and upcoming payment dates onto subscriptions and exact prices onto orders."""
        if self._session is None or not subscriptions:
            return

        for subscription in subscriptions:
            if subscription.next_cutoff_date is not None:
                # Provisional estimate only: current US orders appear to be created
                # immediately after the weekly cutoff. This is a fallback that is
                # overwritten below by _apply_recent_payment_dates whenever the billing
                # API (/gw/api/customers/me/orders) returns an authoritative date. Do not
                # "fix" this heuristic in isolation — the billing-API path is the source
                # of truth when it succeeds.
                subscription.next_payment_date = (
                    subscription.next_cutoff_date + timedelta(seconds=1)
                ).date()

        try:
            response = await self._async_api_get(
                "/gw/api/customers/me/orders",
                params={
                    # The live site sends UPPERCASE country here (HAR: country=US), matching
                    # every other /gw call. An earlier lowercase guess worked only because the
                    # US property is lenient; a stricter region (see uk→GB) can reject country=us.
                    "country": api_country_code(self._country),
                    "locale": subscriptions[0].locale or api_locale(self._country),
                    "limit": 200,
                },
            )
            payload = await self._async_response_json(response)
        except HelloFreshError as err:
            self._record_debug_attempt(
                "payment_attempts",
                {"path": "/gw/api/customers/me/orders", "error": str(err)},
            )
            await self._async_enrich_recent_payment_from_balance_transactions(subscriptions)
            return

        items = payload.get("items") or []
        (
            latest_by_subscription,
            future_by_subscription,
            next_order_nr_by_subscription,
            price_by_key,
        ) = self._accumulate_order_prices(items)

        self._apply_recent_payment_dates(
            subscriptions, latest_by_subscription, future_by_subscription
        )

        if data is not None:
            self._compute_next_delivery_total(
                data, future_by_subscription, next_order_nr_by_subscription, price_by_key
            )

        if orders:
            self._apply_prices_to_orders(orders, price_by_key)

        self._record_debug_attempt(
            "payment_attempts",
            {
                "path": "/gw/api/customers/me/orders",
                "status": self._response_status(response),
                "order_prices_applied": len(price_by_key),
                "subscription_payment_dates": {
                    subscription.subscription_id: {
                        "recent_payment_date": (
                            subscription.recent_payment_date.isoformat()
                            if subscription.recent_payment_date
                            else None
                        ),
                        "next_payment_date": (
                            subscription.next_payment_date.isoformat()
                            if subscription.next_payment_date
                            else None
                        ),
                    }
                    for subscription in subscriptions
                },
            },
        )

        if any(subscription.recent_payment_date is not None for subscription in subscriptions):
            return

        await self._async_enrich_recent_payment_from_balance_transactions(subscriptions)

    async def _async_enrich_recent_payment_from_balance_transactions(
        self,
        subscriptions: Sequence[HelloFreshSubscription],
    ) -> None:
        """Fallback recent-payment lookup using the account balance transactions feed."""
        customer_uuid = next(
            (
                self._find_first_nested_value(subscription.raw, ("uuid",))
                for subscription in subscriptions
                if self._find_first_nested_value(subscription.raw, ("uuid",))
            ),
            None,
        )
        if not isinstance(customer_uuid, str) or not customer_uuid:
            return

        params = {
            "customerUUID": customer_uuid,
            "types": "DEBIT",
        }

        try:
            response = await self._async_api_get(
                "/gw/payments/balance/transactions",
                params=params,
            )
            payload = await self._async_response_json(response)
        except HelloFreshError as err:
            self._record_debug_attempt(
                "payment_attempts",
                {
                    "path": "/gw/payments/balance/transactions",
                    "params": params,
                    "error": str(err),
                },
            )
            return

        if not isinstance(payload, list):
            return

        created_dates = [
            created_at.date()
            for item in payload
            if isinstance(item, dict)
            and item.get("transactionType") == "DEBIT"
            and (created_at := parse_datetime(item.get("createdAt"))) is not None
        ]
        if not created_dates:
            return

        latest_date = max(created_dates)
        for subscription in subscriptions:
            if subscription.recent_payment_date is None:
                subscription.recent_payment_date = latest_date

        self._record_debug_attempt(
            "payment_attempts",
            {
                "path": "/gw/payments/balance/transactions",
                "params": params,
                "status": self._response_status(response),
                "recent_payment_date": latest_date.isoformat(),
            },
        )

    async def _async_enrich_account_credit(
        self,
        data: HelloFreshAccountData,
        subscriptions: Sequence[HelloFreshSubscription],
    ) -> None:
        """Read the account credit balance that applies automatically to the next order.

        Source: ``/gw/payments/customers/{customerUUID}/balance``. The response's
        ``amount`` is the spendable credit the website surfaces as "$X that will apply
        automatically to your next order"; ``currencyCode`` gives the unit.
        """
        if self._session is None:
            return
        customer_uuid = next(
            (
                uuid
                for subscription in subscriptions
                if (uuid := self._find_first_nested_value(subscription.raw, ("uuid",)))
            ),
            None,
        )
        if not isinstance(customer_uuid, str) or not customer_uuid:
            return

        path = f"/gw/payments/customers/{customer_uuid}/balance"
        params = {
            "business_unit": api_country_code(self._country),
            "country": api_country_code(self._country),
        }
        try:
            response = await self._async_api_get(path, params=params)
            payload = await self._async_response_json(response)
        except HelloFreshError as err:
            self._record_debug_attempt(
                "payment_attempts",
                {"path": "/gw/payments/customers/{uuid}/balance", "error": str(err)},
            )
            return

        if not isinstance(payload, dict):
            return

        amount = coerce_float(payload.get("amount"))
        if amount is None:
            return
        data.account_credit = amount
        currency = payload.get("currencyCode")
        data.account_credit_currency = currency if isinstance(currency, str) and currency else None

        self._record_debug_attempt(
            "payment_attempts",
            {
                "path": "/gw/payments/customers/{uuid}/balance",
                "status": self._response_status(response),
                "account_credit": amount,
                "currency": data.account_credit_currency,
            },
        )

    async def _async_enrich_selected_plan_price(
        self,
        data: HelloFreshAccountData,
        subscriptions: Sequence[HelloFreshSubscription],
    ) -> None:
        """Read the standing weekly plan price (incl. shipping) shown in plan settings.

        Source: ``POST /gw/calculate`` for the primary subscription's recurring plan (no
        specific week). The response's ``grandTotal`` is the shipping-included weekly total
        (``subTotal`` + ``shippingAmount``); ``currency`` gives the unit. Best-effort: a
        missing payload or total simply leaves the sensor unavailable.
        """
        if self._session is None or not subscriptions:
            return

        pricing = await self._async_get_calculate_price(subscriptions[0], week=None)
        if pricing is None:
            return

        total = self._extract_total_price(pricing)
        if total is None:
            return
        data.selected_plan_total_price = round(total, 2)
        currency = self._extract_currency_code(pricing)
        data.selected_plan_total_price_currency = currency or None

        self._record_debug_attempt(
            "pricing_attempts",
            {
                "subscription_id": subscriptions[0].subscription_id,
                "path": "/gw/calculate",
                "context": "selected_plan_total_price",
                "selected_plan_total_price": data.selected_plan_total_price,
                "currency": data.selected_plan_total_price_currency,
            },
        )

    async def _async_get_past_delivery_weeks(
        self,
        subscriptions: Sequence[HelloFreshSubscription],
    ) -> list[HelloFreshWeek]:
        """Fetch delivered-week history that is not included in upcoming deliveries."""
        if self._session is None:
            return []

        candidate_calls: list[tuple[str, dict[str, Any] | None]] = [
            ("/gw/customer-complaints/users/me/deliveries", None),
        ]
        history_range = self._build_delivery_history_range()
        candidate_calls.append(
            (
                "/gw/api/customers/me/deliveries",
                {
                    "rangeStart": history_range["range_start"],
                    "rangeEnd": history_range["range_end"],
                },
            )
        )
        candidate_calls.extend(
            (
                "/gw/my-deliveries/past-deliveries",
                {
                    "country": api_country_code(self._country),
                    "locale": self._locale_for_country(),
                    "rating-scale": "5",
                    "subscription": subscription.subscription_id,
                },
            )
            for subscription in subscriptions
        )
        ordered_calls = self._order_candidates_by_preference("history", None, candidate_calls)

        # Accumulate recipe-bearing weeks from EVERY candidate, keyed by week id, instead of
        # returning the first endpoint that answers. The history endpoints are NOT
        # interchangeable: ``/gw/customer-complaints/...`` only knows the last couple of weeks,
        # while ``/gw/my-deliveries/past-deliveries`` is the comprehensive, paginated source the
        # website itself uses. Letting whichever answers first win meant an old week (e.g. the
        # one delivered ~4 months ago) never got its real delivered meals, so the card showed a
        # fabricated selection. Always consult past-deliveries; prefer its recipes per week.
        # The single authoritative history source: the comprehensive, paginated endpoint the
        # website itself uses. Recipes here carry their images; the other history endpoints
        # (e.g. customer-complaints, which only knows the last couple of weeks) return
        # image-less recipes, so this endpoint must WIN per week over them.
        authoritative_path = "/gw/my-deliveries/past-deliveries"
        delivered_by_week: dict[str, HelloFreshWeek] = {}
        # Which endpoint filled each week, so the authoritative source can overwrite a week that a
        # narrower endpoint filled first (all history weeks share source="past_deliveries", so the
        # model's source can't distinguish them — track the fetch path here instead).
        filled_by_path: dict[str, str] = {}
        preferred_endpoint: tuple[str, dict[str, Any] | None] | None = None
        fallback_weeks: tuple[str, dict[str, Any] | None, list[HelloFreshWeek]] | None = None
        for path, params in ordered_calls:
            try:
                response = await self._async_api_get(path, params=params)
                payload = await self._async_response_json(response)
            except HelloFreshError as err:
                self._record_debug_attempt(
                    "history_attempts",
                    {
                        "path": path,
                        "params": params,
                        "error": str(err),
                    },
                )
                continue

            weeks = self._normalize_past_delivery_payload(payload, subscriptions)
            self._record_debug_attempt(
                "history_attempts",
                {
                    "path": path,
                    "params": params,
                    "status": self._response_status(response),
                    "payload_summary": self._summarize_payload(payload),
                    "recognized_week_count": len(weeks),
                },
            )
            if not weeks:
                continue

            # A metadata-only candidate (the ranged deliveries endpoint returns past weeks as
            # shells with no recipe list) can't supply selection; keep it only as a last resort.
            if not any(week.recipes for week in weeks):
                if fallback_weeks is None:
                    fallback_weeks = (path, params, weeks)
                continue

            # The past-deliveries endpoint pages ~4 weeks at a time and hands back a ``nextWeek``
            # cursor; without following it, history stops after the first page (~131 days) and
            # older weeks show no recipe data. Walk the cursor back for that endpoint only.
            if path == "/gw/my-deliveries/past-deliveries":
                weeks = await self._async_paginate_past_deliveries(
                    path=path,
                    params=params,
                    payload=payload,
                    first_page_weeks=weeks,
                    subscriptions=subscriptions,
                )

            for week in weeks:
                if not week.recipes:
                    continue
                # Fill a week unless it was already filled by the authoritative endpoint. This lets
                # /gw/my-deliveries/past-deliveries (with images) OVERWRITE a week that a narrower
                # endpoint (e.g. customer-complaints, image-less) grabbed first, while never
                # letting a narrower endpoint clobber an authoritative fill.
                if filled_by_path.get(week.week_id) == authoritative_path:
                    continue
                delivered_by_week[week.week_id] = week
                filled_by_path[week.week_id] = path
            if preferred_endpoint is None:
                preferred_endpoint = (path, params)

        if delivered_by_week:
            if preferred_endpoint is not None:
                self._remember_preferred_endpoint("history", None, preferred_endpoint)
            return list(delivered_by_week.values())

        if fallback_weeks is not None:
            path, params, weeks = fallback_weeks
            self._remember_preferred_endpoint("history", None, (path, params))
            return weeks
        return []

    async def _async_paginate_past_deliveries(
        self,
        path: str,
        params: dict[str, Any],
        payload: Any,
        first_page_weeks: list[HelloFreshWeek],
        subscriptions: Sequence[HelloFreshSubscription],
    ) -> list[HelloFreshWeek]:
        """Follow the past-deliveries ``nextWeek`` cursor back to the history floor.

        Each page returns ``{"weeks": [...], "nextWeek": "YYYY-Www"}``. Feed ``nextWeek`` back
        in as the ``from`` cursor until the API stops returning one, until a page yields no new
        weeks, or until the cursor passes the configured history floor. Bounded by a hard page
        cap so a misbehaving cursor can never loop forever.
        """
        # Page a couple of weeks PAST the display range floor so a week sitting right at the
        # boundary (and ISO-week rounding / year-boundary ordering around it) always has its
        # delivered meals fetched, not just shown as an empty shell. Keyed off the same lookback
        # the display range uses (+2 weeks margin) so the two stay aligned as the constant moves.
        floor_date = datetime.now(UTC).date() - timedelta(
            weeks=self._history_weeks + 2
        )
        floor_iso = floor_date.isocalendar()
        floor_week = f"{floor_iso.year}-W{floor_iso.week:02d}"
        weeks_by_id: dict[str, HelloFreshWeek] = {
            week.week_id: week for week in first_page_weeks
        }
        next_cursor = self._extract_next_week_cursor(payload)
        # ~14 four-week pages cover the floor; the cap is generous slack against drift.
        max_pages = 20

        for _ in range(max_pages):
            # Compare by absolute week order, not raw string: "2026-W01" < "2025-W52" is True
            # as strings but later in time, which would stop pagination early near a year change.
            if not next_cursor or self._iso_week_sort_key(next_cursor) < self._iso_week_sort_key(
                floor_week
            ):
                break
            page_params = {**params, "from": next_cursor}
            try:
                response = await self._async_api_get(path, params=page_params)
                page_payload = await self._async_response_json(response)
            except HelloFreshError as err:
                self._record_debug_attempt(
                    "history_attempts",
                    {"path": path, "params": page_params, "error": str(err)},
                )
                break

            page_weeks = self._normalize_past_delivery_payload(page_payload, subscriptions)
            new_weeks = [week for week in page_weeks if week.week_id not in weeks_by_id]
            self._record_debug_attempt(
                "history_attempts",
                {
                    "path": path,
                    "params": page_params,
                    "status": self._response_status(response),
                    "recognized_week_count": len(page_weeks),
                    "new_week_count": len(new_weeks),
                    "cursor": next_cursor,
                },
            )
            for week in new_weeks:
                weeks_by_id[week.week_id] = week
            if not new_weeks:
                break
            next_cursor = self._extract_next_week_cursor(page_payload)

        return list(weeks_by_id.values())

    @staticmethod
    def _extract_next_week_cursor(payload: Any) -> str | None:
        """Return the ``nextWeek`` pagination cursor from a past-deliveries payload."""
        if not isinstance(payload, dict):
            return None
        cursor = payload.get("nextWeek") or payload.get("next_week")
        return cursor if isinstance(cursor, str) and cursor else None

    @staticmethod
    def _iso_week_sort_key(week_id: str) -> tuple[int, int]:
        """Order an ISO ``YYYY-Www`` week chronologically.

        Raw string comparison breaks across year boundaries ("2026-W01" < "2025-W52" as
        strings, but it is later in time), so compare on (year, week) instead. An unparseable
        value sorts last (treated as "most recent") so it never trips the history floor.
        """
        match = re.fullmatch(r"(\d{4})-W(\d{1,2})", week_id.strip()) if week_id else None
        if match is None:
            return (9999, 99)
        return (int(match.group(1)), int(match.group(2)))

    async def _async_get_upcoming_deliveries(
        self,
        subscription: HelloFreshSubscription,
    ) -> tuple[list[HelloFreshWeek], list[HelloFreshOrder]]:
        """Try likely upcoming-deliveries endpoints and normalize the first success."""
        today = datetime.now(UTC).date()
        iso_week = f"{today.year}-W{today.isocalendar().week:02d}"
        history_range = self._build_delivery_history_range()
        subscription_id = subscription.subscription_id
        # The HAR-verified endpoint the live US web app actually uses is the ranged
        # ``/gw/api/customers/me/deliveries`` (it returns past + future weeks in one call),
        # so it is tried first. The remaining candidates got zero hits in the capture and
        # are kept only as drift/other-region fallbacks; the sticky-endpoint cache means a
        # working endpoint is reused on later polls instead of re-probing this whole list.
        candidate_calls = (
            (
                "/gw/api/customers/me/deliveries",  # HAR-verified (US)
                {
                    "rangeStart": history_range["range_start"],
                    "rangeEnd": history_range["range_end"],
                },
            ),
            # Unverified fallbacks (no hits in the US HAR; retained for drift/other regions):
            ("/gw/my-deliveries/upcoming-deliveries", {"subscription": subscription_id}),
            (
                "/gw/my-deliveries/upcoming-deliveries",
                {"subscription": subscription_id, "from": iso_week},
            ),
            ("/gw/my-deliveries/deliveries", {"subscription": subscription_id}),
            ("/gw/api/customers/me/deliveries", {"subscription": subscription_id}),
            (
                f"/gw/api/customers/me/subscriptions/{subscription_id}/deliveries",
                None,
            ),
        )
        ordered_calls = self._order_candidates_by_preference(
            "deliveries", subscription_id, candidate_calls
        )

        last_error: str | None = None
        for path, params in ordered_calls:
            try:
                response = await self._async_api_get(path, params=params)
            except HelloFreshAuthError:
                raise
            except HelloFreshError as err:
                self._record_debug_attempt(
                    "delivery_attempts",
                    {
                        "subscription_id": subscription.subscription_id,
                        "path": path,
                        "params": params,
                        "error": str(err),
                    },
                )
                last_error = str(err)
                continue

            try:
                payload = await self._async_response_json(response)
            except HelloFreshError as err:
                self._record_debug_attempt(
                    "delivery_attempts",
                    {
                        "subscription_id": subscription.subscription_id,
                        "path": path,
                        "params": params,
                        "status": self._response_status(response),
                        "error": str(err),
                    },
                )
                last_error = str(err)
                continue

            weeks, orders = self._normalize_weeks_payload(
                payload=payload,
                subscription=subscription,
            )
            self._record_debug_attempt(
                "delivery_attempts",
                {
                    "subscription_id": subscription.subscription_id,
                    "path": path,
                    "params": params,
                    "status": self._response_status(response),
                    "payload_summary": self._summarize_payload(payload),
                    "recognized_week_count": len(weeks),
                    "recognized_order_count": len(orders),
                },
            )
            if weeks:
                self._remember_preferred_endpoint("deliveries", subscription_id, (path, params))
                return weeks, orders

        if last_error:
            _LOGGER.debug(
                "HelloFresh upcoming deliveries unavailable for subscription %s: %s",
                subscription.subscription_id,
                last_error,
            )

        return [], []

    # Cap on concurrent per-week menu fetches. Bounded so a many-week account doesn't open
    # dozens of simultaneous connections against HelloFresh (each its own impersonated TLS
    # session); ~6 keeps the poll fast without looking like a burst.
    _MENU_FETCH_CONCURRENCY = 6

    async def _async_gather_bounded(
        self,
        coros: Sequence[Any],
        limit: int | None = None,
    ) -> list[Any]:
        """Await coroutines concurrently with a concurrency cap, preserving input order.

        A coroutine that raises a non-auth ``HelloFreshError`` resolves to ``[]`` in its slot
        rather than cancelling the whole batch — one week's failed menu fetch must not sink the
        rest of the poll (the per-week fetch already returns ``[]`` on its own errors; this
        guards unexpected ones). A ``HelloFreshAuthError`` still propagates so the coordinator
        can trigger reauth, as does any other unexpected exception (genuine bugs shouldn't be
        masked as an empty week list).
        """
        if not coros:
            return []
        semaphore = asyncio.Semaphore(limit or self._MENU_FETCH_CONCURRENCY)

        async def _run(coro: Any) -> Any:
            async with semaphore:
                try:
                    return await coro
                except HelloFreshAuthError:
                    # A real auth failure must surface so the coordinator can reauth — never
                    # swallow it into an empty week list.
                    raise
                except HelloFreshError as err:
                    _LOGGER.debug("Bounded menu fetch failed for one week: %s", err)
                    return []

        return await asyncio.gather(*(_run(coro) for coro in coros))

    async def _async_get_account_menu_data(
        self,
        subscriptions: Sequence[HelloFreshSubscription],
        account_weeks: Sequence[HelloFreshWeek] | None = None,
    ) -> dict[str, list[HelloFreshWeek] | list[str]] | None:
        """Try authenticated menu endpoints before falling back to public HTML."""
        weeks: list[HelloFreshWeek] = []
        available_labels: list[str] = []
        weeks_by_subscription: dict[str, list[HelloFreshWeek]] = {}
        for account_week in account_weeks or []:
            if account_week.subscription_id is None:
                continue
            weeks_by_subscription.setdefault(account_week.subscription_id, []).append(account_week)

        # Skip the per-week menu fetch for weeks older than the menu grace window: their
        # recipes are unconditionally replaced by the delivered-only set in
        # _merge_past_delivery_recipes_into_account_weeks, so downloading their (often multi-MB
        # aggregate) menu only to discard it is pure waste. Weeks with no date, or dated within
        # the grace window, still fetch — the grace-window branch keeps and overlays the catalog.
        grace_floor = datetime.now(UTC).date() - timedelta(weeks=self.menu_grace_weeks)

        def _needs_menu_fetch(account_week: HelloFreshWeek) -> bool:
            return account_week.delivery_date is None or account_week.delivery_date >= grace_floor

        for subscription in subscriptions:
            subscription_id = subscription.subscription_id
            subscription_weeks: list[HelloFreshWeek] = []
            seen_week_ids: set[str] = set()

            fetch_weeks = [
                account_week
                for account_week in weeks_by_subscription.get(subscription_id, [])
                if _needs_menu_fetch(account_week)
            ]
            # Fetch the eligible weeks concurrently (bounded) instead of one round-trip at a
            # time — the poll's critical path was ~N sequential TLS round-trips per subscription.
            menu_week_lists = await self._async_gather_bounded(
                [
                    self._async_get_delivery_menu_week_data(
                        subscription=subscription,
                        account_week=account_week,
                    )
                    for account_week in fetch_weeks
                ]
            )
            for delivery_menu_weeks in menu_week_lists:
                for delivery_menu_week in delivery_menu_weeks:
                    if delivery_menu_week.week_id in seen_week_ids:
                        continue
                    seen_week_ids.add(delivery_menu_week.week_id)
                    subscription_weeks.append(delivery_menu_week)

            if subscription_weeks:
                weeks.extend(subscription_weeks)
                for week in subscription_weeks:
                    if week.display_name not in available_labels:
                        available_labels.append(week.display_name)
                continue

            # The HAR-verified menu source is ``/gw/my-deliveries/menu`` (fetched per week in
            # _async_get_delivery_menu_week_data above) with a structured-JSON fallback to
            # ``/gw/menus-service/menus`` below. These remaining paths got zero hits in the
            # US capture and are kept only as last-resort drift/other-region fallbacks.
            candidate_calls = (
                ("/gw/my-menu/weeks", {"subscription": subscription_id}),
                ("/gw/my-menu", {"subscription": subscription_id}),
                ("/gw/api/customers/me/menu", {"subscription": subscription_id}),
                (f"/gw/api/customers/me/subscriptions/{subscription_id}/menu", None),
                (f"/gw/api/customers/me/subscriptions/{subscription_id}/weeks", None),
                (f"/gw/api/customers/me/subscriptions/{subscription_id}/menus", None),
            )
            ordered_calls = self._order_candidates_by_preference(
                "menu", subscription_id, candidate_calls
            )

            subscription_weeks = []
            for path, params in ordered_calls:
                try:
                    response = await self._async_api_get(path, params=params)
                    payload = await self._async_response_json(response)
                except HelloFreshError as err:
                    self._record_debug_attempt(
                        "menu_attempts",
                        {
                            "subscription_id": subscription.subscription_id,
                            "path": path,
                            "params": params,
                            "error": str(err),
                        },
                    )
                    continue

                raw_weeks = self._extract_menu_week_candidates(payload)
                subscription_weeks = self._normalize_menu_weeks(
                    raw_weeks,
                    subscription=subscription,
                )
                self._record_debug_attempt(
                    "menu_attempts",
                    {
                        "subscription_id": subscription.subscription_id,
                        "path": path,
                        "params": params,
                        "status": self._response_status(response),
                        "payload_summary": self._summarize_payload(payload),
                        "candidate_week_count": len(raw_weeks),
                        "recognized_week_count": len(subscription_weeks),
                    },
                )
                if subscription_weeks:
                    self._remember_preferred_endpoint("menu", subscription_id, (path, params))
                    break

            if not subscription_weeks:
                continue

            weeks.extend(subscription_weeks)
            for week in subscription_weeks:
                if week.display_name not in available_labels:
                    available_labels.append(week.display_name)

        if weeks:
            return {
                "weeks": weeks,
                "available_labels": available_labels,
            }

        # Structured-JSON fallback before resorting to HTML scraping: the live US web app
        # serves the menu catalog from /gw/menus-service/menus (HAR-confirmed endpoint).
        menus_service = await self._async_get_menus_service_weeks(subscriptions, account_weeks)
        if menus_service is not None:
            return menus_service

        return None

    async def _async_get_menus_service_weeks(
        self,
        subscriptions: Sequence[HelloFreshSubscription],
        account_weeks: Sequence[HelloFreshWeek] | None,
    ) -> dict[str, list[HelloFreshWeek] | list[str]] | None:
        """Fetch structured menu weeks from /gw/menus-service/menus.

        HAR-confirmed endpoint the web app uses for the regional menu catalog. The response
        is ``{items: [<week>...]}`` where each week carries its recipes under ``courses``.
        Note: this catalog can be large (observed ~6.6 MB for one week), so it is only used
        as a fallback when the per-week authenticated menu endpoints returned nothing.
        """
        primary = subscriptions[0] if subscriptions else None
        if primary is None:
            return None

        locale = next(
            (s.locale for s in subscriptions if s.locale),
            api_locale(self._country),
        )
        week_ids = sorted(
            {week.week_id for week in (account_weeks or []) if week.week_id}
        )
        params: dict[str, Any] = {
            "country": api_country_code(self._country),
            "locale": locale,
            "exclude": "",
        }
        if week_ids:
            params["weeks"] = ",".join(week_ids)

        try:
            response = await self._async_api_get("/gw/menus-service/menus", params=params)
            payload = await self._async_response_json(response)
        except HelloFreshError as err:
            self._record_debug_attempt(
                "menu_attempts",
                {"path": "/gw/menus-service/menus", "params": params, "error": str(err)},
            )
            return None

        raw_weeks = self._extract_menu_week_candidates(payload)
        weeks = self._normalize_menu_weeks(raw_weeks, subscription=primary)
        self._record_debug_attempt(
            "menu_attempts",
            {
                "path": "/gw/menus-service/menus",
                "params": params,
                "status": self._response_status(response),
                "payload_summary": self._summarize_payload(payload),
                "candidate_week_count": len(raw_weeks),
                "recognized_week_count": len(weeks),
            },
        )
        if not weeks:
            return None

        available_labels = [
            week.display_name for week in weeks if week.display_name
        ]
        return {"weeks": weeks, "available_labels": available_labels}

    async def _async_get_delivery_menu_week_data(
        self,
        subscription: HelloFreshSubscription,
        account_week: HelloFreshWeek,
    ) -> list[HelloFreshWeek]:
        """Load a subscribed week's full menu from the authenticated delivery menu page."""
        plan_preference = await self._async_get_subscription_plan_preference(subscription)
        params = self._build_delivery_menu_params(subscription, account_week, plan_preference)
        if params is None:
            self._record_debug_attempt(
                "menu_attempts",
                {
                    "subscription_id": subscription.subscription_id,
                    "path": "/gw/my-deliveries/menu",
                    "week_id": account_week.week_id,
                    "skipped": "missing_params",
                },
            )
            return []

        try:
            response = await self._async_api_get("/gw/my-deliveries/menu", params=params)
            payload = await self._async_response_json(response)
        except HelloFreshError as err:
            self._record_debug_attempt(
                "menu_attempts",
                {
                    "subscription_id": subscription.subscription_id,
                    "path": "/gw/my-deliveries/menu",
                    "params": params,
                    "week_id": account_week.week_id,
                    "error": str(err),
                },
            )
            return []

        # Guard against the endpoint serving a SUBSTITUTE menu for the requested week. For an
        # upcoming week the payload's top-level ``week`` matches the request; for a past week
        # the planning-menu endpoint (which the web app never calls for history) can return a
        # nearest/current menu instead. Stamping those recipes onto the requested week would
        # show the wrong meals (e.g. the current menu under a past week), so reject a mismatch
        # and let the recipes embedded in the deliveries payload stand for that week.
        payload_week = payload.get("week") if isinstance(payload, dict) else None
        if (
            isinstance(payload_week, str)
            and payload_week
            and payload_week != account_week.week_id
        ):
            self._record_debug_attempt(
                "menu_attempts",
                {
                    "subscription_id": subscription.subscription_id,
                    "path": "/gw/my-deliveries/menu",
                    "params": params,
                    "week_id": account_week.week_id,
                    "status": self._response_status(response),
                    "rejected": "week_mismatch",
                    "payload_week": payload_week,
                },
            )
            return []

        normalized_weeks = self._normalize_menu_weeks([payload], subscription=subscription)
        enriched_weeks = [
            self._overlay_menu_week_metadata(menu_week, account_week)
            for menu_week in normalized_weeks
        ]
        self._record_debug_attempt(
            "menu_attempts",
            {
                "subscription_id": subscription.subscription_id,
                "path": "/gw/my-deliveries/menu",
                "params": params,
                "week_id": account_week.week_id,
                "status": self._response_status(response),
                "payload_summary": self._summarize_payload(payload),
                "recognized_week_count": len(enriched_weeks),
            },
        )
        return enriched_weeks

    def _build_delivery_menu_params(
        self,
        subscription: HelloFreshSubscription,
        account_week: HelloFreshWeek,
        plan_preference: str | None = None,
    ) -> dict[str, Any] | None:
        """Build the query string required by the live delivery menu endpoint."""
        customer_plan_id = subscription.raw.get("customerPlanId")
        delivery_option = self._find_first_nested_value(
            account_week.raw,
            ("handle", "deliveryOptionHandle"),
        ) or self._find_first_nested_value(subscription.raw, ("handle", "deliveryOptionHandle"))
        postcode = self._find_first_nested_value(subscription.raw, ("postcode", "postalCode"))
        preference = (
            plan_preference
            or subscription.raw.get("planPreference")
            or subscription.raw.get("preset")
            or subscription.raw.get("preference")
        )
        # Prefer the WEEK's own box SKU over the subscription's base plan. A week can be resized
        # to a different box (e.g. a 2-meal box on a 3-meal plan), and HelloFresh serves that
        # week's menu — and its real selection — only when queried with the matching SKU. Using
        # the base-plan SKU returns the larger box's default/preselected view, so a resized week
        # shows the wrong meals (a 3-meal-default set instead of the 2 the customer chose).
        week_product = (
            account_week.raw.get("product")
            if isinstance(account_week.raw.get("product"), dict)
            else {}
        )
        product_sku = (
            week_product.get("handle")
            or week_product.get("sku")
            or self._find_first_nested_value(subscription.raw, ("sku",))
            or self._find_first_nested_value(account_week.raw, ("handle", "sku"))
        )
        # Servings likewise from the week's own product spec first (the box size), then the plan.
        week_specs = week_product.get("specs") if isinstance(week_product.get("specs"), dict) else {}
        servings = coerce_int(
            week_specs.get("size")
            or self._find_first_nested_value(subscription.raw, ("size", "servings", "numberOfPersons"))
            or subscription.servings
        )
        locale = subscription.locale or self._find_first_nested_value(subscription.raw, ("locale",))

        if not all(
            (
                customer_plan_id,
                delivery_option,
                postcode,
                preference,
                product_sku,
                servings,
                subscription.subscription_id,
                account_week.week_id,
                locale,
            )
        ):
            return None

        return {
            "customerPlanId": customer_plan_id,
            "delivery-option": delivery_option,
            "exclude": "",
            "exclude-feedback": "true",
            "include-filters": "true",
            "include-future-feedback": "false",
            "locale": locale,
            "postcode": postcode,
            "preference": preference,
            "product-sku": product_sku,
            "servings": str(servings),
            "subscription": subscription.subscription_id,
            "week": account_week.week_id,
        }

    async def _async_get_subscription_plan_preference(
        self,
        subscription: HelloFreshSubscription,
    ) -> str | None:
        """Return the current plan preference for a subscription when available.

        The preference (e.g. ``"quick"``) feeds the ``/gw/my-deliveries/menu`` request. As of
        the current site it lives on the **profile-service** payload at
        ``taste.plans[customerPlanId].planPreference`` (with ``taste.legacySinglePreference`` as
        a single-value fallback) — HAR-confirmed. The older ``/gw/api/subscriptions/{id}/
        product_options`` no longer carries ``unifiedPreferences`` at all (it now returns the
        product catalog `{count, items[]}`), so that call was dropped: it cost one HTTP round-trip
        per poll and always fell through to ``preset``. ``preset`` remains the final fallback and
        usually matches, but is a distinct field that can diverge from the active preference.
        """
        subscription_id = subscription.subscription_id
        if subscription_id in self._subscription_preferences:
            return self._subscription_preferences[subscription_id]

        customer_plan_id = subscription.raw.get("customerPlanId")
        preference = subscription.raw.get("planPreference") or subscription.raw.get("preset")

        if customer_plan_id:
            try:
                response = await self._async_api_get(
                    self._PROFILE_PATH, params=self._profile_params()
                )
                payload = await self._async_response_json(response)
            except HelloFreshError as err:
                self._record_debug_attempt(
                    "preference_attempts",
                    {
                        "subscription_id": subscription_id,
                        "path": self._PROFILE_PATH,
                        "error": str(err),
                    },
                )
                self._subscription_preferences[subscription_id] = preference
                return preference

            taste = payload.get("taste") if isinstance(payload, dict) else None
            taste = taste if isinstance(taste, dict) else {}
            plans = taste.get("plans") if isinstance(taste.get("plans"), dict) else {}
            plan_pref = (
                (plans.get(customer_plan_id) or {}).get("planPreference")
                if isinstance(plans.get(customer_plan_id), dict)
                else None
            )
            preference = (
                plan_pref
                or taste.get("legacySinglePreference")
                or preference
            )
            self._record_debug_attempt(
                "preference_attempts",
                {
                    "subscription_id": subscription_id,
                    "path": self._PROFILE_PATH,
                    "status": self._response_status(response),
                    "plan_preference": preference,
                },
            )

        if preference:
            subscription.raw["planPreference"] = preference
        self._subscription_preferences[subscription_id] = preference
        return preference

    async def _async_enrich_order_prices(
        self,
        subscriptions: Sequence[HelloFreshSubscription],
        weeks: Sequence[HelloFreshWeek],
        orders: Sequence[HelloFreshOrder],
    ) -> None:
        """Overlay exact cart pricing onto normalized orders when the menu payload supports it."""
        subscriptions_by_id = {
            subscription.subscription_id: subscription for subscription in subscriptions
        }
        orders_by_key = {(order.subscription_id, order.week_id): order for order in orders}

        price_tasks = []
        for week in weeks:
            if week.subscription_id is None:
                continue
            order = orders_by_key.get((week.subscription_id, week.week_id))
            subscription = subscriptions_by_id.get(week.subscription_id)
            if order is None or subscription is None:
                continue
            price_tasks.append(
                self._async_apply_order_price(
                    subscription=subscription,
                    week=week,
                    order=order,
                )
            )

        if price_tasks:
            await asyncio.gather(*price_tasks)

    async def _async_apply_order_price(
        self,
        subscription: HelloFreshSubscription,
        week: HelloFreshWeek,
        order: HelloFreshOrder,
    ) -> None:
        """Fetch and apply cart pricing for a matched subscription week/order."""
        pricing = await self._async_get_cart_price_for_week(subscription, week)
        if pricing is None or self._extract_total_price(pricing) is None:
            # Fall back to the lighter /gw/calculate endpoint when the per-week cart price
            # could not be built or returned no recognizable total.
            calculate = await self._async_get_calculate_price(subscription, week)
            if calculate is not None:
                pricing = calculate
        if pricing is None:
            return
        total_price = self._extract_total_price(pricing)
        if total_price is not None:
            order.total_price = round(total_price, 2)
        currency = self._extract_currency_code(pricing)
        if currency:
            order.currency = currency

    async def _async_get_calculate_price(
        self,
        subscription: HelloFreshSubscription,
        week: HelloFreshWeek | None = None,
    ) -> dict[str, Any] | None:
        """Fetch a box total from /gw/calculate (lighter than the cart-price endpoint).

        HAR-confirmed request shape. With ``week`` it prices that specific delivery; without
        it, the standing recurring plan (used for the plan-settings weekly price). The total
        is read with the same defensive field fallbacks used for other pricing payloads
        (``grandTotal``/``total``/``amount``/…). Treated as best-effort.
        """
        json_payload = self._build_calculate_payload(subscription, week)
        if json_payload is None:
            return None

        week_id = week.week_id if week is not None else None
        cache_key = self._request_fingerprint("/gw/calculate", None, json_payload)
        cached = self._cart_price_cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            response = await self._async_api_request(
                "POST", "/gw/calculate", json_payload=json_payload
            )
            payload = await self._async_response_json(response)
        except HelloFreshError as err:
            self._record_debug_attempt(
                "pricing_attempts",
                {
                    "subscription_id": subscription.subscription_id,
                    "week_id": week_id,
                    "path": "/gw/calculate",
                    "error": str(err),
                },
            )
            return None

        if isinstance(payload, dict):
            self._store_cart_price(cache_key, payload)
        self._record_debug_attempt(
            "pricing_attempts",
            {
                "subscription_id": subscription.subscription_id,
                "week_id": week_id,
                "path": "/gw/calculate",
                "status": self._response_status(response),
                "payload_summary": self._summarize_payload(payload),
            },
        )
        return payload if isinstance(payload, dict) else None

    def _build_calculate_payload(
        self,
        subscription: HelloFreshSubscription,
        week: HelloFreshWeek | None = None,
    ) -> dict[str, Any] | None:
        """Build the /gw/calculate request body from subscription + week metadata.

        When ``week`` is omitted the request describes the standing recurring plan (the
        "plan settings" weekly price), drawing the product handle and delivery option from
        the subscription itself -- which is what the web app posts for that view.
        """
        raw_week = week.raw if week is not None else {}
        customer_id = coerce_int(subscription.account_id)
        subscription_id = coerce_int(subscription.subscription_id)
        plan_id = subscription.raw.get("customerPlanId")
        main_product_handle = self._find_first_nested_value(
            raw_week.get("product"), ("handle", "sku")
        ) or self._find_first_nested_value(subscription.raw, ("sku",))
        # The delivery-option handle (e.g. "US-1-0800-2000") must come from the
        # subscription's deliveryOption object specifically -- a blind nested "handle"
        # search collides with productType.handle (the product SKU, "US-CBU-3-2-0"), which
        # makes /gw/calculate mis-price or reject the request and leaves the sensor unknown.
        delivery_option = (
            self._find_first_nested_value(raw_week.get("deliveryOption"), ("handle",))
            or self._find_first_nested_value(subscription.raw.get("deliveryOption"), ("handle",))
            or self._find_first_nested_value(
                subscription.raw.get("nextDeliveryOption"), ("handle",)
            )
            or self._find_first_nested_value(subscription.raw, ("deliveryOptionHandle",))
        )
        postcode = self._find_first_nested_value(subscription.raw, ("postcode", "postalCode"))
        locale = subscription.locale or self._find_first_nested_value(subscription.raw, ("locale",))

        if not all(
            (customer_id, subscription_id, plan_id, main_product_handle, delivery_option, locale)
        ):
            return None

        return {
            "isFirstOrder": False,
            "products": [{"handle": main_product_handle, "deliveryOption": delivery_option}],
            "skipOneOffCalculation": True,
            "isRecurring": True,
            "subscriptionID": subscription_id,
            "customerID": customer_id,
            "shippingAddress": {"postcode": postcode} if postcode else {},
            "planID": plan_id,
            "couponCode": None,
            "locale": locale,
            "country": api_country_code(self._country),
        }

    async def _async_get_cart_price_for_week(
        self,
        subscription: HelloFreshSubscription,
        week: HelloFreshWeek,
    ) -> dict[str, Any] | None:
        """Fetch exact pricing for a week from the cart pricing endpoint."""
        json_payload = self._build_cart_price_payload(subscription, week)
        path = f"/gw/v1/carts/{week.week_id}/price"
        params = {
            "isFutureWeek": str(self._is_future_week(week)).lower(),
        }
        if json_payload is None:
            self._record_debug_attempt(
                "pricing_attempts",
                {
                    "subscription_id": subscription.subscription_id,
                    "week_id": week.week_id,
                    "path": path,
                    "skipped": "missing_payload",
                },
            )
            return None

        cache_key = self._request_fingerprint(path, params, json_payload)
        cached = self._cart_price_cache.get(cache_key)
        if cached is not None:
            self._record_debug_attempt(
                "pricing_attempts",
                {
                    "subscription_id": subscription.subscription_id,
                    "week_id": week.week_id,
                    "path": path,
                    "cached": True,
                },
            )
            return cached

        try:
            response = await self._async_api_request(
                "POST",
                path,
                params=params,
                json_payload=json_payload,
            )
            payload = await self._async_response_json(response)
        except HelloFreshError as err:
            self._record_debug_attempt(
                "pricing_attempts",
                {
                    "subscription_id": subscription.subscription_id,
                    "week_id": week.week_id,
                    "path": path,
                    "params": params,
                    "json_payload": json_payload,
                    "error": str(err),
                },
            )
            return None

        if isinstance(payload, dict):
            self._store_cart_price(cache_key, payload)
        self._record_debug_attempt(
            "pricing_attempts",
            {
                "subscription_id": subscription.subscription_id,
                "week_id": week.week_id,
                "path": path,
                "params": params,
                "json_payload": json_payload,
                "status": self._response_status(response),
                "payload_summary": self._summarize_payload(payload),
            },
        )
        return payload

    @staticmethod
    def _request_fingerprint(
        path: str,
        params: dict[str, Any] | None,
        json_payload: dict[str, Any] | None,
    ) -> str:
        """Return a stable hash of a request's path, params, and body for caching."""
        canonical = json.dumps(
            {"path": path, "params": params or {}, "body": json_payload or {}},
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _store_cart_price(self, cache_key: str, payload: dict[str, Any]) -> None:
        """Cache a pricing payload, evicting the oldest entry past the FIFO cap."""
        self._cart_price_cache[cache_key] = payload
        self._cart_price_cache.move_to_end(cache_key)
        while len(self._cart_price_cache) > _CART_PRICE_CACHE_MAX:
            self._cart_price_cache.popitem(last=False)

    def _build_cart_price_payload(
        self,
        subscription: HelloFreshSubscription,
        week: HelloFreshWeek,
    ) -> dict[str, Any] | None:
        """Build a cart pricing payload from delivery and menu metadata."""
        menu_payload = week.raw.get("_menu_payload")
        if not isinstance(menu_payload, dict):
            return None

        customer_id = coerce_int(subscription.account_id) or subscription.account_id
        customer_plan_id = subscription.raw.get("customerPlanId")
        box_size = coerce_int(
            self._find_first_nested_value(subscription.raw, ("size", "servings", "numberOfPersons"))
            or subscription.servings
        )
        main_product_handle = self._find_first_nested_value(week.raw.get("product"), ("handle",))
        delivery_option = self._find_first_nested_value(
            week.raw.get("deliveryOption"),
            ("handle",),
        ) or self._find_first_nested_value(subscription.raw, ("handle", "deliveryOptionHandle"))
        unit_price_cents = coerce_float(
            self._find_first_nested_value(week.raw.get("product"), ("price", "unitPrice"))
        )
        box_sku = self._find_first_nested_value(subscription.raw, ("sku",)) or main_product_handle
        locale = subscription.locale or self._find_first_nested_value(subscription.raw, ("locale",))
        shipping_address = self._extract_shipping_address_payload(subscription.raw)
        selected_groups = self._extract_cart_selection_groups(menu_payload, box_sku)

        if not all(
            (
                customer_id,
                customer_plan_id,
                box_size,
                main_product_handle,
                delivery_option,
                locale,
                shipping_address,
                selected_groups,
            )
        ):
            return None

        products: list[dict[str, Any]] = [
            {
                "handle": main_product_handle,
                "deliveryOption": delivery_option,
                "hfWeek": week.week_id,
            }
        ]
        if unit_price_cents is not None:
            products[0]["unitPrice"] = (
                unit_price_cents / 100 if unit_price_cents >= 100 else unit_price_cents
            )

        for group in selected_groups:
            products.append(
                {
                    "boxSku": group["boxSku"],
                    "handle": group["handle"],
                    "hfWeek": week.week_id,
                    "quantityPerCourse": group["quantityPerCourse"],
                    "recipeIndexes": group["recipeIndexes"],
                }
            )

        return {
            "boxSize": box_size,
            "isFirstOrder": bool(
                subscription.raw.get("isFirstOrder")
                or self._find_first_nested_value(subscription.raw, ("isFirstOrder",))
            ),
            "customerID": customer_id,
            "isRecurring": bool(
                subscription.raw.get("isRecurring") if "isRecurring" in subscription.raw else True
            ),
            "subscriptionID": coerce_int(subscription.subscription_id)
            or subscription.subscription_id,
            "planID": customer_plan_id,
            "products": products,
            "shippingAddress": shipping_address,
            "locale": locale,
            "country": api_country_code(self._country),
        }

    def _extract_shipping_address_payload(self, node: Any) -> dict[str, Any] | None:
        """Extract the limited shipping address fields used by cart pricing."""
        address = self._find_first_nested_dict(node, {"shippingAddress", "address"})
        address1 = address.get("address1")
        postcode = address.get("postcode") or address.get("postalCode")
        region = address.get("region") or address.get("state")
        if not all((address1, postcode, region)):
            return None
        return {
            "address1": address1,
            "postcode": postcode,
            "region": region,
        }

    def _extract_cart_selection_groups(
        self,
        menu_payload: dict[str, Any],
        default_box_sku: str | None,
    ) -> list[dict[str, Any]]:
        """Collect selected menu items into the grouped payload expected by cart pricing."""
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for raw_item in self._find_cart_selection_candidates(menu_payload):
            index = raw_item.get("index")
            quantity = coerce_int(
                (raw_item.get("selection") or {}).get("quantity") or raw_item.get("quantity")
            )
            if index is None or quantity is None or quantity <= 0:
                continue

            charge = raw_item.get("charge") if isinstance(raw_item.get("charge"), dict) else {}
            handle = charge.get("handle") or raw_item.get("handle") or raw_item.get("sku")
            box_sku = charge.get("boxSku") or raw_item.get("boxSku") or default_box_sku
            if not handle or not box_sku:
                continue

            key = (str(handle), str(box_sku))
            group = grouped.setdefault(
                key,
                {
                    "handle": str(handle),
                    "boxSku": str(box_sku),
                    "quantityPerCourse": [],
                    "recipeIndexes": [],
                },
            )
            group["quantityPerCourse"].append({"index": index, "quantity": quantity})
            group["recipeIndexes"].append(str(index))

        return list(grouped.values())

    def _find_cart_selection_candidates(self, node: Any) -> list[dict[str, Any]]:
        """Return raw selected menu items with cart-pricing metadata."""
        candidates: list[dict[str, Any]] = []
        if isinstance(node, list):
            for item in node:
                candidates.extend(self._find_cart_selection_candidates(item))
            return candidates
        if not isinstance(node, dict):
            return candidates

        selection = node.get("selection")
        if isinstance(selection, dict) and selection.get("quantity") not in (None, 0, "0"):
            candidates.append(node)

        for key, value in node.items():
            if key in {"selection", "recipe"}:
                continue
            candidates.extend(self._find_cart_selection_candidates(value))
        return candidates

    def _is_future_week(self, week: HelloFreshWeek) -> bool:
        """Return whether a normalized week is still in the future for pricing queries."""
        if week.delivery_date is None:
            return False
        return week.delivery_date >= date.today()

    async def _async_get_public_menu_data(self) -> dict[str, list[HelloFreshWeek] | list[str]]:
        """Fetch and parse the public HelloFresh menus page."""
        # This is a top-level HTML page load, not a CORS XHR, so it gets the navigation
        # Accept/Sec-Fetch values a real browser sends for a document (overriding the XHR
        # defaults from _DEFAULT_HEADERS, which come first so these win on the shared keys).
        response = await self._session.get(
            f"{self._base_url}/menus",
            headers={
                **_DEFAULT_HEADERS,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,image/apng,*/*;q=0.8"
                ),
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        if response.status >= _HTTP_BAD_REQUEST:
            raise HelloFreshError(
                f"Failed to fetch HelloFresh public menu page: HTTP {response.status}"
            )

        html = await response.text()
        soup = BeautifulSoup(html, "html.parser")

        title_text = ""
        title_tag = soup.find(["h1", "title"])
        if title_tag is not None:
            title_text = " ".join(title_tag.get_text(" ", strip=True).split())

        recipes = self._extract_public_menu_recipes(soup)
        available_labels = extract_menu_labels(soup.get_text("\n", strip=True))
        week_label = title_text or (available_labels[0] if available_labels else "Current Menu")
        menu_week = HelloFreshWeek(
            week_id=slugify(week_label) or "current-menu",
            display_name=week_label,
            recipes=recipes,
            source="public_menu",
        )

        return {
            "weeks": [menu_week],
            "available_labels": available_labels,
        }

    def _extract_public_menu_recipes(self, soup: BeautifulSoup) -> list[HelloFreshRecipe]:
        """Extract public menu recipes from menu HTML."""
        recipe_headings = []
        for heading in soup.find_all(["h2", "h3", "h4"]):
            text = " ".join(heading.get_text(" ", strip=True).split())
            if looks_like_recipe_heading(text):
                recipe_headings.append(text)

        seen: set[str] = set()
        recipes = []
        for name in recipe_headings:
            if name in seen:
                continue
            seen.add(name)
            recipe_slug = slugify(name)
            recipes.append(
                HelloFreshRecipe(
                    recipe_id=recipe_slug,
                    name=name,
                )
            )
        return recipes

    async def _async_try_mutation_candidates(
        self,
        path_templates: Sequence[str],
        week: HelloFreshWeek,
        payload_variants: Sequence[dict[str, Any]],
        category: str = "mutation",
    ) -> None:
        """Try best-effort write endpoints until one succeeds.

        These write endpoints are NOT HAR-verified (unlike meal selection's
        ``PUT /gw/v1/carts/{week}``) — the integration probes a matrix of plausible
        ``path × payload × method`` combinations. Once a combination succeeds for a given
        ``category`` (skip/unskip/select), it is remembered and tried first next time, so a
        confirmed write path stops re-probing the dead combinations that preceded it.
        """
        if week.subscription_id is None:
            raise HelloFreshNotImplementedError(
                f"Week {week.week_id} does not expose a subscription id for a safe write operation."
            )

        combos = [
            (path_template, payload, method)
            for path_template in path_templates
            for payload in payload_variants
            for method in ("POST", "PATCH")
        ]
        preferred_key = self._preferred_endpoints.get((category, week.subscription_id or ""))
        if preferred_key is not None:
            combos.sort(key=lambda c: self._mutation_combo_key(*c) != preferred_key)

        errors: list[str] = []
        for path_template, payload, method in combos:
            path = path_template.format(
                week_id=week.week_id,
                subscription_id=week.subscription_id,
            )
            try:
                await self._async_api_request(method, path, json_payload=payload)
            except HelloFreshAuthError:
                raise
            except HelloFreshError as err:
                errors.append(f"{path} [{method}]: {err}")
                continue
            self._preferred_endpoints[(category, week.subscription_id or "")] = (
                self._mutation_combo_key(path_template, payload, method)
            )
            _LOGGER.info("HelloFresh mutation succeeded using %s %s", method, path)
            return

        raise HelloFreshNotImplementedError(
            "HelloFresh write actions were attempted, but no known endpoint accepted the request. "
            + "; ".join(errors[:6])
        )

    @staticmethod
    def _mutation_combo_key(
        path_template: str,
        payload: dict[str, Any],
        method: str,
    ) -> str:
        """Return a stable identity for a write combo (template + payload shape + method)."""
        return f"{method} {path_template} {{{','.join(sorted(payload))}}}"

    async def _async_enrich_order_tracking(
        self,
        subscriptions: Sequence[HelloFreshSubscription],
        orders: Sequence[HelloFreshOrder],
    ) -> None:
        """Overlay richer SCM shipment tracking fields onto known orders."""
        orders_by_public_id: dict[str, list[HelloFreshOrder]] = {}
        locale = next(
            (
                subscription.locale
                for subscription in subscriptions
                if isinstance(subscription.locale, str) and subscription.locale.strip()
            ),
            api_locale(self._country),
        )

        for order in orders:
            public_id = extract_tracking_public_id(order.tracking_url)
            if public_id is None:
                continue
            orders_by_public_id.setdefault(public_id, []).append(order)

        for public_id, matched_orders in orders_by_public_id.items():
            path = f"/gw/scm/tracking-ids/track/public-id/{public_id}"
            params = {
                "country": api_country_code(self._country),
                "locale": locale,
            }
            try:
                response = await self._async_api_get(
                    path,
                    params=params,
                    extra_headers={"x-requested-by": "shipping-and-tracking"},
                )
                payload = await self._async_response_json(response)
            except HelloFreshAuthError:
                raise
            except HelloFreshError as err:
                self._record_debug_attempt(
                    "tracking_attempts",
                    {
                        "public_id": public_id,
                        "path": path,
                        "params": params,
                        "error": str(err),
                    },
                )
                continue

            boxes = payload.get("boxes") or []
            applied_count = 0
            if isinstance(boxes, list):
                applied_count = self._apply_tracking_boxes_to_orders(
                    orders=matched_orders,
                    boxes=boxes,
                )

            self._record_debug_attempt(
                "tracking_attempts",
                {
                    "public_id": public_id,
                    "path": path,
                    "params": params,
                    "status": self._response_status(response),
                    "payload_summary": self._summarize_payload(payload),
                    "box_count": len(boxes) if isinstance(boxes, list) else 0,
                    "matched_order_count": len(matched_orders),
                    "updated_order_count": applied_count,
                },
            )

    def _apply_tracking_boxes_to_orders(
        self,
        orders: Sequence[HelloFreshOrder],
        boxes: Sequence[dict[str, Any]],
    ) -> int:
        """Merge SCM tracking box details into normalized orders."""
        updates = 0
        for order in orders:
            box = self._select_tracking_box_for_order(order, boxes)
            if box is None:
                continue
            details = extract_scm_tracking_details(box)
            if details["tracking_url"] is not None:
                order.tracking_url = details["tracking_url"]
            if details["tracking_number"] is not None:
                order.tracking_number = details["tracking_number"]
            if details["tracking_status"] is not None:
                order.tracking_status = details["tracking_status"]
            if details["carrier"] is not None:
                order.carrier = details["carrier"]
            updates += 1
        return updates

    def _select_tracking_box_for_order(
        self,
        order: HelloFreshOrder,
        boxes: Sequence[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Return the best matching SCM tracking box for an order."""
        if not boxes:
            return None

        matching_delivery_date = [
            box for box in boxes if parse_date(box.get("delivery_date")) == order.delivery_date
        ]
        if len(matching_delivery_date) == 1:
            return matching_delivery_date[0]
        if matching_delivery_date:
            boxes = matching_delivery_date

        if order.tracking_number:
            normalized_tracking_number = order.tracking_number.strip()
            for box in boxes:
                tracking_number = box.get("tracking_code") or box.get("tracking_id")
                if (
                    isinstance(tracking_number, str)
                    and tracking_number.strip() == normalized_tracking_number
                ):
                    return box

        return boxes[0] if boxes else None

    async def _async_api_get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> ClientResponse | AuthResponse:
        """GET an authenticated HelloFresh endpoint."""
        return await self._async_api_request(
            "GET",
            path,
            params=params,
            extra_headers=extra_headers,
        )

    async def _async_api_request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_payload: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        _allow_refresh_retry: bool = True,
    ) -> ClientResponse | AuthResponse:
        """Request an authenticated HelloFresh endpoint.

        Goes through the Chrome-TLS-impersonating transport (``async_request``) so the data
        XHRs present the same real-browser TLS/HTTP2 fingerprint as the auth POSTs; stricter
        Cloudflare regions block on that fingerprint, not on the headers. Falls back to the
        shared aiohttp session when curl_cffi is unavailable.
        """
        await self._tokens.async_ensure_fresh()
        if not self._tokens.has_token:
            raise HelloFreshAuthError("No HelloFresh access token configured")

        response = await async_request(
            self._session,
            method,
            f"{self._base_url}{path}",
            params=params,
            json_payload=json_payload,
            headers={
                **_DEFAULT_HEADERS,
                **_FEATURE_HEADERS,
                # Origin/Referer match the regional host so "Sec-Fetch-Site: same-origin"
                # (from _DEFAULT_HEADERS) is consistent with what a real in-page XHR sends.
                "Origin": self._base_url,
                "Referer": f"{self._base_url}/",
                "Authorization": self._tokens.authorization_header(),
                **(extra_headers or {}),
            },
        )

        if response.status in _AUTH_FAILURE_STATUSES:
            if _allow_refresh_retry and self._tokens.can_obtain_token:
                # Force a refresh under the manager's lock. The manager re-checks whether
                # another concurrent waiter already rotated the token, so only the first
                # 401 burns a refresh-token rotation (HelloFresh invalidates it on use).
                await self._tokens.async_force_refresh_if_unchanged(self._tokens.access_token)
                return await self._async_api_request(
                    method,
                    path,
                    params=params,
                    json_payload=json_payload,
                    extra_headers=extra_headers,
                    _allow_refresh_retry=False,
                )
            self._cached_subscriptions = None
            raise HelloFreshAuthError(
                f"HelloFresh authentication failed for {path}: HTTP {response.status}"
            )
        if response.status >= _HTTP_BAD_REQUEST:
            try:
                details = await response.text()
            except (ClientError, UnicodeDecodeError):  # pragma: no cover - defensive
                details = ""
            detail_text = f" HTTP {response.status}"
            if details:
                detail_text = f"{detail_text} ({details[:200]})"
            raise HelloFreshError(f"HelloFresh request failed for {path}:{detail_text}")

        return response

    # ------------------------------------------------------------------
    # Token delegation
    #
    # All token state and the /gw auth calls live in self._tokens (TokenManager). These thin
    # delegations preserve the attribute/method surface that diagnostics, __init__, and the
    # test-suite read directly off the client (e.g. client._access_token,
    # client._async_login). New code should prefer self._tokens.* directly.
    # ------------------------------------------------------------------

    @property
    def _access_token(self) -> str:
        return self._tokens._access_token

    @property
    def _refresh_token(self) -> str:
        return self._tokens._refresh_token

    @property
    def _refresh_token_issued_at(self) -> int | None:
        return self._tokens._refresh_token_issued_at

    @property
    def _refresh_expires_in(self) -> int | None:
        return self._tokens._refresh_expires_in

    @property
    def _token_issued_at(self) -> int | None:
        return self._tokens._token_issued_at

    @property
    def _token_expires_in(self) -> int | None:
        return self._tokens._token_expires_in

    @property
    def _has_credentials(self) -> bool:
        return self._tokens._has_credentials

    def _refresh_token_expired(self) -> bool:
        return self._tokens._refresh_token_expired()

    def _token_expiring_soon(self) -> bool:
        return self._tokens._token_expiring_soon()

    def _access_token_still_valid(self) -> bool:
        return self._tokens._access_token_still_valid()

    async def _async_refresh_access_token(self, force: bool) -> None:
        await self._tokens._async_refresh_access_token(force=force)

    async def _async_refresh_with_token(self) -> None:
        await self._tokens._async_refresh_with_token()

    async def _async_login(self, force: bool) -> None:
        await self._tokens._async_login(force=force)

    async def _async_fetch_app_token(self) -> None:
        await self._tokens._async_fetch_app_token()

    async def _async_response_json(
        self, response: ClientResponse | AuthResponse
    ) -> dict[str, Any]:
        """Decode a JSON response."""
        try:
            return await response.json(content_type=None)
        except (ClientError, ValueError) as err:  # pragma: no cover - defensive
            raise HelloFreshError(f"Failed to decode HelloFresh response JSON: {err}") from err

    @property
    def base_url(self) -> str:
        """Return the configured regional base URL."""
        return self._base_url

    @staticmethod
    def _response_status(response: Any) -> int | None:
        """Return a response's HTTP status, or None when it is unavailable."""
        return getattr(response, "status", None)
