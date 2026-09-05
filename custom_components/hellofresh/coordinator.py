"""Data update coordinator for HelloFresh."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceResponse, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    HelloFreshAccountData,
    HelloFreshAuthError,
    HelloFreshClient,
    HelloFreshError,
)
from .const import (
    CONF_DELIVERY_WATCH_INTERVAL_MINUTES,
    CONF_SHOW_DATA_QUALITY_ISSUES,
    DEFAULT_DELIVERY_WATCH_INTERVAL_MINUTES,
    DEFAULT_SHOW_DATA_QUALITY_ISSUES,
    DOMAIN,
)
from .issues import (
    async_create_account_data_issue,
    async_create_account_menu_fallback_issue,
    async_create_payload_shape_changed_issue,
    async_delete_account_data_issue,
    async_delete_account_menu_fallback_issue,
    async_delete_payload_shape_changed_issue,
    async_delete_write_actions_issue,
)

_LOGGER = logging.getLogger(__name__)

# Access tokens are short-lived (typically 30 min) while the data poll interval can be
# several hours. A dedicated timer refreshes the token well before it expires, decoupled
# from data polling, so on-demand actions between polls never hit a dead token.
DEFAULT_TOKEN_LIFETIME_SECONDS = 1800
# The client refreshes once a token is past half its life, so the refresh window is wide
# (~half the lifetime). The timer must tick several times inside that window so a refresh
# always happens before expiry, even with clock jitter or a slow event loop. Polling at a
# quarter of the lifetime guarantees at least one tick lands in the back-half window.
TOKEN_REFRESH_LIFETIME_FRACTION = 0.25
MIN_TOKEN_REFRESH_INTERVAL = timedelta(minutes=2)
MAX_TOKEN_REFRESH_INTERVAL = timedelta(minutes=10)

DEFAULT_DELIVERY_WATCH_INTERVAL = timedelta(minutes=DEFAULT_DELIVERY_WATCH_INTERVAL_MINUTES)
# Carrier statuses (SCM tracking vocabulary) that mean a box is physically on its way.
SHIPMENT_ACTIVE_STATUSES = frozenset({"in_transit", "out_for_delivery"})
# Lifecycle `state` values from the deliveries payload that mean the same thing.
WEEK_SHIPPED_STATUSES = frozenset({"ON_THE_WAY", "SHIPPED", "IN_TRANSIT", "OUT_FOR_DELIVERY"})

# Event types fired by event.delivery_events — the lifecycle transitions an automation
# otherwise has to reconstruct by diffing sensor states between polls.
EVENT_BOX_SHIPPED = "box_shipped"
EVENT_BOX_DELIVERED = "box_delivered"
EVENT_DELIVERY_FAILED = "delivery_failed"
EVENT_WEEK_SKIPPED = "week_skipped"
EVENT_WEEK_UNSKIPPED = "week_unskipped"
EVENT_SELECTION_LOCKED = "selection_locked"
EVENT_MENU_PUBLISHED = "menu_published"
DELIVERY_EVENT_TYPES: list[str] = [
    EVENT_BOX_SHIPPED,
    EVENT_BOX_DELIVERED,
    EVENT_DELIVERY_FAILED,
    EVENT_WEEK_SKIPPED,
    EVENT_WEEK_UNSKIPPED,
    EVENT_SELECTION_LOCKED,
    EVENT_MENU_PUBLISHED,
]
# How many fired events the coordinator remembers for late-subscribing entities.
MAX_REMEMBERED_EVENTS = 50

SnapshotKey = tuple[str, str | None]


def delivery_snapshot(data: HelloFreshAccountData) -> dict[SnapshotKey, dict[str, Any]]:
    """Reduce account data to the per-week lifecycle facts the event entity diffs.

    Keyed by (week id, subscription id) — two subscriptions share ISO week ids. Plain values
    only, so a snapshot survives the in-place mutation the delivery watch performs on the
    live data object.
    """
    orders_by_week: dict[SnapshotKey, Any] = {}
    for order in data.orders:
        orders_by_week.setdefault((order.week_id, order.subscription_id), order)
    snapshot: dict[SnapshotKey, dict[str, Any]] = {}
    for week in data.weeks:
        order = orders_by_week.get((week.week_id, week.subscription_id)) or orders_by_week.get(
            (week.week_id, None)
        )
        status = (week.status or "").strip().upper()
        tracking_status = (order.tracking_status or "").strip().lower() if order else ""
        snapshot[(week.week_id, week.subscription_id)] = {
            "display_name": week.display_name,
            "delivery_date": week.delivery_date.isoformat() if week.delivery_date else None,
            "status": status,
            "delivered": week.delivered_at is not None or status == "DELIVERED",
            "delivered_at": week.delivered_at.isoformat() if week.delivered_at else None,
            "shipped": status in WEEK_SHIPPED_STATUSES
            or tracking_status in SHIPMENT_ACTIVE_STATUSES,
            "is_skipped": week.is_skipped,
            "is_editable": week.is_editable,
            "has_menu": bool(week.recipes),
            "tracking_status": tracking_status or None,
            "carrier": order.carrier if order else None,
            "tracking_number": order.tracking_number if order else None,
            "tracking_url": order.tracking_url if order else None,
            "estimated_delivery": (
                order.estimated_delivery.isoformat() if order and order.estimated_delivery else None
            ),
        }
    return snapshot


def delivery_transitions(
    old: dict[SnapshotKey, dict[str, Any]],
    new: dict[SnapshotKey, dict[str, Any]],
) -> list[tuple[str, dict[str, Any]]]:
    """Return the (event type, attributes) pairs implied by two consecutive snapshots.

    Only transitions count: a week that is already delivered when first seen fires nothing,
    and a week that vanishes from the window fires nothing. A week that appears for the
    first time already carrying a menu is a newly published menu.
    """
    events: list[tuple[str, dict[str, Any]]] = []
    for key, cur in new.items():
        prev = old.get(key)
        base: dict[str, Any] = {
            "week_id": key[0],
            "subscription_id": key[1],
            "display_name": cur["display_name"],
            "delivery_date": cur["delivery_date"],
        }
        upcoming = not cur["delivered"] and not cur["is_skipped"]
        if prev is None:
            if cur["has_menu"] and upcoming:
                events.append((EVENT_MENU_PUBLISHED, base))
            continue
        if not prev["has_menu"] and cur["has_menu"] and upcoming:
            events.append((EVENT_MENU_PUBLISHED, base))
        if not prev["is_skipped"] and cur["is_skipped"]:
            events.append((EVENT_WEEK_SKIPPED, base))
        if prev["is_skipped"] and not cur["is_skipped"]:
            events.append((EVENT_WEEK_UNSKIPPED, base))
        if prev["is_editable"] and not cur["is_editable"] and not cur["is_skipped"]:
            events.append((EVENT_SELECTION_LOCKED, base))
        if cur["shipped"] and not prev["shipped"] and not cur["delivered"]:
            events.append(
                (
                    EVENT_BOX_SHIPPED,
                    {
                        **base,
                        "tracking_status": cur["tracking_status"],
                        "carrier": cur["carrier"],
                        "tracking_number": cur["tracking_number"],
                        "tracking_url": cur["tracking_url"],
                        "estimated_delivery": cur["estimated_delivery"],
                    },
                )
            )
        if not prev["delivered"] and cur["delivered"]:
            events.append(
                (
                    EVENT_BOX_DELIVERED,
                    {**base, "delivered_at": cur["delivered_at"], "carrier": cur["carrier"]},
                )
            )
        if prev["status"] != "FAILED" and cur["status"] == "FAILED":
            events.append((EVENT_DELIVERY_FAILED, base))
    return events


def delivery_in_progress(data: HelloFreshAccountData, today: Any) -> bool:
    """True while a box is due or on the road and the carrier has not confirmed delivery."""
    for week in data.weeks:
        if week.is_skipped or week.is_paused or week.delivery_date is None:
            continue
        status = (week.status or "").strip().upper()
        if week.delivered_at is not None or status == "DELIVERED":
            continue
        if status in WEEK_SHIPPED_STATUSES:
            return True
        if today - timedelta(days=1) <= week.delivery_date <= today:
            return True
    return any(
        (order.tracking_status or "").strip().lower() in SHIPMENT_ACTIVE_STATUSES
        for order in data.upcoming_orders
    )


def _token_refresh_interval(lifetime_seconds: int | None) -> timedelta:
    """Return how often to proactively refresh, derived from the token lifetime.

    Must stay well below the token lifetime so a tick reliably lands in the refresh
    window (the back half of the token's life) before it can expire.
    """
    lifetime = lifetime_seconds or DEFAULT_TOKEN_LIFETIME_SECONDS
    interval = timedelta(seconds=max(int(lifetime * TOKEN_REFRESH_LIFETIME_FRACTION), 0))
    if interval < MIN_TOKEN_REFRESH_INTERVAL:
        return MIN_TOKEN_REFRESH_INTERVAL
    if interval > MAX_TOKEN_REFRESH_INTERVAL:
        return MAX_TOKEN_REFRESH_INTERVAL
    return interval


class HelloFreshDataUpdateCoordinator(DataUpdateCoordinator[HelloFreshAccountData]):
    """Coordinate HelloFresh account updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: HelloFreshClient,
        config_entry,
        update_interval: timedelta,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=update_interval,
            always_update=False,
        )
        self.client = client
        self.config_entry = config_entry
        self._cancel_token_refresh = None
        self._cancel_delivery_watch = None
        self._delivery_watch_interval = DEFAULT_DELIVERY_WATCH_INTERVAL
        self._refresh_in_progress = False
        self._last_full_refresh: datetime | None = None
        # Lifecycle events detected between polls, as (serial, type, attributes). The event
        # entity consumes them by serial so a late subscriber never replays history and a
        # listener update with no new transitions fires nothing.
        self.delivery_events: list[tuple[int, str, dict[str, Any]]] = []
        self._event_serial = 0
        # Memoized get_weeks serialization, keyed by the data object it was built from.
        # Each poll assigns a fresh HelloFreshAccountData, so a new object is a cache miss;
        # multiple cards calling get_weeks within one poll cycle reuse one build. The key
        # MUST be the object itself (compared with ``is``), not ``id(data)``: a bare id is
        # a memory address that CPython reuses once the old object is freed, which let a
        # later poll's data collide with the cached key and serve stale weeks to the cards.
        self._weeks_response_cache: tuple[HelloFreshAccountData, ServiceResponse] | None = None
        # Ticked-off prep-list ingredients, keyed (week_id, ingredient_key). SHARED between
        # both prep-list entities (the slots are positional, so a week moving from "next
        # week" to "this week" must arrive with its ticks — a per-entity set stranded them
        # on the old slot), and restored across restarts by each entity's RestoreEntity data.
        self.prep_completed: set[tuple[str, str]] = set()

    def get_weeks_response(
        self,
        build: Callable[[], ServiceResponse],
    ) -> ServiceResponse:
        """Return the cached full get_weeks response for the current data, or build it once.

        ``build`` produces the response for ``self.data``; it is only invoked on a cache miss.
        The cache is invalidated automatically when the coordinator swaps in new data (the
        key is the data object itself), so a card fetching after a poll gets fresh content.
        """
        data = self.data
        if self._weeks_response_cache is not None and self._weeks_response_cache[0] is data:
            return self._weeks_response_cache[1]
        response = build()
        self._weeks_response_cache = (data, response)
        return response

    @callback
    def async_start_token_refresh(self) -> None:
        """Start a periodic, poll-independent token refresh timer."""
        if self._cancel_token_refresh is not None:
            return
        interval = _token_refresh_interval(self.client.token_lifetime_seconds)
        _LOGGER.debug("HelloFresh proactive token refresh scheduled every %s", interval)
        self._cancel_token_refresh = async_track_time_interval(
            self.hass,
            self._async_refresh_token,
            interval,
        )
        self.config_entry.async_on_unload(self.async_stop_token_refresh)

    @callback
    def async_stop_token_refresh(self) -> None:
        """Stop the periodic token refresh timer."""
        if self._cancel_token_refresh is not None:
            self._cancel_token_refresh()
            self._cancel_token_refresh = None

    async def _async_refresh_token(self, _now=None) -> None:
        """Refresh the access token ahead of expiry, swallowing transient failures."""
        try:
            await self.client.async_ensure_token_fresh()
        except HelloFreshAuthError as err:
            # A dead refresh token / rejected credentials cannot heal on their own. Stop the
            # timer and start reauth NOW: leaving it running re-submitted the stored (stale)
            # password every 2-10 minutes indefinitely — the classic pattern that trips
            # provider rate limits or locks the account. The timer restarts when the entry
            # reloads after a successful reauth.
            _LOGGER.warning(
                "HelloFresh proactive token refresh failed (%s); pausing token refresh "
                "and requesting reauthentication",
                err,
            )
            self.async_stop_token_refresh()
            self.config_entry.async_start_reauth(self.hass)
        except HelloFreshError as err:
            _LOGGER.debug("HelloFresh proactive token refresh skipped (transient): %s", err)

    # ---- delivery-day watch + lifecycle events -------------------------------------------

    @property
    def delivery_watch_interval(self) -> timedelta | None:
        """The configured watch cadence, or None when the option turns the watch off."""
        minutes = self.config_entry.options.get(
            CONF_DELIVERY_WATCH_INTERVAL_MINUTES, DEFAULT_DELIVERY_WATCH_INTERVAL_MINUTES
        )
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            minutes = DEFAULT_DELIVERY_WATCH_INTERVAL_MINUTES
        if minutes <= 0:
            return None
        return timedelta(minutes=minutes)

    @callback
    def async_start_delivery_watch(self) -> None:
        """Start the poll-independent delivery-day watch timer (unless the option is 0)."""
        if self._cancel_delivery_watch is not None:
            return
        interval = self.delivery_watch_interval
        if interval is None:
            _LOGGER.debug("HelloFresh delivery-day watch disabled by option")
            return
        self._delivery_watch_interval = interval
        _LOGGER.debug("HelloFresh delivery-day watch scheduled every %s", interval)
        self._cancel_delivery_watch = async_track_time_interval(
            self.hass,
            self._async_delivery_watch_tick,
            interval,
        )
        self.config_entry.async_on_unload(self.async_stop_delivery_watch)

    @callback
    def async_stop_delivery_watch(self) -> None:
        """Stop the delivery-day watch timer."""
        if self._cancel_delivery_watch is not None:
            self._cancel_delivery_watch()
            self._cancel_delivery_watch = None

    def delivery_in_progress(self) -> bool:
        """True while a box is due or on the road and not yet confirmed delivered."""
        data = self.data
        if data is None:
            return False
        return delivery_in_progress(data, dt_util.now().date())

    async def _async_delivery_watch_tick(self, _now=None) -> None:
        """Re-read shipment state when a box is due; a no-op on every other day."""
        data = self.data
        if data is None or self._refresh_in_progress or not self.delivery_in_progress():
            return
        if (
            self._last_full_refresh is not None
            and dt_util.utcnow() - self._last_full_refresh < self._delivery_watch_interval / 2
        ):
            return  # a full poll just ran; nothing new to learn yet
        old_snapshot = delivery_snapshot(data)
        try:
            changed = await self.client.async_refresh_delivery_status(data)
        except HelloFreshAuthError as err:
            _LOGGER.warning(
                "HelloFresh delivery watch failed (%s); requesting reauthentication", err
            )
            self.config_entry.async_start_reauth(self.hass)
            return
        except HelloFreshError as err:
            _LOGGER.debug("HelloFresh delivery watch skipped (transient): %s", err)
            return
        if not changed:
            return
        self._record_transitions(old_snapshot, data)
        # The data object was mutated in place, so the identity-keyed get_weeks cache is stale.
        self._weeks_response_cache = None
        self.async_update_listeners()

    def _record_transitions(
        self,
        old_snapshot: dict[SnapshotKey, dict[str, Any]] | None,
        data: HelloFreshAccountData,
    ) -> None:
        """Diff the previous snapshot against ``data`` and queue the implied events."""
        if old_snapshot is None:
            return
        for event_type, attributes in delivery_transitions(old_snapshot, delivery_snapshot(data)):
            self._event_serial += 1
            self.delivery_events.append((self._event_serial, event_type, attributes))
        del self.delivery_events[:-MAX_REMEMBERED_EVENTS]

    @property
    def event_serial(self) -> int:
        """Serial of the most recently recorded lifecycle event (0 when none yet)."""
        return self._event_serial

    async def _async_update_data(self) -> HelloFreshAccountData:
        """Fetch latest data from HelloFresh."""
        old_snapshot = delivery_snapshot(self.data) if self.data is not None else None
        self._refresh_in_progress = True
        try:
            data = await self.client.async_get_account_data()
        except HelloFreshAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except HelloFreshError as err:
            raise UpdateFailed(str(err)) from err
        finally:
            self._refresh_in_progress = False
        self._last_full_refresh = dt_util.utcnow()
        self._record_transitions(old_snapshot, data)

        # Data-quality Repairs warnings are advisory and user-suppressible: with the
        # option off, nothing is created and anything already raised is cleared, giving
        # users a way to dismiss warnings that would otherwise persist between polls.
        show_issues = self.config_entry.options.get(
            CONF_SHOW_DATA_QUALITY_ISSUES, DEFAULT_SHOW_DATA_QUALITY_ISSUES
        )

        if show_issues and not data.account_data_available:
            async_create_account_data_issue(
                self.hass,
                self.config_entry.entry_id,
                self.config_entry.title,
            )
        else:
            async_delete_account_data_issue(self.hass, self.config_entry.entry_id)

        if show_issues and data.capabilities.using_public_menu_fallback:
            async_create_account_menu_fallback_issue(
                self.hass,
                self.config_entry.entry_id,
                self.config_entry.title,
            )
        else:
            async_delete_account_menu_fallback_issue(self.hass, self.config_entry.entry_id)

        if show_issues and data.capabilities.payload_shape_changed:
            async_create_payload_shape_changed_issue(
                self.hass,
                self.config_entry.entry_id,
                self.config_entry.title,
            )
        else:
            async_delete_payload_shape_changed_issue(self.hass, self.config_entry.entry_id)

        if not show_issues:
            # The write-actions warning is raised outside the coordinator (service and
            # switch handlers) and has no self-clearing condition, so sweep it here too.
            async_delete_write_actions_issue(self.hass, self.config_entry.entry_id)

        return data


# The typed config entry: `entry.runtime_data` carries the coordinator (HA's modern
# per-entry storage, replacing hass.data[DOMAIN][entry_id]). Platforms annotate their
# async_setup_entry with this so the coordinator comes out typed.
HelloFreshConfigEntry = ConfigEntry[HelloFreshDataUpdateCoordinator]
