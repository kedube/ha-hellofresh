"""Data update coordinator for HelloFresh."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    HelloFreshAccountData,
    HelloFreshAuthError,
    HelloFreshClient,
    HelloFreshError,
    HelloFreshInvalidRequestError,
    HelloFreshNotImplementedError,
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
    async_create_write_actions_issue,
    async_delete_account_data_issue,
    async_delete_account_menu_fallback_issue,
    async_delete_payload_shape_changed_issue,
    async_delete_write_actions_issue,
    async_update_payment_problem_issue,
)
from .models import HelloFreshWeek
from .parsers import as_utc

if TYPE_CHECKING:
    # Annotations only: the Tracey module names this one's classes too, and neither module
    # needs the other at runtime. Setup builds both of these (see async_setup_entry).
    from .meal_plan import MealPlan
    from .tracey import HelloFreshTraceyCoordinator

_LOGGER = logging.getLogger(__name__)

# What get_weeks caches per poll: each week with its serialized payload, kept together so the
# fields that change with the clock can be added to the right week on every call.
type SerializedWeeks = list[tuple[HelloFreshWeek, dict[str, Any]]]

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
# How long past its delivery date a box that still reads "shipped" keeps the watch running.
# A box that is merely late is confirmed within a day or two; one whose status never moves
# again (lost, or the carrier never posted its last scan) would otherwise hold the watch and
# every open card on the fast cadence until the week left the data window, about 12 weeks.
SHIPPED_WATCH_GRACE = timedelta(days=3)

# Event types fired by event.delivery_events — the lifecycle transitions an automation
# otherwise has to reconstruct by diffing sensor states between polls.
EVENT_BOX_SHIPPED = "box_shipped"
EVENT_BOX_DELIVERED = "box_delivered"
EVENT_DELIVERY_FAILED = "delivery_failed"
EVENT_WEEK_SKIPPED = "week_skipped"
EVENT_WEEK_UNSKIPPED = "week_unskipped"
EVENT_SELECTION_LOCKED = "selection_locked"
EVENT_MENU_PUBLISHED = "menu_published"
EVENT_BOX_DELAYED = "box_delayed"
EVENT_BOX_EARLY = "box_early"
EVENT_SELECTION_DEADLINE_APPROACHING = "selection_deadline_approaching"
DELIVERY_EVENT_TYPES: list[str] = [
    EVENT_BOX_SHIPPED,
    EVENT_BOX_DELIVERED,
    EVENT_DELIVERY_FAILED,
    EVENT_WEEK_SKIPPED,
    EVENT_WEEK_UNSKIPPED,
    EVENT_SELECTION_LOCKED,
    EVENT_MENU_PUBLISHED,
    EVENT_BOX_DELAYED,
    EVENT_BOX_EARLY,
    EVENT_SELECTION_DEADLINE_APPROACHING,
]
# How many fired events the coordinator remembers for late-subscribing entities.
MAX_REMEMBERED_EVENTS = 50

# The events a week fires at most once, ever. Unlike a skip, which can be undone and done
# again, each says something that is news only the first time, so what has fired is kept
# across restarts (see FiredEvents).
ONCE_PER_WEEK_EVENTS = frozenset(
    {
        EVENT_BOX_DELAYED,
        EVENT_BOX_EARLY,
        EVENT_SELECTION_DEADLINE_APPROACHING,
        EVENT_SELECTION_LOCKED,
    }
)
# The week's ``subStatus`` that means HelloFresh moved its box, and the event that says so.
MOVED_BOX_EVENTS = {"DELAYED": EVENT_BOX_DELAYED, "EARLY": EVENT_BOX_EARLY}
# How long before a week's selection deadline selection_deadline_approaching fires.
SELECTION_DEADLINE_NOTICE = timedelta(hours=24)

FIRED_EVENTS_STORAGE_VERSION = 1
_FIRED_EVENTS_SAVE_DELAY = 1

SnapshotKey = tuple[str, str | None]


def delivery_snapshot(data: HelloFreshAccountData) -> dict[SnapshotKey, dict[str, Any]]:
    """Reduce account data to the per-week lifecycle facts the event entity diffs.

    Keyed by (week id, subscription id) — two subscriptions share ISO week ids. Plain values
    only, so a snapshot survives the in-place mutation the delivery watch performs on the
    live data object.
    """
    snapshot: dict[SnapshotKey, dict[str, Any]] = {}
    for week in data.weeks:
        # The week's own order, and of several records for it the one carrying the shipment
        # (the tracked-shipment sensor's ranking). Taking the first record let a state-only
        # one hide the carrier's status, so a box out for delivery went unannounced.
        order = data.get_order_for_week(week.week_id, week.subscription_id)
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
            "selection_deadline": (
                week.selection_deadline.isoformat() if week.selection_deadline else None
            ),
            "sub_status": (week.sub_status or "").strip().upper() or None,
            "revised_delivery_date": (
                week.revised_delivery_date.isoformat() if week.revised_delivery_date else None
            ),
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
    first time already carrying a menu is a newly published menu. Whether an event that a
    week fires only once has fired before is the coordinator's to decide, not this.
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
            events.append((EVENT_SELECTION_LOCKED, {**base, "deadline": cur["selection_deadline"]}))
        moved = MOVED_BOX_EVENTS.get(cur["sub_status"] or "")
        if moved is not None and prev["sub_status"] != cur["sub_status"] and upcoming:
            events.append((moved, {**base, "revised_delivery_date": cur["revised_delivery_date"]}))
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


def delivery_in_progress(data: HelloFreshAccountData, today: date) -> bool:
    """True while a box is due or on the road and the carrier has not confirmed delivery.

    A box counts from the moment it ships, however early, until it is confirmed or its
    delivery date is more than ``SHIPPED_WATCH_GRACE`` behind us.
    """
    for week in data.weeks:
        if week.is_skipped or week.is_paused or week.delivery_date is None:
            continue
        status = (week.status or "").strip().upper()
        if week.delivered_at is not None or status == "DELIVERED":
            continue
        if status in WEEK_SHIPPED_STATUSES and week.delivery_date >= today - SHIPPED_WATCH_GRACE:
            return True
        if today - timedelta(days=1) <= week.delivery_date <= today:
            return True
    return any(
        (order.tracking_status or "").strip().lower() in SHIPMENT_ACTIVE_STATUSES
        for order in data.upcoming_orders
    )


def fired_events_storage_key(entry_id: str) -> str:
    """The storage file for one account's record of the once-a-week events."""
    return f"{DOMAIN}.delivery_events.{entry_id}"


class FiredEvents:
    """Which once-a-week events (ONCE_PER_WEEK_EVENTS) each week has fired.

    Kept in Home Assistant's storage, one small file per account, so a restart does not
    announce a delay or a deadline again, and a deadline HelloFresh moves does not arm a
    second notice for the same week. Weeks that leave the account's data are forgotten.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, FIRED_EVENTS_STORAGE_VERSION, fired_events_storage_key(entry_id)
        )
        self._fired: dict[str, set[str]] = {}
        # Whether a change is waiting on its delayed save; the unload flush writes only then.
        self._unsaved = False

    @staticmethod
    def _week(key: SnapshotKey) -> str:
        week_id, subscription_id = key
        return f"{subscription_id or ''}|{week_id}"

    async def async_load(self) -> None:
        """Read what fired before the last restart."""
        stored = await self._store.async_load()
        weeks = stored.get("weeks") if isinstance(stored, dict) else None
        self._fired = {
            str(week): {kind for kind in kinds if kind in ONCE_PER_WEEK_EVENTS}
            for week, kinds in (weeks.items() if isinstance(weeks, dict) else ())
            if isinstance(kinds, list)
        }

    def has_fired(self, key: SnapshotKey, kind: str) -> bool:
        """True when ``kind`` has already fired for the week ``key``."""
        return kind in self._fired.get(self._week(key), ())

    def mark(self, key: SnapshotKey, kind: str) -> None:
        """Remember that ``kind`` fired for the week ``key``."""
        self._fired.setdefault(self._week(key), set()).add(kind)
        self._schedule_save()

    def prune(self, keys: Iterable[SnapshotKey]) -> None:
        """Forget the weeks that are no longer among ``keys``."""
        kept = {self._week(key) for key in keys}
        stale = [week for week in self._fired if week not in kept]
        for week in stale:
            del self._fired[week]
        if stale:
            self._schedule_save()

    def _schedule_save(self) -> None:
        self._unsaved = True
        self._store.async_delay_save(self._data_to_save, _FIRED_EVENTS_SAVE_DELAY)

    def _data_to_save(self) -> dict[str, Any]:
        self._unsaved = False
        return {"weeks": {week: sorted(kinds) for week, kinds in self._fired.items()}}

    async def async_flush(self) -> None:
        """Write a change still waiting on its delayed save now (the entry is unloading)."""
        if self._unsaved:
            await self._store.async_save(self._data_to_save())


async def async_remove_fired_events(hass: HomeAssistant, entry_id: str) -> None:
    """Delete an account's record of fired events (the integration entry was removed)."""
    await Store(
        hass, FIRED_EVENTS_STORAGE_VERSION, fired_events_storage_key(entry_id)
    ).async_remove()


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


# The typed config entry: `entry.runtime_data` carries the coordinator (HA's modern
# per-entry storage, replacing hass.data[DOMAIN][entry_id]). Platforms annotate their
# async_setup_entry with this so the coordinator comes out typed.
type HelloFreshConfigEntry = ConfigEntry[HelloFreshDataUpdateCoordinator]


@callback
def async_loaded_coordinators(hass: HomeAssistant) -> list[HelloFreshDataUpdateCoordinator]:
    """Every loaded HelloFresh entry's coordinator.

    Only entries Home Assistant reports as loaded count. Having ``runtime_data`` is not
    enough: setup stores it before the platforms are up, and Home Assistant leaves it in place
    when setup fails after that point or an unload fails, so it can name a coordinator whose
    timers have already stopped. Services and intents must not act on one of those.
    """
    return [
        entry.runtime_data
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]


@callback
def async_client_error(
    hass: HomeAssistant, config_entry: ConfigEntry, err: HelloFreshError
) -> HomeAssistantError:
    """Turn a failed HelloFresh call made for a user's action into the error to raise.

    Every service, the skip switch and the two selects share this one rule:

    * HelloFresh rejected the sign-in: start reauthentication. No retry can succeed until
      the user signs in again, and nothing else would tell them to.
    * HelloFresh has no endpoint for the action on this account: raise the write-actions
      repair, because that is permanent too.
    * The change can't work as asked (an unknown or locked week, a box size HelloFresh doesn't
      sell): a ServiceValidationError, since the input is the problem and nothing was sent.
    * Anything else (a timeout, a 5xx, a rate limit) is only reported. It passes by itself,
      and a repair or a sign-in prompt for it would be a false alarm.
    """
    if isinstance(err, HelloFreshAuthError):
        config_entry.async_start_reauth(hass)
    elif isinstance(err, HelloFreshNotImplementedError):
        async_create_write_actions_issue(hass, config_entry.entry_id, config_entry.title)
    elif isinstance(err, HelloFreshInvalidRequestError):
        # Refused before anything was sent, because of what was asked: the caller's input.
        return ServiceValidationError(str(err))
    return HomeAssistantError(str(err))


@callback
def async_write_succeeded(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Clear the write-actions repair: a write endpoint just accepted a request."""
    async_delete_write_actions_issue(hass, config_entry.entry_id)


class HelloFreshDataUpdateCoordinator(DataUpdateCoordinator[HelloFreshAccountData]):
    """Coordinate HelloFresh account updates."""

    config_entry: HelloFreshConfigEntry
    # Which once-a-week events have fired (FiredEvents), loaded by setup before the first poll.
    # None until then; events are still recorded without it, only not remembered.
    fired_events: FiredEvents | None = None

    def __init__(
        self,
        hass: HomeAssistant,
        client: HelloFreshClient,
        config_entry: HelloFreshConfigEntry,
        update_interval: timedelta,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=update_interval,
            always_update=False,
        )
        self.client = client
        # The platforms setup forwarded for this entry, so unload asks for exactly those.
        # The list cannot be worked out again at unload: turning the prep lists off reloads
        # the entry, and by then the option already reads off while the to-do entities are
        # still loaded.
        self.platforms: list[Platform] = []
        self._cancel_token_refresh: CALLBACK_TYPE | None = None
        # Callbacks for the two token-lifetime sensors. Their values come from the clock and
        # the client, not from account data, so a poll that returns unchanged data never
        # rewrites them; they follow the token itself instead.
        self._token_listeners: list[CALLBACK_TYPE] = []
        # Live last-mile tracking (tracey.HelloFreshTraceyCoordinator), mounted by setup
        # only for countries in TRACEY_COUNTRIES; None everywhere else. Held here so the
        # sensor platform and the get_delivery_tracking service can reach it without a
        # second hass.data map.
        self.tracey: HelloFreshTraceyCoordinator | None = None
        # The Week tab's meal plan (meal_plan.MealPlan), mounted by setup for every entry.
        # Its calendar, sensors and services reach it through here.
        self.meal_plan: MealPlan | None = None
        self._cancel_delivery_watch: CALLBACK_TYPE | None = None
        self._delivery_watch_interval = DEFAULT_DELIVERY_WATCH_INTERVAL
        # Held by a full poll and by a delivery-watch tick, so the two never run at once. A
        # poll that started in the middle of a tick used to take its "before" snapshot ahead
        # of the tick's in-place change, and both then recorded the same box_shipped or
        # box_delivered event.
        self._refresh_lock = asyncio.Lock()
        self._last_full_refresh: datetime | None = None
        # Lifecycle events detected between polls, as (serial, type, attributes). The event
        # entity consumes them by serial so a late subscriber never replays history and a
        # listener update with no new transitions fires nothing.
        self.delivery_events: list[tuple[int, str, dict[str, Any]]] = []
        self._event_serial = 0
        # The selection-deadline events still to come, armed at their moment: (week, event
        # type) -> (when, the deadline it was armed for, the timer's cancel).
        self._deadline_timers: dict[
            tuple[SnapshotKey, str], tuple[datetime, datetime, CALLBACK_TYPE]
        ] = {}
        self._remove_deadline_listener: CALLBACK_TYPE | None = None
        # Memoized get_weeks serialization, keyed by the data object it was built from.
        # Each poll assigns a fresh HelloFreshAccountData, so a new object is a cache miss;
        # multiple cards calling get_weeks within one poll cycle reuse one build. The key
        # MUST be the object itself (compared with ``is``), not ``id(data)``: a bare id is
        # a memory address that CPython reuses once the old object is freed, which let a
        # later poll's data collide with the cached key and serve stale weeks to the cards.
        self._weeks_response_cache: tuple[HelloFreshAccountData, SerializedWeeks] | None = None
        # Ticked-off prep-list ingredients, keyed (week_id, ingredient_key). SHARED between
        # both prep-list entities (the slots are positional, so a week moving from "next
        # week" to "this week" must arrive with its ticks — a per-entity set stranded them
        # on the old slot), and restored across restarts by each entity's RestoreEntity data.
        self.prep_completed: set[tuple[str, str]] = set()
        # Recipe details the prep lists have fetched, keyed (recipe id, servings). Shared for
        # the same reason as the ticks: a week changing slot must not fetch its recipes
        # again. The prep lists drop the recipes no covered week selects any more.
        self.prep_recipe_details: dict[tuple[str, int | None], Any] = {}

    def get_weeks_response(
        self,
        build: Callable[[], SerializedWeeks],
    ) -> SerializedWeeks:
        """Return the cached get_weeks serialization for the current data, or build it once.

        ``build`` serializes the weeks of ``self.data``; it is only invoked on a cache miss.
        The cache is invalidated automatically when the coordinator swaps in new data (the
        key is the data object itself), so a card fetching after a poll gets fresh content.
        Only cache what follows from the data alone: a value that also depends on the clock
        (is a delivery in progress, is a week still editable) would be frozen until the next
        poll.
        """
        data = self.data
        if self._weeks_response_cache is not None and self._weeks_response_cache[0] is data:
            return self._weeks_response_cache[1]
        response = build()
        self._weeks_response_cache = (data, response)
        return response

    @property
    def last_full_refresh(self) -> datetime | None:
        """When the last successful full poll finished (UTC), or None before the first."""
        return self._last_full_refresh

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

    @callback
    def async_add_token_listener(self, update_callback: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Call ``update_callback`` when the token rotates and on every refresh-timer tick."""
        self._token_listeners.append(update_callback)

        @callback
        def _remove() -> None:
            if update_callback in self._token_listeners:
                self._token_listeners.remove(update_callback)

        return _remove

    @callback
    def async_token_updated(self) -> None:
        """Have the token-lifetime sensors read the token and the clock again."""
        for update_callback in list(self._token_listeners):
            update_callback()

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
        # Whatever happened, the time left has changed since the last tick. Without this the
        # sensors showed the value from the last poll that changed account data, hours ago,
        # and would not count down when refreshing stopped working.
        self.async_token_updated()

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
        # A held lock means a full poll (or an earlier, slow tick) is reading the same state
        # right now, so this tick has nothing to add.
        if self.data is None or self._refresh_lock.locked() or not self.delivery_in_progress():
            return
        if (
            self._last_full_refresh is not None
            and dt_util.utcnow() - self._last_full_refresh < self._delivery_watch_interval / 2
        ):
            return  # a full poll just ran; nothing new to learn yet
        async with self._refresh_lock:
            data = self.data
            old_snapshot = delivery_snapshot(data)
            try:
                changed = await self.client.async_refresh_delivery_status(data)
            except HelloFreshAuthError as err:
                # Same reasoning as the token timer: a rejected sign-in cannot heal by itself,
                # so stop asking every few minutes. The watch starts again when the entry
                # reloads after a successful reauth.
                _LOGGER.warning(
                    "HelloFresh delivery watch failed (%s); pausing the watch and requesting "
                    "reauthentication",
                    err,
                )
                self.async_stop_delivery_watch()
                self.config_entry.async_start_reauth(self.hass)
                return
            except HelloFreshError as err:
                _LOGGER.debug("HelloFresh delivery watch skipped (transient): %s", err)
                return
            if not changed:
                return
            self._record_transitions(old_snapshot, data)
            # The data object was mutated in place, so the identity-keyed get_weeks cache is
            # stale.
            self._weeks_response_cache = None
            self.async_update_listeners()

    def _record_transitions(
        self,
        old_snapshot: dict[SnapshotKey, dict[str, Any]] | None,
        data: HelloFreshAccountData,
    ) -> None:
        """Diff the previous snapshot against ``data`` and queue the implied events.

        Nothing is recorded without weeks to compare against: on the first poll, and on the
        first poll that brings account weeks after a menu-only start, every week would look
        newly published.
        """
        if not old_snapshot:
            return
        for event_type, attributes in delivery_transitions(old_snapshot, delivery_snapshot(data)):
            self._queue_event(event_type, attributes)

    def _queue_event(self, event_type: str, attributes: dict[str, Any]) -> bool:
        """Queue one event for the event entity; False when it is a repeat that is dropped.

        An event in ONCE_PER_WEEK_EVENTS that already fired for its week is not queued again:
        the deadline timer and a poll can both see a week lock, and a box can be delayed twice.
        """
        if event_type in ONCE_PER_WEEK_EVENTS and self.fired_events is not None:
            key = (attributes["week_id"], attributes["subscription_id"])
            if self.fired_events.has_fired(key, event_type):
                return False
            self.fired_events.mark(key, event_type)
        self._event_serial += 1
        self.delivery_events.append((self._event_serial, event_type, attributes))
        del self.delivery_events[:-MAX_REMEMBERED_EVENTS]
        return True

    # ---- selection-deadline events --------------------------------------------------------

    async def async_load_fired_events(self) -> None:
        """Read which once-a-week events fired before Home Assistant last stopped."""
        fired = FiredEvents(self.hass, self.config_entry.entry_id)
        await fired.async_load()
        self.fired_events = fired

    @callback
    def async_start_deadline_events(self) -> None:
        """Arm the selection-deadline events, and arm them again whenever the data changes.

        A poll only sees a week lock when it happens to run then, hours after the deadline at
        worst; a timer fires at the moment itself. Stopped when the entry unloads.
        """
        if self._remove_deadline_listener is not None:
            return
        self._remove_deadline_listener = self.async_add_listener(self._async_arm_deadline_events)
        self.config_entry.async_on_unload(self.async_stop_deadline_events)
        self._async_arm_deadline_events()

    @callback
    def async_stop_deadline_events(self) -> None:
        """Cancel every armed deadline timer and stop arming new ones."""
        if self._remove_deadline_listener is not None:
            self._remove_deadline_listener()
            self._remove_deadline_listener = None
        for _point, _deadline, cancel in self._deadline_timers.values():
            cancel()
        self._deadline_timers.clear()

    @callback
    def _async_arm_deadline_events(self) -> None:
        """Arm a timer for each open week's 24-hour notice and for its deadline.

        Armed afresh from the data on every update, so a deadline HelloFresh moves, a week
        skipped or one that has newly opened is followed. A moment already behind us is never
        armed: a deadline that passed while Home Assistant was down is not announced late.
        """
        now = dt_util.utcnow()
        # A timer whose moment has come is left alone: it is already due to run, and fires
        # everything due when it does.
        for armed, (point, _deadline, cancel) in list(self._deadline_timers.items()):
            if point > now:
                cancel()
                del self._deadline_timers[armed]
        data = self.data
        if data is None:
            return
        fired = self.fired_events
        if fired is not None and data.account_data_available:
            fired.prune((week.week_id, week.subscription_id) for week in data.weeks)
        for week in data.weeks:
            if week.selection_deadline is None or not week.is_editable:
                continue
            deadline = as_utc(week.selection_deadline)
            key = (week.week_id, week.subscription_id)
            for kind, point in (
                (EVENT_SELECTION_DEADLINE_APPROACHING, deadline - SELECTION_DEADLINE_NOTICE),
                (EVENT_SELECTION_LOCKED, deadline),
            ):
                if (
                    point <= now
                    or (key, kind) in self._deadline_timers
                    or (fired is not None and fired.has_fired(key, kind))
                ):
                    continue
                self._deadline_timers[(key, kind)] = (
                    point,
                    deadline,
                    async_track_point_in_utc_time(self.hass, self._async_deadline_reached, point),
                )

    @callback
    def _async_deadline_reached(self, _now: datetime) -> None:
        """Fire every armed deadline event whose moment has come.

        All of them, not only this timer's: two weeks (two subscriptions) can share a
        deadline, and the update below re-arms the timers, which would drop the other one.
        """
        now = dt_util.utcnow()
        due = [
            (armed, deadline)
            for armed, (point, deadline, _cancel) in self._deadline_timers.items()
            if point <= now
        ]
        if not due:
            return
        deadline_passed = False
        for armed, deadline in due:
            _point, _deadline, cancel = self._deadline_timers.pop(armed)
            cancel()
            key, kind = armed
            self._fire_deadline_event(key, kind, deadline)
            deadline_passed = deadline_passed or kind == EVENT_SELECTION_LOCKED
        if deadline_passed and self.data is not None:
            # What the clock decides (which weeks still need a selection, the next one to
            # choose for) is worked out again, so the entities change with the deadline instead
            # of at the next poll. In place, as the delivery watch does; the identity-keyed
            # get_weeks cache goes with it.
            self.data.finalize()
            self._weeks_response_cache = None
        # The event entity emits what was queued, and the entities read the clock again.
        self.async_update_listeners()

    def _fire_deadline_event(self, key: SnapshotKey, kind: str, deadline: datetime) -> None:
        """Queue a deadline event for a week that is still the one it was armed for."""
        data = self.data
        week = data.get_week(*key) if data is not None else None
        if (
            week is None
            or week.selection_deadline is None
            or as_utc(week.selection_deadline) != deadline
            or week.is_skipped
            or not week.allowed_actions.get("mealSwap")
        ):
            return
        attributes: dict[str, Any] = {
            "week_id": week.week_id,
            "subscription_id": week.subscription_id,
            "display_name": week.display_name,
            "delivery_date": week.delivery_date.isoformat() if week.delivery_date else None,
            "deadline": week.selection_deadline.isoformat(),
        }
        if kind == EVENT_SELECTION_DEADLINE_APPROACHING:
            attributes.update(
                {
                    "needs_selection": week.needs_selection,
                    "meals_selected": week.meals_selected,
                    "meals_required": week.meals_required,
                }
            )
        self._queue_event(kind, attributes)

    async def async_after_write(self, *, full_refresh: bool = True) -> None:
        """Show what a write changed now; bring the rest of the account up to date after.

        A successful write leaves the client a fresh snapshot of what it changed: the week it
        re-read after a cart write, a skip or a new delivery day, or the hearts after a
        favourite. Publishing that snapshot at once puts the change on the entities and in the
        next get_weeks without waiting for a whole account poll. Waiting held every service
        call for the length of a poll, and a second write within the refresh cooldown got no
        refresh at all, so it answered with the state from before the first one.

        ``full_refresh`` then re-reads the whole account in the background, for what a write
        moves but its own re-read does not cover (the next delivery's total, credit, tracking).
        Favourites pass False: a heart changes nothing a poll reads. When the client could not
        re-read what it wrote, the write waits for that poll, as every write used to.
        """
        snapshot = self.client.last_account_data
        if snapshot is None or snapshot is self.data:
            if full_refresh:
                await self.async_request_refresh()
            return
        self.async_set_updated_data(snapshot)
        if full_refresh:
            self.config_entry.async_create_background_task(
                self.hass, self.async_request_refresh(), name="hellofresh refresh after a write"
            )

    @property
    def event_serial(self) -> int:
        """Serial of the most recently recorded lifecycle event (0 when none yet)."""
        return self._event_serial

    @staticmethod
    def _require_usable_data(
        previous: HelloFreshAccountData | None, data: HelloFreshAccountData
    ) -> None:
        """Fail the poll when its result would replace real data with an empty shell.

        The client answers a failed subscriptions request (a timeout, a 5xx, a rate limit)
        with an account-less result instead of an error, so that a menu-only setup still
        works. Taken at face value mid-run, that emptied every entity for a whole scan
        interval while the poll counted as a success; at startup it made setup "succeed"
        with nothing, so Home Assistant never retried.

        A failed update keeps the previous data, and the next poll's lifecycle events are
        diffed against that last good data, so a recovery does not announce every week again.
        """
        if data.account_data_available:
            return
        if previous is not None and previous.account_data_available:
            raise UpdateFailed(
                "HelloFresh returned no account data; keeping the data from the last good update"
            )
        if not (data.weeks or data.public_menu_weeks):
            raise UpdateFailed("HelloFresh returned no account data and no menu")

    async def _async_update_data(self) -> HelloFreshAccountData:
        """Fetch latest data from HelloFresh."""
        async with self._refresh_lock:
            previous = self.data
            old_snapshot = delivery_snapshot(previous) if previous is not None else None
            try:
                data = await self.client.async_get_account_data()
            except HelloFreshAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except HelloFreshError as err:
                raise UpdateFailed(str(err)) from err
            self._require_usable_data(previous, data)
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

        # Not a data-quality warning but a problem with the account itself, so the option that
        # hides those does not hide this one. Only a read of the account says anything about
        # its payments; a poll without one leaves the warning as it was.
        if data.account_data_available:
            async_update_payment_problem_issue(
                self.hass,
                self.config_entry.entry_id,
                self.config_entry.title,
                data.payment_problem_weeks,
                self.client.base_url,
            )

        return data
