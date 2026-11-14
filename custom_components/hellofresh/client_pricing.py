"""HelloFresh pricing: cart prices (the meal price preview and each week's box), the
``/gw/calculate`` estimate behind the plan and next-box prices, and the poll's price overlays.

Split out of ``client.py`` (which had grown past 4,900 lines) as a mixin on
``client_core._ClientCore``: the methods run on ``HelloFreshClient`` exactly as before — same
``self`` attributes, same call sites — this module only gives the area its own file.
``token_manager.py`` and ``tls_transport.py`` set the precedent.
"""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
import json
import logging
from time import monotonic
from typing import Any

from .client_core import _ClientCore, _dict_at
from .const import api_country_code
from .models import (
    HelloFreshAccountData,
    HelloFreshError,
    HelloFreshOrder,
    HelloFreshSubscription,
    HelloFreshWeek,
)
from .parsers import _seg, coerce_float, coerce_int, local_today

_LOGGER = logging.getLogger(__name__)

# Cart-price cache bound: the cache itself (an OrderedDict) is client state, set up in
# ``_ClientCore.__init__``; only the eviction below reads the limit, so the constant lives here.
_CART_PRICE_CACHE_MAX = 32
# How long a cached price is reused. The request body names the cart but not the price list,
# the vouchers or the fees, so a price HelloFresh changes on its side is picked up within this.
_CART_PRICE_TTL = 6 * 3600.0


class PricingClientMixin(_ClientCore):
    """Price reads and cart-price builders; mixed into HelloFreshClient."""

    async def async_preview_meal_price(
        self,
        week_id: str,
        recipe_ids: Sequence[str],
        quantities: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Price a hypothetical meal selection without saving it.

        Answers "what would this cost?" for a set of recipes the customer has not committed to,
        so a planner UI can show the running total (and which meals carry a premium surcharge)
        while they pick. Read-only: nothing is written to the account.
        """
        week = self._get_known_week_or_raise(week_id)
        subscription = await self._async_get_subscription_for_week(week)
        if subscription is None:
            raise HelloFreshError(f"No HelloFresh subscription found for week {week_id}")

        # Map recipe ids to the course indexes the cart is actually keyed by. The same dish can
        # appear under several ids/indexes (portion variants), so resolve against this week.
        by_id = {recipe.recipe_id: recipe for recipe in week.recipes}
        course_quantities: dict[int, int] = {}
        unknown: list[str] = []
        for recipe_id in recipe_ids:
            recipe = by_id.get(str(recipe_id))
            if recipe is None or recipe.course_index is None:
                unknown.append(str(recipe_id))
                continue
            requested = (quantities or {}).get(str(recipe_id), 1)
            course_quantities[recipe.course_index] = max(1, int(requested))
        if unknown:
            raise HelloFreshError(
                f"Unknown recipes for week {week_id} (or missing a course index): "
                f"{', '.join(sorted(unknown))}"
            )
        if not course_quantities:
            raise HelloFreshError("No recipes were provided to price")

        payload = await self._async_get_cart_price_for_week(
            subscription, week, course_quantities=course_quantities
        )
        if payload is None:
            raise HelloFreshError(
                f"HelloFresh did not return pricing for week {week_id}. Preview pricing needs "
                "the week's full menu payload, which is only available for bookable weeks."
            )
        return self._summarize_cart_price(payload, course_quantities)

    @staticmethod
    def _summarize_cart_price(
        payload: dict[str, Any], course_quantities: dict[int, int]
    ) -> dict[str, Any]:
        """Reduce a raw cart-pricing response to the figures a dashboard actually shows."""
        surcharges: list[dict[str, Any]] = []
        for product in payload.get("products") or []:
            if not isinstance(product, dict):
                continue
            for charge in product.get("charges") or []:
                if not isinstance(charge, dict):
                    continue
                amount = coerce_int(charge.get("amount"))
                surcharges.append(
                    {
                        "reason": charge.get("reason"),
                        "entity_id": charge.get("entity_id"),
                        "entity_type": charge.get("entity_type"),
                        "strategy": charge.get("strategy"),
                        # HelloFresh returns surcharge amounts in minor units (cents).
                        "amount_cents": amount,
                        "amount": (amount / 100) if amount is not None else None,
                    }
                )
        return {
            "meal_count": sum(course_quantities.values()),
            "course_quantities": {str(k): v for k, v in sorted(course_quantities.items())},
            "grand_total": coerce_float(payload.get("grandTotal")),
            "sub_total": coerce_float(payload.get("subTotal")),
            "shipping_amount": coerce_float(payload.get("shippingAmount")),
            "tax_amount": coerce_float(payload.get("taxAmount")),
            "discount_amount": coerce_float(payload.get("discountAmount")),
            "coupon_code": payload.get("couponCode") or None,
            "surcharges": surcharges,
        }

    async def _async_get_cart_price_for_week(
        self,
        subscription: HelloFreshSubscription,
        week: HelloFreshWeek,
        course_quantities: dict[int, int] | None = None,
    ) -> dict[str, Any] | None:
        """Fetch exact pricing for a week from the cart pricing endpoint."""
        # Only pass the override when there is one: the common path keeps the original
        # two-argument call so existing callers/stubs of the builder are unaffected.
        json_payload = (
            self._build_cart_price_payload(subscription, week)
            if course_quantities is None
            else self._build_cart_price_payload(subscription, week, course_quantities)
        )
        path = f"/gw/v1/carts/{_seg(week.week_id)}/price"
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
        cached = self._cached_cart_price(cache_key)
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

        trace = {
            "subscription_id": subscription.subscription_id,
            "week_id": week.week_id,
            "params": params,
            "json_payload": json_payload,
        }
        payload = await self._async_get_json(
            path,
            params,
            trace_category="pricing_attempts",
            method="POST",
            json_payload=json_payload,
            trace=trace,
            optional=True,
        )
        if payload is None:
            return None
        self._store_cart_price(cache_key, payload)
        self._record_debug_attempt(
            "pricing_attempts",
            {**trace, "path": path, "payload_summary": self._summarize_payload(payload)},
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

    def _cached_cart_price(self, cache_key: str) -> dict[str, Any] | None:
        """Return a cached pricing payload while it is younger than ``_CART_PRICE_TTL``."""
        entry = self._cart_price_cache.get(cache_key)
        if entry is None:
            return None
        stored_at, payload = entry
        if monotonic() - stored_at >= _CART_PRICE_TTL:
            del self._cart_price_cache[cache_key]
            return None
        return payload

    def _store_cart_price(self, cache_key: str, payload: dict[str, Any]) -> None:
        """Cache a pricing payload that has a total, evicting the oldest past the FIFO cap.

        An answer without a recognizable total is not kept: it would stand in for the price
        until it expired, where asking again may well give one.
        """
        if self._extract_total_price(payload) is None:
            return
        self._cart_price_cache[cache_key] = (monotonic(), payload)
        self._cart_price_cache.move_to_end(cache_key)
        while len(self._cart_price_cache) > _CART_PRICE_CACHE_MAX:
            self._cart_price_cache.popitem(last=False)

    def _clear_price_cache(self) -> None:
        """Forget every cached price: a write can change what any week costs."""
        self._cart_price_cache.clear()

    def _build_cart_price_payload(
        self,
        subscription: HelloFreshSubscription,
        week: HelloFreshWeek,
        course_quantities: dict[int, int] | None = None,
    ) -> dict[str, Any] | None:
        """Build a cart pricing payload from delivery and menu metadata.

        ``course_quantities`` prices a HYPOTHETICAL selection ({course_index: servings}) instead
        of the week's saved one, which is what makes a "what would this cost?" preview possible
        without writing the selection first. The box SKU is recomputed for the requested meal
        count, mirroring what a real selection write does — otherwise pricing more meals than
        the current box holds is rejected the same way a save would be.
        """
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
        delivery_option = (
            self._delivery_option_handle(week.raw)
            or self._delivery_option_handle(subscription.raw)
            or self._find_first_nested_value(subscription.raw, ("handle", "deliveryOptionHandle"))
        )
        unit_price_cents = coerce_float(
            self._find_first_nested_value(week.raw.get("product"), ("price", "unitPrice"))
        )
        # The week's own box, then the plan's (a week can be resized for that delivery).
        box_sku = main_product_handle or self._find_first_nested_value(subscription.raw, ("sku",))
        locale = subscription.locale or self._find_first_nested_value(subscription.raw, ("locale",))
        shipping_address = self._extract_shipping_address_payload(subscription.raw)
        charge_handle = self._meal_charge_handle()
        selected_groups = self._extract_cart_selection_groups(menu_payload, box_sku, charge_handle)
        if course_quantities is not None:
            selected_groups = self._override_cart_selection_groups(
                selected_groups,
                course_quantities,
                default_box_sku=box_sku,
                default_handle=charge_handle,
            )
            # The meal box SKU encodes how many meals it holds, so a hypothetical selection of a
            # different size needs the matching SKU or HelloFresh rejects it (MEAL_SIZE_MISMATCH).
            meal_count = sum(course_quantities.values())
            if isinstance(main_product_handle, str):
                main_product_handle = self._sku_for_meal_count(main_product_handle, meal_count)

        # Only what HelloFresh's Android app sends is required: the app prices a box with
        # just the postcode of the address and no planID or locale. The web app's extra fields
        # are still sent when they are known.
        if not all(
            (
                customer_id,
                box_size,
                main_product_handle,
                delivery_option,
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
        # The week's Market add-ons are part of the box total: one product per selected item,
        # keyed by its own SKU (HelloFresh's Android app, ``BoxContentAddonMapper``).
        for item in week.market_items:
            quantity = item.selected_quantity or 0
            if not item.is_selected or quantity <= 0 or item.index is None or not item.sku:
                continue
            products.append(
                {
                    "boxSku": item.sku,
                    "handle": item.sku,
                    "hfWeek": week.week_id,
                    "quantityPerCourse": [{"index": item.index, "quantity": quantity}],
                    "recipeIndexes": [str(item.index)],
                }
            )

        payload: dict[str, Any] = {
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
        }
        if customer_plan_id:
            payload["planID"] = customer_plan_id
        payload["products"] = products
        payload["shippingAddress"] = shipping_address
        if locale:
            payload["locale"] = locale
        payload["country"] = api_country_code(self._country)
        return payload

    def _meal_charge_handle(self) -> str:
        """Return the product handle the box's meals are priced under, e.g. ``US-CHARGE-0-0-0``.

        The menu's meals carry no handle of their own: HelloFresh's Android app builds this one
        from the country code (``BoxContentCourseMapper``), and the web's captured request uses
        the same value.
        """
        return f"{api_country_code(self._country)}-CHARGE-0-0-0"

    def _extract_shipping_address_payload(self, node: Any) -> dict[str, Any] | None:
        """Extract the limited shipping address fields used by cart pricing.

        Only the postcode is required (HelloFresh's Android app sends nothing else); the web
        app's ``address1`` and ``region`` are added when present. The request's ``region`` is the
        state's code (``"MA"``), while the subscription's address holds an object
        (``{code, name}``): sent as it was, the whole request failed with HTTP 400 ("failed to
        parse price calculation request").
        """
        address = self._find_first_nested_dict(node, {"shippingAddress", "address"})
        postcode = address.get("postcode") or address.get("postalCode")
        if not postcode:
            return None
        payload: dict[str, Any] = {}
        if address.get("address1"):
            payload["address1"] = address["address1"]
        payload["postcode"] = postcode
        region = address.get("region") or address.get("state")
        if isinstance(region, dict):
            region = region.get("code")
        if isinstance(region, str) and region.strip():
            payload["region"] = region.strip()
        return payload

    def _extract_cart_selection_groups(
        self,
        menu_payload: dict[str, Any],
        default_box_sku: str | None,
        default_handle: str | None = None,
    ) -> list[dict[str, Any]]:
        """Collect selected menu items into the grouped payload expected by cart pricing.

        A meal is listed in ``recipeIndexes`` once per portion, as HelloFresh's Android app
        does, so a meal at quantity 2 is priced as two of the box's meals.
        """
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for raw_item in self._find_cart_selection_candidates(menu_payload):
            index = raw_item.get("index")
            quantity = coerce_int(
                (raw_item.get("selection") or {}).get("quantity") or raw_item.get("quantity")
            )
            if index is None or quantity is None or quantity <= 0:
                continue

            charge = _dict_at(raw_item, "charge")
            handle = (
                charge.get("handle")
                or raw_item.get("handle")
                or raw_item.get("sku")
                or default_handle
            )
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
            group["recipeIndexes"].extend([str(index)] * quantity)

        return list(grouped.values())

    def _override_cart_selection_groups(
        self,
        groups: list[dict[str, Any]],
        course_quantities: dict[int, int],
        *,
        default_box_sku: str | None,
        default_handle: str | None = None,
    ) -> list[dict[str, Any]]:
        """Replace the meals in ``groups`` with a hypothetical ``{course_index: quantity}`` set.

        The group's ``handle``/``boxSku`` (which carry the premium-surcharge wiring) are reused
        from the week's real selection so the priced cart stays structurally identical to one
        HelloFresh would accept — only the chosen courses and their quantities change. The meal
        SKU is resized for the new count, matching what a real selection write does.
        """
        meal_count = sum(course_quantities.values())
        template = groups[0] if groups else None
        handle = (template or {}).get("handle") or default_handle
        box_sku = (template or {}).get("boxSku") or default_box_sku
        if not handle or not box_sku:
            return []
        resized_sku = self._sku_for_meal_count(str(box_sku), meal_count)
        ordered = sorted(course_quantities.items())
        return [
            {
                "handle": str(handle),
                "boxSku": resized_sku,
                "quantityPerCourse": [
                    {"index": index, "quantity": quantity} for index, quantity in ordered
                ],
                "recipeIndexes": [
                    str(index) for index, quantity in ordered for _ in range(quantity)
                ],
            }
        ]

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
        return week.delivery_date >= local_today()

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
        data.selected_plan_price_breakdown = self._price_breakdown_from_calculate(pricing)

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

    @staticmethod
    def _price_breakdown_from_calculate(pricing: dict[str, Any]) -> dict[str, Any] | None:
        """Reduce a ``/gw/calculate`` response to the figures worth exposing as attributes.

        The response carries ``subTotal``, ``shippingAmount``, ``discountAmount`` and
        ``taxAmount`` alongside ``grandTotal`` — available for years but never surfaced. Amounts
        are already major units here (unlike the cents-denominated payments gateway).
        """
        breakdown = {
            "sub_total": coerce_float(pricing.get("subTotal")),
            "shipping_amount": coerce_float(pricing.get("shippingAmount")),
            "discount_amount": coerce_float(pricing.get("discountAmount")),
            "tax_amount": coerce_float(pricing.get("taxAmount")),
            "grand_total": coerce_float(pricing.get("grandTotal")),
            "coupon_code": pricing.get("couponCode") or None,
        }
        if all(value is None for key, value in breakdown.items() if key != "coupon_code"):
            return None
        return breakdown

    async def _async_enrich_next_box_price_breakdown(
        self,
        data: HelloFreshAccountData,
        subscriptions: Sequence[HelloFreshSubscription],
        weeks: Sequence[HelloFreshWeek],
    ) -> None:
        """Price the next upcoming box through ``/gw/calculate`` for its discount breakdown.

        ``next_box_total_price`` itself comes from the billing ledger (the authoritative charged
        amount); this adds the subtotal / shipping / discount split the ledger does not carry,
        as attributes on that sensor. One cached request per poll for the primary
        subscription's next non-skipped week; best-effort.
        """
        if self._session is None or not subscriptions:
            return
        primary = subscriptions[0]
        today = local_today()
        candidates = [
            week
            for week in weeks
            if week.delivery_date is not None
            and week.delivery_date >= today
            and not week.is_skipped
            and (week.subscription_id in (None, primary.subscription_id))
        ]
        if not candidates:
            return
        next_week = min(candidates, key=lambda week: (week.delivery_date, week.week_id))
        pricing = await self._async_get_calculate_price(primary, next_week)
        if pricing is None:
            return
        breakdown = self._price_breakdown_from_calculate(pricing)
        if breakdown is None:
            return
        breakdown["week_id"] = next_week.week_id
        data.next_delivery_price_breakdown = breakdown

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

        today = local_today()
        price_tasks = []
        for week in weeks:
            if week.subscription_id is None:
                continue
            # Never price PAST weeks. Their authoritative total is the billed charge (already
            # applied from payment history); a past week has no menu payload, so pricing it
            # fell through to /gw/calculate — an estimate of TODAY'S recurring plan that both
            # overwrote the real billed amount and burst one POST per history week (default
            # 26) on every cold poll.
            if week.delivery_date is not None and week.delivery_date < today:
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
            # Same cap as the menu fetches — pricing every upcoming week at once was an
            # unbounded burst against the pricing endpoints.
            await self._async_gather_bounded(price_tasks)

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
        *,
        product_handle: str | None = None,
    ) -> dict[str, Any] | None:
        """Fetch a box total from /gw/calculate (lighter than the cart-price endpoint).

        HAR-confirmed request shape. With ``week`` it prices that specific delivery; without
        it, the standing recurring plan (used for the plan-settings weekly price).
        ``product_handle`` prices another box than the week's own (a size change not made
        yet). The total is read with the same defensive field fallbacks used for other pricing
        payloads (``grandTotal``/``total``/``amount``/…). Treated as best-effort.
        """
        json_payload = self._build_calculate_payload(
            subscription, week, product_handle=product_handle
        )
        if json_payload is None:
            return None

        week_id = week.week_id if week is not None else None
        cache_key = self._request_fingerprint("/gw/calculate", None, json_payload)
        cached = self._cached_cart_price(cache_key)
        if cached is not None:
            return cached

        trace = {"subscription_id": subscription.subscription_id, "week_id": week_id}
        payload = await self._async_get_json(
            "/gw/calculate",
            trace_category="pricing_attempts",
            method="POST",
            json_payload=json_payload,
            trace=trace,
            optional=True,
        )
        if payload is None:
            return None
        self._store_cart_price(cache_key, payload)
        self._record_debug_attempt(
            "pricing_attempts",
            {
                **trace,
                "path": "/gw/calculate",
                "payload_summary": self._summarize_payload(payload),
            },
        )
        return payload

    def _build_calculate_payload(
        self,
        subscription: HelloFreshSubscription,
        week: HelloFreshWeek | None = None,
        *,
        product_handle: str | None = None,
    ) -> dict[str, Any] | None:
        """Build the /gw/calculate request body from subscription + week metadata.

        When ``week`` is omitted the request describes the standing recurring plan (the
        "plan settings" weekly price), drawing the product handle and delivery option from
        the subscription itself -- which is what the web app posts for that view.
        ``product_handle`` replaces the box being priced.
        """
        raw_week = week.raw if week is not None else {}
        customer_id = coerce_int(subscription.account_id)
        subscription_id = coerce_int(subscription.subscription_id)
        plan_id = subscription.raw.get("customerPlanId")
        main_product_handle = (
            product_handle
            or self._find_first_nested_value(raw_week.get("product"), ("handle", "sku"))
            or self._find_first_nested_value(subscription.raw, ("sku",))
        )
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
            # The subscription's active coupon, as HelloFresh's Android app sends it
            # (``ProductInfoCalculationRequestFactory``); null when there is none.
            "couponCode": subscription.raw.get("couponCode") or None,
            "locale": locale,
            "country": api_country_code(self._country),
        }
