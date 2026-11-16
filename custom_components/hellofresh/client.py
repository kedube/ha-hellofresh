"""HelloFreshClient: the HelloFresh account client, composed of one mixin per area.

The client's state and HTTP plumbing live in ``client_core``; each area of HelloFresh's API
(cart, delivery, menus, billing, ...) has its own ``client_<area>`` module. This module keeps
the composed class, the poll that assembles the account snapshot, the subscriptions, and the
token surface the rest of the integration reads.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
import logging
from typing import Any

from .client_billing import BillingClientMixin
from .client_cart import CartClientMixin
from .client_catalog import CatalogClientMixin
from .client_core import _ALWAYS_RAISED, _FALLBACK_STATUSES, _refused_with
from .client_delivery import DeliveryClientMixin
from .client_favorites import FavoritesClientMixin
from .client_history import HistoryClientMixin
from .client_menus import MenusClientMixin
from .client_pricing import PricingClientMixin
from .client_profile import ProfileClientMixin
from .client_snapshot import SnapshotClientMixin
from .client_tracking import TrackingClientMixin
from .client_wallet import WalletClientMixin
from .const import api_country_code, api_locale
from .models import (
    HelloFreshAccountData,
    HelloFreshError,
    HelloFreshNoSubscriptionError,
    HelloFreshNotImplementedError,
    HelloFreshOrder,
    HelloFreshSubscription,
    HelloFreshWeek,
)
from .parsers import _seg, coerce_int, local_today
from .tls_transport import async_close_shared_session
from .token_manager import (
    _TOKEN_MIN_REMAINING_BEFORE_REFRESH,  # noqa: F401 - re-exported for back-compat imports
    _TOKEN_REFRESH_AT_LIFETIME_FRACTION,  # noqa: F401 - re-exported for back-compat imports
    _token_fingerprint,  # noqa: F401 - re-exported (used by __init__.py and tests)
)

_LOGGER = logging.getLogger(__name__)


class HelloFreshClient(
    FavoritesClientMixin,
    PricingClientMixin,
    CartClientMixin,
    DeliveryClientMixin,
    ProfileClientMixin,
    CatalogClientMixin,
    MenusClientMixin,
    HistoryClientMixin,
    BillingClientMixin,
    WalletClientMixin,
    TrackingClientMixin,
    SnapshotClientMixin,
):
    """Client wrapper for HelloFresh account access."""

    @property
    def last_account_data(self) -> HelloFreshAccountData | None:
        """The newest account snapshot this client holds, or None before the first poll.

        A poll stores what it returns here. A write that could re-read what it changed stores
        a NEW object built from the previous one with that part replaced (see
        ``_publish_snapshot``); when it couldn't, the previous object stays. So a caller
        holding the previous snapshot can tell the two apart by identity: a different object is
        fresh enough to show, the same object means only a poll will show the write.
        """
        return self._last_account_data

    async def async_close(self) -> None:
        """Release the connections and cookies this client's HTTP session owns.

        Safe to call more than once. The aiohttp session itself belongs to whoever created it
        (Home Assistant closes a config entry's session through its own cleanup hook); this
        closes the browser-impersonating curl sessions pooled for it.
        """
        # A token refresh still in flight gets to finish first: its new refresh token must be
        # saved, and closing the sessions under it would lose it.
        await self._tokens.async_close()
        if self._session is not None:
            await async_close_shared_session(self._session)

    async def async_validate_credentials(self) -> dict[str, Any]:
        """Validate configured bearer token against a real account endpoint."""
        subscription = await self._async_get_primary_subscription()
        return subscription.as_dict()

    async def async_email_registered(self, email: str) -> bool | None:
        """Return whether HelloFresh has an account for this email in this country, if known."""
        return await self._tokens.async_email_registered(email)

    async def async_ensure_token_fresh(self) -> None:
        """Proactively refresh the access token when it is near expiry and in use.

        Safe to call on a timer independent of data polling. Does nothing when no
        refresh token is configured, the current token still has comfortable life, or no
        request has used the token since it was issued (the next request renews it then).
        """
        await self._tokens.async_refresh_if_in_use()

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

    @property
    def has_refresh_token(self) -> bool:
        """Return True when a refresh token is held."""
        return self._tokens.has_refresh_token

    @property
    def has_credentials(self) -> bool:
        """Return True when an email and password are stored for signing in."""
        return self._tokens.has_credentials

    async def async_get_account_data(self) -> HelloFreshAccountData:
        """Fetch account orders, weeks, and menu data."""
        data = HelloFreshAccountData()
        self._reset_debug_trace()
        self._cached_subscriptions = None
        self._subscription_preferences = {}
        self._preference_resolutions = {}
        self._shared_get_tasks = {}
        public_menu_loaded = False

        # Only an account that has no subscription continues without one, to the public menu.
        # Any other failure (a timeout, a 5xx, a rate limit, a bot block) is raised: the
        # menu-only result it used to produce replaced the account's data with an empty shell
        # for a whole poll interval.
        try:
            subscriptions = await self._async_get_subscriptions()
        except HelloFreshNoSubscriptionError as err:
            _LOGGER.warning("HelloFresh account data unavailable, showing the menu only: %s", err)
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
        subscriptions: list[HelloFreshSubscription],
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

        # Stage 1: five independent enrichments. Each hits a DIFFERENT endpoint and writes a
        # disjoint set of `data` fields while reading none of them, so awaiting them one after
        # another only stacked their latency. Run through the bounded gather (not a bare
        # asyncio.gather) so one endpoint's failure degrades just its own fields instead of
        # cancelling its siblings, while a HelloFreshAuthError still propagates for reauth.
        await self._async_gather_bounded(
            [
                self._async_enrich_subscription_payment_dates(subscriptions, all_orders, data),
                self._async_enrich_account_credit(data, subscriptions),
                self._async_enrich_selected_plan_price(data, subscriptions),
                self._async_enrich_next_box_price_breakdown(data, subscriptions, all_weeks),
                self._async_enrich_payment_method_status(data),
            ],
            limit=5,
        )
        # Stage 2: must follow stage 1 — its currency hint falls back through
        # next_delivery_total_currency / selected_plan_total_price_currency /
        # account_credit_currency, all of which stage 1 writes.
        await self._async_enrich_wallet_benefits(data, subscriptions, all_weeks)

        all_weeks = self._backfill_account_weeks_from_subscriptions(
            subscriptions=subscriptions,
            weeks=all_weeks,
        )

        # The menu stage is the poll's longest. Carrier tracking (it reads only the orders)
        # and the cookbook listing (it reads only the cookbook) don't wait for it: they run
        # beside it, and the hearts are set once the menus are in.
        async def _menu_stage() -> tuple[list[HelloFreshWeek], bool]:
            weeks = all_weeks
            menu_result = await self._async_get_account_menu_data(subscriptions, weeks)
            if menu_result is None:
                if self._enable_public_menu_fallback:
                    return weeks, await self._async_load_public_menu_fallback(data)
                return weeks, False
            data.public_menu_weeks = menu_result["weeks"]
            data.available_menu_labels = menu_result["available_labels"]
            data.capabilities.supports_account_menu_api = True
            weeks = self._merge_menu_weeks_into_account_weeks(
                account_weeks=weeks,
                menu_weeks=menu_result["weeks"],
            )
            await self._async_enrich_order_prices(
                subscriptions=subscriptions,
                weeks=weeks,
                orders=all_orders,
            )
            return weeks, False

        (all_weeks, public_menu_loaded), _, cookbook = await self._async_gather_or_cancel(
            _menu_stage(),
            self._async_enrich_order_tracking(subscriptions=subscriptions, orders=all_orders),
            self._async_list_cookbook_for_poll(),
        )

        # Past delivery weeks carry the meals that were ACTUALLY delivered (from the
        # past-deliveries endpoint, keyed by week). The planning-menu endpoint above does not
        # serve real history — for a past week it returns nothing usable or a substitute menu
        # we reject — so the delivered recipes are the authoritative source for those weeks.
        all_weeks = self._merge_past_delivery_recipes_into_account_weeks(
            account_weeks=all_weeks,
            past_delivery_weeks=data.past_delivery_weeks,
        )

        # Same idea for the Market: past-deliveries records the add-ons a shipped week actually
        # came with, and beyond the menu-grace window it is the ONLY record (the browsable
        # catalog is gone with the menu). Runs before _apply_market_items so a purchased list
        # wins over any catalog still attached to the week.
        self._merge_past_delivery_market_items(
            account_weeks=all_weeks,
            past_delivery_weeks=data.past_delivery_weeks,
        )

        # Sold-out availability is only reported by the menus-service catalog, which the
        # primary per-week menu endpoint never carries. Overlay it for the weeks where it
        # can actually change a decision — see _async_apply_menu_availability.
        await self._async_apply_menu_availability(
            subscriptions=subscriptions,
            weeks=all_weeks,
        )

        # Universal pass: resolve each recipe's variant modifier ("2x Bacon", ...) from its
        # week's modularity block, regardless of which path built the recipes. Done here, after
        # the merge, so every assembled week is covered (not just the menu-API path).
        self._apply_variation_titles(all_weeks)
        # Likewise attach each week's Market add-on catalog (appetizers, sides, desserts, ...).
        self._apply_market_items(all_weeks)
        # ...and the website's menu sections (This Week's Menu, Family Menu, Bestsellers, ...),
        # which the meal-planner card offers as a section filter.
        self._apply_menu_categories(all_weeks)
        # ...and the website's filter-panel definitions (cuisine, dish type, ingredients to
        # avoid, ...), which the card resolves server-side via async_get_menu_courses.
        self._apply_menu_filters(all_weeks)
        # Universal pass: a PAUSED week never shipped. Any "selected" meals on it are just the
        # system's auto-fill placeholders, so clear the selection (the catalog still shows for
        # browsing). Done last so it overrides whatever any merge path marked.
        self._clear_paused_week_selection(all_weeks)
        # Universal pass: flag which meals are bookmarked in the customer's cookbook. Runs last
        # so every assembled week is covered, and is best-effort — the cookbook is decoration,
        # so an outage there leaves is_favorite as None (renders no heart) rather than failing.
        if cookbook is not None:
            self._apply_cookbook_favorites(all_weeks, *cookbook)

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
        """Fetch independent account payloads concurrently after subscriptions load.

        When one raises (a rejected sign-in, a rate limit) the others are cancelled rather
        than left sending requests for a poll that has already failed.
        """
        boxes_received, past_delivery_weeks, *delivery_results = await self._async_gather_or_cancel(
            self._async_get_boxes_received(),
            self._async_get_past_delivery_weeks(subscriptions),
            *(self._async_get_upcoming_deliveries(subscription) for subscription in subscriptions),
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

    async def _async_get_subscription_for_week(
        self,
        week: HelloFreshWeek,
    ) -> HelloFreshSubscription:
        """Return the subscription that owns a known week."""
        if week.subscription_id is None:
            raise HelloFreshNotImplementedError(
                f"Week {week.week_id} does not expose a subscription id, so the matching "
                "subscription cannot be loaded."
            )
        subscription = self._known_subscription(week.subscription_id)
        if subscription is not None:
            return subscription
        for subscription in await self._async_get_subscriptions():
            if subscription.subscription_id == week.subscription_id:
                return subscription
        # The id stays out of the message: exception text is logged, and users attach logs to
        # public issues.
        raise HelloFreshNotImplementedError(
            f"Week {week.week_id} belongs to a subscription this account no longer lists."
        )

    def _known_subscription(self, subscription_id: str | None) -> HelloFreshSubscription | None:
        """Return a subscription this client already holds, without asking HelloFresh."""
        holders = (
            self._cached_subscriptions or [],
            self._last_account_data.subscriptions if self._last_account_data else [],
        )
        for subscriptions in holders:
            for subscription in subscriptions:
                if subscription.subscription_id == subscription_id:
                    return subscription
        return None

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
        # The id stays out of the message: exception text is logged, and logs get shared.
        raise HelloFreshError("HelloFresh has no such subscription on this account")

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
            raise HelloFreshNoSubscriptionError("This HelloFresh account has no subscription")
        return subscriptions[0]

    async def _async_get_subscriptions(self) -> list[HelloFreshSubscription]:
        """Fetch subscription and locale details for the current account."""
        if self._cached_subscriptions is not None:
            return self._cached_subscriptions
        subscriptions = await self._async_fetch_subscriptions()
        self._cached_subscriptions = subscriptions
        return subscriptions

    async def _async_fetch_subscriptions(self) -> list[HelloFreshSubscription]:
        """Read the account's subscriptions from HelloFresh, whatever is cached.

        Raises HelloFreshNoSubscriptionError for an account that has none (a valid sign-in),
        and any other failure as the HelloFreshError it is.
        """
        payload = await self._async_get_json(
            "/gw/api/customers/me/subscriptions", trace_category="profile_attempts"
        )
        items = payload.get("items")
        if items is not None and not isinstance(items, list):
            raise HelloFreshError("HelloFresh answered the subscriptions request in another shape")
        rows = [item for item in items or [] if isinstance(item, dict)]
        if not rows:
            raise HelloFreshNoSubscriptionError("This HelloFresh account has no subscription")
        return [self._subscription_from_raw_subscription(item) for item in rows]

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
        # HelloFresh's Android app asks the attributes service for one customer
        # (``customer_id``, the ``id`` from ``/info``), with country and locale as on every call.
        customer_id: Any = next(
            (s.account_id for s in (self._cached_subscriptions or []) if s.account_id), None
        )
        for path in candidate_paths:
            regional = {"country": api_country_code(self._country), "locale": locale}
            if path == "/gw/api/customers/me/info":
                params: dict[str, Any] | None = regional
            elif customer_id is not None:
                params = {"customer_id": str(customer_id), **regional}
            else:
                params = None
            candidate = (path, params)
            if path != candidate_paths[0] and self._endpoint_is_dead("profile", None, candidate):
                continue
            try:
                payload = await self._async_get_json(
                    path,
                    params,
                    trace_category="profile_attempts",
                    extra_headers=(
                        {"x-requested-by": "client-platform"}
                        if path == "/gw/api/customers/me/info"
                        else None
                    ),
                )
            except HelloFreshError as err:
                if not _refused_with(err, *_FALLBACK_STATUSES):
                    # Auth, a block or a rate limit ends the poll; a server error or a timeout
                    # ends only this read, rather than adding a second request to the load.
                    if isinstance(err, _ALWAYS_RAISED):
                        raise
                    break
                if path != candidate_paths[0]:
                    self._mark_endpoint_dead("profile", None, candidate)
                continue

            if path == "/gw/api/customers/me/info":
                customer_id = payload.get("id") or customer_id
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

    async def _async_get_upcoming_deliveries(
        self,
        subscription: HelloFreshSubscription,
    ) -> tuple[list[HelloFreshWeek], list[HelloFreshOrder]]:
        """Try likely upcoming-deliveries endpoints and normalize the first success.

        The next candidate is tried only when one answers 404 or 405, which says the route
        doesn't exist; such a fallback then rests for a day. A server error or a timeout ends
        the read for this poll, and a rate limit, a block or a rejected sign-in is raised.
        """
        today = local_today()
        iso = today.isocalendar()
        iso_week = f"{iso.year}-W{iso.week:02d}"
        subscription_id = subscription.subscription_id
        # The HAR-verified endpoint the live US web app actually uses is the ranged
        # ``/gw/api/customers/me/deliveries`` (it returns past + future weeks in one call),
        # so it is tried first. The remaining candidates got zero hits in the capture and
        # are kept only as drift/other-region fallbacks; the sticky-endpoint cache means a
        # working endpoint is reused on later polls instead of re-probing this whole list.
        verified = self._verified_deliveries_request()
        candidate_calls = (
            verified,  # HAR-verified (US)
            # Unverified fallbacks (no hits in the US HAR; retained for drift/other regions):
            ("/gw/my-deliveries/upcoming-deliveries", {"subscription": subscription_id}),
            (
                "/gw/my-deliveries/upcoming-deliveries",
                {"subscription": subscription_id, "from": iso_week},
            ),
            ("/gw/my-deliveries/deliveries", {"subscription": subscription_id}),
            ("/gw/api/customers/me/deliveries", {"subscription": subscription_id}),
            # HelloFresh's Android app reads a subscription's weeks from this route (its
            # ``DeliveryDateApi``; no range means every week). It replaced a guessed
            # ``/gw/api/customers/me/subscriptions/{id}/deliveries`` that no client uses.
            (f"/gw/api/subscriptions/{_seg(subscription_id)}/deliveries", None),
        )
        ordered_calls = self._order_candidates_by_preference(
            "deliveries", subscription_id, candidate_calls
        )

        last_error: str | None = None
        for path, params in ordered_calls:
            candidate = (path, params)
            is_fallback = candidate != verified
            if is_fallback and self._endpoint_is_dead("deliveries", subscription_id, candidate):
                continue
            try:
                # Shared fetch: the ranged deliveries request carries no subscription filter,
                # so multi-subscription accounts reuse one download.
                payload = await self._async_api_get_json_shared(path, params)
            except _ALWAYS_RAISED:
                raise
            except HelloFreshError as err:
                last_error = str(err)
                if not _refused_with(err, *_FALLBACK_STATUSES):
                    # A server error or a timeout: the next candidate would only add load, and
                    # answering with no weeks would publish an account without any for a whole
                    # interval (and lose the events of that time). The poll fails instead, so
                    # the last good data is kept.
                    raise HelloFreshError(
                        f"HelloFresh upcoming deliveries unavailable: {err}", status=err.status
                    ) from err
                if is_fallback:
                    self._mark_endpoint_dead("deliveries", subscription_id, candidate)
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
                    "payload_summary": self._summarize_payload(payload),
                    "recognized_week_count": len(weeks),
                    "recognized_order_count": len(orders),
                },
            )
            if weeks:
                self._remember_preferred_endpoint("deliveries", subscription_id, (path, params))
                return weeks, orders

        if last_error:
            # No subscription id here: logs are shared in public issues.
            _LOGGER.debug("HelloFresh upcoming deliveries unavailable: %s", last_error)

        return [], []

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
    def _has_credentials(self) -> bool:
        return self._tokens._has_credentials

    def _refresh_token_expired(self) -> bool:
        return self._tokens._refresh_token_expired()

    async def _async_refresh_access_token(self, force: bool) -> None:
        await self._tokens._async_refresh_access_token(force=force)

    async def _async_login(self, force: bool) -> None:
        await self._tokens._async_login(force=force)

    @property
    def base_url(self) -> str:
        """Return the configured regional base URL."""
        return self._base_url
