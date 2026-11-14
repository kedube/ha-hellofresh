"""Tests for the Tracey live delivery tracking module (NL and DE sensors, issues #6 and #12).

The Tracey endpoint is unauthenticated and region-specific, and no maintainer account can
exercise it live (it only exists for HelloFresh's own-fleet markets), so these tests pin the
behavior that has to hold when NL users field-test the beta: token derivation from the
tracking URL, defensive payload parsing, the fast/slow poll cadence, and — most importantly —
that the sensors exist ONLY for supported countries.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime
from types import SimpleNamespace

import pytest

from custom_components.hellofresh.const import (
    CONF_DELIVERY_TRACKING_REFRESH_INTERVAL_SECONDS,
    TRACEY_COUNTRIES,
)
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


def _bare_coordinator(
    session, tracking_url: str | None, *, country: str = "nl"
) -> HelloFreshTraceyCoordinator:
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
    coordinator.config_entry = SimpleNamespace(
        entry_id="test-entry", title="HelloFresh (NL)", options={}, data={"country": country}
    )
    coordinator._last_attempt_monotonic = None
    coordinator._fetch_lock = asyncio.Lock()
    coordinator.data = None
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


def test_coordinator_german_delivery_uses_the_tracking_number_and_german_site() -> None:
    """German links are ``status.hellofresh.de/<tracking number>``; that number is the token.

    Confirmed by a user in issue #12: the German tracking page calls the same cloud function
    with the link's last segment as ``token``. The request names the German page as its
    Origin/Referer, as the browser would.
    """
    session = _FakeSession(LIVE_PAYLOAD)
    coordinator = _bare_coordinator(
        session, "https://status.hellofresh.de/1234567890", country="de"
    )
    data = _run(coordinator._async_update_data())
    assert data.active is True
    request = session.requests[0]
    assert request["params"]["token"] == "1234567890"
    assert request["headers"]["Origin"] == "https://status.hellofresh.de"
    assert request["headers"]["Referer"] == "https://status.hellofresh.de/"


def test_coordinator_live_delivery_uses_configured_seconds_interval() -> None:
    """NL users can tune how often active Tracey deliveries are refreshed."""
    session = _FakeSession(LIVE_PAYLOAD)
    coordinator = _bare_coordinator(session, "https://www.hftrack.nl/abc123")
    coordinator.config_entry.options[CONF_DELIVERY_TRACKING_REFRESH_INTERVAL_SECONDS] = 120
    _run(coordinator._async_update_data())
    assert coordinator.update_interval.total_seconds() == 120


def test_unknown_active_phase_stays_on_fast_polling() -> None:
    """New live Tracey phases must not fall back to the 30-minute idle cadence."""
    session = _FakeSession({**LIVE_PAYLOAD, "traceyPhase": "DRIVER_CLOSE_BY"})
    coordinator = _bare_coordinator(session, "https://www.hftrack.nl/abc123")
    data = _run(coordinator._async_update_data())
    assert data.active is True
    assert data.phase == "DRIVER_CLOSE_BY"
    assert coordinator.update_interval == ACTIVE_UPDATE_INTERVAL


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


def test_coordinator_non_json_answer_raises_update_failed() -> None:
    """A 200 whose body is not JSON is a failed update, not an unexpected error.

    The decode error used to escape, and Home Assistant logged "Unexpected error" with a
    traceback on every tick for as long as the endpoint served an error page.
    """
    import json  # noqa: PLC0415

    from homeassistant.helpers.update_coordinator import UpdateFailed  # noqa: PLC0415

    class _HtmlResponse(_FakeResponse):
        async def json(self, content_type=None):
            return json.loads("<html>Service Unavailable</html>")

    class _HtmlSession(_FakeSession):
        def get(self, url, params=None, headers=None, timeout=None):
            return _HtmlResponse(None)

    coordinator = _bare_coordinator(_HtmlSession(None), "https://www.hftrack.nl/abc123")
    with pytest.raises(UpdateFailed, match="not JSON"):
        _run(coordinator._async_update_data())


def test_coordinator_connection_error_never_carries_the_tracking_token() -> None:
    """aiohttp puts the request URL in some error messages, and this URL holds the token.

    The token opens the unauthenticated tracking page, and the coordinator logs this message
    at ERROR, so only the error's type may be named, and the cause is not chained.
    """
    import aiohttp  # noqa: PLC0415
    from homeassistant.helpers.update_coordinator import UpdateFailed  # noqa: PLC0415
    from multidict import CIMultiDict, CIMultiDictProxy  # noqa: PLC0415
    from yarl import URL  # noqa: PLC0415

    from custom_components.hellofresh.tracey import TRACEY_ENDPOINT  # noqa: PLC0415

    url = URL(TRACEY_ENDPOINT).with_query({"token": "FAKE-TOKEN-123", "screenWidth": "500"})
    request_info = aiohttp.RequestInfo(url, "GET", CIMultiDictProxy(CIMultiDict()), url)
    error = aiohttp.TooManyRedirects(request_info, ())
    assert "FAKE-TOKEN-123" in str(error)  # what made the old message a leak

    class _FailingSession(_FakeSession):
        def get(self, url, params=None, headers=None, timeout=None):
            raise error

    coordinator = _bare_coordinator(_FailingSession(None), "https://www.hftrack.nl/FAKE-TOKEN-123")
    with pytest.raises(UpdateFailed) as raised:
        _run(coordinator._async_update_data())

    assert str(raised.value) == "Tracey endpoint unreachable: TooManyRedirects"
    assert raised.value.__cause__ is None and raised.value.__suppress_context__ is True


def test_snapshots_of_the_same_state_are_equal_whenever_they_were_fetched() -> None:
    """The fetch time differs on every poll; it must not make every poll look like a change.

    The coordinator skips its listeners when the new data equals the old. With the fetch
    time in the comparison that never happened, so every tracking sensor was rewritten on
    every tick.
    """
    first = parse_tracey_payload(
        LIVE_PAYLOAD, tracking_url="u", now=datetime.fromisoformat("2026-08-27T16:00:00+00:00")
    )
    later = parse_tracey_payload(
        {**LIVE_PAYLOAD, "somethingNotParsed": 1},
        tracking_url="u",
        now=datetime.fromisoformat("2026-08-27T16:05:00+00:00"),
    )
    assert first.fetched_at != later.fetched_at
    assert first == later

    moved = parse_tracey_payload({**LIVE_PAYLOAD, "amountOfStopsBefore": 2}, tracking_url="u")
    assert moved != first  # a real change still counts
    assert TraceyData(fetched_at=first.fetched_at) == TraceyData(fetched_at=later.fetched_at)


class _SlowSession(_FakeSession):
    """A session whose answer takes a moment, so callers can pile up behind a request."""

    def __init__(self, payload: object, status: int = 200) -> None:
        super().__init__(payload, status)
        self.gate = asyncio.Event()

    def get(self, url, params=None, headers=None, timeout=None):
        self.requests.append({"url": url, "params": params, "headers": headers})
        gate, payload, status = self.gate, self._payload, self._status

        class _Response(_FakeResponse):
            async def __aenter__(self):
                await gate.wait()
                return self

        return _Response(payload, status)


def _service_coordinator(session) -> HelloFreshTraceyCoordinator:
    """A bare coordinator whose refresh does what Home Assistant's does with a failure."""
    from homeassistant.helpers.update_coordinator import UpdateFailed  # noqa: PLC0415

    coordinator = _bare_coordinator(session, "https://www.hftrack.nl/abc123")

    async def async_refresh() -> None:
        # On a failure the last snapshot stays, as with the real coordinator.
        with suppress(UpdateFailed):
            coordinator.data = await coordinator._async_update_data()

    coordinator.async_refresh = async_refresh  # type: ignore[method-assign]
    return coordinator


def test_service_callers_arriving_together_share_one_request() -> None:
    """Several get_delivery_tracking calls at once make one request, not one each.

    The age of the last fetch was checked before the request started, so every caller that
    arrived while it was in flight saw a stale snapshot and sent its own.
    """
    session = _SlowSession(LIVE_PAYLOAD)
    coordinator = _service_coordinator(session)

    async def _three_callers():
        callers = [asyncio.ensure_future(coordinator.async_fetch_latest()) for _ in range(3)]
        await asyncio.sleep(0)
        session.gate.set()
        results = await asyncio.gather(*callers)
        # A later call within the minute reuses the snapshot as well.
        results.append(await coordinator.async_fetch_latest())
        return results

    results = _run(_three_callers())

    assert len(session.requests) == 1
    assert all(result.phase == "ON_THE_WAY" for result in results)


def test_service_does_not_retry_a_failed_request_within_the_minute(monkeypatch) -> None:
    """A failing endpoint is asked once a minute at most, not once per service call."""
    from custom_components.hellofresh import tracey  # noqa: PLC0415

    now = [5000.0]
    monkeypatch.setattr(tracey.time, "monotonic", lambda: now[0])
    session = _FakeSession({}, status=503)
    coordinator = _service_coordinator(session)

    async def _calls():
        first = await coordinator.async_fetch_latest()
        second = await coordinator.async_fetch_latest()
        now[0] += 61
        await coordinator.async_fetch_latest()
        return first, second

    first, second = _run(_calls())

    assert first.active is False and second.active is False  # no snapshot, but no error
    assert len(session.requests) == 2  # one, then one more after the minute


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


def test_tracey_countries_are_the_confirmed_markets() -> None:
    """The gate is deliberate: only markets with a confirmed Tracey deployment.

    The Netherlands (issue #6) and Germany (issue #12) are confirmed. Belgium/Luxembourg
    likely qualify (own-fleet markets) but are unverified — widen the set only with a
    confirmation from a user there.
    """
    assert frozenset({"nl", "de"}) == TRACEY_COUNTRIES


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
