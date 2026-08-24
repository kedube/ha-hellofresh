"""Tests for the ``tracked_shipment_estimate`` sensor.

The value is the **carrier's own** delivery estimate, read from the SCM tracking lookup's status
history (``statuses[].est_delivery_time`` / ``last_status.est_delivery_time``), verified against
capture 41.

Two things make this field worth its own entity, and both are asserted here:

* It is **not** the `estimated_delivery_time` that appears on the week's `tracking` node. That one
  is byte-identical to `delivery_date` in all 69 samples across the captures that carry both, which
  is why it is deliberately not surfaced (see docs/HELLOFRESH_API.md). This is a different field from a
  different endpoint.
* It is date-precision: the carrier reports midnight of the estimated day, whereas HelloFresh's
  own `delivery_date` is a scheduled **noon** anchor. The two therefore disagree by 12 hours on
  the same box, which is exactly the case pinned below.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from custom_components.hellofresh.client import HelloFreshClient
from custom_components.hellofresh.models import HelloFreshAccountData, HelloFreshOrder
from custom_components.hellofresh.parsers import extract_scm_tracking_details
from custom_components.hellofresh.sensor import SENSORS
from custom_components.hellofresh.sensor_helpers import sensor_native_value

# Verbatim from capture 41 (trimmed to the fields the parser reads).
CAPTURED_BOX = {
    "external_id": "H4234151270",
    "carrier": "VEHO",
    "tracking_code": "HF01000042767022",
    "delivery_date": "2026-08-17T12:00:00Z",
    "carrier_tracking_url": "https://track.shipveho.com/#/trackingId/HF01000042767022",
    "internal_status": "delivered",
    "last_status": {
        "status": "delivered",
        "datetime": "2026-08-17T22:53:06Z",
        "est_delivery_time": "2026-08-17T00:00:00Z",
    },
    "statuses": [
        {"status": "delivered", "est_delivery_time": "2026-08-17T00:00:00Z"},
        {"status": "out_for_delivery", "est_delivery_time": "2026-08-17T00:00:00Z"},
        {"status": "pre_transit", "est_delivery_time": "2026-08-17T00:00:00Z"},
    ],
}


def _order(**overrides) -> HelloFreshOrder:
    defaults = {
        "order_id": "ord-1",
        "week_id": "2026-W34",
        "status": "shipped",
        "subscription_id": "6959884",
        "delivery_date": date(2026, 8, 17),
        "tracking_status": "in_transit",
    }
    return HelloFreshOrder(**{**defaults, **overrides})


def _apply(box) -> HelloFreshOrder:
    client = HelloFreshClient(session=object())  # type: ignore[arg-type]
    order = _order()
    client._apply_tracking_boxes_to_orders([order], [box])
    return order


# --- parsing ------------------------------------------------------------------------


def test_estimate_is_extracted_from_the_captured_box() -> None:
    assert extract_scm_tracking_details(CAPTURED_BOX)["estimated_delivery"] == (
        "2026-08-17T00:00:00Z"
    )


def test_estimate_falls_back_to_the_status_history() -> None:
    """``last_status`` is a convenience mirror; only ``statuses`` is guaranteed present."""
    box = {**CAPTURED_BOX, "last_status": {"status": "delivered"}}
    assert extract_scm_tracking_details(box)["estimated_delivery"] == "2026-08-17T00:00:00Z"


def test_estimate_is_none_when_the_carrier_supplies_no_eta() -> None:
    """Absent is a real state -- the sensor must read unknown, not crash or invent a value."""
    box = {k: v for k, v in CAPTURED_BOX.items() if k not in ("last_status", "statuses")}
    assert extract_scm_tracking_details(box)["estimated_delivery"] is None


def test_malformed_status_entries_do_not_break_extraction() -> None:
    box = {**CAPTURED_BOX, "last_status": {}, "statuses": ["junk", None, {"status": "x"}]}
    assert extract_scm_tracking_details(box)["estimated_delivery"] is None


# --- merge onto the order ------------------------------------------------------------


def test_estimate_lands_on_the_order_as_an_aware_datetime() -> None:
    """``SensorDeviceClass.TIMESTAMP`` requires tz-aware values; HA rejects naive ones."""
    order = _apply(CAPTURED_BOX)
    assert order.estimated_delivery == datetime(2026, 8, 17, 0, 0, tzinfo=UTC)
    assert order.estimated_delivery.tzinfo is not None


def test_estimate_is_distinct_from_the_scheduled_delivery_date() -> None:
    """The whole point of the sensor: the carrier's estimate is not HelloFresh's noon anchor.

    If these were interchangeable the sensor would be redundant with `next_delivery_date`.
    """
    order = _apply(CAPTURED_BOX)
    assert CAPTURED_BOX["delivery_date"] == "2026-08-17T12:00:00Z"  # HelloFresh: noon
    assert order.estimated_delivery.hour == 0  # carrier: midnight (date precision)


def test_an_unparseable_estimate_leaves_the_field_unset() -> None:
    box = {**CAPTURED_BOX, "last_status": {"est_delivery_time": "not-a-date"}, "statuses": []}
    assert _apply(box).estimated_delivery is None


def test_a_missing_estimate_does_not_clear_a_previously_known_one() -> None:
    """A later poll whose box omits the ETA must not blank a value already resolved."""
    client = HelloFreshClient(session=object())  # type: ignore[arg-type]
    order = _order(estimated_delivery=datetime(2026, 8, 17, tzinfo=UTC))
    box = {k: v for k, v in CAPTURED_BOX.items() if k not in ("last_status", "statuses")}
    client._apply_tracking_boxes_to_orders([order], [box])
    assert order.estimated_delivery == datetime(2026, 8, 17, tzinfo=UTC)


# --- the sensor ----------------------------------------------------------------------


def test_sensor_is_registered_as_a_date_not_a_timestamp() -> None:
    """The estimate is date-precision, so the entity must declare DATE.

    Registered as TIMESTAMP it rendered midnight-UTC in the viewer's zone: a US-Eastern user
    saw "Aug 23 @ 8:00 PM" for a box the carrier estimated for Aug 24 — both the wrong day and
    a time of day the carrier never gave.
    """
    from homeassistant.components.sensor import SensorDeviceClass

    description = next(d for d in SENSORS if d.key == "tracked_shipment_estimate")
    assert description.device_class == SensorDeviceClass.DATE
    assert description.translation_key == "tracked_shipment_estimate"


def test_sensor_reports_the_estimated_day() -> None:
    data = HelloFreshAccountData(orders=[_apply(CAPTURED_BOX)]).finalize()
    assert data.tracked_order is not None
    assert sensor_native_value("tracked_shipment_estimate", data, "https://x") == date(2026, 8, 17)


def test_estimated_day_is_read_in_utc_not_local_time() -> None:
    """The calendar day must come from the UTC instant, not the viewer's zone.

    ``est_delivery_time`` is midnight *UTC* of the estimated day. Converting to local time
    before taking ``.date()`` would move it back a day for anyone west of UTC — the same
    off-by-one, just relocated.
    """
    order = _order(estimated_delivery=datetime(2026, 8, 24, 0, 0, tzinfo=UTC))
    order.tracking_number = "HF01000042879712"
    data = HelloFreshAccountData(orders=[order]).finalize()
    assert sensor_native_value("tracked_shipment_estimate", data, "https://x") == date(2026, 8, 24)


def test_sensor_is_none_without_a_tracked_order() -> None:
    """No shipment in flight -> no estimate, rather than a stale or invented one."""
    data = HelloFreshAccountData(orders=[]).finalize()
    assert sensor_native_value("tracked_shipment_estimate", data, "https://x") is None


def test_estimate_is_serialized_in_order_attributes() -> None:
    assert _apply(CAPTURED_BOX).as_dict()["estimated_delivery"] == "2026-08-17T00:00:00+00:00"


def test_capture_47_out_for_delivery_box_reports_the_right_day_and_status() -> None:
    """Regression from capture 47 (a live out-for-delivery box, 2026-08-24).

    Both sensors were reported wrong against this exact box: the status showed "In transit"
    while the website said "Out for delivery", and the estimate showed "Aug 23 @ 8:00 PM" for
    a box arriving Aug 24. The status turned out to be correct-but-stale (the box flipped at
    17:55 UTC and the default poll is 3h); the estimate was a real bug, fixed by declaring the
    entity DATE.
    """
    box = {
        "external_id": "H4237862550",
        "carrier": "VEHO",
        "delivery_date": "2026-08-24T12:00:00Z",
        "tracking_code": "HF01000042879712",
        "public_url": "https://track.shipveho.com/#/trackingId/HF01000042879712",
        "internal_status": "out_for_delivery",
        "last_status": {
            "status": "out_for_delivery",
            "datetime": "2026-08-24T17:55:43Z",
            "est_delivery_time": "2026-08-24T00:00:00Z",
        },
        "statuses": [
            {"status": "out_for_delivery", "est_delivery_time": "2026-08-24T00:00:00Z"},
            {"status": "in_transit", "est_delivery_time": "2026-08-24T00:00:00Z"},
        ],
    }
    details = extract_scm_tracking_details(box)
    assert details["tracking_status"] == "out_for_delivery"
    assert details["estimated_delivery"] == "2026-08-24T00:00:00Z"

    order = _order(estimated_delivery=None)
    order.delivery_date = date(2026, 8, 24)
    client = HelloFreshClient(session=object())  # type: ignore[arg-type]
    client._apply_tracking_boxes_to_orders([order], [box])
    data = HelloFreshAccountData(orders=[order]).finalize()

    # The estimated DAY, not an invented 8pm on the previous day.
    assert sensor_native_value("tracked_shipment_estimate", data, "https://x") == date(2026, 8, 24)
    # And the status the website showed.
    assert sensor_native_value("shipment_tracking_status", data, "https://x") == (
        "Out for delivery"
    )


def test_estimated_day_is_the_same_in_every_timezone() -> None:
    """The estimate must name the same calendar day for every viewer.

    ``est_delivery_time`` is midnight UTC of the estimated day, so an instant that is "Aug 24"
    in UTC is still "Aug 23 evening" in the Americas. Taking the day in UTC is what keeps the
    answer identical worldwide; a local conversion would report Aug 23 for every US zone while
    Europe and Asia-Pacific saw Aug 24 — the same box, two different answers.
    """
    from zoneinfo import ZoneInfo

    order = _order(estimated_delivery=datetime(2026, 8, 24, 0, 0, tzinfo=UTC))
    order.tracking_number = "HF01000042879712"
    data = HelloFreshAccountData(orders=[order]).finalize()

    for tz_name in (
        "Pacific/Auckland",
        "Asia/Tokyo",
        "Europe/Berlin",
        "UTC",
        "America/New_York",
        "America/Los_Angeles",
        "Pacific/Honolulu",
    ):
        # The helper must not consult local time at all; passing an offset-aware value in a
        # different zone must not move the reported day.
        shifted = order.estimated_delivery.astimezone(ZoneInfo(tz_name))
        order.estimated_delivery = shifted
        data = HelloFreshAccountData(orders=[order]).finalize()
        assert sensor_native_value("tracked_shipment_estimate", data, "https://x") == date(
            2026, 8, 24
        ), f"wrong day when the value arrives as {tz_name}"


def test_date_sensors_are_not_re_localized_by_home_assistant() -> None:
    """A DATE sensor's state is the bare ISO day, so no viewer-side shift can occur.

    This is why DATE is the right class here rather than TIMESTAMP with a noon anchor: HA
    serializes a date as ``value.isoformat()`` with no timezone maths, whereas a timestamp is
    converted to UTC and re-rendered in each viewer's zone.
    """
    from homeassistant.components.sensor import SensorDeviceClass

    description = next(d for d in SENSORS if d.key == "tracked_shipment_estimate")
    assert description.device_class is SensorDeviceClass.DATE

    order = _order(estimated_delivery=datetime(2026, 8, 24, 0, 0, tzinfo=UTC))
    order.tracking_number = "HF01000042879712"
    data = HelloFreshAccountData(orders=[order]).finalize()
    value = sensor_native_value("tracked_shipment_estimate", data, "https://x")
    assert isinstance(value, date) and not isinstance(value, datetime)
    assert value.isoformat() == "2026-08-24"
