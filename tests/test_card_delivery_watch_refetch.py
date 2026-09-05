"""Cards follow the delivery-day watch, and surface the new payment / price data.

Runs the real method bodies under Node: the shared ``refetchIntervalMs`` helper (the cadence
the schedule and subscription cards re-pull on), the schedule card's next-box discount row,
and the subscription card's payment banner and Card-on-file / Shipping / Discount cells.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

WWW = Path(__file__).resolve().parents[1] / "custom_components" / "hellofresh" / "www"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SHARED = (WWW / "hellofresh-shared.js").read_text(encoding="utf-8")
SCHEDULE = (WWW / "hellofresh-schedule-card.js").read_text(encoding="utf-8")
SUBSCRIPTION = (WWW / "hellofresh-subscription-card.js").read_text(encoding="utf-8")


def _fn(source: str, name: str) -> str:
    match = re.search(rf"^export function {name}\(.*?^\}}", source, re.S | re.M)
    assert match, name
    return match.group(0).replace("export function", "function")


def _method(source: str, name: str) -> str:
    match = re.search(rf"^  {name}\(.*?^  \}}", source, re.S | re.M)
    assert match, name
    return match.group(0)


def _node(script: str) -> dict:
    result = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(result.stdout)


def test_refetch_interval_drops_to_the_watch_cadence_only_while_a_box_is_in_progress() -> None:
    out = _node(f"""
    {_fn(SHARED, "refetchIntervalMs")}
    console.log(JSON.stringify({{
      idle: refetchIntervalMs({{refresh_interval_minutes: 180, delivery_watch_interval_minutes: 15, delivery_in_progress: false}}),
      due: refetchIntervalMs({{refresh_interval_minutes: 180, delivery_watch_interval_minutes: 15, delivery_in_progress: true}}),
      watchOff: refetchIntervalMs({{refresh_interval_minutes: 180, delivery_watch_interval_minutes: 0, delivery_in_progress: true}}),
      fasterPoll: refetchIntervalMs({{refresh_interval_minutes: 5, delivery_watch_interval_minutes: 15, delivery_in_progress: true}}),
      missing: refetchIntervalMs(null),
    }}));
    """)
    minute = 60000
    assert out == {
        "idle": 180 * minute,
        "due": 15 * minute,
        "watchOff": 180 * minute,
        "fasterPoll": 5 * minute,
        "missing": 180 * minute,
    }


def test_both_cards_delegate_their_cadence_to_the_shared_helper() -> None:
    for source in (SCHEDULE, SUBSCRIPTION):
        assert "refetchIntervalMs," in source  # imported
        assert re.search(r"_refetchIntervalMs\(\) \{\n    return refetchIntervalMs\(", source)


def test_schedule_card_shows_a_discount_only_when_one_applies() -> None:
    out = _node(f"""
    class Card {{
      constructor(account) {{ this._account = account; }}
      _fmtPrice(amount, currency) {{ return `${{currency || "?"}} ${{amount.toFixed(2)}}`; }}
      {_method(SCHEDULE, "_nextBoxDiscount")}
    }}
    console.log(JSON.stringify({{
      applied: new Card({{next_delivery_price_breakdown: {{discount_amount: 12.5}}, next_delivery_total_currency: "USD"}})._nextBoxDiscount(),
      zero: new Card({{next_delivery_price_breakdown: {{discount_amount: 0}}}})._nextBoxDiscount(),
      none: new Card({{}})._nextBoxDiscount(),
      noAccount: new Card(null)._nextBoxDiscount(),
    }}));
    """)
    assert out == {"applied": "USD 12.50", "zero": None, "none": None, "noAccount": None}


def _subscription_card(summary: dict) -> str:
    return f"""
    class Card {{
      constructor(summary) {{ this._summary = summary; }}
      _esc(v) {{ return String(v); }}
      _fmtPrice(amount, currency) {{ return `${{currency || "?"}} ${{Number(amount).toFixed(2)}}`; }}
      {_method(SUBSCRIPTION, "_renderPaymentBanner")}
      {_method(SUBSCRIPTION, "_cardOnFile")}
      {_method(SUBSCRIPTION, "_fmtCardExpiry")}
      {_method(SUBSCRIPTION, "_breakdownPrice")}
      {_method(SUBSCRIPTION, "_cardOnFileCell")}
    }}
    const card = new Card({json.dumps(summary)});
    console.log(JSON.stringify({{
      banner: card._renderPaymentBanner(),
      cell: card._cardOnFileCell(),
      shipping: card._breakdownPrice("shipping_amount", false),
      discount: card._breakdownPrice("discount_amount", true),
    }}));
    """


def test_subscription_card_payment_banner_and_cells() -> None:
    healthy = _node(
        _subscription_card(
            {
                "payment_method_expiring": False,
                "payment_method_expired": False,
                "payment_card_type": "credit_card",
                "payment_card_expiry": "2027-05",
                "selected_plan_total_price_currency": "USD",
                "selected_plan_price_breakdown": {"shipping_amount": 10.99, "discount_amount": 0},
            }
        )
    )
    assert healthy["banner"] == ""
    assert healthy["cell"].startswith("Credit card · exp. ")
    assert "2027" in healthy["cell"]
    assert healthy["shipping"] == "USD 10.99"
    assert healthy["discount"] is None  # a zero discount is not a row

    expiring = _node(
        _subscription_card(
            {
                "payment_method_expiring": True,
                "payment_method_expired": False,
                "payment_card_type": "credit_card",
                "payment_card_expiry": "2026-10",
                "selected_plan_total_price_currency": "USD",
                "selected_plan_price_breakdown": {"shipping_amount": 10.99, "discount_amount": 20},
            }
        )
    )
    assert "expires soon" in expiring["banner"]
    assert 'class="banner"' in expiring["banner"]
    assert expiring["cell"].endswith("· expiring")
    assert expiring["discount"] == "−USD 20.00"

    expired = _node(
        _subscription_card(
            {
                "payment_method_expiring": False,
                "payment_method_expired": True,
                "payment_card_type": None,
            }
        )
    )
    assert "has expired" in expired["banner"]
    assert 'class="banner danger"' in expired["banner"]
    assert expired["cell"] is None  # unknown card type: no cell, banner still shown
    assert expired["shipping"] is None


def test_subscription_card_never_renders_card_number_or_address_fields() -> None:
    """The summary carries type/expiry only; the card must not reach for anything else."""
    for forbidden in ("payment_card_number", "billing_address", "last_four", "card_number"):
        assert forbidden not in SUBSCRIPTION
