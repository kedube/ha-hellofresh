"""Payload normalization helpers for the HelloFresh client.

The client owns HTTP/auth orchestration; this mixin owns conversion from
HelloFresh payloads into integration models.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
import re
from typing import Any

from .const import DEFAULT_MENU_GRACE_WEEKS
from .models import (
    HelloFreshMarketItem,
    HelloFreshOrder,
    HelloFreshRecipe,
    HelloFreshSubscription,
    HelloFreshWeek,
)
from .parsers import (
    MAX_SEARCH_DEPTH,
    clean_optional_str,
    coerce_float,
    coerce_int,
    date_from_iso_week,
    extract_allowed_actions,
    extract_name_list,
    extract_tracking_details,
    find_nested_collection,
    looks_like_recipe_collection,
    money_to_cents,
    normalize_candidate_dict_list,
    parse_date,
    parse_datetime,
    slugify,
)

# Fallback billing currency per config-flow country key, used only when a payload carries no
# currency of its own (see _extract_currency_code). Keys must stay in sync with
# COUNTRY_BASE_URLS in const.py. The Nordic markets each use their own krone/krona -- DKK, NOK
# and SEK are distinct currencies despite sharing the "kr" symbol -- and Switzerland bills in
# CHF, not EUR.
_COUNTRY_CURRENCIES = {
    "us": "USD",
    "ca": "CAD",
    "uk": "GBP",
    "au": "AUD",
    "nz": "NZD",
    "de": "EUR",
    "at": "EUR",
    "ch": "CHF",
    "nl": "EUR",
    "be": "EUR",
    "lu": "EUR",
    "fr": "EUR",
    "ie": "EUR",
    "dk": "DKK",
    "no": "NOK",
    "se": "SEK",
}

# Path segments that carry a per-account identifier the diagnostics export must not leak.
# Each pattern captures the fixed segment before an id and replaces the id with a placeholder,
# so ``/gw/api/subscriptions/12345/oneoff`` becomes ``/gw/api/subscriptions/{id}/oneoff``.
# Key-name redaction in diagnostics.py can't reach these because the value is *in* the path
# string, not a dict key — see _record_debug_attempt.
_DEBUG_PATH_REDACTIONS = (
    # /gw/api/subscriptions/<id>/… and /gw/api/customers/me/subscriptions/<id>/…
    (re.compile(r"(/subscriptions/)[^/]+"), r"\1{id}"),
    # /gw/api/plans/<customerPlanId>/…
    (re.compile(r"(/plans/)[^/]+"), r"\1{id}"),
    # /gw/payments/customers/<customerUUID>/…
    (re.compile(r"(/customers/)[^/]+(/balance)"), r"\1{id}\2"),
    # /gw/scm/tracking-ids/track/public-id/<uuid>
    (re.compile(r"(/public-id/)[^/?]+"), r"\1{id}"),
)


_CSS_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def _safe_css_color(value: Any) -> str | None:
    """Return ``value`` only when it is a plain hex color, else None.

    Badge colors are forwarded into card inline styles, so anything but a strict
    ``#RGB``/``#RRGGBB``/``#RRGGBBAA`` literal is dropped rather than passed through.
    """
    if isinstance(value, str) and _CSS_HEX_COLOR_RE.match(value):
        return value
    return None


def _template_debug_path(path: str) -> str:
    """Replace per-account id segments in a diagnostics path with ``{id}`` placeholders."""
    for pattern, replacement in _DEBUG_PATH_REDACTIONS:
        path = pattern.sub(replacement, path)
    return path


class HelloFreshPayloadNormalizer:
    """Mixin for pure-ish HelloFresh payload normalization methods."""

    @staticmethod
    def _effective_week_status(raw_week: dict[str, Any]) -> str:
        """Return the week's status, preferring the live ``state`` lifecycle field.

        HelloFresh's top-level ``status`` can be stale: a box still being prepared was
        observed with ``status="DELIVERED"`` while ``state="PREPARING"``. The ``state``
        field tracks the actual lifecycle (e.g. ``PREPARING``/``RUNNING``/``DELIVERED``),
        so when it says the box has *not* been delivered it takes precedence; otherwise
        fall back to ``status``/``deliveryStatus``.
        """
        state = raw_week.get("state")
        if isinstance(state, str) and state.strip() and state.strip().upper() != "DELIVERED":
            return state
        return raw_week.get("status") or raw_week.get("deliveryStatus") or state or "scheduled"

    @classmethod
    def _delivered_at_from_raw(cls, raw_week: dict[str, Any]) -> datetime | None:
        """Return the actual delivered timestamp for a DELIVERED week, else None.

        The deliveries payload's ``tracking.delivery_date`` is a real carrier timestamp once
        the box has arrived (e.g. ``2026-06-29T22:20:50+0000`` — HAR-verified), unlike the
        week's ``deliveryDate`` which is a scheduled-noon anchor. Before delivery the same
        tracking field holds a scheduled placeholder, so it is only meaningful once the
        effective status says DELIVERED.
        """
        if str(cls._effective_week_status(raw_week)).strip().upper() != "DELIVERED":
            return None
        tracking = raw_week.get("tracking")
        if not isinstance(tracking, dict):
            return None
        return parse_datetime(
            tracking.get("delivery_date") or tracking.get("estimated_delivery_time")
        )

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

            # Explicit None checks, not ``or``: a real ``mealsSelected: 0`` (nothing chosen
            # yet) must win over the recipe-derived fallback, which counts HelloFresh's
            # auto-fill picks as selected and would fabricate a full selection.
            meals_selected_raw: Any = raw_week.get("mealsSelected")
            if meals_selected_raw is None:
                meals_selected_raw = raw_week.get("selectedMealCount")
            if meals_selected_raw is None:
                meals_selected_raw = self._find_first_nested_value(
                    raw_week,
                    (
                        "mealsSelected",
                        "selectedMealCount",
                        "selectedRecipesCount",
                        "mealCountSelected",
                    ),
                )
            if meals_selected_raw is None and raw_meals:
                meals_selected_raw = sum(1 for recipe in recipes if recipe.is_selected)
            meals_selected = coerce_int(meals_selected_raw)
            # A week's required meal count is the size of ITS OWN box, which can differ from the
            # subscription's base plan when the week has been resized (e.g. a 2-meal box on a
            # 3-meal plan). The per-week ``product.specs.meals`` is authoritative; only fall back
            # to fuzzier fields and the subscription plan when the week doesn't carry its own box.
            meals_required = coerce_int(
                self._week_box_meal_count(raw_week)
                or raw_week.get("mealsRequired")
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
                delivered_at=self._delivered_at_from_raw(raw_week),
                selection_deadline=parse_datetime(
                    raw_week.get("selectionDeadline")
                    or raw_week.get("cutoffDate")
                    or raw_week.get("deadline")
                ),
                status=self._effective_week_status(raw_week),
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

    @staticmethod
    def _recipe_node(raw_meal: dict[str, Any]) -> dict[str, Any]:
        """Return the dict actually carrying a meal's recipe fields.

        Most payloads nest them under ``recipe``, but the past-deliveries endpoint puts them
        directly on the meal AND still emits an empty ``recipe: {}`` alongside. Testing only
        ``isinstance(..., dict)`` therefore picked the empty dict and lost the name, image and
        video for every delivered meal, so the nested node is used only when it is non-empty.
        """
        nested = raw_meal.get("recipe")
        if isinstance(nested, dict) and nested:
            return nested
        return raw_meal

    def _extract_recipe_id_from_raw_meal(self, raw_meal: dict[str, Any]) -> str:
        """Extract just the recipe id from a raw meal dict without full object construction."""
        recipe_data = self._recipe_node(raw_meal)
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
        variation_titles: dict[int, str] | None = None,
    ) -> HelloFreshRecipe:
        """Create a recipe from a recipe-like payload."""
        recipe_data = self._recipe_node(raw_meal)
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
        # Persist the chosen serving count so the dashboard can show/edit quantity. When the
        # meal is selected but the payload gave no explicit count, treat it as a single serving.
        if selection_quantity and selection_quantity > 0:
            selected_quantity = selection_quantity
        elif is_selected:
            selected_quantity = 1
        else:
            selected_quantity = None

        protein_g = coerce_float(nutrition.get("protein") or recipe_data.get("protein"))

        # Per-serving surcharge for premium/variant meals lives on the menu item's `charge`
        # object (`{label, unitAmount, ...}`); it's the clearest signal distinguishing
        # same-named portion/premium variants the catalog lists separately.
        charge = raw_meal.get("charge") if isinstance(raw_meal.get("charge"), dict) else {}
        surcharge_label = charge.get("label") or None
        surcharge_cents = coerce_int(charge.get("unitAmount"))

        # Menu badge (e.g. "Premium Picks") from the recipe's `label` object, with HelloFresh's
        # own colors so cards can paint the badge as the website does. Colors are gated to
        # #RGB/#RRGGBB(AA) hex: they land in inline styles, so a payload can't smuggle CSS.
        label = recipe_data.get("label") if isinstance(recipe_data.get("label"), dict) else {}
        badge = label.get("text") or None
        badge_foreground = _safe_css_color(label.get("foregroundColor"))
        badge_background = _safe_css_color(label.get("backgroundColor"))

        # Variant modifier title ("2x Bacon", "Ground Turkey", ...) resolved from the week's
        # `modularity` block by this meal's `index`. This is what actually distinguishes
        # same-named variants whose price/nutrition can otherwise look identical.
        course_index = coerce_int(raw_meal.get("index"))
        variation_title = None
        if variation_titles and course_index is not None:
            variation_title = variation_titles.get(course_index)

        # The protein category (Poultry/Beef/Pork/Seafood) drives the tile's color dot. Meatless
        # dishes carry no protein `category`, so fall back to "Veggie" when the recipe is tagged
        # Veggie/Vegan — otherwise a plant-based meal shows an unlabeled neutral dot with no
        # signal that it's meatless. Matches the card's PREFERENCE_COLORS "Veggie" swatch.
        preference = recipe_data.get("preference") or recipe_data.get("category")
        if not preference and any(tag in ("Veggie", "Vegan") for tag in tags):
            preference = "Veggie"

        return HelloFreshRecipe(
            recipe_id=str(
                recipe_data.get("id")
                or recipe_data.get("slug")
                or raw_meal.get("id")
                or slugify(name)
                or name
            ),
            name=name,
            preference=preference,
            is_selected=is_selected,
            selected_quantity=selected_quantity,
            course_index=course_index,
            image_url=recipe_data.get("imagePath")
            or recipe_data.get("image")
            or recipe_data.get("imageUrl"),
            # `videoLink` is the only spelling HelloFresh uses for the promo clip, and it is
            # simply absent on most meals, so a missing value is normal rather than an error.
            # It appears on the RECIPE node in my-deliveries/menu and menus-service, but on the
            # MEAL wrapper in past-deliveries — so both are checked, recipe node first. Reading
            # only one of them silently drops the clip for the other payload shape.
            video_url=clean_optional_str(recipe_data.get("videoLink"))
            or clean_optional_str(raw_meal.get("videoLink")),
            description=recipe_data.get("description") or recipe_data.get("headline"),
            ingredients=ingredients,
            allergens=allergens,
            tags=tags,
            nutrition=nutrition,
            cook_time_minutes=cook_time,
            prep_time_minutes=prep_time,
            total_time_minutes=total_time,
            calories_kcal=calories_kcal,
            protein_g=protein_g,
            difficulty=recipe_data.get("difficulty") or recipe_data.get("skillLevel"),
            surcharge_label=surcharge_label,
            surcharge_cents=surcharge_cents,
            badge=badge,
            badge_foreground=badge_foreground,
            badge_background=badge_background,
            variation_title=variation_title,
            # Availability flags live on the MEAL/course wrapper (menus-service), not on the
            # recipe node, so they are read from raw_meal regardless of which node won above.
            is_sold_out=bool(raw_meal.get("isSoldOut")),
            is_hidden=bool(raw_meal.get("isHidden")),
            related_category=clean_optional_str(raw_meal.get("relatedCategory")),
            **self._price_fields(raw_meal),
            **self._feedback_fields(recipe_data),
        )

    @staticmethod
    def _price_fields(raw_meal: dict[str, Any]) -> dict[str, Any]:
        """Extract the meal's real per-serving price from the delivery menu's ``itemPrice``.

        Distinct from the ``charge`` block already parsed as ``surcharge_*``: that is only the
        premium *uplift*, whereas this is what the serving actually costs. Only the delivery
        menu carries it (menus-service omits it), so absence is normal.
        """
        item_price = raw_meal.get("itemPrice")
        if not isinstance(item_price, dict):
            return {}
        per_unit = item_price.get("pricePerUnit")
        cents = money_to_cents(per_unit)
        out: dict[str, Any] = {"price_group": clean_optional_str(item_price.get("group"))}
        if cents is not None:
            out["price_cents"] = cents
            out["price"] = cents / 100
        if isinstance(per_unit, dict):
            out["currency"] = clean_optional_str(per_unit.get("currencyCode"))
        return out

    @staticmethod
    def _feedback_fields(recipe_data: dict[str, Any]) -> dict[str, Any]:
        """Extract HelloFresh's per-recipe ``feedback`` block.

        It carries EITHER a delivery-history pair (``productDeliveryCount`` +
        ``lastDeliveryWeek``) OR the customer's own star rating (``rating`` + ``ratingScale``),
        never both — so each is mapped independently rather than assuming one shape.
        """
        feedback = recipe_data.get("feedback")
        if not isinstance(feedback, dict):
            return {}
        out: dict[str, Any] = {}
        delivered = coerce_int(feedback.get("productDeliveryCount"))
        if delivered is not None:
            out["delivered_count"] = delivered
        last_week = clean_optional_str(feedback.get("lastDeliveryWeek"))
        if last_week:
            out["last_delivered_week"] = last_week
        rating = coerce_int(feedback.get("rating"))
        if rating is not None:
            out["rating"] = rating
            out["rating_scale"] = coerce_int(feedback.get("ratingScale"))
        return out

    @staticmethod
    def _build_variation_titles(raw_week: dict[str, Any]) -> dict[int, str]:
        """Map a meal `index` to its variant modifier title from the `modularity` block.

        HelloFresh lists portion/ingredient variants of a dish as separate meals sharing the
        same name; the `modularity` array names how each variant differs ("2x Bacon",
        "Ground Turkey", ...) via the variation/addOn `index`, which equals the meal's `index`.

        The block can sit on the week payload directly or, for weeks assembled by merging the
        authenticated menu catalog into an account/deliveries week, under ``_menu_payload``.
        Both locations are checked so the modifier is resolved regardless of which endpoint
        produced the week.
        """
        titles: dict[int, str] = {}
        modularity = raw_week.get("modularity")
        if not isinstance(modularity, list):
            nested = raw_week.get("_menu_payload")
            if isinstance(nested, dict):
                modularity = nested.get("modularity")
        if not isinstance(modularity, list):
            return titles
        for group in modularity:
            if not isinstance(group, dict):
                continue
            for key in ("variations", "addOns"):
                items = group.get(key)
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    idx = coerce_int(item.get("index"))
                    title = item.get("title")
                    if idx is not None and isinstance(title, str) and title.strip():
                        titles.setdefault(idx, title.strip())
        return titles

    @staticmethod
    def _build_variation_groups(raw_week: dict[str, Any]) -> dict[int, int]:
        """Map every meal `index` in a variant group to that group's base dish index.

        Keyed on the modularity group's ``defaultCourseIndex`` (the base dish). Each of the
        group's variation/addOn indexes — AND the base index itself — maps to that base index,
        so all members of a dish's variant set share one group key even when their names differ
        (e.g. a Salmon dish whose variants include an "Icelandic Cod" swap). Used to group a
        dish's variants together in the meal-planner card.
        """
        groups: dict[int, int] = {}
        modularity = raw_week.get("modularity")
        if not isinstance(modularity, list):
            nested = raw_week.get("_menu_payload")
            if isinstance(nested, dict):
                modularity = nested.get("modularity")
        if not isinstance(modularity, list):
            return groups
        for group in modularity:
            if not isinstance(group, dict):
                continue
            base = coerce_int(group.get("defaultCourseIndex"))
            if base is None:
                continue
            member_indexes = {base}
            for key in ("variations", "addOns"):
                items = group.get(key)
                if not isinstance(items, list):
                    continue
                for item in items:
                    if isinstance(item, dict):
                        idx = coerce_int(item.get("index"))
                        if idx is not None:
                            member_indexes.add(idx)
            # A single-member "group" (base with no variants) is not a variant set — skip it so
            # standalone dishes don't each get a spurious one-element group key.
            if len(member_indexes) < 2:
                continue
            for idx in member_indexes:
                groups.setdefault(idx, base)
        return groups

    def _apply_variation_titles(self, weeks: Sequence[HelloFreshWeek]) -> None:
        """Fill in each recipe's ``variation_title`` and ``variation_group`` from ``modularity``.

        Recipes are built by several normalization paths (delivery weeks, menu weeks, past
        deliveries) and only some pass the modularity map through at build time. This pass runs
        once over the fully-assembled week list so the variant modifier ("2x Bacon", ...) and the
        variant-group key are present no matter which path produced a given week — resolved by
        ``course_index``, the same index the modularity block keys on. Existing values are left
        untouched.
        """
        for week in weeks:
            if not week.recipes or not isinstance(week.raw, dict):
                continue
            titles = self._build_variation_titles(week.raw)
            group_by_index = self._build_variation_groups(week.raw)
            if not titles and not group_by_index:
                continue
            for recipe in week.recipes:
                if recipe.course_index is not None and recipe.variation_group is None:
                    recipe.variation_group = group_by_index.get(recipe.course_index)
                if recipe.variation_title:
                    continue
                if recipe.course_index is not None:
                    recipe.variation_title = titles.get(recipe.course_index)

    def _market_item_from_raw(
        self, raw_item: dict[str, Any], group_type: str | None
    ) -> HelloFreshMarketItem | None:
        """Build a market item (Market add-on) from one ``addOns.groups[].addOns[]`` entry."""
        recipe_data = (
            raw_item.get("recipe") if isinstance(raw_item.get("recipe"), dict) else raw_item
        )
        name = recipe_data.get("name") or recipe_data.get("title")
        index = coerce_int(raw_item.get("index"))
        if not name or index is None:
            return None

        nutrition = self._extract_nutrition(recipe_data)
        calories = coerce_float(nutrition.get("calories") or recipe_data.get("calories"))

        price_catalog = (
            raw_item.get("priceCatalog") if isinstance(raw_item.get("priceCatalog"), dict) else {}
        )
        price_cents = coerce_int(price_catalog.get("basePrice"))
        price = round(price_cents / 100, 2) if price_cents is not None else None

        # Selected quantity lives on the item's `selection` block when chosen. Market add-ons use
        # `oneOffQuantity` (this-week add) + `preselectedQuantity` (recurring add) — NOT `quantity`
        # (which is what meals use). Unselected items have `selection: null`.
        selection = raw_item.get("selection") if isinstance(raw_item.get("selection"), dict) else {}
        one_off = coerce_int(selection.get("oneOffQuantity")) or 0
        preselected = coerce_int(selection.get("preselectedQuantity")) or 0
        selected_quantity = (one_off + preselected) or coerce_int(
            selection.get("quantity") or raw_item.get("quantity")
        )
        is_selected = bool(selected_quantity and selected_quantity > 0)

        # `item_id` falls back to the SKU or index when no recipe id is present, so it cannot
        # be handed to the recipe-detail API. Keep the real recipe id separately (None when
        # absent) so a caller can tell "look this up" from "there is nothing to look up".
        raw_recipe_id = recipe_data.get("id")
        return HelloFreshMarketItem(
            item_id=str(recipe_data.get("id") or raw_item.get("sku") or index),
            recipe_id=str(raw_recipe_id)
            if isinstance(raw_recipe_id, str) and raw_recipe_id
            else None,
            name=name,
            index=index,
            sku=raw_item.get("sku"),
            group_type=group_type,
            image_url=recipe_data.get("image")
            or recipe_data.get("imagePath")
            or recipe_data.get("imageUrl"),
            description=recipe_data.get("headline") or recipe_data.get("description"),
            category=recipe_data.get("category"),
            tags=extract_name_list(recipe_data.get("tags")),
            nutrition=nutrition,
            calories_kcal=calories,
            price_cents=price_cents,
            price=price,
            max_quantity=coerce_int(raw_item.get("maxQuantity")),
            is_selected=is_selected,
            selected_quantity=selected_quantity if is_selected else None,
            preselected_quantity=preselected or None,
            is_locked=bool(raw_item.get("isLocked")),
            is_sold_out=bool(raw_item.get("isSoldOut")),
        )

    def _build_market_items(self, raw_week: dict[str, Any]) -> list[HelloFreshMarketItem]:
        """Parse the week's Market add-on catalog from its ``addOns.groups`` block.

        Looks on the week payload directly or under ``_menu_payload`` (set when the authenticated
        menu catalog was merged into an account/deliveries week), mirroring the variation-title
        lookup so market items resolve regardless of which endpoint produced the week.
        """
        addons = raw_week.get("addOns")
        if not isinstance(addons, dict):
            nested = raw_week.get("_menu_payload")
            if isinstance(nested, dict):
                addons = nested.get("addOns")
        if not isinstance(addons, dict):
            return []

        items: list[HelloFreshMarketItem] = []
        seen_indexes: set[int] = set()
        for group in addons.get("groups") or []:
            if not isinstance(group, dict):
                continue
            group_type = group.get("groupType")
            for raw_item in group.get("addOns") or []:
                if not isinstance(raw_item, dict):
                    continue
                item = self._market_item_from_raw(raw_item, group_type)
                if item is None or item.index in seen_indexes:
                    continue
                seen_indexes.add(item.index)
                items.append(item)
        return items

    def _apply_market_items(self, weeks: Sequence[HelloFreshWeek]) -> None:
        """Attach each week's Market add-on catalog, parsed from its menu payload."""
        for week in weeks:
            if week.market_items or not isinstance(week.raw, dict):
                continue
            items = self._build_market_items(week.raw)
            if items:
                week.market_items = items

    def _build_menu_categories(self, raw_week: dict[str, Any]) -> list[dict[str, Any]]:
        """Parse the menu payload's ``categories`` block into the week's menu sections.

        These are the sections the website's menu page shows (This Week's Menu, Health
        Conscious Menu, Family Menu, Bestsellers, Your Top Recipes, ...). Each output row is
        ``{name, slug, recipe_ids}``; ``recipe_ids`` merges the section's own ``items`` with
        every subcategory's, because a section like "Featured" lists ONLY subcategories and
        would otherwise come out empty. The Market pseudo-section (slug ``market``) is
        skipped — its members are add-ons, not meals, so it can never match the recipe grid.
        """
        block = raw_week.get("categories")
        if not isinstance(block, dict):
            nested = raw_week.get("_menu_payload")
            if isinstance(nested, dict):
                block = nested.get("categories")
        rows = block.get("categories") if isinstance(block, dict) else None

        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            name = clean_optional_str(row.get("name"))
            slug = clean_optional_str(row.get("slug"))
            if not name or not slug or slug == "market":
                continue
            ids: list[str] = []
            seen: set[str] = set()

            def _collect(items: Any) -> None:
                for item in items if isinstance(items, list) else []:
                    item_id = item.get("id") if isinstance(item, dict) else None
                    if isinstance(item_id, str) and item_id and item_id not in seen:
                        seen.add(item_id)
                        ids.append(item_id)

            _collect(row.get("items"))
            for sub in row.get("subcategories") if isinstance(row.get("subcategories"), list) else []:
                if isinstance(sub, dict):
                    _collect(sub.get("items"))
            if ids:
                out.append({"name": name, "slug": slug, "recipe_ids": ids})
        return out

    def _apply_menu_categories(self, weeks: Sequence[HelloFreshWeek]) -> None:
        """Attach each week's menu sections, parsed from its menu payload."""
        for week in weeks:
            if week.menu_categories or not isinstance(week.raw, dict):
                continue
            categories = self._build_menu_categories(week.raw)
            if categories:
                week.menu_categories = categories

    def _clear_paused_week_selection(self, weeks: Sequence[HelloFreshWeek]) -> None:
        """Clear phantom selections on paused/skipped weeks — nothing was ever delivered.

        A paused/skipped week's menu still carries the system's auto-fill picks
        (``selection.quantity``), which the merges turn into ``is_selected``. But that box never
        shipped, so those are phantom selections; zero them and the selected count.

        For a PAST paused/skipped week the planning-menu catalog (which can be the full selectable
        menu — hundreds of meals) is also meaningless: the week never shipped and can't be edited,
        so it must show an empty meal list, not a flood. Drop its recipes entirely. A FUTURE
        paused week keeps its catalog so it stays browsable if the customer un-pauses — as does a
        week still inside the menu grace window (``menu_grace_weeks``), where the catalog is
        the real published menu, matching the grace treatment of shipped weeks. Market items
        are left alone — pausing meals is independent of add-ons.
        """
        # Delivery dates are local-market calendar dates — use LOCAL today, matching
        # models.py, so week classification can't flip near midnight UTC.
        today = date.today()
        grace_floor = today - timedelta(weeks=self.menu_grace_weeks)
        for week in weeks:
            if not (week.is_paused or week.is_skipped):
                continue
            is_past = week.delivery_date is not None and week.delivery_date < grace_floor
            if is_past:
                week.recipes = []
            else:
                for recipe in week.recipes:
                    recipe.is_selected = False
                    recipe.selected_quantity = None
            week.meals_selected = 0

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

    @staticmethod
    def _week_box_meal_count(raw_week: dict[str, Any]) -> int | None:
        """Return the meal count of a week's OWN box from its ``product.specs.meals``.

        This is the per-delivery box size (which can differ from the subscription's base plan
        when the week was resized). Read explicitly from ``product.specs`` / ``productType.specs``
        rather than a fuzzy nested search so an unrelated ``meals`` key elsewhere can't win.
        """
        for product_key in ("product", "productType"):
            product = raw_week.get(product_key)
            if not isinstance(product, dict):
                continue
            specs = product.get("specs")
            if isinstance(specs, dict):
                count = coerce_int(specs.get("meals"))
                if count:
                    return count
        return None

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

    # Default weeks of past history to request when the client doesn't supply one (~6 months).
    # The instance value ``self._history_lookback_weeks`` (set from the user option,
    # DEFAULT_HISTORY_WEEKS in const.py) overrides this; callers that don't set it fall back to
    # this default. Used for BOTH the ranged display window and the past-deliveries pagination
    # floor so display and fetch stay aligned. Note: a value near a full year (52w) lands ~364
    # days back, so the box from ~12 months ago sits on the ISO-week boundary — raising the
    # option a few weeks past 52 keeps that boundary week visible.
    _HISTORY_LOOKBACK_WEEKS = 26

    @property
    def _history_weeks(self) -> int:
        """Configured history lookback in weeks (falls back to the class default)."""
        return getattr(self, "_history_lookback_weeks", None) or self._HISTORY_LOOKBACK_WEEKS

    @property
    def menu_grace_weeks(self) -> int:
        """Configured menu grace window in whole weeks (falls back to the const default).

        Weeks after its delivery date that a week keeps its full browsable menu (with the
        delivered meals overlaid as the selection) before collapsing to delivered-only. The
        instance value ``self._menu_grace_weeks_option`` is set from the user option
        (DEFAULT_MENU_GRACE_WEEKS in const.py); 0 is a valid value (grace disabled), so only
        None falls back. Public because the ``get_weeks`` service exposes it to the
        meal-planner card, which mirrors the same window in its past-week gating.
        """
        option = getattr(self, "_menu_grace_weeks_option", None)
        return DEFAULT_MENU_GRACE_WEEKS if option is None else option

    # How many weeks ahead to request deliveries for. HelloFresh schedules deliveries further out
    # than it publishes *menus*, so reaching a bit past the menu horizon is fine — weeks beyond the
    # published menu come back with no recipes and are filtered out downstream (the meal-planner
    # card only shows weeks that actually have menu data). The deliveries payload stays bounded
    # because those empty future weeks are tiny.
    _FUTURE_DELIVERY_WEEKS = 8

    def _build_delivery_history_range(self) -> dict[str, str]:
        """Return the range for account delivery lookups.

        Spans ``self._history_weeks`` of history so the meal-planner card and history sensors can
        browse the configured window of past boxes — the API supports far wider ranges, but the
        cap keeps the per-poll deliveries payload bounded. Extends ``_FUTURE_DELIVERY_WEEKS``
        ahead; weeks past the published-menu horizon return empty and are filtered downstream.
        """
        # Delivery dates are local-market calendar dates — use LOCAL today, matching
        # models.py, so week classification can't flip near midnight UTC.
        today = date.today()
        start = today - timedelta(weeks=self._history_weeks)
        end = today + timedelta(weeks=self._FUTURE_DELIVERY_WEEKS)
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
            status=self._derive_subscription_status(raw_subscription),
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
            # planPreference is the resolved active preference (written back onto the raw payload
            # by the client's preference resolution); fall back to preset when not yet resolved.
            plan_preference=(
                raw_subscription.get("planPreference") or raw_subscription.get("preset")
            ),
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

    def _derive_subscription_status(self, raw_subscription: dict[str, Any]) -> str | None:
        """Derive the plan-level status from the subscriptions payload.

        The live ``/gw/api/customers/me/subscriptions`` response carries **no** ``status`` /
        ``subscriptionStatus`` / ``state`` field (confirmed from US HAR captures). The real
        signal is split across ``canceledAt`` / ``pausedAt`` timestamps and the ``isActive``
        boolean, so status is derived from those. An explicit ``status`` / ``state`` field, if
        a region or future payload ever provides one, still wins. (``endlessPausedAt`` is NOT
        used: it carries a stale historical date even on active accounts.)
        """
        explicit = (
            raw_subscription.get("status")
            or raw_subscription.get("subscriptionStatus")
            or raw_subscription.get("state")
            or self._find_first_nested_value(
                raw_subscription, ("status", "subscriptionStatus", "state")
            )
        )
        if explicit:
            return str(explicit)

        if raw_subscription.get("canceledAt"):
            return "cancelled"
        if raw_subscription.get("pausedAt"):
            return "paused"
        is_active = raw_subscription.get("isActive")
        if is_active is True:
            return "active"
        if is_active is False:
            return "inactive"
        return None

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
        menu_week.delivered_at = account_week.delivered_at or menu_week.delivered_at
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
            # The subscriptions payload has no allowedActions dict, but a week surfaced via
            # nextModifiableDeliveryWeek is by definition still changeable — mark meal swaps
            # allowed so models.is_editable (which requires the flag, mirroring the card)
            # doesn't silently drop these fallback weeks from the needs-selection sensors.
            # The cutoff-date gate still applies via selection_deadline.
            allowed_actions=(
                {"mealSwap": True}
                if raw_subscription.get("nextModifiableDeliveryWeek") == week_id
                else {}
            ),
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
        # A single week can arrive as several menu variants under the same id (the
        # ``/gw/menus-service/menus`` fallback returns ~16 product/preset variants per week,
        # one of which is the full catalog and the rest small subsets). Keep the richest
        # (most recipes) per (subscription, week) so the week gets its full browsable catalog
        # instead of whichever variant happened to be last.
        menu_by_key: dict[tuple[str | None, str], HelloFreshWeek] = {}
        for menu_week in menu_weeks:
            key = (menu_week.subscription_id, menu_week.week_id)
            existing = menu_by_key.get(key)
            if existing is None or len(menu_week.recipes) > len(existing.recipes):
                menu_by_key[key] = menu_week
        merged_weeks: list[HelloFreshWeek] = []

        for account_week in account_weeks:
            menu_week = menu_by_key.get((account_week.subscription_id, account_week.week_id))
            if menu_week is None or not menu_week.recipes:
                merged_weeks.append(account_week)
                continue

            # Resolve which recipes are selected, from whichever source actually knows.
            #
            # Two payload shapes exist:
            #  * The account/deliveries week lists the chosen recipes directly. Here the
            #    presence of a recipe (or its is_selected flag) IS the selection, so derive
            #    the selected id set from the account week and project it onto the catalog.
            #  * The account week carries no recipes (the common case: the deliveries endpoint
            #    returns counts but no recipe list). Then the MENU week is authoritative — each
            #    chosen meal arrives with selection.quantity > 0, which _recipe_from_raw_meal
            #    has already turned into is_selected. In that case we must PRESERVE the menu
            #    week's own flags; recomputing from the (empty) account week would blank every
            #    recipe even though the menu payload said which were chosen.
            # Project the account week's selection onto the full menu catalog.
            account_selected_ids = {
                recipe.recipe_id for recipe in account_week.recipes if recipe.is_selected
            }
            # When nothing is explicitly flagged, fall back to "presence == selection" ONLY if
            # the account week is a selection-sized list (the deliveries endpoint lists just the
            # chosen recipes). If it instead holds a full browsable catalog — i.e. about as many
            # recipes as the menu week — that fallback would mark the whole catalog selected,
            # which is what fabricated the wrong selection on past weeks. In that case derive
            # nothing here and let the menu week's own flags / past-deliveries decide.
            if not account_selected_ids and len(account_week.recipes) < len(menu_week.recipes):
                account_selected_ids = {recipe.recipe_id for recipe in account_week.recipes}
            if account_selected_ids:
                for recipe in menu_week.recipes:
                    recipe.is_selected = recipe.recipe_id in account_selected_ids

            account_week.recipes = menu_week.recipes
            account_week.menu_title = menu_week.menu_title or account_week.menu_title
            # The auto-pick flag (mealsPreselected) is only on the menu payload; carry it over.
            account_week.meals_preselected = (
                account_week.meals_preselected or menu_week.meals_preselected
            )
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

    def _merge_past_delivery_recipes_into_account_weeks(
        self,
        account_weeks: Sequence[HelloFreshWeek],
        past_delivery_weeks: Sequence[HelloFreshWeek],
    ) -> list[HelloFreshWeek]:
        """Fill account weeks that lack recipes with the meals actually delivered that week.

        The ranged deliveries endpoint returns past weeks as metadata-only shells (no recipe
        list), and the planning-menu endpoint does not serve real history. The past-deliveries
        payload is the authoritative source of what was delivered, keyed by ``week``. Match on
        ``week_id`` (subscription id when present on both) and copy the delivered recipes onto
        any account week that does not already have its own — never overwriting a week that
        already carries recipes (an upcoming week filled from the live menu).
        """
        if not past_delivery_weeks:
            return list(account_weeks)

        past_by_key: dict[tuple[str | None, str], HelloFreshWeek] = {}
        past_by_week_id: dict[str, HelloFreshWeek] = {}
        for past_week in past_delivery_weeks:
            if not past_week.recipes:
                continue
            past_by_key[(past_week.subscription_id, past_week.week_id)] = past_week
            # id-only index used ONLY when a subscription id is missing on one side; with
            # two subscriptions the same ISO week maps to two different delivered menus.
            past_by_week_id[past_week.week_id] = past_week

        # Delivery dates are local-market calendar dates — use LOCAL today, matching
        # models.py, so week classification can't flip near midnight UTC.
        today = date.today()
        grace_floor = today - timedelta(weeks=self.menu_grace_weeks)
        for account_week in account_weeks:
            # Only touch weeks that are actually in the PAST. The deliveries/past-deliveries
            # endpoints can surface the current week (once its cutoff passes it starts reporting
            # as "delivered"), and replacing a current/future week's full browsable menu with
            # just the delivered/selected meals would strip its menu so the customer can no
            # longer see or change their options. A week with no delivery_date, or one dated
            # today or later, is current/future — leave its menu intact.
            if account_week.delivery_date is None or account_week.delivery_date >= today:
                continue

            past_week = past_by_key.get((account_week.subscription_id, account_week.week_id))
            if past_week is None:
                candidate = past_by_week_id.get(account_week.week_id)
                # Fall back to the id-only match ONLY when a subscription id is missing on
                # either side. When both sides carry (different) subscription ids, matching
                # by week id alone would stamp another subscription's delivered meals onto
                # this week.
                if candidate is not None and (
                    candidate.subscription_id is None or account_week.subscription_id is None
                ):
                    past_week = candidate
            if past_week is None:
                continue

            # This week actually shipped (it has delivered-history data), so whatever was sent IS
            # the real selection — not HelloFresh's auto-pick. The menu's ``mealsPreselected``
            # flag for a long-past week is stale/default and must not stick, or the card would
            # wrongly badge a week you personally chose as "Preselected".
            account_week.meals_preselected = False

            delivered = self._delivered_meals_only(account_week, past_week.recipes)
            if not delivered:
                continue

            # A week within the menu grace window (delivered less than menu_grace_weeks ago)
            # keeps its full browsable catalog: HelloFresh still publishes the REAL menu for the
            # immediately previous week, and the per-week fetch validated the payload's week id,
            # so the catalog is trustworthy. Delivered history stays the source of truth for the
            # SELECTION — overlay it onto the catalog rather than trusting the menu's own flags.
            # A recent week whose menu fetch was rejected (no catalog) falls through to the
            # delivered-only replacement below, same as an old week.
            if account_week.delivery_date >= grace_floor and account_week.recipes:
                self._overlay_delivered_selection(account_week, delivered)
                account_week.menu_title = account_week.menu_title or past_week.menu_title
                account_week.meals_selected = past_week.meals_selected or len(delivered)
                account_week.meals_required = (
                    past_week.meals_required or account_week.meals_required
                )
                continue

            # An OLDER past week shows exactly the meals that were DELIVERED. We deliberately do
            # NOT keep whatever the planning-menu endpoint attached as a "browsable catalog": for
            # an old week HelloFresh has no real menu, so it returns a bloated multi-week
            # AGGREGATE (~1000+ dishes spanning many weeks), and there is no reliable structural
            # signal to tell that apart from a genuine per-week menu (only fragile size
            # heuristics). Rather than risk flooding a past week with meals that were never
            # available that week, the delivered set (from past-deliveries, with images) is
            # authoritative and replaces it.
            for recipe in delivered:
                recipe.is_selected = True
            account_week.recipes = delivered
            account_week.menu_title = account_week.menu_title or past_week.menu_title
            # Counts follow what actually shipped, overriding any stale menu/plan values.
            account_week.meals_selected = past_week.meals_selected or len(delivered)
            account_week.meals_required = past_week.meals_required or account_week.meals_required
            # The browsable catalog was replaced by the delivered set, so the (possibly
            # multi-MB aggregate) menu payload stashed on this week is now dead weight kept
            # alive for the whole poll interval. Drop it — nothing reads it back for an old
            # week (writes/pricing only touch editable current/future weeks).
            account_week.raw.pop("_menu_payload", None)

        return list(account_weeks)

    @staticmethod
    def _overlay_delivered_selection(
        account_week: HelloFreshWeek,
        delivered: Sequence[HelloFreshRecipe],
    ) -> None:
        """Mark exactly the delivered meals as selected within a week's browsable catalog.

        Used for past weeks inside the menu grace window, where the full catalog is kept but
        the menu payload's own selection flags can't be trusted for a shipped week (they revert
        to the system's auto-fill view). Clears every selection flag, then re-selects catalog
        entries matching a delivered meal by recipe id, falling back to a case-insensitive name
        match (past-deliveries ids don't always line up with menu ids). A delivered meal with no
        catalog match at all is appended, so what shipped is always visible.
        """
        catalog_by_id = {recipe.recipe_id: recipe for recipe in account_week.recipes}
        catalog_by_name: dict[str, HelloFreshRecipe] = {}
        for recipe in account_week.recipes:
            recipe.is_selected = False
            recipe.selected_quantity = None
            if recipe.name:
                catalog_by_name.setdefault(recipe.name.strip().casefold(), recipe)

        for delivered_recipe in delivered:
            match = catalog_by_id.get(delivered_recipe.recipe_id)
            if match is None and delivered_recipe.name:
                match = catalog_by_name.get(delivered_recipe.name.strip().casefold())
            if match is None:
                delivered_recipe.is_selected = True
                account_week.recipes.append(delivered_recipe)
                continue
            match.is_selected = True
            match.selected_quantity = delivered_recipe.selected_quantity

    def _merge_past_delivery_market_items(
        self,
        account_weeks: Sequence[HelloFreshWeek],
        past_delivery_weeks: Sequence[HelloFreshWeek],
    ) -> None:
        """Stamp each past week's PURCHASED Market add-ons onto its account week.

        A week's browsable ``addOns`` catalog only exists while HelloFresh still publishes that
        week's menu (~2-3 weeks, the menu-grace window). Beyond it the sole record of Market
        activity is the ``addons`` array on past-deliveries, so without this merge every older
        week reports an empty catalog and drops out of the Market card entirely -- which is why
        the card's history collapsed to the grace window while My Menu kept its full span.

        Purchased items REPLACE a past week's catalog rather than merging into it: for a shipped
        week the question is "what did I order", and the catalog (when one survived) lists
        everything that was merely on offer. Weeks with no recorded purchase are left untouched
        so an in-grace week keeps its catalog and still renders normally.
        """
        if not past_delivery_weeks:
            return

        past_by_key: dict[tuple[str | None, str], HelloFreshWeek] = {}
        past_by_week_id: dict[str, HelloFreshWeek] = {}
        for past_week in past_delivery_weeks:
            if not past_week.market_items:
                continue
            past_by_key[(past_week.subscription_id, past_week.week_id)] = past_week
            past_by_week_id[past_week.week_id] = past_week
        if not past_by_key:
            return

        today = date.today()
        for account_week in account_weeks:
            # Only PAST weeks. A current/future week's live catalog is what makes it editable,
            # and past-deliveries can list the current week once its cutoff passes -- replacing
            # that catalog would strip the customer's ability to change their order.
            if account_week.delivery_date is None or account_week.delivery_date >= today:
                continue

            past_week = past_by_key.get((account_week.subscription_id, account_week.week_id))
            if past_week is None:
                candidate = past_by_week_id.get(account_week.week_id)
                # Same guard as the recipe merge: fall back to an id-only match ONLY when a
                # subscription id is absent on one side, so two subscriptions sharing an ISO
                # week can't inherit each other's purchases.
                if candidate is not None and (
                    candidate.subscription_id is None or account_week.subscription_id is None
                ):
                    past_week = candidate
            if past_week is None:
                continue

            account_week.market_items = list(past_week.market_items)

    def _delivered_meals_only(
        self,
        account_week: HelloFreshWeek,
        delivered_recipes: Sequence[HelloFreshRecipe],
    ) -> list[HelloFreshRecipe]:
        """Return the delivered MEALS, with market add-ons filtered out.

        The delivered record includes ordered market add-ons (appetizers/sides/desserts)
        alongside the box meals, but those belong to the Market view, not the meal list. A
        delivered item that is a known market item for the week is dropped so it never shows in
        My Menu; the market merge marks those selected separately. Known items come from BOTH the
        week's raw ``addOns`` catalog and its already-merged purchased add-ons -- past the
        menu-grace window no catalog survives, so the purchased list is the only thing that can
        still identify an add-on and keep it out of the meal list.
        """
        market_ids: set[str] = set()
        market_names: set[str] = set()

        def _remember(item: HelloFreshMarketItem) -> None:
            if item.item_id:
                market_ids.add(item.item_id)
            if item.recipe_id:
                market_ids.add(item.recipe_id)
            if item.name:
                market_names.add(item.name.strip().casefold())

        for item in account_week.market_items:
            _remember(item)
        if isinstance(account_week.raw, dict):
            for item in self._build_market_items(account_week.raw):
                _remember(item)

        meals: list[HelloFreshRecipe] = []
        for delivered in delivered_recipes:
            delivered_name = delivered.name.strip().casefold() if delivered.name else ""
            if delivered.recipe_id in market_ids or delivered_name in market_names:
                continue
            meals.append(delivered)
        return meals

    def _normalize_menu_weeks(
        self,
        raw_weeks: list[dict[str, Any]],
        subscription: HelloFreshSubscription,
    ) -> list[HelloFreshWeek]:
        """Normalize menu-style payloads into public menu week models."""
        weeks: list[HelloFreshWeek] = []
        for index, raw_week in enumerate(raw_weeks):
            raw_recipes = self._extract_menu_week_recipe_candidates(raw_week)
            variation_titles = self._build_variation_titles(raw_week)
            recipes = [
                self._recipe_from_raw_meal(
                    raw_recipe,
                    default_selected=False,
                    variation_titles=variation_titles,
                )
                for raw_recipe in raw_recipes
            ]
            if not recipes:
                continue

            # Prefer the ISO calendar week (``week``: "2026-W26") over ``id``. The
            # ``/gw/menus-service/menus`` items carry BOTH an internal Mongo ``id`` and the
            # ISO ``week``; keying by ``id`` produced ObjectId week ids that never match the
            # account weeks in the merge, so a week's catalog got mis-attached (and the
            # menus-service "first item" absorbed ~all courses → 1000+ recipes on one week).
            # Account weeks and past-deliveries are keyed by the ISO week, so use it here too.
            week_id = str(
                raw_week.get("week")
                or raw_week.get("calendarWeek")
                or raw_week.get("id")
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
                    # The week's own box size wins over the subscription's base plan (a resized
                    # week can hold fewer/more meals than the plan default).
                    meals_required=self._week_box_meal_count(raw_week)
                    or subscription.meals_required,
                    meals_selected=meals_selected,
                    meals_preselected=bool(raw_week.get("mealsPreselected")),
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
            # The /gw/my-deliveries/past-deliveries payload identifies each delivered week by its
            # ISO ``week`` id ONLY (no explicit date), so derive the date from the week id when no
            # date field is present — otherwise these weeks stay date-less and "Last delivery date"
            # is Unknown even though the box shipped.
            if delivery_date is None:
                delivery_date = date_from_iso_week(week_id)
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
            # The Market add-ons this week actually shipped with. Carried on the history week so
            # _merge_past_delivery_market_items can stamp them onto the matching account week.
            purchased_market_items = self._build_purchased_market_items(raw_week)
            weeks.append(
                HelloFreshWeek(
                    week_id=week_id,
                    display_name=str(display_name),
                    subscription_id=subscription_id,
                    delivery_date=delivery_date,
                    status=raw_week.get("status") or "delivered",
                    # For a HISTORICAL week, the number of meals actually delivered (len(recipes))
                    # is the truth for that week — prefer it over the CURRENT subscription's plan,
                    # which can differ (e.g. a 4-meal box that week vs a 3-meal plan today) and
                    # would otherwise cap a past week at the wrong count.
                    meals_required=coerce_int(raw_week.get("recipe_count"))
                    or len(recipes)
                    or (subscription.meals_required if subscription is not None else None)
                    or None,
                    meals_selected=len(recipes) or None,
                    recipes=recipes,
                    market_items=purchased_market_items,
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

    def _build_purchased_market_items(self, raw_week: dict[str, Any]) -> list[HelloFreshMarketItem]:
        """Parse the Market add-ons a past week ACTUALLY shipped with.

        Distinct from :meth:`_build_market_items`, which reads the browsable ``addOns`` CATALOG
        (capital O, ``{groups: [{addOns: [...]}]}``) published on a week's menu payload. The
        past-deliveries history endpoint instead carries a flat lowercase ``addons`` array of the
        items the customer bought that week — the only record of a purchase once HelloFresh stops
        serving that week's menu (which it does after ~2-3 weeks). The two shapes never coexist on
        one payload, so they get separate parsers rather than one overloaded key lookup.

        History entries are deliberately sparse: no ``index``, ``quantity``, ``price`` or
        ``groupType``. So:

        * ``index`` is left None. It is the CART SELECTION UNIT used to submit writes, and a
          synthesized one could address the wrong product; a past week is not editable, so it is
          never needed. This is also why these items cannot go through
          ``_market_item_from_raw``, which requires an index.
        * ``group_type`` is left None. The card groups by it when present and falls back to a
          plain ordered list otherwise, which is the intended presentation for weeks older than
          the menu-grace window where no catalog (and therefore no grouping) survives.
        * quantity is recorded as 1 — history reports no count, and one unit is what a listed
          add-on represents.
        """
        raw_addons = raw_week.get("addons")
        if not isinstance(raw_addons, list):
            return []

        items: list[HelloFreshMarketItem] = []
        seen_ids: set[str] = set()
        for raw_item in raw_addons:
            if not isinstance(raw_item, dict):
                continue
            name = raw_item.get("name") or raw_item.get("title")
            if not name:
                continue
            # ``id`` is a normal 24-hex recipe id here, so it doubles as the recipe reference
            # that powers the recipe-detail lookup.
            item_id = str(
                raw_item.get("id")
                or raw_item.get("shoppableProductId")
                or str(name).strip().casefold()
            )
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)

            recipe_id = raw_item.get("id")
            nutrition = self._extract_nutrition(raw_item)
            items.append(
                HelloFreshMarketItem(
                    item_id=item_id,
                    name=str(name),
                    recipe_id=str(recipe_id) if recipe_id else None,
                    image_url=raw_item.get("image") or raw_item.get("imageUrl"),
                    description=raw_item.get("headline") or raw_item.get("description"),
                    category=raw_item.get("category"),
                    tags=[
                        str(tag.get("name"))
                        for tag in raw_item.get("tags") or []
                        if isinstance(tag, dict) and tag.get("name")
                    ],
                    nutrition=nutrition,
                    calories_kcal=coerce_float(nutrition.get("calories")),
                    is_selected=True,
                    selected_quantity=1,
                )
            )
        return items

    def _extract_past_delivery_recipes(self, raw_week: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract recipe-like payloads from a delivered week record."""
        return self._extract_recipe_candidates(
            raw_week,
            ("recipes", "items", "meals", "selectedMeals"),
            fallback_to_node=True,
        )

    def _record_debug_attempt(self, category: str, details: dict[str, Any]) -> None:
        """Append a sanitized debug event for diagnostics.

        The diagnostics export redacts sensitive values by *key name*, which cannot reach an
        identifier baked into a path string (e.g. ``/gw/api/subscriptions/12345/oneoff``). So
        before storing, any recorded ``path`` has its known-identifier segments templated out
        here — this is the single choke point every debug attempt flows through, so it covers
        current and future call sites without per-site care.
        """
        if category not in self._debug_trace:
            self._debug_trace[category] = []
        path = details.get("path")
        if isinstance(path, str):
            details = {**details, "path": _template_debug_path(path)}
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
