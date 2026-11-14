"""What the coordinator does with a poll that came back without account data.

The client answers a failed subscriptions request (a timeout, a 5xx, a rate limit) with an
account-less result instead of raising, so that a menu-only setup keeps working. The
coordinator has to tell that apart from a real answer: taken at face value it replaced good
data with an empty account for a whole scan interval, counted as a successful poll, and made
every week look newly published once the account came back.

``tests/test_ha_lifecycle.py`` drives the same rule through a real Home Assistant.
"""

from __future__ import annotations

from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest

from custom_components.hellofresh.api import (
    HelloFreshAccountData,
    HelloFreshRecipe,
    HelloFreshSubscription,
    HelloFreshWeek,
)
from custom_components.hellofresh.coordinator import (
    EVENT_MENU_PUBLISHED,
    HelloFreshDataUpdateCoordinator,
    delivery_snapshot,
)

check = HelloFreshDataUpdateCoordinator._require_usable_data


def _week(week_id: str = "2026-W47") -> HelloFreshWeek:
    return HelloFreshWeek(
        week_id=week_id,
        display_name=week_id,
        subscription_id="sub-1",
        recipes=[HelloFreshRecipe(recipe_id=f"{week_id}-r1", name="Soup")],
    )


def _account(*week_ids: str) -> HelloFreshAccountData:
    return HelloFreshAccountData(
        account_data_available=True,
        weeks=[_week(week_id) for week_id in week_ids],
        subscriptions=[HelloFreshSubscription(subscription_id="sub-1")],
    ).finalize()


def _menu_only() -> HelloFreshAccountData:
    return HelloFreshAccountData(public_menu_weeks=[_week()]).finalize()


def _empty() -> HelloFreshAccountData:
    return HelloFreshAccountData().finalize()


def test_account_data_is_always_accepted() -> None:
    check(None, _account("2026-W47"))
    check(_account("2026-W47"), _account("2026-W47", "2026-W48"))
    check(_menu_only(), _account("2026-W47"))
    # An account with no weeks yet (a brand-new subscription) is still an account.
    check(None, _account())


@pytest.mark.parametrize("result", [_empty, _menu_only], ids=["empty", "menu only"])
def test_losing_the_account_fails_the_poll_and_keeps_the_previous_data(result) -> None:
    """Once the account has been read, a poll without it is a failed poll.

    Even one that still carries the public menu: swapping a real account for the fallback
    would blank the deliveries, the orders and the subscription just the same.
    """
    with pytest.raises(UpdateFailed, match="keeping the data from the last good update"):
        check(_account("2026-W47"), result())


def test_a_first_poll_with_nothing_at_all_fails_so_setup_is_retried() -> None:
    with pytest.raises(UpdateFailed, match="no account data and no menu"):
        check(None, _empty())


def test_the_public_menu_fallback_is_a_usable_result() -> None:
    """A first poll without an account is accepted when it actually carries a menu."""
    check(None, _menu_only())
    check(_menu_only(), _menu_only())
    with pytest.raises(UpdateFailed):
        check(_menu_only(), _empty())


def _bare_coordinator() -> HelloFreshDataUpdateCoordinator:
    coordinator = HelloFreshDataUpdateCoordinator.__new__(HelloFreshDataUpdateCoordinator)
    coordinator.delivery_events = []
    coordinator._event_serial = 0
    return coordinator


def test_no_events_are_recorded_without_earlier_weeks_to_compare_against() -> None:
    """With nothing before, every week would look newly published.

    That holds for the very first poll, and for the first poll that brings account weeks
    after a start on the public menu alone.
    """
    coordinator = _bare_coordinator()
    account = _account("2026-W47", "2026-W48")

    coordinator._record_transitions(None, account)
    coordinator._record_transitions({}, account)

    assert coordinator.delivery_events == []


def test_a_week_that_really_is_new_is_still_announced() -> None:
    coordinator = _bare_coordinator()
    before = delivery_snapshot(_account("2026-W47"))

    coordinator._record_transitions(before, _account("2026-W47", "2026-W48"))

    assert [(kind, attrs["week_id"]) for _, kind, attrs in coordinator.delivery_events] == [
        (EVENT_MENU_PUBLISHED, "2026-W48")
    ]
