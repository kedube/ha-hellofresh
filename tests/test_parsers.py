"""Unit tests for the pure parsing helpers in custom_components.hellofresh.parsers."""

from __future__ import annotations

import base64
from datetime import date, datetime
import json

from custom_components.hellofresh.parsers import (
    MAX_SEARCH_DEPTH,
    coerce_float,
    coerce_int,
    date_from_iso_week,
    decode_jwt_claims,
    extract_allowed_actions,
    extract_menu_labels,
    extract_name_list,
    extract_scm_tracking_details,
    extract_tracking_details,
    extract_tracking_public_id,
    find_nested_collection,
    iso_week_label,
    looks_like_recipe_collection,
    looks_like_recipe_heading,
    normalize_candidate_dict_list,
    normalize_carrier_name,
    parse_date,
    parse_datetime,
    slugify,
)

# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------


def _make_jwt(claims: dict) -> str:
    """Build an unsigned-but-well-formed JWT string for the given claims."""

    def _segment(data: dict) -> str:
        raw = json.dumps(data).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{_segment({'alg': 'RS256', 'typ': 'JWT'})}.{_segment(claims)}.signature"


def test_decode_jwt_claims_reads_payload() -> None:
    token = _make_jwt({"iat": 1781271373, "exp": 1781273173, "sub": "user"})
    claims = decode_jwt_claims(token)
    assert claims == {"iat": 1781271373, "exp": 1781273173, "sub": "user"}


def test_decode_jwt_claims_handles_padding_variations() -> None:
    # Vary claim size so the base64 payload needs different amounts of padding.
    for filler in ("a", "ab", "abc", "abcd"):
        token = _make_jwt({"exp": 1781273173, "pad": filler})
        assert decode_jwt_claims(token) == {"exp": 1781273173, "pad": filler}


def test_decode_jwt_claims_rejects_malformed_values() -> None:
    assert decode_jwt_claims(None) is None
    assert decode_jwt_claims("") is None
    assert decode_jwt_claims("not-a-jwt") is None
    assert decode_jwt_claims("only.two") is None
    assert decode_jwt_claims("a.!!!notbase64!!!.c") is None


def test_parse_date_handles_iso_and_zulu_and_plain() -> None:
    assert parse_date("2026-06-15") == date(2026, 6, 15)
    assert parse_date("2026-06-15T00:00:00Z") == date(2026, 6, 15)
    assert parse_date("not-a-date") is None
    assert parse_date(None) is None
    assert parse_date(12345) is None


def test_parse_datetime_handles_zulu_suffix() -> None:
    parsed = parse_datetime("2026-06-15T08:30:00Z")
    assert isinstance(parsed, datetime)
    assert parsed.year == 2026 and parsed.hour == 8
    assert parse_datetime("garbage") is None
    assert parse_datetime(None) is None


def test_iso_week_label_returns_week_identifier() -> None:
    # A valid ISO week id passes through (normalized to YYYY-Www).
    assert iso_week_label("2026-W25") == "2026-W25"
    # Derived from the delivery date's ISO week (Fri 2026-06-19 is in ISO week 2026-W25).
    assert iso_week_label(None, date(2026, 6, 19)) == "2026-W25"
    # Unparseable / out-of-range week id falls back to the date anchor's ISO week.
    assert iso_week_label("garbage", date(2026, 6, 19)) == "2026-W25"
    assert iso_week_label("2026-W99", date(2026, 6, 19)) == "2026-W25"
    # ISO-week year can differ from the calendar year near year boundaries.
    assert iso_week_label(None, date(2026, 12, 31)) == "2026-W53"
    assert iso_week_label(None, date(2027, 1, 1)) == "2026-W53"
    # Nothing usable -> None.
    assert iso_week_label(None, None) is None


def test_date_from_iso_week_returns_monday_of_week() -> None:
    # ISO week 2026-W27's Monday is Jun 29, 2026 — matches HelloFresh's UI label.
    assert date_from_iso_week("2026-W27") == date(2026, 6, 29)
    assert date_from_iso_week("2026-W26") == date(2026, 6, 22)
    # Non-week / out-of-range / None inputs return None.
    assert date_from_iso_week("garbage") is None
    assert date_from_iso_week("2026-W99") is None
    assert date_from_iso_week(None) is None
    assert iso_week_label("garbage", None) is None


def test_coerce_int_and_float() -> None:
    assert coerce_int("42") == 42
    assert coerce_int(42.9) == 42
    assert coerce_int(None) is None
    assert coerce_int("x") is None
    assert coerce_float("3.5") == 3.5
    assert coerce_float(None) is None
    assert coerce_float("x") is None


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------


def test_slugify_normalizes_and_truncates() -> None:
    assert slugify("One-Pan Cantina Shrimp!") == "one-pan-cantina-shrimp"
    assert len(slugify("x" * 200)) == 120


def test_looks_like_recipe_heading_filters_navigation() -> None:
    assert looks_like_recipe_heading("Southwest Steak Fajita Tacos") is True
    assert looks_like_recipe_heading("Our Plans") is False  # disallowed (case-insensitive)
    assert looks_like_recipe_heading("new") is False
    assert looks_like_recipe_heading("abc") is False  # too short
    assert looks_like_recipe_heading("12345 ALLCAPS") is False  # no lowercase / not alpha start


def test_extract_menu_labels_dedupes_and_caps() -> None:
    text = "Menu for Jun 15 - Jun 21 ... Jun 15-21 ... Jun 15-21"
    labels = extract_menu_labels(text)
    assert labels  # at least one label parsed
    assert len(labels) == len(set(labels))  # no duplicates
    assert len(labels) <= 6


# ---------------------------------------------------------------------------
# Recipe / week field parsers
# ---------------------------------------------------------------------------


def test_extract_name_list_handles_strings_and_objects() -> None:
    assert extract_name_list("Solo") == ["Solo"]
    assert extract_name_list(["A", "A", "B"]) == ["A", "B"]
    assert extract_name_list([{"name": "X"}, {"title": "Y"}, {"label": "Z"}]) == ["X", "Y", "Z"]
    assert extract_name_list(None) == []
    assert extract_name_list(42) == []


def test_extract_allowed_actions_coerces_to_bool() -> None:
    result = extract_allowed_actions({"allowedActions": {"mealSwap": 1, "pause": 0, 5: True}})
    assert result == {"mealSwap": True, "pause": False}
    assert extract_allowed_actions({}) == {}
    assert extract_allowed_actions({"allowedActions": "nope"}) == {}


def test_normalize_candidate_dict_list_filters_non_dicts() -> None:
    assert normalize_candidate_dict_list([{"a": 1}, "x", 3, {"b": 2}]) == [{"a": 1}, {"b": 2}]
    assert normalize_candidate_dict_list("not-a-list") == []


def test_looks_like_recipe_collection_detects_recipe_shapes() -> None:
    assert looks_like_recipe_collection([{"name": "Tacos", "id": "1"}]) is True
    assert looks_like_recipe_collection([{"recipe": {"title": "X", "headline": "h"}}]) is True
    assert looks_like_recipe_collection([{"unrelated": "value"}]) is False
    assert looks_like_recipe_collection([]) is False


# ---------------------------------------------------------------------------
# Tracking parsers
# ---------------------------------------------------------------------------


def test_normalize_carrier_name_maps_known_codes() -> None:
    assert normalize_carrier_name("DDASH") == "DoorDash"
    assert normalize_carrier_name("fedex") == "FedEx"
    assert normalize_carrier_name("SomeCarrier") == "SomeCarrier"
    assert normalize_carrier_name("  ") is None
    assert normalize_carrier_name(None) is None


def test_extract_tracking_details_picks_first_present_field() -> None:
    details = extract_tracking_details(
        {
            "trackingUrl": "https://track.example/abc",
            "trackingNumber": "TN-1",
            "trackingStatus": "shipped",
            "carrier": "UPS",
        }
    )
    assert details["tracking_url"] == "https://track.example/abc"
    assert details["tracking_number"] == "TN-1"
    assert details["tracking_status"] == "shipped"
    assert details["carrier"] == "UPS"


def test_extract_tracking_public_id_parses_uuid_from_path() -> None:
    url = "https://www.hellofresh.com/delivery-tracking/fb7efa96-d168-4f19-a465-0a4098d15a1e"
    assert extract_tracking_public_id(url) == "fb7efa96-d168-4f19-a465-0a4098d15a1e"
    assert extract_tracking_public_id("https://example.com/other") is None
    assert extract_tracking_public_id(None) is None


def test_extract_scm_tracking_prefers_external_status() -> None:
    details = extract_scm_tracking_details(
        {
            "carrier": "DDASH",
            "tracking_code": "TRACK123",
            "last_status": {"status": "in_transit", "internal_status": "transit"},
        }
    )
    assert details["tracking_status"] == "in_transit"
    assert details["carrier"] == "DoorDash"
    assert details["tracking_number"] == "TRACK123"


def test_extract_scm_tracking_reads_out_for_delivery() -> None:
    """The 'out for delivery' carrier status (SCM feed) is surfaced as tracking_status.

    Confirmed from live data: the box lifecycle `state` stays ON_THE_WAY while the SCM
    tracking box reports out_for_delivery under last_status.status / internal_status.
    """
    details = extract_scm_tracking_details(
        {
            "carrier": "OnTrac",
            "tracking_code": "TRK",
            "internal_status": "out_for_delivery",
            "last_status": {
                "status": "out_for_delivery",
                "internal_status": "out_for_delivery",
            },
        }
    )
    assert details["tracking_status"] == "out_for_delivery"


# ---------------------------------------------------------------------------
# find_nested_collection (unified recursive walker)
# ---------------------------------------------------------------------------


def _has_id(items: list[dict]) -> bool:
    """Predicate: a collection whose first item carries an 'id' key."""
    return bool(items) and "id" in items[0]


def test_find_nested_collection_finds_via_priority_key() -> None:
    payload = {"weeks": [{"id": "w1"}, {"id": "w2"}]}
    assert find_nested_collection(payload, ("weeks", "items"), _has_id) == [
        {"id": "w1"},
        {"id": "w2"},
    ]


def test_find_nested_collection_recurses_into_values() -> None:
    # Target is not under a priority key; the walker must fall through to dict values.
    payload = {"wrapper": {"deep": {"weeks": [{"id": "x"}]}}}
    assert find_nested_collection(payload, ("weeks",), _has_id) == [{"id": "x"}]


def test_find_nested_collection_returns_none_when_predicate_never_matches() -> None:
    payload = {"weeks": [{"name": "no-id"}]}
    assert find_nested_collection(payload, ("weeks",), _has_id) is None


def test_find_nested_collection_respects_depth_cap() -> None:
    # Build a chain deeper than MAX_SEARCH_DEPTH; the collection sits below the cap.
    node: dict = {"id": "deep-target"}
    payload: dict = {"items": [node]}
    for _ in range(MAX_SEARCH_DEPTH + 2):
        payload = {"wrapper": payload}
    assert find_nested_collection(payload, ("items",), _has_id) is None


def test_find_nested_collection_dict_first_vs_list_first_ordering() -> None:
    # A node that is a dict containing both a keyed list AND list-shaped values.
    # dict_first should prefer the keyed branch; list_first prefers a bare list branch.
    payload = {
        "primary": [{"id": "from-dict-branch"}],
    }
    # dict_first (default): the 'primary' priority key wins.
    assert find_nested_collection(payload, ("primary",), _has_id) == [{"id": "from-dict-branch"}]

    bare_list = [{"id": "from-list"}]
    # list_first: a top-level list is evaluated as a candidate before dict descent.
    assert find_nested_collection(bare_list, ("primary",), _has_id, dict_first=False) == [
        {"id": "from-list"}
    ]
