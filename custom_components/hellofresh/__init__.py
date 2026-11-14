"""The HelloFresh integration."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import HelloFreshClient
from .client import _token_fingerprint
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_COUNTRY,
    CONF_ENABLE_FAVORITES,
    CONF_ENABLE_PREP_LISTS,
    CONF_ENABLE_PUBLIC_MENU_FALLBACK,
    CONF_EXPIRES_IN,
    CONF_HISTORY_WEEKS,
    CONF_ISSUED_AT,
    CONF_LOG_AUTH_DIAGNOSTICS,
    CONF_MENU_GRACE_WEEKS,
    CONF_PASSWORD,
    CONF_REFRESH_EXPIRES_IN,
    CONF_REFRESH_TOKEN,
    CONF_REFRESH_TOKEN_ISSUED_AT,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_SHOW_DATA_QUALITY_ISSUES,
    CONF_TOKEN_TYPE,
    CONF_USE_CURL_CFFI_HEADERS,
    CONF_USERNAME,
    DEFAULT_ENABLE_FAVORITES,
    DEFAULT_ENABLE_PREP_LISTS,
    DEFAULT_ENABLE_PUBLIC_MENU_FALLBACK,
    DEFAULT_HISTORY_WEEKS,
    DEFAULT_LOG_AUTH_DIAGNOSTICS,
    DEFAULT_MENU_GRACE_WEEKS,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DEFAULT_SHOW_DATA_QUALITY_ISSUES,
    DEFAULT_USE_CURL_CFFI_HEADERS,
    DOMAIN,
    PLATFORMS,
    TRACEY_COUNTRIES,
)
from .coordinator import (
    HelloFreshConfigEntry,
    HelloFreshDataUpdateCoordinator,
    async_remove_fired_events,
)
from .frontend import (
    async_add_entry_panel,
    async_register_card,
    async_remove_entry_panel,
)
from .intent import async_register_intents
from .issues import (
    async_cleanup_stale_issues,
    async_delete_data_quality_issues,
    async_delete_payment_problem_issue,
)
from .meal_plan import MealPlan, async_remove_meal_plan
from .services import async_register_services
from .tracey import HelloFreshTraceyCoordinator

_LOGGER = logging.getLogger(__name__)

INTENTS_REGISTERED_KEY = f"{DOMAIN}_intents_registered"
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


# Credential keys belong in entry.data only. Older entries stored some of these in
# entry.options (the source of the data/options split-brain that broke refresh); this set
# drives a one-time, idempotent heal on load.
_CREDENTIAL_KEYS = (
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_ISSUED_AT,
    CONF_EXPIRES_IN,
    CONF_REFRESH_EXPIRES_IN,
    CONF_REFRESH_TOKEN_ISSUED_AT,
    CONF_TOKEN_TYPE,
)


def _heal_credential_storage(hass: HomeAssistant, entry: HelloFreshConfigEntry) -> None:
    """Consolidate credentials into entry.data and strip them from entry.options.

    Idempotent and safe to run on every load. For any credential key present in options:
    if data lacks it (or has null), adopt the options value; then remove it from options.
    data is otherwise authoritative because the runtime refresh writes there. This undoes
    the legacy state where a stale token in options shadowed a fresher token in data.
    """
    options_has_credentials = any(key in entry.options for key in _CREDENTIAL_KEYS)
    if not options_has_credentials:
        return

    new_data = dict(entry.data)
    new_options = dict(entry.options)
    for key in _CREDENTIAL_KEYS:
        if key in new_options:
            option_value = new_options.pop(key)
            if new_data.get(key) in (None, "") and option_value not in (None, ""):
                new_data[key] = option_value

    hass.config_entries.async_update_entry(entry, data=new_data, options=new_options)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the HelloFresh component."""
    if not hass.data.get(INTENTS_REGISTERED_KEY):
        async_register_intents(hass)
        hass.data[INTENTS_REGISTERED_KEY] = True
    async_register_services(hass)
    await async_register_card(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: HelloFreshConfigEntry) -> bool:
    """Set up HelloFresh from a config entry."""
    # Heal any legacy credentials-in-options before reading them (data-only from here on).
    _heal_credential_storage(hass, entry)

    # Clear repair issues that polling alone can never clear: issues orphaned by removed
    # config entries, and — when the warnings option is off — this entry's own issues.
    # Done at setup (not just in the coordinator) so the cleanup happens even when the
    # first refresh fails, e.g. during an auth outage.
    async_cleanup_stale_issues(
        hass,
        {existing.entry_id for existing in hass.config_entries.async_entries(DOMAIN)},
    )
    if not entry.options.get(CONF_SHOW_DATA_QUALITY_ISSUES, DEFAULT_SHOW_DATA_QUALITY_ISSUES):
        async_delete_data_quality_issues(hass, entry.entry_id)

    # An entry must be able to authenticate by *some* means: either credentials (to log in
    # and self-heal across token expiry) or a token (the deliberate token-only backup path).
    # Only when BOTH are absent is the entry unusable -- that's a stale legacy entry, so
    # trigger reauth to collect one or the other. A token-only entry is intentional and must
    # NOT be forced to provide a password it doesn't have.
    has_credentials = bool(entry.data.get(CONF_USERNAME) and entry.data.get(CONF_PASSWORD))
    has_token = bool(entry.data.get(CONF_ACCESS_TOKEN) or entry.data.get(CONF_REFRESH_TOKEN))
    if not has_credentials and not has_token:
        raise ConfigEntryAuthFailed(
            "HelloFresh needs your account email and password, or a valid access token, "
            "to connect. Please reauthenticate to continue."
        )

    # Each account needs its own cookie jar on both the aiohttp and curl_cffi paths.
    session = async_create_clientsession(hass)
    # Assigned once the client exists. The token callback below reads it when it is called,
    # not when it is defined: the client needs the callback before the coordinator can be
    # built around the client.
    coordinator: HelloFreshDataUpdateCoordinator | None = None

    def _persist_refreshed_token(token_data: dict) -> None:
        """Write a refreshed/rotated token back to the config entry so it survives restarts.

        Credentials live in entry.data only. The live client already holds the new token
        in memory, and a data write never reloads the entry (it has no update listener; see
        the note after the platforms are set up below), so the entities stay up. The write
        to HA's in-memory store is synchronous, which closes most of the rotation-vs-crash
        window; HA flushes it to disk shortly after.

        If the write fails, the error is logged loudly: the in-memory client keeps working,
        but the rotated token won't survive a restart until the next successful persist.
        """
        new_refresh = token_data.get(CONF_REFRESH_TOKEN)
        new_fp = _token_fingerprint(new_refresh)
        try:
            hass.config_entries.async_update_entry(
                entry,
                data={**entry.data, **token_data},
            )
        except Exception:  # noqa: BLE001 - persistence must never crash the refresh path
            _LOGGER.exception(
                "HelloFresh could not persist the refreshed token for %s; it will work "
                "until the next restart and retry on the next refresh",
                entry.entry_id,
            )
            return
        # Re-read what actually landed in entry.data. If the stored fingerprint does not
        # match what we just wrote, the rotated token did not persist — the smoking gun for
        # a 401 after the next reload/restart.
        stored_fp = _token_fingerprint(entry.data.get(CONF_REFRESH_TOKEN))
        _LOGGER.debug(
            "HelloFresh persisted refreshed token for %s: wrote fp=%s, entry.data now fp=%s (match=%s)",
            entry.entry_id,
            new_fp,
            stored_fp,
            new_fp == stored_fp,
        )

    def _token_refreshed(token_data: dict) -> None:
        """Store a rotated token, then let the token-lifetime sensors show it."""
        _persist_refreshed_token(token_data)
        if coordinator is not None:
            coordinator.async_token_updated()

    # Credentials live in entry.data ONLY. Options hold user preferences (scan interval,
    # fallback toggle). Reading credentials from a single store removes the data/options
    # split-brain that previously let an options write silently shadow or wipe a refreshed
    # token. _heal_credential_storage above has already moved any legacy options-stored
    # credentials into data before this runs.
    client = HelloFreshClient(
        session=session,
        country=entry.data[CONF_COUNTRY],
        # A credential entry starts with the token its validation login obtained; an entry
        # created before that was stored has none, and the client logs in on first use.
        access_token=entry.data.get(CONF_ACCESS_TOKEN),
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
        token_issued_at=entry.data.get(CONF_ISSUED_AT),
        token_expires_in=entry.data.get(CONF_EXPIRES_IN),
        refresh_expires_in=entry.data.get(CONF_REFRESH_EXPIRES_IN),
        refresh_token_issued_at=entry.data.get(CONF_REFRESH_TOKEN_ISSUED_AT),
        token_type=entry.data.get(CONF_TOKEN_TYPE),
        username=entry.data.get(CONF_USERNAME),
        password=entry.data.get(CONF_PASSWORD),
        enable_public_menu_fallback=entry.options.get(
            CONF_ENABLE_PUBLIC_MENU_FALLBACK,
            DEFAULT_ENABLE_PUBLIC_MENU_FALLBACK,
        ),
        enable_favorites=entry.options.get(CONF_ENABLE_FAVORITES, DEFAULT_ENABLE_FAVORITES),
        use_curl_cffi_headers=entry.options.get(
            CONF_USE_CURL_CFFI_HEADERS, DEFAULT_USE_CURL_CFFI_HEADERS
        ),
        log_auth_diagnostics=entry.options.get(
            CONF_LOG_AUTH_DIAGNOSTICS, DEFAULT_LOG_AUTH_DIAGNOSTICS
        ),
        history_weeks=entry.options.get(CONF_HISTORY_WEEKS, DEFAULT_HISTORY_WEEKS),
        menu_grace_weeks=entry.options.get(CONF_MENU_GRACE_WEEKS, DEFAULT_MENU_GRACE_WEEKS),
        token_refresh_callback=_token_refreshed,
    )
    # Registered before anything can fail: Home Assistant runs these hooks after a setup that
    # raises as well as after an unload, so the pooled curl sessions and their cookies are
    # released on every path. (It closes the aiohttp session itself.)
    entry.async_on_unload(client.async_close)
    coordinator = HelloFreshDataUpdateCoordinator(
        hass,
        client,
        entry,
        update_interval=timedelta(
            minutes=entry.options.get(
                CONF_SCAN_INTERVAL_MINUTES,
                DEFAULT_SCAN_INTERVAL_MINUTES,
            )
        ),
    )
    # What the once-a-week delivery events fired before a restart, read before any poll can
    # record one.
    await coordinator.async_load_fired_events()
    await coordinator.async_config_entry_first_refresh()
    coordinator.async_start_token_refresh()
    coordinator.async_start_delivery_watch()
    coordinator.async_start_deadline_events()

    # Live last-mile tracking is mounted only where the Tracey stack exists (see
    # TRACEY_COUNTRIES) — everywhere else coordinator.tracey stays None, no tracking
    # sensors are created, and get_delivery_tracking answers {"available": false}.
    # Its first refresh is best-effort (async_refresh, not first_refresh): a hiccup on the
    # unauthenticated tracking endpoint must never fail setup of the whole integration.
    if entry.data[CONF_COUNTRY] in TRACEY_COUNTRIES:
        tracey = HelloFreshTraceyCoordinator(hass, session, coordinator, entry)
        await tracey.async_refresh()
        coordinator.tracey = tracey

    # The Week tab's meal plan, kept in Home Assistant's storage.
    meal_plan = MealPlan(hass, entry, coordinator)
    await meal_plan.async_load()
    coordinator.meal_plan = meal_plan

    # HA's per-entry storage: platforms, services, diagnostics and intents all read the
    # coordinator from here. HA clears it when the entry unloads.
    entry.runtime_data = coordinator

    # Remembered for unload, which must ask for these same platforms (see async_unload_entry).
    coordinator.platforms = _entry_platforms(entry)
    await hass.config_entries.async_forward_entry_setups(entry, coordinator.platforms)
    # No update listener: the options flow (OptionsFlowWithReload) and the reauth/reconfigure
    # steps (async_update_reload_and_abort) each schedule the one reload their change needs,
    # and token writes need none. A listener on top would reload twice, which Home Assistant
    # deprecated in 2026.6 and rejects from 2026.12.
    # The sidebar entry that opens the HelloFresh card full screen (when the option is on).
    await async_add_entry_panel(hass, entry)
    return True


def _entry_platforms(entry: HelloFreshConfigEntry) -> list[Platform]:
    """The platforms this entry runs, honouring the optional prep-list to-do platform."""
    if entry.options.get(CONF_ENABLE_PREP_LISTS, DEFAULT_ENABLE_PREP_LISTS):
        return [*PLATFORMS, Platform.TODO]
    return list(PLATFORMS)


async def async_unload_entry(hass: HomeAssistant, entry: HelloFreshConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator = getattr(entry, "runtime_data", None)
    # Unload exactly the platforms setup forwarded. The list cannot be derived from the
    # options here: turning the prep lists off reloads the entry, and by then the option
    # already reads off while the to-do entities are still loaded. Asking for every platform
    # is not safe either. When the to-do platform was not set up for this entry but another
    # integration had loaded it, Home Assistant used to fail the unload ("Config entry was
    # never loaded!"), which left the entry in "Unload failed" with its timers running until
    # a restart; newer versions log a warning instead.
    platforms = coordinator.platforms if coordinator is not None else []
    unload_ok = await hass.config_entries.async_unload_platforms(entry, platforms)
    if unload_ok:
        await async_remove_entry_panel(hass, entry)
        # A plan change still waiting on its delayed save is written now, before a reload's
        # new plan reads the file. The same for the record of fired delivery events.
        meal_plan = getattr(coordinator, "meal_plan", None)
        if meal_plan is not None:
            await meal_plan.async_flush()
        fired_events = getattr(coordinator, "fired_events", None)
        if fired_events is not None:
            await fired_events.async_flush()
        # Only a loaded entry polls the account, so only it can say the problem is still
        # there; a reload raises the warning again on its first poll. An entry that never got
        # a coordinator never polled, so it raised none.
        if coordinator is not None:
            async_delete_payment_problem_issue(hass, entry.entry_id)
        # The client's curl sessions close through the hook async_setup_entry registered.
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: HelloFreshConfigEntry) -> None:
    """Purge the entry's repair issues and what it stored when it is removed.

    Without this, issues keyed to the removed entry id survive in the registry
    forever with no owner left to delete them, and so would the stored meal plan and the
    record of fired delivery events.
    """
    async_delete_data_quality_issues(hass, entry.entry_id)
    async_delete_payment_problem_issue(hass, entry.entry_id)
    await async_remove_meal_plan(hass, entry.entry_id)
    await async_remove_fired_events(hass, entry.entry_id)
