"""Unit tests for the HelloFresh conversation intents.

``intent.py`` backs the voice/conversation surface and sat at 34% coverage: only the
module-level registration was ever touched, none of the three handlers' actual speech.
That is the code a user hears, and it reads through several optional fields
(``delivery_date``, ``status``, ``selection_deadline``) that would raise or read badly
if any of them were absent.

Each handler has a distinct "not configured" path, a happy path, and -- for the
multi-account case -- an ordering rule, all pinned here.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from custom_components.hellofresh.api import (
    HelloFreshAccountData,
    HelloFreshOrder,
    HelloFreshWeek,
)
from custom_components.hellofresh.const import (
    DOMAIN,
    INTENT_GET_MEAL_SELECTION,
    INTENT_GET_NEXT_DELIVERY,
    INTENT_REFRESH,
)
from custom_components.hellofresh.intent import (
    HelloFreshMealSelectionIntentHandler,
    HelloFreshNextDeliveryIntentHandler,
    HelloFreshRefreshIntentHandler,
    async_setup_intents,
)


class _Response:
    """Stands in for HA's intent response object, capturing the speech."""

    def __init__(self) -> None:
        self.speech: str | None = None

    def async_set_speech(self, speech: str) -> None:
        self.speech = speech


class _Intent:
    def __init__(self, hass) -> None:
        self.hass = hass
        self.response = _Response()

    def create_response(self) -> _Response:
        return self.response


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def _speak(handler, *coordinators) -> str:
    """Run a handler against the given coordinators and return the spoken text."""
    hass = SimpleNamespace(
        data={DOMAIN: {f"entry-{i}": c for i, c in enumerate(coordinators)}}
    )
    intent_obj = _Intent(hass)
    _run(handler.async_handle(intent_obj))
    return intent_obj.response.speech


def _coordinator(
    title: str,
    *,
    orders: list[HelloFreshOrder] | None = None,
    weeks: list[HelloFreshWeek] | None = None,
) -> SimpleNamespace:
    data = HelloFreshAccountData(
        weeks=weeks or [],
        orders=orders or [],
    ).finalize()
    return SimpleNamespace(
        data=data,
        config_entry=SimpleNamespace(entry_id=f"entry-{title}", title=title),
    )


def _order(delivery: date, *, status: str | None = "scheduled") -> HelloFreshOrder:
    return HelloFreshOrder(
        order_id=f"ord-{delivery}",
        week_id="2026-W25",
        subscription_id="sub-1",
        status=status,
        delivery_date=delivery,
    )


def _week_needing_selection(week_id: str, *, deadline: datetime | None) -> HelloFreshWeek:
    """A week the account still has to pick meals for."""
    return HelloFreshWeek(
        week_id=week_id,
        display_name=f"Week {week_id}",
        subscription_id="sub-1",
        delivery_date=date.today() + timedelta(days=4),
        selection_deadline=deadline,
        meals_required=3,
        meals_selected=0,
        allowed_actions={"mealSwap": True},
    )


# --- registration hook -------------------------------------------------------------


def test_setup_intents_is_a_deliberate_no_op() -> None:
    """HA's intent platform loader requires the symbol to exist.

    Registration happens in ``async_setup`` behind a guard; re-registering here would
    log "is being overwritten" warnings. The hook must therefore exist and do nothing.
    """
    assert _run(async_setup_intents(SimpleNamespace())) is None


def test_handlers_declare_the_documented_intent_types() -> None:
    assert HelloFreshNextDeliveryIntentHandler.intent_type == INTENT_GET_NEXT_DELIVERY
    assert HelloFreshMealSelectionIntentHandler.intent_type == INTENT_GET_MEAL_SELECTION
    assert HelloFreshRefreshIntentHandler.intent_type == INTENT_REFRESH


# --- next delivery -----------------------------------------------------------------


def test_next_delivery_reports_no_configuration_when_no_accounts_exist() -> None:
    speech = _speak(HelloFreshNextDeliveryIntentHandler())
    assert speech == "HelloFresh is not configured yet."


def test_next_delivery_speaks_the_date_and_status() -> None:
    delivery = date.today() + timedelta(days=3)
    speech = _speak(
        HelloFreshNextDeliveryIntentHandler(),
        _coordinator("HelloFresh US", orders=[_order(delivery)]),
    )
    assert delivery.isoformat() in speech
    assert "HelloFresh US" in speech
    assert "Its current status is scheduled." in speech


def test_next_delivery_omits_the_status_sentence_when_absent() -> None:
    """A status-less order must not speak a dangling "status is None"."""
    delivery = date.today() + timedelta(days=3)
    speech = _speak(
        HelloFreshNextDeliveryIntentHandler(),
        _coordinator("HelloFresh US", orders=[_order(delivery, status=None)]),
    )
    assert "status" not in speech
    assert speech.endswith(f"{delivery.isoformat()}.")


def test_next_delivery_picks_the_soonest_across_accounts() -> None:
    """With two accounts configured, the earliest delivery wins -- not the first entry.

    Handler order follows ``hass.data`` insertion, so a naive implementation would
    announce whichever account was set up first.
    """
    soon = date.today() + timedelta(days=2)
    later = date.today() + timedelta(days=9)
    speech = _speak(
        HelloFreshNextDeliveryIntentHandler(),
        _coordinator("Later Account", orders=[_order(later)]),
        _coordinator("Sooner Account", orders=[_order(soon)]),
    )
    assert "Sooner Account" in speech
    assert soon.isoformat() in speech
    assert later.isoformat() not in speech


def test_next_delivery_handles_accounts_with_no_upcoming_box() -> None:
    """Past-only orders are not upcoming, so there is nothing to announce."""
    speech = _speak(
        HelloFreshNextDeliveryIntentHandler(),
        _coordinator("HelloFresh US", orders=[_order(date.today() - timedelta(days=5))]),
    )
    assert speech == "I couldn't find an upcoming HelloFresh delivery right now."


# --- meal selection ----------------------------------------------------------------


def test_meal_selection_reports_no_configuration_when_no_accounts_exist() -> None:
    speech = _speak(HelloFreshMealSelectionIntentHandler())
    assert speech == "HelloFresh is not configured yet."


def test_meal_selection_reports_all_clear_when_nothing_is_pending() -> None:
    speech = _speak(
        HelloFreshMealSelectionIntentHandler(), _coordinator("HelloFresh US")
    )
    assert speech == "All configured HelloFresh accounts are up to date on meal selection."


def test_meal_selection_lists_each_pending_week_with_its_account() -> None:
    deadline = datetime(2026, 9, 17, 6, 59, 59)
    speech = _speak(
        HelloFreshMealSelectionIntentHandler(),
        _coordinator(
            "HelloFresh US",
            weeks=[_week_needing_selection("2026-W39", deadline=deadline)],
        ),
    )
    assert "HelloFresh US" in speech
    assert "Week 2026-W39" in speech
    assert deadline.isoformat() in speech


def test_meal_selection_says_no_deadline_rather_than_crashing() -> None:
    """``selection_deadline`` is optional; formatting it unguarded would raise."""
    speech = _speak(
        HelloFreshMealSelectionIntentHandler(),
        _coordinator(
            "HelloFresh US",
            weeks=[_week_needing_selection("2026-W39", deadline=None)],
        ),
    )
    assert "no deadline" in speech


def test_meal_selection_aggregates_across_accounts() -> None:
    """Both accounts' pending weeks appear, joined into one utterance."""
    speech = _speak(
        HelloFreshMealSelectionIntentHandler(),
        _coordinator(
            "Account A", weeks=[_week_needing_selection("2026-W39", deadline=None)]
        ),
        _coordinator(
            "Account B", weeks=[_week_needing_selection("2026-W40", deadline=None)]
        ),
    )
    assert "Account A" in speech
    assert "Account B" in speech
    assert "; " in speech


# --- refresh -----------------------------------------------------------------------


def test_refresh_reports_no_configuration_when_no_accounts_exist() -> None:
    speech = _speak(HelloFreshRefreshIntentHandler())
    assert speech == "HelloFresh is not configured yet."


def test_refresh_fans_out_to_every_configured_account() -> None:
    """Unlike the refresh *button* (one entity, one coordinator), the intent has no
    account context, so it must refresh all of them."""
    refreshed: list[str] = []

    def _make(title: str) -> SimpleNamespace:
        coordinator = _coordinator(title)

        async def _request_refresh() -> None:
            refreshed.append(title)

        coordinator.async_request_refresh = _request_refresh
        return coordinator

    speech = _speak(HelloFreshRefreshIntentHandler(), _make("Account A"), _make("Account B"))
    assert refreshed == ["Account A", "Account B"]
    assert "refreshed at" in speech
