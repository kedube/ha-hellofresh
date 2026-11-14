"""Delivered-week history: past deliveries, walked back page by page and kept between
polls.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
import re
from time import monotonic
from typing import Any

from .client_core import _ALWAYS_RAISED, _ClientCore
from .const import api_country_code
from .models import HelloFreshError, HelloFreshSubscription, HelloFreshWeek
from .parsers import local_today

# Delivered weeks don't change, so the history pages behind the first are kept between polls
# and re-read once a day (a rating given to an older box shows up then).
_HISTORY_WEEK_TTL = 24 * 3600.0


class HistoryClientMixin(_ClientCore):
    """Past-delivery reads; mixed into HelloFreshClient."""

    # The history the website itself uses: per subscription, paged, with images.
    _PAST_DELIVERIES_PATH = "/gw/my-deliveries/past-deliveries"
    # The complaint form's list: the last couple of weeks only, with image-less recipes.
    _RECENT_DELIVERIES_PATH = "/gw/customer-complaints/users/me/deliveries"

    async def _async_get_past_delivery_weeks(
        self,
        subscriptions: Sequence[HelloFreshSubscription],
    ) -> list[HelloFreshWeek]:
        """Fetch the delivered-week history: the meals and Market items each box came with.

        ``/gw/my-deliveries/past-deliveries`` is the authoritative source, asked once per
        subscription and walked back page by page. Only when it fails for a subscription is
        the complaint form's list asked (once, for the account): it knows just the last couple
        of weeks, with image-less recipes, so it fills only weeks past-deliveries left out.
        """
        if self._session is None:
            return []
        self._prune_history_cache()
        # Keyed by (subscription id, week id): two subscriptions share ISO week ids, and by the
        # week alone the second subscription's history was dropped as already filled.
        delivered: dict[tuple[str | None, str], HelloFreshWeek] = {}
        incomplete = False
        for subscription in subscriptions:
            try:
                weeks = await self._async_get_subscription_history(subscription, subscriptions)
            except _ALWAYS_RAISED:
                # A dead token has to reach the coordinator: every other request fails the
                # same way, and treating it as a failed read showed empty history instead of
                # asking the user to sign in again.
                raise
            except HelloFreshError:
                incomplete = True
                continue
            for week in weeks:
                if self._is_history_week(week):
                    delivered[(week.subscription_id, week.week_id)] = week
        if incomplete:
            for week in await self._async_get_recent_deliveries(subscriptions):
                key = (week.subscription_id, week.week_id)
                if key not in delivered and self._is_history_week(week):
                    delivered[key] = week
        return list(delivered.values())

    @staticmethod
    def _is_history_week(week: HelloFreshWeek) -> bool:
        """Return True for a delivered week that says what came in the box.

        A week that delivered only Market add-ons counts: its add-ons are the record.
        """
        return bool(week.recipes or week.market_items)

    async def _async_get_subscription_history(
        self,
        subscription: HelloFreshSubscription,
        subscriptions: Sequence[HelloFreshSubscription],
    ) -> list[HelloFreshWeek]:
        """Read one subscription's delivered weeks from past-deliveries, every page of them.

        The first page is read on every poll; the older ones come from what earlier polls read
        while it is fresh (see ``_async_paginate_past_deliveries``). Raises the first page's
        failure.
        """
        params = {
            "country": api_country_code(self._country),
            "locale": self._locale_for_country(),
            "rating-scale": "5",
            "subscription": subscription.subscription_id,
        }
        payload = await self._async_get_json(
            self._PAST_DELIVERIES_PATH,
            params,
            trace_category="history_attempts",
            trace={"params": params},
        )
        # The rows don't say whose they are, so the normalizer is told which subscription
        # this answer is for.
        weeks = self._normalize_past_delivery_payload(
            payload, subscriptions, requested_subscription=subscription
        )
        self._record_debug_attempt(
            "history_attempts",
            {
                "path": self._PAST_DELIVERIES_PATH,
                "params": params,
                "payload_summary": self._summarize_payload(payload),
                "recognized_week_count": len(weeks),
            },
        )
        return await self._async_paginate_past_deliveries(
            path=self._PAST_DELIVERIES_PATH,
            params=params,
            payload=payload,
            first_page_weeks=weeks,
            subscriptions=subscriptions,
            requested_subscription=subscription,
        )

    async def _async_get_recent_deliveries(
        self, subscriptions: Sequence[HelloFreshSubscription]
    ) -> list[HelloFreshWeek]:
        """Read the complaint form's list of the last delivered weeks (best effort)."""
        payload = await self._async_get_json(
            self._RECENT_DELIVERIES_PATH, trace_category="history_attempts", optional=True
        )
        if payload is None:
            return []
        weeks = self._normalize_past_delivery_payload(payload, subscriptions)
        self._record_debug_attempt(
            "history_attempts",
            {
                "path": self._RECENT_DELIVERIES_PATH,
                "payload_summary": self._summarize_payload(payload),
                "recognized_week_count": len(weeks),
            },
        )
        return weeks

    async def _async_paginate_past_deliveries(
        self,
        path: str,
        params: dict[str, Any],
        payload: Any,
        first_page_weeks: list[HelloFreshWeek],
        subscriptions: Sequence[HelloFreshSubscription],
        requested_subscription: HelloFreshSubscription | None = None,
    ) -> list[HelloFreshWeek]:
        """Follow the past-deliveries ``nextWeek`` cursor back to the history floor.

        Each page returns ``{"weeks": [...], "nextWeek": "YYYY-Www"}``. Feed ``nextWeek`` back
        in as the ``from`` cursor until the API stops returning one, until a page yields no new
        weeks, or until the cursor passes the configured history floor. Bounded by a hard page
        cap so a misbehaving cursor can never loop forever.

        Delivered weeks don't change, so every week read is kept (``_history_cache``) with the
        id of the next older one. When the cursor reaches a week an earlier poll already read,
        the rest of the walk follows those links instead of asking for the pages again; only
        the first page is read every poll. A kept week is read again after a day.
        """
        # Page a couple of weeks PAST the display range floor so a week sitting right at the
        # boundary (and ISO-week rounding / year-boundary ordering around it) always has its
        # delivered meals fetched, not just shown as an empty shell. Keyed off the same lookback
        # the display range uses (+2 weeks margin) so the two stay aligned as the constant moves.
        floor_date = local_today() - timedelta(weeks=self._history_weeks + 2)
        floor_iso = floor_date.isocalendar()
        floor_week = f"{floor_iso.year}-W{floor_iso.week:02d}"
        subscription_key = requested_subscription.subscription_id if requested_subscription else ""
        weeks_by_id: dict[str, HelloFreshWeek] = {week.week_id: week for week in first_page_weeks}
        self._remember_history_page(subscription_key, payload)
        next_cursor = self._extract_next_week_cursor(payload)
        # Derive the page cap from the configured lookback instead of hardcoding it: pages have
        # held ~4 weeks, but a fixed 20 silently stopped ~80 weeks back, so a history_weeks near
        # the 104 maximum lost its oldest weeks. Assume a conservative 2 weeks per page and add
        # slack, so the cap scales with the option and still bounds a misbehaving cursor.
        max_pages = max(20, (self._history_weeks // 2) + 10)
        pages_read = 0
        weeks_reused = 0
        visited: set[str] = set()

        # Compare by absolute week order, not raw string: "2026-W01" < "2025-W52" is True as
        # strings but later in time, which would stop pagination early near a year change.
        while next_cursor and self._iso_week_sort_key(next_cursor) >= self._iso_week_sort_key(
            floor_week
        ):
            if next_cursor in visited:
                break
            visited.add(next_cursor)
            cached = self._cached_history_week(subscription_key, next_cursor)
            if cached is not None:
                raw_week, older = cached
                for week in self._normalize_past_delivery_payload(
                    {"weeks": [raw_week]},
                    subscriptions,
                    requested_subscription=requested_subscription,
                ):
                    weeks_by_id.setdefault(week.week_id, week)
                weeks_reused += 1
                next_cursor = older
                continue
            if pages_read >= max_pages:
                break
            page_params = {**params, "from": next_cursor}
            try:
                page_payload = await self._async_get_json(
                    path,
                    page_params,
                    trace_category="history_attempts",
                    trace={"params": page_params},
                )
            except _ALWAYS_RAISED:
                # Same contract as the caller: a dead token has to reach the coordinator. Keeping
                # the pages already collected and breaking would hide the expiry behind a
                # plausible-looking (but truncated) history for the whole poll interval.
                raise
            except HelloFreshError:
                break
            pages_read += 1
            self._remember_history_page(subscription_key, page_payload)

            page_weeks = self._normalize_past_delivery_payload(
                page_payload, subscriptions, requested_subscription=requested_subscription
            )
            new_weeks = [week for week in page_weeks if week.week_id not in weeks_by_id]
            self._record_debug_attempt(
                "history_attempts",
                {
                    "path": path,
                    "params": page_params,
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

        if weeks_reused:
            self._record_debug_attempt(
                "history_attempts",
                {"path": path, "pages_read": pages_read, "weeks_reused": weeks_reused},
            )
        return list(weeks_by_id.values())

    def _remember_history_page(self, subscription_key: str, payload: Any) -> None:
        """Keep a past-deliveries page's weeks, each linked to the next older week."""
        if not isinstance(payload, dict):
            return
        raw_weeks = [
            raw
            for raw in self._extract_past_delivery_candidates(payload)
            if isinstance(raw.get("week") or raw.get("id"), str)
        ]
        cursor = self._extract_next_week_cursor(payload)
        now = monotonic()
        for position, raw_week in enumerate(raw_weeks):
            older = (
                str(raw_weeks[position + 1].get("week") or raw_weeks[position + 1].get("id"))
                if position + 1 < len(raw_weeks)
                else cursor
            )
            week_id = str(raw_week.get("week") or raw_week.get("id"))
            self._history_cache[(subscription_key, week_id)] = (now, raw_week, older)

    def _cached_history_week(
        self, subscription_key: str, week_id: str
    ) -> tuple[dict[str, Any], str | None] | None:
        """Return a kept history week and the id of the one before it, while still fresh."""
        entry = self._history_cache.get((subscription_key, week_id))
        if entry is None or monotonic() - entry[0] >= _HISTORY_WEEK_TTL:
            return None
        return entry[1], entry[2]

    def _prune_history_cache(self) -> None:
        """Forget kept history weeks that are due to be read again."""
        now = monotonic()
        for key in [
            key
            for key, (stored_at, _raw, _older) in self._history_cache.items()
            if now - stored_at >= _HISTORY_WEEK_TTL
        ]:
            del self._history_cache[key]

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
