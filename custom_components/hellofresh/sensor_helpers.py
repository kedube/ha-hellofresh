"""Sensor value, icon, and attribute helpers for HelloFresh."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .models import HelloFreshAccountData
from .parsers import iso_week_label


def token_seconds_remaining(expires_at: datetime | None) -> int | None:
    """Return whole seconds until the access token expires, never negative."""
    if expires_at is None:
        return None
    remaining = expires_at.timestamp() - datetime.now(UTC).timestamp()
    return max(int(remaining), 0)


def token_minutes_remaining(expires_at: datetime | None) -> int | None:
    """Return whole minutes until a token expires, never negative."""
    seconds = token_seconds_remaining(expires_at)
    if seconds is None:
        return None
    return seconds // 60


def token_days_remaining(expires_at: datetime | None) -> int | None:
    """Return whole days until a token expires, never negative."""
    seconds = token_seconds_remaining(expires_at)
    if seconds is None:
        return None
    return seconds // 86400


def _next_order_value(attr: str) -> Callable[[HelloFreshAccountData], Any]:
    return lambda data: getattr(data.next_order, attr) if data.next_order else None


def _tracked_order_value(attr: str) -> Callable[[HelloFreshAccountData], Any]:
    return lambda data: getattr(data.tracked_order, attr) if data.tracked_order else None


def _week_value(attr: str) -> Callable[[HelloFreshAccountData], Any]:
    def _get(data: HelloFreshAccountData) -> Any:
        week = data.next_configurable_week
        return getattr(week, attr) if week is not None else None

    return _get


def _sub_value(attr: str) -> Callable[[HelloFreshAccountData], Any]:
    return lambda data: (
        getattr(data.primary_subscription, attr) if data.primary_subscription else None
    )


VALUE_GETTERS: dict[str, Callable[[HelloFreshAccountData], Any]] = {
    "next_delivery_date": _next_order_value("delivery_date"),
    "next_order_status": _next_order_value("status"),
    "next_box_total_price": lambda data: data.next_delivery_total,
    "next_delivery_subscription": _next_order_value("subscription_id"),
    "next_delivery_slot": _next_order_value("slot_label"),
    "upcoming_delivery_count": lambda data: len(data.upcoming_orders),
    "shipment_tracking_status": _tracked_order_value("tracking_status"),
    "shipment_tracking_number": _tracked_order_value("tracking_number"),
    "tracked_shipment_carrier": _tracked_order_value("carrier"),
    "weeks_needing_selection": lambda data: len(data.weeks_needing_selection),
    "next_selection_deadline": _week_value("selection_deadline"),
    "selected_meal_count": lambda data: (
        (data.next_configurable_week.meals_selected or 0) if data.next_configurable_week else 0
    ),
    "required_meal_count": lambda data: (
        data.next_configurable_week.meals_required
        if data.next_configurable_week and data.next_configurable_week.meals_required is not None
        else (data.primary_subscription.meals_required if data.primary_subscription else 0)
    ),
    "selected_plan": lambda data: (
        (data.primary_subscription.plan_name or data.primary_subscription.display_name)
        if data.primary_subscription
        else None
    ),
    "number_of_people": _sub_value("servings"),
    "delivery_address": _sub_value("delivery_address"),
    "recent_payment_date": _sub_value("recent_payment_date"),
    "next_payment_date": _sub_value("next_payment_date"),
    # The ISO week identifier of the next configurable delivery week (e.g. "2026-W25"), so
    # this reads as a distinct "which week" value rather than duplicating next_delivery_date.
    # Falls back to the ISO week derived from the delivery date when week_id isn't a usable
    # ISO id.
    "next_delivery_week": lambda data: (
        iso_week_label(
            data.next_configurable_week.week_id,
            data.next_configurable_week.delivery_date,
        )
        if data.next_configurable_week
        else None
    ),
    "delivery_count_this_week": lambda data: data.delivery_count_this_week,
    "public_menu_recipe_count": lambda data: (
        len(data.current_public_menu.recipes) if data.current_public_menu else 0
    ),
    "subscription_count": lambda data: data.subscription_count,
    "skipped_week_count": lambda data: len(data.skipped_weeks),
    "next_skipped_week": lambda data: (
        data.next_skipped_week.display_name if data.next_skipped_week else None
    ),
    "boxes_received": lambda data: data.boxes_received,
    "last_delivery_date": lambda data: (
        data.last_delivery_week.delivery_date if data.last_delivery_week else None
    ),
    "account_id": lambda data: data.account_id,
    "recent_order_id": lambda data: data.recent_order_id,
    "next_box_coupon": _sub_value("coupon_code"),
    "next_delivery_tracking_url": _tracked_order_value("tracking_url"),
    "next_holiday_delivery_date": _week_value("holiday_delivery_date"),
    "next_holiday_message": _week_value("holiday_message"),
    "next_delivery_blocked": _week_value("delivery_blocked"),
}


ORDER_ATTRIBUTE_KEYS = frozenset(
    {
        "next_delivery_date",
        "next_box_total_price",
        "next_delivery_subscription",
        "next_delivery_slot",
    }
)

WEEK_ATTRIBUTE_KEYS = frozenset(
    {
        "next_delivery_week",
        "next_selection_deadline",
        "selected_meal_count",
        "required_meal_count",
    }
)

SUBSCRIPTION_CONTEXT_KEYS = frozenset(
    {
        "selected_plan",
        "subscription_count",
        "number_of_people",
        "delivery_address",
        "recent_payment_date",
        "next_payment_date",
        "upcoming_delivery_count",
        "tracked_shipment_carrier",
        "skipped_week_count",
        "next_skipped_week",
    }
)

DELIVERY_HISTORY_KEYS = frozenset({"boxes_received", "last_delivery_date"})


def sensor_native_value(
    key: str,
    data: HelloFreshAccountData,
    api_base_url: str,
) -> Any:
    """Return the native value for a sensor key."""
    if key == "api_base_url":
        return api_base_url
    handler = VALUE_GETTERS.get(key)
    return handler(data) if handler is not None else None


def sensor_icon(key: str, data: HelloFreshAccountData, default_icon: str | None) -> str | None:
    """Return the dynamic or static icon for a sensor key."""
    if key == "next_order_status":
        next_order = data.next_order
        if next_order is None or not next_order.status:
            return "mdi:package-variant-closed"

        status = next_order.status.casefold()
        if status in {"delivered", "complete", "completed"}:
            return "mdi:package-variant-closed-check"
        if status in {"in_transit", "out_for_delivery", "shipped"}:
            return "mdi:truck-delivery-outline"
        if status in {"cancelled", "canceled", "skipped"}:
            return "mdi:package-variant-closed-remove"
        return "mdi:package-variant-closed"

    if key == "shipment_tracking_status":
        tracked_order = data.tracked_order
        if tracked_order is None or not tracked_order.tracking_status:
            return "mdi:truck-remove-outline"

        status = tracked_order.tracking_status.casefold()
        if status in {"delivered", "complete", "completed"}:
            return "mdi:package-check"
        if status in {"in_transit", "out_for_delivery", "shipped"}:
            return "mdi:truck-delivery-outline"
        if status in {"exception", "failed", "delivery_exception", "delayed"}:
            return "mdi:truck-alert-outline"
        return "mdi:truck-outline"

    return default_icon


def sensor_extra_state_attributes(
    key: str,
    data: HelloFreshAccountData,
) -> dict[str, object] | None:
    """Return extra state attributes for a sensor key."""
    if key == "next_order_status":
        next_order = data.next_order
        if next_order is None:
            return None
        next_week = data.get_week(next_order.week_id) if next_order.week_id else None
        return {
            "order_id": next_order.order_id,
            "week_id": next_order.week_id,
            "delivery_date": (
                next_order.delivery_date.isoformat() if next_order.delivery_date else None
            ),
            "tracking_url": next_order.tracking_url,
            "total_price": next_order.total_price,
            "currency": next_order.currency,
            "tracking_number": next_order.tracking_number,
            "tracking_status": next_order.tracking_status,
            "carrier": next_order.carrier,
            "week": next_week.as_summary_dict() if next_week else None,
        }

    if key in {"shipment_tracking_status", "shipment_tracking_number"}:
        tracked_order = data.tracked_order
        if tracked_order is None:
            return {"tracked_order_available": False}
        # `order` is the single tracked order (small); the full `orders` list was dropped
        # to stay under the recorder's 16 KB cap. `tracking_url` is read by the dashboard.
        return {
            "tracked_order_available": True,
            "order": tracked_order.as_dict(),
            "tracking_url": tracked_order.tracking_url,
        }

    if key == "weeks_needing_selection":
        return {"weeks": data.summarized_weeks_needing_selection}

    if key in WEEK_ATTRIBUTE_KEYS:
        next_configurable_week = data.next_configurable_week
        attributes: dict[str, object] = {
            "next_selection_week": (
                next_configurable_week.as_summary_dict() if next_configurable_week else None
            ),
        }
        # The per-week list is exposed ONLY on the deadline sensor, which the example
        # dashboard's per-week table reads. It uses recipe-free summaries: the table reads
        # only scalar week metadata, and the full recipe catalog would blow the recorder's
        # 16 KB per-state attribute cap (a single week's menu already exceeds it).
        if key == "next_selection_deadline":
            attributes["weeks"] = data.summarized_weeks_needing_selection
        return attributes

    if key == "delivery_count_this_week":
        return {"order_count": len(data.orders)}

    if key in ORDER_ATTRIBUTE_KEYS:
        next_order = data.next_order
        next_week = data.get_week(next_order.week_id) if next_order and next_order.week_id else None
        # `order`/`week` are single objects; the full `orders` list was dropped to stay
        # under the recorder's 16 KB attribute cap and is unused by any consumer. The week
        # uses its recipe-free summary form for the same reason — a single week's recipe
        # catalog alone can exceed the cap, and no consumer reads recipes from attributes.
        return {
            "order": next_order.as_dict() if next_order else None,
            "week": next_week.as_summary_dict() if next_week else None,
        }

    if key == "public_menu_recipe_count":
        current_menu = data.current_public_menu
        # Both `public_menu_weeks` and the full `current_menu` recipe list were dropped:
        # a menu's worth of recipes is by far the largest payload and exceeds the 16 KB
        # recorder cap on its own. Expose a count and the menu's identifying fields only.
        return {
            "account_data_available": data.account_data_available,
            "capabilities": data.capabilities.as_dict(),
            "available_menu_labels": data.available_menu_labels,
            "current_menu_recipe_count": (
                len(current_menu.recipes) if current_menu else 0
            ),
        }

    if key in SUBSCRIPTION_CONTEXT_KEYS:
        # Summary only. The full serialized subscriptions/orders/weeks blobs were
        # dropped here: they pushed these sensors past the recorder's 16 KB attribute
        # cap and no consumer (dashboard or intent) reads them.
        return {
            "account_data_available": data.account_data_available,
            "capabilities": data.capabilities.as_dict(),
            "subscription_count": data.subscription_count,
            "order_count": len(data.orders),
        }

    if key in DELIVERY_HISTORY_KEYS:
        return {
            "account_data_available": data.account_data_available,
            "boxes_received": data.boxes_received,
            "last_delivery_week": (
                data.last_delivery_week.as_summary_dict() if data.last_delivery_week else None
            ),
        }

    if key == "api_base_url":
        return {
            "capabilities": data.capabilities.as_dict(),
            "subscriptions": data.serialized_subscriptions,
        }

    return None
