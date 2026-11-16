"""The delivery-day watch follows the day a delayed or early box now comes.

A DELAYED or EARLY box keeps its scheduled ``delivery_date``; the day it now comes is
``revised_delivery_date``. Watching only the scheduled day left the watch off on the real one,
so box_shipped and box_delivered landed up to a scan interval late.
"""

from __future__ import annotations

from datetime import timedelta

from custom_components.hellofresh.api import (
    HelloFreshAccountData,
    HelloFreshSubscription,
    HelloFreshWeek,
)
from custom_components.hellofresh.coordinator import delivery_in_progress
from custom_components.hellofresh.parsers import local_today

TODAY = local_today()


def _data(week: HelloFreshWeek) -> HelloFreshAccountData:
    return HelloFreshAccountData(
        account_data_available=True,
        weeks=[week],
        subscriptions=[HelloFreshSubscription(subscription_id="sub-1")],
    ).finalize()


def _week(**overrides) -> HelloFreshWeek:
    defaults = {
        "week_id": "2026-W47",
        "display_name": "Week 47",
        "subscription_id": "sub-1",
        "status": "RUNNING",
    }
    return HelloFreshWeek(**{**defaults, **overrides})


def test_a_delayed_box_is_watched_on_the_day_it_now_comes() -> None:
    delayed = _week(
        delivery_date=TODAY - timedelta(days=2),
        sub_status="DELAYED",
        revised_delivery_date=TODAY,
    )
    assert delivery_in_progress(_data(delayed), TODAY) is True


def test_an_early_box_is_watched_on_the_day_it_now_comes() -> None:
    early = _week(
        delivery_date=TODAY + timedelta(days=2),
        sub_status="EARLY",
        revised_delivery_date=TODAY,
    )
    assert delivery_in_progress(_data(early), TODAY) is True


def test_an_on_time_box_is_watched_on_its_day() -> None:
    assert delivery_in_progress(_data(_week(delivery_date=TODAY)), TODAY) is True
