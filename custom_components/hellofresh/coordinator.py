"""Data update coordinator for HelloFresh."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
import logging

from homeassistant.core import HomeAssistant, ServiceResponse, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    HelloFreshAccountData,
    HelloFreshAuthError,
    HelloFreshClient,
    HelloFreshError,
)
from .const import (
    CONF_SHOW_DATA_QUALITY_ISSUES,
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
        # Memoized get_weeks serialization, keyed by the data object it was built from.
        # Each poll assigns a fresh HelloFreshAccountData, so a new object is a cache miss;
        # multiple cards calling get_weeks within one poll cycle reuse one build. The key
        # MUST be the object itself (compared with ``is``), not ``id(data)``: a bare id is
        # a memory address that CPython reuses once the old object is freed, which let a
        # later poll's data collide with the cached key and serve stale weeks to the cards.
        self._weeks_response_cache: tuple[HelloFreshAccountData, ServiceResponse] | None = None

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

    async def _async_update_data(self) -> HelloFreshAccountData:
        """Fetch latest data from HelloFresh."""
        try:
            data = await self.client.async_get_account_data()
        except HelloFreshAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except HelloFreshError as err:
            raise UpdateFailed(str(err)) from err

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
