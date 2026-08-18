"""Tests for the ``tracked_shipment_date`` sensor.

Reports **when the box actually arrived** — the carrier timestamp on the most recent delivered
week (`HelloFreshWeek.delivered_at`), not the day it was scheduled for.

Three fields in this area are easy to confuse, and the distinction is the reason this sensor
exists at all:

| Value | Source | Meaning |
| --- | --- | --- |
| `sensor.last_delivery_date` | week `deliveryDate` | the **scheduled** day (a noon anchor) |
| `sensor.tracked_shipment_estimate` | SCM `est_delivery_time` | the carrier's **estimate** |
| `sensor.tracked_shipment_date` | week `tracking.delivery_date` | when it **actually** arrived |

A box handed over at 22:53 ET is already the next day in UTC, so the scheduled and actual values
legitimately disagree — asserted below with verbatim capture data.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from custom_components.hellofresh.models import HelloFreshAccountData, HelloFreshWeek
from custom_components.hellofresh.normalizers import HelloFreshPayloadNormalizer
from custom_components.hellofresh.sensor import SENSORS
from custom_components.hellofresh.sensor_helpers import sensor_native_value

# Verbatim from a capture: a DELIVERED week whose scheduled day and real arrival differ.
DELIVERED_RAW = {
    "id": "2026-W26",
    "status": "DELIVERED",
    "deliveryDate": "2026-06-23T12:00:00-0700",
    "tracking": {"delivery_date": "2026-06-23T14:37:34+0000"},
}


def _week(**overrides) -> HelloFreshWeek:
    defaults = {
        "week_id": "2026-W26",
        "display_name": "Week 26",
        "subscription_id": "sub-1",
        "delivery_date": date(2026, 6, 23),
        "status": "DELIVERED",
        "delivered_at": datetime(2026, 6, 23, 14, 37, 34, tzinfo=UTC),
    }
    return HelloFreshWeek(**{**defaults, **overrides})


def _value(*weeks) -> object:
    data = HelloFreshAccountData(past_delivery_weeks=list(weeks)).finalize()
    return sensor_native_value("tracked_shipment_date", data, "https://x")


def test_sensor_is_registered_as_a_timestamp() -> None:
    from homeassistant.components.sensor import SensorDeviceClass

    description = next(d for d in SENSORS if d.key == "tracked_shipment_date")
    assert description.device_class == SensorDeviceClass.TIMESTAMP
    assert description.translation_key == "tracked_shipment_date"


def test_reports_the_actual_arrival_timestamp() -> None:
    assert _value(_week()) == datetime(2026, 6, 23, 14, 37, 34, tzinfo=UTC)


def test_value_is_timezone_aware() -> None:
    """``SensorDeviceClass.TIMESTAMP`` rejects naive datetimes."""
    assert _value(_week()).tzinfo is not None


def test_it_differs_from_the_scheduled_delivery_date() -> None:
    """The whole point: scheduled day != actual arrival.

    If these always agreed the sensor would be redundant with `last_delivery_date`.
    """
    data = HelloFreshAccountData(past_delivery_weeks=[_week()]).finalize()
    actual = sensor_native_value("tracked_shipment_date", data, "https://x")
    scheduled = sensor_native_value("last_delivery_date", data, "https://x")
    assert scheduled == date(2026, 6, 23)
    assert actual.hour == 14 and actual.minute == 37  # a real handover time, not noon


def test_parses_from_the_captured_delivered_week() -> None:
    """End-to-end from the raw payload the API actually returns."""
    parsed = HelloFreshPayloadNormalizer._delivered_at_from_raw(DELIVERED_RAW)
    assert parsed == datetime(2026, 6, 23, 14, 37, 34, tzinfo=UTC)


def test_undelivered_weeks_are_not_reported() -> None:
    """Before delivery the same tracking field holds a scheduled placeholder.

    Reporting it would claim a box arrived when it has not, so the extractor is gated on the
    week's effective status.
    """
    pending = {**DELIVERED_RAW, "status": "RUNNING"}
    assert HelloFreshPayloadNormalizer._delivered_at_from_raw(pending) is None


def test_is_none_without_any_delivered_week() -> None:
    """A brand-new account has nothing delivered yet — unknown, not a fabricated date."""
    assert _value() is None


def test_is_none_when_the_delivered_week_has_no_carrier_timestamp() -> None:
    """Most weeks carry no tracking node at all; the sensor stays unknown rather than
    falling back to the scheduled date, which would misreport it as an arrival time."""
    assert _value(_week(delivered_at=None)) is None


def test_uses_the_most_recent_delivered_week() -> None:
    """With a history of delivered boxes, the newest one wins."""
    older = _week(
        week_id="2026-W20",
        delivery_date=date(2026, 5, 12),
        delivered_at=datetime(2026, 5, 12, 9, 0, tzinfo=UTC),
    )
    assert _value(older, _week()) == datetime(2026, 6, 23, 14, 37, 34, tzinfo=UTC)
