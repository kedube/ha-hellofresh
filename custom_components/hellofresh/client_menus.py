"""Week menus: the per-week menu read and its fallbacks, the sold-out overlay, the plan
preference the menu request needs, menu presets, the website's menu filters and the
public menu page.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
import re
from time import monotonic
from typing import Any, TypedDict

from aiohttp import ClientError
from bs4 import BeautifulSoup

from .client_core import (
    _ALWAYS_RAISED,
    _FALLBACK_STATUSES,
    _HTTP_BAD_REQUEST,
    _HTTP_NOT_FOUND,
    _async_await_shared,
    _ClientCore,
    _dict_at,
    _refused_with,
    _shared_task_joinable,
    _start_shared_task,
)
from .const import api_country_code, api_locale
from .models import (
    HelloFreshError,
    HelloFreshNoMenuError,
    HelloFreshNotImplementedError,
    HelloFreshRecipe,
    HelloFreshSubscription,
    HelloFreshWeek,
)
from .parsers import (
    _seg,
    as_utc,
    coerce_float,
    coerce_int,
    extract_menu_labels,
    local_today,
    looks_like_recipe_heading,
    slugify,
)
from .tls_transport import async_request

# The sold-out overlay's regional catalog is 3-4 MB per week, so it is asked for the weeks
# closest to their meal deadline only, and each week's answer is reused for an hour.
_AVAILABILITY_MAX_WEEKS = 2
_AVAILABILITY_TTL = 3600.0


class _MenuData(TypedDict):
    """Menu weeks read for the poll, with the menu labels they offer (a plain dict)."""

    weeks: list[HelloFreshWeek]
    available_labels: list[str]


class MenusClientMixin(_ClientCore):
    """Menu reads; mixed into HelloFreshClient."""

    async def async_get_presets(self, take: int = 150) -> list[dict[str, Any]]:
        """Return the region's menu presets (Chef's Choice, Veggie, Quick & Easy, …).

        Each preset carries its handle, display name, and description — the human-readable names
        behind a plan's ``preset`` slug. Two paths serve the identical ``{items:[…]}`` catalog
        (HAR-confirmed): the account-context ``/gw/api/presets`` (sorted by weight) is tried
        first, and the menus-service catalog only when that one answers 404 or 405. Any other
        failure is raised: it says nothing about which path works.
        """
        locale = self._locale_for_country()
        cc = api_country_code(self._country)
        # (path, params) candidates in preference order.
        candidates = (
            ("/gw/api/presets", {"country": cc.lower(), "locale": locale, "sort": "-weight"}),
            ("/gw/menus-service/presets", {"country": cc, "locale": locale, "take": str(take)}),
        )
        refusal: HelloFreshError | None = None
        for path, params in candidates:
            try:
                payload = await self._async_get_json(
                    path, params, trace_category="preference_attempts"
                )
            except HelloFreshError as err:
                if _refused_with(err, *_FALLBACK_STATUSES):
                    refusal = err
                    continue
                raise
            items = payload.get("items")
            if isinstance(items, list):
                return [p for p in items if isinstance(p, dict)]
        if refusal is not None:
            raise refusal
        raise HelloFreshError("HelloFresh returned no menu presets")

    async def async_get_menu_courses(
        self,
        week_id: str,
        filters: Mapping[str, Any] | None = None,
    ) -> list[str]:
        """Return the recipe ids on a week's menu matching HelloFresh's own menu filters.

        HAR-verified (captures 2026-09): the website's filter panel calls
        ``GET /gw/my-deliveries/courses?country=US&locale=en-US&week=<YYYY-Www>`` with the
        active filters as query params — one param per group slug, multiple values joined
        with commas (``dish-type=family-style,salad-and-bowls``) — and gets back only id
        references (``courses[]`` of ``{index, parent, recipeFamily, recipeId}``) to
        intersect with the already-loaded menu. Filtering is server-side; the response carries
        no recipe data. The group and option slugs are the ``type`` values of the week's
        ``menu_filters`` block (``cuisine``, ``dish-type``, ``exclude-allergens``, ``diet``,
        ``main-protein``, ``total-cooking-time``).

        ``filters`` maps a group slug to one slug, a comma-separated string, or a list of
        slugs. Slugs are restricted to ``[a-z0-9-]`` — anything else is rejected rather than
        forwarded — and an empty filter set is a plain unfiltered request.
        """
        week = (week_id or "").strip()
        if not re.fullmatch(r"\d{4}-W\d{2}", week):
            raise HelloFreshError(f"Invalid week_id {week_id!r}; expected YYYY-Www")

        params: dict[str, Any] = {
            "country": api_country_code(self._country),
            "locale": self._locale_for_country(),
            "week": week,
        }
        applied: dict[str, list[str]] = {}
        for key, raw_values in (filters or {}).items():
            group = str(key).strip().lower()
            if not re.fullmatch(r"[a-z0-9-]+", group) or group in params:
                raise HelloFreshError(f"Invalid filter group {key!r}")
            if isinstance(raw_values, str):
                candidates: list[Any] = raw_values.split(",")
            elif isinstance(raw_values, (list, tuple, set)):
                candidates = list(raw_values)
            elif raw_values is None:
                candidates = []
            else:
                raise HelloFreshError(f"Invalid values for filter {key!r}")
            values: list[str] = []
            for candidate in candidates:
                slug = str(candidate).strip().lower()
                if not slug:
                    continue
                if not re.fullmatch(r"[a-z0-9-]+", slug):
                    raise HelloFreshError(f"Invalid filter value {candidate!r} for {key!r}")
                if slug not in values:
                    values.append(slug)
            if values:
                applied[group] = values
                params[group] = ",".join(values)

        payload = await self._async_get_json(
            "/gw/my-deliveries/courses", params, trace_category="menu_attempts"
        )
        courses = payload.get("courses")
        recipe_ids: list[str] = []
        for course in courses if isinstance(courses, list) else []:
            if not isinstance(course, dict):
                continue
            recipe_id = course.get("recipeId") or course.get("recipe_id") or course.get("id")
            if isinstance(recipe_id, str) and recipe_id and recipe_id not in recipe_ids:
                recipe_ids.append(recipe_id)

        self._record_debug_attempt(
            "menu_attempts",
            {
                "path": "/gw/my-deliveries/courses",
                "week": week,
                "filters": applied,
                "course_count": len(recipe_ids),
            },
        )
        return recipe_ids

    @staticmethod
    def _iso_week_tuple(value: Any) -> tuple[int, int] | None:
        """Return ``(year, week)`` for a ``YYYY-Www`` ISO week, or None when it isn't one."""
        if not isinstance(value, str):
            return None
        match = re.fullmatch(r"(\d{4})-W(\d{1,2})", value.strip())
        return (int(match.group(1)), int(match.group(2))) if match else None

    async def _async_get_account_menu_data(
        self,
        subscriptions: Sequence[HelloFreshSubscription],
        account_weeks: Sequence[HelloFreshWeek] | None = None,
    ) -> _MenuData | None:
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
        grace_floor = local_today() - timedelta(weeks=self.menu_grace_weeks)

        def _needs_menu_fetch(account_week: HelloFreshWeek) -> bool:
            return account_week.delivery_date is None or account_week.delivery_date >= grace_floor

        for subscription in subscriptions:
            subscription_id = subscription.subscription_id
            subscription_weeks: list[HelloFreshWeek] = []
            seen_week_ids: set[str] = set()

            # Also skip weeks HelloFresh hasn't published a menu for yet. The subscription
            # names the last week that has one (``weekWithLatestMenu``); asking for a later
            # week answers 404 NOT_FOUND on every poll. HelloFresh's Android app skips them
            # the same way (``MenuDelegate.isWeekWithoutMenu``). Without a readable value,
            # every week is fetched as before.
            raw_latest = (
                subscription.raw.get("weekWithLatestMenu")
                if isinstance(subscription.raw, dict)
                else None
            )
            latest_menu_week = self._iso_week_tuple(raw_latest)
            fetch_weeks: list[HelloFreshWeek] = []
            for account_week in weeks_by_subscription.get(subscription_id, []):
                if not _needs_menu_fetch(account_week):
                    continue
                week_key = self._iso_week_tuple(account_week.week_id)
                if latest_menu_week and week_key and week_key > latest_menu_week:
                    self._record_debug_attempt(
                        "menu_attempts",
                        {
                            "subscription_id": subscription_id,
                            "path": "/gw/my-deliveries/menu",
                            "week_id": account_week.week_id,
                            "skipped": "menu_not_published",
                            "latest_menu_week": raw_latest,
                        },
                    )
                    continue
                fetch_weeks.append(account_week)
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
                (f"/gw/api/customers/me/subscriptions/{_seg(subscription_id)}/menu", None),
                (f"/gw/api/customers/me/subscriptions/{_seg(subscription_id)}/weeks", None),
                (f"/gw/api/customers/me/subscriptions/{_seg(subscription_id)}/menus", None),
            )
            ordered_calls = self._order_candidates_by_preference(
                "menu", subscription_id, candidate_calls
            )

            # Each answers 404 where it doesn't exist; such a route rests for a day, and a
            # server error or a timeout ends the probing for this poll.
            subscription_weeks = []
            for path, params in ordered_calls:
                candidate = (path, params)
                if self._endpoint_is_dead("menu", subscription_id, candidate):
                    continue
                try:
                    payload = await self._async_get_json(
                        path,
                        params,
                        trace_category="menu_attempts",
                        trace={"subscription_id": subscription_id, "params": params},
                    )
                except _ALWAYS_RAISED:
                    raise
                except HelloFreshError as err:
                    if not _refused_with(err, *_FALLBACK_STATUSES):
                        break
                    self._mark_endpoint_dead("menu", subscription_id, candidate)
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
    ) -> _MenuData | None:
        """Fetch structured menu weeks from /gw/menus-service/menus.

        HAR-confirmed endpoint the web app uses for the regional menu catalog. The response
        is ``{items: [<week>...]}`` where each week carries its recipes under ``courses``.
        Note: this catalog can be large (observed ~6.6 MB for one week), so it is only used
        as a fallback when the per-week authenticated menu endpoints returned nothing, and only
        for weeks still to come: a delivered week's meals come from its history.
        """
        primary = subscriptions[0] if subscriptions else None
        if primary is None:
            return None

        locale = next(
            (s.locale for s in subscriptions if s.locale),
            api_locale(self._country),
        )
        today = local_today()
        known_weeks = [week for week in (account_weeks or []) if week.week_id]
        week_ids = sorted(
            {
                week.week_id
                for week in known_weeks
                if week.delivery_date is None or week.delivery_date >= today
            }
        )
        if known_weeks and not week_ids:
            return None
        params: dict[str, Any] = {
            "country": api_country_code(self._country),
            "locale": locale,
            "exclude": "",
        }
        if week_ids:
            params["weeks"] = ",".join(week_ids)

        payload = await self._async_get_json(
            "/gw/menus-service/menus",
            params,
            trace_category="menu_attempts",
            trace={"params": params},
            optional=True,
        )
        if payload is None:
            return None

        raw_weeks = self._extract_menu_week_candidates(payload)
        weeks = self._normalize_menu_weeks(raw_weeks, subscription=primary)
        self._record_debug_attempt(
            "menu_attempts",
            {
                "path": "/gw/menus-service/menus",
                "params": params,
                "payload_summary": self._summarize_payload(payload),
                "candidate_week_count": len(raw_weeks),
                "recognized_week_count": len(weeks),
            },
        )
        if not weeks:
            return None

        available_labels = [week.display_name for week in weeks if week.display_name]
        return {"weeks": weeks, "available_labels": available_labels}

    async def _async_apply_menu_availability(
        self,
        subscriptions: Sequence[HelloFreshSubscription],
        weeks: Sequence[HelloFreshWeek],
    ) -> None:
        """Overlay ``isSoldOut``/``isHidden`` onto weeks the customer can still change.

        The two menu sources are disjoint: the primary ``/gw/my-deliveries/menu`` endpoint
        carries prices, ratings and delivery history but no availability flags, while
        ``/gw/menus-service/menus`` carries the flags and none of the rest. The fallback path
        in _async_get_account_menu_data swaps recipe lists wholesale, so a week can only ever
        have one of the two sets. This overlays the flags FIELD-BY-FIELD instead, leaving
        every other attribute of the existing recipes untouched.

        Scoped to editable weeks (``is_editable``: meal swaps allowed, not skipped, deadline
        still open) because that is the only place the flag can change an outcome — a past or
        locked week's selection cannot be altered whether or not a meal is sold out. The
        regional catalog is 3-4 MB a week, so only the (at most two) editable weeks whose meal
        deadline comes first are asked for, and each week's answer is reused for an hour.
        Best-effort throughout: a failure leaves the flags unset (meals simply show as
        available) rather than degrading the menu; only a rejected sign-in, a block or a rate
        limit is raised.
        """
        primary = subscriptions[0] if subscriptions else None
        if primary is None:
            return

        # Sold-out only matters while the selection can still change. But the flag does not
        # arrive solely through this overlay: when the per-week menu fell back to the
        # menus-service catalog, the week's own recipes already carry the catalog's native
        # `isSoldOut` — so a LOCKED week (deadline passed, box not yet delivered) or a
        # delivered week can surface a "Sold out" ribbon the customer can do nothing about.
        # Clear the flags on every week that is not editable, then re-apply them below only
        # where they can change a decision. Delivery date is checked as well because
        # `is_editable` does not look at it: a past week with no recorded deadline and a
        # lingering `mealSwap` flag still reads as editable.
        today = local_today()
        editable_weeks: list[HelloFreshWeek] = []
        for week in weeks:
            delivered = week.delivery_date is not None and week.delivery_date < today
            if week.is_editable and not delivered:
                # A meal already in the box stays the customer's once it sells out: HelloFresh
                # holds it for them and its website shows it as usual. Sold out (and hidden)
                # means it can't be added, so neither the customer's menu nor the catalog may
                # flag it.
                for recipe in week.recipes:
                    if recipe.is_selected:
                        recipe.is_sold_out = recipe.is_hidden = False
                if week.week_id and week.recipes:
                    editable_weeks.append(week)
                continue
            for recipe in week.recipes:
                recipe.is_sold_out = False
                recipe.is_hidden = False
        if not editable_weeks:
            return

        no_deadline = datetime.max.replace(tzinfo=UTC)
        target_ids: list[str] = []
        for week in sorted(
            editable_weeks,
            key=lambda week: (
                as_utc(week.selection_deadline) if week.selection_deadline else no_deadline,
                week.week_id,
            ),
        ):
            if week.week_id not in target_ids:
                target_ids.append(week.week_id)
        target_ids = target_ids[:_AVAILABILITY_MAX_WEEKS]

        now = monotonic()
        availability_by_week: dict[str, dict[str, tuple[bool, bool]]] = {}
        for week_id in target_ids:
            cached = self._availability_cache.get(week_id)
            if cached is not None and now - cached[0] < _AVAILABILITY_TTL:
                availability_by_week[week_id] = cached[1]
        missing = sorted(week_id for week_id in target_ids if week_id not in availability_by_week)
        params: dict[str, Any] = {}
        if missing:
            locale = next(
                (s.locale for s in subscriptions if s.locale),
                api_locale(self._country),
            )
            params = {
                "country": api_country_code(self._country),
                "locale": locale,
                "exclude": "",
                "weeks": ",".join(missing),
            }
            fetched = await self._async_get_availability(params)
            if fetched is not None:
                for week_id in missing:
                    # A week the catalog didn't list is remembered as such, so it isn't asked
                    # for again within the hour.
                    week_flags = fetched.get(week_id, {})
                    self._availability_cache[week_id] = (now, week_flags)
                    availability_by_week[week_id] = week_flags
        for week_id in [
            week_id
            for week_id, (stored_at, _flags) in self._availability_cache.items()
            if now - stored_at >= _AVAILABILITY_TTL
        ]:
            del self._availability_cache[week_id]

        marked = 0
        for week in editable_weeks:
            if week.week_id not in target_ids:
                continue
            availability = availability_by_week.get(week.week_id)
            if not availability:
                continue
            for recipe in week.recipes:
                flags = availability.get(recipe.recipe_id)
                if flags is None:
                    # Absent from the catalog is NOT evidence of being sold out — the two
                    # payloads can disagree on membership. Leave the recipe untouched.
                    continue
                # The customer's own menu can flag `meals[].isSoldOut` (HelloFresh's Android
                # app models and enforces it); the regional catalog must not clear that. Nor
                # flag a meal the customer already has (see above).
                sold_out, hidden = flags
                recipe.is_sold_out = not recipe.is_selected and (recipe.is_sold_out or sold_out)
                recipe.is_hidden = not recipe.is_selected and hidden
                if recipe.is_sold_out:
                    marked += 1

        self._record_debug_attempt(
            "menu_availability_attempts",
            {
                "path": "/gw/menus-service/menus",
                "params": params,
                "editable_week_count": len(editable_weeks),
                "overlaid_weeks": target_ids,
                "weeks_requested": missing,
                "catalog_week_count": len(availability_by_week),
                "sold_out_recipe_count": marked,
            },
        )

    async def _async_get_availability(
        self, params: dict[str, Any]
    ) -> dict[str, dict[str, tuple[bool, bool]]] | None:
        """Read the regional catalog's sold-out flags for the weeks ``params`` names.

        Broad by intent: the overlay is a cosmetic enrichment on top of an already-complete
        menu, so an unexpected failure here costs the sold-out flags and nothing else. Only a
        rejected sign-in, a block or a rate limit is raised.
        """
        try:
            payload = await self._async_get_json(
                "/gw/menus-service/menus",
                params,
                trace_category="menu_availability_attempts",
                trace={"params": params},
                optional=True,
            )
        except _ALWAYS_RAISED:
            raise
        except (ClientError, AttributeError, ValueError, TypeError) as err:
            self._record_debug_attempt(
                "menu_availability_attempts",
                {"path": "/gw/menus-service/menus", "params": params, "error": str(err)},
            )
            return None
        if payload is None:
            return None
        try:
            return self._extract_menu_availability(self._extract_menu_week_candidates(payload))
        except (AttributeError, ValueError, TypeError, KeyError) as err:
            self._record_debug_attempt(
                "menu_availability_attempts",
                {"path": "/gw/menus-service/menus", "parse_error": str(err)},
            )
            return None

    def _extract_menu_availability(
        self, raw_weeks: list[dict[str, Any]]
    ) -> dict[str, dict[str, tuple[bool, bool]]]:
        """Per ISO week: ``recipe_id -> (is_sold_out, is_hidden)`` from raw menu candidates.

        A deliberately light scan. The overlay needs exactly two booleans per recipe, and
        running the full week normalizer here constructed hundreds of complete recipe
        models per poll only to throw everything but these flags away. Week ids and recipe
        ids are derived exactly as the normalizer derives them (ISO ``week`` over the
        internal Mongo ``id``; ``_extract_recipe_id_from_raw_meal``), so the matching
        behaves identically. As in ``_merge_menu_weeks_into_account_weeks``, one week id
        can arrive as several product/preset variants; the variant with the most courses
        wins so a small subset cannot shadow the full catalog.
        """
        best: dict[str, dict[str, tuple[bool, bool]]] = {}
        for index, raw_week in enumerate(raw_weeks):
            week_id = str(
                raw_week.get("week")
                or raw_week.get("calendarWeek")
                or raw_week.get("id")
                or f"menu-week-{index}"
            )
            flags: dict[str, tuple[bool, bool]] = {}
            for raw_meal in self._extract_menu_week_recipe_candidates(raw_week):
                recipe_id = self._extract_recipe_id_from_raw_meal(raw_meal)
                if recipe_id:
                    flags[recipe_id] = (
                        bool(raw_meal.get("isSoldOut")),
                        bool(raw_meal.get("isHidden")),
                    )
            existing = best.get(week_id)
            if flags and (existing is None or len(flags) > len(existing)):
                best[week_id] = flags
        return best

    _WEEK_MENU_PATH = "/gw/my-deliveries/menu"

    async def _async_get_delivery_menu_week_data(
        self,
        subscription: HelloFreshSubscription,
        account_week: HelloFreshWeek,
    ) -> list[HelloFreshWeek]:
        """Load a subscribed week's full menu for the poll; ``[]`` when there is none.

        Best effort like the rest of the menu stage, except that a rejected sign-in, a block
        or a rate limit is raised (see ``_async_fetch_week_menu``).
        """
        try:
            return [await self._async_fetch_week_menu(subscription, account_week)]
        except _ALWAYS_RAISED:
            raise
        except HelloFreshError:
            return []

    async def _async_fetch_week_menu(
        self,
        subscription: HelloFreshSubscription,
        account_week: HelloFreshWeek,
    ) -> HelloFreshWeek:
        """Read one week's menu, with the customer's current choices, from HelloFresh.

        The verified ``GET /gw/my-deliveries/menu``, which the poll makes for every week and a
        cart write makes again for its week. Returns the menu week, its metadata taken from
        ``account_week``. Raises HelloFreshNotImplementedError when the request can't be built
        (naming what is missing), HelloFreshNoMenuError when HelloFresh has no menu for the
        week yet (404, or no meals), and HelloFreshError when the request fails or answers
        with another week's menu.
        """
        plan_preference = await self._async_get_subscription_plan_preference(subscription)
        params, missing = self._delivery_menu_params(subscription, account_week, plan_preference)
        trace = {
            "subscription_id": subscription.subscription_id,
            "week_id": account_week.week_id,
            "params": params,
        }
        if params is None:
            self._record_debug_attempt(
                "menu_attempts",
                {
                    "subscription_id": subscription.subscription_id,
                    "path": self._WEEK_MENU_PATH,
                    "week_id": account_week.week_id,
                    "skipped": "missing_params",
                    "missing": missing,
                },
            )
            raise HelloFreshNotImplementedError(
                f"Week {account_week.week_id}'s menu can't be requested without its "
                f"{', '.join(missing)}"
            )

        try:
            payload = await self._async_get_json(
                self._WEEK_MENU_PATH, params, trace_category="menu_attempts", trace=trace
            )
        except HelloFreshError as err:
            if _refused_with(err, _HTTP_NOT_FOUND):
                raise HelloFreshNoMenuError(
                    f"HelloFresh has no menu for week {account_week.week_id} yet"
                ) from err
            raise

        # Guard against the endpoint serving a SUBSTITUTE menu for the requested week. For an
        # upcoming week the payload's top-level ``week`` matches the request; for a past week
        # the planning-menu endpoint (which the web app never calls for history) can return a
        # nearest/current menu instead. Stamping those recipes onto the requested week would
        # show the wrong meals (e.g. the current menu under a past week), so reject a mismatch
        # and let the recipes embedded in the deliveries payload stand for that week.
        payload_week = payload.get("week")
        if isinstance(payload_week, str) and payload_week and payload_week != account_week.week_id:
            self._record_debug_attempt(
                "menu_attempts",
                {
                    **trace,
                    "path": self._WEEK_MENU_PATH,
                    "rejected": "week_mismatch",
                    "payload_week": payload_week,
                },
            )
            raise HelloFreshError(
                f"HelloFresh answered week {account_week.week_id}'s menu request with another "
                "week's menu"
            )

        normalized_weeks = self._normalize_menu_weeks([payload], subscription=subscription)
        self._record_debug_attempt(
            "menu_attempts",
            {
                **trace,
                "path": self._WEEK_MENU_PATH,
                "payload_summary": self._summarize_payload(payload),
                "recognized_week_count": len(normalized_weeks),
            },
        )
        if not normalized_weeks:
            raise HelloFreshNoMenuError(
                f"HelloFresh has no menu for week {account_week.week_id} yet"
            )
        menu_week = self._overlay_menu_week_metadata(normalized_weeks[0], account_week)
        return menu_week

    @staticmethod
    def _delivery_option_handle(raw: Any) -> str | None:
        """Return the delivery-option handle a week or subscription payload names explicitly.

        Read from ``deliveryOption.handle`` (then ``deliveryOptionHandle`` and the
        subscription's ``deliveryTime``), as HelloFresh's Android app does. A search for the
        first nested ``handle`` anywhere can find the product's handle instead.
        """
        if not isinstance(raw, dict):
            return None
        option = raw.get("deliveryOption")
        if isinstance(option, dict) and isinstance(option.get("handle"), str) and option["handle"]:
            return option["handle"]
        for key in ("deliveryOptionHandle", "deliveryTime"):
            value = raw.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    def _build_delivery_menu_params(
        self,
        subscription: HelloFreshSubscription,
        account_week: HelloFreshWeek,
        plan_preference: str | None = None,
    ) -> dict[str, Any] | None:
        """Build the query string required by the live delivery menu endpoint."""
        return self._delivery_menu_params(subscription, account_week, plan_preference)[0]

    def _delivery_menu_params(
        self,
        subscription: HelloFreshSubscription,
        account_week: HelloFreshWeek,
        plan_preference: str | None = None,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """Return the week-menu query, or None with the names of what it lacks."""
        customer_plan_id = subscription.raw.get("customerPlanId")
        delivery_option = (
            self._delivery_option_handle(account_week.raw)
            or self._delivery_option_handle(subscription.raw)
            or self._find_first_nested_value(account_week.raw, ("handle", "deliveryOptionHandle"))
            or self._find_first_nested_value(subscription.raw, ("handle", "deliveryOptionHandle"))
        )
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
        week_product = _dict_at(account_week.raw, "product")
        product_sku = (
            week_product.get("handle")
            or week_product.get("sku")
            or self._find_first_nested_value(subscription.raw, ("sku",))
            or self._find_first_nested_value(account_week.raw, ("handle", "sku"))
        )
        # Servings likewise from the week's own product spec first (the box size), then the plan.
        week_specs = _dict_at(week_product, "specs")
        servings = coerce_int(
            week_specs.get("size")
            or self._find_first_nested_value(
                subscription.raw, ("size", "servings", "numberOfPersons")
            )
            or subscription.servings
        )
        locale = subscription.locale or self._find_first_nested_value(subscription.raw, ("locale",))

        missing = [
            name
            for name, value in (
                ("plan id", customer_plan_id),
                ("delivery option", delivery_option),
                ("postcode", postcode),
                ("meal preference", preference),
                ("box", product_sku),
                ("servings", servings),
                ("subscription id", subscription.subscription_id),
                ("week id", account_week.week_id),
                ("locale", locale),
            )
            if not value
        ]
        if missing:
            return None, missing

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
        }, []

    # Canonical source for the plan preference (e.g. "quick"). No params; returns
    # ``{unifiedPreferences: {plans: {customerPlanId: {planPreference}}}}`` — HAR-confirmed.
    _UNIFIED_PREFERENCES_PATH = "/gw/v1/profile/me/unified-preferences"

    async def _async_get_subscription_plan_preference(
        self,
        subscription: HelloFreshSubscription,
    ) -> str | None:
        """Return the current plan preference for a subscription when available.

        The preference (e.g. ``"quick"``) feeds the ``/gw/my-deliveries/menu`` request. Resolved
        in order:

        1. **``GET /gw/v1/profile/me/unified-preferences``** —
           ``unifiedPreferences.plans[customerPlanId].planPreference``. The dedicated, canonical
           source the current web app uses (HAR-confirmed).
        2. **``GET /gw/profile-service/v2/customers/me/profile``** —
           ``taste.plans[customerPlanId].planPreference`` (with ``taste.legacySinglePreference``),
           a fallback for accounts/regions where the unified endpoint isn't served.
        3. The subscription ``preset`` — the last resort; usually matches but is a distinct
           field that can diverge from the active preference.

        (An even older path, ``/gw/api/subscriptions/{id}/product_options``
        ``unifiedPreferences.plans[...]``, was dropped when the site stopped returning that
        structure there — it now serves the product catalog instead.)
        """
        subscription_id = subscription.subscription_id
        if subscription_id in self._subscription_preferences:
            return self._subscription_preferences[subscription_id]

        # The per-week menu fetches run 6-wide and each needs the preference; before the
        # first resolution lands, every concurrent task missed the cache and issued its own
        # preference GET(s). Share ONE in-flight resolution per subscription instead. A cart
        # write shares it too, so a write its automation stops must not fail the poll's menus.
        task = self._preference_resolutions.get(subscription_id)
        if task is None or not _shared_task_joinable(task):
            task = _start_shared_task(self._async_fetch_subscription_plan_preference(subscription))
            self._preference_resolutions[subscription_id] = task
        try:
            return await _async_await_shared(task)
        finally:
            if task.done() and self._preference_resolutions.get(subscription_id) is task:
                self._preference_resolutions.pop(subscription_id, None)

    async def _async_fetch_subscription_plan_preference(
        self,
        subscription: HelloFreshSubscription,
    ) -> str | None:
        """Resolve and cache the plan preference (single flight per subscription)."""
        subscription_id = subscription.subscription_id
        customer_plan_id = subscription.raw.get("customerPlanId")
        preference = subscription.raw.get("planPreference") or subscription.raw.get("preset")

        if customer_plan_id:
            resolved = await self._async_resolve_plan_preference(subscription_id, customer_plan_id)
            if resolved:
                preference = resolved

        if not preference:
            # Last resort, as in HelloFresh's Android app (``SubscriptionWithPresetDelegate``):
            # the region's highest-weight preset. Without a preference the week's menu request
            # cannot be built at all, so any preset beats skipping the menu.
            preference = await self._async_default_preset_handle(subscription_id)

        if preference:
            subscription.raw["planPreference"] = preference
        self._subscription_preferences[subscription_id] = preference
        return preference

    async def _async_default_preset_handle(self, subscription_id: str | None) -> str | None:
        """Return the handle of the region's highest-weight menu preset, or None."""
        try:
            presets = await self.async_get_presets()
        except _ALWAYS_RAISED:
            raise
        except HelloFreshError as err:
            self._record_debug_attempt(
                "preference_attempts",
                {"subscription_id": subscription_id, "path": "presets", "error": str(err)},
            )
            return None
        best = max(
            (preset for preset in presets if isinstance(preset.get("handle"), str)),
            key=lambda preset: coerce_float(preset.get("weight")) or 0.0,
            default=None,
        )
        handle = best["handle"] if best else None
        self._record_debug_attempt(
            "preference_attempts",
            {"subscription_id": subscription_id, "path": "presets", "plan_preference": handle},
        )
        return handle

    async def _async_resolve_plan_preference(
        self,
        subscription_id: str | None,
        customer_plan_id: str,
    ) -> str | None:
        """Fetch the plan preference from the unified endpoint, falling back to profile-service."""
        trace = {"subscription_id": subscription_id}
        # 1) Dedicated unified-preferences endpoint (no params).
        payload = await self._async_get_json(
            self._UNIFIED_PREFERENCES_PATH,
            trace_category="preference_attempts",
            trace=trace,
            optional=True,
        )
        if payload is not None:
            unified = payload.get("unifiedPreferences")
            pref = self._plan_pref_from_plans(
                unified.get("plans") if isinstance(unified, dict) else None,
                customer_plan_id,
            )
            self._record_debug_attempt(
                "preference_attempts",
                {**trace, "path": self._UNIFIED_PREFERENCES_PATH, "plan_preference": pref},
            )
            if pref:
                return pref

        # 2) Fallback: profile-service taste.plans / legacySinglePreference.
        payload = await self._async_get_json(
            self._PROFILE_PATH,
            self._profile_params(),
            trace_category="preference_attempts",
            trace=trace,
            optional=True,
        )
        if payload is None:
            return None

        taste = payload.get("taste")
        taste = taste if isinstance(taste, dict) else {}
        pref = self._plan_pref_from_plans(taste.get("plans"), customer_plan_id) or taste.get(
            "legacySinglePreference"
        )
        self._record_debug_attempt(
            "preference_attempts",
            {**trace, "path": self._PROFILE_PATH, "plan_preference": pref},
        )
        return pref

    @staticmethod
    def _plan_pref_from_plans(plans: Any, customer_plan_id: str) -> str | None:
        """Extract ``plans[customerPlanId].planPreference`` when present."""
        if not isinstance(plans, dict):
            return None
        entry = plans.get(customer_plan_id)
        return entry.get("planPreference") if isinstance(entry, dict) else None

    async def _async_get_public_menu_data(self) -> _MenuData:
        """Fetch and parse the public HelloFresh menus page."""
        self._tokens.raise_if_bot_blocked()
        # A top-level HTML page load, not a CORS XHR. It goes through the same Chrome TLS
        # impersonation as the API calls: on aiohttp's fingerprint a strict region answered
        # Cloudflare's 403, which also paused the entry's auth and data requests.
        try:
            response = await async_request(
                self._session,
                "GET",
                f"{self._base_url}/menus",
                headers=self._website_request_headers("/menus"),
                use_curl_cffi_headers=self._use_curl_cffi_headers,
                navigation=True,
            )
        except (ClientError, TimeoutError) as err:
            raise HelloFreshError(
                f"HelloFresh public menu page could not connect: {type(err).__name__}"
            ) from err
        await self._tokens.async_raise_if_data_blocked(response, public_page=True)
        if response.status >= _HTTP_BAD_REQUEST:
            raise HelloFreshError(
                f"Failed to fetch HelloFresh public menu page: HTTP {response.status}"
            )

        html = await response.text()
        # BeautifulSoup over a multi-hundred-KB marketing page is pure CPU; parsing it
        # inline stalled HA's whole event loop for the duration on every fallback poll.
        return await asyncio.to_thread(self._parse_public_menu_html, html)

    def _parse_public_menu_html(self, html: str) -> _MenuData:
        """Parse the public menu page into a fallback week (runs in a worker thread)."""
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
