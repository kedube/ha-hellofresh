"""HelloFresh cart-pricing (meal price preview) operations.

Split out of ``client.py`` (which had grown past 4,900 lines) as a mixin: the methods run
on ``HelloFreshClient`` exactly as before — same ``self`` attributes, same call sites —
this module only gives the area its own file. ``token_manager.py`` and
``tls_transport.py`` set the precedent.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
import hashlib
import json
import logging
from typing import Any

from .const import api_country_code
from .models import (
    HelloFreshError,
    HelloFreshSubscription,
    HelloFreshWeek,
)
from .parsers import _seg, coerce_float, coerce_int

_LOGGER = logging.getLogger(__name__)

# Cart-price cache bound: the cache itself (an OrderedDict) lives on HelloFreshClient;
# only the eviction below reads the limit, so the constant moved here with it.
_CART_PRICE_CACHE_MAX = 32


class PricingClientMixin:
    """Cart price preview/build helpers; mixed into HelloFreshClient."""

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
            "meal_count": len(course_quantities),
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
        if course_quantities is not None:
            selected_groups = self._override_cart_selection_groups(
                selected_groups, course_quantities, default_box_sku=box_sku
            )
            # The meal box SKU encodes how many meals it holds, so a hypothetical selection of a
            # different size needs the matching SKU or HelloFresh rejects it (MEAL_SIZE_MISMATCH).
            meal_count = len(course_quantities)
            if isinstance(main_product_handle, str):
                main_product_handle = self._sku_for_meal_count(main_product_handle, meal_count)

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

    def _override_cart_selection_groups(
        self,
        groups: list[dict[str, Any]],
        course_quantities: dict[int, int],
        *,
        default_box_sku: str | None,
    ) -> list[dict[str, Any]]:
        """Replace the meals in ``groups`` with a hypothetical ``{course_index: quantity}`` set.

        The group's ``handle``/``boxSku`` (which carry the premium-surcharge wiring) are reused
        from the week's real selection so the priced cart stays structurally identical to one
        HelloFresh would accept — only the chosen courses and their quantities change. The meal
        SKU is resized for the new count, matching what a real selection write does.
        """
        meal_count = len(course_quantities)
        template = groups[0] if groups else None
        handle = (template or {}).get("handle")
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
                "recipeIndexes": [str(index) for index, _ in ordered],
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
        return week.delivery_date >= date.today()
