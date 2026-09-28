"""The SCM tracking lookup's finer status, scan history and proof of delivery.

HAR 54 (2026-09-28) captured an in-transit Veho box; HAR 41 a delivered one (its fixture lives
in tests/test_scm_tracking_har41.py). Both leave `proof_of_delivery_photo_urls` null and
`signed_by` empty, so the photo shapes below are defensive guesses, pinned so that anything
unexpected is dropped rather than rendered.
"""

from __future__ import annotations

from datetime import date

from custom_components.hellofresh.api import HelloFreshClient
from custom_components.hellofresh.models import HelloFreshAccountData, HelloFreshOrder
from custom_components.hellofresh.parsers import extract_scm_tracking_details
from custom_components.hellofresh.sensor_helpers import sensor_extra_state_attributes
from test_scm_tracking_har41 import SCM_BOX as DELIVERED_BOX


def _status(when, status, detail, internal=None, internal_detail=None, message="package.x"):
    return {
        "status": status,
        "status_detail": detail,
        "internal_status": internal or status,
        "internal_status_detail": internal_detail or detail,
        "datetime": when,
        "message": message,
        "source": "SCANDATA",
        "est_delivery_time": "2026-09-28T00:00:00Z",
    }


# HAR 54's box, identifiers replaced. Note the finer steps and the untranslated `message` keys.
IN_TRANSIT_BOX = {
    "carrier": "VEHO",
    "delivery_date": "2026-09-28T12:00:00Z",
    "tracking_code": "HF0100000000000",
    "signed_by": "",
    "carrier_tracking_url": "https://track.shipveho.com/#/trackingId/HF0100000000000",
    "proof_of_delivery_photo_urls": None,
    "internal_status": "transit",
    "internal_status_detail": "arrived_at_hub",
    "last_status": _status(
        "2026-09-28T07:56:00Z",
        "in_transit",
        "received_at_origin_facility",
        "transit",
        "arrived_at_hub",
    ),
    "statuses": [
        _status("2026-09-28T07:56:00Z", "in_transit", "received_at_origin_facility", "transit"),
        _status("2026-09-27T12:56:10Z", "in_transit", "received_at_origin_facility", "transit"),
        _status("2026-09-25T15:29:28Z", "pre_transit", "label_created"),
        _status("2026-09-25T04:49:54Z", "pre_transit", "label_created", message="Label Generated"),
    ],
}


def test_in_transit_box_reports_the_finer_step_and_history() -> None:
    details = extract_scm_tracking_details(IN_TRANSIT_BOX)

    assert details["tracking_status"] == "in_transit"
    assert details["tracking_status_detail"] == "received_at_origin_facility"
    assert details["tracking_events"] == [
        {
            "time": "2026-09-28T07:56:00+00:00",
            "status": "in_transit",
            "detail": "received_at_origin_facility",
        },
        {
            "time": "2026-09-27T12:56:10+00:00",
            "status": "in_transit",
            "detail": "received_at_origin_facility",
        },
        {"time": "2026-09-25T15:29:28+00:00", "status": "pre_transit", "detail": "label_created"},
        {"time": "2026-09-25T04:49:54+00:00", "status": "pre_transit", "detail": "label_created"},
    ]
    # Empty in every capture: no photo, and a blank signer is None rather than "".
    assert details["delivery_photo_urls"] == []
    assert details["delivery_signed_by"] is None


def test_delivered_box_from_har41() -> None:
    details = extract_scm_tracking_details(DELIVERED_BOX)

    assert details["tracking_status_detail"] == "delivered"
    assert details["tracking_events"][0]["detail"] == "delivered"
    assert [e["detail"] for e in details["tracking_events"]][:3] == [
        "delivered",
        "out_for_delivery",
        "received_at_origin_facility",
    ]
    assert details["delivery_photo_urls"] == []
    assert details["delivery_signed_by"] is None


def test_history_is_sorted_and_falls_back_to_internal_fields() -> None:
    """Order is not trusted; an internal_* twin fills a missing field; junk is dropped."""
    box = {
        "statuses": [
            {"datetime": "2026-09-25T04:00:00", "internal_status": "pre_transit"},  # no offset
            {
                "datetime": "2026-09-28T07:00:00Z",
                "status": "delivered",
                "status_detail": "delivered",
            },
            {"status": "exception"},  # no time: sorts last
            {"message": "package.x"},  # nothing usable
            "not-a-dict",
        ]
    }
    events = extract_scm_tracking_details(box)["tracking_events"]

    assert events == [
        {"time": "2026-09-28T07:00:00+00:00", "status": "delivered", "detail": "delivered"},
        {"time": "2026-09-25T04:00:00+00:00", "status": "pre_transit", "detail": None},
        {"time": None, "status": "exception", "detail": None},
    ]


def test_proof_of_delivery_accepts_plausible_shapes_and_drops_the_rest() -> None:
    def photos(value):
        return extract_scm_tracking_details({"proof_of_delivery_photo_urls": value})[
            "delivery_photo_urls"
        ]

    assert photos(["https://pod.example/1.jpg", " https://pod.example/2.jpg "]) == [
        "https://pod.example/1.jpg",
        "https://pod.example/2.jpg",
    ]
    assert photos("https://pod.example/1.jpg") == ["https://pod.example/1.jpg"]
    assert photos(
        [{"url": "https://pod.example/1.jpg"}, {"link": "https://pod.example/2.jpg"}]
    ) == [
        "https://pod.example/1.jpg",
        "https://pod.example/2.jpg",
    ]
    assert photos(["javascript:alert(1)", "data:image/png;base64,xx", 7, None, {"x": 1}]) == []
    assert photos(["https://pod.example/1.jpg", "https://pod.example/1.jpg"]) == [
        "https://pod.example/1.jpg"
    ]
    assert extract_scm_tracking_details({"signed_by": " Pat "})["delivery_signed_by"] == "Pat"


def test_tracking_box_details_land_on_the_order_and_sensor() -> None:
    client = HelloFreshClient(session=None)  # type: ignore[arg-type]
    order = HelloFreshOrder(
        order_id="1",
        week_id="2026-W40",
        status="ON_THE_WAY",
        delivery_date=date(2026, 9, 28),
        tracking_url="https://www.hellofresh.com/delivery-tracking/33cf5320-aa22-4013-b7df-f67d8d6cf32c",
    )
    box = {
        **IN_TRANSIT_BOX,
        "proof_of_delivery_photo_urls": ["https://pod.example/1.jpg"],
        "signed_by": "Pat",
    }

    assert client._apply_tracking_boxes_to_orders(orders=[order], boxes=[box]) == 1
    assert order.tracking_status_detail == "received_at_origin_facility"
    assert len(order.tracking_events) == 4
    assert order.delivery_photo_urls == ["https://pod.example/1.jpg"]
    assert order.delivery_signed_by == "Pat"
    serialized = order.as_dict()
    assert serialized["tracking_status_detail"] == "received_at_origin_facility"
    assert serialized["tracking_events"][0]["detail"] == "received_at_origin_facility"

    data = HelloFreshAccountData(orders=[order]).finalize()
    attrs = sensor_extra_state_attributes("shipment_tracking_status", data)
    assert attrs is not None
    assert attrs["status_detail"] == "received_at_origin_facility"
    assert attrs["tracking_events"][-1]["detail"] == "label_created"
    assert attrs["delivery_photo_urls"] == ["https://pod.example/1.jpg"]
    assert attrs["delivery_signed_by"] == "Pat"
