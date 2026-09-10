"""Cards show the weekly discount: the schedule card's voucher badge / row, the planner's
header label, and the cost card's realized savings.

Runs the real method bodies under Node against the shapes the integration emits (a week's
``benefits`` list, the account payload's ``next_box_discount`` and the spending ledger's
``discount`` fields).
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

SCHEDULE = (WWW / "hellofresh-schedule-card.js").read_text(encoding="utf-8")
PLANNER = (WWW / "hellofresh-meal-planner-card.js").read_text(encoding="utf-8")
COST = (WWW / "hellofresh-cost-card.js").read_text(encoding="utf-8")

BENEFIT = {
    "label": "$10 off premium meals",
    "status": "available",
    "voucher_code": "RX-9BNR6",
    "expires_at": "2026-09-09T23:59:59-07:00",
    "one_time": False,
}


def _method(source: str, name: str) -> str:
    match = re.search(rf"^  {name}\(.*?^  \}}", source, re.S | re.M)
    assert match, name
    return match.group(0)


def _node(script: str) -> dict:
    result = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(result.stdout)


def test_schedule_card_voucher_row_and_badge() -> None:
    out = _node(f"""
    class Card {{
      constructor(account) {{ this._account = account; }}
      _esc(v) {{ return String(v); }}
      _fmtDate(d) {{ return new Date(d).toISOString().slice(0, 10); }}
      {_method(SCHEDULE, "_weekBenefit")}
      {_method(SCHEDULE, "_nextBoxVoucher")}
    }}
    const card = new Card({{ next_box_discount: {{ ...{json.dumps(BENEFIT)}, week_id: "2026-W38" }} }});
    const withBenefit = {{ week_id: "2026-W38", benefits: [{json.dumps(BENEFIT)}] }};
    const used = {{ week_id: "2026-W38", benefits: [{{ ...{json.dumps(BENEFIT)}, status: "used" }}] }};
    const bare = {{ week_id: "2026-W38" }};
    const other = {{ week_id: "2026-W40" }};
    const oneTime = {{ week_id: "2026-W41", benefits: [{{ ...{json.dumps(BENEFIT)}, one_time: true, expires_at: null }}] }};
    console.log(JSON.stringify({{
      badge: card._weekBenefit(withBenefit) && card._weekBenefit(withBenefit).label,
      usedBadge: card._weekBenefit(used),
      row: card._nextBoxVoucher(withBenefit),
      fallback: card._nextBoxVoucher(bare),
      otherWeek: card._nextBoxVoucher(other),
      oneTime: card._nextBoxVoucher(oneTime),
    }}));
    """)
    assert out["badge"] == "$10 off premium meals"
    assert out["usedBadge"] is None
    assert out["row"] == {"label": "$10 off premium meals", "note": "expires 2026-09-10"}
    # A week without its own list still gets the account's next-box promise when it names it.
    assert out["fallback"]["label"] == "$10 off premium meals"
    assert out["otherWeek"] is None
    assert out["oneTime"] == {"label": "$10 off premium meals", "note": "one-time"}
    # The row renders in the summary and the badge in the week row.
    assert 'sumlabel">Voucher</span>' in SCHEDULE
    assert 'class="badge benefit"' in SCHEDULE


def test_planner_header_label_only_for_available_promises_on_shipping_weeks() -> None:
    out = _node(f"""
    class Card {{ {_method(PLANNER, "_weekBenefitLabel")} }}
    const card = new Card();
    console.log(JSON.stringify({{
      available: card._weekBenefitLabel({{ benefits: [{json.dumps(BENEFIT)}] }}),
      skipped: card._weekBenefitLabel({{ is_skipped: true, benefits: [{json.dumps(BENEFIT)}] }}),
      used: card._weekBenefitLabel({{ benefits: [{{ ...{json.dumps(BENEFIT)}, status: "used" }}] }}),
      none: card._weekBenefitLabel({{ benefits: [] }}),
      missing: card._weekBenefitLabel({{}}),
    }}));
    """)
    assert out == {
        "available": "$10 off premium meals",
        "skipped": None,
        "used": None,
        "none": None,
        "missing": None,
    }
    assert 'class="benefit"' in PLANNER


def test_cost_card_shows_realized_savings() -> None:
    out = _node(f"""
    class Card {{
      constructor() {{ this._config = {{ weeks: 5 }}; }}
      _esc(v) {{ return String(v); }}
      _fmtDate(d) {{ return String(d); }}
      _fmtPrice(a, c) {{ return `${{c || "?"}} ${{Number(a).toFixed(2)}}`; }}
      {_method(COST, "_renderTotal")}
      {_method(COST, "_renderWeeks")}
    }}
    const card = new Card();
    console.log(JSON.stringify({{
      total: card._renderTotal({{ amount: 262.5, currency: "USD", box_count: 3, discount: 30 }}),
      totalNoSaving: card._renderTotal({{ amount: 262.5, currency: "USD", box_count: 3, discount: 0 }}),
      weeks: card._renderWeeks([
        {{ delivery_date: "2026-08-17", amount: 106.89, currency: "USD", upcoming: false, discount: 10, coupon_code: "RX-9BNR6" }},
        {{ delivery_date: "2026-08-10", amount: 90.91, currency: "USD", upcoming: false, discount: 0, coupon_code: null }},
      ]),
    }}));
    """)
    assert "USD 30.00 saved with vouchers" in out["total"]
    assert "saved" not in out["totalNoSaving"]
    assert "−USD 10.00" in out["weeks"]
    assert 'title="Voucher RX-9BNR6"' in out["weeks"]
    assert out["weeks"].count("saved") == 1
