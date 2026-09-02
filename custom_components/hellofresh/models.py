"""Pure data models for the HelloFresh integration.

No HTTP, no aiohttp, no BeautifulSoup — just dataclasses and exceptions.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
import re
from typing import Any

# Smallest valid box HelloFresh sells: a week with fewer distinct meals than this has no valid
# box, so it genuinely still needs a selection. Mirrors the client's MIN_MEALS_PER_WEEK; kept as
# a plain module constant here so models.py stays dependency-free (no import from client).
MIN_MEALS_PER_WEEK = 2


def _coerce_number(value: Any) -> float | None:
    """Return a float for numeric input, else None. Bools are rejected (bool is an int)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _iso_duration_to_minutes(value: Any) -> int | None:
    """Convert an ISO-8601 duration like ``PT45M`` / ``PT1H30M`` to whole minutes.

    Cookbook and catalog payloads express times this way, unlike the weekly menu which uses
    plain integer minutes. Only hours and minutes appear in practice; anything unparseable
    yields None rather than a misleading zero.
    """
    if not isinstance(value, str):
        return None
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:\d+S)?)?",
        value.strip(),
        re.IGNORECASE,
    )
    if match is None:
        return None
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    total = days * 1440 + hours * 60 + minutes
    return total or None


def _parse_iso_datetime(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp, normalizing a trailing ``Z`` to UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


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
    # Short promo clip for the dish, from the menu payload's `recipe.videoLink`. Only a handful
    # of meals per week carry one (~5-20 of 400+), and the field is absent — not null — on meals
    # without one. Past weeks CAN carry one: the past-deliveries payload puts the field on the
    # meal itself rather than under `recipe` (see _recipe_node), so delivered meals keep their
    # clip. The card must still always fall back to `image_url` — most meals have no video, and
    # formats are mixed (.mp4 and .mov), with .mov unplayable in Chrome/Firefox.
    video_url: str | None = None
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
    # Menu badge text (e.g. "Premium Picks"), from `recipe.label.text`, plus HelloFresh's own
    # badge colors (`label.foregroundColor`/`backgroundColor`, "#RRGGBB") so cards can paint
    # the badge the way the website does instead of a one-size-fits-all pill.
    badge: str | None = None
    badge_foreground: str | None = None
    badge_background: str | None = None
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
    # Sold out for this week, so it cannot be chosen. Only the menus-service catalog carries
    # this (the delivery-menu endpoint omits it entirely), and HelloFresh sets `isHidden`
    # alongside it — the website drops such meals from the grid rather than showing them
    # greyed out. Surfaced rather than silently dropped so a card can explain why a meal it
    # previously showed has gone, and so a selection write can refuse it up front instead of
    # being rejected by the server.
    is_sold_out: bool = False
    is_hidden: bool = False
    # Actual per-serving price of the meal, from the delivery menu's `itemPrice` (distinct
    # from `surcharge_*`, which is only the premium *uplift* over a classic meal). HelloFresh
    # sends money as protobuf-style {units, nanos}; both a cents integer and a display float
    # are kept so callers don't have to reassemble it.
    price_cents: int | None = None
    price: float | None = None
    currency: str | None = None
    # "premium" / "classic" — HelloFresh's own pricing tier for the meal.
    price_group: str | None = None
    # Menu grouping for add-on style items ("appetizers", "desserts", ...).
    related_category: str | None = None
    # How many times this dish has previously been delivered to the customer, and the ISO week
    # of the most recent one — HelloFresh's own "you've had this before" signal, from
    # `recipe.feedback`. Absent for dishes never delivered.
    delivered_count: int | None = None
    last_delivered_week: str | None = None
    # The customer's own star rating for the dish, when they have rated it. `feedback` carries
    # EITHER the delivery-history pair above OR this rating pair, never both at once.
    rating: int | None = None
    rating_scale: int | None = None
    # Whether this dish is bookmarked in the customer's cookbook. Resolved by cross-referencing
    # the week's recipe ids against the cookbook search endpoint, which is a *filter* (you send
    # candidate ids, it returns the subset that is bookmarked) rather than a list endpoint —
    # so this is only populated when that lookup ran and succeeded. None means "not looked up",
    # which is deliberately distinct from False ("looked up, not a favorite") so the card can
    # hide the heart entirely rather than render a misleading empty one.
    is_favorite: bool | None = None

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
            "video_url": self.video_url,
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
            "badge_foreground": self.badge_foreground,
            "badge_background": self.badge_background,
            "variation_title": self.variation_title,
            "variation_group": self.variation_group,
            "is_favorite": self.is_favorite,
            "is_sold_out": self.is_sold_out,
            "is_hidden": self.is_hidden,
            "price_cents": self.price_cents,
            "price": self.price,
            "currency": self.currency,
            "price_group": self.price_group,
            "related_category": self.related_category,
            "delivered_count": self.delivered_count,
            "last_delivered_week": self.last_delivered_week,
            "rating": self.rating,
            "rating_scale": self.rating_scale,
        }


@dataclass(slots=True)
class HelloFreshMarketItem:
    """A HelloFresh Market add-on (appetizer, side, dessert, protein, etc.) for a week.

    Distinct from a meal recipe: market items are billed per-quantity extras, identified in the
    cart by their ``index``/``sku`` and ordered with a quantity up to ``max_quantity``.
    """

    item_id: str
    name: str
    # The underlying HelloFresh recipe id, when the add-on has one — market items DO carry a
    # normal 24-hex recipe id, so the full recipe (ingredients, steps) can be looked up like a
    # meal's. Distinct from `item_id`, which falls back to the SKU or index and therefore
    # cannot be handed to the recipe-detail API.
    recipe_id: str | None = None
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
            "recipe_id": self.recipe_id,
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
    # The moment the box was ACTUALLY delivered, from the deliveries payload's
    # ``tracking.delivery_date`` — a real carrier timestamp, unlike ``delivery_date`` which is
    # the scheduled day. Only set for DELIVERED weeks (before delivery the same field holds a
    # scheduled-noon placeholder). Full datetime (with offset) so the frontend can render the
    # date in the viewer's timezone — an evening ET delivery is already the next day in UTC.
    delivered_at: datetime | None = None
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
    # The website's menu sections, from the menu payload's `categories` block: each row is
    # {name, slug, recipe_ids} with the ids drawn from the section's own items plus its
    # subcategories' (a section like "Featured" lists ONLY subcategories). Menu-payload weeks
    # only; empty for history-sourced weeks. Lets the meal-planner card offer the same
    # section browsing the site's menu page grew, without re-deriving sections from tags.
    menu_categories: list[dict[str, Any]] = field(default_factory=list)
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
    # Excluded from equality: the coordinator's always_update=False deep-compares each poll's
    # HelloFreshAccountData against the last, and this dict holds the full raw delivery payload
    # plus (for merged weeks) the entire menu payload under "_menu_payload" — MBs of nested
    # dicts. Comparing them field-by-field every unchanged poll is pure cost; the normalized
    # scalar fields above already capture every change that should notify listeners.
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def is_paused(self) -> bool:
        """Return True when the week's subscription is paused — its box never ships.

        A paused week can still carry HelloFresh's auto-fill picks in the payload, but those
        are phantom selections (nothing ships), so paused weeks are excluded from the
        "needs selection" / "preselected" signals, matching the meal-planner card's
        ``_isPaused`` treatment.
        """
        return (self.status or "").strip().upper() == "PAUSED"

    @property
    def selection_deadline_passed(self) -> bool:
        """Return True when the week's selection deadline is known and already behind us."""
        if self.selection_deadline is None:
            return False
        deadline = self.selection_deadline
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        return deadline <= datetime.now(UTC)

    @property
    def is_editable(self) -> bool:
        """Return True while the customer can still change this week's meal selection.

        Mirrors the meal-planner card's ``_isEditable`` exactly: the API must allow meal
        swaps (``allowed_actions.mealSwap``), the week must not be skipped, and the
        selection deadline (when known) must not have passed. Keeping the two in lockstep
        is what guarantees the "Weeks preselected by HelloFresh" sensor and the card's
        "still need a meal selection" banner agree.
        """
        if self.is_skipped:
            return False
        if not self.allowed_actions.get("mealSwap"):
            return False
        return not self.selection_deadline_passed

    @property
    def needs_selection(self) -> bool:
        """Return True when the week still needs the customer's attention for meal choices.

        A week needs attention when it is still EDITABLE (see ``is_editable``) and either:
          * HelloFresh AUTO-PICKED the meals (``meals_preselected``) and the customer hasn't
            overridden them — the app's "we picked these, review them" state; or
          * the selection is below the smallest valid box (< ``MIN_MEALS_PER_WEEK``), e.g. an
            untouched week with 0 meals.

        Crucially it does NOT fire merely because ``meals_selected < meals_required``: a customer
        can deliberately RESIZE a week to fewer meals than their base plan (e.g. pick 2 on a
        3-meal plan). That is a complete, valid choice — flagging it would be a false "needs
        selection" (it wrongly kept ``binary_sensor.needs_meal_selection`` on).

        Once the week is locked (selection deadline passed, or the API disallows meal swaps)
        there is nothing actionable left — the box ships with whatever is on it — so it no
        longer "needs" anything. This keeps ``binary_sensor.needs_meal_selection`` and the
        meal-planner card's banner (which only prompts for editable weeks) in agreement.
        Past and paused weeks are never counted for the same reason: a shipped box can't be
        changed and a paused box never ships.
        """
        if not self.is_editable or self.is_paused:
            return False
        if self.delivery_date is not None and self.delivery_date < date.today():
            return False
        if self.meals_selected is None:
            return False
        if self.meals_selected < MIN_MEALS_PER_WEEK:
            return True
        return self.meals_preselected

    @property
    def auto_picked(self) -> bool:
        """Return True when HelloFresh auto-picked this week's meals AND that is still fixable.

        Driven by the menu's week-level ``mealsPreselected`` flag, gated on ``is_editable``:
        once the selection deadline passes the box ships with the auto-picks regardless, so
        the week stops counting toward "Weeks preselected by HelloFresh" — the sensor is a
        call to action, and matches the card's banner (which only prompts for editable
        weeks). The per-week informational flag remains available as ``meals_preselected``
        (and the "Next delivery preselected" sensor). Skipped, paused, and past weeks are
        never counted — no box ships (or it already shipped), so there is nothing to act on.
        """
        if not self.meals_preselected or self.is_paused or not self.is_editable:
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
            "delivered_at": self.delivered_at.isoformat() if self.delivered_at else None,
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
            "is_editable": self.is_editable,
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
            "menu_categories": self.menu_categories,
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
    # The carrier's own delivery estimate, from the SCM tracking lookup's status history
    # (``est_delivery_time``). Distinct from ``delivery_date``, which is HelloFresh's scheduled
    # noon anchor: the carrier reports midnight-of-the-estimated-day, so this is a date-precision
    # estimate despite being carried as a timestamp.
    estimated_delivery: datetime | None = None
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
            "estimated_delivery": (
                self.estimated_delivery.isoformat() if self.estimated_delivery else None
            ),
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
    # The active plan preference (e.g. "quick", "veggie") HelloFresh uses to auto-preselect
    # meals — resolved from unified-preferences/profile-service and written back onto the raw
    # payload by the client, falling back to ``preset`` when the resolved value isn't available.
    plan_preference: str | None = None
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
    # Excluded from equality (see HelloFreshWeek.raw): holds the raw subscription payload,
    # deep-compared each unchanged poll under the coordinator's always_update=False otherwise.
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

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
            "plan_preference": self.plan_preference,
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
    def from_api(cls, payload: dict[str, Any]) -> HelloFreshFoodProfileOptions:
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
class HelloFreshDeliveryOption:
    """One selectable delivery day/slot for a plan (from ``/gw/api/delivery_dates_options``).

    Richer than the per-week ``availableOneOffOptions`` (which carries only ``{handle,
    delivery_date}``): this includes the human-readable weekday name, weekday number, price, and
    whether it is the plan's current default — enough to render a full delivery-day picker.
    """

    handle: str
    delivery_name: str | None = None
    delivery_day: int | None = None
    delivery_from: str | None = None
    delivery_to: str | None = None
    price_cents: int | None = None
    is_default: bool = False

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> HelloFreshDeliveryOption | None:
        if not isinstance(raw, dict):
            return None
        handle = raw.get("handle")
        if not handle:
            return None
        return cls(
            handle=str(handle),
            delivery_name=raw.get("deliveryName"),
            delivery_day=raw.get("deliveryDay"),
            delivery_from=raw.get("deliveryFrom"),
            delivery_to=raw.get("deliveryTo"),
            price_cents=raw.get("priceInCents"),
            is_default=bool(raw.get("isDefault")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "delivery_name": self.delivery_name,
            "delivery_day": self.delivery_day,
            "delivery_from": self.delivery_from,
            "delivery_to": self.delivery_to,
            "price_cents": self.price_cents,
            "price": (self.price_cents / 100) if self.price_cents is not None else None,
            "is_default": self.is_default,
        }


@dataclass(slots=True)
class HelloFreshFavorite:
    """A recipe bookmarked in the customer's cookbook (``/gw/cookbook/v1/internal-recipes``).

    HelloFresh keys bookmarks by ``bookmark_id``, which is the recipe id suffixed with the
    locale (``<recipeId>-en-US``) — the same 24-hex recipe id namespace the weekly menu uses,
    so a menu recipe can be matched to a bookmark by stripping the suffix. ``favorite_id`` is a
    separate server-assigned id returned when the bookmark is created; it identifies the
    bookmark row itself and is what a delete would target.
    """

    bookmark_id: str
    recipe_id: str
    # Server-assigned id of the bookmark row. The search (filter) endpoint returns it alongside
    # bookmark_id; the create endpoint returns it in a much richer body.
    favorite_id: str | None = None
    name: str | None = None
    headline: str | None = None
    description: str | None = None
    image_url: str | None = None
    url: str | None = None
    total_time_minutes: int | None = None
    prep_time_minutes: int | None = None
    calories_kcal: float | None = None
    protein_g: float | None = None
    created_at: datetime | None = None

    @staticmethod
    def recipe_id_from_bookmark(bookmark_id: Any) -> str | None:
        """Strip the ``-<locale>`` suffix off a bookmark id to get the bare recipe id.

        Bookmark ids look like ``694053fe353e00bd89ba2d3e-en-US``. Only the locale tail is
        removed: recipe ids are hex and never contain a hyphen, so splitting on the first
        hyphen is safe and keeps this correct for locales like ``en-US`` that contain one.
        """
        if not isinstance(bookmark_id, str) or not bookmark_id.strip():
            return None
        return bookmark_id.strip().split("-", 1)[0] or None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> HelloFreshFavorite | None:
        if not isinstance(raw, dict):
            return None
        bookmark_id = raw.get("bookmark_id")
        if not isinstance(bookmark_id, str) or not bookmark_id.strip():
            return None
        recipe_id = cls.recipe_id_from_bookmark(bookmark_id)
        if recipe_id is None:
            return None
        nutrition = raw.get("nutrition") if isinstance(raw.get("nutrition"), dict) else {}
        return cls(
            bookmark_id=bookmark_id.strip(),
            recipe_id=recipe_id,
            favorite_id=raw.get("id") if isinstance(raw.get("id"), str) else None,
            name=raw.get("title") or raw.get("name"),
            headline=raw.get("headline"),
            description=raw.get("description"),
            image_url=raw.get("thumbnail_url") or raw.get("image_url"),
            url=raw.get("url"),
            total_time_minutes=_iso_duration_to_minutes(raw.get("total_time")),
            prep_time_minutes=_iso_duration_to_minutes(raw.get("prep_time")),
            calories_kcal=_coerce_number(nutrition.get("calories")),
            protein_g=_coerce_number(nutrition.get("protein")),
            created_at=_parse_iso_datetime(raw.get("created_at")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "bookmark_id": self.bookmark_id,
            "recipe_id": self.recipe_id,
            "favorite_id": self.favorite_id,
            "name": self.name,
            "headline": self.headline,
            "description": self.description,
            "image_url": self.image_url,
            "url": self.url,
            "total_time_minutes": self.total_time_minutes,
            "prep_time_minutes": self.prep_time_minutes,
            "calories_kcal": self.calories_kcal,
            "protein_g": self.protein_g,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


@dataclass(slots=True)
class HelloFreshRecipeDetail:
    """A complete recipe from ``/gw/recipes/recipes/{id}`` — steps, ingredients, the lot.

    Distinct from :class:`HelloFreshRecipe` (a menu meal for one delivery week, carrying
    selection state) and :class:`HelloFreshCatalogRecipe` (a browse-listing row). This is the
    full cooking detail, and notably it comes from a plain ``/gw/`` API rather than the
    website's Next.js data URLs — so unlike the browse catalog it needs no build-id scraping.

    ``ingredients`` are merged with the amounts from the matching ``yields`` entry, so each
    line already reads "1.5 tablespoon Sour Cream" for the requested serving count instead of
    forcing every caller to join the two arrays itself.
    """

    recipe_id: str
    name: str
    headline: str | None = None
    description: str | None = None
    slug: str | None = None
    image_url: str | None = None
    video_url: str | None = None
    # Printable recipe-card PDF, when HelloFresh has generated one.
    card_url: str | None = None
    url: str | None = None
    difficulty: int | None = None
    prep_time_minutes: int | None = None
    total_time_minutes: int | None = None
    rating: float | None = None
    ratings_count: int | None = None
    favorites_count: int | None = None
    category: str | None = None
    cuisines: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    allergens: list[str] = field(default_factory=list)
    utensils: list[str] = field(default_factory=list)
    nutrition: dict[str, str] = field(default_factory=dict)
    calories_kcal: float | None = None
    # Serving counts this recipe can be scaled to (from the `yields` array), e.g. [2, 4].
    available_yields: list[int] = field(default_factory=list)
    # The serving count `ingredients` amounts were resolved for.
    servings: int | None = None
    # Each entry: {name, amount, unit, image_url, shipped}.
    ingredients: list[dict[str, Any]] = field(default_factory=list)
    # Each entry: {index, instructions}. HTML is deliberately dropped — the cards render text.
    steps: list[dict[str, Any]] = field(default_factory=list)
    is_favorite: bool | None = None

    @classmethod
    def from_api(
        cls,
        raw: dict[str, Any],
        *,
        servings: int | None = None,
        image_base: str | None = None,
    ) -> Any:
        if not isinstance(raw, dict):
            return None
        recipe_id = raw.get("id")
        name = raw.get("name")
        if not isinstance(recipe_id, str) or not isinstance(name, str) or not name.strip():
            return None

        # The payload offers BOTH a bare `imagePath` and a ready-made absolute `imageLink`,
        # and the tempting one is wrong: `imageLink` points at a CloudFront distribution that
        # now answers 502 for every path. So the path is joined to the verified host instead,
        # exactly as the catalog rows are, and `imageLink` is used only as a last resort.
        image_path = raw.get("imagePath")
        image_url = None
        if isinstance(image_path, str) and image_path.strip() and image_base:
            image_url = f"{image_base.rstrip('/')}{image_path}"
        elif isinstance(image_path, str) and image_path.strip():
            image_url = image_path
        else:
            link = raw.get("imageLink")
            image_url = link if isinstance(link, str) and link.strip() else None

        yields = raw.get("yields") if isinstance(raw.get("yields"), list) else []
        available = sorted(
            {
                int(entry["yields"])
                for entry in yields
                if isinstance(entry, dict) and isinstance(entry.get("yields"), (int, float))
            }
        )
        # Pick the requested serving count when offered, else the smallest available — which
        # is the standard 2-person box and matches what the website shows by default.
        chosen = servings if servings in available else (available[0] if available else None)
        amounts: dict[str, dict[str, Any]] = {}
        for entry in yields:
            if not isinstance(entry, dict) or entry.get("yields") != chosen:
                continue
            for line in entry.get("ingredients") or []:
                if isinstance(line, dict) and isinstance(line.get("id"), str):
                    amounts[line["id"]] = line

        ingredients: list[dict[str, Any]] = []
        for item in raw.get("ingredients") or []:
            if not isinstance(item, dict):
                continue
            amount = amounts.get(str(item.get("id")), {})
            ingredients.append(
                {
                    "name": item.get("name"),
                    "amount": amount.get("amount"),
                    "unit": amount.get("unit"),
                    "image_url": item.get("imageLink"),
                    # False marks a pantry staple you supply yourself (salt, oil, ...) rather
                    # than something that arrives in the box. Deliberately tri-state: a
                    # *missing* key stays None ("unknown"), because coercing it to False
                    # would claim HelloFresh isn't shipping an ingredient it is. Only the
                    # prep list depends on telling those apart, and it treats None as
                    # in-box; the recipe-detail card already tests `shipped === false`.
                    "shipped": (bool(item["shipped"]) if item.get("shipped") is not None else None),
                }
            )

        steps: list[dict[str, Any]] = []
        for step in raw.get("steps") or []:
            if not isinstance(step, dict):
                continue
            text = step.get("instructions")
            if not isinstance(text, str) or not text.strip():
                continue
            # HelloFresh pads instructions with blank lines; collapse to tidy paragraphs.
            cleaned = "\n".join(line.strip() for line in text.split("\n") if line.strip())
            steps.append({"index": step.get("index"), "instructions": cleaned})

        nutrition: dict[str, str] = {}
        calories: float | None = None
        for entry in raw.get("nutrition") or []:
            if not isinstance(entry, dict):
                continue
            label = entry.get("name")
            amount = entry.get("amount")
            if not isinstance(label, str) or amount is None:
                continue
            nutrition[label] = f"{amount}{entry.get('unit') or ''}"
            if label.lower() == "calories":
                calories = _coerce_number(amount)

        category = raw.get("category")
        return cls(
            recipe_id=recipe_id,
            name=name.strip(),
            headline=raw.get("headline"),
            description=raw.get("description"),
            slug=raw.get("slug"),
            image_url=image_url,
            video_url=raw.get("videoLink") or None,
            card_url=raw.get("cardLink"),
            url=raw.get("websiteUrl") or raw.get("canonicalLink"),
            difficulty=raw.get("difficulty") if isinstance(raw.get("difficulty"), int) else None,
            prep_time_minutes=_iso_duration_to_minutes(raw.get("prepTime")),
            total_time_minutes=_iso_duration_to_minutes(raw.get("totalTime")),
            rating=_coerce_number(raw.get("averageRating")),
            ratings_count=(
                int(raw["ratingsCount"]) if isinstance(raw.get("ratingsCount"), int) else None
            ),
            favorites_count=(
                int(raw["favoritesCount"]) if isinstance(raw.get("favoritesCount"), int) else None
            ),
            category=category.get("name") if isinstance(category, dict) else None,
            cuisines=[
                c["name"]
                for c in raw.get("cuisines") or []
                if isinstance(c, dict) and isinstance(c.get("name"), str)
            ],
            tags=[
                t["name"]
                for t in raw.get("tags") or []
                if isinstance(t, dict) and isinstance(t.get("name"), str)
            ],
            allergens=[
                a["name"]
                for a in raw.get("allergens") or []
                if isinstance(a, dict) and isinstance(a.get("name"), str)
            ],
            utensils=[
                u["name"]
                for u in raw.get("utensils") or []
                if isinstance(u, dict) and isinstance(u.get("name"), str)
            ],
            nutrition=nutrition,
            calories_kcal=calories,
            available_yields=available,
            servings=chosen,
            ingredients=ingredients,
            steps=steps,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "recipe_id": self.recipe_id,
            "name": self.name,
            "headline": self.headline,
            "description": self.description,
            "slug": self.slug,
            "image_url": self.image_url,
            "video_url": self.video_url,
            "card_url": self.card_url,
            "url": self.url,
            "difficulty": self.difficulty,
            "prep_time_minutes": self.prep_time_minutes,
            "total_time_minutes": self.total_time_minutes,
            "rating": self.rating,
            "ratings_count": self.ratings_count,
            "favorites_count": self.favorites_count,
            "category": self.category,
            "cuisines": self.cuisines,
            "tags": self.tags,
            "allergens": self.allergens,
            "utensils": self.utensils,
            "nutrition": self.nutrition,
            "calories_kcal": self.calories_kcal,
            "available_yields": self.available_yields,
            "servings": self.servings,
            "ingredients": self.ingredients,
            "steps": self.steps,
            "is_favorite": self.is_favorite,
        }


@dataclass(slots=True)
class HelloFreshProfileCompletion:
    """How complete the customer's food profile is (``/profile/completion``).

    HelloFresh groups the fields into priority tiers (p0 = the ones it most wants answered),
    each with its own rate; ``overall`` is the headline figure the card shows.
    """

    completed: int = 0
    total: int = 0
    # Field name -> whether it has been answered, flattened across all priority tiers.
    fields: dict[str, bool] = field(default_factory=dict)

    @property
    def rate(self) -> float:
        """Completion as a 0.0-1.0 fraction (0.0 when the profile reports no fields)."""
        return (self.completed / self.total) if self.total else 0.0

    @property
    def percent(self) -> int:
        return round(self.rate * 100)

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Any:
        if not isinstance(raw, dict):
            return None
        overall = raw.get("overall")
        if not isinstance(overall, dict):
            return None
        fields: dict[str, bool] = {}
        for key, section in raw.items():
            if key == "overall" or not isinstance(section, dict):
                continue
            for entry in section.get("fields") or []:
                if isinstance(entry, dict) and isinstance(entry.get("field"), str):
                    fields[entry["field"]] = bool(entry.get("completed"))
        return cls(
            completed=overall.get("completed") if isinstance(overall.get("completed"), int) else 0,
            total=overall.get("total") if isinstance(overall.get("total"), int) else 0,
            fields=fields,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "completed": self.completed,
            "total": self.total,
            "rate": self.rate,
            "percent": self.percent,
            # The fields still worth prompting the user about.
            "incomplete_fields": sorted(k for k, done in self.fields.items() if not done),
            "fields": self.fields,
        }


@dataclass(slots=True)
class HelloFreshCatalogRecipe:
    """A recipe from the public ``/recipes`` browse catalog (~10k recipes, all customers).

    Distinct from :class:`HelloFreshRecipe`, which is a *menu* meal for a specific delivery week
    and carries selection state. A catalog recipe is subscription-independent browse content, so
    it has ratings and a canonical URL but no course index, selection, or surcharge.
    """

    recipe_id: str
    name: str
    headline: str | None = None
    slug: str | None = None
    image_url: str | None = None
    url: str | None = None
    rating: float | None = None
    ratings_count: int | None = None
    prep_time_minutes: int | None = None
    is_favorite: bool | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any], *, image_base: str | None = None) -> Any:
        if not isinstance(raw, dict):
            return None
        recipe_id = raw.get("recipeId") or raw.get("id")
        name = raw.get("name")
        if not isinstance(recipe_id, str) or not isinstance(name, str) or not name.strip():
            return None
        # Catalog rows carry `imagePath` (a bare path like "/image/foo.jpg"), not a full URL;
        # the CDN host has to be prefixed for it to be usable in an <img src>.
        image_path = raw.get("imagePath")
        image_url = None
        if isinstance(image_path, str) and image_path.strip():
            image_url = (
                f"{image_base.rstrip('/')}{image_path}"
                if image_base and image_path.startswith("/")
                else image_path
            )
        return cls(
            recipe_id=recipe_id.split("-", 1)[0],
            name=name.strip(),
            headline=raw.get("headline"),
            slug=raw.get("slug"),
            image_url=image_url,
            url=raw.get("websiteUrl"),
            rating=_coerce_number(raw.get("aggregateRating") or raw.get("averageRating")),
            ratings_count=(
                int(raw["aggregateRatingsCount"])
                if isinstance(raw.get("aggregateRatingsCount"), (int, float))
                else None
            ),
            prep_time_minutes=_iso_duration_to_minutes(raw.get("prepTime")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "recipe_id": self.recipe_id,
            "name": self.name,
            "headline": self.headline,
            "slug": self.slug,
            "image_url": self.image_url,
            "url": self.url,
            "rating": self.rating,
            "ratings_count": self.ratings_count,
            "prep_time_minutes": self.prep_time_minutes,
            "is_favorite": self.is_favorite,
        }


@dataclass(slots=True)
class HelloFreshRecipeCollection:
    """A browsable category in the recipe catalog (e.g. "Chicken Recipes", "Hall of Fame")."""

    slug: str
    name: str
    collection_id: str | None = None
    thumbnail_url: str | None = None
    # Catalog path relative to /recipes/, WITHOUT a leading slash: "noodle-recipes" for a
    # top-level category, "noodle-recipes/ramen-noodles" for a child. A nested category is NOT
    # reachable at its bare slug — /recipes/ramen-noodles 301-redirects away — so this, not
    # `slug`, is what a lookup must use.
    path: str | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any], *, image_base: str | None = None) -> Any:
        if not isinstance(raw, dict):
            return None
        slug = raw.get("slug")
        name = raw.get("name")
        if not isinstance(slug, str) or not isinstance(name, str):
            return None
        thumb = raw.get("thumbnail")
        thumbnail_url = None
        if isinstance(thumb, str) and thumb.strip():
            thumbnail_url = (
                f"{image_base.rstrip('/')}{thumb}"
                if image_base and thumb.startswith("/")
                else thumb
            )
        return cls(
            slug=slug,
            name=name,
            collection_id=raw.get("id") if isinstance(raw.get("id"), str) else None,
            thumbnail_url=thumbnail_url,
            path=cls._path_from_breadcrumbs(raw.get("breadcrumbs")) or slug,
        )

    @staticmethod
    def _path_from_breadcrumbs(breadcrumbs: Any) -> str | None:
        """Derive the catalog path from a row's breadcrumb trail.

        The last crumb's ``url`` is the category's real page ("/recipes/noodle-recipes/
        ramen-noodles"); everything after "/recipes/" is the path a lookup needs.
        """
        if not isinstance(breadcrumbs, list) or not breadcrumbs:
            return None
        last = breadcrumbs[-1]
        url = last.get("url") if isinstance(last, dict) else None
        if not isinstance(url, str):
            return None
        prefix = "/recipes/"
        if not url.startswith(prefix):
            return None
        return url[len(prefix) :].strip("/") or None

    def as_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "collection_id": self.collection_id,
            "thumbnail_url": self.thumbnail_url,
            # What a lookup must pass back as `collection` — differs from `slug` for a nested
            # category, which is not reachable at its bare slug.
            "path": self.path or self.slug,
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
    def from_api(cls, payload: dict[str, Any]) -> HelloFreshFoodProfile:
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
            patch["goals"] = {
                k: (list(v) if isinstance(v, (list, tuple)) else v) for k, v in goals.items()
            }

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
    # Lazily-memoized serializations MUST NOT participate in equality: entities populate
    # them on the OLD data object between polls while the fresh object has them reset, so
    # including them made every poll compare unequal and defeated always_update=False.
    _serialized_orders: list[dict[str, Any]] | None = field(default=None, compare=False)
    _serialized_weeks: list[dict[str, Any]] | None = field(default=None, compare=False)
    _serialized_past_delivery_weeks: list[dict[str, Any]] | None = field(
        default=None, compare=False
    )
    _serialized_weeks_needing_selection: list[dict[str, Any]] | None = field(
        default=None, compare=False
    )
    _summarized_weeks_needing_selection: list[dict[str, Any]] | None = field(
        default=None, compare=False
    )
    _serialized_public_menu_weeks: list[dict[str, Any]] | None = field(default=None, compare=False)
    _serialized_subscriptions: list[dict[str, Any]] | None = field(default=None, compare=False)
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
        # Week ids are ISO weeks, which two subscriptions can share. Keyed by week_id alone
        # the last-sorted week silently won, so id-based lookups (get_week, the skip switch,
        # service writes) could act on the OTHER subscription's box. On collision the primary
        # subscription's week wins deterministically, matching next_modifiable_week, which is
        # anchored on the primary subscription's handles.
        primary_sub_id = self.subscriptions[0].subscription_id if self.subscriptions else None
        self._weeks_by_id = {}
        for week in self.weeks:
            existing = self._weeks_by_id.get(week.week_id)
            if (
                existing is not None
                and existing.subscription_id == primary_sub_id
                and week.subscription_id != primary_sub_id
            ):
                continue
            self._weeks_by_id[week.week_id] = week
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
            and not ((week := self._weeks_by_id.get(order.week_id)) is not None and week.is_skipped)
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

        # The past-deliveries endpoint reports no carrier timestamp, so a week chosen from that
        # list has `delivered_at` unset and "Tracked shipment date" would read Unknown even
        # though the box arrived. Only the ranged deliveries payload carries
        # `tracking.delivery_date`, so back-fill the real arrival time from the matching account
        # week. Match on week id, requiring subscription ids to agree when BOTH sides carry one
        # (with two subscriptions the same ISO week is two different boxes).
        if self._last_delivery_week is not None and self._last_delivery_week.delivered_at is None:
            for account_week in self.weeks:
                if (
                    account_week.week_id == self._last_delivery_week.week_id
                    and account_week.delivered_at is not None
                    and (
                        account_week.subscription_id is None
                        or self._last_delivery_week.subscription_id is None
                        or account_week.subscription_id == self._last_delivery_week.subscription_id
                    )
                ):
                    self._last_delivery_week.delivered_at = account_week.delivered_at
                    break
        self._serialized_orders = None
        self._serialized_weeks = None
        self._serialized_past_delivery_weeks = None
        self._serialized_weeks_needing_selection = None
        self._summarized_weeks_needing_selection = None
        self._serialized_public_menu_weeks = None
        self._serialized_subscriptions = None
        return self
