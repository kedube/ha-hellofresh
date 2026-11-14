"""Billing: the orders ledger and what it adds up to (payment dates, prices, spending,
failed charges), and the card on file.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
import re
from time import monotonic
from typing import Any

from .client_core import _LOGGER, _ClientCore
from .const import api_country_code, api_locale
from .models import HelloFreshAccountData, HelloFreshOrder, HelloFreshSubscription
from .parsers import coerce_float, coerce_int, local_today, parse_date, parse_datetime

# The spending view reuses the orders ledger the last poll read when it is this fresh.
_LEDGER_TTL = 3600.0


class BillingClientMixin(_ClientCore):
    """Billing reads; mixed into HelloFreshClient."""

    async def async_get_spending(self) -> dict[str, Any]:
        """Return the customer's HelloFresh spending ledger from the billing history.

        Fetches ``/gw/api/customers/me/orders`` (up to ~200 past+upcoming charges) and reuses the
        same per-delivery total accumulation that backs the payment sensors, so the numbers agree
        with what those sensors report. Aggregates into three views for a running-cost card:

        * ``weeks``   — one entry per delivered/charged box (delivery date, amount, currency),
          newest first, spanning the full available history rather than the schedule window.
        * ``months``  — per-calendar-month rollup (``month`` = ``YYYY-MM``, box count, total).
        * ``total``   — the running total across all *past* deliveries (the lifetime spend),
          plus ``currency`` and ``box_count``.

        Upcoming (not-yet-delivered) deliveries are reported in ``weeks``/``months`` flagged
        ``upcoming: true`` but are EXCLUDED from the running ``total`` — a running cost is money
        already spent. Returns empty structures (not an error) when the billing endpoint is
        unavailable, so the card degrades gracefully.

        The ledger the last poll read (about 1 MB) is reused while it is under an hour old.
        ``truncated`` is True when HelloFresh counts more orders than the one request returns,
        so the oldest spending is missing from the totals.
        """
        empty: dict[str, Any] = {"weeks": [], "months": [], "total": None, "truncated": False}
        subscriptions = self._cached_subscriptions or await self._async_get_subscriptions()
        if not subscriptions:
            return empty

        ledger = self._orders_ledger
        if ledger is None or monotonic() - ledger[0] >= _LEDGER_TTL:
            payload = await self._async_get_json(
                self._ORDERS_PATH,
                self._orders_params(subscriptions),
                trace_category="payment_attempts",
                optional=True,
            )
            if payload is None:
                return empty
            self._remember_orders_ledger(payload)
            ledger = self._orders_ledger
        if ledger is None:
            return empty
        _stored_at, price_by_key, discount_by_key, truncated = ledger
        return {
            **self._build_spending_dataset(price_by_key, discount_by_key),
            "truncated": truncated,
        }

    _ORDERS_PATH = "/gw/api/customers/me/orders"

    def _orders_params(self, subscriptions: Sequence[HelloFreshSubscription]) -> dict[str, Any]:
        """Query of the orders-ledger request, as the website sends it."""
        return {
            # The live site sends UPPERCASE country here (HAR: country=US), matching every
            # other /gw call. An earlier lowercase guess worked only because the US property
            # is lenient; a stricter region (see uk→GB) can reject country=us.
            "country": api_country_code(self._country),
            "locale": subscriptions[0].locale or api_locale(self._country),
            "limit": 200,
        }

    def _remember_orders_ledger(
        self,
        payload: Mapping[str, Any],
        accumulated: tuple[Any, ...] | None = None,
    ) -> None:
        """Keep what an orders-ledger answer adds up to, for the spending view.

        ``accumulated`` is ``_accumulate_order_prices``' result when the caller already has it.
        """
        items = payload.get("items")
        items = items if isinstance(items, list) else []
        if accumulated is None:
            accumulated = self._accumulate_order_prices(items)
        count = coerce_int(payload.get("count"))
        truncated = count is not None and count > len(items)
        if truncated:
            _LOGGER.debug(
                "HelloFresh orders ledger returned %d of %d orders; older spending is left out",
                len(items),
                count,
            )
        self._orders_ledger = (monotonic(), accumulated[3], accumulated[4], truncated)

    @staticmethod
    def _build_spending_dataset(
        price_by_key: dict[tuple[str, date], tuple[float, str | None]],
        discount_by_key: dict[tuple[str, date], tuple[float, str | None]] | None = None,
    ) -> dict[str, Any]:
        """Aggregate the per-(subscription, delivery date) billing totals into the spending views.

        Charges are summed across subscriptions per delivery date (a household with two plans that
        deliver the same day spends the combined amount that week). The running ``total`` counts
        only deliveries on or before today. ``discount_by_key`` (the realized voucher amounts per
        delivery) adds ``discount`` / ``coupon_code`` per week, ``discount`` per month and a
        running ``discount`` on the total — the amounts are already net in ``amount``.
        """
        discount_by_key = discount_by_key or {}
        # Delivery dates are local-market calendar dates; gate them with LOCAL today so a
        # US-evening poll (already past midnight UTC) does not misclassify today's box.
        today = local_today()

        # Collapse to one amount per delivery DATE (summing across subscriptions), preferring the
        # first non-null currency seen — a single account never mixes currencies.
        by_date: dict[date, tuple[float, str | None]] = {}
        for (_subscription_id, delivery_date), (amount, currency) in price_by_key.items():
            if delivery_date is None or amount is None:
                continue
            existing = by_date.get(delivery_date)
            if existing is None:
                by_date[delivery_date] = (amount, currency)
            else:
                by_date[delivery_date] = (existing[0] + amount, existing[1] or currency)
        discount_by_date: dict[date, tuple[float, str | None]] = {}
        for (_subscription_id, delivery_date), (amount, code) in discount_by_key.items():
            if delivery_date is None or not amount:
                continue
            existing = discount_by_date.get(delivery_date)
            discount_by_date[delivery_date] = (
                (existing[0] if existing else 0.0) + amount,
                (existing[1] if existing else None) or code,
            )

        weeks: list[dict[str, Any]] = []
        months: dict[str, dict[str, Any]] = {}
        running_total = 0.0
        running_discount = 0.0
        running_currency: str | None = None
        running_box_count = 0

        for delivery_date in sorted(by_date, reverse=True):
            amount, currency = by_date[delivery_date]
            rounded = round(amount, 2)
            upcoming = delivery_date > today
            discount_amount, coupon_code = discount_by_date.get(delivery_date, (0.0, None))
            discount_rounded = round(discount_amount, 2)
            weeks.append(
                {
                    "delivery_date": delivery_date.isoformat(),
                    "amount": rounded,
                    "currency": currency,
                    "upcoming": upcoming,
                    "discount": discount_rounded,
                    "coupon_code": coupon_code,
                }
            )
            month_key = delivery_date.strftime("%Y-%m")
            bucket = months.get(month_key)
            if bucket is None:
                bucket = {
                    "month": month_key,
                    "amount": 0.0,
                    "currency": currency,
                    "box_count": 0,
                    "upcoming": True,
                    "discount": 0.0,
                }
                months[month_key] = bucket
            bucket["amount"] = round(bucket["amount"] + rounded, 2)
            bucket["discount"] = round(bucket["discount"] + discount_rounded, 2)
            bucket["currency"] = bucket["currency"] or currency
            bucket["box_count"] += 1
            # A month counts as fully in the past only once none of its boxes are upcoming.
            if not upcoming:
                bucket["upcoming"] = False

            if not upcoming:
                running_total += rounded
                running_discount += discount_rounded
                running_currency = running_currency or currency
                running_box_count += 1

        months_list = [months[key] for key in sorted(months, reverse=True)]
        total = (
            {
                "amount": round(running_total, 2),
                "currency": running_currency,
                "box_count": running_box_count,
                "discount": round(running_discount, 2),
            }
            if running_box_count
            else None
        )
        return {"weeks": weeks, "months": months_list, "total": total}

    # ------------------------------------------------------------------
    # Payment-date enrichment helpers
    # ------------------------------------------------------------------

    # Order-line ``paymentStatus`` values that mean no money was kept. HelloFresh's Android app
    # models paid / failed / chargeback / canceled and counts only ``paid`` as a successful
    # order (``GetLastSuccessfulOrderUseCase``).
    _UNCHARGED_PAYMENT_STATUSES = frozenset({"failed", "canceled", "cancelled", "chargeback"})

    @classmethod
    def _order_was_not_charged(cls, order_lines: list[Any]) -> bool:
        """Return True when every order line that reports a payment status was not paid.

        Such an order is not a payment: it must not date the last charge, count toward
        spending, or price a box. Lines without a status (an upcoming box not billed yet) and
        partly-paid orders still count.
        """
        statuses = [
            line["paymentStatus"].strip().lower()
            for line in order_lines
            if isinstance(line, dict) and isinstance(line.get("paymentStatus"), str)
        ]
        return bool(statuses) and all(
            status in cls._UNCHARGED_PAYMENT_STATUSES for status in statuses
        )

    # Order-line ``paymentStatus`` values that mean HelloFresh could not keep the money for a
    # box, most serious first: the bank took a payment back, or the charge failed. ``canceled``
    # is not a problem (nothing was owed), so it is not among them.
    _PROBLEM_PAYMENT_STATUSES = ("chargeback", "failed")

    def _failed_charges(self, items: list[Any]) -> list[dict[str, Any]]:
        """Return the ledger's failed and charged-back charges, one per (subscription, day).

        The billing scan below skips these orders, since no money was kept; here they are what
        is wanted. An order counts when any of its lines failed or was charged back, and the
        failed orders of one delivery are added up (a delivery can have several: the box, the
        Market, fees). Each is ``{subscription_id, delivery_date, status, amount, currency}``,
        the amount in major units like the ledger's. Which boxes are recent enough to report
        is decided when the account data is finalized.
        """
        failures: dict[tuple[str, date], dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            order_lines = item.get("orderLines")
            if not isinstance(order_lines, list) or not order_lines:
                continue
            statuses = {
                line["paymentStatus"].strip().lower()
                for line in order_lines
                if isinstance(line, dict) and isinstance(line.get("paymentStatus"), str)
            }
            status = next((s for s in self._PROBLEM_PAYMENT_STATUSES if s in statuses), None)
            if status is None:
                continue
            first_line = order_lines[0] if isinstance(order_lines[0], dict) else {}
            subscription = first_line.get("subscription")
            raw_sub_id = subscription.get("id") if isinstance(subscription, dict) else None
            delivery_date = parse_date(first_line.get("deliveryDate"))
            if raw_sub_id is None or delivery_date is None:
                continue
            key = (str(raw_sub_id), delivery_date)
            failure = failures.get(key)
            if failure is None:
                failure = failures[key] = {
                    "subscription_id": key[0],
                    "delivery_date": delivery_date,
                    "status": status,
                    "amount": None,
                    "currency": None,
                }
            elif self._PROBLEM_PAYMENT_STATUSES.index(
                status
            ) < self._PROBLEM_PAYMENT_STATUSES.index(failure["status"]):
                failure["status"] = status
            amount = coerce_float(
                item.get("grandTotal") or item.get("totalPrice") or item.get("total")
            )
            if amount is not None:
                failure["amount"] = round((failure["amount"] or 0.0) + amount, 2)
                failure["currency"] = failure["currency"] or self._extract_currency_code(item)
        return list(failures.values())

    def _accumulate_order_prices(
        self,
        items: list[Any],
    ) -> tuple[
        dict[str, datetime],  # latest_by_subscription
        dict[str, tuple[date, datetime]],  # future_by_subscription
        dict[str, str],  # next_order_nr_by_subscription
        dict[tuple[str, date], tuple[float, str | None]],  # price_by_key
        dict[tuple[str, date], tuple[float, str | None]],  # discount_by_key
    ]:
        """Scan raw billing items and return per-subscription price/date accumulators.

        ``discount_by_key`` is the realized weekly discount: the order lines' ``couponMoneyValue``
        summed per (subscription, delivery date), with the item's ``couponCode`` — HAR-verified,
        a wallet voucher shows up here as a $10 coupon line on the premium-charge order while
        the cart-pricing call reports no discount at all.
        """
        today = local_today()
        latest_by_subscription: dict[str, datetime] = {}
        future_by_subscription: dict[str, tuple[date, datetime]] = {}
        next_order_nr_by_subscription: dict[str, str] = {}
        price_by_key: dict[tuple[str, date], tuple[float, str | None]] = {}
        discount_by_key: dict[tuple[str, date], tuple[float, str | None]] = {}

        for item in items:
            if not isinstance(item, dict):
                continue
            created_at = parse_datetime(item.get("createdAt"))
            if created_at is None:
                continue

            order_lines = item.get("orderLines")
            if not isinstance(order_lines, list) or not order_lines:
                continue
            first_line = order_lines[0] if isinstance(order_lines[0], dict) else {}
            raw_sub_id = (
                (first_line.get("subscription") or {}).get("id")
                if isinstance(first_line.get("subscription"), dict)
                else None
            )
            subscription_id = str(raw_sub_id) if raw_sub_id is not None else None
            if not subscription_id:
                continue
            if self._order_was_not_charged(order_lines):
                continue

            delivery_date = parse_date(first_line.get("deliveryDate"))

            # "recent payment" = the most recent order that has actually been CHARGED, i.e.
            # whose createdAt is in the past. HelloFresh bills a box several days before its
            # delivery date, so an upcoming box can already be a real, recent charge. Filtering
            # on delivery_date (the previous behaviour) skipped that charge and reported the
            # PRIOR box instead, leaving the date ~a week behind the customer's last charge.
            if created_at.date() <= today:
                latest_existing = latest_by_subscription.get(subscription_id)
                if latest_existing is None or created_at > latest_existing:
                    latest_by_subscription[subscription_id] = created_at

            if delivery_date is None:
                continue

            grand_total = coerce_float(
                item.get("grandTotal") or item.get("totalPrice") or item.get("total")
            )
            if grand_total is not None:
                currency = self._extract_currency_code(item)
                price_key = (subscription_id, delivery_date)
                existing = price_by_key.get(price_key)
                price_by_key[price_key] = (
                    (existing[0] if existing else 0.0) + grand_total,
                    existing[1] if existing else currency,
                )
            discount = sum(
                coerce_float(line.get("couponMoneyValue")) or 0.0
                for line in order_lines
                if isinstance(line, dict)
            )
            if discount:
                coupon_code = item.get("couponCode")
                discount_key = (subscription_id, delivery_date)
                existing_discount = discount_by_key.get(discount_key)
                discount_by_key[discount_key] = (
                    (existing_discount[0] if existing_discount else 0.0) + discount,
                    (existing_discount[1] if existing_discount else None)
                    or (str(coupon_code) if coupon_code else None),
                )

            if delivery_date < today:
                continue
            future_existing = future_by_subscription.get(subscription_id)
            if future_existing is None or delivery_date < future_existing[0]:
                future_by_subscription[subscription_id] = (delivery_date, created_at)
                raw_order_nr = item.get("orderNr") or item.get("id")
                if raw_order_nr is not None:
                    next_order_nr_by_subscription[subscription_id] = str(raw_order_nr)

        return (
            latest_by_subscription,
            future_by_subscription,
            next_order_nr_by_subscription,
            price_by_key,
            discount_by_key,
        )

    def _apply_recent_payment_dates(
        self,
        subscriptions: Sequence[HelloFreshSubscription],
        latest_by_subscription: dict[str, datetime],
        future_by_subscription: dict[str, tuple[date, datetime]],
    ) -> None:
        """Write recent_payment_date and next_payment_date back onto each subscription."""
        for subscription in subscriptions:
            latest = latest_by_subscription.get(subscription.subscription_id)
            if latest is not None:
                subscription.recent_payment_date = latest.date()
            future = future_by_subscription.get(subscription.subscription_id)
            if future is not None:
                subscription.next_payment_date = future[0]

    def _compute_next_delivery_total(
        self,
        data: HelloFreshAccountData,
        future_by_subscription: dict[str, tuple[date, datetime]],
        next_order_nr_by_subscription: dict[str, str],
        price_by_key: dict[tuple[str, date], tuple[float, str | None]],
        discount_by_key: dict[tuple[str, date], tuple[float, str | None]] | None = None,
    ) -> None:
        """Sum all billing charges for the earliest upcoming delivery date and write to data."""
        if not future_by_subscription:
            return
        next_sub = min(future_by_subscription, key=lambda s: future_by_subscription[s][0])
        data.recent_order_id = next_order_nr_by_subscription.get(next_sub)

        next_date = min(d for d, _ in future_by_subscription.values())
        total = sum(v[0] for k, v in price_by_key.items() if k[1] == next_date)
        currency = next(
            (v[1] for k, v in price_by_key.items() if k[1] == next_date and v[1]),
            None,
        )
        data.next_delivery_total = round(total, 2) if total else None
        data.next_delivery_total_currency = currency
        discount = sum(v[0] for k, v in (discount_by_key or {}).items() if k[1] == next_date)
        data.next_delivery_discount = round(discount, 2) if discount else None

    def _apply_prices_to_orders(
        self,
        orders: Sequence[HelloFreshOrder],
        price_by_key: dict[tuple[str, date], tuple[float, str | None]],
        discount_by_key: dict[tuple[str, date], tuple[float, str | None]] | None = None,
    ) -> None:
        """Overlay accumulated billing totals (and realized discounts) onto matching orders."""
        for order in orders:
            if order.subscription_id is None or order.delivery_date is None:
                continue
            discount_entry = (discount_by_key or {}).get(
                (order.subscription_id, order.delivery_date)
            )
            if discount_entry is not None:
                order.discount_amount = round(discount_entry[0], 2)
                order.coupon_code = discount_entry[1]
            price_entry = price_by_key.get((order.subscription_id, order.delivery_date))
            if price_entry is None:
                continue
            grand_total, currency = price_entry
            rounded_total = round(grand_total, 2)
            order.total_price = rounded_total
            # Preserve the summed billing total separately: a later cart/calculate estimate may
            # overwrite total_price, but billed_total_price stays the authoritative charge sum
            # (matching the next_box_total_price sensor).
            order.billed_total_price = rounded_total
            if currency:
                order.currency = currency
                order.billed_total_currency = currency

    async def _async_enrich_subscription_payment_dates(
        self,
        subscriptions: Sequence[HelloFreshSubscription],
        orders: Sequence[HelloFreshOrder] | None = None,
        data: HelloFreshAccountData | None = None,
    ) -> None:
        """Overlay recent and upcoming payment dates onto subscriptions and exact prices onto orders."""
        if self._session is None or not subscriptions:
            return

        for subscription in subscriptions:
            if subscription.next_cutoff_date is not None:
                # Provisional estimate only: current US orders appear to be created
                # immediately after the weekly cutoff. This is a fallback that is
                # overwritten below by _apply_recent_payment_dates whenever the billing
                # API (/gw/api/customers/me/orders) returns an authoritative date. Do not
                # "fix" this heuristic in isolation — the billing-API path is the source
                # of truth when it succeeds.
                subscription.next_payment_date = (
                    subscription.next_cutoff_date + timedelta(seconds=1)
                ).date()

        payload = await self._async_get_json(
            self._ORDERS_PATH,
            self._orders_params(subscriptions),
            trace_category="payment_attempts",
            optional=True,
        )
        if payload is None:
            await self._async_enrich_recent_payment_from_balance_transactions(subscriptions)
            return

        items = payload.get("items")
        items = items if isinstance(items, list) else []
        accumulated = self._accumulate_order_prices(items)
        # The spending view reads the same ledger; it reuses this answer for a while.
        self._remember_orders_ledger(payload, accumulated)
        if data is not None:
            data.payment_failures = self._failed_charges(items)
        (
            latest_by_subscription,
            future_by_subscription,
            next_order_nr_by_subscription,
            price_by_key,
            discount_by_key,
        ) = accumulated

        self._apply_recent_payment_dates(
            subscriptions, latest_by_subscription, future_by_subscription
        )

        if data is not None:
            self._compute_next_delivery_total(
                data,
                future_by_subscription,
                next_order_nr_by_subscription,
                price_by_key,
                discount_by_key,
            )

        if orders:
            self._apply_prices_to_orders(orders, price_by_key, discount_by_key)

        self._record_debug_attempt(
            "payment_attempts",
            {
                "path": self._ORDERS_PATH,
                "order_prices_applied": len(price_by_key),
                # A list in subscription order, not keyed by subscription id: diagnostics
                # redact by key name, so an id used as a key was published as is.
                "subscription_payment_dates": [
                    {
                        "recent_payment_date": (
                            subscription.recent_payment_date.isoformat()
                            if subscription.recent_payment_date
                            else None
                        ),
                        "next_payment_date": (
                            subscription.next_payment_date.isoformat()
                            if subscription.next_payment_date
                            else None
                        ),
                    }
                    for subscription in subscriptions
                ],
            },
        )

        if any(subscription.recent_payment_date is not None for subscription in subscriptions):
            return

        await self._async_enrich_recent_payment_from_balance_transactions(subscriptions)

    def _customer_uuid(self, subscriptions: Sequence[HelloFreshSubscription]) -> str | None:
        """Return the customer's UUID, which the payments endpoints are keyed by.

        Read from the subscription's ``customer.uuid``, where HelloFresh's Android app takes it,
        then from the first ``uuid`` anywhere in the payload (another object's id could match).
        """
        for subscription in subscriptions:
            customer = subscription.raw.get("customer")
            uuid = customer.get("uuid") if isinstance(customer, dict) else None
            if isinstance(uuid, str) and uuid:
                return uuid
        for subscription in subscriptions:
            uuid = self._find_first_nested_value(subscription.raw, ("uuid",))
            if isinstance(uuid, str) and uuid:
                return uuid
        return None

    async def _async_enrich_recent_payment_from_balance_transactions(
        self,
        subscriptions: Sequence[HelloFreshSubscription],
    ) -> None:
        """Fallback recent-payment lookup using the account balance transactions feed."""
        customer_uuid = self._customer_uuid(subscriptions)
        if not customer_uuid:
            return

        params = {
            "customerUUID": customer_uuid,
            "types": "DEBIT",
        }
        # A bare list, unlike the other billing reads.
        payload = await self._async_get_json(
            "/gw/payments/balance/transactions",
            params,
            trace_category="payment_attempts",
            expect=list,
            trace={"params": params},
            optional=True,
        )
        if payload is None:
            return

        created_dates = [
            created_at.date()
            for item in payload
            if isinstance(item, dict)
            and str(item.get("transactionType") or "").upper() == "DEBIT"
            and (created_at := parse_datetime(item.get("createdAt"))) is not None
        ]
        if not created_dates:
            return

        latest_date = max(created_dates)
        for subscription in subscriptions:
            if subscription.recent_payment_date is None:
                subscription.recent_payment_date = latest_date

        self._record_debug_attempt(
            "payment_attempts",
            {
                "path": "/gw/payments/balance/transactions",
                "params": params,
                "recent_payment_date": latest_date.isoformat(),
            },
        )

    async def _async_enrich_payment_method_status(self, data: HelloFreshAccountData) -> None:
        """Read whether the card on file is expiring, for the payment-method binary sensor.

        HAR-verified: the site POSTs an empty body to
        ``/gw/payments/v1/checktokenstatus?country=US`` (header ``x-requested-by: gateway``)
        and gets ``{isTokenExpiring, isTokenExpired, primaryToken: {type, provider, method,
        details: {expiry_month, expiry_year, number}, billing_address: {...}}}``. Only the two
        flags, the card type/provider/brand, the expiry month and the last four digits are
        kept (``number`` is already just the suffix, but only its trailing four digits survive
        regardless); the billing address is dropped here and never reaches attributes or
        diagnostics, and the digits are redacted from diagnostics exports.
        Best-effort: a failure leaves the sensor unavailable rather than failing the poll.
        """
        if self._session is None:
            return
        path = "/gw/payments/v1/checktokenstatus"
        params = {"country": api_country_code(self._country)}
        payload = await self._async_get_json(
            path,
            params,
            trace_category="payment_attempts",
            method="POST",
            extra_headers={"x-requested-by": "gateway"},
            optional=True,
        )
        if payload is None:
            return

        expiring = payload.get("isTokenExpiring")
        expired = payload.get("isTokenExpired")
        if not isinstance(expiring, bool) and not isinstance(expired, bool):
            return
        data.payment_method_expiring = bool(expiring)
        data.payment_method_expired = bool(expired)

        token = payload.get("primaryToken")
        if isinstance(token, dict):
            card_type = token.get("type")
            provider = token.get("provider")
            data.payment_card_type = card_type if isinstance(card_type, str) and card_type else None
            data.payment_card_provider = (
                provider if isinstance(provider, str) and provider else None
            )
            brand = token.get("method")
            data.payment_card_brand = brand if isinstance(brand, str) and brand else None
            details = token.get("details")
            if isinstance(details, dict):
                month = coerce_int(details.get("expiry_month"))
                year = coerce_int(details.get("expiry_year"))
                if month is not None and year is not None and 1 <= month <= 12:
                    if year < 100:
                        year += 2000
                    data.payment_card_expiry = f"{year:04d}-{month:02d}"
                number = details.get("number")
                if isinstance(number, (str, int)):
                    digits = re.sub(r"\D", "", str(number))
                    data.payment_card_last4 = digits[-4:] if len(digits) >= 4 else None

        # The debug trace deliberately omits the last four digits.
        self._record_debug_attempt(
            "payment_attempts",
            {
                "path": path,
                "is_token_expiring": data.payment_method_expiring,
                "is_token_expired": data.payment_method_expired,
                "card_type": data.payment_card_type,
                "card_brand": data.payment_card_brand,
                "card_expiry": data.payment_card_expiry,
            },
        )
