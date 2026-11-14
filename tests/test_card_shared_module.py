"""Tests for the shared card helper module (`hellofresh-shared.js`).

This module is the single source of truth for helpers that were once hand-copied into the seven
classic card files (since removed). Many behaviours asserted here correspond to a bug that shipped
because one copy drifted from the others, so these are regression tests as much as unit tests.

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


def test_safe_url_returns_the_url_itself_for_esc_at_the_sink() -> None:
    """One URL check for every payload URL, with one contract: http(s) or "", returned as the URL
    and not as HTML, so a query keeps its "&" and esc() where it enters markup escapes it once.
    The characters that could end an attribute come back percent-encoded, so a sink that forgot
    its esc() still can't be broken out of. `httpsOnly` is for what loads by itself from a host
    a payload names (delivery photos): plain http would be mixed content."""
    out = _eval(
        "return ["
        'S.safeUrl("https://img.example/a.jpg?w=1&h=2"),'
        "S.safeUrl('https://x.example/\"><img src=x onerror=alert(1)>'),"
        'S.safeUrl("https://x.example/a b"),'
        'S.safeUrl("http://photos.example/p.jpg", { httpsOnly: true }),'
        'S.safeUrl("HTTPS://photos.example/p.jpg", { httpsOnly: true }),'
        "S.safeUrl(null), S.safeUrl(undefined), S.safeUrl(42),"
        'S.esc(S.safeUrl("https://img.example/a.jpg?w=1&h=2")),'
        "];"
    )
    assert out == [
        "https://img.example/a.jpg?w=1&h=2",
        "https://x.example/%22%3E%3Cimg%20src=x%20onerror=alert(1)%3E",
        "https://x.example/a%20b",
        "",
        "HTTPS://photos.example/p.jpg",
        "",
        "",
        "",
        "https://img.example/a.jpg?w=1&amp;h=2",
    ]


def test_each_helper_has_one_definition() -> None:
    """The recipe sheet kept its own HTML escape, URL check, image resizer and duration
    formatter, and the copies drifted. Each now exists once: the escape in the text layer (which
    every module imports), the rest in the shared module."""
    definitions = {
        "esc": "hellofresh-i18n.js",
        "safeUrl": "hellofresh-shared.js",
        "resizedImage": "hellofresh-shared.js",
        "formatMinutes": "hellofresh-shared.js",
    }
    for name, home in definitions.items():
        owners = [
            path.name
            for path in sorted(WWW.glob("*.js"))
            if f"function {name}(" in path.read_text(encoding="utf-8")
        ]
        assert owners == [home], (name, owners)
    for path in WWW.glob("*.js"):
        source = path.read_text(encoding="utf-8")
        assert "escapeHtml" not in source and "safeHttpUrl" not in source, path.name


def test_format_minutes_reads_durations() -> None:
    out = _eval(
        "return [S.formatMinutes(35), S.formatMinutes(65), S.formatMinutes(120),"
        " S.formatMinutes(0), S.formatMinutes(null), S.formatMinutes(undefined)];"
    )
    assert out == ["35 min", "1 h 5 min", "2 h", "0 min", "", ""]


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


def test_fmt_price_shows_a_dash_for_a_missing_amount() -> None:
    """Number(null) is 0 and String(undefined) is "undefined", so a missing amount read as a
    real "$0.00" or as the word. It is a dash; a real zero still reads as one."""
    out = _eval(
        'return [S.fmtPrice(null, "USD"), S.fmtPrice(undefined, "USD"), S.fmtPrice("", "USD"),'
        ' S.fmtPrice(0, "USD")];'
    )
    assert out[:3] == ["—", "—", "—"]
    assert "0.00" in out[3] and "$" in out[3]


def test_fmt_date_guards_invalid_dates() -> None:
    """toLocaleDateString on an Invalid Date returns "Invalid Date" without throwing."""
    out = _eval('return [S.fmtDate(null), S.fmtDate(""), S.fmtDate("not-a-date")];')
    assert out == ["—", "—", "—"]


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


def test_a_write_to_one_week_names_that_week() -> None:
    """After a save, a skip or a day change the event names the week, so sibling cards re-read
    that week alone; without one it is the event it always was (re-read everything)."""
    out = _eval(
        "globalThis.__events = [];"
        'const cfg = {config_entry_id:"abc"};'
        'S.broadcastDataChanged(cfg, "card-1", "2026-W41");'
        'S.broadcastDataChanged(cfg, "card-1");'
        "S.broadcastDataChanged(cfg);"
        "return globalThis.__events.map((e) => e.detail);"
    )
    assert out == [
        {"accountKey": "abc", "source": "card-1", "weekId": "2026-W41"},
        {"accountKey": "abc", "source": "card-1"},
        {"accountKey": "abc"},
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


def test_refetch_interval_drops_to_the_watch_cadence_only_while_a_box_is_in_progress() -> None:
    """The card re-reads on the delivery-day watch cadence only while a box is on the road."""
    out = _eval(
        "return {"
        "idle: S.refetchIntervalMs({refresh_interval_minutes: 180,"
        " delivery_watch_interval_minutes: 15, delivery_in_progress: false}),"
        "due: S.refetchIntervalMs({refresh_interval_minutes: 180,"
        " delivery_watch_interval_minutes: 15, delivery_in_progress: true}),"
        "watchOff: S.refetchIntervalMs({refresh_interval_minutes: 180,"
        " delivery_watch_interval_minutes: 0, delivery_in_progress: true}),"
        "fasterPoll: S.refetchIntervalMs({refresh_interval_minutes: 5,"
        " delivery_watch_interval_minutes: 15, delivery_in_progress: true}),"
        "missing: S.refetchIntervalMs(null),"
        "};"
    )
    minute = 60000
    assert out == {
        "idle": 180 * minute,
        "due": 15 * minute,
        "watchOff": 180 * minute,
        "fasterPoll": 5 * minute,
        "missing": 180 * minute,
    }
