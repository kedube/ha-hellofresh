"""The snapshot after a write: re-read what a write changed and publish a new account
snapshot with it, without waiting for the next poll.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from .client_core import _LOGGER, _ClientCore
from .models import HelloFreshError, HelloFreshOrder, HelloFreshSubscription, HelloFreshWeek
from .parsers import local_today


class SnapshotClientMixin(_ClientCore):
    """Re-reads after a write; mixed into HelloFreshClient."""

    # ------------------------------------------------------------------
    # The snapshot after a write
    #
    # A write re-reads what it changed and stores a NEW HelloFreshAccountData with that part
    # replaced (see last_account_data), so the integration can show the result without a full
    # poll. The objects of the previous snapshot are never changed in place: it may still be
    # what Home Assistant shows. Storing the new snapshot (``_publish_week``,
    # ``_publish_snapshot``) is in client_core, which other writes use too.
    # ------------------------------------------------------------------

    async def _async_read_account_weeks(
        self, subscription: HelloFreshSubscription
    ) -> tuple[list[HelloFreshWeek], list[HelloFreshOrder]]:
        """Read a subscription's weeks afresh: the verified ranged request, not shared."""
        path, params = self._verified_deliveries_request()
        payload = await self._async_get_json(
            path, params, trace_category="delivery_attempts", trace={"params": params}
        )
        return self._normalize_weeks_payload(payload, subscription)

    async def _async_refresh_week_after_write(
        self, subscription: HelloFreshSubscription, week: HelloFreshWeek
    ) -> None:
        """Re-read the week a write just changed and publish a snapshot with it replaced.

        Reads the subscription's weeks (the week's state and box), then, for a week that had a
        menu, its menu with the customer's choices, and prices it again. Best effort: the
        write has succeeded, so a failed re-read leaves the snapshot as it was instead of
        turning the write into an error.
        """
        data = self._last_account_data
        old_week = data.get_week(week.week_id, week.subscription_id) if data is not None else None
        if old_week is None or old_week.subscription_id != week.subscription_id:
            return
        try:
            fresh_weeks, fresh_orders = await self._async_read_account_weeks(subscription)
            fresh = next(
                (
                    candidate
                    for candidate in fresh_weeks
                    if (candidate.week_id, candidate.subscription_id)
                    == (week.week_id, week.subscription_id)
                ),
                None,
            )
            had_menu = isinstance(old_week.raw.get("_menu_payload"), dict)
            refreshed = fresh if fresh is not None else self._detached_week(old_week, had_menu)
            if had_menu:
                menu = await self._async_fetch_week_menu(subscription, refreshed)
                [refreshed] = self._merge_menu_weeks_into_account_weeks([refreshed], [menu])
            self._dress_refreshed_week(refreshed, old_week)
            orders = [
                order
                for order in fresh_orders
                if (order.week_id, order.subscription_id) == (week.week_id, week.subscription_id)
            ]
            old_orders = [
                order
                for order in (data.orders if data is not None else [])
                if (order.week_id, order.subscription_id) == (week.week_id, week.subscription_id)
            ]
            for order in orders:
                self._carry_order_details(order, old_orders)
                if not refreshed.is_skipped and (
                    refreshed.delivery_date is None or refreshed.delivery_date >= local_today()
                ):
                    await self._async_apply_order_price(subscription, refreshed, order)
        except HelloFreshError as err:
            _LOGGER.debug(
                "HelloFresh week %s was saved but could not be read again: %s", week.week_id, err
            )
            return
        self._publish_week(refreshed, orders if orders else None)

    def _detached_week(self, week: HelloFreshWeek, drop_menu: bool) -> HelloFreshWeek:
        """A copy of a snapshot week that shares nothing it could change with the original.

        ``drop_menu`` empties what the week's menu fills (the meals, the Market, the sections
        and filters, the selection count), for a week whose menu is about to be read again.
        """
        if drop_menu:
            return replace(
                week,
                recipes=[],
                market_items=[],
                menu_categories=[],
                menu_filters=[],
                meals_selected=None,
                meals_preselected=False,
                benefits=list(week.benefits),
                raw={key: value for key, value in week.raw.items() if key != "_menu_payload"},
            )
        return replace(
            week,
            recipes=[replace(recipe) for recipe in week.recipes],
            market_items=[replace(item) for item in week.market_items],
            benefits=list(week.benefits),
            raw=dict(week.raw),
        )

    def _dress_refreshed_week(self, week: HelloFreshWeek, old_week: HelloFreshWeek) -> None:
        """Give a re-read week what the poll adds to a week, and what wasn't re-read.

        The poll's per-week passes run on it; the wallet discounts, the regional catalog's
        sold-out flags and the cookbook hearts, which the re-read doesn't fetch, come from the
        week it replaces.
        """
        weeks = [week]
        self._apply_variation_titles(weeks)
        self._apply_market_items(weeks)
        self._apply_menu_categories(weeks)
        self._apply_menu_filters(weeks)
        self._clear_paused_week_selection(weeks)
        if not week.benefits:
            week.benefits = list(old_week.benefits)
        old_recipes = {recipe.recipe_id: recipe for recipe in old_week.recipes}
        for recipe in week.recipes:
            old = old_recipes.get(recipe.recipe_id)
            if old is None:
                continue
            if recipe.is_favorite is None:
                recipe.is_favorite = old.is_favorite
            if week.is_editable and not recipe.is_selected:
                recipe.is_sold_out = recipe.is_sold_out or old.is_sold_out
                recipe.is_hidden = recipe.is_hidden or old.is_hidden
            else:
                # As in the poll: a locked week shows nothing sold out, and a meal in the box
                # stays the customer's.
                recipe.is_sold_out = recipe.is_hidden = False

    @staticmethod
    def _carry_order_details(order: HelloFreshOrder, old_orders: Sequence[HelloFreshOrder]) -> None:
        """Copy onto a re-read order what only the poll's other reads give an order.

        The billing ledger's charged total and discount, and the carrier's tracking detail.
        """
        old = next(
            (candidate for candidate in old_orders if candidate.order_id == order.order_id), None
        )
        if old is None and old_orders:
            old = old_orders[0]
        if old is None:
            return
        for name in (
            "billed_total_price",
            "billed_total_currency",
            "discount_amount",
            "coupon_code",
            "total_price",
            "currency",
            "estimated_delivery",
            "tracking_status_detail",
            "delivery_signed_by",
            "carrier",
            "tracking_status",
        ):
            if getattr(order, name) is None:
                setattr(order, name, getattr(old, name))
        if not order.tracking_events:
            order.tracking_events = list(old.tracking_events)
        if not order.delivery_photo_urls:
            order.delivery_photo_urls = list(old.delivery_photo_urls)

    async def _async_refresh_subscription_after_write(
        self,
        subscription: HelloFreshSubscription,
        *,
        delivery_time: str | None = None,
    ) -> None:
        """Re-read a subscription a plan or delivery-day change just changed, and its weeks.

        The subscription and its upcoming weeks' state, box and dates are replaced; their
        menus and selections are not read again (that is every week's menu: the next poll
        does it). ``delivery_time`` is the delivery option just saved: HelloFresh's own read
        can lag that write by a second, and the write has committed, so it is shown.
        Best effort, as for a week (see ``_async_refresh_week_after_write``).
        """
        if self._last_account_data is None:
            return
        try:
            fresh = next(
                (
                    candidate
                    for candidate in await self._async_fetch_subscriptions()
                    if candidate.subscription_id == subscription.subscription_id
                ),
                None,
            )
            if fresh is None:
                return
            if delivery_time and fresh.raw.get("deliveryTime") != delivery_time:
                fresh.raw["deliveryTime"] = delivery_time
            if subscription.raw.get("planPreference") and not fresh.raw.get("planPreference"):
                fresh.raw["planPreference"] = subscription.raw["planPreference"]
                fresh.plan_preference = subscription.raw["planPreference"]
            fresh.recent_payment_date = subscription.recent_payment_date
            fresh.next_payment_date = subscription.next_payment_date
            fresh_weeks, fresh_orders = await self._async_read_account_weeks(fresh)
        except HelloFreshError as err:
            _LOGGER.debug("HelloFresh plan was saved but could not be read again: %s", err)
            return

        current = self._last_account_data
        if current is None:
            return
        today = local_today()
        subscription_id = subscription.subscription_id
        fresh_by_week = {week.week_id: week for week in fresh_weeks}
        # Only weeks still to come: a delivered week's box and dates don't change with the plan.
        replaced: set[str] = set()
        weeks: list[HelloFreshWeek] = []
        for week in current.weeks:
            fresh_week = (
                fresh_by_week.get(week.week_id) if week.subscription_id == subscription_id else None
            )
            if fresh_week is not None and (
                week.delivery_date is None or week.delivery_date >= today
            ):
                weeks.append(self._week_with_fresh_delivery(week, fresh_week))
                replaced.add(week.week_id)
            else:
                weeks.append(week)
        orders: list[HelloFreshOrder] = []
        for order in current.orders:
            if order.subscription_id == subscription_id and order.week_id in replaced:
                continue
            orders.append(order)
        for order in fresh_orders:
            if order.week_id in replaced:
                self._carry_order_details(
                    order,
                    [
                        old
                        for old in current.orders
                        if (old.week_id, old.subscription_id) == (order.week_id, subscription_id)
                    ],
                )
                orders.append(order)
        subscriptions = [
            fresh if candidate.subscription_id == subscription_id else candidate
            for candidate in current.subscriptions
        ]
        if self._cached_subscriptions is not None:
            self._cached_subscriptions = [
                fresh if candidate.subscription_id == subscription_id else candidate
                for candidate in self._cached_subscriptions
            ]
        self._publish_snapshot(weeks=weeks, orders=orders, subscriptions=subscriptions)

    @staticmethod
    def _week_with_fresh_delivery(old: HelloFreshWeek, fresh: HelloFreshWeek) -> HelloFreshWeek:
        """A re-read week's state, box and dates, with the menu the snapshot had for it."""
        raw = dict(fresh.raw)
        if "_menu_payload" in old.raw:
            raw["_menu_payload"] = old.raw["_menu_payload"]
        return replace(
            fresh,
            recipes=old.recipes,
            market_items=old.market_items,
            menu_categories=old.menu_categories,
            menu_filters=old.menu_filters,
            benefits=old.benefits,
            meals_preselected=old.meals_preselected,
            meals_selected=fresh.meals_selected
            if fresh.meals_selected is not None
            else old.meals_selected,
            menu_title=old.menu_title or fresh.menu_title,
            raw=raw,
        )
