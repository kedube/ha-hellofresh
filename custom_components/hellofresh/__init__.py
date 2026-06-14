"""The HelloFresh integration."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
import inspect
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
import voluptuous as vol

from .api import HelloFreshClient, HelloFreshError, HelloFreshNotImplementedError
from .client import _token_fingerprint
from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DELIVERY_INTERVAL,
    ATTR_DELIVERY_OPTION,
    ATTR_RECIPE_IDS,
    ATTR_SUBSCRIPTION_ID,
    ATTR_WEEK_ID,
    CONF_ACCESS_TOKEN,
    CONF_COUNTRY,
    CONF_ENABLE_PUBLIC_MENU_FALLBACK,
    CONF_EXPIRES_IN,
    CONF_ISSUED_AT,
    CONF_PASSWORD,
    CONF_REFRESH_EXPIRES_IN,
    CONF_REFRESH_TOKEN,
    CONF_REFRESH_TOKEN_ISSUED_AT,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_TOKEN_TYPE,
    CONF_USERNAME,
    DEFAULT_ENABLE_PUBLIC_MENU_FALLBACK,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    PLATFORMS,
    SERVICE_CHANGE_DELIVERY_WEEKDAY,
    SERVICE_REFRESH_DATA,
    SERVICE_RESCHEDULE_WEEK,
    SERVICE_SELECT_MEALS,
    SERVICE_SKIP_WEEK,
    SERVICE_UNSKIP_WEEK,
)
from .coordinator import HelloFreshDataUpdateCoordinator
from .intent import async_register_intents
from .issues import async_create_write_actions_issue

_LOGGER = logging.getLogger(__name__)

CoordinatorMap = dict[str, HelloFreshDataUpdateCoordinator]
INTENTS_REGISTERED_KEY = f"{DOMAIN}_intents_registered"
# Tracks entry IDs whose most recent config-entry write was a token-only refresh, so the
# update listener can skip a full reload for those writes.
TOKEN_ONLY_UPDATE_KEY = f"{DOMAIN}_token_only_update"
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


def _heal_credential_storage(hass: HomeAssistant, entry: ConfigEntry) -> None:
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
    hass.data.setdefault(DOMAIN, {})
    if not hass.data.get(INTENTS_REGISTERED_KEY):
        async_register_intents(hass)
        hass.data[INTENTS_REGISTERED_KEY] = True
    await _async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up HelloFresh from a config entry."""
    # Heal any legacy credentials-in-options before reading them (data-only from here on).
    _heal_credential_storage(hass, entry)

    # Migration: pre-credentials entries stored only a pasted token. The auth flow now
    # requires a username/password to log in and self-heal across token expiry, so trigger
    # reauth to collect them. The existing access token keeps working until then.
    if not entry.data.get(CONF_USERNAME) or not entry.data.get(CONF_PASSWORD):
        raise ConfigEntryAuthFailed(
            "HelloFresh now signs in with your account email and password. "
            "Please reauthenticate to continue."
        )

    session = async_get_clientsession(hass)

    def _persist_refreshed_token(token_data: dict) -> None:
        """Write a refreshed/rotated token back to the config entry so it survives restarts.

        Credentials live in entry.data only. The live client already holds the new token
        in memory, so this token-only write must not trigger a full reload — flag it so the
        update listener skips the reload. The write to HA's in-memory store is synchronous,
        which closes most of the rotation-vs-crash window; HA flushes it to disk shortly
        after.

        If the write fails, the flag is cleared (so a later reload isn't wrongly skipped)
        and the error is logged loudly: the in-memory client keeps working, but the rotated
        token won't survive a restart until the next successful persist.
        """
        token_only = hass.data.setdefault(TOKEN_ONLY_UPDATE_KEY, set())
        token_only.add(entry.entry_id)
        new_refresh = token_data.get(CONF_REFRESH_TOKEN)
        new_fp = _token_fingerprint(new_refresh)
        try:
            hass.config_entries.async_update_entry(
                entry,
                data={**entry.data, **token_data},
            )
        except Exception:  # noqa: BLE001 - persistence must never crash the refresh path
            token_only.discard(entry.entry_id)
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

    # Credentials live in entry.data ONLY. Options hold user preferences (scan interval,
    # fallback toggle). Reading credentials from a single store removes the data/options
    # split-brain that previously let an options write silently shadow or wipe a refreshed
    # token. _heal_credential_storage above has already moved any legacy options-stored
    # credentials into data before this runs.
    client = HelloFreshClient(
        session=session,
        country=entry.data[CONF_COUNTRY],
        # No access token on a freshly-configured entry — the client logs in on first use.
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
        token_refresh_callback=_persist_refreshed_token,
    )
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
    await coordinator.async_config_entry_first_refresh()
    coordinator.async_start_token_refresh()

    coordinators: CoordinatorMap = hass.data.setdefault(DOMAIN, {})
    coordinators[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        # Drop any pending token-only flag so a removed entry's id can't linger in the set.
        token_only = hass.data.get(TOKEN_ONLY_UPDATE_KEY)
        if token_only is not None:
            token_only.discard(entry.entry_id)
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry, unless the change was a token-only refresh write."""
    token_only = hass.data.get(TOKEN_ONLY_UPDATE_KEY)
    if token_only and entry.entry_id in token_only:
        # A proactive token refresh persisted new credentials; the running client already
        # uses them, so skip the costly reload that would tear down all entities.
        token_only.discard(entry.entry_id)
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_register_services(hass: HomeAssistant) -> None:
    """Register HelloFresh services once."""
    if hass.services.has_service(DOMAIN, SERVICE_REFRESH_DATA):
        return

    def _get_target_coordinators(
        service_call: ServiceCall,
    ) -> list[HelloFreshDataUpdateCoordinator]:
        coordinators: CoordinatorMap = hass.data.get(DOMAIN, {})
        target_entry_id = service_call.data.get(ATTR_CONFIG_ENTRY_ID)
        if target_entry_id:
            coordinator = coordinators.get(target_entry_id)
            if coordinator is None:
                raise HomeAssistantError(f"HelloFresh config entry not found: {target_entry_id}")
            return [coordinator]

        if len(coordinators) == 1:
            return list(coordinators.values())

        raise HomeAssistantError(
            "Multiple HelloFresh accounts are configured. Specify config_entry_id."
        )

    async def _for_each_coordinator(
        service_call: ServiceCall,
        handler: Callable[[HelloFreshDataUpdateCoordinator, ServiceCall], object],
    ) -> None:
        for coordinator in _get_target_coordinators(service_call):
            try:
                result = handler(coordinator, service_call)
                if inspect.isawaitable(result):
                    await result
            except HelloFreshNotImplementedError as err:
                async_create_write_actions_issue(
                    hass,
                    coordinator.config_entry.entry_id,
                    coordinator.config_entry.title,
                )
                raise HomeAssistantError(str(err)) from err
            except HelloFreshError as err:
                raise HomeAssistantError(str(err)) from err

    async def async_refresh_data(service_call: ServiceCall) -> None:
        """Refresh all configured HelloFresh coordinators."""
        await _for_each_coordinator(
            service_call,
            lambda coordinator, _: coordinator.async_request_refresh(),
        )

    async def async_select_meals(service_call: ServiceCall) -> None:
        """Submit meal selections."""
        week_id = service_call.data[ATTR_WEEK_ID]
        recipe_ids = list(service_call.data[ATTR_RECIPE_IDS])
        await _for_each_coordinator(
            service_call,
            lambda coordinator, _: _async_mutation(
                coordinator,
                coordinator.client.async_select_meals(week_id, recipe_ids),
            ),
        )

    async def async_skip_week(service_call: ServiceCall) -> None:
        """Skip a delivery week."""
        week_id = service_call.data[ATTR_WEEK_ID]
        await _for_each_coordinator(
            service_call,
            lambda coordinator, _: _async_mutation(
                coordinator,
                coordinator.client.async_skip_week(week_id),
            ),
        )

    async def async_unskip_week(service_call: ServiceCall) -> None:
        """Restore a skipped delivery week."""
        week_id = service_call.data[ATTR_WEEK_ID]
        await _for_each_coordinator(
            service_call,
            lambda coordinator, _: _async_mutation(
                coordinator,
                coordinator.client.async_unskip_week(week_id),
            ),
        )

    async def async_reschedule_week(service_call: ServiceCall) -> None:
        """Reschedule a single delivery week to a different delivery option."""
        week_id = service_call.data[ATTR_WEEK_ID]
        delivery_option = service_call.data[ATTR_DELIVERY_OPTION]
        await _for_each_coordinator(
            service_call,
            lambda coordinator, _: _async_mutation(
                coordinator,
                coordinator.client.async_change_one_off_delivery(week_id, delivery_option),
            ),
        )

    async def async_change_delivery_weekday(service_call: ServiceCall) -> None:
        """Change the recurring delivery option/interval for a subscription's plan."""
        delivery_option = service_call.data[ATTR_DELIVERY_OPTION]
        delivery_interval = service_call.data.get(ATTR_DELIVERY_INTERVAL, 1)
        subscription_id = service_call.data.get(ATTR_SUBSCRIPTION_ID)
        await _for_each_coordinator(
            service_call,
            lambda coordinator, _: _async_mutation(
                coordinator,
                coordinator.client.async_change_delivery_weekday(
                    delivery_option, delivery_interval, subscription_id
                ),
            ),
        )

    async def _async_mutation(
        coordinator: HelloFreshDataUpdateCoordinator,
        coro,
    ) -> None:
        """Run a state-changing action and refresh after success."""
        await coro
        await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH_DATA,
        async_refresh_data,
        schema=vol.Schema({vol.Optional(ATTR_CONFIG_ENTRY_ID): str}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SELECT_MEALS,
        async_select_meals,
        schema=vol.Schema(
            {
                vol.Optional(ATTR_CONFIG_ENTRY_ID): str,
                vol.Required(ATTR_WEEK_ID): str,
                vol.Required(ATTR_RECIPE_IDS): vol.All([str], vol.Length(min=1)),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SKIP_WEEK,
        async_skip_week,
        schema=vol.Schema(
            {
                vol.Optional(ATTR_CONFIG_ENTRY_ID): str,
                vol.Required(ATTR_WEEK_ID): str,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_UNSKIP_WEEK,
        async_unskip_week,
        schema=vol.Schema(
            {
                vol.Optional(ATTR_CONFIG_ENTRY_ID): str,
                vol.Required(ATTR_WEEK_ID): str,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESCHEDULE_WEEK,
        async_reschedule_week,
        schema=vol.Schema(
            {
                vol.Optional(ATTR_CONFIG_ENTRY_ID): str,
                vol.Required(ATTR_WEEK_ID): str,
                vol.Required(ATTR_DELIVERY_OPTION): str,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CHANGE_DELIVERY_WEEKDAY,
        async_change_delivery_weekday,
        schema=vol.Schema(
            {
                vol.Optional(ATTR_CONFIG_ENTRY_ID): str,
                vol.Required(ATTR_DELIVERY_OPTION): str,
                vol.Optional(ATTR_DELIVERY_INTERVAL, default=1): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=4)
                ),
                vol.Optional(ATTR_SUBSCRIPTION_ID): str,
            }
        ),
    )
