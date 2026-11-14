"""Carrier tracking for recent boxes, and the delivery-day watch that re-reads shipment
state between polls.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from .client_core import _ClientCore
from .const import api_country_code, api_locale
from .models import HelloFreshAccountData, HelloFreshOrder, HelloFreshSubscription, HelloFreshWeek
from .parsers import (
    _seg,
    extract_scm_tracking_details,
    extract_tracking_public_id,
    local_today,
    parse_date,
    parse_datetime,
)

# Carrier tracking is looked up for boxes delivered at most this many days ago.
_TRACKING_LOOKBACK_DAYS = 3


class TrackingClientMixin(_ClientCore):
    """Shipment tracking reads; mixed into HelloFreshClient."""

    async def async_refresh_delivery_status(self, data: HelloFreshAccountData) -> bool:
        """Re-read only shipment state and overlay it onto ``data`` in place.

        The delivery-day fast poll: one ranged deliveries GET per subscription (the payload
        that carries each week's lifecycle ``state`` and, once delivered, the carrier's
        ``tracking.delivery_date``) plus the carrier tracking lookup for orders with a
        tracking id. No menus, history, pricing or profile — a few requests, not the full
        poll. Week and order objects are updated in place so every derived view
        (``last_delivery_week``, ``tracked_order``, the calendar) follows after
        ``finalize()``. Returns True when anything changed; raises ``HelloFreshAuthError``
        so the coordinator can start reauth, and lets other ``HelloFreshError``s through for
        the caller to log.
        """
        if self._session is None or not data.subscriptions:
            return False
        # The per-poll shared-GET cache would otherwise hand back the previous poll's
        # deliveries payload.
        self._shared_get_tasks = {}
        results = await self._async_gather_or_cancel(
            *(
                self._async_get_upcoming_deliveries(subscription)
                for subscription in data.subscriptions
            )
        )

        fresh_weeks: dict[tuple[str, str | None], HelloFreshWeek] = {}
        # By (subscription, order id): an order without an id of its own is named after its
        # week, which two subscriptions share, and by the id alone one subscription's tracking
        # was laid over the other's order.
        fresh_orders: dict[tuple[str | None, str], HelloFreshOrder] = {}
        for weeks, orders in results:
            for week in weeks:
                fresh_weeks[(week.week_id, week.subscription_id)] = week
            for order in orders:
                fresh_orders[(order.subscription_id, order.order_id)] = order

        # The box's day moving (DELAYED/EARLY) and its payment failing are part of its state on
        # delivery day too, and are what the box_delayed/box_early events and the payment
        # warning follow.
        week_fields = (
            "status",
            "is_skipped",
            "delivered_at",
            "delivery_state",
            "sub_status",
            "revised_delivery_date",
            "payment_failed",
        )
        order_fields = ("status", "tracking_url", "tracking_number", "tracking_status", "carrier")

        def _state() -> list[tuple[Any, ...]]:
            return [
                *(tuple(getattr(week, name) for name in week_fields) for week in data.weeks),
                *(
                    (*(getattr(order, name) for name in order_fields), order.estimated_delivery)
                    for order in data.orders
                ),
            ]

        before = _state()
        changed = False
        for week in data.weeks:
            fresh = fresh_weeks.get((week.week_id, week.subscription_id))
            if fresh is None:
                continue
            for name in week_fields:
                setattr(week, name, getattr(fresh, name))
            # The cards read the carrier node straight off the raw week; keep it current.
            if isinstance(week.raw, dict) and isinstance(fresh.raw, dict):
                tracking = fresh.raw.get("tracking")
                if tracking is not None and week.raw.get("tracking") != tracking:
                    week.raw["tracking"] = tracking
                    changed = True

        for order in data.orders:
            fresh_order = fresh_orders.get((order.subscription_id, order.order_id))
            if fresh_order is None:
                continue
            for name in order_fields:
                value = getattr(fresh_order, name)
                if value is not None:
                    setattr(order, name, value)

        await self._async_enrich_order_tracking(data.subscriptions, data.orders)
        # Compared only after this: a box HelloFresh calls delivered too early comes back
        # DELIVERED from every re-read, which must not count as a change each time.
        data.reconcile_premature_deliveries()
        if _state() != before:
            changed = True

        if changed:
            data.finalize()
        self._record_debug_attempt(
            "delivery_attempts",
            {"context": "delivery_watch", "changed": changed, "weeks_seen": len(fresh_weeks)},
        )
        return changed

    async def _async_enrich_order_tracking(
        self,
        subscriptions: Sequence[HelloFreshSubscription],
        orders: Sequence[HelloFreshOrder],
    ) -> None:
        """Overlay richer SCM shipment tracking fields onto known orders.

        A box delivered more than ``_TRACKING_LOOKBACK_DAYS`` ago keeps what it has: its
        carrier history is final, and it was looked up again on every poll and every
        delivery-day tick.
        """
        orders_by_public_id: dict[str, list[HelloFreshOrder]] = {}
        locale = next(
            (
                subscription.locale
                for subscription in subscriptions
                if isinstance(subscription.locale, str) and subscription.locale.strip()
            ),
            api_locale(self._country),
        )
        oldest = local_today() - timedelta(days=_TRACKING_LOOKBACK_DAYS)

        for order in orders:
            if order.delivery_date is not None and order.delivery_date < oldest:
                continue
            public_id = extract_tracking_public_id(order.tracking_url)
            if public_id is None:
                continue
            orders_by_public_id.setdefault(public_id, []).append(order)

        for public_id, matched_orders in orders_by_public_id.items():
            path = f"/gw/scm/tracking-ids/track/public-id/{_seg(public_id)}"
            params = {
                "country": api_country_code(self._country),
                "locale": locale,
            }
            payload = await self._async_get_json(
                path,
                params,
                trace_category="tracking_attempts",
                extra_headers={"x-requested-by": "shipping-and-tracking"},
                trace={"public_id": public_id, "params": params},
                optional=True,
            )
            if payload is None:
                continue

            boxes = payload.get("boxes") or []
            applied_count = 0
            if isinstance(boxes, list):
                applied_count = self._apply_tracking_boxes_to_orders(
                    orders=matched_orders,
                    boxes=boxes,
                )

            self._record_debug_attempt(
                "tracking_attempts",
                {
                    "public_id": public_id,
                    "path": path,
                    "params": params,
                    "payload_summary": self._summarize_payload(payload),
                    "box_count": len(boxes) if isinstance(boxes, list) else 0,
                    "matched_order_count": len(matched_orders),
                    "updated_order_count": applied_count,
                },
            )

    def _apply_tracking_boxes_to_orders(
        self,
        orders: Sequence[HelloFreshOrder],
        boxes: Sequence[dict[str, Any]],
    ) -> int:
        """Merge SCM tracking box details into normalized orders."""
        updates = 0
        for order in orders:
            box = self._select_tracking_box_for_order(order, boxes)
            if box is None:
                continue
            details = extract_scm_tracking_details(box)
            if details["tracking_url"] is not None:
                order.tracking_url = details["tracking_url"]
            if details["tracking_number"] is not None:
                order.tracking_number = details["tracking_number"]
            if details["tracking_status"] is not None:
                order.tracking_status = details["tracking_status"]
            if details["carrier"] is not None:
                order.carrier = details["carrier"]
            if details["estimated_delivery"] is not None:
                # Parsed here rather than in the parser so the parser stays string-in/string-out
                # like its siblings; an unparseable value simply leaves the field unset.
                estimated = parse_datetime(details["estimated_delivery"])
                if estimated is not None:
                    order.estimated_delivery = estimated
            if details["tracking_status_detail"] is not None:
                order.tracking_status_detail = details["tracking_status_detail"]
            if details["tracking_events"]:
                order.tracking_events = details["tracking_events"]
            if details["delivery_photo_urls"]:
                order.delivery_photo_urls = details["delivery_photo_urls"]
            if details["delivery_signed_by"] is not None:
                order.delivery_signed_by = details["delivery_signed_by"]
            updates += 1
        return updates

    def _select_tracking_box_for_order(
        self,
        order: HelloFreshOrder,
        boxes: Sequence[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Return the best matching SCM tracking box for an order."""
        if not boxes:
            return None

        matching_delivery_date = [
            box for box in boxes if parse_date(box.get("delivery_date")) == order.delivery_date
        ]
        if len(matching_delivery_date) == 1:
            return matching_delivery_date[0]
        if matching_delivery_date:
            boxes = matching_delivery_date

        if order.tracking_number:
            normalized_tracking_number = order.tracking_number.strip()
            for box in boxes:
                tracking_number = box.get("tracking_code") or box.get("tracking_id")
                if (
                    isinstance(tracking_number, str)
                    and tracking_number.strip() == normalized_tracking_number
                ):
                    return box

        return boxes[0] if boxes else None
