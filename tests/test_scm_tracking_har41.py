"""Contract tests for the SCM shipment-tracking response (HAR capture 41).

`GET /gw/scm/tracking-ids/track/public-id/{public_id}` was the integration's longest-standing
evidence gap: it appeared in no capture at all, yet it is the **only** possible source of
`sensor.tracked_shipment_carrier` — the delivery payload carries no carrier field whatsoever
(see tests/test_tracking_payload_shape.py). Until capture 41 nobody had confirmed the endpoint
still existed, let alone what it returned, so the carrier sensor was effectively unverifiable.

Capture 41 answers it: the endpoint returns `{"boxes": [...]}`, and a box **does** carry a real
`carrier` value, a carrier-hosted tracking URL, and a full status history. The fixture below is
a verbatim box from that capture (its `statuses` list trimmed to three entries for readability).

This also pins the round trip that makes the lookup possible at all: the delivery payload's
`tracking_link` yields a public id, which is exactly the id this request is keyed on.
"""

from __future__ import annotations

from custom_components.hellofresh.parsers import (
    extract_scm_tracking_details,
    extract_tracking_public_id,
    normalize_carrier_name,
    parse_date,
)

# Verbatim from HAR 41.
SCM_BOX = {
    "external_id": "H4234151270",
    "tracking_id": "",
    "country": "us",
    "carrier": "VEHO",
    "delivery_date": "2026-08-17T12:00:00Z",
    "packing_date": None,
    "tracking_code": "HF01000042767022",
    "weight": 14,
    "lane": "NJ_VEHO-SOMPA",
    "signed_by": "",
    "public_url": "https://track.shipveho.com/#/trackingId/HF01000042767022",
    "carrier_tracking_url": "https://track.shipveho.com/#/trackingId/HF01000042767022",
    "hf_tracking_url": "https://www.hellofresh.com/delivery-tracking/711f9257-5782-4d5a-a96b-3cc195bdc3e5",
    "proof_of_delivery_photo_urls": None,
    "internal_status": "delivered",
    "internal_status_detail": "delivered",
    "last_status": {
        "status": "delivered",
        "status_detail": "delivered",
        "internal_status": "delivered",
        "internal_status_detail": "delivered",
        "datetime": "2026-08-17T22:53:06Z",
        "origin": "tracking-create-endpoint",
        "message": "package.delivered",
        "source": "SCANDATA",
        "created_at": "2026-08-17T22:59:37.880638Z",
        "updated_at": None,
        "est_delivery_time": "2026-08-17T00:00:00Z",
        "sent": True,
        "sent_at": "2026-08-17T22:59:41.079956Z",
        "processed_at": None,
        "tracking_id": ""
    },
    "statuses": [
        {
            "status": "delivered",
            "status_detail": "delivered",
            "internal_status": "delivered",
            "internal_status_detail": "delivered",
            "datetime": "2026-08-17T22:53:06Z",
            "origin": "tracking-create-endpoint",
            "message": "package.delivered",
            "source": "SCANDATA",
            "created_at": "2026-08-17T22:59:37.880638Z",
            "updated_at": None,
            "est_delivery_time": "2026-08-17T00:00:00Z",
            "sent": True,
            "sent_at": "2026-08-17T22:59:41.079956Z",
            "processed_at": None,
            "tracking_id": ""
        },
        {
            "status": "out_for_delivery",
            "status_detail": "out_for_delivery",
            "internal_status": "out_for_delivery",
            "internal_status_detail": "out_for_delivery",
            "datetime": "2026-08-17T15:02:56Z",
            "origin": "tracking-create-endpoint",
            "message": "package.pickedUpFromVeho",
            "source": "SCANDATA",
            "created_at": "2026-08-17T15:11:01.712755Z",
            "updated_at": None,
            "est_delivery_time": "2026-08-17T00:00:00Z",
            "sent": True,
            "sent_at": "2026-08-17T15:11:05.380586Z",
            "processed_at": None,
            "tracking_id": ""
        },
        {
            "status": "in_transit",
            "status_detail": "received_at_origin_facility",
            "internal_status": "transit",
            "internal_status_detail": "received_at_origin_facility",
            "datetime": "2026-08-17T08:37:17Z",
            "origin": "tracking-create-endpoint",
            "message": "package.droppedOffAtVeho",
            "source": "SCANDATA",
            "created_at": "2026-08-17T08:40:00.130113Z",
            "updated_at": None,
            "est_delivery_time": "2026-08-17T00:00:00Z",
            "sent": True,
            "sent_at": "2026-08-17T08:40:03.573568Z",
            "processed_at": None,
            "tracking_id": ""
        }
    ]
}

PUBLIC_ID = "711f9257-5782-4d5a-a96b-3cc195bdc3e5"


def test_carrier_is_obtainable_from_the_scm_response() -> None:
    """The whole reason this endpoint matters: it is the only carrier source that exists."""
    details = extract_scm_tracking_details(SCM_BOX)
    assert details["carrier"] == "Veho"


def test_veho_is_mapped_to_its_brand_name() -> None:
    """The API shouts "VEHO"; the company brands itself "Veho"."""
    assert normalize_carrier_name("VEHO") == "Veho"
    assert normalize_carrier_name("veho") == "Veho"


def test_unknown_carriers_pass_through_unchanged() -> None:
    """Guards the over-correction: an unmapped carrier must still reach the user."""
    assert normalize_carrier_name("SOMETHING-NEW") == "SOMETHING-NEW"
    assert normalize_carrier_name("") is None
    assert normalize_carrier_name(None) is None


def test_all_four_tracking_fields_extract_from_a_real_box() -> None:
    """Every field the order model takes from SCM, against real data."""
    details = extract_scm_tracking_details(SCM_BOX)
    assert details["tracking_number"] == "HF01000042767022"
    assert details["tracking_status"] == "delivered"
    assert details["carrier"] == "Veho"
    # The carrier's own page is preferred over HelloFresh's wrapper.
    assert details["tracking_url"] == "https://track.shipveho.com/#/trackingId/HF01000042767022"


def test_tracking_number_prefers_the_code_over_the_empty_id() -> None:
    """`tracking_id` is "" here, exactly as in the delivery payload."""
    assert SCM_BOX["tracking_id"] == ""
    assert extract_scm_tracking_details(SCM_BOX)["tracking_number"] == "HF01000042767022"


def test_status_comes_from_last_status_not_the_history() -> None:
    """`last_status` is the current state; `statuses` is the (newest-first) history."""
    assert SCM_BOX["last_status"]["status"] == "delivered"
    assert extract_scm_tracking_details(SCM_BOX)["tracking_status"] == "delivered"


def test_public_id_round_trips_from_the_boxs_own_hf_url() -> None:
    """The delivery payload's tracking link is what keys this very request."""
    assert extract_tracking_public_id(SCM_BOX["hf_tracking_url"]) == PUBLIC_ID


def test_box_delivery_date_parses_for_order_matching() -> None:
    """`_select_tracking_box_for_order` matches on this date; an unparseable one drops it."""
    assert str(parse_date(SCM_BOX["delivery_date"])) == "2026-08-17"


def test_a_box_missing_optional_fields_does_not_raise() -> None:
    """Not every box is delivered; partial boxes must degrade, not explode."""
    details = extract_scm_tracking_details({"tracking_code": "X1", "carrier": "UPS"})
    assert details["tracking_number"] == "X1"
    assert details["carrier"] == "UPS"
    assert details["tracking_url"] is None
    assert details["tracking_status"] is None
