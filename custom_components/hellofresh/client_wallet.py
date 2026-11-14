"""Account credit and the wallet's discounts on upcoming weeks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any

from .client_core import _ClientCore, _dict_at
from .const import api_country_code, api_locale
from .models import HelloFreshAccountData, HelloFreshSubscription, HelloFreshWeek
from .parsers import _seg, coerce_float, coerce_int, local_today, parse_datetime


class WalletClientMixin(_ClientCore):
    """Credit balance and wallet benefits; mixed into HelloFreshClient."""

    async def _async_enrich_account_credit(
        self,
        data: HelloFreshAccountData,
        subscriptions: Sequence[HelloFreshSubscription],
    ) -> None:
        """Read the account credit balance that applies to the next order, and its parts.

        Source: ``/gw/payments/customers/{customerUUID}/balance``. The response's
        ``amount`` is the spendable credit the website surfaces as "$X that will apply
        automatically to your next order" (unless the account is in HelloFresh's manual
        credit-application experiment); ``currencyCode`` gives the unit. Beside it are the
        HelloFresh Cash the customer bought (``cash``), the bonus credit (``bonus``), the
        restricted part (``restrictedAmount``) and the credits that can still be cancelled
        (``cancellableCredits``), all in minor units like ``amount``.
        """
        if self._session is None:
            return
        customer_uuid = self._customer_uuid(subscriptions)
        if not customer_uuid:
            return

        path = f"/gw/payments/customers/{_seg(customer_uuid)}/balance"
        params = {
            "business_unit": api_country_code(self._country),
            "country": api_country_code(self._country),
        }
        payload = await self._async_get_json(
            path, params, trace_category="payment_attempts", optional=True
        )
        if payload is None:
            return

        amount = coerce_float(payload.get("amount"))
        if amount is None:
            return
        # The payments gateway reports amounts in minor units (cents), like priceInCents
        # elsewhere; convert to major units for the sensor (8992 -> 89.92).
        data.account_credit = amount / 100
        currency = payload.get("currencyCode")
        data.account_credit_currency = currency if isinstance(currency, str) and currency else None
        # The parts are read one by one: an account whose balance leaves one out (HelloFresh's
        # Android app models restrictedAmount as optional and has no cancellableCredits) still
        # gets the others.
        for name, key in (
            ("account_credit_cash", "cash"),
            ("account_credit_bonus", "bonus"),
            ("account_credit_restricted", "restrictedAmount"),
            ("account_credit_cancellable", "cancellableCredits"),
        ):
            part = coerce_float(payload.get(key))
            setattr(data, name, part / 100 if part is not None else None)

        self._record_debug_attempt(
            "payment_attempts",
            {
                "path": path,
                "account_credit": data.account_credit,
                "amount_raw": amount,
                "currency": data.account_credit_currency,
            },
        )

    _WALLET_BENEFITS_PATH = "/gw/customer-wallet/v2/benefit-distribution"
    # What a promise's ``applicableTo`` means in words, for the human label.
    _BENEFIT_TARGETS = {
        "premiumSurcharge": "premium meals",
        "premium_surcharge": "premium meals",
        "shipping": "shipping",
        "mealbox": "your box",
        "box": "your box",
        "addons": "add-ons",
        "addon": "add-ons",
        "freeAddons": "free add-ons",
        "externalProduct": "a partner product",
    }
    # Unit statuses that mean the discount is on the week: ``applied`` (chosen for it, see
    # HelloFresh's Android app's ``UnitStatus``) before ``available``; ``inactive`` is not.
    _ACTIVE_UNIT_STATUSES = ("applied", "available")
    _CURRENCY_SYMBOLS = {
        "USD": "$",
        "CAD": "$",
        "AUD": "$",
        "NZD": "$",
        "EUR": "€",
        "GBP": "£",
    }

    @classmethod
    def _benefit_label(
        cls,
        applies_to: str | None,
        amount: float | None,
        amount_type: str | None,
        currency: str | None,
    ) -> str | None:
        """Word a promise the way the site does: "$10 off premium meals", "15% off shipping"."""
        target = cls._BENEFIT_TARGETS.get(applies_to or "")
        if target is None and applies_to:
            target = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(applies_to)).replace("_", " ").lower()
        if amount is None:
            return f"Voucher for {target}" if target else None
        if amount_type in ("percent", "percentage"):
            amount_text = f"{amount:g}%"
        else:
            symbol = cls._CURRENCY_SYMBOLS.get((currency or "").upper())
            number = f"{amount:g}" if float(amount).is_integer() else f"{amount:.2f}"
            amount_text = f"{symbol}{number}" if symbol else f"{number} {currency}".strip()
        return f"{amount_text} off {target}" if target else f"{amount_text} off"

    @staticmethod
    def _wallet_budget_amount(benefit: Mapping[str, Any]) -> tuple[float | None, str | None]:
        """Return a benefit's (amount, budget type) in major units or percent.

        ``budget.value`` is in hundredths: minor units for ``fixed`` and hundredths of a percent
        for ``percent``/``percentage`` (HelloFresh's Android app divides both by 100), so 1000 is
        $10 and 1500 is 15%.
        """
        budget = _dict_at(benefit, "budget")
        raw_value = coerce_float(budget.get("value"))
        return (round(raw_value / 100, 2) if raw_value is not None else None), budget.get("type")

    @classmethod
    def _parse_wallet_promises(
        cls, payload: Mapping[str, Any], currency: str | None
    ) -> dict[str, dict[str, Any]]:
        """Reduce the wallet's ``promises`` to the fields worth keeping, keyed by promise id.

        Observed shape (captures 37-51): ``{id, voucherCode, source, expirationDateTime,
        attachmentDateTime, unlimited, type, campaign: {type}, units: [{box, benefits:
        [{id, applicableTo, budget: {type: "fixed", value: 1000}}], used}]}``. Each unit is one
        box of the promise with its own budget (a "3 boxes" promise has three); a week's amount
        is the unit for that week's ``box``, as in HelloFresh's Android app (``WeekDiscountMapper``).
        The promise-level amount is its first unused box's. Only a single-box fixed
        premium-surcharge voucher has been captured; other kinds label themselves generically.
        The per-box amounts are kept under ``_boxes`` for ``_async_enrich_wallet_benefits`` and
        are not exposed.
        """
        promises: dict[str, dict[str, Any]] = {}
        for raw in payload.get("promises") or []:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            units = [unit for unit in raw.get("units") or [] if isinstance(unit, dict)]
            boxes: dict[int, dict[str, Any]] = {}
            first: dict[str, Any] | None = None
            first_unused: dict[str, Any] | None = None
            for position, unit in enumerate(units, start=1):
                benefit = next((b for b in unit.get("benefits") or [] if isinstance(b, dict)), None)
                if benefit is None:
                    continue
                amount, amount_type = cls._wallet_budget_amount(benefit)
                entry = {
                    "amount": amount,
                    "amount_type": amount_type,
                    "applies_to": benefit.get("applicableTo") or raw.get("type"),
                }
                boxes[coerce_int(unit.get("box")) or position] = entry
                first = first or entry
                if first_unused is None and not unit.get("used"):
                    first_unused = entry
            chosen = first_unused or first or {"amount": None, "amount_type": None}
            applies_to = chosen.get("applies_to") or raw.get("type")
            campaign = _dict_at(raw, "campaign")
            expires_at = parse_datetime(raw.get("expirationDateTime"))
            attached_at = parse_datetime(raw.get("attachmentDateTime"))
            unlimited = bool(raw.get("unlimited"))
            promises[str(raw["id"])] = {
                "promise_id": str(raw["id"]),
                "voucher_code": raw.get("voucherCode") or None,
                "type": raw.get("type"),
                "applies_to": applies_to,
                "amount": chosen["amount"],
                "amount_type": chosen["amount_type"],
                "currency": currency,
                "label": cls._benefit_label(
                    applies_to, chosen["amount"], chosen["amount_type"], currency
                ),
                "one_time": not unlimited and len(units) <= 1,
                "boxes": None if unlimited else len(units),
                "boxes_used": sum(1 for unit in units if unit.get("used")),
                "expires_at": expires_at.isoformat() if expires_at else None,
                "attached_at": attached_at.isoformat() if attached_at else None,
                "source": raw.get("source"),
                "campaign": campaign.get("type"),
                "weeks": [],
                "_boxes": boxes,
            }
        return promises

    async def _async_enrich_wallet_benefits(
        self,
        data: HelloFreshAccountData,
        subscriptions: Sequence[HelloFreshSubscription],
        weeks: Sequence[HelloFreshWeek],
    ) -> None:
        """Attach the weekly discounts (wallet promises) to their weeks and to ``data``.

        HAR-verified (capture 62, October 2026): the deliveries page POSTs every upcoming week
        to ``/gw/customer-wallet/v2/benefit-distribution?externalPartnerBenefits=true&
        subscription={id}`` (header ``x-requested-by: deliveries``) as ``{"deliveries":
        [{"delivery": {"hfWeek", "state", "cutoffDateTime"}, "selections": {"mealboxHandle"},
        "subscriptionId"}], "planId", "legacyVoucherCode", "legacyVoucherShippedDiscountedBoxes",
        "systemCountry", "locale"}`` and gets back, per week, the promises available for it
        (``deliveries[].units[].{promiseId, status}``) plus the promise definitions. Without the
        top-level fields (the August captures 37-51 had none) HelloFresh answers HTTP 400
        "validation failed". A promise (e.g. "$10 off premium meals", unlimited, expiring at a
        cutoff) is a discount the billing ledger later realizes as a coupon line — the
        cart-pricing call never shows it. Best-effort per subscription; a failure leaves the
        sensor unknown.
        """
        if self._session is None or not subscriptions:
            return
        today = local_today()
        currency_hint = (
            data.next_delivery_total_currency
            or data.selected_plan_total_price_currency
            or data.account_credit_currency
        )
        all_promises: dict[str, dict[str, Any]] = {}
        for subscription in subscriptions:
            sub_id = subscription.subscription_id
            raw_subscription = subscription.raw if isinstance(subscription.raw, dict) else {}
            sub_weeks = [
                week
                for week in weeks
                if week.delivery_date is not None
                and week.delivery_date >= today
                and week.subscription_id in (None, sub_id)
                and isinstance(week.raw, dict)
                and week.raw
            ]
            if not sub_weeks:
                continue
            # Every upcoming week with a cutoff and a box, as the website sends them (W43-W51 in
            # capture 62, past the last published menu too).
            deliveries: list[dict[str, Any]] = []
            for upcoming in sub_weeks:
                raw = upcoming.raw
                cutoff = raw.get("cutoffDate") or raw.get("cutoffDateTime")
                product = _dict_at(raw, "product")
                handle = product.get("handle") or upcoming.box_size
                if not cutoff or not handle:
                    continue
                deliveries.append(
                    {
                        "delivery": {
                            "hfWeek": upcoming.week_id,
                            "state": raw.get("state") or raw.get("status") or "RUNNING",
                            "cutoffDateTime": cutoff,
                        },
                        "selections": {"mealboxHandle": handle},
                        "subscriptionId": int(sub_id) if str(sub_id).isdigit() else sub_id,
                    }
                )
            if not deliveries:
                continue
            body = {
                "deliveries": deliveries,
                "planId": raw_subscription.get("customerPlanId"),
                # Null in capture 62, for a subscription without a coupon. What fills them
                # otherwise isn't known.
                "legacyVoucherCode": None,
                "legacyVoucherShippedDiscountedBoxes": None,
                "systemCountry": api_country_code(self._country),
                "locale": subscription.locale or api_locale(self._country),
            }
            params = {"externalPartnerBenefits": "true", "subscription": sub_id}
            # A refused body goes in the trace, so diagnostics show what was sent
            # (``subscriptionId`` is redacted by key name).
            payload = await self._async_get_json(
                self._WALLET_BENEFITS_PATH,
                params,
                trace_category="wallet_attempts",
                method="POST",
                json_payload=body,
                extra_headers={"x-requested-by": "deliveries"},
                trace={"json_payload": body},
                optional=True,
            )
            if payload is None:
                continue
            currency = currency_hint or getattr(subscription, "currency", None)
            promises = self._parse_wallet_promises(payload, currency)
            weeks_by_id = {week.week_id: week for week in sub_weeks}
            weeks_with_benefits = 0
            for entry in payload.get("deliveries") or []:
                if not isinstance(entry, dict):
                    continue
                week = weeks_by_id.get(str(entry.get("hfWeek")))
                if week is None:
                    continue
                attached: list[dict[str, Any]] = []
                for unit in entry.get("units") or []:
                    if not isinstance(unit, dict):
                        continue
                    promise = promises.get(str(unit.get("promiseId")))
                    if promise is None:
                        continue
                    status = unit.get("status")
                    summary = {
                        k: v for k, v in promise.items() if k != "weeks" and not k.startswith("_")
                    }
                    summary["status"] = status
                    # The week's own box of the promise (unit ``box``, 1 when absent).
                    box = promise["_boxes"].get(coerce_int(unit.get("box")) or 1)
                    if box is not None and box["amount"] != promise["amount"]:
                        summary["amount"] = box["amount"]
                        summary["amount_type"] = box["amount_type"]
                        summary["label"] = self._benefit_label(
                            box["applies_to"], box["amount"], box["amount_type"], currency
                        )
                    attached.append(summary)
                    if (
                        status in self._ACTIVE_UNIT_STATUSES
                        and week.week_id not in promise["weeks"]
                    ):
                        promise["weeks"].append(week.week_id)
                if attached:
                    week.benefits = attached
                    weeks_with_benefits += 1
            all_promises.update(promises)
            self._record_debug_attempt(
                "wallet_attempts",
                {
                    "path": self._WALLET_BENEFITS_PATH,
                    "weeks_sent": len(deliveries),
                    "promises": len(promises),
                    "weeks_with_benefits": weeks_with_benefits,
                },
            )

        data.wallet_benefits = [
            {k: v for k, v in promise.items() if not k.startswith("_")}
            for promise in all_promises.values()
        ]
        # The next box's discount: the earliest upcoming week that will actually ship and has an
        # active promise (the same "next box" the price sensors describe). A promise ``applied``
        # to the week is its discount; otherwise the first ``available`` one.
        candidates = sorted(
            (
                week
                for week in weeks
                if week.delivery_date is not None
                and week.delivery_date >= today
                and not week.is_skipped
                and any(b.get("status") in self._ACTIVE_UNIT_STATUSES for b in week.benefits)
            ),
            key=lambda week: (week.delivery_date, week.week_id),
        )
        if candidates:
            next_week = candidates[0]
            benefit = min(
                (b for b in next_week.benefits if b.get("status") in self._ACTIVE_UNIT_STATUSES),
                key=lambda b: self._ACTIVE_UNIT_STATUSES.index(b["status"]),
            )
            data.next_box_discount = {**benefit, "week_id": next_week.week_id}
