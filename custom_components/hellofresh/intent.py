"""Intent handlers for HelloFresh."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import cast

from homeassistant.helpers import intent
from homeassistant.util import dt as dt_util

from .const import (
    INTENT_GET_MEAL_SELECTION,
    INTENT_GET_NEXT_DELIVERY,
    INTENT_REFRESH,
)
from .coordinator import async_loaded_coordinators
from .sensor_helpers import humanize_status


def async_register_intents(hass) -> None:
    """Register HelloFresh intent handlers."""
    intent.async_register(hass, HelloFreshNextDeliveryIntentHandler())
    intent.async_register(hass, HelloFreshMealSelectionIntentHandler())
    intent.async_register(hass, HelloFreshRefreshIntentHandler())


async def async_setup_intents(hass) -> None:
    """Entry point called by Home Assistant's intent platform loader.

    HA discovers this module as an intent platform and invokes ``async_setup_intents``;
    without it the loader logged an AttributeError at startup. ``async_setup`` already
    registers the handlers (guarded by INTENTS_REGISTERED_KEY) so they exist even if the
    intent component is not loaded — re-registering here would just log "is being
    overwritten" warnings. This hook intentionally does nothing beyond existing.
    """
    return


# The handlers answer in English, so dates and times are worded the way English says them.
# A spoken "2026-11-17T04:59:59+00:00" is noise, and for anyone west of UTC it is also the
# wrong day: a Monday 23:59 cutoff in New York is already Tuesday in UTC.


def _spoken_date(value: date) -> str:
    """A calendar day as it is said: "Tuesday, November 17"."""
    return f"{value:%A}, {value:%B} {value.day}"


def _spoken_time(value: datetime) -> str:
    """A time of day in Home Assistant's time zone: "11:59 PM"."""
    local = dt_util.as_local(value)
    return f"{local.hour % 12 or 12}:{local:%M} {'AM' if local.hour < 12 else 'PM'}"


def _spoken_deadline(value: datetime) -> str:
    """A selection deadline in Home Assistant's time zone: "Monday, November 16 at 11:59 PM"."""
    # A deadline without an offset is UTC, as it is everywhere else in the integration.
    local = dt_util.as_local(value if value.tzinfo is not None else value.replace(tzinfo=UTC))
    return f"{_spoken_date(local.date())} at {_spoken_time(local)}"


class HelloFreshBaseIntentHandler(intent.IntentHandler):
    """Base HelloFresh intent handler."""

    slot_schema = {}
    platforms = {"conversation"}

    def _response(self, intent_obj, speech: str):
        """Create a simple speech response."""
        response = intent_obj.create_response()
        response.async_set_speech(speech)
        return response


class HelloFreshNextDeliveryIntentHandler(HelloFreshBaseIntentHandler):
    """Report the next HelloFresh delivery."""

    intent_type = INTENT_GET_NEXT_DELIVERY

    async def async_handle(self, intent_obj):
        """Handle the intent."""
        coordinators = async_loaded_coordinators(intent_obj.hass)
        if not coordinators:
            return self._response(
                intent_obj,
                "HelloFresh is not configured yet.",
            )

        next_orders = [
            (coordinator.config_entry.title, coordinator.data.next_order)
            for coordinator in coordinators
            if coordinator.data.next_order is not None
        ]
        if not next_orders:
            return self._response(
                intent_obj,
                "I couldn't find an upcoming HelloFresh delivery right now.",
            )

        title, order = min(
            next_orders,
            # A next order is an upcoming one, and only orders with a delivery date are
            # upcoming (HelloFreshAccountData), so the date is never None here.
            key=lambda item: cast(date, item[1].delivery_date),
        )
        delivery_date = (
            _spoken_date(order.delivery_date) if order.delivery_date else "an unknown date"
        )
        speech = f"The next HelloFresh delivery for {title} is scheduled for {delivery_date}."
        if order.status:
            # "ON_THE_WAY" is said "on the way", the wording the status sensors show.
            speech = f"{speech} Its current status is {humanize_status(order.status).lower()}."
        return self._response(intent_obj, speech)


class HelloFreshMealSelectionIntentHandler(HelloFreshBaseIntentHandler):
    """Report meal-selection status."""

    intent_type = INTENT_GET_MEAL_SELECTION

    async def async_handle(self, intent_obj):
        """Handle the intent."""
        coordinators = async_loaded_coordinators(intent_obj.hass)
        if not coordinators:
            return self._response(intent_obj, "HelloFresh is not configured yet.")

        pending: list[str] = []
        for coordinator in coordinators:
            for week in coordinator.data.weeks_needing_selection:
                deadline = (
                    f"deadline {_spoken_deadline(week.selection_deadline)}"
                    if week.selection_deadline is not None
                    else "no deadline"
                )
                pending.append(f"{coordinator.config_entry.title}: {week.display_name}, {deadline}")

        if not pending:
            return self._response(
                intent_obj,
                "All configured HelloFresh accounts are up to date on meal selection.",
            )

        return self._response(
            intent_obj,
            "These HelloFresh weeks still need meal selection: " + "; ".join(pending),
        )


class HelloFreshRefreshIntentHandler(HelloFreshBaseIntentHandler):
    """Refresh HelloFresh data on demand."""

    intent_type = INTENT_REFRESH

    async def async_handle(self, intent_obj):
        """Handle the intent."""
        coordinators = async_loaded_coordinators(intent_obj.hass)
        if not coordinators:
            return self._response(intent_obj, "HelloFresh is not configured yet.")

        for coordinator in coordinators:
            await coordinator.async_request_refresh()

        # A refresh that fails does not raise: the coordinator logs it and keeps the previous
        # data. Say so, and never give the time of this request as the time of the data.
        failed = [
            coordinator.config_entry.title
            for coordinator in coordinators
            if not coordinator.last_update_success
        ]
        if failed:
            return self._response(
                intent_obj,
                "I couldn't refresh HelloFresh data for "
                + " and ".join(failed)
                + ". The earlier data is still in use.",
            )

        # The time the data was actually read. Home Assistant spaces refresh requests a few
        # seconds apart, so this can be slightly before now.
        refreshed = [
            coordinator.last_full_refresh
            for coordinator in coordinators
            if coordinator.last_full_refresh is not None
        ]
        if not refreshed:
            return self._response(intent_obj, "HelloFresh data is up to date.")
        return self._response(
            intent_obj,
            f"HelloFresh data was refreshed at {_spoken_time(min(refreshed))}.",
        )
