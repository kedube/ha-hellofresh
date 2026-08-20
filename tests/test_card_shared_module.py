"""Tests for the shared card helper module (`hellofresh-shared.js`).

This module is the single source of truth for helpers that were previously hand-copied into up
to seven card files. Each behaviour asserted here corresponds to a bug that shipped because one
copy drifted from the others, so these are regression tests as much as unit tests.

The module is real ESM, so it is imported directly under Node rather than having its functions
lifted out of a card with a regex.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

WWW = Path(__file__).resolve().parents[1] / "custom_components" / "hellofresh" / "www"
SHARED = WWW / "hellofresh-shared.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _eval(expression_body: str) -> object:
    """Import the shared module under Node and evaluate an expression against it."""
    script = f"""
    import * as S from {json.dumps(SHARED.as_uri())};
    // A minimal window/localStorage so the sync helpers are exercisable off-browser.
    const store = new Map();
    globalThis.window = {{
      localStorage: {{
        getItem: (k) => (store.has(k) ? store.get(k) : null),
        setItem: (k, v) => store.set(k, String(v)),
      }},
      dispatchEvent: (ev) => {{ (globalThis.__events ||= []).push(ev); return true; }},
    }};
    globalThis.CustomEvent = class {{
      constructor(type, init) {{ this.type = type; this.detail = (init || {{}}).detail; }}
    }};
    console.log(JSON.stringify((() => {{ {expression_body} }})()));
    """
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)


def test_esc_neutralizes_html_injection() -> None:
    """Card markup is built from template strings, so payload text must be escaped."""
    out = _eval("return S.esc('<img src=x onerror=\"alert(1)\">');")
    assert "<" not in out and ">" not in out
    assert "&lt;" in out and "&quot;" in out


def test_esc_renders_absent_values_as_empty() -> None:
    assert _eval("return [S.esc(null), S.esc(undefined), S.esc(0)];") == ["", "", "0"]


def test_safe_url_rejects_non_http_schemes() -> None:
    """A `javascript:` href from a payload must never reach the DOM."""
    out = _eval(
        'return [S.safeUrl("javascript:alert(1)"), S.safeUrl("data:text/html,x"),'
        ' S.safeUrl("  https://hellofresh.com/x  ")];'
    )
    assert out[0] == "" and out[1] == ""
    assert out[2].startswith("https://hellofresh.com")


def test_parse_local_date_does_not_shift_bare_dates_west_of_utc() -> None:
    """`new Date("2026-08-20")` is UTC midnight and reads a day early west of UTC."""
    out = _eval(
        'const d = S.parseLocalDate("2026-08-20");'
        "return [d.getFullYear(), d.getMonth() + 1, d.getDate()];"
    )
    assert out == [2026, 8, 20]


def test_title_case_lowercases_screaming_snake() -> None:
    """HelloFresh sends SCREAMING_SNAKE; without .toLowerCase() this stays shouting."""
    out = _eval(
        'return [S.titleCase("ON_THE_WAY"), S.titleCase("DELIVERED"),'
        ' S.titleCase("out_for_delivery"), S.titleCase(null)];'
    )
    assert out == ["On The Way", "Delivered", "Out For Delivery", ""]


def test_fmt_price_keeps_the_currency_in_its_fallback() -> None:
    """An unknown ISO code must not silently render as a bare number."""
    out = _eval('return [S.fmtPrice(12.5, "USD"), S.fmtPrice(12.5, "XYZ"), S.fmtPrice("n/a")];')
    assert "12.5" in out[0].replace(",", "") or "12.50" in out[0]
    assert "XYZ" in out[1]
    assert out[2] == "n/a"


def test_fmt_date_guards_invalid_dates() -> None:
    """toLocaleDateString on an Invalid Date returns "Invalid Date" without throwing."""
    out = _eval('return [S.fmtDate(null), S.fmtDate(""), S.fmtDate("not-a-date")];')
    assert out == ["—", "—", "—"]


def test_is_editable_requires_meal_swap() -> None:
    """A locked week, and a delivered week with no allowed_actions, are not editable."""
    out = _eval(
        "return ["
        "S.isEditable({allowed_actions:{mealSwap:false}}),"
        "S.isEditable({}),"
        "S.isEditable(null),"
        'S.isEditable({allowed_actions:{mealSwap:true}, selection_deadline:"2099-01-01T00:00:00+00:00"}),'
        "S.isEditable({allowed_actions:{mealSwap:true}, is_skipped:true}),"
        'S.isEditable({allowed_actions:{mealSwap:true}, selection_deadline:"2000-01-01T00:00:00+00:00"})'
        "];"
    )
    assert out == [False, False, False, True, False, False]


def test_is_past_ignores_undated_weeks() -> None:
    """An undated week is unscheduled, not historical."""
    out = _eval(
        'return [S.isPast({}), S.isPast({delivery_date:"2000-01-01"}),'
        ' S.isPast({delivery_date:"2099-01-01"})];'
    )
    assert out == [False, True, False]


def test_account_key_defaults_to_the_shared_sentinel() -> None:
    """Both sides of the filter must agree on "default" or single-account setups break."""
    out = _eval(
        'return [S.accountKey(null), S.accountKey({}), S.accountKey({config_entry_id:"abc"})];'
    )
    assert out == ["default", "default", "abc"]


def test_broadcasts_always_carry_the_account_key() -> None:
    """The reported bug: an empty detail is dropped by every listener, silently."""
    out = _eval(
        "globalThis.__events = [];"
        'S.broadcastDataChanged({config_entry_id:"abc"});'
        'S.broadcastWeek({config_entry_id:"abc"}, "2026-W20");'
        "return globalThis.__events.map((e) => [e.type, e.detail.accountKey]);"
    )
    assert out == [
        ["hellofresh-data-changed", "abc"],
        ["hellofresh-week-selected", "abc"],
    ]


def test_event_matches_account_pairs_with_the_broadcasts() -> None:
    """Round-trip: what broadcastDataChanged emits must pass eventMatchesAccount."""
    out = _eval(
        "globalThis.__events = [];"
        'const cfg = {config_entry_id:"abc"};'
        "S.broadcastDataChanged(cfg);"
        "const ev = globalThis.__events[0];"
        "return [S.eventMatchesAccount(ev.detail, cfg),"
        ' S.eventMatchesAccount(ev.detail, {config_entry_id:"other"}),'
        " S.eventMatchesAccount({}, {})];"
    )
    assert out == [True, False, True]


def test_week_sync_storage_key_is_account_scoped() -> None:
    """Two accounts on one dashboard must not share a selected week."""
    out = _eval('return [S.syncStorageKey({}), S.syncStorageKey({config_entry_id:"abc"})];')
    assert out == ["hellofresh:selected-week:default", "hellofresh:selected-week:abc"]


def test_broadcast_week_round_trips_through_storage() -> None:
    """A card mounting later reads the selection from storage, not the live event."""
    out = _eval(
        'const cfg = {config_entry_id:"abc"};'
        'S.broadcastWeek(cfg, "2026-W20");'
        "return S.loadSyncedWeekId(cfg);"
    )
    assert out == "2026-W20"
