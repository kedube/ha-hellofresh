"""The HelloFresh integration."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
import inspect
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
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
    ATTR_GOALS,
    ATTR_HOUSEHOLD,
    ATTR_QUANTITIES,
    ATTR_RECIPE_IDS,
    ATTR_SUBSCRIPTION_ID,
    ATTR_TASTE,
    ATTR_WEEK_ID,
    CONF_ACCESS_TOKEN,
    CONF_COUNTRY,
    CONF_ENABLE_PUBLIC_MENU_FALLBACK,
    CONF_EXPIRES_IN,
    CONF_HISTORY_WEEKS,
    CONF_ISSUED_AT,
    CONF_MENU_GRACE_WEEKS,
    CONF_PASSWORD,
    CONF_REFRESH_EXPIRES_IN,
    CONF_REFRESH_TOKEN,
    CONF_REFRESH_TOKEN_ISSUED_AT,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_TOKEN_TYPE,
    CONF_USERNAME,
    DEFAULT_ENABLE_PUBLIC_MENU_FALLBACK,
    DEFAULT_HISTORY_WEEKS,
    DEFAULT_MENU_GRACE_WEEKS,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    PLATFORMS,
    SERVICE_CHANGE_DELIVERY_WEEKDAY,
    SERVICE_GET_ACCOUNT_SUMMARY,
    SERVICE_GET_FOOD_PROFILE,
    SERVICE_GET_WEEKS,
    SERVICE_REFRESH_DATA,
    SERVICE_RESCHEDULE_WEEK,
    SERVICE_SELECT_MARKET_ITEMS,
    SERVICE_SELECT_MEALS,
    SERVICE_SET_FOOD_PROFILE,
    SERVICE_SKIP_WEEK,
    SERVICE_UNSKIP_WEEK,
)
from .coordinator import HelloFreshDataUpdateCoordinator
from .frontend import async_register_meal_planner_card
from .intent import async_register_intents
from .issues import async_create_write_actions_issue
from .sensor_helpers import sensor_native_value

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


def _variation_join_debug(week: object) -> dict[str, object]:
    """Diagnostic: expose why a recipe's variant modifier did (not) resolve for a week.

    Returns the modularity ``index`` -> title map the week carries (from its own payload or the
    merged ``_menu_payload``), plus each recipe's ``course_index`` and resolved ``variation_title``.
    A duplicate dish that shows only "Variant" with no modifier means its ``course_index`` is not
    a key in ``modularity_titles`` for that week — this surfaces exactly that mismatch.
    """
    raw = getattr(week, "raw", {}) or {}
    has_modularity = isinstance(raw.get("modularity"), list)
    menu_payload = raw.get("_menu_payload") if isinstance(raw.get("_menu_payload"), dict) else {}
    has_menu_modularity = isinstance(menu_payload.get("modularity"), list)
    # Reuse the normalizer's own parser so this reflects the real join logic.
    from .normalizers import HelloFreshPayloadNormalizer

    titles = HelloFreshPayloadNormalizer._build_variation_titles(raw)
    return {
        "week_id": getattr(week, "week_id", None),
        "source": getattr(week, "source", None),
        "has_modularity_on_week": has_modularity,
        "has_modularity_in_menu_payload": has_menu_modularity,
        "modularity_index_count": len(titles),
        "modularity_titles": titles,
        "recipes": [
            {
                "name": r.name,
                "course_index": r.course_index,
                "variation_title": r.variation_title,
                "matches_modularity": r.course_index in titles if r.course_index is not None else False,
            }
            for r in getattr(week, "recipes", [])
        ],
        "market": _market_selection_debug(raw, menu_payload),
    }


def _market_selection_debug(raw: dict, menu_payload: dict) -> dict[str, object]:
    """Diagnostic: surface how selected Market add-ons appear in the raw payload.

    We don't know which raw field marks a chosen add-on (the captured catalog had none selected),
    so this dumps every addon entry that carries ANY selection-like key, plus the full set of keys
    seen on addon entries, so the real "selected quantity" field can be identified from live data.
    """
    addons = raw.get("addOns")
    if not isinstance(addons, dict):
        addons = menu_payload.get("addOns") if isinstance(menu_payload, dict) else None
    if not isinstance(addons, dict):
        return {"has_addons": False}

    selection_keys = ("selection", "quantity", "selectedQuantity", "isSelected", "selected")
    all_item_keys: set[str] = set()
    selected_samples: list[dict] = []
    total = 0
    for group in addons.get("groups") or []:
        if not isinstance(group, dict):
            continue
        for item in group.get("addOns") or []:
            if not isinstance(item, dict):
                continue
            total += 1
            all_item_keys.update(item.keys())
            if any(item.get(k) for k in selection_keys):
                selected_samples.append(
                    {"group": group.get("groupType"), **{k: item.get(k) for k in item}}
                )
    return {
        "has_addons": True,
        "addon_item_count": total,
        "addon_item_keys_seen": sorted(all_item_keys),
        "selection_keys_probed": list(selection_keys),
        "items_with_selection_like_field": selected_samples[:10],
    }


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
    await async_register_meal_planner_card(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up HelloFresh from a config entry."""
    # Heal any legacy credentials-in-options before reading them (data-only from here on).
    _heal_credential_storage(hass, entry)

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
        history_weeks=entry.options.get(CONF_HISTORY_WEEKS, DEFAULT_HISTORY_WEEKS),
        menu_grace_weeks=entry.options.get(CONF_MENU_GRACE_WEEKS, DEFAULT_MENU_GRACE_WEEKS),
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

    async def async_get_weeks(service_call: ServiceCall) -> ServiceResponse:
        """Return delivery weeks with full recipe detail for a single account.

        Read-only. Recipes are intentionally absent from sensor attributes (16 KB
        recorder cap), so this response service is how a dashboard reads per-week
        recipes/selection on demand. Optionally filter to one ``week_id``.
        """
        coordinators = _get_target_coordinators(service_call)
        if len(coordinators) != 1:
            raise HomeAssistantError(
                "Multiple HelloFresh accounts are configured. Specify config_entry_id."
            )
        coordinator = coordinators[0]
        data = coordinator.data
        week_id = service_call.data.get(ATTR_WEEK_ID)
        if week_id is not None:
            week = data.get_week(week_id)
            weeks = [week] if week is not None else []
        else:
            weeks = list(data.weeks)

        def _week_dict(week: object) -> dict[str, object]:
            # Attach the week's matching order (tracking, status, carrier, order id, price) so a
            # dashboard card can show shipment/billing detail alongside the recipes for that week.
            payload = week.as_dict()
            order = data.get_order_for_week(week.week_id)
            payload["order"] = order.as_dict() if order is not None else None
            return payload

        # Account-level fallback fields the card can use when a week has no billed order yet —
        # e.g. the recurring plan total (matches the selected_plan_total_price sensor). Also
        # carries the configured menu grace window so the card's past-week gating matches the
        # integration's (a just-delivered week keeps its browsable menu for this many weeks),
        # and the configured refresh interval so cards can auto-refetch on the same cadence
        # the coordinator polls HelloFresh (fetching more often would return the same data).
        primary_subscription = data.primary_subscription
        account = {
            "selected_plan_total_price": data.selected_plan_total_price,
            "selected_plan_total_price_currency": data.selected_plan_total_price_currency,
            "menu_grace_weeks": coordinator.client.menu_grace_weeks,
            "refresh_interval_minutes": coordinator.config_entry.options.get(
                CONF_SCAN_INTERVAL_MINUTES, DEFAULT_SCAN_INTERVAL_MINUTES
            ),
            # Subscription-level next charge date and coupon (matching the next_payment_date
            # and next_box_coupon sensors); shown in the schedule card's next-box summary.
            "next_payment_date": (
                primary_subscription.next_payment_date.isoformat()
                if primary_subscription is not None
                and primary_subscription.next_payment_date is not None
                else None
            ),
            "next_box_coupon": (
                primary_subscription.coupon_code if primary_subscription is not None else None
            ),
        }

        if service_call.data.get("include_debug"):
            return {
                "weeks": [_week_dict(week) for week in weeks],
                "account": account,
                "variation_debug": [
                    _variation_join_debug(week) for week in weeks
                ],
            }

        # The common case — no week filter, no debug — is what the dashboard cards call, often
        # several at once per poll cycle for identical data. Serialize the full weeks list once
        # per coordinator update and reuse it; the cache self-invalidates on the next poll.
        if week_id is None:
            return coordinator.get_weeks_response(
                lambda: {
                    "weeks": [_week_dict(week) for week in weeks],
                    "account": account,
                }
            )
        return {"weeks": [_week_dict(week) for week in weeks], "account": account}

    async def async_get_account_summary(service_call: ServiceCall) -> ServiceResponse:
        """Return the account/subscription headline values for the subscription card.

        Read-only. Every value comes from the same ``sensor_native_value`` dispatcher that
        backs the corresponding sensor entity, so the card and the sensors can never
        disagree. Dates are ISO strings; empty values are null (the sensors' literal
        "None" placeholder text is normalized back to null for JSON consumers).
        """
        coordinators = _get_target_coordinators(service_call)
        if len(coordinators) != 1:
            raise HomeAssistantError(
                "Multiple HelloFresh accounts are configured. Specify config_entry_id."
            )
        coordinator = coordinators[0]
        data = coordinator.data

        def _value(key: str) -> object:
            value = sensor_native_value(key, data, coordinator.client.base_url)
            if value == "None":  # NONE_WHEN_EMPTY_KEYS placeholder — null in a response
                return None
            if isinstance(value, (datetime, date)):
                return value.isoformat()
            return value

        keys = (
            "subscription_status",
            "account_id",
            "selected_plan",
            "selected_plan_total_price",
            "account_credit",
            "number_of_people",
            "required_meal_count",
            "boxes_received",
            "delivery_address",
            "upcoming_delivery_count",
            "weeks_needing_selection",
            "skipped_week_count",
            "next_skipped_week",
            "next_box_coupon",
            "next_payment_date",
            "next_selectable_delivery_preselected",
            "next_holiday_message",
            "next_holiday_delivery_date",
        )
        summary: dict[str, object] = {key: _value(key) for key in keys}
        summary["selected_plan_total_price_currency"] = data.selected_plan_total_price_currency
        summary["account_credit_currency"] = data.account_credit_currency
        # Week ids behind the counters, so the card can make them clickable (the click
        # broadcasts the cross-card week-sync event to jump the other cards there).
        summary["weeks_needing_selection_ids"] = [
            week.week_id for week in data.weeks_auto_picked
        ]
        summary["next_skipped_week_id"] = (
            data.next_skipped_week.week_id if data.next_skipped_week else None
        )
        # Same auto-refresh contract as get_weeks: the card re-pulls on the poll cadence.
        summary["refresh_interval_minutes"] = coordinator.config_entry.options.get(
            CONF_SCAN_INTERVAL_MINUTES, DEFAULT_SCAN_INTERVAL_MINUTES
        )
        return summary

    async def async_get_food_profile(service_call: ServiceCall) -> ServiceResponse:
        """Return the customer's food profile plus the full catalog of selectable options.

        Read-only. Fetched live from HelloFresh's profile-service (it is not part of the
        regular sensor poll), so the Food Profile card can read current picks and every
        possible option in one call.
        """
        coordinators = _get_target_coordinators(service_call)
        if len(coordinators) != 1:
            raise HomeAssistantError(
                "Multiple HelloFresh accounts are configured. Specify config_entry_id."
            )
        client = coordinators[0].client
        profile = await client.async_get_food_profile()
        options = await client.async_get_food_profile_options()
        return {"profile": profile.as_dict(), "options": options.as_dict()}

    async def async_set_food_profile(service_call: ServiceCall) -> ServiceResponse:
        """Update the customer's food profile (auto-preselection preferences).

        Accepts partial ``taste`` / ``household`` / ``goals`` sections; only what is provided
        is changed. Returns the saved profile so the card can re-render from the server's truth.
        """
        coordinators = _get_target_coordinators(service_call)
        if len(coordinators) != 1:
            raise HomeAssistantError(
                "Multiple HelloFresh accounts are configured. Specify config_entry_id."
            )
        changes: dict[str, object] = {}
        if ATTR_TASTE in service_call.data:
            changes["taste"] = service_call.data[ATTR_TASTE]
        if ATTR_HOUSEHOLD in service_call.data:
            changes["household"] = service_call.data[ATTR_HOUSEHOLD]
        if ATTR_GOALS in service_call.data:
            changes["goals"] = service_call.data[ATTR_GOALS]
        if not changes:
            raise HomeAssistantError("Provide at least one of: taste, household, goals.")
        saved = await coordinators[0].client.async_update_food_profile(changes)
        return {"profile": saved.as_dict()}

    async def async_select_meals(service_call: ServiceCall) -> None:
        """Submit meal selections."""
        week_id = service_call.data[ATTR_WEEK_ID]
        recipe_ids = list(service_call.data[ATTR_RECIPE_IDS])
        quantities = dict(service_call.data.get(ATTR_QUANTITIES) or {})
        await _for_each_coordinator(
            service_call,
            lambda coordinator, _: _async_mutation(
                coordinator,
                coordinator.client.async_select_meals(week_id, recipe_ids, quantities),
            ),
        )

    async def async_select_market_items(service_call: ServiceCall) -> None:
        """Submit HelloFresh Market add-on (extras) selections for a week."""
        week_id = service_call.data[ATTR_WEEK_ID]
        quantities = dict(service_call.data.get(ATTR_QUANTITIES) or {})
        await _for_each_coordinator(
            service_call,
            lambda coordinator, _: _async_mutation(
                coordinator,
                coordinator.client.async_select_market_items(week_id, quantities),
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
        SERVICE_GET_WEEKS,
        async_get_weeks,
        schema=vol.Schema(
            {
                vol.Optional(ATTR_CONFIG_ENTRY_ID): str,
                vol.Optional(ATTR_WEEK_ID): str,
                vol.Optional("include_debug"): bool,
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_ACCOUNT_SUMMARY,
        async_get_account_summary,
        schema=vol.Schema({vol.Optional(ATTR_CONFIG_ENTRY_ID): str}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_FOOD_PROFILE,
        async_get_food_profile,
        schema=vol.Schema({vol.Optional(ATTR_CONFIG_ENTRY_ID): str}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_FOOD_PROFILE,
        async_set_food_profile,
        schema=vol.Schema(
            {
                vol.Optional(ATTR_CONFIG_ENTRY_ID): str,
                # Each section is a free-form mapping mirroring the API's profile shape; the
                # client normalizes weighted taste fields to +/-100 before PATCHing.
                vol.Optional(ATTR_TASTE): dict,
                vol.Optional(ATTR_HOUSEHOLD): dict,
                vol.Optional(ATTR_GOALS): dict,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
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
                # Optional per-recipe serving counts: {recipe_id: positive int}. Recipes absent
                # from the map default to one serving.
                vol.Optional(ATTR_QUANTITIES): {str: vol.All(vol.Coerce(int), vol.Range(min=1))},
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SELECT_MARKET_ITEMS,
        async_select_market_items,
        schema=vol.Schema(
            {
                vol.Optional(ATTR_CONFIG_ENTRY_ID): str,
                vol.Required(ATTR_WEEK_ID): str,
                # Map of market item id/sku/index -> quantity (>=1). Items omitted (or set to 0)
                # are removed from the week's extras.
                vol.Required(ATTR_QUANTITIES): {str: vol.All(vol.Coerce(int), vol.Range(min=0))},
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
