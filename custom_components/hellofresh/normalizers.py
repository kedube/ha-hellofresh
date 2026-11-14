"""Payload normalization helpers for the HelloFresh client.

The client owns HTTP/auth orchestration; this mixin owns conversion from
HelloFresh payloads into integration models.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
import logging
import re
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from .const import DEFAULT_MENU_GRACE_WEEKS, RECIPE_IMAGE_BASE
from .models import (
    _DEAD_IMAGE_HOST,
    HelloFreshMarketItem,
    HelloFreshOrder,
    HelloFreshRecipe,
    HelloFreshSubscription,
    HelloFreshWeek,
    WeekLookup,
    _iso_duration_to_minutes,
    _usable_image_url,
)
from .parsers import (
    MAX_SEARCH_DEPTH,
    box_handle_parts,
    clean_optional_str,
    coerce_float,
    coerce_int,
    date_from_iso_week,
    dict_or_empty,
    dict_path,
    extract_allowed_actions,
    extract_name_list,
    extract_tracking_details,
    find_nested_collection,
    http_url_or_none,
    local_date,
    local_today,
    looks_like_recipe_collection,
    money_to_cents,
    normalize_candidate_dict_list,
    parse_date,
    parse_datetime,
    slugify,
)

_LOGGER = logging.getLogger(__name__)

# A modularity option's ingredient photo is linked on the retired CloudFront host
# (".../200,200/ingredient/<file>.png", HTTP 502), but the working host serves the same file under
# the bare "/ingredient/<file>" path that other payloads use (HTTP 200, checked 2026-09-29).
_DEAD_INGREDIENT_PATH = re.compile(r"^/(?:[^/]+/)?(ingredient/[^/]+)$")


def _option_image_url(ingredient: dict[str, Any]) -> str | None:
    """Return a loadable photo URL for a customization option's ingredient."""
    candidates: list[str] = []
    for key in ("imageUrl", "imageURL", "imagePath", "image_path"):
        value = ingredient.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        parts = urlsplit(value)
        if parts.hostname == _DEAD_IMAGE_HOST and (
            match := _DEAD_INGREDIENT_PATH.match(parts.path)
        ):
            value = f"/{match.group(1)}"
        candidates.append(value)
    return _usable_image_url(candidates, RECIPE_IMAGE_BASE)


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


def _coerce_minutes(value: Any) -> int | None:
    """Whole minutes from either spelling HelloFresh uses for a time field.

    Some payloads carry plain integer minutes; the authenticated weekly menu carries
    ISO-8601 durations (``"PT35M"``, observed 2026-W35). Coercing with ``int()`` alone
    silently dropped the ISO form, leaving every menu recipe with no time data — which
    made the meal-planner card's Total Cooking Time filter match nothing.
    """
    result = coerce_int(value)
    if result is not None:
        return result
    return _iso_duration_to_minutes(value)


_CSS_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def _safe_css_color(value: Any) -> str | None:
    """Return ``value`` only when it is a plain hex color, else None.

    Badge colors are forwarded into card inline styles, so anything but a strict
    ``#RGB``/``#RRGGBB``/``#RRGGBBAA`` literal is dropped rather than passed through.
    """
    if isinstance(value, str) and _CSS_HEX_COLOR_RE.match(value):
        return value
    return None


# What building a model from an entry of an unexpected shape raises (a string where an object
# was expected, a missing key, ...). Caught per entry, so one bad week or meal is left out
# instead of failing the whole refresh.
_MALFORMED_PAYLOAD_ERRORS = (AttributeError, KeyError, TypeError, ValueError, IndexError)
# How many skipped entries one diagnostics trace keeps.
_MAX_SKIPPED_ENTRIES = 50


def _template_debug_path(path: str) -> str:
    """Replace per-account id segments in a diagnostics path with ``{id}`` placeholders."""
    for pattern, replacement in _DEBUG_PATH_REDACTIONS:
        path = pattern.sub(replacement, path)
    return path


class _ModularityMaps(NamedTuple):
    """What a week's ``modularity`` block says about its meals, keyed by meal ``index``.

    See ``HelloFreshPayloadNormalizer._modularity_maps`` for what each map holds.
    """

    titles: dict[int, str]
    groups: dict[int, int]
    details: dict[int, dict[str, Any]]


class HelloFreshPayloadNormalizer:
    """Mixin for pure-ish HelloFresh payload normalization methods."""

    # The diagnostics trace: event lists by category. The client creates it, and
    # _reset_debug_trace starts it over for each refresh.
    _debug_trace: dict[str, list[dict[str, Any]]]

    # ``subStatus`` phases that follow delivery: the meals are to be cooked, then rated.
    _DELIVERED_SUB_STATUSES = frozenset({"COOK_IT", "RATING"})

    @classmethod
    def _effective_week_status(cls, raw_week: dict[str, Any]) -> str:
        """Return the week's status, preferring the live ``state`` lifecycle field.

        HelloFresh's top-level ``status`` can be stale: a box still being prepared was
        observed with ``status="DELIVERED"`` while ``state="PREPARING"``. The ``state``
        field tracks the actual lifecycle (e.g. ``PREPARING``/``RUNNING``/``DELIVERED``),
        so when it says the box has *not* been delivered it takes precedence; otherwise
        fall back to ``status``/``deliveryStatus``.

        HelloFresh keeps a box's progress for a few weeks only. Older weeks come back with
        ``state="UNKNOWN"``, ``status`` still ``RUNNING`` and no tracking (capture 62, W35-W37),
        which says nothing. Their ``subStatus`` is still ``RATING``, a phase after delivery, so
        such a week is delivered, as the website shows it.

        A live ``state`` still outranks that ``subStatus``: ``COOK_IT`` can come on the morning
        of delivery day, before the box arrives (issue #18).
        """
        # A state that is not a string is never returned: its key is empty either way.
        raw_state = raw_week.get("state")
        state = raw_state if isinstance(raw_state, str) else ""
        state_key = state.strip().upper()
        if state_key and state_key not in ("DELIVERED", "UNKNOWN"):
            return state
        sub_status = str(raw_week.get("subStatus") or "").strip().upper()
        if sub_status in cls._DELIVERED_SUB_STATUSES and not cls._raw_week_is_skipped(raw_week):
            return "DELIVERED"
        status = raw_week.get("status") or raw_week.get("deliveryStatus")
        return status or (state if state_key not in ("", "UNKNOWN") else "scheduled")

    @staticmethod
    def _raw_week_is_skipped(raw_week: dict[str, Any]) -> bool:
        """Return True when the customer skipped this week's box.

        HelloFresh marks a skipped week only with ``status: "PAUSED"`` (skipping PATCHes
        ``PAUSED``, unskipping ``RUNNING``); its delivery model has no ``skipped`` field, as
        HelloFresh's Android app confirms. ``state`` is checked too, because the week's
        effective status prefers it (see ``_effective_week_status``). The boolean flags are
        kept for any payload that does carry them.
        """
        if raw_week.get("skipped") or raw_week.get("isSkipped"):
            return True
        return any(
            isinstance(value, str) and value.strip().upper() in ("PAUSED", "SKIPPED")
            for value in (raw_week.get("status"), raw_week.get("state"))
        )

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
        # HelloFresh's collections use ``items``, ``data`` or ``result`` (the Android app accepts
        # all three); only a list counts, so a dict-shaped ``data`` can't turn into weeks.
        raw_weeks = next(
            (
                value
                for key in ("weeks", "items", "deliveries", "data", "result")
                if isinstance(value := payload.get(key), list) and value
            ),
            [],
        )
        weeks: list[HelloFreshWeek] = []
        orders: list[HelloFreshOrder] = []

        for index, raw_week in enumerate(raw_weeks):
            try:
                week = self._week_from_raw_delivery(index, raw_week, subscription)
                if week is None:
                    continue
                weeks.append(week)
                orders.append(self._order_from_raw_week(raw_week=raw_week, week=week))
            except _MALFORMED_PAYLOAD_ERRORS as err:
                # One malformed week must cost THAT week, not the whole poll — an
                # unguarded raise here failed the entire refresh. Drop it with a
                # warning and let every other week land.
                self._skip_malformed("HelloFresh week entry", index, err)
                continue

        return weeks, orders

    def _week_from_raw_delivery(
        self,
        index: int,
        raw_week: dict[str, Any],
        subscription: HelloFreshSubscription,
    ) -> HelloFreshWeek | None:
        """Build one week of a deliveries payload; None when it is another subscription's."""
        raw_subscription_id = raw_week.get("subscriptionId") or raw_week.get("subscription_id")
        if (
            raw_subscription_id is not None
            and str(raw_subscription_id) != subscription.subscription_id
        ):
            return None
        week_id = (
            raw_week.get("id")
            or raw_week.get("week")
            or raw_week.get("deliveryWeek")
            or raw_week.get("calendarWeek")
            or f"week-{index}"
        )
        # The week's box (`product.displayName`) names it when the week has no label of its
        # own; the delivery window is `deliveryOption.deliveryName`, and what kind of delivery
        # it is `deliveryOption.type`. These are read from where the API reference puts them:
        # the first `name` or `type` found anywhere in the week could be a meal tag's.
        box_name = dict_path(raw_week, "product", "displayName")
        display_name = (
            raw_week.get("label")
            or raw_week.get("title")
            or raw_week.get("displayName")
            or box_name
            or str(week_id)
        )
        raw_meals = self._extract_delivery_week_recipe_candidates(raw_week)
        recipes = self._recipes_from_raw_meals(raw_meals, where=f"week {week_id}")

        # A week's required meal count is the size of ITS OWN box, which can differ from the
        # subscription's base plan when the week has been resized (e.g. a 2-meal box on a
        # 3-meal plan). The per-week ``product.specs.meals`` is authoritative; only fall back
        # to other spellings and the subscription plan when the week doesn't carry its own box.
        meals_required = coerce_int(
            self._week_box_meal_count(raw_week)
            or raw_week.get("mealsRequired")
            or raw_week.get("requiredMealCount")
            or raw_week.get("recipeCount")
            # The reference documents no other home for a box size. These names say what they
            # are wherever they sit, so an older or regional payload that nests one is still
            # read; a bare `meals` is not searched for, since it is also the name of meal lists.
            or self._find_first_nested_value(
                raw_week,
                ("mealsRequired", "requiredMealCount", "recipeCount", "numberOfRecipes"),
            )
            or subscription.meals_required
        )

        return HelloFreshWeek(
            week_id=str(week_id),
            display_name=display_name,
            subscription_id=(
                str(raw_subscription_id)
                if raw_subscription_id is not None
                else subscription.subscription_id
            ),
            # A holiday-shifted box arrives on ``holidayDelivery``, so that is the date
            # shown everywhere (calendar, next order, the Week tab), as HelloFresh's own
            # app does. Writes still send the raw ``deliveryDate`` (see
            # ``_async_patch_delivery_status``), and the shifted date is also kept as
            # ``holiday_delivery_date`` for the holiday notices.
            delivery_date=parse_date(
                raw_week.get("holidayDelivery")
                or raw_week.get("deliveryDate")
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
            meals_selected=self._meals_selected_from_raw(
                raw_week, recipes if raw_meals else None, search_nested=True
            ),
            is_skipped=self._raw_week_is_skipped(raw_week),
            recipes=recipes,
            source="account",
            menu_title=raw_week.get("menuTitle") or raw_week.get("title") or box_name,
            slot_label=raw_week.get("timeSlot")
            or raw_week.get("slotLabel")
            or dict_path(raw_week, "deliveryOption", "deliveryName"),
            shipping_method=raw_week.get("shippingMethod")
            or dict_path(raw_week, "deliveryOption", "type")
            or subscription.shipping_method,
            box_size=raw_week.get("boxSize") or subscription.box_size,
            **self._common_week_fields(raw_week),
        )

    @staticmethod
    def _selected_portions(recipes: Iterable[HelloFreshRecipe]) -> int:
        """Return how many meals of a box the selected recipes fill.

        HelloFresh counts portions, not dishes: a meal at quantity 2 is two of the box's meals,
        so one doubled recipe is a complete 2-meal box.
        """
        return sum(recipe.selected_quantity or 1 for recipe in recipes if recipe.is_selected)

    def _meals_selected_from_raw(
        self,
        raw_week: dict[str, Any],
        recipes: Sequence[HelloFreshRecipe] | None,
        *,
        search_nested: bool,
    ) -> int | None:
        """Return a week's selected-meal count: the payload's own, else from its recipes.

        Explicit None checks, not ``or``: a real ``mealsSelected: 0`` (nothing chosen yet)
        must win over the recipe-derived fallback, which counts HelloFresh's auto-fill picks
        as selected and would fabricate a full selection. ``recipes`` is None when the payload
        listed no meals at all, which says nothing about the selection.

        ``search_nested`` looks for the count anywhere in the week. The API reference gives a
        deliveries week no selected count at all, so there is no path to read it from, and the
        names are specific enough that a match is the count. A menu week is not searched: its
        selection is documented, on each meal.
        """
        selected: Any = raw_week.get("mealsSelected")
        if selected is None:
            selected = raw_week.get("selectedMealCount")
        if selected is None and search_nested:
            selected = self._find_first_nested_value(
                raw_week,
                (
                    "mealsSelected",
                    "selectedMealCount",
                    "selectedRecipesCount",
                    "mealCountSelected",
                ),
            )
        if selected is None and recipes is not None:
            selected = self._selected_portions(recipes)
        return coerce_int(selected)

    def _recipes_from_raw_meals(
        self,
        raw_meals: Sequence[dict[str, Any]],
        *,
        where: str,
        **options: Any,
    ) -> list[HelloFreshRecipe]:
        """Build a recipe per raw meal, leaving out any that cannot be parsed.

        A menu holds hundreds of meals. One with an unexpected shape costs that meal, not the
        week it is in and not the refresh; the skips are reported once per week.
        """
        recipes: list[HelloFreshRecipe] = []
        skipped = 0
        first_error: Exception | None = None
        for raw_meal in raw_meals:
            try:
                recipes.append(self._recipe_from_raw_meal(raw_meal, **options))
            except _MALFORMED_PAYLOAD_ERRORS as err:
                skipped += 1
                first_error = first_error or err
        if skipped and first_error is not None:
            self._skip_malformed(f"{skipped} meal(s) in HelloFresh {where}", None, first_error)
        return recipes

    def _skip_malformed(self, what: str, index: int | None, err: Exception) -> None:
        """Log an entry that was left out, and note it in the diagnostics trace."""
        label = what if index is None else f"{what} {index}"
        _LOGGER.warning("%s could not be parsed and was skipped: %s", label, err)
        trace = getattr(self, "_debug_trace", None)
        if isinstance(trace, dict):
            skipped = trace.setdefault("skipped_entries", [])
            # Capped: the delivery-day fast poll re-reads the same weeks into one trace. The
            # error's type only, since its message can quote the payload.
            if len(skipped) < _MAX_SKIPPED_ENTRIES:
                skipped.append({"entry": label, "error": type(err).__name__})

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

    @staticmethod
    def _recipe_name(recipe_data: dict[str, Any]) -> str:
        """Return a meal's name from its recipe node, or a stand-in when it has none.

        Text only: every later pass strips and compares names, so a name of another type
        would fail the refresh there instead of costing one meal its label here.
        """
        for key in ("name", "title", "slug"):
            value = recipe_data.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return "Meal"

    def _extract_recipe_id_from_raw_meal(self, raw_meal: dict[str, Any]) -> str:
        """Extract just the recipe id from a raw meal dict without full object construction."""
        recipe_data = self._recipe_node(raw_meal)
        name = self._recipe_name(recipe_data)
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
        name = self._recipe_name(recipe_data)
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
        # NOTE the swapped naming in the authenticated menu payload: `prepTime` (PT35M) is
        # the headline time the website shows on the tile, while `totalTime` (PT5M) is the
        # smaller hands-on number. Both are mapped verbatim — consumers that want "the time
        # HelloFresh displays" must read prep_time_minutes (the card's time filter does).
        cook_time = _coerce_minutes(
            recipe_data.get("cookTime") or recipe_data.get("cookTimeMinutes")
        )
        prep_time = _coerce_minutes(
            recipe_data.get("prepTime") or recipe_data.get("prepTimeMinutes")
        )
        total_time = _coerce_minutes(
            recipe_data.get("totalTime")
            or recipe_data.get("totalTimeMinutes")
            or ((cook_time or 0) + (prep_time or 0) if cook_time or prep_time else None)
        )
        selection = dict_or_empty(raw_meal.get("selection"))
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
        charge = dict_or_empty(raw_meal.get("charge"))
        surcharge_label = charge.get("label") or None
        surcharge_cents = coerce_int(charge.get("unitAmount"))

        # Menu badge (e.g. "Premium Picks") from the recipe's `label` object, with HelloFresh's
        # own colors so cards can paint the badge as the website does. Colors are gated to
        # #RGB/#RRGGBB(AA) hex: they land in inline styles, so a payload can't smuggle CSS.
        label = dict_or_empty(recipe_data.get("label"))
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
            recipe_id=self._extract_recipe_id_from_raw_meal(raw_meal),
            name=name,
            preference=preference,
            is_selected=is_selected,
            selected_quantity=selected_quantity,
            course_index=course_index,
            # Full-URL fields first: the menu's `image` is on the working host. The menus-service
            # fallback has only a bare `imagePath` (+ a dead `imageLink`), customer-complaints a
            # bare `image_path`; those get the host joined on. See _usable_image_url.
            image_url=_usable_image_url(
                (
                    recipe_data.get("image"),
                    recipe_data.get("imageUrl"),
                    recipe_data.get("imagePath"),
                    recipe_data.get("image_path"),
                    recipe_data.get("imageLink"),
                ),
                RECIPE_IMAGE_BASE,
            ),
            # `videoLink` is the only spelling HelloFresh uses for the promo clip, and it is
            # simply absent on most meals, so a missing value is normal rather than an error.
            # It appears on the RECIPE node in my-deliveries/menu and menus-service, but on the
            # MEAL wrapper in past-deliveries — so both are checked, recipe node first. Reading
            # only one of them silently drops the clip for the other payload shape. Only an
            # http(s) link is kept: it ends up in a <video src>.
            video_url=http_url_or_none(recipe_data.get("videoLink"))
            or http_url_or_none(raw_meal.get("videoLink")),
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

    def _common_week_fields(self, raw_week: dict[str, Any]) -> dict[str, Any]:
        """The week fields read identically from any deliveries-shaped payload.

        Both the upcoming-deliveries normalizer and the past-deliveries normalizer built this
        same block verbatim; keeping one copy means a new lifecycle field is added in one
        place and both paths pick it up. Fields whose source differs between the two (week id,
        dates, box size, shipping method, ...) stay at the call sites.
        """
        return {
            "sub_status": raw_week.get("subStatus"),
            "revised_delivery_date": self._revised_delivery_date(raw_week),
            "servings": self._week_servings(raw_week),
            "payment_failed": str(raw_week.get("status") or "").strip().upper() == "PAYMENT_FAILED",
            "delivery_state": raw_week.get("state"),
            "actionable": bool(raw_week.get("actionable")),
            "prepaid": bool(raw_week.get("prepaid")),
            "delivery_blocked": bool(raw_week.get("deliveryBlocked") or raw_week.get("isBlocked")),
            "holiday_delivery_date": parse_date(raw_week.get("holidayDelivery")),
            "holiday_message": raw_week.get("holidayMessage"),
            "holiday_shift_visible": bool(raw_week.get("isHolidayShiftVisible")),
            "allowed_actions": extract_allowed_actions(raw_week),
            "available_one_off_options": self._extract_available_one_off_options(raw_week),
            "raw": raw_week,
        }

    @staticmethod
    def _menu_block(raw_week: dict[str, Any], key: str, expected: type) -> Any | None:
        """Return ``raw_week[key]``, falling back to the merged ``_menu_payload`` copy.

        A menu block (``modularity``, ``addOns``, ``categories``, ``filters``, ...) sits on the
        week payload directly, or — for weeks assembled by merging the authenticated menu
        catalog into an account/deliveries week — under ``_menu_payload``. Both locations are
        checked so the block resolves regardless of which endpoint produced the week.

        Returns ``None`` when neither location holds a value of ``expected`` type, so callers
        can bail with a single ``is None`` check instead of repeating the isinstance dance.
        """
        block = raw_week.get(key)
        if isinstance(block, expected):
            return block
        nested = raw_week.get("_menu_payload")
        if isinstance(nested, dict):
            block = nested.get(key)
            if isinstance(block, expected):
                return block
        return None

    @staticmethod
    def _modularity_options(group: Any) -> list[dict[str, Any]]:
        """A modularity group's customization options: its `variations`, in the site's order.

        Only `variations` are options. A group's `addOns` are Market cross-sell suggestions
        ("Garlic Bread", "Snickerdoodle Cookie Skillet") shown beside the dish: their indexes
        are Market items, never meals (HAR-verified on W41-W43), and every dish carries some.
        """
        items = group.get("variations") if isinstance(group, dict) else None
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    @classmethod
    def _modularity_maps(cls, raw_week: dict[str, Any]) -> _ModularityMaps:
        """Read a week's ``modularity`` block once, into three maps keyed by meal ``index``.

        * titles: an option's modifier title ("2x Bacon", "Ground Turkey"). HelloFresh lists
          the options of a dish as separate meals sharing its name, and this is how each
          differs.
        * groups: the base dish (the group's ``defaultCourseIndex``) of every member of a
          variant set, the base itself included, so a dish and its options share one key even
          when their names differ (a Salmon dish with an "Icelandic Cod" swap). Every meal also
          has an entry of its own, options included, usually without variations; such an entry
          is not a variant set and claims no key.
        * details: what the dish's customization list shows. An option gets ``order`` (its
          position in the website's list) and ``image_url`` (its ingredient photo); a base dish
          with options gets ``default_title``, the site's label for the unchanged version
          (``noVariationsDefaultTitle``: "No Change", "No Protein", "Ground Beef", ...).

        The block sits on the week payload directly or, for a week assembled by merging the
        menu into a deliveries week, under ``_menu_payload``; both are checked.
        """
        titles: dict[int, str] = {}
        groups: dict[int, int] = {}
        details: dict[int, dict[str, Any]] = {}
        for group in cls._menu_block(raw_week, "modularity", list) or []:
            options = cls._modularity_options(group)
            indexes = [coerce_int(item.get("index")) for item in options]
            for item, idx in zip(options, indexes, strict=True):
                title = item.get("title")
                if idx is not None and isinstance(title, str) and title.strip():
                    titles.setdefault(idx, title.strip())

            base = coerce_int(group.get("defaultCourseIndex")) if isinstance(group, dict) else None
            if base is None:
                continue
            members = {base, *(idx for idx in indexes if idx is not None)}
            if len(members) >= 2:
                for idx in members:
                    groups.setdefault(idx, base)
            if not options:
                continue
            default_title = clean_optional_str(group.get("noVariationsDefaultTitle"))
            if default_title and "default_title" not in details.get(base, {}):
                details.setdefault(base, {})["default_title"] = default_title
            for order, (item, idx) in enumerate(zip(options, indexes, strict=True)):
                if idx is None or idx == base or "order" in details.get(idx, {}):
                    continue
                ingredient = item.get("ingredient")
                details.setdefault(idx, {}).update(
                    order=order,
                    image_url=_option_image_url(ingredient)
                    if isinstance(ingredient, dict)
                    else None,
                )
        return _ModularityMaps(titles, groups, details)

    @classmethod
    def _build_variation_titles(cls, raw_week: dict[str, Any]) -> dict[int, str]:
        """Map a meal ``index`` to its variant modifier title (see ``_modularity_maps``)."""
        return cls._modularity_maps(raw_week).titles

    def _apply_variation_titles(self, weeks: Sequence[HelloFreshWeek]) -> None:
        """Fill in each recipe's ``variation_*`` fields from the week's ``modularity`` block.

        Recipes are built by several normalization paths (delivery weeks, menu weeks, past
        deliveries) and only some pass the modularity map through at build time. This pass runs
        once over the fully-assembled week list so the variant modifier ("2x Bacon", ...), the
        variant-group key and the customization-list details are present no matter which path
        produced a given week — resolved by ``course_index``, the same index the modularity
        block keys on. Existing values are left untouched.
        """
        for week in weeks:
            if not week.recipes or not isinstance(week.raw, dict):
                continue
            titles, group_by_index, options = self._modularity_maps(week.raw)
            if not titles and not group_by_index and not options:
                continue
            for recipe in week.recipes:
                if recipe.course_index is None:
                    continue
                if recipe.variation_group is None:
                    recipe.variation_group = group_by_index.get(recipe.course_index)
                if not recipe.variation_title:
                    recipe.variation_title = titles.get(recipe.course_index)
                detail = options.get(recipe.course_index, {})
                if recipe.variation_order is None:
                    recipe.variation_order = detail.get("order")
                if recipe.variation_image_url is None:
                    recipe.variation_image_url = detail.get("image_url")
                if recipe.variation_default_title is None:
                    recipe.variation_default_title = detail.get("default_title")

    def _market_item_from_raw(
        self,
        raw_item: dict[str, Any],
        group_type: str | None,
        group_sku: str | None = None,
    ) -> HelloFreshMarketItem | None:
        """Build a market item (Market add-on) from one ``addOns.groups[].addOns[]`` entry."""
        nested_recipe = raw_item.get("recipe")
        recipe_data = nested_recipe if isinstance(nested_recipe, dict) else raw_item
        name = recipe_data.get("name") or recipe_data.get("title")
        index = coerce_int(raw_item.get("index"))
        if not isinstance(name, str) or not name.strip() or index is None:
            return None

        nutrition = self._extract_nutrition(recipe_data)
        calories = coerce_float(nutrition.get("calories") or recipe_data.get("calories"))

        price_catalog = dict_or_empty(raw_item.get("priceCatalog"))
        price_cents = coerce_int(price_catalog.get("basePrice"))
        price = round(price_cents / 100, 2) if price_cents is not None else None

        # The selected quantity lives on the item's `selection` block (`selection: null` when
        # unselected), split into `preselectedQuantity`, the recurring quantity carried week to
        # week, and `oneOffQuantity`, THIS week's quantity when the customer changed it. As in
        # HelloFresh's Android app (`ExtrasDomainMapper.getQuantityChosen`), the week's quantity is
        # `oneOffQuantity` when one is set or the item is skipped for the week, otherwise the
        # recurring `preselectedQuantity`; the two are never added together.
        selection = dict_or_empty(raw_item.get("selection"))
        one_off = coerce_int(selection.get("oneOffQuantity")) or 0
        preselected = coerce_int(selection.get("preselectedQuantity")) or 0
        if selection.get("skipped") or one_off > 0:
            selected_quantity: int | None = one_off
        elif preselected > 0:
            selected_quantity = preselected
        else:
            selected_quantity = coerce_int(selection.get("quantity") or raw_item.get("quantity"))
        is_selected = bool(selected_quantity and selected_quantity > 0)
        # Pairing add-ons (`groupType: "modularity"`) are linked to a meal through their
        # selection's `courses`; a re-sent selection must keep that link. These are the MEALS'
        # indexes and stay apart from the item's own `index`, which is what the cart orders it
        # by: an assignment expression here once reused that name and overwrote it.
        course_indexes = (
            coerce_int(course.get("index"))
            for course in selection.get("courses") or []
            if isinstance(course, dict)
        )
        courses = [course_index for course_index in course_indexes if course_index is not None]
        # HelloFresh's app has no `maxQuantity`: an item's limit is its largest quantity option.
        option_quantities = (
            coerce_int(option.get("quantity"))
            for option in raw_item.get("quantityOptions") or []
            if isinstance(option, dict)
        )
        max_quantity = coerce_int(raw_item.get("maxQuantity")) or max(
            (quantity for quantity in option_quantities if quantity is not None),
            default=None,
        )

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
            # An add-on without its own SKU is ordered under its group's (as the app does).
            sku=raw_item.get("sku") or group_sku,
            group_type=group_type,
            image_url=_usable_image_url(
                (
                    recipe_data.get("image"),
                    recipe_data.get("imageUrl"),
                    recipe_data.get("imagePath"),
                    recipe_data.get("image_path"),
                ),
                RECIPE_IMAGE_BASE,
            ),
            description=recipe_data.get("headline") or recipe_data.get("description"),
            category=recipe_data.get("category"),
            tags=extract_name_list(recipe_data.get("tags")),
            nutrition=nutrition,
            calories_kcal=calories,
            price_cents=price_cents,
            price=price,
            max_quantity=max_quantity,
            is_selected=is_selected,
            selected_quantity=selected_quantity if is_selected else None,
            preselected_quantity=preselected or None,
            is_locked=bool(raw_item.get("isLocked")),
            is_sold_out=bool(raw_item.get("isSoldOut")),
            courses=courses,
        )

    def _build_market_items(self, raw_week: dict[str, Any]) -> list[HelloFreshMarketItem]:
        """Parse the week's Market add-on catalog from its ``addOns.groups`` block.

        Looks on the week payload directly or under ``_menu_payload`` (set when the authenticated
        menu catalog was merged into an account/deliveries week), mirroring the variation-title
        lookup so market items resolve regardless of which endpoint produced the week.
        """
        addons = HelloFreshPayloadNormalizer._menu_block(raw_week, "addOns", dict)
        if addons is None:
            return []

        items: list[HelloFreshMarketItem] = []
        # A market item's index is optional on the model, though one built here always has it.
        seen_indexes: set[int | None] = set()
        groups = addons.get("groups")
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict):
                continue
            group_type = group.get("groupType")
            group_sku = group.get("sku") if isinstance(group.get("sku"), str) else None
            raw_items = group.get("addOns")
            for raw_item in raw_items if isinstance(raw_items, list) else []:
                if not isinstance(raw_item, dict):
                    continue
                try:
                    item = self._market_item_from_raw(raw_item, group_type, group_sku)
                except _MALFORMED_PAYLOAD_ERRORS:
                    # One add-on of an unexpected shape is left out of the Market; the rest of
                    # the catalog, and the refresh, carry on.
                    continue
                if item is None or item.index in seen_indexes:
                    continue
                seen_indexes.add(item.index)
                items.append(item)
        return items

    @staticmethod
    def _attach_menu_block(
        weeks: Sequence[HelloFreshWeek],
        attribute: str,
        build: Callable[[dict[str, Any]], list[Any]],
    ) -> None:
        """Fill ``attribute`` on each week that lacks it with what ``build`` reads from its raw.

        The menu's blocks (Market catalog, sections, filter panel) are parsed in one pass over
        the assembled weeks, whichever endpoint produced each. A week that already has the
        attribute keeps it: a past week's purchased add-ons must not be replaced by a catalog.
        """
        for week in weeks:
            if getattr(week, attribute) or not isinstance(week.raw, dict):
                continue
            parsed = build(week.raw)
            if parsed:
                setattr(week, attribute, parsed)

    def _apply_market_items(self, weeks: Sequence[HelloFreshWeek]) -> None:
        """Attach each week's Market add-on catalog, parsed from its menu payload."""
        self._attach_menu_block(weeks, "market_items", self._build_market_items)
        # The catalog prices its items in bare minor units. Their currency is the one the
        # week's meals are priced in, or failing that the market's own.
        for week in weeks:
            priced = [
                item
                for item in week.market_items
                if item.price_cents is not None and item.currency is None
            ]
            if not priced:
                continue
            currency = next(
                (recipe.currency for recipe in week.recipes if recipe.currency), None
            ) or _COUNTRY_CURRENCIES.get(getattr(self, "_country", None) or "")
            for item in priced:
                item.currency = currency

    def _build_menu_categories(self, raw_week: dict[str, Any]) -> list[dict[str, Any]]:
        """Parse the menu payload's ``categories`` block into the week's menu sections.

        These are the sections the website's menu page shows (This Week's Menu, Health
        Conscious Menu, Family Menu, Bestsellers, Your Top Recipes, ...). Each output row is
        ``{name, slug, recipe_ids}``; ``recipe_ids`` merges the section's own ``items`` with
        every subcategory's, because a section like "Featured" lists ONLY subcategories and
        would otherwise come out empty. The Market pseudo-section (slug ``market``) is
        skipped — its members are add-ons, not meals, so it can never match the recipe grid.
        """
        block = HelloFreshPayloadNormalizer._menu_block(raw_week, "categories", dict)
        rows = block.get("categories") if block is not None else None

        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            name = clean_optional_str(row.get("name"))
            slug = clean_optional_str(row.get("slug"))
            if not name or not slug or slug == "market":
                continue
            item_lists: list[Any] = [row.get("items")]
            subcategories = row.get("subcategories")
            for sub in subcategories if isinstance(subcategories, list) else []:
                if isinstance(sub, dict):
                    item_lists.append(sub.get("items"))

            ids: list[str] = []
            seen: set[str] = set()
            for items in item_lists:
                for item in items if isinstance(items, list) else []:
                    item_id = item.get("id") if isinstance(item, dict) else None
                    if isinstance(item_id, str) and item_id and item_id not in seen:
                        seen.add(item_id)
                        ids.append(item_id)
            if ids:
                out.append({"name": name, "slug": slug, "recipe_ids": ids})
        return out

    def _apply_menu_categories(self, weeks: Sequence[HelloFreshWeek]) -> None:
        """Attach each week's menu sections, parsed from its menu payload."""
        self._attach_menu_block(weeks, "menu_categories", self._build_menu_categories)

    def _build_menu_filters(self, raw_week: dict[str, Any]) -> list[dict[str, Any]]:
        """Parse the menu payload's ``filters`` block into the website's filter groups.

        Each input row is ``{name, type, choice, options[]}`` with options ``{name, type,
        default}``; ``type`` is the slug the server-side ``/gw/my-deliveries/courses`` filter
        accepts (``cuisine``, ``dish-type``, ``exclude-allergens``, ...) and ``choice`` the
        combination rule (``MULTI-OR`` / ``MULTI-AND`` / ``SINGLE``). Output rows are
        ``{name, slug, choice, options: [{name, slug, default}]}``; groups without a usable
        slug or without any option are dropped.
        """
        block = HelloFreshPayloadNormalizer._menu_block(raw_week, "filters", list)

        out: list[dict[str, Any]] = []
        for row in block if isinstance(block, list) else []:
            if not isinstance(row, dict):
                continue
            name = clean_optional_str(row.get("name"))
            slug = clean_optional_str(row.get("type"))
            if not name or not slug:
                continue
            options: list[dict[str, Any]] = []
            raw_options = row.get("options")
            for option in raw_options if isinstance(raw_options, list) else []:
                if not isinstance(option, dict):
                    continue
                option_name = clean_optional_str(option.get("name"))
                option_slug = clean_optional_str(option.get("type"))
                if not option_name or not option_slug:
                    continue
                options.append(
                    {
                        "name": option_name,
                        "slug": option_slug,
                        "default": bool(option.get("default")),
                    }
                )
            if options:
                out.append(
                    {
                        "name": name,
                        "slug": slug,
                        "choice": clean_optional_str(row.get("choice")) or "MULTI-OR",
                        "options": options,
                    }
                )
        return out

    def _apply_menu_filters(self, weeks: Sequence[HelloFreshWeek]) -> None:
        """Attach each week's filter-panel definitions, parsed from its menu payload."""
        self._attach_menu_block(weeks, "menu_filters", self._build_menu_filters)

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
        today = local_today()
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

    # ``subStatus`` values of a box HelloFresh moved to another day; its new date is the week's
    # ``tracking.delivery_date`` (see the API reference, "Weeks").
    _MOVED_SUB_STATUSES = frozenset({"DELAYED", "EARLY"})

    @classmethod
    def _revised_delivery_date(cls, raw_week: dict[str, Any]) -> date | None:
        """Return the day a delayed or early box now comes, else None.

        The new date is a carrier timestamp, so its day is read in Home Assistant's time zone,
        as "Last delivery day" reads the arrival; a bare date is taken as it is.
        """
        sub_status = str(raw_week.get("subStatus") or "").strip().upper()
        if sub_status not in cls._MOVED_SUB_STATUSES:
            return None
        tracking = raw_week.get("tracking")
        if not isinstance(tracking, dict):
            return None
        value = tracking.get("delivery_date") or tracking.get("estimated_delivery_time")
        if isinstance(value, str) and "T" not in value:
            return parse_date(value)
        moved = parse_datetime(value)
        return local_date(moved) if moved is not None else None

    @staticmethod
    def _week_servings(raw_week: dict[str, Any]) -> int | None:
        """Return how many people a week's own box is for, else None.

        Its ``product.specs.size``, as the menu request reads it, then the people segment of
        the box's handle. A week without a box of its own (delivered history) has none.
        """
        product = raw_week.get("product")
        if not isinstance(product, dict):
            return None
        size = coerce_int(dict_path(product, "specs", "size"))
        if size:
            return size
        for key in ("handle", "sku"):
            parts = box_handle_parts(product.get(key))
            if parts is not None:
                return parts[2]
        return None

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

    def _stated_amount(self, raw_week: dict[str, Any], *keys: str) -> float | None:
        """Return the first amount a payload states under one of ``keys``, at any depth.

        For the totals and subtotals only. The API reference lists none of them on a week (it
        gives the box's price parts under ``product``), so there is no path to read them from;
        their names are specific enough that a match anywhere is that amount. Field names that
        many objects share (``price``, ``total``, ``amount``) are never searched for this way.
        """
        return coerce_float(self._find_first_nested_value(raw_week, keys))

    def _extract_total_price(self, raw_week: dict[str, Any]) -> float | None:
        """Return the best available total price, including shipping when split out."""
        direct_total = coerce_float(
            raw_week.get("grandTotal")
            or raw_week.get("totalPrice")
            or raw_week.get("total")
            or raw_week.get("amount")
        )
        if direct_total is None:
            direct_total = self._stated_amount(raw_week, "grandTotal", "totalPrice")
        if direct_total is not None:
            return direct_total

        direct_total_cents = self._stated_amount(
            raw_week, "grandTotalInCents", "totalPriceInCents", "totalInCents"
        )
        if direct_total_cents is not None:
            return direct_total_cents / 100

        subtotal = self._stated_amount(raw_week, "subTotal", "subtotal")
        shipping = coerce_float(raw_week.get("shippingAmount") or raw_week.get("shipping"))
        if shipping is None:
            shipping = self._stated_amount(raw_week, "shippingAmount")
        if subtotal is not None and shipping is not None:
            return subtotal + shipping

        subtotal_cents = self._stated_amount(raw_week, "subTotalInCents", "subtotalInCents")
        shipping_cents = self._stated_amount(raw_week, "shippingAmountInCents", "shippingInCents")
        if subtotal_cents is not None and shipping_cents is not None:
            return (subtotal_cents + shipping_cents) / 100

        # What the API reference documents on a week: its box under `product`, priced in minor
        # units, with the fees beside the price.
        product_price_cents = coerce_float(
            dict_path(raw_week, "product", "price") or dict_path(raw_week, "product", "unitPrice")
        )
        if product_price_cents is not None:
            return product_price_cents / 100 + self._extract_delivery_fee(raw_week)

        single_cents = coerce_float(
            raw_week.get("priceInCents") or dict_path(raw_week, "deliveryOption", "priceInCents")
        )
        if single_cents is not None:
            return single_cents / 100

        return coerce_float(raw_week.get("price"))

    @staticmethod
    def _extract_delivery_fee(raw_week: dict[str, Any]) -> float:
        """Return the fees on a week's box, in currency units.

        ``product.specialFee`` and ``product.shippingPrice`` are both charged on top of the
        box's price and both in minor units. Each is read from its own field and they are
        added: a zero in one is a real zero, not a reason to look for a fee somewhere else.
        """
        fees = (
            coerce_float(dict_path(raw_week, "product", "specialFee")),
            coerce_float(dict_path(raw_week, "product", "shippingPrice")),
        )
        return sum(fee for fee in fees if fee is not None) / 100

    def _extract_currency_code(self, raw_week: dict[str, Any]) -> str | None:
        """Return the best available currency code for an order."""
        currency = (
            raw_week.get("currency")
            or raw_week.get("currencyCode")
            # The API reference gives a week no currency field, so there is no path to read
            # one from; a `currency` or `currencyCode` anywhere in it is the week's.
            or self._find_first_nested_value(raw_week, ("currency", "currencyCode"))
        )
        if isinstance(currency, str) and currency.strip():
            return currency.strip().upper()
        return _COUNTRY_CURRENCIES.get(getattr(self, "_country", None) or "")

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
        today = local_today()
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
        # `customer` and the plan are objects in every capture. A null or a bare string in
        # their place must cost those fields, not the sign-in and every poll.
        customer = raw_subscription.get("customer")
        if not isinstance(customer, dict):
            customer = {}
        loyalty = customer.get("loyalty")
        if not isinstance(loyalty, dict):
            loyalty = {}
        plan = raw_subscription.get("plan")
        if not isinstance(plan, dict) or not plan:
            # The API reference documents no plan object on a subscription (its size is under
            # `productType`), so there is no path to read one from; these key names say what
            # they are wherever an older or regional payload nests one.
            plan = self._find_first_nested_dict(
                raw_subscription,
                {"plan", "activePlan", "subscriptionPlan"},
            )
        # What the API reference documents: `productType` is {productName, specs: {meals,
        # size}}, the plan's name, meals per week and number of people. They are read from
        # there, not from the first `name`, `meals` or `size` anywhere in the subscription,
        # which is how the delivery address's region once became the plan's name.
        product_type = raw_subscription.get("productType")
        if not isinstance(product_type, dict):
            product_type = {}
        meals_required = coerce_int(
            plan.get("numberOfRecipes")
            or plan.get("recipesPerWeek")
            or dict_path(product_type, "specs", "meals")
            # No documented home besides `productType.specs`; these names say what they are
            # wherever an older or regional payload puts them.
            or self._find_first_nested_value(
                raw_subscription, ("numberOfRecipes", "recipesPerWeek")
            )
            or raw_subscription.get("mealsPerWeek")
        )
        servings = coerce_int(
            plan.get("numberOfPersons")
            or plan.get("servings")
            or dict_path(product_type, "specs", "size")
            # As above: no documented home besides `productType.specs`.
            or self._find_first_nested_value(raw_subscription, ("numberOfPersons", "servings"))
        )
        plan_name = (
            plan.get("name") or plan.get("displayName") or product_type.get("productName") or None
        )
        display_name = (
            raw_subscription.get("name") or raw_subscription.get("displayName") or plan_name
        )

        return HelloFreshSubscription(
            subscription_id=str(raw_subscription.get("id")),
            account_id=customer.get("id"),
            locale=customer.get("locale"),
            status=self._derive_subscription_status(raw_subscription),
            display_name=display_name,
            plan_name=plan_name,
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
            # The API reference lists no loyalty counters on a subscription, so there is no
            # path to read them from: `customer.loyalty` first, then these names wherever they
            # sit. They are specific enough that a match is the counter.
            loyalty_boxes_received=coerce_int(
                raw_subscription.get("loyaltyBoxesReceived")
                or raw_subscription.get("totalBoxesReceived")
                or loyalty.get("value")
                or loyalty.get("boxesReceived")
                or self._find_first_nested_value(
                    raw_subscription,
                    ("loyaltyBoxesReceived", "totalBoxesReceived", "boxesReceived"),
                )
            ),
            loyalty_boxes_until_next_freebie=coerce_int(
                raw_subscription.get("loyaltyBoxesUntilNextFreebie")
                or raw_subscription.get("boxesUntilNextFreebie")
                or loyalty.get("boxesUntilNextFreebie")
                or loyalty.get("loyaltyBoxesUntilNextFreebie")
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

        Only the subscription's own field counts. A ``state`` or ``status`` nested further
        down belongs to something else (a US address has a ``state``), so it is not looked for.
        """
        explicit = (
            raw_subscription.get("status")
            or raw_subscription.get("subscriptionStatus")
            or raw_subscription.get("state")
        )
        if isinstance(explicit, list):
            # Some regions return a status history instead of a single status. The final
            # nonempty entry is the one to show, rather than the Python repr of the list.
            explicit = next(
                (
                    status.strip()
                    for status in reversed(explicit)
                    if isinstance(status, str) and status.strip()
                ),
                None,
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
            existing_week.servings = existing_week.servings or fallback_week.servings
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

        next_option = raw_subscription.get("nextDeliveryOption")
        plan_option = raw_subscription.get("deliveryOption")
        delivery_option: dict[str, Any] = (
            next_option
            if isinstance(next_option, dict)
            else plan_option
            if isinstance(plan_option, dict)
            else {}
        )
        # The plan's name and size from where the API reference puts them (`productType`),
        # then the box's own name.
        product_name = (
            dict_path(raw_subscription, "productType", "productName")
            or dict_path(raw_subscription, "productType", "name")
            or dict_path(raw_subscription, "product", "displayName")
            or dict_path(raw_subscription, "product", "name")
            or subscription.display_name
            or str(week_id)
        )
        meals_required = coerce_int(
            dict_path(raw_subscription, "productType", "specs", "meals")
            or dict_path(raw_subscription, "productType", "numberOfRecipes")
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
            # Built from the plan, so the week's box is the plan's.
            servings=subscription.servings,
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
        for variant in menu_weeks:
            key = (variant.subscription_id, variant.week_id)
            existing = menu_by_key.get(key)
            if existing is None or len(variant.recipes) > len(existing.recipes):
                menu_by_key[key] = variant
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
            # Project the account week's selection onto the full menu catalog: each chosen
            # recipe id with the portions the account week has it at.
            account_selected = {
                recipe.recipe_id: recipe.selected_quantity
                for recipe in account_week.recipes
                if recipe.is_selected
            }
            # When nothing is explicitly flagged, fall back to "presence == selection" ONLY if
            # the account week is a selection-sized list (the deliveries endpoint lists just the
            # chosen recipes). If it instead holds a full browsable catalog — i.e. about as many
            # recipes as the menu week — that fallback would mark the whole catalog selected,
            # which is what fabricated the wrong selection on past weeks. In that case derive
            # nothing here and let the menu week's own flags / past-deliveries decide.
            if not account_selected and len(account_week.recipes) < len(menu_week.recipes):
                account_selected = {
                    recipe.recipe_id: recipe.selected_quantity for recipe in account_week.recipes
                }
            if account_selected:
                for recipe in menu_week.recipes:
                    if recipe.recipe_id not in account_selected:
                        recipe.is_selected = False
                        recipe.selected_quantity = None
                        continue
                    # The quantity moves with the flag. A selected meal has at least one
                    # portion; where both payloads count them, the larger count stands, since
                    # a side that gives no count reads as 1.
                    menu_quantity = recipe.selected_quantity if recipe.is_selected else None
                    recipe.is_selected = True
                    recipe.selected_quantity = max(
                        account_selected[recipe.recipe_id] or 1, menu_quantity or 1
                    )

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
            # The menu week's raw is already only the blocks read later (see
            # _menu_payload_essentials), not the whole menu.
            account_week.raw = {
                **account_week.raw,
                "_menu_payload": menu_week.raw,
            }
            merged_weeks.append(account_week)

        return merged_weeks

    # The blocks of a week's menu payload that are read after it is normalized: the dish
    # options (variation titles), the Market catalog, the menu sections and the filter panel,
    # by the per-week passes and the get_weeks debug view. The meals themselves (hundreds per
    # week, each with its whole recipe) are already in the week's recipes; only the ones in
    # the box are kept, with the fields cart pricing builds the priced selection from.
    _MENU_BLOCKS_KEPT = frozenset({"week", "id", "modularity", "addOns", "categories", "filters"})
    _MEAL_FIELDS_KEPT = ("index", "selection", "charge", "handle", "sku", "boxSku")
    _MEAL_LIST_KEYS = frozenset(
        {"recipes", "meals", "menuItems", "items", "dishes", "entries", "courses"}
    )

    def _menu_payload_essentials(self, raw_week: dict[str, Any]) -> dict[str, Any]:
        """Return the parts of a week's menu payload that are read after normalization.

        A week's menu runs to about 1 MB of JSON, several times that as Python objects, and
        each week kept the whole of it for the life of the data (on the menu week and, merged,
        on the account week). Keeps the keys' order, so code walking the result meets the
        blocks in the order it met them in the payload.
        """
        selected = [
            {key: meal[key] for key in self._MEAL_FIELDS_KEPT if key in meal}
            for meal in self._extract_menu_week_recipe_candidates(raw_week)
            if isinstance(meal, dict)
            and isinstance(meal.get("selection"), dict)
            and meal["selection"].get("quantity") not in (None, 0, "0")
        ]
        kept: dict[str, Any] = {}
        for key, value in raw_week.items():
            if key in self._MENU_BLOCKS_KEPT:
                kept[key] = value
            elif key in self._MEAL_LIST_KEYS and selected and "meals" not in kept:
                kept["meals"] = selected
        if selected and "meals" not in kept:
            kept["meals"] = selected
        return kept

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

        # Matched by (week, subscription): with two subscriptions the same ISO week maps to
        # two different delivered menus (see WeekLookup).
        delivered_history = WeekLookup(week for week in past_delivery_weeks if week.recipes)

        # Delivery dates are local-market calendar dates — use LOCAL today, matching
        # models.py, so week classification can't flip near midnight UTC.
        today = local_today()
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

            past_week = delivered_history.find(account_week.week_id, account_week.subscription_id)
            if past_week is None:
                continue

            # This week actually shipped (it has delivered-history data), so whatever was sent IS
            # the real selection — not HelloFresh's auto-pick. The menu's ``mealsPreselected``
            # flag for a long-past week is stale/default and must not stick, or the card would
            # wrongly badge a week you personally chose as "Preselected".
            account_week.meals_preselected = False

            delivered = self._delivered_meals_only(account_week, past_week)
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
            else:
                # An OLDER past week shows exactly the meals that were DELIVERED. We deliberately
                # do NOT keep whatever the planning-menu endpoint attached as a "browsable
                # catalog": for an old week HelloFresh has no real menu, so it returns a bloated
                # multi-week AGGREGATE (~1000+ dishes spanning many weeks), and there is no
                # reliable structural signal to tell that apart from a genuine per-week menu
                # (only fragile size heuristics). Rather than risk flooding a past week with
                # meals that were never available that week, the delivered set (from
                # past-deliveries, with images) is authoritative and replaces it.
                self._mark_delivered(account_week.recipes, delivered)
                account_week.recipes = delivered
                # The browsable catalog was replaced by the delivered set, so the (possibly
                # multi-MB aggregate) menu payload stashed on this week is now dead weight kept
                # alive for the whole poll interval. Drop it — nothing reads it back for an old
                # week (writes/pricing only touch editable current/future weeks). It goes only
                # after the Market has taken what it alone knows: how many of each add-on.
                if past_week.market_items:
                    account_week.market_items = self._purchased_market_items(
                        account_week, past_week
                    )
                account_week.raw.pop("_menu_payload", None)

            account_week.menu_title = account_week.menu_title or past_week.menu_title
            # Counts follow what actually shipped, overriding any stale menu/plan values, and
            # they count portions: a doubled meal fills two of the box's meals. A box held at
            # least what it delivered, which history (one line per dish) can undercount.
            portions = self._selected_portions(account_week.recipes)
            account_week.meals_selected = portions
            required = past_week.meals_required or account_week.meals_required
            account_week.meals_required = max(required, portions) if required else required

        return list(account_weeks)

    @staticmethod
    def _catalog_matcher(
        recipes: Sequence[HelloFreshRecipe],
    ) -> Callable[[HelloFreshRecipe], HelloFreshRecipe | None]:
        """Return a function that finds a delivered meal's entry among ``recipes``.

        By recipe id, falling back to a case-insensitive name match: past-deliveries ids don't
        always line up with menu ids.
        """
        by_id = {recipe.recipe_id: recipe for recipe in recipes}
        by_name: dict[str, HelloFreshRecipe] = {}
        for recipe in recipes:
            if recipe.name:
                by_name.setdefault(recipe.name.strip().casefold(), recipe)

        def find(delivered: HelloFreshRecipe) -> HelloFreshRecipe | None:
            match = by_id.get(delivered.recipe_id)
            if match is None and delivered.name:
                match = by_name.get(delivered.name.strip().casefold())
            return match

        return find

    @staticmethod
    def _ordered_portions(recipe: HelloFreshRecipe | None) -> int:
        """Return how many portions a catalog entry was ordered at (0 when it wasn't)."""
        if recipe is None or not recipe.is_selected:
            return 0
        return recipe.selected_quantity or 1

    @classmethod
    def _mark_delivered(
        cls,
        catalog: Sequence[HelloFreshRecipe],
        delivered: Sequence[HelloFreshRecipe],
    ) -> None:
        """Mark the delivered meals selected, at the portions the week's catalog had them.

        History lists each delivered dish once, whatever its quantity. Where the week's menu
        is still at hand it says how many portions were ordered, and the larger count stands:
        otherwise a double portion became a single one the day after it arrived, and the meal
        plan refused its second night.
        """
        find = cls._catalog_matcher(catalog)
        for recipe in delivered:
            ordered = cls._ordered_portions(find(recipe))
            recipe.is_selected = True
            recipe.selected_quantity = max(recipe.selected_quantity or 1, ordered)

    @classmethod
    def _overlay_delivered_selection(
        cls,
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

        A delivered meal keeps the portions the catalog had it at (see ``_mark_delivered``).
        """
        find = cls._catalog_matcher(account_week.recipes)
        ordered = {id(recipe): cls._ordered_portions(recipe) for recipe in account_week.recipes}
        for recipe in account_week.recipes:
            recipe.is_selected = False
            recipe.selected_quantity = None

        for delivered_recipe in delivered:
            match = find(delivered_recipe)
            if match is None:
                delivered_recipe.is_selected = True
                delivered_recipe.selected_quantity = delivered_recipe.selected_quantity or 1
                account_week.recipes.append(delivered_recipe)
                continue
            match.is_selected = True
            match.selected_quantity = max(
                delivered_recipe.selected_quantity or 1, ordered.get(id(match), 0)
            )

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
        # Same pairing as the recipe merge: two subscriptions sharing an ISO week can't
        # inherit each other's purchases (see WeekLookup).
        purchase_history = WeekLookup(week for week in past_delivery_weeks if week.market_items)

        today = local_today()
        for account_week in account_weeks:
            # Only PAST weeks. A current/future week's live catalog is what makes it editable,
            # and past-deliveries can list the current week once its cutoff passes -- replacing
            # that catalog would strip the customer's ability to change their order.
            if account_week.delivery_date is None or account_week.delivery_date >= today:
                continue

            past_week = purchase_history.find(account_week.week_id, account_week.subscription_id)
            if past_week is None:
                continue

            account_week.market_items = self._purchased_market_items(account_week, past_week)

    def _known_market_items(self, account_week: HelloFreshWeek) -> list[HelloFreshMarketItem]:
        """Return every Market item a week is known to have: attached ones and its catalog's."""
        items = list(account_week.market_items)
        if isinstance(account_week.raw, dict):
            items.extend(self._build_market_items(account_week.raw))
        return items

    def _purchased_market_items(
        self,
        account_week: HelloFreshWeek,
        past_week: HelloFreshWeek,
    ) -> list[HelloFreshMarketItem]:
        """Return the add-ons a past week shipped with, at the quantities it ordered them.

        History lists each add-on once, without a count. While the week's catalog survives it
        says how many were ordered, so that count is carried over by item id before the catalog
        is replaced; otherwise two of an add-on became one the day after delivery.
        """
        ordered: dict[str, int] = {}
        for item in self._known_market_items(account_week):
            if not item.is_selected or not item.selected_quantity:
                continue
            for key in (item.item_id, item.recipe_id):
                if key:
                    ordered[key] = max(ordered.get(key, 0), item.selected_quantity)

        purchased: list[HelloFreshMarketItem] = []
        for item in past_week.market_items:
            quantity = max(ordered.get(item.item_id, 0), ordered.get(item.recipe_id or "", 0))
            if quantity > (item.selected_quantity or 1):
                # A copy: the history week keeps what history said.
                item = replace(item, selected_quantity=quantity)
            purchased.append(item)
        return purchased

    @staticmethod
    def _without_market_items(
        recipes: Iterable[HelloFreshRecipe],
        market_items: Iterable[HelloFreshMarketItem],
    ) -> list[HelloFreshRecipe]:
        """Return ``recipes`` without those that are one of ``market_items``, by id or name."""
        market_ids: set[str] = set()
        market_names: set[str] = set()
        for item in market_items:
            if item.item_id:
                market_ids.add(item.item_id)
            if item.recipe_id:
                market_ids.add(item.recipe_id)
            if item.name:
                market_names.add(item.name.strip().casefold())
        return [
            recipe
            for recipe in recipes
            if recipe.recipe_id not in market_ids
            and (recipe.name.strip().casefold() if recipe.name else "") not in market_names
        ]

    def _delivered_meals_only(
        self,
        account_week: HelloFreshWeek,
        past_week: HelloFreshWeek,
    ) -> list[HelloFreshRecipe]:
        """Return the delivered MEALS, with market add-ons filtered out.

        The delivered record includes ordered market add-ons (appetizers/sides/desserts)
        alongside the box meals, but those belong to the Market view, not the meal list. A
        delivered item that is a known market item for the week is dropped so it never shows in
        My Menu; the market merge marks those selected separately.

        Known items are, first, the history week's OWN purchased add-ons: past the menu-grace
        window no catalog survives, and the account week only gets its purchased list in a
        later pass, so the history record is the one source that is always there. The account
        week's catalog and attached items are added for an add-on history left out of its list.
        """
        return self._without_market_items(
            past_week.recipes,
            (*past_week.market_items, *self._known_market_items(account_week)),
        )

    def _normalize_menu_weeks(
        self,
        raw_weeks: list[dict[str, Any]],
        subscription: HelloFreshSubscription,
    ) -> list[HelloFreshWeek]:
        """Normalize menu-style payloads into public menu week models."""
        weeks: list[HelloFreshWeek] = []
        for index, raw_week in enumerate(raw_weeks):
            try:
                week = self._week_from_raw_menu(index, raw_week, subscription)
            except _MALFORMED_PAYLOAD_ERRORS as err:
                # As for a deliveries week: a menu of an unexpected shape costs that week's
                # menu, not the refresh.
                self._skip_malformed("HelloFresh menu week entry", index, err)
                continue
            if week is not None:
                weeks.append(week)
        return weeks

    def _week_from_raw_menu(
        self,
        index: int,
        raw_week: dict[str, Any],
        subscription: HelloFreshSubscription,
    ) -> HelloFreshWeek | None:
        """Build one menu week; None when the payload lists no meals."""
        if not isinstance(raw_week, dict):
            return None
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
        raw_recipes = self._extract_menu_week_recipe_candidates(raw_week)
        recipes = self._recipes_from_raw_meals(
            raw_recipes,
            where=f"menu week {week_id}",
            default_selected=False,
            variation_titles=self._build_variation_titles(raw_week),
        )
        if not recipes:
            return None

        display_name = (
            raw_week.get("label")
            or raw_week.get("title")
            or raw_week.get("displayName")
            or f"Menu {index + 1}"
        )
        return HelloFreshWeek(
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
            meals_required=self._week_box_meal_count(raw_week) or subscription.meals_required,
            # The menu marks each chosen meal itself (`selection.quantity`), so the count is
            # the portions selected; a count the payload states outright still wins.
            meals_selected=self._meals_selected_from_raw(raw_week, recipes, search_nested=False),
            meals_preselected=bool(raw_week.get("mealsPreselected")),
            recipes=recipes,
            source="account_menu_api",
            menu_title=raw_week.get("title") or raw_week.get("displayName"),
            raw=self._menu_payload_essentials(raw_week),
        )

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
        requested_subscription: HelloFreshSubscription | None = None,
    ) -> list[HelloFreshWeek]:
        """Normalize delivered history payloads into stable week models.

        ``requested_subscription`` is the subscription the payload was asked for
        (``/gw/my-deliveries/past-deliveries?subscription=...``). That endpoint's rows carry no
        subscription id of their own, so they are stamped with the one they were requested
        for; stamping the account's first subscription instead gave a second subscription's
        history to the first. Without it (an account-wide endpoint) a row that names no
        subscription still falls back to the first.
        """
        raw_weeks = self._extract_past_delivery_candidates(payload)
        if not raw_weeks:
            return []

        subscriptions_by_id = {
            subscription.subscription_id: subscription for subscription in subscriptions
        }
        default_subscription = requested_subscription or (
            subscriptions[0] if subscriptions else None
        )
        weeks: list[HelloFreshWeek] = []

        for index, raw_week in enumerate(raw_weeks):
            try:
                week = self._week_from_raw_past_delivery(
                    index, raw_week, subscriptions_by_id, default_subscription
                )
            except _MALFORMED_PAYLOAD_ERRORS as err:
                # As for a deliveries week: one delivered week of an unexpected shape is left
                # out of the history, and the rest of the refresh carries on.
                self._skip_malformed("HelloFresh past delivery entry", index, err)
                continue
            if week is not None:
                weeks.append(week)

        return weeks

    def _week_from_raw_past_delivery(
        self,
        index: int,
        raw_week: dict[str, Any],
        subscriptions_by_id: dict[str, HelloFreshSubscription],
        default_subscription: HelloFreshSubscription | None,
    ) -> HelloFreshWeek | None:
        """Build one delivered week; None when it has no week, date or subscription to key on."""
        week_id = str(raw_week.get("week") or raw_week.get("id") or f"past-week-{index}")
        delivery_date = parse_date(
            raw_week.get("delivery_date") or raw_week.get("deliveryDate") or raw_week.get("date")
        )
        # The /gw/my-deliveries/past-deliveries payload identifies each delivered week by its
        # ISO ``week`` id ONLY (no explicit date), so stamp the week with that ISO week's
        # Monday as a sortable PLACEHOLDER rather than leaving it date-less. It is not a
        # delivery date: ``HelloFreshAccountData.finalize`` replaces it with the matching
        # account week's real scheduled ``deliveryDate``, and "Last delivery day" never reads
        # it — that sensor follows the carrier's ``tracking.delivery_date`` (issue #6).
        if delivery_date is None:
            delivery_date = date_from_iso_week(week_id)
        if week_id.startswith("past-week-") and delivery_date is None:
            return None

        # The row's own subscription id wins; only without one does the default stand in.
        subscription_id = str(
            raw_week.get("subscription_id")
            or raw_week.get("subscriptionId")
            or (default_subscription.subscription_id if default_subscription is not None else "")
        )
        if not subscription_id:
            return None

        subscription = subscriptions_by_id.get(subscription_id, default_subscription)
        # The Market add-ons this week actually shipped with. Carried on the history week so
        # _merge_past_delivery_market_items can stamp them onto the matching account week.
        purchased_market_items = self._build_purchased_market_items(raw_week)
        # History can list a bought add-on among the meals as well. The week's own `addons`
        # say which those are, so they are taken out here: the meal list and the counts below
        # are then right for every reader, in whatever order the later merges run.
        recipes = self._without_market_items(
            self._recipes_from_raw_meals(
                self._extract_past_delivery_recipes(raw_week),
                where=f"past delivery {week_id}",
            ),
            purchased_market_items,
        )
        portions = self._selected_portions(recipes)
        display_name = raw_week.get("label") or raw_week.get("title") or week_id
        return HelloFreshWeek(
            week_id=week_id,
            display_name=str(display_name),
            subscription_id=subscription_id,
            delivery_date=delivery_date,
            status=raw_week.get("status") or "delivered",
            # For a HISTORICAL week, the number of meals actually delivered is the truth for
            # that week — prefer it over the CURRENT subscription's plan, which can differ
            # (e.g. a 4-meal box that week vs a 3-meal plan today) and would otherwise cap a
            # past week at the wrong count.
            meals_required=coerce_int(raw_week.get("recipe_count"))
            or max(len(recipes), portions)
            or (subscription.meals_required if subscription is not None else None)
            or None,
            # Portions, as everywhere: a meal at quantity 2 counts as two.
            meals_selected=portions or len(recipes) or None,
            recipes=recipes,
            market_items=purchased_market_items,
            source="past_deliveries",
            # A delivered week is read from its documented fields only. Searching it for the
            # first `type` or `size` at any depth returned a meal tag's type as the shipping
            # method: its meals carry tags shaped {name, type}.
            slot_label=raw_week.get("timeSlot")
            or raw_week.get("slotLabel")
            or dict_path(raw_week, "deliveryOption", "deliveryName"),
            shipping_method=raw_week.get("shippingMethod")
            or dict_path(raw_week, "deliveryOption", "type"),
            box_size=raw_week.get("boxSize"),
            **self._common_week_fields(raw_week),
        )

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
                    image_url=_usable_image_url(
                        (
                            raw_item.get("image"),
                            raw_item.get("imageUrl"),
                            raw_item.get("imagePath"),
                        ),
                        RECIPE_IMAGE_BASE,
                    ),
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
