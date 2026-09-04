"""Live last-mile delivery tracking via HelloFresh's Tracey API.

In markets where HelloFresh runs its own delivery fleet (currently the Netherlands —
tracked in issue #6), the customer tracking page (``hftrack.nl``) is backed by an
unauthenticated Google Cloud Function that reports the live delivery state: the phase
(packed / departed / on the way / delivered), the driver's name and GPS position, how many
stops remain before this customer, and a minute-precision ETA. None of that exists in the
core ``/gw`` API — carriers there expose only coarse shipment status.

The endpoint is keyed by the per-delivery token that is the last path segment of the
order's ``tracking_url`` and returns ``{"traceyPhase": "INVALID_LINK"}`` once a link is
stale, so no-delivery days are cheap and unambiguous.

This module is deliberately self-contained (not part of ``client.py``): the Tracey stack
lives outside the authenticated HelloFresh API, needs none of the token machinery, and is
only mounted for countries in ``TRACEY_COUNTRIES``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
import time
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .token_manager import _BROWSER_USER_AGENT

_LOGGER = logging.getLogger(__name__)

TRACEY_ENDPOINT = "https://europe-west1-hellofresh-prod.cloudfunctions.net/c_hf_getTraceyData"
# The tracking site whose frontend calls the cloud function; sent as Origin/Referer so the
# request matches what the endpoint normally sees. NL-specific, like the endpoint itself.
TRACEY_SITE = "https://www.hftrack.nl"

# Phases observed in the community capture (issue #6). DELAYED counts as active — the
# delivery still exists and the endpoint keeps updating it.
ACTIVE_PHASES = frozenset({"AT_DEPOT", "DRIVER_DEPARTED", "ON_THE_WAY", "DELAYED"})
DELIVERED_PHASES = frozenset({"DELIVERED", "DELIVERED_HOME"})
INVALID_PHASE = "INVALID_LINK"

# Poll fast while a delivery is live (driver GPS / ETA / stop count change by the minute),
# slow when there is nothing to watch. The idle tick still runs so a delivery that starts
# between main-coordinator polls is picked up within half an hour.
ACTIVE_UPDATE_INTERVAL = timedelta(minutes=5)
IDLE_UPDATE_INTERVAL = timedelta(minutes=30)

# The response-returning service refetches live data for the tracking card; this floor
# keeps a card refresh loop (or several dashboards open at once) from hammering the
# endpoint more than once a minute.
MIN_SERVICE_FETCH_INTERVAL_SECONDS = 60


def tracey_token(tracking_url: str | None) -> str | None:
    """Return the Tracey token — the last path segment of the order's tracking URL.

    The community capture derives it exactly this way (``tracking_url.split('/')[-1]``).
    Guards against URLs with query strings, trailing slashes, and non-hftrack carriers'
    URLs whose last segment is clearly not a bare token.
    """
    if not tracking_url or not isinstance(tracking_url, str):
        return None
    path = tracking_url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    token = path.rsplit("/", 1)[-1] if "/" in path else ""
    # A real token is an opaque slug. An empty tail means a bare domain with no path; a tail
    # containing a dot is the hostname itself (``https://www.hftrack.nl`` → ``www.hftrack.nl``)
    # or a filename — either way not a per-delivery token.
    if not token or "." in token:
        return None
    return token


def _location(value: Any) -> dict[str, float] | None:
    """Normalize a ``{latitude, longitude}`` payload; None when absent or malformed."""
    if not isinstance(value, dict):
        return None
    try:
        latitude = float(value["latitude"])
        longitude = float(value["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    return {"latitude": latitude, "longitude": longitude}


@dataclass
class TraceyData:
    """One snapshot of the live tracking state.

    ``active`` is the headline flag: True only when a token exists AND the endpoint reports
    a live (non-invalid) delivery. All detail fields are None when inactive, so the sensors
    read as Unknown outside a delivery window rather than carrying stale values.
    """

    active: bool = False
    token_present: bool = False
    phase: str | None = None
    message: str | None = None
    driver_name: str | None = None
    stops_before: int | None = None
    eta: datetime | None = None
    delivery_time: str | None = None
    driver_location: dict[str, float] | None = None
    customer_location: dict[str, float] | None = None
    tracking_url: str | None = None
    fetched_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe form for the ``get_delivery_tracking`` service response."""
        return {
            "active": self.active,
            "token_present": self.token_present,
            "phase": self.phase,
            "message": self.message,
            "driver_name": self.driver_name,
            "stops_before": self.stops_before,
            "eta": self.eta.isoformat() if self.eta else None,
            "delivery_time": self.delivery_time,
            "driver_location": self.driver_location,
            "customer_location": self.customer_location,
            "tracking_url": self.tracking_url,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
        }


def parse_tracey_payload(
    payload: Any,
    *,
    tracking_url: str | None,
    now: datetime | None = None,
) -> TraceyData:
    """Build a TraceyData snapshot from the cloud function's JSON response."""
    fetched_at = now or dt_util.utcnow()
    if not isinstance(payload, dict):
        return TraceyData(token_present=True, tracking_url=tracking_url, fetched_at=fetched_at)
    phase = payload.get("traceyPhase")
    phase = phase if isinstance(phase, str) and phase else None
    if phase is None or phase == INVALID_PHASE:
        # A dead link is a definite "no active delivery", not an error.
        return TraceyData(
            token_present=True,
            phase=phase,
            tracking_url=tracking_url,
            fetched_at=fetched_at,
            raw=payload,
        )
    stops = payload.get("amountOfStopsBefore")
    eta_raw = payload.get("estimatedTimeOfArrival")
    eta = dt_util.parse_datetime(eta_raw) if isinstance(eta_raw, str) else None
    if eta is not None and eta.tzinfo is None:
        # The capture's ETA parsed cleanly with HA's as_datetime, implying an offset is
        # normally present; if one is ever missing, UTC is the least-wrong assumption for a
        # cloud-function timestamp, and a TIMESTAMP entity requires an aware value.
        eta = eta.replace(tzinfo=dt_util.UTC)
    return TraceyData(
        active=True,
        token_present=True,
        phase=phase,
        message=payload.get("personalCustomerMessage") or None,
        driver_name=payload.get("driverName") or None,
        stops_before=(
            int(stops) if isinstance(stops, (int, float)) and not isinstance(stops, bool) else None
        ),
        eta=eta,
        delivery_time=payload.get("deliveryTime") or None,
        driver_location=_location(payload.get("driverLocation")),
        customer_location=_location(payload.get("customerLocation")),
        tracking_url=tracking_url,
        fetched_at=fetched_at,
        raw=payload,
    )


class HelloFreshTraceyCoordinator(DataUpdateCoordinator[TraceyData]):
    """Poll the Tracey endpoint on its own (fast) cadence, separate from account polling.

    Reads the current tracking token from the main coordinator's data on every tick, so it
    follows the account's tracked order without duplicating any account logic. The interval
    self-adjusts: minutes while a delivery is live, half-hourly otherwise — the idle tick
    performs a single unauthenticated GET at most (none when no token exists at all).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        main_coordinator,
        config_entry,
    ) -> None:
        """Initialize with the shared aiohttp session and the account coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            # Passed explicitly (newer HA warns when left to context detection); the shared
            # entity base (device info, pinned entity ids) reads it as ``config_entry`` too.
            config_entry=config_entry,
            name=f"{DOMAIN}_tracey",
            update_interval=IDLE_UPDATE_INTERVAL,
            always_update=False,
        )
        self._session = session
        self._main_coordinator = main_coordinator
        self._last_fetch_monotonic: float | None = None

    def _current_tracking_url(self) -> str | None:
        """Return the tracked order's tracking URL from the latest account data."""
        data = getattr(self._main_coordinator, "data", None)
        order = getattr(data, "tracked_order", None) if data is not None else None
        url = getattr(order, "tracking_url", None)
        if url:
            return url
        # Fall back to the next order — early in the week the tracked order may not have
        # its link yet while the upcoming one already does.
        order = getattr(data, "next_order", None) if data is not None else None
        return getattr(order, "tracking_url", None)

    async def _async_update_data(self) -> TraceyData:
        """Fetch the live tracking snapshot, or an idle snapshot when nothing is trackable."""
        tracking_url = self._current_tracking_url()
        token = tracey_token(tracking_url)
        if token is None:
            self.update_interval = IDLE_UPDATE_INTERVAL
            return TraceyData(fetched_at=dt_util.utcnow())

        try:
            async with self._session.get(
                TRACEY_ENDPOINT,
                params={"token": token, "screenWidth": "500"},
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Origin": TRACEY_SITE,
                    "Referer": f"{TRACEY_SITE}/",
                    "User-Agent": _BROWSER_USER_AGENT,
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                if response.status != 200:
                    raise UpdateFailed(f"Tracey endpoint returned HTTP {response.status}")
                payload = await response.json(content_type=None)
        except UpdateFailed:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise UpdateFailed(f"Tracey endpoint unreachable: {err}") from err

        self._last_fetch_monotonic = time.monotonic()
        data = parse_tracey_payload(payload, tracking_url=tracking_url)
        self.update_interval = (
            ACTIVE_UPDATE_INTERVAL if data.phase in ACTIVE_PHASES else IDLE_UPDATE_INTERVAL
        )
        return data

    async def async_fetch_latest(self) -> TraceyData:
        """Return a fresh snapshot for the tracking card, throttled to one fetch a minute.

        The card refetches on its own timer while a delivery is live; reusing a snapshot
        younger than the floor keeps multiple open dashboards from multiplying requests.
        A failed refresh degrades to the last snapshot rather than raising, so the card
        keeps rendering the most recent known state.
        """
        age = (
            time.monotonic() - self._last_fetch_monotonic
            if self._last_fetch_monotonic is not None
            else None
        )
        if self.data is not None and age is not None and age < MIN_SERVICE_FETCH_INTERVAL_SECONDS:
            return self.data
        await self.async_refresh()
        return self.data if self.data is not None else TraceyData(fetched_at=dt_util.utcnow())
