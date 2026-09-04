"""Tests for the Tracey live delivery tracking module (NL-only sensors, issue #6).

The Tracey endpoint is unauthenticated and region-specific, and no maintainer account can
exercise it live (it only exists for HelloFresh's own-fleet markets), so these tests pin the
behavior that has to hold when NL users field-test the beta: token derivation from the
tracking URL, defensive payload parsing, the fast/slow poll cadence, and — most importantly —
that the sensors exist ONLY for supported countries.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

from custom_components.hellofresh.const import TRACEY_COUNTRIES
from custom_components.hellofresh.sensor import TRACEY_SENSORS, HelloFreshTraceySensor
from custom_components.hellofresh.tracey import (
    ACTIVE_UPDATE_INTERVAL,
    IDLE_UPDATE_INTERVAL,
    HelloFreshTraceyCoordinator,
    TraceyData,
    parse_tracey_payload,
    tracey_token,
)

# The exact payload shape captured by the community REST sensors in issue #6.
LIVE_PAYLOAD = {
    "traceyPhase": "ON_THE_WAY",
    "personalCustomerMessage": "Tot zo!",
    "customerLocation": {"latitude": 52.37, "longitude": 4.89},
    "deliveryTime": "17:00 - 22:00",
    "driverName": "Daan",
    "amountOfStopsBefore": 3,
    "driverLocation": {"latitude": 52.35, "longitude": 4.85},
    "estimatedTimeOfArrival": "2026-08-27T18:42:00+02:00",
}


# ---- token derivation ------------------------------------------------------------------


def test_tracey_token_is_last_path_segment() -> None:
    """The community capture derives the token as tracking_url.split('/')[-1]."""
    assert tracey_token("https://www.hftrack.nl/abc123XYZ") == "abc123XYZ"
    assert tracey_token("https://www.hftrack.nl/track/abc123XYZ") == "abc123XYZ"


def test_tracey_token_survives_url_decoration() -> None:
    """Trailing slashes, query strings, and fragments must not corrupt the token."""
    assert tracey_token("https://www.hftrack.nl/abc123/") == "abc123"
    assert tracey_token("https://www.hftrack.nl/abc123?utm_source=mail") == "abc123"
    assert tracey_token("https://www.hftrack.nl/abc123#top") == "abc123"


def test_tracey_token_rejects_tokenless_urls() -> None:
    """A bare domain (or no URL at all) means there is nothing to poll."""
    assert tracey_token("https://www.hftrack.nl") is None
    assert tracey_token("https://www.hftrack.nl/") is None
    assert tracey_token(None) is None
    assert tracey_token("") is None
    # A carrier URL whose tail is a filename is not a Tracey token either.
    assert tracey_token("https://carrier.example.com/track.html") is None


# ---- payload parsing -------------------------------------------------------------------


def test_parse_live_payload() -> None:
    """A live delivery payload maps field-for-field onto the snapshot."""
    data = parse_tracey_payload(LIVE_PAYLOAD, tracking_url="https://www.hftrack.nl/abc")
    assert data.active is True
    assert data.phase == "ON_THE_WAY"
    assert data.message == "Tot zo!"
    assert data.driver_name == "Daan"
    assert data.stops_before == 3
    assert data.eta == datetime.fromisoformat("2026-08-27T18:42:00+02:00")
    assert data.eta.utcoffset() is not None  # TIMESTAMP entities require aware datetimes
    assert data.driver_location == {"latitude": 52.35, "longitude": 4.85}
    assert data.customer_location == {"latitude": 52.37, "longitude": 4.89}
    assert data.delivery_time == "17:00 - 22:00"
    assert data.tracking_url == "https://www.hftrack.nl/abc"


def test_parse_invalid_link_is_inactive_not_error() -> None:
    """INVALID_LINK is the endpoint's clean 'no active delivery' answer."""
    data = parse_tracey_payload({"traceyPhase": "INVALID_LINK"}, tracking_url="u")
    assert data.active is False
    assert data.token_present is True
    assert data.phase == "INVALID_LINK"
    assert data.driver_name is None


def test_parse_defends_against_malformed_payloads() -> None:
    """Unknown shapes must degrade to an inactive snapshot, never raise."""
    assert parse_tracey_payload(None, tracking_url="u").active is False
    assert parse_tracey_payload("nope", tracking_url="u").active is False
    data = parse_tracey_payload(
        {
            "traceyPhase": "AT_DEPOT",
            "amountOfStopsBefore": "many",  # non-numeric -> None, not a crash
            "driverLocation": {"latitude": "x"},  # malformed -> None
            "estimatedTimeOfArrival": "not-a-date",
        },
        tracking_url="u",
    )
    assert data.active is True
    assert data.stops_before is None
    assert data.driver_location is None
    assert data.eta is None


def test_parse_naive_eta_becomes_aware() -> None:
    """An offset-less ETA is stamped UTC so the TIMESTAMP entity never rejects it."""
    data = parse_tracey_payload(
        {"traceyPhase": "ON_THE_WAY", "estimatedTimeOfArrival": "2026-08-27T18:42:00"},
        tracking_url="u",
    )
    assert data.eta is not None
    assert data.eta.tzinfo is not None


def test_snapshot_as_dict_is_json_safe() -> None:
    """The service response form must carry only JSON-serializable values."""
    import json

    payload = parse_tracey_payload(LIVE_PAYLOAD, tracking_url="u").as_dict()
    assert json.dumps(payload)  # raises on anything non-serializable
    assert payload["eta"].startswith("2026-08-27T18:42:00")
    assert payload["active"] is True


# ---- coordinator polling ---------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _FakeSession:
    def __init__(self, payload: object, status: int = 200) -> None:
        self._payload = payload
        self._status = status
        self.requests: list[dict] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.requests.append({"url": url, "params": params, "headers": headers})
        return _FakeResponse(self._payload, self._status)


def _bare_coordinator(session, tracking_url: str | None) -> HelloFreshTraceyCoordinator:
    """Build a coordinator without hass: only the attributes _async_update_data reads.

    ``update_interval`` is a plain property (setter touches no hass machinery), so the
    polling-cadence adaptation can be asserted without a full Home Assistant instance.
    """
    coordinator = HelloFreshTraceyCoordinator.__new__(HelloFreshTraceyCoordinator)
    coordinator._session = session
    coordinator._main_coordinator = SimpleNamespace(
        data=SimpleNamespace(
            tracked_order=SimpleNamespace(tracking_url=tracking_url),
            next_order=None,
        )
    )
    coordinator.config_entry = SimpleNamespace(entry_id="test-entry", title="HelloFresh (NL)")
    coordinator._last_fetch_monotonic = None
    coordinator._update_interval_seconds = None
    coordinator.update_interval = IDLE_UPDATE_INTERVAL
    return coordinator


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_coordinator_idle_without_token_makes_no_request() -> None:
    """No tracking link -> an idle snapshot and zero HTTP traffic."""
    session = _FakeSession(LIVE_PAYLOAD)
    coordinator = _bare_coordinator(session, tracking_url=None)
    data = _run(coordinator._async_update_data())
    assert data.active is False
    assert data.token_present is False
    assert session.requests == []
    assert coordinator.update_interval == IDLE_UPDATE_INTERVAL


def test_coordinator_live_delivery_polls_fast() -> None:
    """A live phase switches the poll cadence to the fast interval."""
    session = _FakeSession(LIVE_PAYLOAD)
    coordinator = _bare_coordinator(session, "https://www.hftrack.nl/abc123")
    data = _run(coordinator._async_update_data())
    assert data.active is True
    assert coordinator.update_interval == ACTIVE_UPDATE_INTERVAL
    # The request must look like the hftrack.nl frontend's own call.
    request = session.requests[0]
    assert request["params"]["token"] == "abc123"
    assert request["headers"]["Origin"] == "https://www.hftrack.nl"


def test_coordinator_delivered_slows_back_down() -> None:
    """Once delivered there is nothing to watch closely; cadence returns to idle."""
    session = _FakeSession({**LIVE_PAYLOAD, "traceyPhase": "DELIVERED_HOME"})
    coordinator = _bare_coordinator(session, "https://www.hftrack.nl/abc123")
    data = _run(coordinator._async_update_data())
    assert data.active is True
    assert data.phase == "DELIVERED_HOME"
    assert coordinator.update_interval == IDLE_UPDATE_INTERVAL


def test_coordinator_http_error_raises_update_failed() -> None:
    """A non-200 becomes UpdateFailed so entities go unavailable, not stale."""
    from homeassistant.helpers.update_coordinator import UpdateFailed

    session = _FakeSession({}, status=503)
    coordinator = _bare_coordinator(session, "https://www.hftrack.nl/abc123")
    try:
        _run(coordinator._async_update_data())
    except UpdateFailed:
        pass
    else:
        raise AssertionError("expected UpdateFailed for HTTP 503")


def test_coordinator_falls_back_to_next_order_link() -> None:
    """Early in the week only the upcoming order may carry the tracking link."""
    session = _FakeSession(LIVE_PAYLOAD)
    coordinator = _bare_coordinator(session, tracking_url=None)
    coordinator._main_coordinator = SimpleNamespace(
        data=SimpleNamespace(
            tracked_order=None,
            next_order=SimpleNamespace(tracking_url="https://www.hftrack.nl/next456"),
        )
    )
    _run(coordinator._async_update_data())
    assert session.requests[0]["params"]["token"] == "next456"


# ---- country gating --------------------------------------------------------------------


def test_tracey_is_netherlands_only_for_now() -> None:
    """The gate is deliberate: only markets with a confirmed Tracey deployment.

    Belgium/Luxembourg likely qualify (own-fleet markets) but are unverified — widen the
    set only with a confirmation from a user there, per the discussion on issue #6.
    """
    assert frozenset({"nl"}) == TRACEY_COUNTRIES


# ---- sensors ---------------------------------------------------------------------------


def _tracey_sensor(key: str, data: TraceyData | None) -> HelloFreshTraceySensor:
    coordinator = SimpleNamespace(
        config_entry=SimpleNamespace(entry_id="test-entry", title="HelloFresh (NL)"),
        data=data,
    )
    description = next(item for item in TRACEY_SENSORS if item.key == key)
    return HelloFreshTraceySensor(coordinator, description)


def test_tracking_sensor_ids_are_pinned() -> None:
    """Same stable-id contract as every other entity: <title-slug>_<key>."""
    sensor = _tracey_sensor("delivery_tracking_phase", None)
    assert sensor.entity_id == "sensor.hellofresh_nl_delivery_tracking_phase"


def test_tracking_sensors_read_the_snapshot() -> None:
    data = parse_tracey_payload(LIVE_PAYLOAD, tracking_url="https://www.hftrack.nl/abc")
    assert _tracey_sensor("delivery_tracking_phase", data).native_value == "On the way"
    assert _tracey_sensor("delivery_tracking_stops_before", data).native_value == 3
    assert _tracey_sensor("delivery_tracking_driver", data).native_value == "Daan"
    assert _tracey_sensor("delivery_tracking_eta", data).native_value == data.eta


def test_tracking_sensors_unknown_when_inactive() -> None:
    """Stale detail must never linger after a delivery ends."""
    idle = TraceyData()
    for key in (
        "delivery_tracking_phase",
        "delivery_tracking_eta",
        "delivery_tracking_stops_before",
        "delivery_tracking_driver",
    ):
        assert _tracey_sensor(key, idle).native_value is None
        assert _tracey_sensor(key, None).native_value is None


def test_driver_sensor_exposes_map_coordinates() -> None:
    """Bare latitude/longitude attributes are what the built-in map card plots."""
    data = parse_tracey_payload(LIVE_PAYLOAD, tracking_url="u")
    attributes = _tracey_sensor("delivery_tracking_driver", data).extra_state_attributes
    assert attributes == {"latitude": 52.35, "longitude": 4.85}
    # Without a live position there is nothing to plot.
    assert _tracey_sensor("delivery_tracking_driver", TraceyData()).extra_state_attributes is None


def test_phase_sensor_attributes_carry_the_full_snapshot() -> None:
    data = parse_tracey_payload(LIVE_PAYLOAD, tracking_url="https://www.hftrack.nl/abc")
    attributes = _tracey_sensor("delivery_tracking_phase", data).extra_state_attributes
    assert attributes["phase"] == "ON_THE_WAY"
    assert attributes["personal_customer_message"] == "Tot zo!"
    assert attributes["amount_of_stops_before"] == 3
    assert attributes["tracking_url"] == "https://www.hftrack.nl/abc"


def test_tracking_sensor_descriptions_have_translation_names() -> None:
    """Same contract as the main sensor set: every key needs a translated name."""
    import json
    from pathlib import Path

    strings = json.loads(Path("custom_components/hellofresh/strings.json").read_text())
    translated = set(strings["entity"]["sensor"])
    assert {description.key for description in TRACEY_SENSORS} <= translated
