"""Pure data models for the HelloFresh integration.

No HTTP, no aiohttp, no BeautifulSoup — just dataclasses and exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


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
    difficulty: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Serialize recipe data for Home Assistant state attributes."""
        return {
            "recipe_id": self.recipe_id,
            "name": self.name,
            "preference": self.preference,
            "is_selected": self.is_selected,
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
            "difficulty": self.difficulty,
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
    recipes: list[HelloFreshRecipe] = field(default_factory=list)
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
        """Return True when the week still needs meal choices."""
        if self.is_skipped:
            return False
        if self.meals_required is None or self.meals_selected is None:
            return False
        return self.meals_selected < self.meals_required

    @property
    def selection_progress(self) -> str | None:
        """Return human-friendly meal selection progress."""
        if self.meals_required is None or self.meals_selected is None:
            return None
        return f"{self.meals_selected}/{self.meals_required}"

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
    first_box_delivered: bool | None = None
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
            "first_box_delivered": self.first_box_delivered,
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
    def delivery_count_this_week(self) -> int:
        """Return the number of deliveries in the current ISO week."""
        return self._delivery_count_this_week

    @property
    def selection_deadline_passed(self) -> bool:
        """Return True if the next upcoming week's selection deadline has passed."""
        next_week = self._next_configurable_week
        if next_week is None or next_week.selection_deadline is None:
            return False
        now = (
            datetime.now(next_week.selection_deadline.tzinfo)
            if next_week.selection_deadline.tzinfo is not None
            else datetime.now()
        )
        return next_week.selection_deadline < now

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
        # Only deliveries today or later are "upcoming". The deliveries endpoint returns a
        # wide window (≈12 weeks back to 1 week ahead), so without this filter next_order
        # would resolve to the *oldest* historical delivery instead of the next one.
        today = date.today()
        self._upcoming_orders = [
            order
            for order in self.orders
            if order.delivery_date is not None and order.delivery_date >= today
        ]
        self._next_order = self._upcoming_orders[0] if self._upcoming_orders else None
        self._weeks_by_id = {week.week_id: week for week in self.weeks}
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
        self._last_delivery_week = (
            self.past_delivery_weeks[-1] if self.past_delivery_weeks else None
        )
        self._serialized_orders = None
        self._serialized_weeks = None
        self._serialized_past_delivery_weeks = None
        self._serialized_weeks_needing_selection = None
        self._summarized_weeks_needing_selection = None
        self._serialized_public_menu_weeks = None
        self._serialized_subscriptions = None
        return self
