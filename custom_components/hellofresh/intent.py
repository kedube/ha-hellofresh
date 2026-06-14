"""Intent handlers for HelloFresh."""

from __future__ import annotations

from datetime import datetime

from homeassistant.helpers import intent

from .const import (
    DOMAIN,
    INTENT_GET_MEAL_SELECTION,
    INTENT_GET_NEXT_DELIVERY,
    INTENT_REFRESH,
)


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


def _coordinators(hass):
    """Return all configured HelloFresh coordinators."""
    return list(hass.data.get(DOMAIN, {}).values())


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
        coordinators = _coordinators(intent_obj.hass)
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
            key=lambda item: item[1].delivery_date,
        )
        delivery_date = order.delivery_date.isoformat() if order.delivery_date else "an unknown date"
        speech = f"The next HelloFresh delivery for {title} is scheduled for {delivery_date}."
        if order.status:
            speech = f"{speech} Its current status is {order.status}."
        return self._response(intent_obj, speech)


class HelloFreshMealSelectionIntentHandler(HelloFreshBaseIntentHandler):
    """Report meal-selection status."""

    intent_type = INTENT_GET_MEAL_SELECTION

    async def async_handle(self, intent_obj):
        """Handle the intent."""
        coordinators = _coordinators(intent_obj.hass)
        if not coordinators:
            return self._response(intent_obj, "HelloFresh is not configured yet.")

        pending: list[str] = []
        for coordinator in coordinators:
            for week in coordinator.data.weeks_needing_selection:
                deadline = (
                    week.selection_deadline.isoformat()
                    if week.selection_deadline is not None
                    else "no deadline"
                )
                pending.append(
                    f"{coordinator.config_entry.title}: {week.display_name}, deadline {deadline}"
                )

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
        coordinators = _coordinators(intent_obj.hass)
        if not coordinators:
            return self._response(intent_obj, "HelloFresh is not configured yet.")

        for coordinator in coordinators:
            await coordinator.async_request_refresh()

        timestamp = datetime.now().strftime("%H:%M")
        return self._response(
            intent_obj,
            f"HelloFresh data was refreshed at {timestamp}.",
        )
