"""Pure data models for the HelloFresh integration.

No HTTP, no aiohttp, no BeautifulSoup — just dataclasses and exceptions.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

# Smallest valid box HelloFresh sells: a week with fewer distinct meals than this has no valid
# box, so it genuinely still needs a selection. Mirrors the client's MIN_MEALS_PER_WEEK; kept as
# a plain module constant here so models.py stays dependency-free (no import from client).
MIN_MEALS_PER_WEEK = 2


class HelloFreshError(Exception):
    """Base error for HelloFresh integration."""


class HelloFreshAuthError(HelloFreshError):
    """Raised when authentication fails."""


class HelloFreshNotImplementedError(HelloFreshError):
    """Raised when the underlying HelloFresh API call is not wired yet."""


@dataclass(slots=True)
class HelloFreshRecipe:
    """Recipe information for a menu week."""

    recipe_id: str
    name: str
    preference: str | None = None
    is_selected: bool = False
    # How many servings of this meal are currently selected (HelloFresh's per-recipe quantity).
    # 0/None when not selected; typically 1, or 2+ for a doubled portion.
    selected_quantity: int | None = None
    # Course index within the week's menu (the cart's ``recipeIndexes`` unit). HelloFresh
    # identifies a selection by this index, not the recipe id, and the same dish can appear
    # under several ids/indexes (portion variants), so the index is the robust key for
    # building meal-selection writes and for a dashboard card to round-trip selections.
    course_index: int | None = None
    image_url: str | None = None
    description: str | None = None
    ingredients: list[str] = field(default_factory=list)
    allergens: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    nutrition: dict[str, str] = field(default_factory=dict)
    cook_time_minutes: int | None = None
    prep_time_minutes: int | None = None
    total_time_minutes: int | None = None
    calories_kcal: float | None = None
    protein_g: float | None = None
    difficulty: str | None = None
    # Per-serving surcharge for premium/variant meals (e.g. "+$7.99/serving"), from the menu
    # item's `charge` object, plus the raw cents amount for comparisons. These distinguish
    # otherwise same-named portion/premium variants that the catalog lists separately.
    surcharge_label: str | None = None
    surcharge_cents: int | None = None
    # Menu badge text (e.g. "Premium Picks"), from `recipe.label.text`.
    badge: str | None = None
    # Human-readable modifier that names how this variant differs from the base dish, from the
    # menu's `modularity` block (e.g. "2x Bacon", "Ground Turkey", "Added Broccoli"). This is the
    # clearest distinguisher for same-named variants whose price/nutrition look identical.
    variation_title: str | None = None
    # The `course_index` of the base dish this recipe is a variant of (the modularity group's
    # `defaultCourseIndex`). Every variant in a group — including the base itself and protein
    # swaps that carry a different NAME (Salmon vs. Cod) — shares this key, so the meal planner
    # card can group a dish's variants together even when their names differ. None when the meal
    # is not part of any variant group.
    variation_group: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """Serialize recipe data for Home Assistant state attributes."""
        return {
            "recipe_id": self.recipe_id,
            "name": self.name,
            "preference": self.preference,
            "is_selected": self.is_selected,
            "selected_quantity": self.selected_quantity,
            "course_index": self.course_index,
            "image_url": self.image_url,
            "description": self.description,
            "ingredients": self.ingredients,
            "allergens": self.allergens,
            "tags": self.tags,
            "nutrition": self.nutrition,
            "cook_time_minutes": self.cook_time_minutes,
            "prep_time_minutes": self.prep_time_minutes,
            "total_time_minutes": self.total_time_minutes,
            "calories_kcal": self.calories_kcal,
            "protein_g": self.protein_g,
            "difficulty": self.difficulty,
            "surcharge_label": self.surcharge_label,
            "surcharge_cents": self.surcharge_cents,
            "badge": self.badge,
            "variation_title": self.variation_title,
            "variation_group": self.variation_group,
        }


@dataclass(slots=True)
class HelloFreshMarketItem:
    """A HelloFresh Market add-on (appetizer, side, dessert, protein, etc.) for a week.

    Distinct from a meal recipe: market items are billed per-quantity extras, identified in the
    cart by their ``index``/``sku`` and ordered with a quantity up to ``max_quantity``.
    """

    item_id: str
    name: str
    # Cart selection unit for extras (mirrors a meal's course_index).
    index: int | None = None
    sku: str | None = None
    group_type: str | None = None  # appetizer / breakfast / dessert / protein / ...
    image_url: str | None = None
    description: str | None = None
    category: str | None = None
    tags: list[str] = field(default_factory=list)
    nutrition: dict[str, str] = field(default_factory=dict)
    calories_kcal: float | None = None
    # Base (single-unit) price in cents and a derived display amount in major units.
    price_cents: int | None = None
    price: float | None = None
    currency: str | None = None
    max_quantity: int | None = None
    is_selected: bool = False
    # Total selected servings (one-off + preselected). preselected_quantity is the recurring
    # portion (carried week to week); the cart write preserves it and applies changes as one-off.
    selected_quantity: int | None = None
    preselected_quantity: int | None = None
    is_locked: bool = False
    is_sold_out: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Serialize market-item data for the get_weeks response / dashboard card."""
        return {
            "item_id": self.item_id,
            "name": self.name,
            "index": self.index,
            "sku": self.sku,
            "group_type": self.group_type,
            "image_url": self.image_url,
            "description": self.description,
            "category": self.category,
            "tags": self.tags,
            "nutrition": self.nutrition,
            "calories_kcal": self.calories_kcal,
            "price_cents": self.price_cents,
            "price": self.price,
            "currency": self.currency,
            "max_quantity": self.max_quantity,
            "is_selected": self.is_selected,
            "selected_quantity": self.selected_quantity,
            "preselected_quantity": self.preselected_quantity,
            "is_locked": self.is_locked,
            "is_sold_out": self.is_sold_out,
        }


@dataclass(slots=True)
class HelloFreshWeek:
    """Customer week/menu selection state."""

    week_id: str
    display_name: str
    subscription_id: str | None = None
    delivery_date: date | None = None
    selection_deadline: datetime | None = None
    status: str | None = None
    meals_required: int | None = None
    meals_selected: int | None = None
    is_skipped: bool = False
    # True when HelloFresh auto-selected this week's meals from the food profile (the menu's
    # week-level ``mealsPreselected`` flag) instead of the customer choosing them — the UI's
    # "We picked N meals we thought you'd like" state.
    meals_preselected: bool = False
    recipes: list[HelloFreshRecipe] = field(default_factory=list)
    market_items: list[HelloFreshMarketItem] = field(default_factory=list)
    source: str = "account"
    menu_title: str | None = None
    slot_label: str | None = None
    shipping_method: str | None = None
    box_size: str | None = None
    sub_status: str | None = None
    delivery_state: str | None = None
    actionable: bool = False
    prepaid: bool = False
    delivery_blocked: bool = False
    holiday_delivery_date: date | None = None
    holiday_message: str | None = None
    holiday_shift_visible: bool = False
    allowed_actions: dict[str, bool] = field(default_factory=dict)
    available_one_off_options: list[dict[str, str | None]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_selection(self) -> bool:
        """Return True when the week still needs the customer's attention for meal choices.

        A week needs attention when either:
          * HelloFresh AUTO-PICKED the meals (``meals_preselected``) and the customer hasn't
            overridden them — the app's "we picked these, review them" state; or
          * the selection is below the smallest valid box (< ``MIN_MEALS_PER_WEEK``), e.g. an
            untouched week with 0 meals.

        Crucially it does NOT fire merely because ``meals_selected < meals_required``: a customer
        can deliberately RESIZE a week to fewer meals than their base plan (e.g. pick 2 on a
        3-meal plan). That is a complete, valid choice — flagging it would be a false "needs
        selection" (it wrongly kept ``binary_sensor.needs_meal_selection`` on).
        """
        if self.is_skipped:
            return False
        if self.meals_selected is None:
            return False
        if self.meals_selected < MIN_MEALS_PER_WEEK:
            return True
        return self.meals_preselected

    @property
    def auto_picked(self) -> bool:
        """Return True when HelloFresh auto-picked this week's meals (vs. the customer choosing).

        Driven by the menu's week-level ``mealsPreselected`` flag. A skipped week (no box ships)
        and a past week (delivery date before today) are never counted — only the next delivery
        week and later are considered, since a customer can no longer change an already-shipped box.
        """
        if self.is_skipped or not self.meals_preselected:
            return False
        return not (self.delivery_date is not None and self.delivery_date < date.today())

    @property
    def selection_progress(self) -> str | None:
        """Return human-friendly meal selection progress."""
        if self.meals_required is None or self.meals_selected is None:
            return None
        return f"{self.meals_selected}/{self.meals_required}"

    @property
    def market_items_selected(self) -> int:
        """Return the number of distinct HelloFresh Market add-ons selected for this week."""
        return sum(1 for item in self.market_items if item.is_selected)

    def as_summary_dict(self) -> dict[str, Any]:
        """Serialize week metadata WITHOUT the heavy per-recipe / action lists.

        A week's ``recipes`` catalog (now populated from the authenticated menu API) is by
        far the largest part of ``as_dict`` and can exceed Home Assistant's 16 KB per-state
        recorder attribute cap on its own. Sensors that attach a single week as context only
        need its scalar metadata (dates, deadline, counts, slot), so they use this lighter
        form. The full recipe list remains available on the ``next_selection_deadline``
        sensor's ``weeks`` attribute for the per-week dashboard table.
        """
        return {
            "week_id": self.week_id,
            "display_name": self.display_name,
            "subscription_id": self.subscription_id,
            "delivery_date": self.delivery_date.isoformat() if self.delivery_date else None,
            "selection_deadline": (
                self.selection_deadline.isoformat() if self.selection_deadline else None
            ),
            "status": self.status,
            "meals_required": self.meals_required,
            "meals_selected": self.meals_selected,
            "selection_progress": self.selection_progress,
            "is_skipped": self.is_skipped,
            "needs_selection": self.needs_selection,
            "meals_preselected": self.meals_preselected,
            "auto_picked": self.auto_picked,
            "source": self.source,
            "menu_title": self.menu_title,
            "slot_label": self.slot_label,
            "shipping_method": self.shipping_method,
            "box_size": self.box_size,
            "sub_status": self.sub_status,
            "delivery_state": self.delivery_state,
            "actionable": self.actionable,
            "prepaid": self.prepaid,
            "delivery_blocked": self.delivery_blocked,
            "holiday_delivery_date": (
                self.holiday_delivery_date.isoformat() if self.holiday_delivery_date else None
            ),
            "holiday_message": self.holiday_message,
            "holiday_shift_visible": self.holiday_shift_visible,
            # Small list of alternative delivery-date options (handle + date) for the week.
            # Kept in the summary because it is bounded and useful for "move my box" flows;
            # it is nowhere near the per-recipe payload that forced the recipe-free summary.
            "available_one_off_options": self.available_one_off_options,
        }

    def as_dict(self) -> dict[str, Any]:
        """Serialize full week data (including recipes) for state attributes."""
        return {
            **self.as_summary_dict(),
            "allowed_actions": self.allowed_actions,
            "recipes": [recipe.as_dict() for recipe in self.recipes],
            "market_items": [item.as_dict() for item in self.market_items],
        }


@dataclass(slots=True)
class HelloFreshOrder:
    """Customer order information."""

    order_id: str
    week_id: str
    status: str
    subscription_id: str | None = None
    delivery_date: date | None = None
    tracking_url: str | None = None
    tracking_number: str | None = None
    tracking_status: str | None = None
    carrier: str | None = None
    total_price: float | None = None
    currency: str | None = None
    slot_label: str | None = None
    # Authoritative total of all billing charges for this delivery (summed per
    # subscription+delivery date from the billing API), the same figure the
    # ``next_box_total_price`` sensor reports. Kept separate from ``total_price`` (which a
    # later cart/calculate estimate may overwrite) so the billed amount is preserved.
    billed_total_price: float | None = None
    billed_total_currency: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Serialize order data for Home Assistant state attributes."""
        return {
            "order_id": self.order_id,
            "week_id": self.week_id,
            "status": self.status,
            "subscription_id": self.subscription_id,
            "delivery_date": self.delivery_date.isoformat() if self.delivery_date else None,
            "tracking_url": self.tracking_url,
            "tracking_number": self.tracking_number,
            "tracking_status": self.tracking_status,
            "carrier": self.carrier,
            "total_price": self.total_price,
            "currency": self.currency,
            "billed_total_price": self.billed_total_price,
            "billed_total_currency": self.billed_total_currency,
            "slot_label": self.slot_label,
        }


@dataclass(slots=True)
class HelloFreshSubscription:
    """HelloFresh subscription/account plan metadata."""

    subscription_id: str
    account_id: str | None = None
    locale: str | None = None
    status: str | None = None
    display_name: str | None = None
    plan_name: str | None = None
    meals_required: int | None = None
    servings: int | None = None
    delivery_address: str | None = None
    box_size: str | None = None
    shipping_method: str | None = None
    delivery_weekday: int | None = None
    preset: str | None = None
    next_delivery: date | None = None
    next_delivery_week: str | None = None
    next_cutoff_date: datetime | None = None
    next_modifiable_delivery_date: date | None = None
    next_modifiable_delivery_week: str | None = None
    next_delivery_time: str | None = None
    payment_method: str | None = None
    payment_gateway: str | None = None
    recent_payment_date: date | None = None
    next_payment_date: date | None = None
    coupon_code: str | None = None
    loyalty_boxes_received: int | None = None
    loyalty_boxes_until_next_freebie: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Serialize subscription metadata for attributes and diagnostics."""
        return {
            "subscription_id": self.subscription_id,
            "account_id": self.account_id,
            "locale": self.locale,
            "status": self.status,
            "display_name": self.display_name,
            "plan_name": self.plan_name,
            "meals_required": self.meals_required,
            "servings": self.servings,
            "delivery_address": self.delivery_address,
            "box_size": self.box_size,
            "shipping_method": self.shipping_method,
            "delivery_weekday": self.delivery_weekday,
            "preset": self.preset,
            "next_delivery": self.next_delivery.isoformat() if self.next_delivery else None,
            "next_delivery_week": self.next_delivery_week,
            "next_cutoff_date": (
                self.next_cutoff_date.isoformat() if self.next_cutoff_date else None
            ),
            "next_modifiable_delivery_date": (
                self.next_modifiable_delivery_date.isoformat()
                if self.next_modifiable_delivery_date
                else None
            ),
            "next_modifiable_delivery_week": self.next_modifiable_delivery_week,
            "next_delivery_time": self.next_delivery_time,
            "payment_method": self.payment_method,
            "payment_gateway": self.payment_gateway,
            "recent_payment_date": (
                self.recent_payment_date.isoformat() if self.recent_payment_date else None
            ),
            "next_payment_date": (
                self.next_payment_date.isoformat() if self.next_payment_date else None
            ),
            "coupon_code": self.coupon_code,
            "loyalty_boxes_received": self.loyalty_boxes_received,
            "loyalty_boxes_until_next_freebie": self.loyalty_boxes_until_next_freebie,
        }


@dataclass(slots=True)
class HelloFreshCapabilities:
    """Runtime capabilities and fallbacks observed by the integration."""

    supports_meal_selection: bool = False
    supports_account_menu_api: bool = False
    supports_update_delivery_address: bool = False
    supports_update_delivery_weekday: bool = False
    supports_pause: bool = False
    supports_one_off_change: bool = False
    supports_update_payment_method: bool = False
    supports_donation: bool = False
    using_public_menu_fallback: bool = False
    payload_shape_changed: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def supports_write_actions(self) -> bool:
        """Return True when at least one write path is likely available."""
        return any(
            (
                self.supports_meal_selection,
                self.supports_update_delivery_address,
                self.supports_update_delivery_weekday,
                self.supports_pause,
                self.supports_one_off_change,
                self.supports_update_payment_method,
                self.supports_donation,
            )
        )

    def as_dict(self) -> dict[str, Any]:
        """Serialize capabilities for diagnostics and entity attributes."""
        return {
            "supports_write_actions": self.supports_write_actions,
            "supports_meal_selection": self.supports_meal_selection,
            "supports_account_menu_api": self.supports_account_menu_api,
            "supports_update_delivery_address": self.supports_update_delivery_address,
            "supports_update_delivery_weekday": self.supports_update_delivery_weekday,
            "supports_pause": self.supports_pause,
            "supports_one_off_change": self.supports_one_off_change,
            "supports_update_payment_method": self.supports_update_payment_method,
            "supports_donation": self.supports_donation,
            "using_public_menu_fallback": self.using_public_menu_fallback,
            "payload_shape_changed": self.payload_shape_changed,
            "notes": self.notes,
        }


# HelloFresh's food-profile "taste" section is split into two kinds of fields:
#   • list fields — a flat array of chosen slugs (e.g. exclusions, nutritions).
#   • weighted fields — a {slug: weight} map where weight is +100 (like) or -100 (dislike);
#     a slug absent from the map is "neutral". The web app only ever writes +100/-100.
# Keeping these two sets explicit lets the model normalize a PATCH payload without guessing.
FOOD_PROFILE_TASTE_LIST_FIELDS = (
    "exclusions",
    "nutritions",
    "mealTypes",
    "dietaryPreferences",
)
FOOD_PROFILE_TASTE_WEIGHTED_FIELDS = (
    "cuisines",
    "flavors",
    "dishTypes",
    "primaryProteins",
)
FOOD_PROFILE_LIKE = 100
FOOD_PROFILE_DISLIKE = -100


@dataclass(slots=True)
class HelloFreshFoodProfileOptions:
    """All selectable food-profile options (the API's ``/profile/options`` catalog).

    This is the universe of choices HelloFresh offers; the customer's actual picks live in
    :class:`HelloFreshFoodProfile`. Stored as the decoded JSON so new option groups appear
    in the dashboard automatically, with the ``_meta`` block (e.g. which fields support a
    "none" choice, the per-diet primary-protein groupings) preserved for the card.
    """

    taste: dict[str, list[Any]] = field(default_factory=dict)
    household: dict[str, list[Any]] = field(default_factory=dict)
    goals: dict[str, list[Any]] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "HelloFreshFoodProfileOptions":
        payload = payload or {}
        return cls(
            taste=dict(payload.get("taste") or {}),
            household=dict(payload.get("household") or {}),
            goals=dict(payload.get("goals") or {}),
            meta=dict(payload.get("_meta") or {}),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "taste": self.taste,
            "household": self.household,
            "goals": self.goals,
            "meta": self.meta,
        }


@dataclass(slots=True)
class HelloFreshFoodProfile:
    """A customer's food profile — the preferences HelloFresh uses to auto-pick future meals.

    Mirrors the ``/customers/me/profile`` shape (``taste`` / ``household`` / ``goals``). The
    raw decoded payload is kept verbatim so read-only extras the card doesn't model (e.g.
    ``plans``, ``legacySinglePreference``, ``ingredients``) survive a round-trip.
    """

    taste: dict[str, Any] = field(default_factory=dict)
    household: dict[str, Any] = field(default_factory=dict)
    goals: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "HelloFreshFoodProfile":
        payload = payload or {}
        return cls(
            taste=dict(payload.get("taste") or {}),
            household=dict(payload.get("household") or {}),
            goals=dict(payload.get("goals") or {}),
            raw=dict(payload),
        )

    def as_dict(self) -> dict[str, Any]:
        """Serialize for the get_food_profile response service (consumed by the card)."""
        return {
            "taste": self.taste,
            "household": self.household,
            "goals": self.goals,
        }

    @staticmethod
    def build_patch(changes: dict[str, Any]) -> dict[str, Any]:
        """Normalize a partial ``{taste, household, goals}`` update into a PATCH payload.

        Accepts the same nested shape the card edits and coerces weighted taste fields to the
        canonical ``{slug: +/-100}`` map form (a bare list of slugs is treated as all-liked,
        and any non-zero weight is snapped to +/-100). Only the sections present in ``changes``
        are included, so callers can PATCH just ``household`` without touching ``taste``.
        """
        patch: dict[str, Any] = {}

        taste = changes.get("taste")
        if isinstance(taste, dict):
            out_taste: dict[str, Any] = {}
            for key, value in taste.items():
                if key in FOOD_PROFILE_TASTE_WEIGHTED_FIELDS:
                    out_taste[key] = HelloFreshFoodProfile._coerce_weighted(value)
                else:
                    out_taste[key] = list(value) if isinstance(value, (list, tuple)) else value
            patch["taste"] = out_taste

        household = changes.get("household")
        if isinstance(household, dict):
            patch["household"] = {k: v for k, v in household.items()}

        goals = changes.get("goals")
        if isinstance(goals, dict):
            patch["goals"] = {k: (list(v) if isinstance(v, (list, tuple)) else v) for k, v in goals.items()}

        return patch

    @staticmethod
    def _coerce_weighted(value: Any) -> dict[str, int]:
        """Coerce a weighted taste field to ``{slug: +/-100}``.

        A list/tuple of slugs becomes all-liked; a mapping snaps each weight to LIKE/DISLIKE
        by its sign (0 / falsy is dropped, i.e. treated as neutral/unset).
        """
        if isinstance(value, (list, tuple)):
            return {str(slug): FOOD_PROFILE_LIKE for slug in value}
        if isinstance(value, dict):
            out: dict[str, int] = {}
            for slug, weight in value.items():
                try:
                    num = int(weight)
                except (TypeError, ValueError):
                    continue
                if not num:
                    continue
                out[str(slug)] = FOOD_PROFILE_LIKE if num > 0 else FOOD_PROFILE_DISLIKE
            return out
        return {}


def _tracked_order_sort_key(
    order: HelloFreshOrder,
) -> tuple[bool, bool, bool, bool, bool, date]:
    """Prefer orders with concrete shipment details over generic state-only records."""
    has_tracking_number = bool(order.tracking_number and order.tracking_number.strip())
    has_tracking_url = bool(order.tracking_url and order.tracking_url.strip())
    has_carrier = bool(order.carrier and order.carrier.strip())
    has_tracking_status = bool(order.tracking_status and order.tracking_status.strip())
    has_concrete_tracking = has_tracking_number or has_tracking_url
    return (
        has_concrete_tracking,
        has_tracking_number,
        has_tracking_url,
        has_carrier,
        has_tracking_status,
        order.delivery_date or date.min,
    )


@dataclass(slots=True)
class HelloFreshAccountData:
    """Top-level account data consumed by Home Assistant."""

    weeks: list[HelloFreshWeek] = field(default_factory=list)
    orders: list[HelloFreshOrder] = field(default_factory=list)
    past_delivery_weeks: list[HelloFreshWeek] = field(default_factory=list)
    public_menu_weeks: list[HelloFreshWeek] = field(default_factory=list)
    subscriptions: list[HelloFreshSubscription] = field(default_factory=list)
    available_menu_labels: list[str] = field(default_factory=list)
    account_id: str | None = None
    subscription_id: str | None = None
    locale: str | None = None
    boxes_received: int | None = None
    account_data_available: bool = False
    capabilities: HelloFreshCapabilities = field(default_factory=HelloFreshCapabilities)
    debug_trace: dict[str, Any] = field(default_factory=dict)
    next_delivery_total: float | None = None
    next_delivery_total_currency: str | None = None
    account_credit: float | None = None
    account_credit_currency: str | None = None
    selected_plan_total_price: float | None = None
    selected_plan_total_price_currency: str | None = None
    recent_order_id: str | None = None
    _serialized_orders: list[dict[str, Any]] | None = field(default=None)
    _serialized_weeks: list[dict[str, Any]] | None = field(default=None)
    _serialized_past_delivery_weeks: list[dict[str, Any]] | None = field(default=None)
    _serialized_weeks_needing_selection: list[dict[str, Any]] | None = field(default=None)
    _summarized_weeks_needing_selection: list[dict[str, Any]] | None = field(default=None)
    _serialized_public_menu_weeks: list[dict[str, Any]] | None = field(default=None)
    _serialized_subscriptions: list[dict[str, Any]] | None = field(default=None)
    _next_order: HelloFreshOrder | None = None
    _upcoming_orders: list[HelloFreshOrder] = field(default_factory=list)
    _tracked_order: HelloFreshOrder | None = None
    _weeks_needing_selection: list[HelloFreshWeek] = field(default_factory=list)
    _weeks_auto_picked: list[HelloFreshWeek] = field(default_factory=list)
    _skipped_weeks: list[HelloFreshWeek] = field(default_factory=list)
    _next_selection_week: HelloFreshWeek | None = None
    _next_configurable_week: HelloFreshWeek | None = None
    _next_skipped_week: HelloFreshWeek | None = None
    _delivery_count_this_week: int = 0
    _current_public_menu: HelloFreshWeek | None = None
    _last_delivery_week: HelloFreshWeek | None = None
    _weeks_by_id: dict[str, HelloFreshWeek] = field(default_factory=dict)

    @property
    def next_order(self) -> HelloFreshOrder | None:
        """Return the next order by delivery date."""
        return self._next_order

    @property
    def upcoming_orders(self) -> list[HelloFreshOrder]:
        """Return all upcoming orders sorted by delivery date."""
        return self._upcoming_orders

    @property
    def tracked_order(self) -> HelloFreshOrder | None:
        """Return the most relevant order with tracking information."""
        return self._tracked_order

    @property
    def weeks_needing_selection(self) -> list[HelloFreshWeek]:
        """Return weeks that still need meal selection."""
        return self._weeks_needing_selection

    @property
    def weeks_auto_picked(self) -> list[HelloFreshWeek]:
        """Return weeks where HelloFresh auto-picked the meals (menu ``mealsPreselected``)."""
        return self._weeks_auto_picked

    @property
    def summarized_weeks_auto_picked(self) -> list[dict[str, Any]]:
        """Recipe-free summaries of the auto-picked weeks (recorder-safe attribute payload)."""
        return [week.as_summary_dict() for week in self._weeks_auto_picked]

    @property
    def skipped_weeks(self) -> list[HelloFreshWeek]:
        """Return weeks that are currently skipped."""
        return self._skipped_weeks

    @property
    def next_selection_week(self) -> HelloFreshWeek | None:
        """Return the next week that still needs meal selection."""
        return self._next_selection_week

    @property
    def next_configurable_week(self) -> HelloFreshWeek | None:
        """Return the next non-skipped upcoming week with selection-related context."""
        return self._next_configurable_week

    @property
    def primary_subscription(self) -> HelloFreshSubscription | None:
        """Return the primary subscription when one is available."""
        return self.subscriptions[0] if self.subscriptions else None

    @property
    def next_skipped_week(self) -> HelloFreshWeek | None:
        """Return the earliest skipped week."""
        return self._next_skipped_week

    @property
    def next_delivery_week_obj(self) -> HelloFreshWeek | None:
        """Return the week for the subscription's next delivery.

        Anchored to the subscription's ``next_delivery_week`` handle (``nextDeliveryWeek``,
        a ``YYYY-Www`` ISO week). Used to read that week's cutoff for the next-delivery
        selection deadline, distinct from the later modifiable week.
        """
        subscription = self.primary_subscription
        if subscription is None or not subscription.next_delivery_week:
            return None
        return self.get_week(subscription.next_delivery_week)

    @property
    def next_modifiable_week(self) -> HelloFreshWeek | None:
        """Return the next delivery week that can still be modified.

        Anchored to the subscription's ``next_modifiable_delivery_week`` handle (a
        ``YYYY-Www`` ISO week) rather than the next undelivered week, so skip/restore
        actions target the soonest week the customer is actually allowed to change.
        """
        subscription = self.primary_subscription
        if subscription is None or not subscription.next_modifiable_delivery_week:
            return None
        return self.get_week(subscription.next_modifiable_delivery_week)

    @property
    def delivery_count_this_week(self) -> int:
        """Return the number of deliveries in the current ISO week."""
        return self._delivery_count_this_week

    @property
    def current_public_menu(self) -> HelloFreshWeek | None:
        """Return the currently visible public menu week."""
        return self._current_public_menu

    @property
    def last_delivery_week(self) -> HelloFreshWeek | None:
        """Return the latest delivered week discovered from account history."""
        return self._last_delivery_week

    @property
    def past_delivery_count(self) -> int:
        """Return the number of delivered weeks available from account history."""
        return len(self.past_delivery_weeks)

    @property
    def subscription_count(self) -> int:
        """Return the number of subscriptions on the account."""
        return len(self.subscriptions)

    @property
    def serialized_orders(self) -> list[dict[str, Any]]:
        """Return serialized orders, computed once per finalize cycle."""
        if self._serialized_orders is None:
            self._serialized_orders = [order.as_dict() for order in self.orders]
        return self._serialized_orders

    @property
    def serialized_weeks(self) -> list[dict[str, Any]]:
        """Return serialized weeks, computed once per finalize cycle."""
        if self._serialized_weeks is None:
            self._serialized_weeks = [week.as_dict() for week in self.weeks]
        return self._serialized_weeks

    @property
    def serialized_past_delivery_weeks(self) -> list[dict[str, Any]]:
        """Return serialized past delivery weeks, computed once per finalize cycle."""
        if self._serialized_past_delivery_weeks is None:
            self._serialized_past_delivery_weeks = [
                week.as_dict() for week in self.past_delivery_weeks
            ]
        return self._serialized_past_delivery_weeks

    @property
    def serialized_weeks_needing_selection(self) -> list[dict[str, Any]]:
        """Return serialized weeks needing selection, computed once per finalize cycle."""
        if self._serialized_weeks_needing_selection is None:
            self._serialized_weeks_needing_selection = [
                week.as_dict() for week in self._weeks_needing_selection
            ]
        return self._serialized_weeks_needing_selection

    @property
    def summarized_weeks_needing_selection(self) -> list[dict[str, Any]]:
        """Return recipe-free week summaries for recorder-bound sensor attributes.

        Same weeks as ``serialized_weeks_needing_selection`` but without the per-recipe
        catalog, so the list stays under Home Assistant's 16 KB per-state attribute cap.
        The full form remains available for diagnostics. Memoized per finalize cycle because
        ``extra_state_attributes`` (which reads this) is called frequently by Home Assistant.
        """
        if self._summarized_weeks_needing_selection is None:
            self._summarized_weeks_needing_selection = [
                week.as_summary_dict() for week in self._weeks_needing_selection
            ]
        return self._summarized_weeks_needing_selection

    @property
    def serialized_public_menu_weeks(self) -> list[dict[str, Any]]:
        """Return serialized public menu weeks, computed once per finalize cycle."""
        if self._serialized_public_menu_weeks is None:
            self._serialized_public_menu_weeks = [week.as_dict() for week in self.public_menu_weeks]
        return self._serialized_public_menu_weeks

    @property
    def serialized_subscriptions(self) -> list[dict[str, Any]]:
        """Return serialized subscriptions, computed once per finalize cycle."""
        if self._serialized_subscriptions is None:
            self._serialized_subscriptions = [s.as_dict() for s in self.subscriptions]
        return self._serialized_subscriptions

    def get_week(self, week_id: str) -> HelloFreshWeek | None:
        """Return a cached week by id."""
        return self._weeks_by_id.get(week_id)

    def get_order_for_week(self, week_id: str) -> HelloFreshOrder | None:
        """Return the best order for a week id.

        A week can have several order records (a generic state-only one plus a richer one with
        shipment tracking); prefer the one with concrete tracking/details via the same ranking
        used for the tracked-shipment sensor.
        """
        candidates = [order for order in self.orders if order.week_id == week_id]
        if not candidates:
            return None
        return max(candidates, key=_tracked_order_sort_key)

    def finalize(self) -> HelloFreshAccountData:
        """Populate serialized views used by entities and diagnostics."""
        self.orders.sort(key=lambda order: order.delivery_date or date.max)
        self.weeks.sort(
            key=lambda week: (
                week.delivery_date.isoformat() if week.delivery_date is not None else "9999-12-31",
                week.week_id,
            )
        )
        self.past_delivery_weeks.sort(
            key=lambda week: (
                week.delivery_date.isoformat() if week.delivery_date is not None else "0001-01-01",
                week.week_id,
            )
        )
        self._weeks_by_id = {week.week_id: week for week in self.weeks}
        # Only non-skipped deliveries today or later are "upcoming". The deliveries endpoint
        # returns a wide window (≈12 weeks back to 6 weeks ahead) including weeks the customer
        # skipped, where no box ships — those are excluded so the count and next_order reflect
        # real upcoming deliveries. Without the date filter, next_order would resolve to the
        # oldest historical delivery instead of the next one. Skip state is read from the
        # week (its is_skipped is computed robustly) rather than the order status string.
        today = date.today()
        self._upcoming_orders = [
            order
            for order in self.orders
            if order.delivery_date is not None
            and order.delivery_date >= today
            and not (
                (week := self._weeks_by_id.get(order.week_id)) is not None and week.is_skipped
            )
        ]
        self._next_order = self._upcoming_orders[0] if self._upcoming_orders else None
        tracked_orders = [
            order
            for order in self.orders
            if order.tracking_url or order.tracking_number or order.tracking_status
        ]
        self._tracked_order = max(
            tracked_orders,
            default=None,
            key=_tracked_order_sort_key,
        )
        self._weeks_needing_selection = [week for week in self.weeks if week.needs_selection]
        self._weeks_auto_picked = [week for week in self.weeks if week.auto_picked]
        self._skipped_weeks = [week for week in self.weeks if week.is_skipped]
        self._next_selection_week = min(
            self._weeks_needing_selection,
            default=None,
            key=lambda week: (
                week.selection_deadline.isoformat()
                if week.selection_deadline is not None
                else "9999-12-31T23:59:59",
                week.delivery_date.isoformat() if week.delivery_date is not None else "9999-12-31",
                week.week_id,
            ),
        )
        if self._next_selection_week is not None:
            self._next_configurable_week = self._next_selection_week
        else:
            _today = today
            _candidates: list[HelloFreshWeek] = []
            _first_future: HelloFreshWeek | None = None
            for _week in self.weeks:
                if _week.is_skipped:
                    continue
                if not (
                    _week.meals_selected is not None
                    or _week.meals_required is not None
                    or _week.selection_deadline is not None
                ):
                    continue
                _candidates.append(_week)
                if _first_future is None and (
                    _week.delivery_date is None or _week.delivery_date >= _today
                ):
                    _first_future = _week
            self._next_configurable_week = (
                _first_future
                if _first_future is not None
                else (_candidates[0] if _candidates else None)
            )
        self._next_skipped_week = min(
            self._skipped_weeks,
            default=None,
            key=lambda week: (
                week.delivery_date.isoformat() if week.delivery_date is not None else "9999-12-31",
                week.week_id,
            ),
        )
        current_iso = today.isocalendar()[:2]
        self._delivery_count_this_week = sum(
            1
            for order in self.orders
            if order.delivery_date is not None
            and order.delivery_date.isocalendar()[:2] == current_iso
        )
        self._current_public_menu = self.public_menu_weeks[0] if self.public_menu_weeks else None
        # The most recent DELIVERED week — its date drives the "Last delivery date" sensor.
        #
        # Prefer the dedicated past-deliveries history, but that endpoint also returns the
        # UPCOMING week (e.g. it lists W28/Jul 6 alongside the delivered W27/Jun 29), so a naive
        # "newest week" would report a future box as the last delivery. Restrict to weeks dated
        # strictly before today. When history yields nothing usable, fall back to the newest
        # past-dated, non-skipped week from the main deliveries list, so the sensor still resolves
        # for accounts/regions where the history endpoint is empty.
        def _newest_past(weeks: Iterable[HelloFreshWeek]) -> HelloFreshWeek | None:
            return max(
                (
                    week
                    for week in weeks
                    if week.delivery_date is not None
                    and week.delivery_date < today
                    and not week.is_skipped
                ),
                default=None,
                key=lambda week: (week.delivery_date, week.week_id),
            )

        self._last_delivery_week = _newest_past(self.past_delivery_weeks) or _newest_past(
            self.weeks
        )
        self._serialized_orders = None
        self._serialized_weeks = None
        self._serialized_past_delivery_weeks = None
        self._serialized_weeks_needing_selection = None
        self._summarized_weeks_needing_selection = None
        self._serialized_public_menu_weeks = None
        self._serialized_subscriptions = None
        return self
