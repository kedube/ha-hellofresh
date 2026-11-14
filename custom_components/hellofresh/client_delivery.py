"""Delivery and plan changes: skipping and restoring a week, a one-off reschedule, the
recurring delivery day and box size, and the option lists they choose from.
"""

from __future__ import annotations

from typing import Any

from .client_core import _LOGGER, _ClientCore, _dict_at
from .const import api_country_code
from .models import (
    HelloFreshDeliveryOption,
    HelloFreshError,
    HelloFreshNotImplementedError,
    HelloFreshSubscription,
    HelloFreshWeek,
)
from .normalizers import _template_debug_path
from .parsers import _seg, coerce_int


class DeliveryClientMixin(_ClientCore):
    """Delivery-schedule and plan reads and writes; mixed into HelloFreshClient."""

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

        family = self._find_first_nested_value(subscription.raw.get("productType"), ("family",))
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
            # The web app's account-settings reschedule flow sends these two context hints on
            # every call (HAR-confirmed). They don't change the returned option set for an active
            # subscriber, but we match the browser so a stricter path can't reject the request.
            "customerPriority": "active_subscription",
            "customerJourney": "account_setting",
        }
        payload = await self._async_get_json(
            "/gw/api/delivery_dates_options", params, trace_category="delivery_attempts"
        )
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
        payload = await self._async_get_json(
            "/gw/api/plans",
            {"includeCanceled": "false"},
            trace_category="profile_attempts",
            expect=(list, dict),
        )
        if isinstance(payload, list):
            return [p for p in payload if isinstance(p, dict)]
        items = payload.get("items")
        return [p for p in (items if isinstance(items, list) else []) if isinstance(p, dict)]

    async def async_skip_week(self, week_id: str) -> None:
        """Skip a delivery week (set its delivery status to PAUSED)."""
        await self._async_set_week_status(week_id, "PAUSED")

    async def async_unskip_week(self, week_id: str) -> None:
        """Undo a skipped delivery week (set its delivery status back to RUNNING)."""
        await self._async_set_week_status(week_id, "RUNNING")

    async def _async_set_week_status(self, week_id: str, status: str) -> None:
        """Skip or unskip a week, then re-read it.

        No "already skipped" early-return: the local snapshot is the previous poll, and an
        unskip done on the website since then would have made this explicit request a silent
        no-op. The status PATCH is idempotent.
        """
        week = self._get_known_week_or_raise(week_id)
        async with self._week_lock(week):
            await self._async_patch_delivery_status(week, status)
            self._clear_price_cache()
            subscription = self._known_subscription(week.subscription_id)
            if subscription is not None:
                await self._async_refresh_week_after_write(subscription, week)

    async def _async_patch_delivery_status(self, week: HelloFreshWeek, status: str) -> None:
        """Skip/unskip a week via the HAR-verified delivery-status PATCH.

        Observed in a capture as
        ``PATCH /gw/api/subscriptions/{subscription_id}/delivery_dates/{week_id}`` with body
        ``{"delivery": {"cutoffDate", "deliveryDate", "status", "subscriptionId", "id"}}``,
        where ``status`` is ``PAUSED`` (skip) or ``RUNNING`` (unskip). ``cutoffDate`` is
        optional: the website sends it, while HelloFresh's Android app leaves it out (its
        request model marks it optional and both skip and unskip pass none), so it is sent
        only when known. A week without a subscription id or a delivery date can't be sent
        (HelloFreshNotImplementedError); a rejection propagates as the HelloFreshError it is.
        """
        if week.subscription_id is None:
            raise HelloFreshNotImplementedError(
                f"Week {week.week_id} does not expose a subscription id, so its delivery "
                "can't be changed."
            )

        # Prefer the exact server-formatted timestamps from the raw payload; fall back to the
        # normalized fields. The endpoint echoes these back, so the format should round-trip.
        cutoff_date = week.raw.get("cutoffDate") or (
            week.selection_deadline.isoformat() if week.selection_deadline else None
        )
        delivery_date = week.raw.get("deliveryDate") or (
            week.delivery_date.isoformat() if week.delivery_date else None
        )
        if not delivery_date:
            raise HelloFreshNotImplementedError(
                f"Week {week.week_id} has no delivery date, so its delivery can't be changed."
            )

        path = f"/gw/api/subscriptions/{_seg(week.subscription_id)}/delivery_dates/{_seg(week.week_id)}"
        params = {"country": api_country_code(self._country), "locale": self._locale_for_country()}
        delivery: dict[str, Any] = {"cutoffDate": cutoff_date} if cutoff_date else {}
        delivery.update(
            {
                "deliveryDate": delivery_date,
                "status": status,
                "subscriptionId": week.subscription_id,
                "id": week.week_id,
            }
        )
        json_payload = {"delivery": delivery}
        await self._async_api_request("PATCH", path, params=params, json_payload=json_payload)
        _LOGGER.info(
            "HelloFresh week %s delivery status set to %s via %s",
            week.week_id,
            status,
            _template_debug_path(path),
        )

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

        path = f"/gw/api/subscriptions/{_seg(week.subscription_id)}/oneoff"
        params = {"country": api_country_code(self._country), "locale": self._locale_for_country()}
        json_payload = {
            "id": week.subscription_id,
            "delivery_option": delivery_option.strip(),
            "week": week_id,
            "source": "reschedule-delivery-feature",
        }
        async with self._week_lock(week):
            # The answer is a stub week (no status, date or allowed actions), so it is not
            # read; the week is read again instead.
            await self._async_api_request("POST", path, params=params, json_payload=json_payload)
            _LOGGER.info(
                "HelloFresh week %s rescheduled to delivery option %s", week_id, delivery_option
            )
            self._clear_price_cache()
            subscription = self._known_subscription(week.subscription_id)
            if subscription is not None:
                await self._async_refresh_week_after_write(subscription, week)

    async def async_list_plan_options(
        self,
        subscription_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the box sizes this subscription can switch to.

        HAR-verified (capture 43): ``GET /gw/api/subscriptions/{id}/product_options``. The
        response nests one entry per product family, each with a ``products`` list whose handles
        are the plan SKUs (``US-CBU-<meals>-<people>-0``) accepted by
        :meth:`async_change_plan`.

        Note this endpoint used to serve ``unifiedPreferences`` and no longer does -- see
        ``_async_resolve_plan_preference``, which dropped it for that purpose. It now serves the
        product catalog, which is what this reads.

        Prices are integer **cents** (``6594`` = $65.94) and are the full box price, not
        per-serving.
        """
        subscription = await self._async_resolve_subscription(subscription_id)
        params = {
            "country": api_country_code(self._country),
            "locale": self._locale_for_country(),
        }
        # A body that isn't an object (an error envelope served as a bare list, an HTML page)
        # is raised as the HelloFreshError it is, which the service layer reports.
        payload = await self._async_get_json(
            f"/gw/api/subscriptions/{_seg(subscription.subscription_id)}/product_options",
            params,
            trace_category="profile_attempts",
        )

        options: list[dict[str, Any]] = []
        for family in payload.get("items") or []:
            if not isinstance(family, dict):
                continue
            family_handle = family.get("handle")
            family_name = family.get("name")
            for product in family.get("products") or []:
                if not isinstance(product, dict):
                    continue
                handle = product.get("handle")
                if not handle:
                    continue
                specs = _dict_at(product, "specs")
                # Prices are integer cents in every capture, but coerce anyway: a numeric string
                # would otherwise pass the isinstance check below as "not a number" and silently
                # drop the price while keeping the raw value in price_cents.
                price_cents = coerce_int(product.get("price"))
                options.append(
                    {
                        "handle": handle,
                        "name": product.get("name") or product.get("productName"),
                        "family_handle": family_handle,
                        "family_name": family_name,
                        # Whole positive numbers or None, so the card and the per-week resize
                        # can compare sizes without guessing at a "3" or a 0.
                        "meals": self._positive_count(specs.get("meals")),
                        "servings": self._positive_count(specs.get("size")),
                        "price_cents": price_cents,
                        "price": (round(price_cents / 100, 2) if price_cents is not None else None),
                    }
                )
        # Smallest box first reads far better than the API's own ordering, which starts at the
        # largest and is not monotonic.
        options.sort(
            key=lambda option: (
                option["meals"] if isinstance(option["meals"], int) else 99,
                option["servings"] if isinstance(option["servings"], int) else 99,
            )
        )
        return options

    @staticmethod
    def _positive_count(value: Any) -> int | None:
        """Return a count as a whole number above zero, else None."""
        count = coerce_int(value)
        return count if count is not None and count > 0 else None

    async def async_change_plan(
        self,
        product_handle: str,
        subscription_id: str | None = None,
    ) -> None:
        """Change the subscription's recurring box size (meals per week × servings).

        HAR-verified (capture 43): ``PATCH /gw/api/plans/{planId}`` with body
        ``{"productHandle": "US-CBU-3-2-0"}``, returning **204 No Content**.

        This is a **recurring** change affecting all future boxes, and it changes what you are
        billed. It is distinct from the per-week box resize in ``_sku_for_meal_count``, which only
        rewrites the SKU on a single week's cart write to match that week's meal count.

        The capture pairs this with the delivery-weekday PATCH (the web app's "change plan" screen
        submits both), but the two are independent requests and either works alone.

        Note the query params here are **uppercase** ``country=US``, unlike the weekday PATCH on
        ``/gw/api/subscriptions/{id}`` which sends lowercase ``country=us``. Both are as captured.
        """
        if not product_handle or not product_handle.strip():
            raise HelloFreshError("A product_handle is required to change the plan")

        handle = product_handle.strip()
        subscription = await self._async_resolve_subscription(subscription_id)
        plan_id = subscription.raw.get("customerPlanId")
        if not plan_id:
            raise HelloFreshNotImplementedError(
                "The subscription does not expose a customerPlanId, so its plan cannot be changed."
            )

        params = {
            "country": api_country_code(self._country),
            "locale": self._locale_for_country(),
        }
        # 204 No Content: there is no body to read; the subscription is read again instead.
        await self._async_api_request(
            "PATCH",
            f"/gw/api/plans/{_seg(plan_id)}",
            params=params,
            json_payload={"productHandle": handle},
        )
        # Week ids and product handles are not account-identifying; the plan UUID is, and
        # users attach these logs to public GitHub issues — so it stays out.
        _LOGGER.info("HelloFresh plan changed to product %s", handle)
        self._clear_price_cache()
        await self._async_refresh_subscription_after_write(subscription)

    async def async_change_delivery_weekday(
        self,
        delivery_option: str,
        delivery_interval: int = 1,
        subscription_id: str | None = None,
    ) -> None:
        """Change the recurring delivery option for a subscription's plan.

        HAR-verified (capture 42): ``PATCH /gw/api/subscriptions/{subscriptionId}`` with body
        ``{"subscription": {"id", "deliveryTime"}}``. This affects **all** future deliveries for
        the subscription, not a single week. It is the only request sent: HelloFresh's Android
        app changes the day through ``POST /gw/api/plans/{planId}/changePlanDeliveryDetails``,
        which no web capture shows, and a write is never retried against an endpoint nobody
        has seen the website accept (a timeout may already have committed the PATCH).

        Two properties of the verified response are worth knowing, both pinned by tests:

        * The 200 body echoes the **pre-change** ``deliveryTime``, not the new one. A ``GET`` one
          second later in the capture already shows the new value, so the write commits
          immediately and only the response is stale. Adopting that body would roll the UI back to
          the old weekday until the next poll, so it is discarded here.
        * ``deliveryInterval`` is **not** part of this request. The web app sends only the
          ``deliveryTime`` handle. The parameter is still accepted for backward compatibility,
          but a value other than 1 cannot be honoured -- callers are warned rather than
          silently misled.
        """
        if not delivery_option or not delivery_option.strip():
            raise HelloFreshError("A delivery_option handle is required")

        handle = delivery_option.strip()
        subscription = await self._async_resolve_subscription(subscription_id)
        if delivery_interval != 1:
            _LOGGER.warning(
                "HelloFresh delivery_interval=%s cannot be applied: the weekday change sends "
                "only the delivery option handle, so the interval is ignored.",
                delivery_interval,
            )

        path = f"/gw/api/subscriptions/{_seg(subscription.subscription_id)}"
        params = {
            "country": api_country_code(self._country).lower(),
            "locale": self._locale_for_country(),
        }
        json_payload = {
            "subscription": {"id": subscription.subscription_id, "deliveryTime": handle}
        }
        # The response body is deliberately discarded: it carries the pre-change value.
        await self._async_api_request("PATCH", path, params=params, json_payload=json_payload)
        _LOGGER.info("HelloFresh subscription delivery option changed to %s", handle)
        self._clear_price_cache()
        await self._async_refresh_subscription_after_write(subscription, delivery_time=handle)
