"""Payload normalization helpers for the HelloFresh client.

The client owns HTTP/auth orchestration; this mixin owns conversion from
HelloFresh payloads into integration models.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from .models import HelloFreshOrder, HelloFreshRecipe, HelloFreshSubscription, HelloFreshWeek
from .parsers import (
    MAX_SEARCH_DEPTH,
    coerce_float,
    coerce_int,
    extract_allowed_actions,
    extract_name_list,
    extract_tracking_details,
    find_nested_collection,
    looks_like_recipe_collection,
    normalize_candidate_dict_list,
    parse_date,
    parse_datetime,
    slugify,
)

_COUNTRY_CURRENCIES = {
    "us": "USD",
    "ca": "CAD",
    "uk": "GBP",
    "au": "AUD",
    "de": "EUR",
    "nl": "EUR",
}


class HelloFreshPayloadNormalizer:
    """Mixin for pure-ish HelloFresh payload normalization methods."""

    def _normalize_weeks_payload(
        self,
        payload: dict[str, Any],
        subscription: HelloFreshSubscription,
    ) -> tuple[list[HelloFreshWeek], list[HelloFreshOrder]]:
        """Normalize a deliveries payload into HelloFresh weeks and orders."""
        raw_weeks = payload.get("weeks") or payload.get("items") or payload.get("deliveries") or []
        weeks: list[HelloFreshWeek] = []
        orders: list[HelloFreshOrder] = []

        for index, raw_week in enumerate(raw_weeks):
            raw_subscription_id = raw_week.get("subscriptionId") or raw_week.get("subscription_id")
            if (
                raw_subscription_id is not None
                and str(raw_subscription_id) != subscription.subscription_id
            ):
                continue
            week_id = (
                raw_week.get("id")
                or raw_week.get("week")
                or raw_week.get("deliveryWeek")
                or raw_week.get("calendarWeek")
                or f"week-{index}"
            )
            display_name = (
                raw_week.get("label")
                or raw_week.get("title")
                or raw_week.get("displayName")
                or self._find_first_nested_value(raw_week, ("name", "displayName", "deliveryName"))
                or str(week_id)
            )
            raw_meals = self._extract_delivery_week_recipe_candidates(raw_week)
            recipes = [self._recipe_from_raw_meal(raw_meal) for raw_meal in raw_meals]

            meals_selected = coerce_int(
                raw_week.get("mealsSelected")
                or raw_week.get("selectedMealCount")
                or self._find_first_nested_value(
                    raw_week,
                    (
                        "mealsSelected",
                        "selectedMealCount",
                        "selectedRecipesCount",
                        "mealCountSelected",
                    ),
                )
                or (sum(1 for recipe in recipes if recipe.is_selected) if raw_meals else None)
            )
            meals_required = coerce_int(
                raw_week.get("mealsRequired")
                or raw_week.get("requiredMealCount")
                or raw_week.get("recipeCount")
                or self._find_first_nested_value(
                    raw_week,
                    (
                        "mealsRequired",
                        "requiredMealCount",
                        "recipeCount",
                        "numberOfRecipes",
                        "meals",
                    ),
                )
                or subscription.meals_required
            )

            week = HelloFreshWeek(
                week_id=str(week_id),
                display_name=display_name,
                subscription_id=(
                    str(raw_subscription_id)
                    if raw_subscription_id is not None
                    else subscription.subscription_id
                ),
                delivery_date=parse_date(
                    raw_week.get("deliveryDate")
                    or raw_week.get("date")
                    or raw_week.get("shipmentDate")
                    or raw_week.get("expectedDeliveryDate")
                ),
                selection_deadline=parse_datetime(
                    raw_week.get("selectionDeadline")
                    or raw_week.get("cutoffDate")
                    or raw_week.get("deadline")
                ),
                status=raw_week.get("status") or raw_week.get("deliveryStatus") or "scheduled",
                meals_required=meals_required,
                meals_selected=meals_selected,
                is_skipped=bool(
                    raw_week.get("skipped")
                    or raw_week.get("isSkipped")
                    or raw_week.get("status") == "skipped"
                ),
                recipes=recipes,
                source="account",
                menu_title=raw_week.get("menuTitle")
                or raw_week.get("title")
                or self._find_first_nested_value(raw_week, ("name", "displayName")),
                slot_label=raw_week.get("timeSlot")
                or raw_week.get("slotLabel")
                or self._find_first_nested_value(
                    raw_week,
                    ("deliveryName", "deliveryFrom", "deliveryTo"),
                ),
                shipping_method=raw_week.get("shippingMethod")
                or self._find_first_nested_value(raw_week, ("type", "deliveryType"))
                or subscription.shipping_method,
                box_size=raw_week.get("boxSize") or subscription.box_size,
                sub_status=raw_week.get("subStatus"),
                delivery_state=raw_week.get("state"),
                actionable=bool(raw_week.get("actionable")),
                prepaid=bool(raw_week.get("prepaid")),
                delivery_blocked=bool(raw_week.get("deliveryBlocked") or raw_week.get("isBlocked")),
                holiday_delivery_date=parse_date(raw_week.get("holidayDelivery")),
                holiday_message=raw_week.get("holidayMessage"),
                holiday_shift_visible=bool(raw_week.get("isHolidayShiftVisible")),
                allowed_actions=extract_allowed_actions(raw_week),
                available_one_off_options=self._extract_available_one_off_options(raw_week),
                raw=raw_week,
            )
            weeks.append(week)
            orders.append(self._order_from_raw_week(raw_week=raw_week, week=week))

        return weeks, orders

    def _extract_delivery_week_recipe_candidates(
        self, raw_week: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Extract recipe-like items from a delivery week payload."""
        return self._extract_recipe_candidates(
            raw_week,
            (
                "meals",
                "recipes",
                "selectedMeals",
                "menuItems",
                "menu",
                "selection",
                "box",
                "delivery",
            ),
            fallback_to_node=True,
        )

    def _extract_recipe_id_from_raw_meal(self, raw_meal: dict[str, Any]) -> str:
        """Extract just the recipe id from a raw meal dict without full object construction."""
        recipe_data = (
            raw_meal.get("recipe") if isinstance(raw_meal.get("recipe"), dict) else raw_meal
        )
        name = (
            recipe_data.get("name") or recipe_data.get("title") or recipe_data.get("slug") or "Meal"
        )
        return str(
            recipe_data.get("id")
            or recipe_data.get("slug")
            or raw_meal.get("id")
            or slugify(name)
            or name
        )

    def _recipe_from_raw_meal(
        self,
        raw_meal: dict[str, Any],
        *,
        default_selected: bool = True,
    ) -> HelloFreshRecipe:
        """Create a recipe from a recipe-like payload."""
        recipe_data = (
            raw_meal.get("recipe") if isinstance(raw_meal.get("recipe"), dict) else raw_meal
        )
        name = (
            recipe_data.get("name") or recipe_data.get("title") or recipe_data.get("slug") or "Meal"
        )
        nutrition = self._extract_nutrition(recipe_data)
        ingredients = extract_name_list(
            recipe_data.get("ingredients")
            or recipe_data.get("ingredientLines")
            or recipe_data.get("ingredientNames")
        )
        allergens = extract_name_list(recipe_data.get("allergens"))
        tags = extract_name_list(recipe_data.get("tags") or recipe_data.get("labels"))
        calories_kcal = coerce_float(
            recipe_data.get("caloriesKcal")
            or recipe_data.get("calories")
            or nutrition.get("calories")
            or nutrition.get("kcal")
        )
        cook_time = coerce_int(recipe_data.get("cookTime") or recipe_data.get("cookTimeMinutes"))
        prep_time = coerce_int(recipe_data.get("prepTime") or recipe_data.get("prepTimeMinutes"))
        total_time = coerce_int(
            recipe_data.get("totalTime")
            or recipe_data.get("totalTimeMinutes")
            or ((cook_time or 0) + (prep_time or 0) if cook_time or prep_time else None)
        )
        selection = raw_meal.get("selection") if isinstance(raw_meal.get("selection"), dict) else {}
        selection_quantity = coerce_int(selection.get("quantity"))
        selection_selected = selection.get("selected")
        if isinstance(selection_selected, bool):
            is_selected = selection_selected
        elif selection_quantity is not None:
            is_selected = selection_quantity > 0
        else:
            is_selected = bool(
                raw_meal.get("selected", recipe_data.get("selected", default_selected))
            )

        return HelloFreshRecipe(
            recipe_id=str(
                recipe_data.get("id")
                or recipe_data.get("slug")
                or raw_meal.get("id")
                or slugify(name)
                or name
            ),
            name=name,
            preference=recipe_data.get("preference") or recipe_data.get("category"),
            is_selected=is_selected,
            image_url=recipe_data.get("imagePath")
            or recipe_data.get("image")
            or recipe_data.get("imageUrl"),
            description=recipe_data.get("description") or recipe_data.get("headline"),
            ingredients=ingredients,
            allergens=allergens,
            tags=tags,
            nutrition=nutrition,
            cook_time_minutes=cook_time,
            prep_time_minutes=prep_time,
            total_time_minutes=total_time,
            calories_kcal=calories_kcal,
            difficulty=recipe_data.get("difficulty") or recipe_data.get("skillLevel"),
        )

    def _extract_available_one_off_options(
        self,
        raw_week: dict[str, Any],
    ) -> list[dict[str, str | None]]:
        """Normalize alternative delivery date options for a week."""
        raw_options = raw_week.get("availableOneOffOptions")
        if not isinstance(raw_options, list):
            return []

        options: list[dict[str, str | None]] = []
        for item in raw_options:
            if not isinstance(item, dict):
                continue
            delivery_date = parse_date(item.get("deliveryDate"))
            options.append(
                {
                    "handle": str(item.get("handle")) if item.get("handle") is not None else None,
                    "delivery_date": delivery_date.isoformat() if delivery_date else None,
                }
            )
        return options

    @staticmethod
    def _extract_nutrition(raw_meal: dict[str, Any]) -> dict[str, str]:
        """Extract a normalized nutrition mapping."""
        nutrition = raw_meal.get("nutrition")
        if isinstance(nutrition, dict):
            return {
                str(key): str(value) for key, value in nutrition.items() if value not in (None, "")
            }
        if isinstance(nutrition, list):
            result: dict[str, str] = {}
            for item in nutrition:
                if not isinstance(item, dict):
                    continue
                key = item.get("name") or item.get("label")
                value = item.get("value")
                if key and value not in (None, ""):
                    result[str(key)] = str(value)
            return result
        return {}

    def _find_first_nested_dict(
        self,
        node: Any,
        keys: set[str],
        _depth: int = 0,
    ) -> dict[str, Any]:
        """Return the first nested dict matching one of the provided keys."""
        if _depth >= MAX_SEARCH_DEPTH:
            return {}
        if isinstance(node, list):
            for item in node:
                nested = self._find_first_nested_dict(item, keys, _depth + 1)
                if nested:
                    return nested
            return {}

        if not isinstance(node, dict):
            return {}

        for key in keys:
            value = node.get(key)
            if isinstance(value, dict):
                return value

        for value in node.values():
            nested = self._find_first_nested_dict(value, keys, _depth + 1)
            if nested:
                return nested

        return {}

    def _find_first_nested_value(
        self,
        node: Any,
        keys: tuple[str, ...],
        _depth: int = 0,
    ) -> Any:
        """Return the first non-empty nested value for any of the provided keys."""
        if _depth >= MAX_SEARCH_DEPTH:
            return None
        if isinstance(node, dict):
            for key in keys:
                value = node.get(key)
                if value not in (None, "", [], {}):
                    return value

            for value in node.values():
                nested = self._find_first_nested_value(value, keys, _depth + 1)
                if nested not in (None, "", [], {}):
                    return nested

        if isinstance(node, list):
            for item in node:
                nested = self._find_first_nested_value(item, keys, _depth + 1)
                if nested not in (None, "", [], {}):
                    return nested

        return None

    def _order_from_raw_week(
        self,
        raw_week: dict[str, Any],
        week: HelloFreshWeek,
    ) -> HelloFreshOrder:
        """Create an order record from a raw delivery week payload."""
        tracking = extract_tracking_details(raw_week)
        total_price = self._extract_total_price(raw_week)
        return HelloFreshOrder(
            order_id=str(
                raw_week.get("orderId")
                or raw_week.get("shipmentId")
                or raw_week.get("deliveryId")
                or week.week_id
            ),
            week_id=week.week_id,
            status=week.status or ("skipped" if week.is_skipped else "scheduled"),
            subscription_id=week.subscription_id,
            delivery_date=week.delivery_date,
            tracking_url=tracking.get("tracking_url"),
            tracking_number=tracking.get("tracking_number"),
            tracking_status=tracking.get("tracking_status"),
            carrier=tracking.get("carrier"),
            total_price=round(total_price, 2) if total_price is not None else None,
            currency=self._extract_currency_code(raw_week),
            slot_label=week.slot_label,
        )

    def _extract_total_price(self, raw_week: dict[str, Any]) -> float | None:
        """Return the best available total price, including shipping when split out."""
        direct_total = coerce_float(
            raw_week.get("grandTotal")
            or raw_week.get("totalPrice")
            or raw_week.get("total")
            or raw_week.get("amount")
            or self._find_first_nested_value(
                raw_week, ("grandTotal", "totalPrice", "total", "amount")
            )
        )
        if direct_total is not None:
            return direct_total

        direct_total_cents = coerce_float(
            raw_week.get("grandTotalInCents")
            or raw_week.get("totalPriceInCents")
            or raw_week.get("totalInCents")
            or self._find_first_nested_value(
                raw_week,
                ("grandTotalInCents", "totalPriceInCents", "totalInCents"),
            )
        )
        if direct_total_cents is not None:
            return direct_total_cents / 100

        subtotal = coerce_float(
            raw_week.get("subTotal")
            or raw_week.get("subtotal")
            or self._find_first_nested_value(raw_week, ("subTotal", "subtotal"))
        )
        shipping = coerce_float(
            raw_week.get("shippingAmount")
            or raw_week.get("shipping")
            or self._find_first_nested_value(raw_week, ("shippingAmount", "shipping"))
        )
        if subtotal is not None and shipping is not None:
            return subtotal + shipping

        subtotal_cents = coerce_float(
            raw_week.get("subTotalInCents")
            or raw_week.get("subtotalInCents")
            or self._find_first_nested_value(raw_week, ("subTotalInCents", "subtotalInCents"))
        )
        shipping_cents = coerce_float(
            raw_week.get("shippingAmountInCents")
            or raw_week.get("shippingInCents")
            or self._find_first_nested_value(
                raw_week,
                ("shippingAmountInCents", "shippingInCents"),
            )
        )
        if subtotal_cents is not None and shipping_cents is not None:
            return (subtotal_cents + shipping_cents) / 100

        product_price_cents = coerce_float(
            self._find_first_nested_value(raw_week.get("product"), ("price", "unitPrice"))
        )
        if product_price_cents is not None:
            return product_price_cents / 100 + self._extract_delivery_fee(raw_week)

        single_cents = coerce_float(
            raw_week.get("priceInCents")
            or self._find_first_nested_value(raw_week, ("priceInCents",))
        )
        if single_cents is not None:
            return single_cents / 100

        return coerce_float(
            raw_week.get("price") or self._find_first_nested_value(raw_week, ("price",))
        )

    def _extract_delivery_fee(self, raw_week: dict[str, Any]) -> float:
        """Return the best available shipping or special fee in currency units."""
        fee_candidates = (
            coerce_float(
                self._find_first_nested_value(
                    raw_week,
                    (
                        "specialFee",
                        "shippingPrice",
                        "shippingAmountInCents",
                        "shippingInCents",
                        "priceInCents",
                    ),
                )
            ),
            coerce_float(
                self._find_first_nested_value(
                    raw_week,
                    (
                        "shippingAmount",
                        "shipping",
                    ),
                )
            ),
        )
        cents_fee, amount_fee = fee_candidates
        if cents_fee is not None and cents_fee >= 100:
            return cents_fee / 100
        if amount_fee is not None:
            return amount_fee
        if cents_fee is not None:
            return cents_fee / 100
        return 0.0

    def _extract_currency_code(self, raw_week: dict[str, Any]) -> str | None:
        """Return the best available currency code for an order."""
        currency = (
            raw_week.get("currency")
            or raw_week.get("currencyCode")
            or self._find_first_nested_value(raw_week, ("currency", "currencyCode"))
        )
        if isinstance(currency, str) and currency.strip():
            return currency.strip().upper()
        return _COUNTRY_CURRENCIES.get(self._country)

    @staticmethod
    def _build_delivery_history_range() -> dict[str, str]:
        """Return a conservative range for account delivery history lookups."""
        today = datetime.now(UTC).date()
        start = today - timedelta(weeks=12)
        end = today + timedelta(weeks=1)
        start_iso = start.isocalendar()
        end_iso = end.isocalendar()
        return {
            "range_start": f"{start_iso.year}-W{start_iso.week:02d}",
            "range_end": f"{end_iso.year}-W{end_iso.week:02d}",
        }

    def _subscription_from_raw_subscription(
        self, raw_subscription: dict[str, Any]
    ) -> HelloFreshSubscription:
        """Normalize a subscription payload."""
        customer = raw_subscription.get("customer") or {}
        plan = raw_subscription.get("plan") or self._find_first_nested_dict(
            raw_subscription,
            {"plan", "activePlan", "subscriptionPlan"},
        )
        meals_required = coerce_int(
            plan.get("numberOfRecipes")
            or plan.get("recipesPerWeek")
            or self._find_first_nested_value(
                raw_subscription, ("numberOfRecipes", "recipesPerWeek")
            )
            or self._find_first_nested_value(raw_subscription, ("meals",))
            or raw_subscription.get("mealsPerWeek")
            or raw_subscription.get("recipesPerWeek")
        )
        servings = coerce_int(
            plan.get("numberOfPersons")
            or plan.get("servings")
            or self._find_first_nested_value(raw_subscription, ("numberOfPersons", "servings"))
            or self._find_first_nested_value(raw_subscription, ("size",))
            or raw_subscription.get("numberOfPersons")
            or raw_subscription.get("servings")
        )
        display_name = (
            raw_subscription.get("name")
            or raw_subscription.get("displayName")
            or plan.get("name")
            or plan.get("displayName")
            or self._find_first_nested_value(raw_subscription, ("name", "displayName"))
        )

        return HelloFreshSubscription(
            subscription_id=str(raw_subscription.get("id")),
            account_id=customer.get("id"),
            locale=customer.get("locale"),
            status=(
                raw_subscription.get("status")
                or raw_subscription.get("subscriptionStatus")
                or raw_subscription.get("state")
                or self._find_first_nested_value(
                    raw_subscription, ("status", "subscriptionStatus", "state")
                )
            ),
            display_name=display_name,
            plan_name=plan.get("name") or plan.get("displayName"),
            meals_required=meals_required,
            servings=servings,
            delivery_address=self._format_subscription_address(
                raw_subscription.get("shippingAddress")
            ),
            box_size=raw_subscription.get("boxSize") or raw_subscription.get("size"),
            shipping_method=raw_subscription.get("shippingMethod")
            or raw_subscription.get("deliveryType"),
            delivery_weekday=coerce_int(raw_subscription.get("deliveryWeekday")),
            preset=raw_subscription.get("preset"),
            next_delivery=parse_date(raw_subscription.get("nextDelivery")),
            next_delivery_week=raw_subscription.get("nextDeliveryWeek"),
            next_cutoff_date=parse_datetime(raw_subscription.get("nextCutoffDate")),
            next_modifiable_delivery_date=parse_date(
                raw_subscription.get("nextModifiableDeliveryDate")
            ),
            next_modifiable_delivery_week=raw_subscription.get("nextModifiableDeliveryWeek"),
            next_delivery_time=raw_subscription.get("nextDeliveryTime"),
            payment_method=raw_subscription.get("paymentMethod"),
            payment_gateway=raw_subscription.get("paymentGateway"),
            coupon_code=raw_subscription.get("couponCode"),
            first_box_delivered=(
                bool(raw_subscription.get("firstBoxDelivered"))
                if raw_subscription.get("firstBoxDelivered") is not None
                else None
            ),
            loyalty_boxes_received=coerce_int(
                raw_subscription.get("loyaltyBoxesReceived")
                or raw_subscription.get("totalBoxesReceived")
                or (
                    self._find_first_nested_value(
                        raw_subscription["customer"].get("loyalty"),
                        ("value", "boxesReceived"),
                    )
                    if isinstance(raw_subscription.get("customer"), dict)
                    and isinstance(raw_subscription["customer"].get("loyalty"), dict)
                    else None
                )
                or self._find_first_nested_value(
                    raw_subscription,
                    ("loyaltyBoxesReceived", "totalBoxesReceived", "boxesReceived"),
                )
            ),
            loyalty_boxes_until_next_freebie=coerce_int(
                raw_subscription.get("loyaltyBoxesUntilNextFreebie")
                or raw_subscription.get("boxesUntilNextFreebie")
                or self._find_first_nested_value(
                    raw_subscription.get("customer", {}).get("loyalty") or {},
                    ("boxesUntilNextFreebie", "loyaltyBoxesUntilNextFreebie"),
                )
                or self._find_first_nested_value(
                    raw_subscription,
                    ("loyaltyBoxesUntilNextFreebie", "boxesUntilNextFreebie"),
                )
            ),
            raw=raw_subscription,
        )

    @staticmethod
    def _format_subscription_address(raw_address: Any) -> str | None:
        """Format a delivery address into a compact single-line label."""
        if not isinstance(raw_address, dict):
            return None

        region = raw_address.get("region")
        if isinstance(region, dict):
            region = region.get("code") or region.get("name")

        parts = [
            raw_address.get("address1"),
            raw_address.get("city"),
            region,
            raw_address.get("postcode"),
        ]
        normalized = [str(part).strip() for part in parts if isinstance(part, str) and part.strip()]
        if not normalized:
            return None
        return ", ".join(normalized)

    def _overlay_menu_week_metadata(
        self,
        menu_week: HelloFreshWeek,
        account_week: HelloFreshWeek,
    ) -> HelloFreshWeek:
        """Preserve delivery metadata when the menu payload only carries recipes."""
        menu_week.week_id = account_week.week_id
        menu_week.subscription_id = account_week.subscription_id
        menu_week.display_name = account_week.display_name or menu_week.display_name
        menu_week.delivery_date = account_week.delivery_date or menu_week.delivery_date
        menu_week.selection_deadline = (
            account_week.selection_deadline or menu_week.selection_deadline
        )
        menu_week.status = account_week.status or menu_week.status
        menu_week.meals_required = account_week.meals_required or menu_week.meals_required
        if account_week.meals_selected not in (None, 0) or menu_week.meals_selected is None:
            menu_week.meals_selected = account_week.meals_selected
        menu_week.is_skipped = account_week.is_skipped
        menu_week.menu_title = menu_week.menu_title or account_week.menu_title
        menu_week.slot_label = account_week.slot_label or menu_week.slot_label
        menu_week.shipping_method = account_week.shipping_method or menu_week.shipping_method
        menu_week.box_size = account_week.box_size or menu_week.box_size
        return menu_week

    def _backfill_account_weeks_from_subscriptions(
        self,
        subscriptions: Sequence[HelloFreshSubscription],
        weeks: Sequence[HelloFreshWeek],
    ) -> list[HelloFreshWeek]:
        """Fill missing next-week metadata from subscription payloads when deliveries are sparse."""
        weeks_by_key = {(week.subscription_id, week.week_id): week for week in weeks}
        merged_weeks = list(weeks)

        for subscription in subscriptions:
            fallback_week = self._week_from_subscription(subscription)
            if fallback_week is None:
                continue

            key = (fallback_week.subscription_id, fallback_week.week_id)
            existing_week = weeks_by_key.get(key)
            if existing_week is None:
                merged_weeks.append(fallback_week)
                weeks_by_key[key] = fallback_week
                continue

            existing_week.display_name = (
                existing_week.display_name
                if existing_week.display_name
                and existing_week.display_name != existing_week.week_id
                else fallback_week.display_name
            )
            existing_week.delivery_date = existing_week.delivery_date or fallback_week.delivery_date
            existing_week.selection_deadline = (
                existing_week.selection_deadline or fallback_week.selection_deadline
            )
            existing_week.status = existing_week.status or fallback_week.status
            existing_week.meals_required = (
                existing_week.meals_required or fallback_week.meals_required
            )
            existing_week.meals_selected = (
                existing_week.meals_selected
                if existing_week.meals_selected is not None
                else fallback_week.meals_selected
            )
            existing_week.slot_label = existing_week.slot_label or fallback_week.slot_label
            existing_week.shipping_method = (
                existing_week.shipping_method or fallback_week.shipping_method
            )
            existing_week.box_size = existing_week.box_size or fallback_week.box_size
            existing_week.raw = {
                **fallback_week.raw,
                **existing_week.raw,
            }

        return merged_weeks

    def _week_from_subscription(
        self,
        subscription: HelloFreshSubscription,
    ) -> HelloFreshWeek | None:
        """Build a fallback week from subscription next-delivery metadata."""
        raw_subscription = subscription.raw
        week_id = raw_subscription.get("nextModifiableDeliveryWeek") or raw_subscription.get(
            "nextDeliveryWeek"
        )
        if not isinstance(week_id, str) or not week_id.strip():
            return None

        delivery_option = (
            raw_subscription.get("nextDeliveryOption")
            if isinstance(raw_subscription.get("nextDeliveryOption"), dict)
            else raw_subscription.get("deliveryOption")
            if isinstance(raw_subscription.get("deliveryOption"), dict)
            else {}
        )
        product_name = (
            self._find_first_nested_value(
                raw_subscription.get("productType"), ("productName", "name")
            )
            or self._find_first_nested_value(
                raw_subscription.get("product"), ("displayName", "name")
            )
            or subscription.display_name
            or str(week_id)
        )
        meals_required = coerce_int(
            self._find_first_nested_value(
                raw_subscription.get("productType"), ("meals", "numberOfRecipes")
            )
            or subscription.meals_required
        )
        return HelloFreshWeek(
            week_id=week_id,
            display_name=str(product_name),
            subscription_id=subscription.subscription_id,
            delivery_date=parse_date(
                raw_subscription.get("nextModifiableDeliveryDate")
                or raw_subscription.get("nextDelivery")
            ),
            selection_deadline=parse_datetime(
                raw_subscription.get("nextCutoffDate")
                or raw_subscription.get("reactivationNextCutoffDate")
            ),
            status="scheduled" if raw_subscription.get("isActive", True) else "inactive",
            meals_required=meals_required,
            meals_selected=0 if meals_required else None,
            is_skipped=False,
            source="account",
            menu_title=str(product_name),
            slot_label=delivery_option.get("deliveryName"),
            shipping_method=delivery_option.get("type") or subscription.shipping_method,
            box_size=subscription.box_size,
            raw={
                **raw_subscription,
                "deliveryOption": delivery_option,
                "deliveryDate": raw_subscription.get("nextModifiableDeliveryDate")
                or raw_subscription.get("nextDelivery"),
                "cutoffDate": raw_subscription.get("nextCutoffDate")
                or raw_subscription.get("reactivationNextCutoffDate"),
                "week": week_id,
            },
        )

    def _merge_menu_weeks_into_account_weeks(
        self,
        account_weeks: Sequence[HelloFreshWeek],
        menu_weeks: Sequence[HelloFreshWeek],
    ) -> list[HelloFreshWeek]:
        """Merge menu recipe catalogs into account weeks while preserving selection state."""
        menu_by_key = {
            (menu_week.subscription_id, menu_week.week_id): menu_week for menu_week in menu_weeks
        }
        merged_weeks: list[HelloFreshWeek] = []

        for account_week in account_weeks:
            menu_week = menu_by_key.get((account_week.subscription_id, account_week.week_id))
            if menu_week is None or not menu_week.recipes:
                merged_weeks.append(account_week)
                continue

            selected_recipe_ids = {
                recipe.recipe_id for recipe in account_week.recipes if recipe.is_selected
            }
            if not selected_recipe_ids and account_week.recipes:
                selected_recipe_ids = {recipe.recipe_id for recipe in account_week.recipes}

            for recipe in menu_week.recipes:
                recipe.is_selected = recipe.recipe_id in selected_recipe_ids

            account_week.recipes = menu_week.recipes
            account_week.menu_title = menu_week.menu_title or account_week.menu_title
            if account_week.meals_required is None:
                account_week.meals_required = menu_week.meals_required
            if account_week.meals_selected in (None, 0) and menu_week.meals_selected is not None:
                account_week.meals_selected = menu_week.meals_selected
            account_week.raw = {
                **account_week.raw,
                "_menu_payload": menu_week.raw,
            }
            merged_weeks.append(account_week)

        return merged_weeks

    def _normalize_menu_weeks(
        self,
        raw_weeks: list[dict[str, Any]],
        subscription: HelloFreshSubscription,
    ) -> list[HelloFreshWeek]:
        """Normalize menu-style payloads into public menu week models."""
        weeks: list[HelloFreshWeek] = []
        for index, raw_week in enumerate(raw_weeks):
            raw_recipes = self._extract_menu_week_recipe_candidates(raw_week)
            recipes = [
                self._recipe_from_raw_meal(raw_recipe, default_selected=False)
                for raw_recipe in raw_recipes
            ]
            if not recipes:
                continue

            week_id = str(
                raw_week.get("id")
                or raw_week.get("week")
                or raw_week.get("calendarWeek")
                or f"menu-week-{index}"
            )
            display_name = (
                raw_week.get("label")
                or raw_week.get("title")
                or raw_week.get("displayName")
                or f"Menu {index + 1}"
            )
            meals_selected = coerce_int(
                raw_week.get("mealsSelected")
                or raw_week.get("selectedMealCount")
                or self._find_first_nested_value(
                    raw_week,
                    (
                        "mealsSelected",
                        "selectedMealCount",
                        "selectedRecipesCount",
                        "mealCountSelected",
                    ),
                )
                or (sum(1 for recipe in recipes if recipe.is_selected) if raw_recipes else None)
            )
            weeks.append(
                HelloFreshWeek(
                    week_id=week_id,
                    display_name=display_name,
                    subscription_id=subscription.subscription_id,
                    delivery_date=parse_date(raw_week.get("deliveryDate") or raw_week.get("date")),
                    selection_deadline=parse_datetime(
                        raw_week.get("selectionDeadline") or raw_week.get("cutoffDate")
                    ),
                    status=raw_week.get("status") or "menu",
                    meals_required=subscription.meals_required,
                    meals_selected=meals_selected,
                    recipes=recipes,
                    source="account_menu_api",
                    menu_title=raw_week.get("title") or raw_week.get("displayName"),
                    raw=raw_week,
                )
            )
        return weeks

    def _extract_menu_week_candidates(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Return the most likely list of menu-week objects from a nested payload."""
        direct_candidates = (
            payload.get("weeks"),
            payload.get("items"),
            payload.get("menus"),
        )
        for candidate in direct_candidates:
            normalized = normalize_candidate_dict_list(candidate)
            if normalized:
                return normalized

        nested = self._find_nested_menu_week_candidates(payload)
        return nested or []

    _MENU_WEEK_KEYS = (
        "weeks",
        "items",
        "menus",
        "menuWeeks",
        "menuEntries",
        "data",
        "menu",
        "menusBySubscription",
        "subscriptionMenu",
    )

    def _find_nested_menu_week_candidates(self, node: Any) -> list[dict[str, Any]] | None:
        """Recursively search a payload for menu-like week collections."""
        return find_nested_collection(
            node, self._MENU_WEEK_KEYS, self._looks_like_menu_week_collection
        )

    def _looks_like_menu_week_collection(self, candidate: list[dict[str, Any]]) -> bool:
        """Heuristically identify menu-week collections."""
        if not candidate:
            return False
        identity_keys = {
            "id",
            "week",
            "calendarWeek",
            "label",
            "title",
            "displayName",
            "deliveryDate",
            "date",
        }
        for item in candidate:
            if identity_keys.intersection(item) and self._extract_menu_week_recipe_candidates(item):
                return True
        return False

    def _extract_recipe_candidates(
        self,
        node: Any,
        priority_keys: Sequence[str],
        *,
        fallback_to_node: bool,
    ) -> list[dict[str, Any]]:
        """Extract recipe-like items from a week payload.

        Tries each of ``priority_keys`` in order, returning the first non-empty
        recipe collection found beneath it. When ``fallback_to_node`` is true and no
        keyed branch matched, the whole node is searched recursively.
        """
        if not isinstance(node, dict):
            return []

        for key in priority_keys:
            if key not in node:
                continue
            recipes = self._find_nested_recipe_candidates(node[key])
            if recipes:
                return recipes

        if fallback_to_node:
            return self._find_nested_recipe_candidates(node)
        return []

    def _extract_menu_week_recipe_candidates(self, node: Any) -> list[dict[str, Any]]:
        """Extract recipe-like items from a menu-week payload.

        ``courses`` is the container the ``/gw/menus-service/menus`` items use (each course
        wraps its recipe in a nested ``recipe`` object, which the recipe normalizer unwraps).
        """
        return self._extract_recipe_candidates(
            node,
            ("recipes", "meals", "menuItems", "items", "dishes", "entries", "courses"),
            fallback_to_node=False,
        )

    _RECIPE_KEYS = (
        "items",
        "entries",
        "nodes",
        "edges",
        "data",
        "results",
        "recipes",
        "meals",
    )

    def _find_nested_recipe_candidates(self, node: Any) -> list[dict[str, Any]]:
        """Recursively search a payload fragment for recipe-like collections."""
        return (
            find_nested_collection(
                node, self._RECIPE_KEYS, looks_like_recipe_collection, dict_first=False
            )
            or []
        )

    def _reset_debug_trace(self) -> None:
        """Reset per-refresh debug trace data."""
        self._debug_trace = {
            "menu_attempts": [],
            "delivery_attempts": [],
            "tracking_attempts": [],
            "profile_attempts": [],
            "history_attempts": [],
        }

    def _normalize_past_delivery_payload(
        self,
        payload: dict[str, Any],
        subscriptions: Sequence[HelloFreshSubscription],
    ) -> list[HelloFreshWeek]:
        """Normalize delivered history payloads into stable week models."""
        raw_weeks = self._extract_past_delivery_candidates(payload)
        if not raw_weeks:
            return []

        subscriptions_by_id = {
            subscription.subscription_id: subscription for subscription in subscriptions
        }
        default_subscription = subscriptions[0] if subscriptions else None
        weeks: list[HelloFreshWeek] = []

        for index, raw_week in enumerate(raw_weeks):
            week_id = str(raw_week.get("week") or raw_week.get("id") or f"past-week-{index}")
            delivery_date = parse_date(
                raw_week.get("delivery_date")
                or raw_week.get("deliveryDate")
                or raw_week.get("date")
            )
            if week_id.startswith("past-week-") and delivery_date is None:
                continue

            subscription_id = str(
                raw_week.get("subscription_id")
                or raw_week.get("subscriptionId")
                or default_subscription.subscription_id
                if default_subscription is not None
                else ""
            )
            if not subscription_id:
                continue

            subscription = subscriptions_by_id.get(subscription_id, default_subscription)
            recipes = [
                self._recipe_from_raw_meal(raw_recipe)
                for raw_recipe in self._extract_past_delivery_recipes(raw_week)
            ]
            display_name = raw_week.get("label") or raw_week.get("title") or week_id
            weeks.append(
                HelloFreshWeek(
                    week_id=week_id,
                    display_name=str(display_name),
                    subscription_id=subscription_id,
                    delivery_date=delivery_date,
                    status=raw_week.get("status") or "delivered",
                    meals_required=coerce_int(raw_week.get("recipe_count"))
                    or (subscription.meals_required if subscription is not None else None)
                    or len(recipes)
                    or None,
                    meals_selected=len(recipes) or None,
                    recipes=recipes,
                    source="past_deliveries",
                    slot_label=self._find_first_nested_value(
                        raw_week,
                        ("deliveryName", "timeSlot", "slotLabel"),
                    ),
                    shipping_method=self._find_first_nested_value(
                        raw_week,
                        ("type", "deliveryType"),
                    ),
                    box_size=self._find_first_nested_value(raw_week, ("boxSize", "size")),
                    sub_status=raw_week.get("subStatus"),
                    delivery_state=raw_week.get("state"),
                    actionable=bool(raw_week.get("actionable")),
                    prepaid=bool(raw_week.get("prepaid")),
                    delivery_blocked=bool(
                        raw_week.get("deliveryBlocked") or raw_week.get("isBlocked")
                    ),
                    holiday_delivery_date=parse_date(raw_week.get("holidayDelivery")),
                    holiday_message=raw_week.get("holidayMessage"),
                    holiday_shift_visible=bool(raw_week.get("isHolidayShiftVisible")),
                    allowed_actions=extract_allowed_actions(raw_week),
                    available_one_off_options=self._extract_available_one_off_options(raw_week),
                    raw=raw_week,
                )
            )

        return weeks

    def _extract_past_delivery_candidates(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Return the most likely delivered-history list from a payload."""
        direct_candidates = (
            payload.get("data"),
            payload.get("items"),
            payload.get("deliveries"),
        )
        for candidate in direct_candidates:
            normalized = normalize_candidate_dict_list(candidate)
            if normalized and self._looks_like_past_delivery_collection(normalized):
                return normalized

        nested = self._find_nested_past_delivery_candidates(payload)
        return nested or []

    _PAST_DELIVERY_KEYS = ("data", "items", "deliveries", "pastDeliveries", "orders")

    def _find_nested_past_delivery_candidates(self, node: Any) -> list[dict[str, Any]] | None:
        """Recursively search a payload for past-delivery collections."""
        return find_nested_collection(
            node, self._PAST_DELIVERY_KEYS, self._looks_like_past_delivery_collection
        )

    def _looks_like_past_delivery_collection(self, candidate: list[dict[str, Any]]) -> bool:
        """Heuristically identify delivered-history payloads."""
        if not candidate:
            return False
        for item in candidate:
            if {"week", "delivery_date", "recipes"}.intersection(item):
                return True
            if {"deliveryDate", "recipes"}.intersection(
                item
            ) and self._extract_past_delivery_recipes(item):
                return True
        return False

    def _extract_past_delivery_recipes(self, raw_week: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract recipe-like payloads from a delivered week record."""
        return self._extract_recipe_candidates(
            raw_week,
            ("recipes", "items", "meals", "selectedMeals"),
            fallback_to_node=True,
        )

    def _record_debug_attempt(self, category: str, details: dict[str, Any]) -> None:
        """Append a sanitized debug event for diagnostics."""
        if category not in self._debug_trace:
            self._debug_trace[category] = []
        self._debug_trace[category].append(details)

    def _summarize_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return a compact summary of a response payload for diagnostics."""
        summary: dict[str, Any] = {
            "top_level_keys": sorted(str(key) for key in payload),
        }

        for key in ("weeks", "items", "menus"):
            value = payload.get(key)
            if isinstance(value, list):
                summary[f"{key}_count"] = len(value)
                if value and isinstance(value[0], dict):
                    summary[f"{key}_first_item"] = self._summarize_structure(value[0])
                    summary[f"{key}_first_item_keys"] = sorted(
                        str(item_key) for item_key in value[0]
                    )
                    interesting_paths = self._find_interesting_paths(value[0])
                    if interesting_paths:
                        summary[f"{key}_interesting_paths"] = interesting_paths

        data_node = payload.get("data")
        if isinstance(data_node, dict):
            summary["data_keys"] = sorted(str(key) for key in data_node)
            for key in ("weeks", "items", "menus", "menuWeeks", "menuEntries"):
                value = data_node.get(key)
                if isinstance(value, list):
                    summary[f"data_{key}_count"] = len(value)
                    if value and isinstance(value[0], dict):
                        summary[f"data_{key}_first_item"] = self._summarize_structure(value[0])
                        summary[f"data_{key}_first_item_keys"] = sorted(
                            str(item_key) for item_key in value[0]
                        )
                        interesting_paths = self._find_interesting_paths(value[0])
                        if interesting_paths:
                            summary[f"data_{key}_interesting_paths"] = interesting_paths

        return summary

    def _summarize_structure(self, node: Any, depth: int = 0) -> dict[str, Any] | list[str] | str:
        """Return a small structural preview of nested payload data."""
        if depth >= 2:
            if isinstance(node, dict):
                return sorted(str(key) for key in node)
            if isinstance(node, list):
                return [f"list[{len(node)}]"]
            return type(node).__name__

        if isinstance(node, dict):
            summary: dict[str, Any] = {}
            for key in sorted(str(key) for key in node)[:12]:
                value = node.get(key)
                if isinstance(value, dict):
                    summary[key] = {
                        "type": "dict",
                        "keys": self._summarize_structure(value, depth + 1),
                    }
                elif isinstance(value, list):
                    entry: dict[str, Any] = {"type": "list", "count": len(value)}
                    if value and isinstance(value[0], dict):
                        entry["first_item"] = self._summarize_structure(value[0], depth + 1)
                    summary[key] = entry
                else:
                    summary[key] = type(value).__name__
            return summary

        if isinstance(node, list):
            if not node:
                return []
            first = node[0]
            if isinstance(first, dict):
                return [f"list[{len(node)}]", str(self._summarize_structure(first, depth + 1))]
            return [f"list[{len(node)}]", type(first).__name__]

        return type(node).__name__

    def _find_interesting_paths(self, node: Any) -> list[str]:
        """Return nested paths that may reveal recipes, counts, or selection data."""
        interesting_keys = {
            "meals",
            "recipes",
            "selectedMeals",
            "menuItems",
            "selection",
            "menu",
            "box",
            "delivery",
            "entries",
            "nodes",
            "requiredMealCount",
            "selectedMealCount",
            "mealsRequired",
            "mealsSelected",
            "recipeCount",
            "numberOfRecipes",
        }

        paths: list[str] = []

        def walk(current: Any, path: str, depth: int) -> None:
            if depth > 3 or len(paths) >= 20:
                return
            if isinstance(current, dict):
                for key, value in current.items():
                    key_str = str(key)
                    next_path = f"{path}.{key_str}" if path else key_str
                    if key_str in interesting_keys:
                        if isinstance(value, dict):
                            descriptor = (
                                f"{next_path} (dict:{','.join(sorted(str(k) for k in value)[:8])})"
                            )
                        elif isinstance(value, list):
                            descriptor = f"{next_path} (list[{len(value)}])"
                        else:
                            descriptor = f"{next_path} ({type(value).__name__})"
                        if descriptor not in paths:
                            paths.append(descriptor)
                    walk(value, next_path, depth + 1)
            elif isinstance(current, list) and current:
                walk(current[0], f"{path}[0]" if path else "[0]", depth + 1)

        walk(node, "", 0)
        return paths
