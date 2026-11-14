"""HelloFresh services: the handlers, their input schemas and the table that registers them.

Every service is one row of ``SERVICES``. Registration is a loop over that table, and each
handler runs inside the same wrapper, so two things are decided in one place for all of them:
which account a call acts on, and what a HelloFresh failure turns into.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, cast

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

if TYPE_CHECKING:
    # From 2026.10 Home Assistant validates with probatio and, as it starts, installs it under
    # the name voluptuous, so `vol` is probatio at runtime and Home Assistant's signatures name
    # probatio's classes. mypy cannot follow that swap: it would read the separate voluptuous
    # package, whose Schema is unrelated to the one async_register takes.
    import probatio as vol
else:
    import voluptuous as vol

from .api import HelloFreshAccountData, HelloFreshError, HelloFreshWeek
from .const import (
    ATTR_COLLECTION,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_COURSE,
    ATTR_DATE,
    ATTR_DELIVERY_INTERVAL,
    ATTR_DELIVERY_OPTION,
    ATTR_FILTERS,
    ATTR_GOALS,
    ATTR_HOUSEHOLD,
    ATTR_LIMIT,
    ATTR_MARKET_ITEM_ID,
    ATTR_MARKET_QUANTITIES,
    ATTR_MEAL,
    ATTR_OFFSET,
    ATTR_PRODUCT_HANDLE,
    ATTR_QUANTITIES,
    ATTR_RECIPE_ID,
    ATTR_RECIPE_IDS,
    ATTR_SEARCH,
    ATTR_SERVINGS,
    ATTR_START_TIME,
    ATTR_SUBSCRIPTION_ID,
    ATTR_TASTE,
    ATTR_TEXT,
    ATTR_WEEK_ID,
    CONF_COUNTRY,
    CONF_DELIVERY_TRACKING_REFRESH_INTERVAL_SECONDS,
    CONF_DELIVERY_WATCH_INTERVAL_MINUTES,
    CONF_SCAN_INTERVAL_MINUTES,
    DEFAULT_DELIVERY_TRACKING_REFRESH_INTERVAL_SECONDS,
    DEFAULT_DELIVERY_WATCH_INTERVAL_MINUTES,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    SERVICE_ADD_FAVORITE,
    SERVICE_CHANGE_DELIVERY_WEEKDAY,
    SERVICE_CHANGE_PLAN,
    SERVICE_CHANGE_WEEK_SERVINGS,
    SERVICE_CLEAR_PLANNED_MEAL,
    SERVICE_GET_ACCOUNT_SUMMARY,
    SERVICE_GET_CATALOG_RECIPES,
    SERVICE_GET_DELIVERY_OPTIONS,
    SERVICE_GET_DELIVERY_TRACKING,
    SERVICE_GET_FAVORITES,
    SERVICE_GET_FOOD_PROFILE,
    SERVICE_GET_MEAL_PLAN,
    SERVICE_GET_MENU_COURSES,
    SERVICE_GET_PLAN_OPTIONS,
    SERVICE_GET_PLANS,
    SERVICE_GET_PRESETS,
    SERVICE_GET_RECIPE_COLLECTIONS,
    SERVICE_GET_RECIPE_DETAIL,
    SERVICE_GET_SPENDING,
    SERVICE_GET_WEEKS,
    SERVICE_PREVIEW_MEAL_PRICE,
    SERVICE_REFRESH_DATA,
    SERVICE_REMOVE_FAVORITE,
    SERVICE_RESCHEDULE_WEEK,
    SERVICE_SELECT_MARKET_ITEMS,
    SERVICE_SELECT_MEALS,
    SERVICE_SET_FOOD_PROFILE,
    SERVICE_SET_PLANNED_MEAL,
    SERVICE_SKIP_WEEK,
    SERVICE_UNSKIP_WEEK,
    TRACEY_COUNTRIES,
)
from .coordinator import (
    HelloFreshDataUpdateCoordinator,
    async_client_error,
    async_loaded_coordinators,
    async_write_succeeded,
)
from .meal_plan import COURSES, MEALS, MealPlan
from .parsers import dict_or_empty
from .sensor_helpers import sensor_native_value

ATTR_INCLUDE_RECIPES = "include_recipes"
# A maintainer's switch on get_weeks, deliberately absent from services.yaml.
ATTR_INCLUDE_DEBUG = "include_debug"

# What a handler answers with: a JSON object, or None for a service that answers nothing. The
# values come from the models' as_dict() and the client's readers, JSON-safe but typed as
# dict[str, Any]. Home Assistant's ServiceResponse would refuse a list of those, because its
# JSON list type is invariant; the wrapper below returns the answer as a ServiceResponse.
type HandlerResponse = dict[str, Any] | None

type ServiceHandler = Callable[
    [HelloFreshDataUpdateCoordinator, ServiceCall], Awaitable[HandlerResponse]
]


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    """One service: its name, what runs it, the fields it takes and whether it answers.

    ``fields`` leaves out ``config_entry_id``, which every service accepts.
    """

    name: str
    handler: ServiceHandler
    fields: dict[vol.Marker, Any] = field(default_factory=dict)
    supports_response: SupportsResponse = SupportsResponse.NONE


# ---- which account, and what a failure becomes -------------------------------------------


def _target_coordinator(call: ServiceCall) -> HelloFreshDataUpdateCoordinator:
    """The one account a call acts on: the named entry, or the only loaded one.

    A call never fans out. With several accounts loaded and none named it is refused, because
    guessing would write to the wrong account.
    """
    hass = call.hass
    entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
    if entry_id:
        entry = hass.config_entries.async_get_entry(entry_id)
        # Guard the domain: async_get_entry resolves ANY integration's entry id, and a
        # foreign entry's runtime_data would be some other integration's object.
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(f"HelloFresh config entry not found: {entry_id}")
        # Loaded, not merely "has runtime_data": see async_loaded_coordinators.
        if entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError(f"HelloFresh config entry is not loaded: {entry_id}")
        return entry.runtime_data

    coordinators = async_loaded_coordinators(hass)
    if not coordinators:
        raise HomeAssistantError(
            "No HelloFresh account is currently loaded. Check the integration's "
            "status on the Devices & services page."
        )
    if len(coordinators) > 1:
        raise ServiceValidationError(
            "Multiple HelloFresh accounts are configured. Specify config_entry_id."
        )
    return coordinators[0]


def _service_function(
    spec: ServiceSpec,
) -> Callable[[ServiceCall], Coroutine[Any, Any, ServiceResponse]]:
    """Wrap a handler in what every service does the same way.

    It resolves the account, then turns any HelloFresh failure into the error Home Assistant
    shows (see coordinator.async_client_error for the rule). A handler that let the client's
    own exception through made Home Assistant log an "unexpected exception" traceback for
    what was only an outage, and report an unknown error to the caller.
    """

    async def _handle(call: ServiceCall) -> ServiceResponse:
        coordinator = _target_coordinator(call)
        try:
            return await spec.handler(coordinator, call)
        except HelloFreshError as err:
            raise async_client_error(call.hass, coordinator.config_entry, err) from err

    return _handle


async def _async_write[T](
    coordinator: HelloFreshDataUpdateCoordinator,
    call: ServiceCall,
    action: Awaitable[T],
    *,
    refresh: bool = True,
) -> T:
    """Run a state-changing action, then show its result (see ``async_after_write``).

    Returns whatever the action returned (the select services' seamless-downgrade flag).
    ``refresh`` is off for a write the account poll does not read (the food profile), and for
    a favourite, whose snapshot already carries the new heart.
    """
    result = await action
    async_write_succeeded(call.hass, coordinator.config_entry)
    await coordinator.async_after_write(full_refresh=refresh)
    return result


# ---- account data ------------------------------------------------------------------------


async def _refresh_data(coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall) -> None:
    """Refresh the target account's data now, and fail when the refresh does.

    The coordinator records a failed refresh instead of raising it, so this used to report
    success either way and the card's "Refresh now" said it had worked. It is not debounced
    either: someone asking for fresh data just after an automatic refresh gets a new one.
    """
    await coordinator.async_refresh()
    if not coordinator.last_update_success:
        error = coordinator.last_exception
        raise HomeAssistantError(
            f"HelloFresh could not be refreshed: {error}"
            if error
            else "HelloFresh could not be refreshed"
        )


def _refresh_contract(coordinator: HelloFreshDataUpdateCoordinator) -> dict[str, object]:
    """The auto-refresh contract shared by the get_weeks account payload and the summary.

    Cards re-fetch on ``refresh_interval_minutes`` — fetching more often would return the
    coordinator's identical cached data. While a box is due or on the road the delivery-day
    watch updates the data every ``delivery_watch_interval_minutes`` instead, so the cards
    drop to that cadence for as long as ``delivery_in_progress`` is true; otherwise a
    dashboard open on delivery day would keep the stale status for hours.

    ``delivery_in_progress`` depends on today's date, so this is computed for every call and
    never cached: at midnight on delivery day the cards must learn of it from their next
    fetch, not from the next poll.
    """
    options = coordinator.config_entry.options
    return {
        "refresh_interval_minutes": options.get(
            CONF_SCAN_INTERVAL_MINUTES, DEFAULT_SCAN_INTERVAL_MINUTES
        ),
        "delivery_watch_interval_minutes": options.get(
            CONF_DELIVERY_WATCH_INTERVAL_MINUTES, DEFAULT_DELIVERY_WATCH_INTERVAL_MINUTES
        ),
        "delivery_in_progress": coordinator.delivery_in_progress(),
    }


def _week_payload(
    data: HelloFreshAccountData, week: HelloFreshWeek, *, include_recipes: bool
) -> dict[str, Any]:
    """One week for a get_weeks response, with its order attached.

    The order carries tracking, status, carrier, order id and price, so a card can show
    shipment and billing detail alongside the week. It is the order of the same week AND the
    same subscription: week ids are ISO weeks, which two subscriptions share, and paired by
    week id alone the second subscription's week was shown with the first one's tracking,
    status and price.

    Without recipes the week keeps its schedule, selection counts and allowed actions and
    leaves out the menu itself: the recipes, the Market items and the menu's sections and
    filters.
    """
    if include_recipes:
        payload = week.as_dict()
    else:
        payload = {**week.as_summary_dict(), "allowed_actions": week.allowed_actions}
    order = data.get_order_for_week(week.week_id, week.subscription_id)
    payload["order"] = order.as_dict() if order is not None else None
    return payload


def _live_week_fields(week: HelloFreshWeek) -> dict[str, bool]:
    """The week fields that change with the clock, not with the data.

    A week stops being editable the moment its deadline passes. Read fresh for every call,
    so a response built from the cached payloads does not go on calling a locked week open
    until the next poll.
    """
    return {
        "is_editable": week.is_editable,
        "needs_selection": week.needs_selection,
        "auto_picked": week.auto_picked,
    }


def _account_payload(coordinator: HelloFreshDataUpdateCoordinator) -> dict[str, Any]:
    """Account-level fields a card can use when a week has no billed order yet.

    E.g. the recurring plan total (matches the selected_plan_total_price sensor). Also
    carries the configured menu grace window so the card's past-week gating matches the
    integration's (a just-delivered week keeps its browsable menu for this many weeks), and
    the refresh contract so cards re-fetch on the cadence the data actually changes.
    """
    data = coordinator.data
    primary_subscription = data.primary_subscription
    return {
        "selected_plan_total_price": data.selected_plan_total_price,
        "selected_plan_total_price_currency": data.selected_plan_total_price_currency,
        "menu_grace_weeks": coordinator.client.menu_grace_weeks,
        **_refresh_contract(coordinator),
        # The next box's /gw/calculate split (subtotal, shipping, discount, tax, coupon), so
        # the schedule card can show the discount applied to the box it summarizes.
        "next_delivery_price_breakdown": data.next_delivery_price_breakdown,
        "next_delivery_total_currency": data.next_delivery_total_currency,
        # The wallet promise that applies to the next box ("$10 off premium meals"), for
        # the schedule card's Voucher row; each week's own list rides on the week.
        "next_box_discount": data.next_box_discount,
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


async def _get_weeks(
    coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall
) -> HandlerResponse:
    """Return delivery weeks, by default with full recipe detail.

    Read-only. Recipes are intentionally absent from sensor attributes (16 KB recorder
    cap), so this response service is how a dashboard reads per-week recipes/selection on
    demand. Optionally filter to one ``week_id``, or pass ``include_recipes: false`` for
    the weeks without their menus — a single week's menu can be several hundred kilobytes.
    """
    data = coordinator.data
    week_id = call.data.get(ATTR_WEEK_ID)
    include_recipes = call.data.get(ATTR_INCLUDE_RECIPES, True)
    if week_id is not None:
        # Every week with that id, in the full list's order. Two subscriptions share ISO week
        # ids, and returning only the primary subscription's week let a card that re-reads one
        # week merge it over the other subscription's entry.
        weeks = [week for week in data.weeks if week.week_id == week_id]
    else:
        weeks = list(data.weeks)

    if include_recipes and week_id is None:
        # The common case is what the dashboard cards call, often several at once per poll
        # cycle for identical data. Serialize the full weeks list once per coordinator
        # update and reuse it; the cache drops itself when the data changes. Each payload is
        # kept with its week, so the clock-dependent fields below go on the right one.
        serialized = coordinator.get_weeks_response(
            lambda: [(week, _week_payload(data, week, include_recipes=True)) for week in weeks]
        )
    else:
        serialized = [
            (week, _week_payload(data, week, include_recipes=include_recipes)) for week in weeks
        ]

    # Only the serialized weeks are cached. Everything that depends on the clock is added
    # per call: the account block's refresh contract, and each week's editable state.
    response: dict[str, Any] = {
        "weeks": [{**payload, **_live_week_fields(week)} for week, payload in serialized],
        "account": _account_payload(coordinator),
    }
    if call.data.get(ATTR_INCLUDE_DEBUG):
        response["variation_debug"] = [_variation_join_debug(week) for week in weeks]
    return response


def _variation_join_debug(week: HelloFreshWeek) -> dict[str, object]:
    """Diagnostic: expose why a recipe's variant modifier did (not) resolve for a week.

    Returns the modularity ``index`` -> title map the week carries (from its own payload or the
    merged ``_menu_payload``), plus each recipe's ``course_index`` and resolved ``variation_title``.
    A duplicate dish that shows only "Variant" with no modifier means its ``course_index`` is not
    a key in ``modularity_titles`` for that week — this surfaces exactly that mismatch.
    """
    raw = getattr(week, "raw", {}) or {}
    has_modularity = isinstance(raw.get("modularity"), list)
    menu_payload = dict_or_empty(raw.get("_menu_payload"))
    has_menu_modularity = isinstance(menu_payload.get("modularity"), list)
    # Reuse the normalizer's own parser so this reflects the real join logic.
    from .normalizers import HelloFreshPayloadNormalizer  # noqa: PLC0415

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
                "matches_modularity": r.course_index in titles
                if r.course_index is not None
                else False,
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


# The sensor values the account summary repeats, in the card's order.
_SUMMARY_KEYS = (
    "subscription_status",
    "account_id",
    "selected_plan",
    "plan_preference",
    "selected_plan_total_price",
    "account_credit",
    "account_credit_cash",
    "account_credit_bonus",
    "number_of_people",
    "required_meal_count",
    "boxes_received",
    "loyalty_boxes_until_free_box",
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


async def _get_account_summary(
    coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall
) -> HandlerResponse:
    """Return the account/subscription headline values for the subscription card.

    Read-only. Every value comes from the same ``sensor_native_value`` dispatcher that
    backs the corresponding sensor entity, so the card and the sensors can never
    disagree. Dates are ISO strings; empty values are null (the sensors' literal
    "None" placeholder text is normalized back to null for JSON consumers).
    """
    data = coordinator.data

    def _value(key: str) -> object:
        value = sensor_native_value(key, data, coordinator.client.base_url)
        if value == "None":  # NONE_WHEN_EMPTY_KEYS placeholder — null in a response
            return None
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        return value

    summary: dict[str, object] = {key: _value(key) for key in _SUMMARY_KEYS}
    summary["selected_plan_total_price_currency"] = data.selected_plan_total_price_currency
    summary["account_credit_currency"] = data.account_credit_currency
    # Week ids behind the counters, so the card can make them clickable (the click
    # broadcasts the cross-card week-sync event to jump the other cards there).
    summary["weeks_needing_selection_ids"] = [week.week_id for week in data.weeks_auto_picked]
    summary["next_skipped_week_id"] = (
        data.next_skipped_week.week_id if data.next_skipped_week else None
    )
    # Same auto-refresh contract as get_weeks: the card re-pulls on the poll cadence.
    summary.update(_refresh_contract(coordinator))
    # Plan price split and payment-method health (card brand, last four digits and
    # expiry — never the billing address), for the subscription card's rows and banner.
    summary["selected_plan_price_breakdown"] = data.selected_plan_price_breakdown
    summary["payment_method_expiring"] = data.payment_method_expiring
    summary["payment_method_expired"] = data.payment_method_expired
    summary["payment_card_type"] = data.payment_card_type
    summary["payment_card_provider"] = data.payment_card_provider
    summary["payment_card_brand"] = data.payment_card_brand
    summary["payment_card_last4"] = data.payment_card_last4
    summary["payment_card_expiry"] = data.payment_card_expiry
    summary["next_box_discount"] = data.next_box_discount
    # The boxes HelloFresh could not charge (binary_sensor.payment_problem and its weeks):
    # each {week_id, delivery_date, status, amount, currency}, oldest first.
    summary["payment_problem"] = data.payment_problem
    summary["payment_problem_weeks"] = [dict(week) for week in data.payment_problem_weeks]
    return summary


async def _get_delivery_tracking(
    coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall
) -> HandlerResponse:
    """Return the live last-mile tracking snapshot.

    Read-only. Only meaningful for accounts in TRACEY_COUNTRIES (the Netherlands and
    Germany): elsewhere the response is ``{"available": false}``. When available,
    this triggers a live fetch of the Tracey endpoint (throttled to one request a
    minute); the sensors poll it on the configured live-tracking cadence.
    """
    country = coordinator.config_entry.data.get(CONF_COUNTRY)
    # The next delivery date gives the card something useful to say while no delivery
    # is actively trackable ("nothing on the road until Tuesday").
    next_delivery = sensor_native_value(
        "next_delivery_date", coordinator.data, coordinator.client.base_url
    )
    base: dict[str, object] = {
        "country": country,
        "delivery_tracking_refresh_interval_seconds": (
            coordinator.config_entry.options.get(
                CONF_DELIVERY_TRACKING_REFRESH_INTERVAL_SECONDS,
                DEFAULT_DELIVERY_TRACKING_REFRESH_INTERVAL_SECONDS,
            )
            if country in TRACEY_COUNTRIES
            else None
        ),
        "next_delivery_date": (
            next_delivery.isoformat()
            if isinstance(next_delivery, (datetime, date))
            else next_delivery
        ),
    }
    tracey = coordinator.tracey
    if tracey is None:
        return {**base, "available": False, "reason": "unsupported_country"}
    snapshot = await tracey.async_fetch_latest()
    return {**base, "available": True, **snapshot.as_dict()}


# ---- food profile, plans and spending ----------------------------------------------------


async def _get_food_profile(
    coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall
) -> HandlerResponse:
    """Return the customer's food profile plus the full catalog of selectable options.

    Read-only. Fetched live from HelloFresh's profile-service (it is not part of the
    regular sensor poll), so the Food Profile card can read current picks and every
    possible option in one call. The three reads do not depend on each other, so they are
    sent together.
    """
    client = coordinator.client
    profile, options, completion = await asyncio.gather(
        client.async_get_food_profile(),
        client.async_get_food_profile_options(),
        # How much of the profile is filled in, so the card can show a progress figure.
        # Best-effort — None when HelloFresh doesn't answer, and the card just omits it.
        client.async_get_food_profile_completion(),
    )
    return {
        "profile": profile.as_dict(),
        "options": options.as_dict(),
        "completion": completion.as_dict() if completion is not None else None,
    }


async def _set_food_profile(
    coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall
) -> HandlerResponse:
    """Update the customer's food profile (auto-preselection preferences).

    Accepts partial ``taste`` / ``household`` / ``goals`` sections; only what is provided
    is changed. Returns the saved profile so the card can re-render from the server's truth.
    """
    changes = {
        section: call.data[section]
        for section in (ATTR_TASTE, ATTR_HOUSEHOLD, ATTR_GOALS)
        if section in call.data
    }
    if not changes:
        raise ServiceValidationError("Provide at least one of: taste, household, goals.")
    # The profile is not part of the account poll, so there is nothing to re-read.
    saved = await _async_write(
        coordinator, call, coordinator.client.async_update_food_profile(changes), refresh=False
    )
    return {"profile": saved.as_dict()}


async def _get_delivery_options(
    coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall
) -> HandlerResponse:
    """Return the plan's selectable delivery days (weekday, name, price, default).

    Read-only. Richer than a week's ``availableOneOffOptions`` — the full delivery-day
    picker the web app uses. Fetched live; not part of the sensor poll.
    """
    options = await coordinator.client.async_get_delivery_options()
    return {"delivery_options": [option.as_dict() for option in options]}


async def _get_plans(
    coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall
) -> HandlerResponse:
    """Return the account's plan catalog (product handle, price, status). Read-only."""
    return {"plans": await coordinator.client.async_get_plans()}


async def _get_presets(
    coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall
) -> HandlerResponse:
    """Return the region's menu presets (Chef's Choice, Veggie, …). Read-only."""
    return {"presets": await coordinator.client.async_get_presets()}


async def _get_spending(
    coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall
) -> HandlerResponse:
    """Return the customer's spending ledger (weeks, monthly rollup, running total).

    Read-only. Built from the full billing history so the running total reflects lifetime
    HelloFresh spend, not just the schedule window. Powers the cost card.
    """
    return await coordinator.client.async_get_spending()


async def _get_plan_options(
    coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall
) -> HandlerResponse:
    """Return the box sizes this subscription can switch to.

    Read-only, fetched live (the plan catalog is not part of the regular poll). Pairs with
    ``change_plan``: the ``handle`` of any returned option is what that service accepts.
    """
    options = await coordinator.client.async_list_plan_options(call.data.get(ATTR_SUBSCRIPTION_ID))
    return {"options": options}


# ---- the Week tab's meal plan ------------------------------------------------------------


def _meal_plan(coordinator: HelloFreshDataUpdateCoordinator) -> MealPlan:
    """The entry's meal plan.

    The coordinator's attribute is None only during setup, which mounts the plan before the
    entry counts as loaded, and services act on loaded entries only (_target_coordinator).
    """
    return cast(MealPlan, coordinator.meal_plan)


async def _get_meal_plan(
    coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall
) -> HandlerResponse:
    """Return the Week tab's meal plan: its settings and every planned day.

    Read-only. Each planned meal carries its computed ``start`` and ``end``.
    """
    return _meal_plan(coordinator).as_dict()


async def _set_planned_meal(
    coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall
) -> HandlerResponse:
    """Plan one course of a day's meal (a box meal, a Market item or a note), and/or move
    that meal's start time for the day. Returns the updated plan."""
    meal_plan = _meal_plan(coordinator)
    data = call.data
    await meal_plan.async_set(
        data[ATTR_DATE],
        data[ATTR_MEAL],
        data[ATTR_COURSE],
        recipe_id=data.get(ATTR_RECIPE_ID),
        market_item_id=data.get(ATTR_MARKET_ITEM_ID),
        text=data.get(ATTR_TEXT),
        week_id=data.get(ATTR_WEEK_ID),
        start_time=data.get(ATTR_START_TIME),
    )
    return meal_plan.as_dict()


async def _clear_planned_meal(
    coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall
) -> HandlerResponse:
    """Clear one course of a day's meal, or the whole meal. Returns the updated plan."""
    meal_plan = _meal_plan(coordinator)
    meal_plan.clear(call.data[ATTR_DATE], call.data[ATTR_MEAL], call.data.get(ATTR_COURSE))
    return meal_plan.as_dict()


# ---- cookbook and recipe catalog ---------------------------------------------------------


async def _get_favorites(
    coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall
) -> HandlerResponse:
    """Return the customer's cookbook bookmarks.

    With no ``recipe_ids``, the whole cookbook is listed with full detail (title, image,
    times, nutrition). Passing ``recipe_ids`` instead asks the cheaper filter endpoint
    "which of THESE are bookmarked?", which returns ids only — useful for decorating a
    menu you already have in hand.
    """
    client = coordinator.client
    recipe_ids = list(call.data.get(ATTR_RECIPE_IDS) or [])
    if not recipe_ids:
        # HelloFresh also runs a second, separate favorites store ("cfs") behind its
        # /recipes/favorites page. It is reported alongside rather than merged: the two
        # are not synchronized, and in all captured traffic it was empty while the
        # cookbook held bookmarks. Surfacing it separately means a user who does have
        # rows there can see them without the counts silently disagreeing. The two stores
        # are read together.
        favorites, secondary = await asyncio.gather(
            client.async_list_favorites(), client.async_get_cfs_favorites()
        )
        return {
            "favorites": [favorite.as_dict() for favorite in favorites],
            "favorite_recipe_ids": sorted({f.recipe_id for f in favorites}),
            "total": len(favorites),
            "secondary_favorites": secondary,
        }
    found = await client.async_get_favorite_recipe_ids(recipe_ids)
    return {
        "favorites": [favorite.as_dict() for favorite in found.values()],
        "favorite_recipe_ids": sorted(found),
        "checked_count": len(recipe_ids),
    }


async def _add_favorite(
    coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall
) -> HandlerResponse:
    """Bookmark a recipe in the customer's cookbook."""
    favorite = await _async_write(
        coordinator,
        call,
        coordinator.client.async_add_favorite(call.data[ATTR_RECIPE_ID]),
        refresh=False,
    )
    return {"favorite": favorite.as_dict()}


async def _remove_favorite(
    coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall
) -> HandlerResponse:
    """Remove a recipe bookmark.

    The delete (``DELETE /gw/cookbook/v1/external-recipes/{id}``) is HAR-verified, and
    HelloFresh's app uses the same route. A failure is explained in the error.
    """
    removed = await _async_write(
        coordinator,
        call,
        coordinator.client.async_remove_favorite(call.data[ATTR_RECIPE_ID]),
        refresh=False,
    )
    return {"removed": removed}


async def _get_recipe_detail(
    coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall
) -> HandlerResponse:
    """Return one recipe's full cooking detail.

    Unlike the browse catalog, this reads a plain HelloFresh API rather than the website's
    Next.js data URLs, so it does not depend on the site's build id.
    """
    detail = await coordinator.client.async_get_recipe_detail(
        call.data[ATTR_RECIPE_ID],
        servings=call.data.get(ATTR_SERVINGS),
    )
    return {"recipe": detail.as_dict()}


async def _get_recipe_collections(
    coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall
) -> HandlerResponse:
    """Return the browsable categories of the public recipe catalog."""
    collections = await coordinator.client.async_get_recipe_collections()
    return {"collections": [collection.as_dict() for collection in collections]}


async def _get_catalog_recipes(
    coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall
) -> HandlerResponse:
    """Return recipes from the public catalog, optionally filtered to one collection.

    With ``search``, text-search the WHOLE catalog instead (every category at once, via
    the recipes-service search API); ``collection`` is ignored then, and no
    subcollections are returned since results span categories.

    ``limit`` recipes from ``offset`` on: the response says where the page stands with
    ``offset``, ``total`` (how many there are, or null when HelloFresh doesn't say) and
    ``has_more``, so a caller can ask for the next page.
    """
    query = str(call.data.get(ATTR_SEARCH) or "").strip()
    limit = int(call.data.get(ATTR_LIMIT, 50))
    offset = int(call.data.get(ATTR_OFFSET, 0))
    if query:
        found = await coordinator.client.async_search_catalog_page(
            query, limit=limit, offset=offset
        )
        return {
            "collection": None,
            "search": query,
            "recipes": [recipe.as_dict() for recipe in found["recipes"]],
            "subcollections": [],
            **_page_position(found),
        }
    page = await coordinator.client.async_get_catalog_page(
        call.data.get(ATTR_COLLECTION),
        limit=limit,
        offset=offset,
    )
    return {
        "collection": page["collection"],
        "recipes": [recipe.as_dict() for recipe in page["recipes"]],
        # Child categories of this collection (Noodle -> Ramen/Udon/…). Not present in the
        # top-level category list, so this is the only way to reach them.
        "subcollections": [sub.as_dict() for sub in page["subcollections"]],
        **_page_position(page),
    }


def _page_position(page: dict[str, Any]) -> dict[str, Any]:
    """Where a catalog page stands: its offset, the total (or None) and whether more follow."""
    total = page.get("total")
    known = isinstance(total, int) and not isinstance(total, bool) and total >= 0
    return {
        "offset": int(page.get("offset") or 0),
        "total": total if known else None,
        "has_more": bool(page.get("has_more")),
    }


async def _get_menu_courses(
    coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall
) -> HandlerResponse:
    """Resolve a week's menu through HelloFresh's own server-side filters.

    Backs the meal-planner card's Cuisine / Dish type / Ingredients-to-avoid groups: menu
    recipes carry no allergen data and their tag slugs don't match the site's cuisine and
    dish-type option slugs, so only the site's filter service can answer these exactly.
    Read-only, fetched live; returns only recipe ids to intersect with the loaded menu.
    """
    week_id = call.data[ATTR_WEEK_ID]
    filters = call.data.get(ATTR_FILTERS) or {}
    recipe_ids = await coordinator.client.async_get_menu_courses(week_id, filters)
    return {
        "week_id": week_id,
        "recipe_ids": recipe_ids,
        "count": len(recipe_ids),
        "filters": {
            str(group): (values if isinstance(values, list) else [values])
            for group, values in filters.items()
        },
    }


# ---- changes to a week -------------------------------------------------------------------


async def _preview_meal_price(
    coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall
) -> HandlerResponse:
    """Price a hypothetical meal selection without saving it."""
    return await coordinator.client.async_preview_meal_price(
        call.data[ATTR_WEEK_ID],
        list(call.data[ATTR_RECIPE_IDS]),
        dict(call.data.get(ATTR_QUANTITIES) or {}),
    )


def _notify_seamless_downgrade(
    hass: HomeAssistant, coordinator: HelloFreshDataUpdateCoordinator, week_id: str
) -> None:
    """Warn the user that HelloFresh silently shrank the box to fit the new selection.

    The cart write succeeded but HelloFresh applied a seamless downgrade, so fewer meals
    were saved than requested. A persistent notification is the surfacing that works no
    matter which path (service call or dashboard card) triggered the write.
    """
    persistent_notification.async_create(
        hass,
        (
            f"HelloFresh accepted your meal change for week {week_id} but **downsized the "
            "box** to fit — fewer meals were saved than you selected. Open the meal planner "
            "to review the saved selection and re-pick if needed."
        ),
        title="HelloFresh box downsized",
        notification_id=f"{DOMAIN}_seamless_downgrade_{coordinator.config_entry.entry_id}_{week_id}",
    )


async def _async_select(
    coordinator: HelloFreshDataUpdateCoordinator,
    call: ServiceCall,
    week_id: str,
    action: Awaitable[Any],
) -> HandlerResponse:
    """Run a meal or Market selection write and report a seamless downgrade.

    Returns ``{"downgraded": bool}`` — ``True`` when HelloFresh accepted the write but
    silently shrank the box to fit. A card can read this via ``return_response=true`` to
    show an inline warning right where the edit happened; a persistent notification is
    raised as well, so the warning reaches the user either way.
    """
    downgraded = bool(await _async_write(coordinator, call, action))
    if downgraded:
        _notify_seamless_downgrade(call.hass, coordinator, week_id)
    return {"downgraded": downgraded}


async def _select_meals(
    coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall
) -> HandlerResponse:
    """Submit meal selections. Returns ``{"downgraded": bool}`` (see ``_async_select``)."""
    week_id = call.data[ATTR_WEEK_ID]
    recipe_ids = list(call.data[ATTR_RECIPE_IDS])
    quantities = dict(call.data.get(ATTR_QUANTITIES) or {})
    # Present (even empty) = set the Market add-ons in this same cart write; absent = keep
    # them. An empty map therefore clears them, exactly like select_market_items.
    market_quantities = (
        dict(call.data[ATTR_MARKET_QUANTITIES]) if ATTR_MARKET_QUANTITIES in call.data else None
    )
    return await _async_select(
        coordinator,
        call,
        week_id,
        coordinator.client.async_select_meals(week_id, recipe_ids, quantities, market_quantities),
    )


async def _select_market_items(
    coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall
) -> HandlerResponse:
    """Submit HelloFresh Market add-on (extras) selections for a week.

    Returns ``{"downgraded": bool}`` (see ``_async_select``).
    """
    week_id = call.data[ATTR_WEEK_ID]
    quantities = dict(call.data.get(ATTR_QUANTITIES) or {})
    return await _async_select(
        coordinator,
        call,
        week_id,
        coordinator.client.async_select_market_items(week_id, quantities),
    )


async def _skip_week(coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall) -> None:
    """Skip a delivery week."""
    await _async_write(
        coordinator, call, coordinator.client.async_skip_week(call.data[ATTR_WEEK_ID])
    )


async def _unskip_week(coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall) -> None:
    """Restore a skipped delivery week."""
    await _async_write(
        coordinator, call, coordinator.client.async_unskip_week(call.data[ATTR_WEEK_ID])
    )


async def _reschedule_week(coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall) -> None:
    """Reschedule a single delivery week to a different delivery option."""
    await _async_write(
        coordinator,
        call,
        coordinator.client.async_change_one_off_delivery(
            call.data[ATTR_WEEK_ID], call.data[ATTR_DELIVERY_OPTION]
        ),
    )


async def _change_week_servings(
    coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall
) -> HandlerResponse:
    """Change how many people one week's box is for (experimental; the plan stays).

    Returns ``{week_id, servings, product_handle, price, currency}``. A box already for that
    many people is left alone: nothing is written, so nothing needs showing or clearing.
    """
    result = dict(
        await coordinator.client.async_change_week_servings(
            call.data[ATTR_WEEK_ID], call.data[ATTR_SERVINGS]
        )
    )
    if result.pop("changed", True):
        async_write_succeeded(call.hass, coordinator.config_entry)
        await coordinator.async_after_write()
    return result


# ---- changes to the plan -----------------------------------------------------------------


async def _change_plan(coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall) -> None:
    """Change the recurring box size (meals per week x servings).

    This changes what the account is billed for every future box, so it is deliberately a
    separate service from the per-week meal selection.
    """
    await _async_write(
        coordinator,
        call,
        coordinator.client.async_change_plan(
            call.data[ATTR_PRODUCT_HANDLE], call.data.get(ATTR_SUBSCRIPTION_ID)
        ),
    )


async def _change_delivery_weekday(
    coordinator: HelloFreshDataUpdateCoordinator, call: ServiceCall
) -> None:
    """Change the recurring delivery day and slot for a subscription's plan.

    The request HelloFresh accepts carries the delivery option only, so it cannot change
    how often boxes come. An interval other than weekly used to be accepted and then
    ignored: the call reported success while boxes and charges stayed weekly. It is refused
    now, before anything is sent.
    """
    delivery_interval = call.data.get(ATTR_DELIVERY_INTERVAL, 1)
    if delivery_interval != 1:
        raise ServiceValidationError(
            f"delivery_interval {delivery_interval} is not supported: this service changes the "
            "delivery day only and cannot change how often boxes come. Leave "
            "delivery_interval out, or set it to 1."
        )
    await _async_write(
        coordinator,
        call,
        coordinator.client.async_change_delivery_weekday(
            call.data[ATTR_DELIVERY_OPTION], delivery_interval, call.data.get(ATTR_SUBSCRIPTION_ID)
        ),
    )


# ---- the table ---------------------------------------------------------------------------

_SERVING_COUNTS = {str: vol.All(vol.Coerce(int), vol.Range(min=1))}
# Market quantities may be 0, which removes the item.
_MARKET_COUNTS = {str: vol.All(vol.Coerce(int), vol.Range(min=0))}

SERVICES: tuple[ServiceSpec, ...] = (
    ServiceSpec(SERVICE_REFRESH_DATA, _refresh_data),
    ServiceSpec(
        SERVICE_GET_WEEKS,
        _get_weeks,
        {
            vol.Optional(ATTR_WEEK_ID): str,
            vol.Optional(ATTR_INCLUDE_RECIPES, default=True): cv.boolean,
            vol.Optional(ATTR_INCLUDE_DEBUG): cv.boolean,
        },
        SupportsResponse.ONLY,
    ),
    ServiceSpec(
        SERVICE_GET_MENU_COURSES,
        _get_menu_courses,
        {
            vol.Required(ATTR_WEEK_ID): str,
            vol.Optional(ATTR_FILTERS): vol.Schema(
                {str: vol.Any(str, [str])}, extra=vol.PREVENT_EXTRA
            ),
        },
        SupportsResponse.ONLY,
    ),
    ServiceSpec(
        SERVICE_GET_ACCOUNT_SUMMARY,
        _get_account_summary,
        supports_response=SupportsResponse.ONLY,
    ),
    ServiceSpec(
        SERVICE_GET_FOOD_PROFILE,
        _get_food_profile,
        supports_response=SupportsResponse.ONLY,
    ),
    ServiceSpec(
        SERVICE_SET_FOOD_PROFILE,
        _set_food_profile,
        {
            # Each section is a free-form mapping mirroring the API's profile shape; the
            # client normalizes weighted taste fields to +/-100 before PATCHing.
            vol.Optional(ATTR_TASTE): dict,
            vol.Optional(ATTR_HOUSEHOLD): dict,
            vol.Optional(ATTR_GOALS): dict,
        },
        SupportsResponse.OPTIONAL,
    ),
    ServiceSpec(
        SERVICE_SELECT_MEALS,
        _select_meals,
        {
            vol.Required(ATTR_WEEK_ID): str,
            vol.Required(ATTR_RECIPE_IDS): vol.All([str], vol.Length(min=1)),
            # Optional per-recipe serving counts: {recipe_id: positive int}. Recipes absent
            # from the map default to one serving.
            vol.Optional(ATTR_QUANTITIES): _SERVING_COUNTS,
            # Optional Market add-ons written in the SAME cart update, with
            # select_market_items' shape ({item id/sku/index: quantity}, 0 removes).
            vol.Optional(ATTR_MARKET_QUANTITIES): _MARKET_COUNTS,
        },
        SupportsResponse.OPTIONAL,
    ),
    ServiceSpec(
        SERVICE_SELECT_MARKET_ITEMS,
        _select_market_items,
        {
            vol.Required(ATTR_WEEK_ID): str,
            # Map of market item id/sku/index -> quantity (>=1). Items omitted (or set to 0)
            # are removed from the week's extras.
            vol.Required(ATTR_QUANTITIES): _MARKET_COUNTS,
        },
        SupportsResponse.OPTIONAL,
    ),
    ServiceSpec(SERVICE_SKIP_WEEK, _skip_week, {vol.Required(ATTR_WEEK_ID): str}),
    ServiceSpec(SERVICE_UNSKIP_WEEK, _unskip_week, {vol.Required(ATTR_WEEK_ID): str}),
    ServiceSpec(
        SERVICE_RESCHEDULE_WEEK,
        _reschedule_week,
        {vol.Required(ATTR_WEEK_ID): str, vol.Required(ATTR_DELIVERY_OPTION): str},
    ),
    ServiceSpec(
        SERVICE_CHANGE_WEEK_SERVINGS,
        _change_week_servings,
        {
            vol.Required(ATTR_WEEK_ID): str,
            # The number of people; which ones a box comes in is checked against the plan's
            # box sizes before anything is written.
            vol.Required(ATTR_SERVINGS): vol.All(vol.Coerce(int), vol.Range(min=1, max=12)),
        },
        SupportsResponse.OPTIONAL,
    ),
    ServiceSpec(
        SERVICE_CHANGE_DELIVERY_WEEKDAY,
        _change_delivery_weekday,
        {
            vol.Required(ATTR_DELIVERY_OPTION): str,
            # Kept so existing calls that pass 1 still validate; the handler refuses
            # anything else, because the request cannot carry an interval.
            vol.Optional(ATTR_DELIVERY_INTERVAL, default=1): vol.Coerce(int),
            vol.Optional(ATTR_SUBSCRIPTION_ID): str,
        },
    ),
    ServiceSpec(
        SERVICE_GET_PLAN_OPTIONS,
        _get_plan_options,
        {vol.Optional(ATTR_SUBSCRIPTION_ID): str},
        SupportsResponse.ONLY,
    ),
    ServiceSpec(
        SERVICE_CHANGE_PLAN,
        _change_plan,
        {vol.Required(ATTR_PRODUCT_HANDLE): str, vol.Optional(ATTR_SUBSCRIPTION_ID): str},
    ),
    ServiceSpec(
        SERVICE_GET_DELIVERY_OPTIONS,
        _get_delivery_options,
        supports_response=SupportsResponse.ONLY,
    ),
    ServiceSpec(
        SERVICE_GET_DELIVERY_TRACKING,
        _get_delivery_tracking,
        supports_response=SupportsResponse.ONLY,
    ),
    ServiceSpec(SERVICE_GET_PLANS, _get_plans, supports_response=SupportsResponse.ONLY),
    ServiceSpec(SERVICE_GET_PRESETS, _get_presets, supports_response=SupportsResponse.ONLY),
    ServiceSpec(SERVICE_GET_SPENDING, _get_spending, supports_response=SupportsResponse.ONLY),
    ServiceSpec(
        SERVICE_GET_FAVORITES,
        _get_favorites,
        # Optional: with none given the whole cookbook is listed.
        {vol.Optional(ATTR_RECIPE_IDS): [str]},
        SupportsResponse.ONLY,
    ),
    ServiceSpec(
        SERVICE_ADD_FAVORITE,
        _add_favorite,
        {vol.Required(ATTR_RECIPE_ID): str},
        SupportsResponse.OPTIONAL,
    ),
    ServiceSpec(
        SERVICE_REMOVE_FAVORITE,
        _remove_favorite,
        {vol.Required(ATTR_RECIPE_ID): str},
        SupportsResponse.OPTIONAL,
    ),
    ServiceSpec(
        SERVICE_GET_RECIPE_COLLECTIONS,
        _get_recipe_collections,
        supports_response=SupportsResponse.ONLY,
    ),
    ServiceSpec(
        SERVICE_GET_CATALOG_RECIPES,
        _get_catalog_recipes,
        {
            # Collection slug from get_recipe_collections; omit for the top-level listing.
            vol.Optional(ATTR_COLLECTION): str,
            # Free-text search across the WHOLE catalog; overrides `collection`.
            vol.Optional(ATTR_SEARCH): str,
            vol.Optional(ATTR_LIMIT, default=50): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=200)
            ),
            # How many recipes to pass over first: the next page starts at offset + limit.
            vol.Optional(ATTR_OFFSET, default=0): vol.All(vol.Coerce(int), vol.Range(min=0)),
        },
        SupportsResponse.ONLY,
    ),
    ServiceSpec(
        SERVICE_PREVIEW_MEAL_PRICE,
        _preview_meal_price,
        {
            vol.Required(ATTR_WEEK_ID): str,
            vol.Required(ATTR_RECIPE_IDS): vol.All([str], vol.Length(min=1)),
            vol.Optional(ATTR_QUANTITIES): _SERVING_COUNTS,
        },
        SupportsResponse.ONLY,
    ),
    ServiceSpec(
        SERVICE_GET_RECIPE_DETAIL,
        _get_recipe_detail,
        {
            vol.Required(ATTR_RECIPE_ID): str,
            # Which yields entry the ingredient amounts are scaled to (2, 4, ...).
            # Defaults to the smallest the recipe offers.
            vol.Optional(ATTR_SERVINGS): vol.All(vol.Coerce(int), vol.Range(min=1, max=12)),
        },
        SupportsResponse.ONLY,
    ),
    ServiceSpec(SERVICE_GET_MEAL_PLAN, _get_meal_plan, supports_response=SupportsResponse.ONLY),
    ServiceSpec(
        SERVICE_SET_PLANNED_MEAL,
        _set_planned_meal,
        {
            vol.Required(ATTR_DATE): cv.date,
            vol.Optional(ATTR_MEAL, default="dinner"): vol.In(MEALS),
            vol.Optional(ATTR_COURSE, default="main"): vol.In(COURSES),
            # One of these three: a meal from the box, a Market item, or a note.
            vol.Optional(ATTR_RECIPE_ID): str,
            vol.Optional(ATTR_MARKET_ITEM_ID): str,
            vol.Optional(ATTR_TEXT): str,
            # The box the meal or item came from, when the date alone is ambiguous.
            vol.Optional(ATTR_WEEK_ID): str,
            vol.Optional(ATTR_START_TIME): cv.time,
        },
        SupportsResponse.OPTIONAL,
    ),
    ServiceSpec(
        SERVICE_CLEAR_PLANNED_MEAL,
        _clear_planned_meal,
        {
            vol.Required(ATTR_DATE): cv.date,
            vol.Optional(ATTR_MEAL, default="dinner"): vol.In(MEALS),
            vol.Optional(ATTR_COURSE): vol.In(COURSES),
        },
        SupportsResponse.OPTIONAL,
    ),
)


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register every HelloFresh service, once for the whole integration."""
    if hass.services.has_service(DOMAIN, SERVICE_REFRESH_DATA):
        return
    for spec in SERVICES:
        hass.services.async_register(
            DOMAIN,
            spec.name,
            _service_function(spec),
            schema=vol.Schema({vol.Optional(ATTR_CONFIG_ENTRY_ID): str, **spec.fields}),
            supports_response=spec.supports_response,
        )
