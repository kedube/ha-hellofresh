"""Source-level guards on per-region currency formatting in the Lovelace cards.

The cards are plain JS with no JS test runner in this project, so these tests assert against the
card source. They exist because the currency bugs they cover were invisible in the US-only
default and only surfaced once non-USD regions were enabled:

* ``_fmtSurcharge`` hardcoded ``+$``. Worse, ``surcharge_label`` is HelloFresh's raw localized
  string, so a Danish "+7,99/portion" matched only "7" against a digits-and-dots regex and
  rendered as "+$7" -- wrong symbol AND a silently truncated amount.
* Market tiles priced from ``item.currency`` alone, which is frequently null; ``_fmtPrice``
  then fell through to its "USD" default and put a "$" on every tile for e.g. a Danish account.
"""

from __future__ import annotations

from pathlib import Path

WWW = Path(__file__).resolve().parents[1] / "custom_components" / "hellofresh" / "www"
MEAL_PLANNER = (WWW / "hellofresh-meal-planner-card.js").read_text(encoding="utf-8")
MARKET = (WWW / "hellofresh-market-card.js").read_text(encoding="utf-8")


def test_surcharge_formatter_takes_a_currency() -> None:
    assert "_fmtSurcharge(label, currency)" in MEAL_PLANNER
    assert "_fmtSurcharge(r.surcharge_label, currency)" in MEAL_PLANNER


def test_surcharge_does_not_hardcode_a_dollar_sign() -> None:
    """The original bug: `+$${m[1]}` regardless of region.

    Scoped to the function body -- the comment above it quotes the old broken output on purpose.
    """
    body = MEAL_PLANNER.split("_fmtSurcharge(label, currency)")[1].split("\n  }")[0]
    assert "+$" not in body


def test_surcharge_normalizes_a_decimal_comma() -> None:
    """Without this, "+7,99" parses as 7 and the cents are silently dropped."""
    assert 'replace(/,/g, ".")' in MEAL_PLANNER


def test_surcharge_currency_reads_account_field_defensively() -> None:
    """``this._account`` starts as null, so the read must be optional-chained."""
    assert "this._account?.selected_plan_total_price_currency" in MEAL_PLANNER


def test_surcharge_formatting_cannot_break_the_render() -> None:
    """An unknown currency code makes toLocaleString throw RangeError; it must be caught."""
    body = MEAL_PLANNER.split("_fmtSurcharge(label, currency)")[1].split("\n  }")[0]
    assert "try {" in body and "catch" in body


def test_market_tiles_fall_back_to_the_order_currency() -> None:
    """item.currency is often null; the week's order currency is the regional fallback."""
    assert "const fallbackCurrency = week?.order?.currency;" in MARKET
    assert "this._fmtPrice(item.price, item.currency || fallbackCurrency)" in MARKET
    assert "_renderTile(week, item, editable, qty, fallbackCurrency)" in MARKET


def test_market_total_falls_back_to_the_order_currency() -> None:
    assert "week.market_items.find((i) => i.currency) || week.order || {}" in MARKET
