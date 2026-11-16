"""The shared base of HelloFreshClient: its state, the HTTP plumbing and what every area
uses.

HelloFreshClient (client.py) is composed of mixins, one per area of HelloFresh's API, all
built on ``_ClientCore``. The core sets up the client's state in one place and holds what
the areas share: the authenticated request and its JSON decoding, the regional locale,
the probe order and rest periods of fallback routes, bounded and cancelling gathers, the
shared GET of a poll, the per-week write lock and publishing a new account snapshot.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import date
import json
import logging
from time import monotonic
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

from aiohttp import ClientError, ClientResponse, ClientSession

from .const import COUNTRY_BASE_URLS, DEFAULT_COUNTRY, DEFAULT_LOG_AUTH_DIAGNOSTICS, api_locale
from .models import (
    HelloFreshAccountData,
    HelloFreshAuthError,
    HelloFreshBotBlockedError,
    HelloFreshError,
    HelloFreshFavorite,
    HelloFreshOrder,
    HelloFreshRateLimitedError,
    HelloFreshSubscription,
    HelloFreshWeek,
)
from .normalizers import HelloFreshPayloadNormalizer, _template_debug_path
from .parsers import safe_error_summary
from .tls_transport import AuthResponse, async_request
from .token_manager import (
    _BROWSER_CLIENT_HINTS,
    _BROWSER_DEVICE_HINTS,
    _BROWSER_FETCH_HEADERS,
    _BROWSER_USER_AGENT,
    TokenManager,
    _browser_accept_language,
    _tracing_headers,
)

# Every client module logs through the logger client.py has always used, so a
# `custom_components.hellofresh.client: debug` logger setting (the README's tip for
# tracing the HTTP calls) still covers all of them.
_LOGGER = logging.getLogger(f"{__package__}.client")


# HTTP status thresholds used when interpreting HelloFresh responses.
_HTTP_BAD_REQUEST = 400
_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403
_HTTP_NOT_FOUND = 404
_HTTP_TOO_MANY_REQUESTS = 429

# Failures that end a poll (and a service call) instead of costing one piece of data: a
# rejected sign-in needs reauthentication, and after a bot-protection block or a rate limit
# every further request only adds to what HelloFresh is refusing.
_ALWAYS_RAISED = (HelloFreshAuthError, HelloFreshBotBlockedError, HelloFreshRateLimitedError)

# The answers that say a probed endpoint does not exist (or takes no such method), so the next
# candidate is worth a try. A server error, a timeout or a rate limit says nothing about the
# route, and probing on would only add requests while HelloFresh is struggling.
_FALLBACK_STATUSES = frozenset({404, 405})

# A fallback endpoint that answered 404/405 is not asked again for a day.
_DEAD_ENDPOINT_TTL = 24 * 3600.0

# Bodies above this size are parsed in a worker thread: a week's menu runs to about 1 MB and
# the regional menus to several, and json.loads on those held the event loop for tens of
# milliseconds, longer on small hardware.
_LARGE_JSON_BYTES = 256 * 1024


def _refused_with(err: HelloFreshError, *statuses: int) -> bool:
    """Return True when HelloFresh answered a request with one of ``statuses``.

    Never for the failures that end a poll (see ``_ALWAYS_RAISED``), whatever their status.
    """
    return not isinstance(err, _ALWAYS_RAISED) and err.status in statuses


def _dict_at(node: Mapping[str, Any], key: str) -> dict[str, Any]:
    """Return ``node[key]`` when it is a JSON object, else a new empty dict.

    For a payload field that should hold an object: a missing or malformed one reads as
    empty, and the result is known to be a dict wherever it is used next.
    """
    value = node.get(key)
    return value if isinstance(value, dict) else {}


async def async_gather_or_cancel(*coros: Awaitable[Any]) -> list[Any]:
    """Await ``coros`` together; when one raises, cancel the rest and raise its error.

    A bare ``asyncio.gather`` raises the first error but leaves the others running, still
    sending requests for a poll (or a service call) that has already failed.
    """
    tasks = [asyncio.ensure_future(coro) for coro in coros]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


# How many callers are waiting on each shared task (see _async_await_shared). Weak, so a
# finished task is not kept alive by its count.
_SHARED_TASK_WAITERS: WeakKeyDictionary[asyncio.Task[Any], int] = WeakKeyDictionary()


def _read_outcome(task: asyncio.Task[Any]) -> None:
    """Mark a shared task's failure as seen.

    A cancelled waiter's shield lets go of the task, so a failure that lands after every
    waiter has gone would otherwise be logged by asyncio as an exception nobody retrieved.
    """
    if not task.cancelled():
        task.exception()


def _start_shared_task[T](coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
    """Start a task that several callers will wait on through ``_async_await_shared``."""
    task = asyncio.get_running_loop().create_task(coro)
    task.add_done_callback(_read_outcome)
    return task


def _shared_task_joinable(task: asyncio.Task[Any]) -> bool:
    """Return whether a cached shared task can still be joined: it has not been cancelled."""
    return not task.cancelled() and not task.cancelling()


async def _async_await_shared[T](task: asyncio.Task[T]) -> T:
    """Wait on a task other callers share, so that cancelling one caller cancels only it.

    Awaiting a shared task directly passes a caller's cancellation on to the task, and so to
    every other caller waiting on it: a meal change stopped by its automation failed the poll
    that shared its plan-preference lookup with ``CancelledError``. The task itself is
    cancelled only when its last waiter leaves, so a fetch nobody waits for any more still
    stops instead of running on.
    """
    _SHARED_TASK_WAITERS[task] = _SHARED_TASK_WAITERS.get(task, 0) + 1
    try:
        return await asyncio.shield(task)
    finally:
        remaining = _SHARED_TASK_WAITERS.pop(task, 1) - 1
        if remaining:
            _SHARED_TASK_WAITERS[task] = remaining
        elif not task.done():
            task.cancel()


# Browser headers of a same-origin fetch() XHR. Accept is fetch's default, which nearly every
# /gw call in the captures used (one micro-frontend's axios client aside).
_DEFAULT_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": _browser_accept_language(api_locale(DEFAULT_COUNTRY)),
    "User-Agent": _BROWSER_USER_AGENT,
    "Priority": "u=1, i",
    **_BROWSER_FETCH_HEADERS,
    **_BROWSER_CLIENT_HINTS,
    **_BROWSER_DEVICE_HINTS,
}

# A top-level page load, with the values Chrome sends for one (curl_cffi's chrome150 preset,
# matched by Chrome 154): typed-URL fetch metadata with no Origin or Referer, the document
# Accept and the highest priority. A normal page load sends no cache headers.
_NAVIGATION_HEADERS = {
    **_BROWSER_CLIENT_HINTS,
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": _BROWSER_USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
        "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
    "Accept-Encoding": _BROWSER_FETCH_HEADERS["Accept-Encoding"],
    "Priority": "u=0, i",
}

# Next.js data routes: the router fetches them as XHRs, unlike the pages they belong to.
_NEXT_DATA_PREFIX = "/_next/data/"

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


class _ClientCore(HelloFreshPayloadNormalizer):
    """State and plumbing shared by every part of HelloFreshClient.

    Every mixin of the client derives from this class, so the client's state is declared,
    with its types, in ``__init__`` below, and the methods one mixin calls on another are
    declared in the ``TYPE_CHECKING`` block after it.
    """

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
        enable_favorites: bool = True,
        history_weeks: int | None = None,
        menu_grace_weeks: int | None = None,
        token_refresh_callback: Callable[[dict[str, Any]], None] | None = None,
        use_curl_cffi_headers: bool = False,
        log_auth_diagnostics: bool = DEFAULT_LOG_AUTH_DIAGNOSTICS,
    ) -> None:
        """Initialize the client."""
        self._session: ClientSession = session
        self._use_curl_cffi_headers: bool = use_curl_cffi_headers
        self._country: str = country
        # How many weeks of past history to fetch/browse. None -> the normalizer default.
        self._history_lookback_weeks: int | None = history_weeks
        # How many weeks a just-delivered week keeps its full menu. None -> normalizer default.
        self._menu_grace_weeks_option: int | None = menu_grace_weeks
        self._base_url: str = COUNTRY_BASE_URLS.get(country, COUNTRY_BASE_URLS[DEFAULT_COUNTRY])
        # All access/refresh token state and the /gw login/refresh calls live in the
        # TokenManager (composition, not inheritance). The client reads the current token /
        # auth header from it per request and asks it to refresh proactively and reactively.
        self._tokens: TokenManager = TokenManager(
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
            use_curl_cffi_headers=use_curl_cffi_headers,
            log_auth_diagnostics=log_auth_diagnostics,
        )
        self._cached_subscriptions: list[HelloFreshSubscription] | None = None
        self._subscription_preferences: dict[str, str | None] = {}
        # In-flight plan-preference resolutions, so 6-wide concurrent menu fetches share ONE
        # preference GET per subscription instead of each issuing its own (thundering herd).
        self._preference_resolutions: dict[str | None, asyncio.Task[str | None]] = {}
        # Poll-scoped shared GETs: identical large requests (the ranged deliveries payload is
        # needed by both the upcoming and the history paths, for every subscription) resolve
        # to one HTTP fetch per poll instead of N+1.
        self._shared_get_tasks: dict[
            tuple[str, tuple[tuple[str, Any], ...]], asyncio.Task[Any]
        ] = {}
        self._enable_public_menu_fallback: bool = enable_public_menu_fallback
        # Whether each poll spends one extra batched request resolving cookbook bookmark state
        # for the menu's recipes. The favorite add/remove services work regardless.
        self._enable_favorites: bool = enable_favorites
        self._last_account_data: HelloFreshAccountData | None = None
        self._debug_trace: dict[str, list[dict[str, Any]]] = {}
        # Remembers which probed endpoint last produced a usable payload, keyed by
        # (category, subscription_id). The integration was built to probe a list of
        # candidate endpoints in order because the HAR captures didn't confirm which one
        # works per account; once one succeeds, recording it lets the next poll try the
        # winner first (and skip the doomed 404/403 probes that precede it). This persists
        # ACROSS polls — it is intentionally not cleared by the per-poll debug-trace reset.
        self._preferred_endpoints: dict[tuple[str, str], str] = {}
        # Fallback endpoints that answered 404/405, keyed by (category, subscription id,
        # candidate) with the monotonic time they may be tried again. Without it a subscription
        # whose weeks the verified endpoint doesn't list re-probed the same dead routes on
        # every poll.
        self._dead_endpoints: dict[tuple[str, str, str], float] = {}
        # Caches cart-pricing responses per identical request, with the monotonic time each
        # was stored. The body names the cart, not the price list, vouchers or fees, so an
        # entry expires (client_pricing._CART_PRICE_TTL) and every successful write empties
        # the cache. FIFO-bounded to client_pricing._CART_PRICE_CACHE_MAX.
        self._cart_price_cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        # One lock per (subscription id, week id), held across each write to that week. A
        # cart write re-reads the week under it, so a second write waits for the first and
        # builds on its result instead of on the last poll.
        self._week_locks: dict[tuple[str | None, str], asyncio.Lock] = {}
        # The regional catalog's sold-out flags per week: (monotonic time, recipe id ->
        # (is_sold_out, is_hidden)).
        self._availability_cache: dict[str, tuple[float, dict[str, tuple[bool, bool]]]] = {}
        # Delivered-history weeks per (subscription id, week id): (monotonic time, the raw
        # week as past-deliveries returned it, the next older week's id). The links let a
        # poll follow history it already walked without asking for those pages again.
        self._history_cache: dict[tuple[str, str], tuple[float, dict[str, Any], str | None]] = {}
        # Catalog documents by (page, params): (monotonic time, the parts of the page read).
        self._catalog_cache: OrderedDict[tuple[str, str], tuple[float, Any]] = OrderedDict()
        # Until this monotonic time the catalog goes straight to the page HTML.
        self._next_data_paused_until: float = 0.0
        # What the last orders-ledger read added up to, for the spending view: (monotonic
        # time, price per (subscription, delivery date), discount per same key, truncated).
        self._orders_ledger: (
            tuple[
                float,
                dict[tuple[str, date], tuple[float, str | None]],
                dict[tuple[str, date], tuple[float, str | None]],
                bool,
            ]
            | None
        ) = None
        # The public recipe catalog is served from Next.js data URLs that embed the site's
        # current build id. It rotates on every HelloFresh web deploy, so it is scraped once
        # and re-scraped only when a catalog request 404s (see _async_get_catalog_json).
        self._build_id: str | None = None

    if TYPE_CHECKING:
        # Methods one mixin calls on another (or on HelloFreshClient in client.py). They are
        # declared for the type checker only, so nothing is added to the class at run time:
        # each mixin is checked against the composed client, and mypy checks every
        # implementation against its declaration here.

        # client.py: the subscriptions and the weeks the client holds.
        def _get_known_week_or_raise(self, week_id: str) -> HelloFreshWeek: ...

        def _known_subscription(
            self, subscription_id: str | None
        ) -> HelloFreshSubscription | None: ...

        async def _async_get_subscriptions(self) -> list[HelloFreshSubscription]: ...

        async def _async_fetch_subscriptions(self) -> list[HelloFreshSubscription]: ...

        async def _async_get_subscription_for_week(
            self,
            week: HelloFreshWeek,
        ) -> HelloFreshSubscription: ...

        async def _async_resolve_subscription(
            self,
            subscription_id: str | None,
        ) -> HelloFreshSubscription: ...

        async def _async_get_upcoming_deliveries(
            self,
            subscription: HelloFreshSubscription,
        ) -> tuple[list[HelloFreshWeek], list[HelloFreshOrder]]: ...

        # client_cart.py
        @classmethod
        def _sku_for_meal_count(cls, product_sku: str, meal_count: int) -> str: ...

        # client_delivery.py
        async def async_list_plan_options(
            self,
            subscription_id: str | None = None,
        ) -> list[dict[str, Any]]: ...

        # client_profile.py
        _PROFILE_PATH: str

        def _profile_params(self, *, source: str | None = None) -> dict[str, str]: ...

        # client_favorites.py
        async def async_get_favorite_recipe_ids(
            self, recipe_ids: Sequence[str]
        ) -> dict[str, HelloFreshFavorite]: ...

        async def _async_search_favorites(
            self, recipe_ids: Sequence[str]
        ) -> tuple[dict[str, HelloFreshFavorite], bool]: ...

        # client_menus.py
        async def _async_fetch_week_menu(
            self,
            subscription: HelloFreshSubscription,
            account_week: HelloFreshWeek,
        ) -> HelloFreshWeek: ...

        async def _async_get_subscription_plan_preference(
            self,
            subscription: HelloFreshSubscription,
        ) -> str | None: ...

        @staticmethod
        def _delivery_option_handle(raw: Any) -> str | None: ...

        # client_billing.py
        def _customer_uuid(self, subscriptions: Sequence[HelloFreshSubscription]) -> str | None: ...

        # client_pricing.py
        def _clear_price_cache(self) -> None: ...

        async def _async_get_calculate_price(
            self,
            subscription: HelloFreshSubscription,
            week: HelloFreshWeek | None = None,
            *,
            product_handle: str | None = None,
        ) -> dict[str, Any] | None: ...

        async def _async_apply_order_price(
            self,
            subscription: HelloFreshSubscription,
            week: HelloFreshWeek,
            order: HelloFreshOrder,
        ) -> None: ...

        # client_snapshot.py
        async def _async_read_account_weeks(
            self, subscription: HelloFreshSubscription
        ) -> tuple[list[HelloFreshWeek], list[HelloFreshOrder]]: ...

        async def _async_refresh_week_after_write(
            self, subscription: HelloFreshSubscription, week: HelloFreshWeek
        ) -> None: ...

        async def _async_refresh_subscription_after_write(
            self,
            subscription: HelloFreshSubscription,
            *,
            delivery_time: str | None = None,
        ) -> None: ...

        def _detached_week(self, week: HelloFreshWeek, drop_menu: bool) -> HelloFreshWeek: ...

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

    def _endpoint_is_dead(
        self,
        category: str,
        subscription_id: str | None,
        candidate: tuple[str, dict[str, Any] | None],
    ) -> bool:
        """Return True while a fallback candidate is resting after a 404/405."""
        key = (category, subscription_id or "", self._candidate_key(candidate))
        until = self._dead_endpoints.get(key)
        if until is None:
            return False
        if monotonic() >= until:
            del self._dead_endpoints[key]
            return False
        return True

    def _mark_endpoint_dead(
        self,
        category: str,
        subscription_id: str | None,
        candidate: tuple[str, dict[str, Any] | None],
    ) -> None:
        """Rest a fallback candidate that answered 404/405 for ``_DEAD_ENDPOINT_TTL``."""
        key = (category, subscription_id or "", self._candidate_key(candidate))
        self._dead_endpoints[key] = monotonic() + _DEAD_ENDPOINT_TTL

    def _week_lock(self, week: HelloFreshWeek) -> asyncio.Lock:
        """Return the lock that serializes writes to one subscription's week."""
        key = (week.subscription_id, week.week_id)
        lock = self._week_locks.get(key)
        if lock is None:
            lock = self._week_locks[key] = asyncio.Lock()
        return lock

    def _locale_for_country(self) -> str:
        """Return the locale of the primary subscription, defaulting to en-<CC>."""
        for subscription in self._cached_subscriptions or []:
            if subscription.locale:
                return subscription.locale
        return api_locale(self._country)

    def restore_account_data(self, data: HelloFreshAccountData | None) -> None:
        """Make ``data`` the snapshot later writes build on: the one the coordinator shows.

        Every poll's result is kept as that snapshot, including one the coordinator then
        refuses (an empty account) or sets aside (a read that predates a write).
        """
        self._last_account_data = data

    def _publish_week(self, week: HelloFreshWeek, orders: Sequence[HelloFreshOrder] | None) -> None:
        """Store a snapshot with ``week`` (and, when given, its orders) replaced."""
        current = self._last_account_data
        if current is None:
            return
        key = (week.week_id, week.subscription_id)
        if not any(
            (candidate.week_id, candidate.subscription_id) == key for candidate in current.weeks
        ):
            return
        weeks = [
            week if (candidate.week_id, candidate.subscription_id) == key else candidate
            for candidate in current.weeks
        ]
        new_orders: list[HelloFreshOrder] | None = None
        if orders is not None:
            new_orders = [
                order for order in current.orders if (order.week_id, order.subscription_id) != key
            ]
            new_orders.extend(orders)
        self._publish_snapshot(weeks=weeks, orders=new_orders)

    def _publish_snapshot(
        self,
        *,
        weeks: Iterable[HelloFreshWeek] | None = None,
        orders: Iterable[HelloFreshOrder] | None = None,
        subscriptions: Iterable[HelloFreshSubscription] | None = None,
    ) -> None:
        """Store a new finalized snapshot: the current one with the given lists replaced."""
        current = self._last_account_data
        if current is None:
            return
        snapshot = replace(
            current,
            weeks=list(current.weeks if weeks is None else weeks),
            orders=list(current.orders if orders is None else orders),
            subscriptions=list(current.subscriptions if subscriptions is None else subscriptions),
            past_delivery_weeks=list(current.past_delivery_weeks),
            public_menu_weeks=list(current.public_menu_weeks),
            available_menu_labels=list(current.available_menu_labels),
            wallet_benefits=list(current.wallet_benefits),
            payment_failures=list(current.payment_failures),
        )
        self._last_account_data = snapshot.finalize()

    async def _async_api_get_json_shared(
        self,
        path: str,
        params: dict[str, Any] | None,
        *,
        trace_category: str = "delivery_attempts",
    ) -> Any:
        """GET + JSON-decode with per-poll request coalescing.

        Identical ``(path, params)`` requests within one poll share a single HTTP fetch and
        one decoded payload. The ranged deliveries request is the motivating case: its
        multi-week body is needed by every subscription's read of its weeks, and was
        previously downloaded and parsed once per subscription. Callers must treat the shared
        payload as read-only. A failure is raised to every caller (see ``_async_get_json``).
        A caller that is cancelled leaves the fetch to the others; one that every caller left
        is cancelled, and the next caller fetches again.
        """
        key = (path, tuple(sorted((params or {}).items())))
        task = self._shared_get_tasks.get(key)
        if task is None or not _shared_task_joinable(task):
            task = _start_shared_task(
                self._async_get_json(
                    path, params, trace_category=trace_category, trace={"params": params}
                )
            )
            self._shared_get_tasks[key] = task
        return await _async_await_shared(task)

    def _verified_deliveries_request(self) -> tuple[str, dict[str, Any]]:
        """The ranged weeks request the website makes, for every subscription at once."""
        history_range = self._build_delivery_history_range()
        return (
            "/gw/api/customers/me/deliveries",
            {
                "rangeStart": history_range["range_start"],
                "rangeEnd": history_range["range_end"],
            },
        )

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

        A coroutine that raises an ordinary ``HelloFreshError`` resolves to ``[]`` in its slot
        rather than cancelling the whole batch — one week's failed menu fetch must not sink the
        rest of the poll (the per-week fetch already returns ``[]`` on its own errors; this
        guards unexpected ones). A rejected sign-in, a bot block or a rate limit still
        propagates (the coordinator starts reauth, or keeps the last data), as does any other
        unexpected exception (genuine bugs shouldn't be masked as an empty week list).
        """
        if not coros:
            return []
        semaphore = asyncio.Semaphore(limit or self._MENU_FETCH_CONCURRENCY)

        async def _run(coro: Any) -> Any:
            async with semaphore:
                try:
                    return await coro
                except _ALWAYS_RAISED:
                    raise
                except HelloFreshError as err:
                    _LOGGER.debug("Bounded menu fetch failed for one week: %s", err)
                    return []

        return await self._async_gather_or_cancel(*(_run(coro) for coro in coros))

    _async_gather_or_cancel = staticmethod(async_gather_or_cancel)

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
        authenticated: bool = True,
    ) -> ClientResponse | AuthResponse:
        """Request a HelloFresh endpoint (authenticated by default).

        Goes through the Chrome-TLS-impersonating transport (``async_request``) so the data
        XHRs present the same real-browser TLS/HTTP2 fingerprint as the auth POSTs; stricter
        Cloudflare regions block on that fingerprint, not on the headers. Falls back to the
        shared aiohttp session when curl_cffi is unavailable.

        ``authenticated=False`` is for the PUBLIC website (catalog pages, the build-id page,
        Next.js data routes): they need no credentials, and sending the bearer token to page
        endpoints that never asked for it widens its exposure for nothing. They are sent as
        the browser itself requests them, and the caller judges the response status: a data
        route's 404 means a rotated build id to refresh, not a failure.
        """
        self._tokens.raise_if_bot_blocked()
        # The token this request carries; a public page carries none, and its response
        # returns below before the 401 handling that reads this.
        token_used = ""
        if authenticated:
            await self._tokens.async_ensure_fresh()
            if not self._tokens.has_token:
                raise HelloFreshAuthError("No HelloFresh access token configured")
            # Captured at header-build time: the 401 handler below must compare against the
            # token this request actually USED. Reading it after the 401 arrives raced with a
            # concurrent rotation and could force a second, needless refresh-token burn.
            token_used = self._tokens.access_token
            headers = self._api_request_headers(method, extra_headers)
        else:
            headers = self._website_request_headers(path, extra_headers)
        try:
            response = await async_request(
                self._session,
                method,
                f"{self._base_url}{path}",
                params=params,
                json_payload=json_payload,
                headers=headers,
                use_curl_cffi_headers=self._use_curl_cffi_headers,
                navigation=not authenticated and not path.startswith(_NEXT_DATA_PREFIX),
            )
        except (ClientError, TimeoutError) as err:
            raise HelloFreshError(
                f"HelloFresh API request could not connect: {type(err).__name__}"
            ) from err
        await self._tokens.async_raise_if_data_blocked(response, public_page=not authenticated)
        if not authenticated:
            return response

        # 401 means the token was rejected. 403 means it was accepted but this call is not
        # allowed -- unless the token is at or near expiry, which a gateway may also report as
        # 403. Treating every 403 as expiry spent a refresh-token rotation and then started
        # reauth for an endpoint the account simply may not use.
        if response.status == _HTTP_UNAUTHORIZED or (
            response.status == _HTTP_FORBIDDEN and not self._tokens.access_token_valid()
        ):
            if _allow_refresh_retry and self._tokens.can_obtain_token:
                # Force a refresh under the manager's lock. The manager re-checks whether
                # another concurrent waiter already rotated the token, so only the first
                # 401 burns a refresh-token rotation (HelloFresh invalidates it on use).
                await self._tokens.async_force_refresh_if_unchanged(token_used)
                return await self._async_api_request(
                    method,
                    path,
                    params=params,
                    json_payload=json_payload,
                    extra_headers=extra_headers,
                    _allow_refresh_retry=False,
                )
            self._cached_subscriptions = None
            # Exception messages land in logs (UpdateFailed / HomeAssistantError print them
            # at ERROR), which are NOT run through the diagnostics redactor — so the path is
            # templated ({id} for subscription/plan/customer ids) and the body reduced to
            # its machine error fields, matching the redaction policy diagnostics enforces.
            raise HelloFreshAuthError(
                f"HelloFresh authentication failed for {_template_debug_path(path)}: "
                f"HTTP {response.status}"
            )
        if response.status >= _HTTP_BAD_REQUEST:
            try:
                details = await response.text()
            except (ClientError, UnicodeDecodeError):  # pragma: no cover - defensive
                details = ""
            summary = safe_error_summary(details) if details else None
            detail_text = f" HTTP {response.status}"
            if summary:
                detail_text = f"{detail_text} ({summary})"
            # A rate limit is a type of its own: it ends a poll rather than costing one piece
            # of data, since every further request only adds to what HelloFresh is refusing.
            error_type = (
                HelloFreshRateLimitedError
                if response.status == _HTTP_TOO_MANY_REQUESTS
                else HelloFreshError
            )
            raise error_type(
                f"HelloFresh request failed for {_template_debug_path(path)}:{detail_text}",
                status=response.status,
                detail=summary,
            )

        return response

    def _api_request_headers(
        self, method: str, extra_headers: dict[str, str] | None
    ) -> dict[str, str]:
        """Return the headers of the web app's own fetch() call to a /gw endpoint."""
        return {
            **_DEFAULT_HEADERS,
            "Accept-Language": _browser_accept_language(self._locale_for_country()),
            # The web app's API client tags every /gw call with fresh tracing ids.
            **_tracing_headers(),
            **_FEATURE_HEADERS,
            # Chrome sends Origin on a same-origin fetch() only when it is not a GET or HEAD.
            **({} if method.upper() in ("GET", "HEAD") else {"Origin": self._base_url}),
            "Referer": f"{self._base_url}/",
            "Authorization": self._tokens.authorization_header(),
            **(extra_headers or {}),
        }

    def _website_request_headers(
        self, path: str, extra_headers: dict[str, str] | None = None
    ) -> dict[str, str]:
        """Return the headers the browser sends for a public page or Next.js data route.

        A page is a top-level navigation. A data route is the Next.js router's own fetch(),
        marked with ``x-nextjs-data`` and carrying none of the /gw API client's headers.
        """
        accept_language = _browser_accept_language(self._locale_for_country())
        if path.startswith(_NEXT_DATA_PREFIX):
            return {
                **_DEFAULT_HEADERS,
                "Accept-Language": accept_language,
                "Referer": f"{self._base_url}/",
                "x-nextjs-data": "1",
                **(extra_headers or {}),
            }
        return {**_NAVIGATION_HEADERS, "Accept-Language": accept_language, **(extra_headers or {})}

    async def _async_get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        trace_category: str,
        expect: type | tuple[type, ...] = dict,
        method: str = "GET",
        json_payload: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        trace: Mapping[str, Any] | None = None,
        optional: bool = False,
    ) -> Any:
        """Request a JSON document from HelloFresh and check what came back.

        A refused request, an unreadable body or a body of another JSON type than ``expect``
        is noted under ``trace_category`` in the debug trace (with ``trace`` added to the
        entry) and raised as a HelloFreshError. With ``optional``, for a read whose data only
        decorates the account, it returns None instead. Either way a rejected sign-in, a
        bot-protection block and a rate limit are raised as they are: the poll has to stop on
        those (and the coordinator start reauth for the first), not carry on without the data.
        """
        kwargs: dict[str, Any] = {}
        if params is not None:
            kwargs["params"] = params
        if json_payload is not None:
            kwargs["json_payload"] = json_payload
        if extra_headers is not None:
            kwargs["extra_headers"] = extra_headers
        try:
            if method == "GET":
                response = await self._async_api_get(path, **kwargs)
            else:
                response = await self._async_api_request(method, path, **kwargs)
            payload = await self._async_response_json(response)
            if not isinstance(payload, expect):
                raise HelloFreshError(
                    f"HelloFresh answered {_template_debug_path(path)} with an unexpected "
                    f"{type(payload).__name__} body"
                )
        except _ALWAYS_RAISED:
            raise
        except HelloFreshError as err:
            entry: dict[str, Any] = {"path": path, **(trace or {}), "error": str(err)}
            if err.status is not None:
                entry["status"] = err.status
            self._record_debug_attempt(trace_category, entry)
            if optional:
                return None
            raise
        return payload

    async def _async_response_json(self, response: ClientResponse | AuthResponse) -> Any:
        """Decode a JSON response body.

        A large body (a week's menu runs to about 1 MB, the regional menus to several) is
        parsed in a worker thread, so decoding it doesn't hold Home Assistant's event loop.
        An empty body is None, as aiohttp reads it.
        """
        try:
            if isinstance(response, AuthResponse):
                body: str | bytes = await response.text()
            elif isinstance(response, ClientResponse):
                body = await response.read()
            else:
                json_method = getattr(response, "json", None)
                if not callable(json_method):
                    raise HelloFreshError("HelloFresh returned a response with no readable body")
                return await json_method(content_type=None)
            if not body.strip():
                return None
            return await self._async_json_loads(body)
        except (ClientError, ValueError) as err:
            raise HelloFreshError(f"Failed to decode HelloFresh response JSON: {err}") from err

    @staticmethod
    async def _async_json_loads(body: str | bytes) -> Any:
        """``json.loads``, in a worker thread when the text is large."""
        if len(body) > _LARGE_JSON_BYTES:
            return await asyncio.to_thread(json.loads, body)
        return json.loads(body)
