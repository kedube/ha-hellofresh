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
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntryState
from homeassistant.util import dt as dt_util
import pytest

from custom_components.hellofresh.api import (
    HelloFreshAccountData,
    HelloFreshOrder,
    HelloFreshWeek,
)
from custom_components.hellofresh.const import (
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
from custom_components.hellofresh.parsers import local_today


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


@pytest.fixture(autouse=True)
def _eastern_time():
    """Home Assistant set to New York: far enough from UTC that a wrong zone shows."""
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(ZoneInfo("America/New_York"))
    yield
    dt_util.set_default_time_zone(previous)


def _speak(handler, *coordinators, states: dict[int, ConfigEntryState] | None = None) -> str:
    """Run a handler against the given coordinators and return the spoken text."""
    # Coordinators are read from each loaded entry's runtime_data (not hass.data).
    entries = [
        SimpleNamespace(
            entry_id=f"entry-{i}",
            state=(states or {}).get(i, ConfigEntryState.LOADED),
            runtime_data=c,
        )
        for i, c in enumerate(coordinators)
    ]
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda _domain: entries))
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
        last_update_success=True,
        last_full_refresh=None,
    )


def _spoken_date(day: date) -> str:
    """The wording the handlers use for a day: "Tuesday, November 17"."""
    return f"{day:%A}, {day:%B} {day.day}"


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
        delivery_date=local_today() + timedelta(days=4),
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
    delivery = local_today() + timedelta(days=3)
    speech = _speak(
        HelloFreshNextDeliveryIntentHandler(),
        _coordinator("HelloFresh US", orders=[_order(delivery)]),
    )
    assert _spoken_date(delivery) in speech
    assert delivery.isoformat() not in speech, "a spoken ISO date is noise"
    assert "HelloFresh US" in speech
    assert "Its current status is scheduled." in speech


def test_next_delivery_says_the_status_in_words() -> None:
    """A status is spoken the way the status sensors show it, not as the API's constant."""
    speech = _speak(
        HelloFreshNextDeliveryIntentHandler(),
        _coordinator(
            "HelloFresh US",
            orders=[_order(local_today() + timedelta(days=1), status="ON_THE_WAY")],
        ),
    )
    assert "Its current status is on the way." in speech
    assert "ON_THE_WAY" not in speech


def test_an_account_that_is_not_loaded_is_not_spoken_for() -> None:
    """Only loaded entries count: one whose setup failed has no data worth reading."""
    broken = _coordinator("Broken", orders=[_order(local_today() + timedelta(days=1))])
    speech = _speak(
        HelloFreshNextDeliveryIntentHandler(),
        broken,
        states={0: ConfigEntryState.SETUP_ERROR},
    )
    assert speech == "HelloFresh is not configured yet."


def test_next_delivery_omits_the_status_sentence_when_absent() -> None:
    """A status-less order must not speak a dangling "status is None"."""
    delivery = local_today() + timedelta(days=3)
    speech = _speak(
        HelloFreshNextDeliveryIntentHandler(),
        _coordinator("HelloFresh US", orders=[_order(delivery, status=None)]),
    )
    assert "status" not in speech
    assert speech.endswith(f"{_spoken_date(delivery)}.")


def test_next_delivery_picks_the_soonest_across_accounts() -> None:
    """With two accounts configured, the earliest delivery wins -- not the first entry.

    Handler order follows ``hass.data`` insertion, so a naive implementation would
    announce whichever account was set up first.
    """
    soon = local_today() + timedelta(days=2)
    later = local_today() + timedelta(days=9)
    speech = _speak(
        HelloFreshNextDeliveryIntentHandler(),
        _coordinator("Later Account", orders=[_order(later)]),
        _coordinator("Sooner Account", orders=[_order(soon)]),
    )
    assert "Sooner Account" in speech
    assert _spoken_date(soon) in speech
    assert _spoken_date(later) not in speech


def test_next_delivery_handles_accounts_with_no_upcoming_box() -> None:
    """Past-only orders are not upcoming, so there is nothing to announce."""
    speech = _speak(
        HelloFreshNextDeliveryIntentHandler(),
        _coordinator("HelloFresh US", orders=[_order(local_today() - timedelta(days=5))]),
    )
    assert speech == "I couldn't find an upcoming HelloFresh delivery right now."


# --- meal selection ----------------------------------------------------------------


def test_meal_selection_reports_no_configuration_when_no_accounts_exist() -> None:
    speech = _speak(HelloFreshMealSelectionIntentHandler())
    assert speech == "HelloFresh is not configured yet."


def test_meal_selection_reports_all_clear_when_nothing_is_pending() -> None:
    speech = _speak(HelloFreshMealSelectionIntentHandler(), _coordinator("HelloFresh US"))
    assert speech == "All configured HelloFresh accounts are up to date on meal selection."


def test_meal_selection_lists_each_pending_week_with_its_account() -> None:
    # Relative so the week stays editable (and therefore pending) as the calendar advances.
    # Without an offset, as some payloads give it: that means UTC.
    deadline = datetime.combine(local_today() + timedelta(days=2), time(6, 59, 59))
    speech = _speak(
        HelloFreshMealSelectionIntentHandler(),
        _coordinator(
            "HelloFresh US",
            weeks=[_week_needing_selection("2026-W39", deadline=deadline)],
        ),
    )
    assert "HelloFresh US" in speech
    assert "Week 2026-W39" in speech
    local = deadline.replace(tzinfo=UTC).astimezone(ZoneInfo("America/New_York"))
    assert f"deadline {_spoken_date(local.date())} at {local.hour}:59 AM" in speech
    assert deadline.isoformat() not in speech


def test_meal_selection_speaks_the_deadline_in_the_homes_time_zone() -> None:
    """A late-evening cutoff is said on its own day, not on the UTC day after it.

    Thursday 23:59 in New York is Friday 04:59 UTC (03:59 in summer). Spoken as the stored
    UTC timestamp, the deadline was a day late.
    """
    eastern = ZoneInfo("America/New_York")
    cutoff_day = local_today() + timedelta(days=3)
    local_deadline = datetime.combine(cutoff_day, time(23, 59, 59), tzinfo=eastern)
    deadline = local_deadline.astimezone(UTC)
    assert deadline.date() != cutoff_day, "the test needs a cutoff that crosses midnight UTC"

    speech = _speak(
        HelloFreshMealSelectionIntentHandler(),
        _coordinator(
            "HelloFresh US",
            weeks=[_week_needing_selection("2026-W39", deadline=deadline)],
        ),
    )

    assert f"deadline {_spoken_date(cutoff_day)} at 11:59 PM" in speech
    assert _spoken_date(deadline.date()) not in speech
    assert "T0" not in speech and "+00:00" not in speech


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
        _coordinator("Account A", weeks=[_week_needing_selection("2026-W39", deadline=None)]),
        _coordinator("Account B", weeks=[_week_needing_selection("2026-W40", deadline=None)]),
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
    assert speech == "HelloFresh data is up to date."


def _refreshable(title: str, *, succeeds: bool, read_at: datetime | None) -> SimpleNamespace:
    """A coordinator whose refresh request ends the way a real one does: it never raises."""
    coordinator = _coordinator(title)

    async def _request_refresh() -> None:
        coordinator.last_update_success = succeeds
        if succeeds:
            coordinator.last_full_refresh = read_at

    coordinator.async_request_refresh = _request_refresh
    return coordinator


def test_refresh_reports_when_the_data_was_read_in_local_time() -> None:
    """The time spoken is when the data was read, in Home Assistant's time zone."""
    read_at = datetime(2026, 11, 14, 20, 7, tzinfo=UTC)  # 3:07 PM in New York
    speech = _speak(
        HelloFreshRefreshIntentHandler(),
        _refreshable("HelloFresh US", succeeds=True, read_at=read_at),
    )
    assert speech == "HelloFresh data was refreshed at 3:07 PM."


def test_refresh_does_not_claim_success_when_the_refresh_failed() -> None:
    """A failed refresh keeps the old data; saying "refreshed at <now>" would be untrue.

    The coordinator swallows the failure (it logs it and keeps the previous data), so the
    handler has to ask whether the update succeeded.
    """
    speech = _speak(
        HelloFreshRefreshIntentHandler(),
        _refreshable("Account A", succeeds=True, read_at=datetime.now(UTC)),
        _refreshable("Account B", succeeds=False, read_at=None),
    )
    assert "refreshed at" not in speech
    assert speech == (
        "I couldn't refresh HelloFresh data for Account B. The earlier data is still in use."
    )
