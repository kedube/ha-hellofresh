"""Tests pinning the tracking parser to the payload HelloFresh actually sends.

The tracking extractor probes a wide net of plausible field names (`trackingUrl`, `carrierName`,
`parcelNumber`, …) because the real shape was originally unknown. A survey of every HAR capture
settles it: across 409 delivery weeks, 59 carried a non-null ``tracking`` node and **every one of
them had exactly these six keys**:

    tracking_id, tracking_link, tracking_code, tracking_link_type,
    estimated_delivery_time, delivery_date

Two facts follow that are easy to regress against, because both look like bugs and are not:

* **There is no carrier field.** Not one of the carrier-ish names the extractor probes has ever
  been observed, so `carrier` is legitimately None from this payload alone — it can only arrive
  from the separate SCM lookup.
* **`tracking_id` is always the empty string.** The usable identifier is `tracking_code`, so an
  extractor that preferred `tracking_id` would yield "" (falsy but present) rather than the code.

The fixtures below are verbatim tracking nodes from the captures.
"""

from __future__ import annotations

from custom_components.hellofresh.parsers import (
    extract_tracking_details,
    extract_tracking_public_id,
)

# Verbatim from HAR 26 — a UPS-style code with an hf-hosted tracking page.
REAL_UPS = {
    "tracking_id": "",
    "tracking_link": "https://www.hellofresh.com/delivery-tracking/b1f6d5e4-e88a-459f-9989-a192a2331a41",
    "tracking_code": "1Z16F8B3P200030044",
    "tracking_link_type": "hf",
    "estimated_delivery_time": "2026-06-23T14:37:34+0000",
    "delivery_date": "2026-06-23T14:37:34+0000",
}

# Verbatim from HAR 26 — HelloFresh's own courier numbering.
REAL_HF = {
    "tracking_id": "",
    "tracking_link": "https://www.hellofresh.com/delivery-tracking/70e989b0-8f86-4721-acdd-b53f77b5062c",
    "tracking_code": "HF01000041974678",
    "tracking_link_type": "hf",
    "estimated_delivery_time": "2026-06-29T22:20:50+0000",
    "delivery_date": "2026-06-29T22:20:50+0000",
}

# The observed field set, exhaustively. A payload growing a seventh key is worth noticing.
OBSERVED_KEYS = {
    "tracking_id",
    "tracking_link",
    "tracking_code",
    "tracking_link_type",
    "estimated_delivery_time",
    "delivery_date",
}


def test_real_payload_yields_url_and_number() -> None:
    """The two fields that must work: the tracking page and the code."""
    details = extract_tracking_details({"tracking": REAL_UPS})
    assert details["tracking_url"] == REAL_UPS["tracking_link"]
    assert details["tracking_number"] == "1Z16F8B3P200030044"


def test_tracking_number_comes_from_the_code_not_the_empty_id() -> None:
    """`tracking_id` is always "" in practice; preferring it would lose the real code."""
    for payload in (REAL_UPS, REAL_HF):
        details = extract_tracking_details({"tracking": payload})
        assert details["tracking_number"] == payload["tracking_code"]
        assert details["tracking_number"], "must not fall back to the empty tracking_id"


def test_carrier_is_absent_from_the_delivery_payload() -> None:
    """Documents reality: no carrier field exists here, so None is correct, not a bug.

    `sensor.tracked_shipment_carrier` can only populate via the SCM lookup. If this ever starts
    returning a value, HelloFresh added a field and the docs claiming otherwise need updating.
    """
    assert extract_tracking_details({"tracking": REAL_UPS})["carrier"] is None
    assert not (OBSERVED_KEYS & {"carrier", "carrierName", "deliveryPartner", "provider"})


def test_carrier_is_not_guessed_from_the_tracking_code_prefix() -> None:
    """"1Z…" is a UPS convention, but inferring from it would be a guess, not data."""
    assert extract_tracking_details({"tracking": REAL_UPS})["carrier"] is None
    assert extract_tracking_details({"tracking": REAL_HF})["carrier"] is None


def test_public_id_is_recovered_from_the_tracking_link() -> None:
    """The SCM enrichment path keys on this id, so the URL must parse."""
    assert (
        extract_tracking_public_id(REAL_UPS["tracking_link"])
        == "b1f6d5e4-e88a-459f-9989-a192a2331a41"
    )
    assert (
        extract_tracking_public_id(REAL_HF["tracking_link"])
        == "70e989b0-8f86-4721-acdd-b53f77b5062c"
    )


def test_a_null_tracking_node_is_not_an_error() -> None:
    """~86% of weeks have `tracking: null`, including delivered ones."""
    for payload in ({"tracking": None}, {}, {"tracking": "unexpected"}):
        details = extract_tracking_details(payload)
        assert details["tracking_url"] is None
        assert details["tracking_number"] is None
        assert details["carrier"] is None
