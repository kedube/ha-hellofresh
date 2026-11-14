"""Boxes HelloFresh could not charge: the ledger scan, the weeks' flag, the sensor and the notice.

HelloFresh marks a week ``status: PAYMENT_FAILED``, and order lines in the orders ledger carry a
``paymentStatus`` of ``failed`` or ``chargeback``. Both are collected, for boxes still to come
and those delivered in the last 14 days, into ``payment_problem_weeks``: one entry per box,
``{week_id, delivery_date, status, amount, currency}``. Every account detail here is made up.
"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.helpers import issue_registry as ir
import pytest

from custom_components.hellofresh.binary_sensor import SENSORS as BINARY_SENSORS
from custom_components.hellofresh.binary_sensor import HelloFreshBinarySensor
from custom_components.hellofresh.const import DOMAIN
from custom_components.hellofresh.issues import (
    async_cleanup_stale_issues,
    async_delete_payment_problem_issue,
    async_update_payment_problem_issue,
)
from custom_components.hellofresh.models import (
    PAYMENT_PROBLEM_LOOKBACK_DAYS,
    HelloFreshAccountData,
    HelloFreshOrder,
    HelloFreshWeek,
)
from custom_components.hellofresh.parsers import iso_week_label, local_today
from custom_components.hellofresh.services import _get_account_summary
from test_client_fixes import SUB, Account, FakeHelloFresh, make_client

TODAY = local_today()


def _order(day: date, total: float, *statuses: str, subscription: str = SUB) -> dict:
    """An orders-ledger charge for the box due on ``day``, its lines paid as ``statuses``."""
    return {
        "orderNr": "30000000001",
        "createdAt": f"{(day - timedelta(days=5)).isoformat()}T00:13:06-0700",
        "grandTotal": total,
        "orderLines": [
            {
                "deliveryDate": f"{day.isoformat()}T00:00:00-0700",
                "subscription": {"id": subscription},
                "paymentStatus": status,
            }
            for status in statuses
        ],
    }


# ---- the ledger scan -------------------------------------------------------------------------


def test_the_ledger_scan_collects_failed_and_charged_back_boxes() -> None:
    client = make_client(FakeHelloFresh())
    first, second, third = (TODAY + timedelta(days=n) for n in (3, 10, 17))
    items = [
        _order(first, 65.94, "paid"),
        _order(first, 70.0, "failed"),
        _order(first, 12.0, "failed"),  # the box's Market order failed too: added up
        _order(second, 60.0, "paid", "chargeback"),  # one line taken back is enough
        _order(third, 50.0, "failed"),
        _order(third, 5.0, "chargeback"),  # the more serious status wins
        _order(TODAY, 40.0, "canceled"),  # nothing was owed: not a problem
        {"grandTotal": 1.0, "orderLines": [{"paymentStatus": "failed"}]},  # names no box
        "not an order",
    ]

    failures = client._failed_charges(items)

    assert failures == [
        {
            "subscription_id": SUB,
            "delivery_date": first,
            "status": "failed",
            "amount": 82.0,
            "currency": "USD",
        },
        {
            "subscription_id": SUB,
            "delivery_date": second,
            "status": "chargeback",
            "amount": 60.0,
            "currency": "USD",
        },
        {
            "subscription_id": SUB,
            "delivery_date": third,
            "status": "chargeback",
            "amount": 55.0,
            "currency": "USD",
        },
    ]


def test_a_failed_charge_still_counts_for_nothing_in_the_billing_totals() -> None:
    """The billing scan keeps skipping a failed charge; only the problem scan reads it."""
    client = make_client(FakeHelloFresh())
    day = TODAY + timedelta(days=3)
    _latest, _future, _numbers, prices, _discounts = client._accumulate_order_prices(
        [_order(day, 70.0, "failed")]
    )
    assert prices == {}
    assert client._failed_charges([_order(day, 70.0, "failed")])[0]["amount"] == 70.0


# ---- weeks and charges, paired up by finalize() ----------------------------------------------


def _week(week_id: str, day: date | None, **fields) -> HelloFreshWeek:
    return HelloFreshWeek(
        **{
            "week_id": week_id,
            "display_name": week_id,
            "subscription_id": SUB,
            "delivery_date": day,
            **fields,
        }
    )


def _failure(day: date, status: str = "failed", amount: float | None = 70.0, **fields) -> dict:
    return {
        "subscription_id": SUB,
        "delivery_date": day,
        "status": status,
        "amount": amount,
        "currency": "USD" if amount is not None else None,
        **fields,
    }


def test_a_week_hellofresh_flags_is_a_problem_priced_at_its_box() -> None:
    day = TODAY + timedelta(days=5)
    data = HelloFreshAccountData(
        weeks=[_week("2026-W47", day, payment_failed=True)],
        orders=[
            HelloFreshOrder(
                order_id="o1",
                week_id="2026-W47",
                status="PAYMENT_FAILED",
                subscription_id=SUB,
                delivery_date=day,
                total_price=65.94,
                currency="USD",
            )
        ],
    ).finalize()

    assert data.payment_problem is True
    assert data.payment_problem_weeks == [
        {
            "week_id": "2026-W47",
            "delivery_date": day.isoformat(),
            "status": "payment_failed",
            "amount": 65.94,
            "currency": "USD",
        }
    ]


def test_a_flagged_week_without_a_price_has_no_amount() -> None:
    data = HelloFreshAccountData(
        weeks=[_week("2026-W47", TODAY + timedelta(days=5), payment_failed=True)]
    ).finalize()
    [problem] = data.payment_problem_weeks
    assert problem["amount"] is None
    assert problem["currency"] is None


def test_the_ledgers_charge_names_the_amount_and_the_more_serious_status() -> None:
    day = TODAY + timedelta(days=5)
    data = HelloFreshAccountData(
        weeks=[_week("2026-W47", day, payment_failed=True)],
        orders=[
            HelloFreshOrder(
                order_id="o1",
                week_id="2026-W47",
                status="RUNNING",
                subscription_id=SUB,
                delivery_date=day,
                total_price=65.94,
                currency="USD",
            )
        ],
        payment_failures=[_failure(day, "chargeback", 70.0)],
    ).finalize()

    assert data.payment_problem_weeks == [
        {
            "week_id": "2026-W47",
            "delivery_date": day.isoformat(),
            "status": "chargeback",
            "amount": 70.0,
            "currency": "USD",
        }
    ]


def test_a_charge_without_an_amount_keeps_the_boxs_price() -> None:
    day = TODAY + timedelta(days=5)
    data = HelloFreshAccountData(
        weeks=[_week("2026-W47", day, payment_failed=True)],
        orders=[
            HelloFreshOrder(
                order_id="o1",
                week_id="2026-W47",
                status="RUNNING",
                subscription_id=SUB,
                delivery_date=day,
                billed_total_price=61.0,
                billed_total_currency="CAD",
            )
        ],
        payment_failures=[_failure(day, "failed", None)],
    ).finalize()

    [problem] = data.payment_problem_weeks
    assert (problem["status"], problem["amount"], problem["currency"]) == ("failed", 61.0, "CAD")


def test_only_boxes_to_come_and_those_of_the_last_two_weeks_count() -> None:
    recent = TODAY - timedelta(days=PAYMENT_PROBLEM_LOOKBACK_DAYS)
    old = recent - timedelta(days=1)
    upcoming = TODAY + timedelta(days=12)
    data = HelloFreshAccountData(
        weeks=[
            _week("2026-W40", old, payment_failed=True),
            _week("2026-W42", recent),
            _week("2026-W46", upcoming),
            _week("2026-W48", None, payment_failed=True),  # no date yet: still to come
        ],
        payment_failures=[_failure(old), _failure(recent, "chargeback"), _failure(upcoming)],
    ).finalize()

    assert [(p["week_id"], p["status"]) for p in data.payment_problem_weeks] == [
        ("2026-W42", "chargeback"),
        ("2026-W46", "failed"),
        ("2026-W48", "payment_failed"),
    ]
    # Oldest first, and a box without a date last.
    assert data.payment_problem_weeks[-1]["delivery_date"] is None


def test_a_week_skipped_since_its_charge_failed_owes_nothing() -> None:
    upcoming = TODAY + timedelta(days=12)
    data = HelloFreshAccountData(
        weeks=[_week("2026-W46", upcoming, is_skipped=True, status="PAUSED")],
        payment_failures=[_failure(upcoming)],
    ).finalize()
    assert data.payment_problem is False


def test_a_charge_is_paired_with_its_week_by_the_day_its_box_is_due() -> None:
    holiday = TODAY + timedelta(days=6)
    scheduled = TODAY + timedelta(days=5)
    orphan = TODAY + timedelta(days=20)
    data = HelloFreshAccountData(
        weeks=[
            # Moved for a holiday: dated by its new day, but the ledger may keep the old one.
            _week(
                "2026-W47",
                holiday,
                raw={"deliveryDate": f"{scheduled.isoformat()}T12:00:00-0700"},
            ),
            _week("2026-W47", scheduled, subscription_id="other-sub"),
        ],
        payment_failures=[_failure(scheduled), _failure(orphan, subscription_id="other-sub")],
    ).finalize()

    assert data.payment_problem_weeks == [
        {
            "week_id": "2026-W47",
            # The week's own (holiday) day, as the calendar shows it.
            "delivery_date": holiday.isoformat(),
            "status": "failed",
            "amount": 70.0,
            "currency": "USD",
        },
        # A charge for a box outside the weeks read is named by the ISO week of its day.
        {
            "week_id": iso_week_label(None, orphan),
            "delivery_date": orphan.isoformat(),
            "status": "failed",
            "amount": 70.0,
            "currency": "USD",
        },
    ]


def test_no_problem_is_no_problem() -> None:
    data = HelloFreshAccountData(weeks=[_week("2026-W47", TODAY)]).finalize()
    assert data.payment_problem is False
    assert data.payment_problem_weeks == []


# ---- through the client ----------------------------------------------------------------------


async def test_a_poll_reports_the_failed_charge_of_the_upcoming_box() -> None:
    server = FakeHelloFresh()
    account = Account(server)
    server.route(
        "GET",
        "/gw/api/customers/me/orders",
        (200, {"count": 1, "items": [_order(account.delivery, 65.94, "failed")]}),
    )
    client = make_client(server)

    data = await client.async_get_account_data()

    assert data.payment_problem_weeks == [
        {
            "week_id": account.week_id,
            "delivery_date": account.delivery.isoformat(),
            "status": "failed",
            "amount": 65.94,
            "currency": "USD",
        }
    ]

    # A write's snapshot keeps the problem: it is about the box, not about what was written.
    await client.async_select_meals(account.week_id, ["recipe-11", "recipe-18", "recipe-20"])
    assert client.last_account_data is not data
    assert client.last_account_data.payment_problem is True
    # Skipped, the box is not coming, and nothing is owed for it.
    await client.async_skip_week(account.week_id)
    assert client.last_account_data.payment_problem is False


def test_a_weeks_payment_failed_status_is_kept_apart_from_its_progress() -> None:
    client = make_client(FakeHelloFresh())
    week = client._week_from_raw_delivery(
        0,
        {"id": "2026-W47", "subscriptionId": SUB, "status": "PAYMENT_FAILED", "state": "RUNNING"},
        SimpleNamespace(subscription_id=SUB, meals_required=3, box_size=None, shipping_method=None),
    )
    assert week.payment_failed is True
    assert week.status == "RUNNING"  # the box's progress, which the state gives


# ---- the binary sensor and the account summary -----------------------------------------------


def _coordinator(data: HelloFreshAccountData) -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        config_entry=SimpleNamespace(entry_id="entry-1", title="HelloFresh", options={}),
        client=SimpleNamespace(base_url="https://www.hellofresh.com", menu_grace_weeks=2),
        last_update_success=True,
        delivery_in_progress=lambda: False,
    )


def _problem_data() -> HelloFreshAccountData:
    day = TODAY + timedelta(days=5)
    return HelloFreshAccountData(
        weeks=[_week("2026-W47", day)], payment_failures=[_failure(day)]
    ).finalize()


def _sensor(data: HelloFreshAccountData) -> HelloFreshBinarySensor:
    description = next(d for d in BINARY_SENSORS if d.key == "payment_problem")
    return HelloFreshBinarySensor(_coordinator(data), description)


def test_the_payment_problem_sensor_lists_the_boxes_but_does_not_record_them() -> None:
    sensor = _sensor(_problem_data())

    assert sensor.entity_id == "binary_sensor.hellofresh_payment_problem"
    assert sensor.device_class is BinarySensorDeviceClass.PROBLEM
    assert sensor.entity_category is None  # a dashboard and voice concern, not a diagnostic
    assert sensor.is_on is True
    assert sensor.icon == "mdi:credit-card-remove-outline"
    assert sensor.extra_state_attributes == {
        "weeks": [
            {
                "week_id": "2026-W47",
                "delivery_date": (TODAY + timedelta(days=5)).isoformat(),
                "status": "failed",
                "amount": 70.0,
                "currency": "USD",
            }
        ]
    }
    assert "weeks" in type(sensor)._unrecorded_attributes

    quiet = _sensor(HelloFreshAccountData().finalize())
    assert quiet.is_on is False
    assert quiet.icon == "mdi:credit-card-check-outline"
    assert quiet.extra_state_attributes == {"weeks": []}


async def test_the_account_summary_reports_the_payment_problems() -> None:
    data = _problem_data()

    summary = await _get_account_summary(_coordinator(data), SimpleNamespace(data={}))

    assert summary["payment_problem"] is True
    assert summary["payment_problem_weeks"] == data.payment_problem_weeks
    # A copy: a caller changing the response does not change the account data.
    summary["payment_problem_weeks"][0]["amount"] = 0
    assert data.payment_problem_weeks[0]["amount"] == 70.0

    calm = await _get_account_summary(
        _coordinator(HelloFreshAccountData().finalize()), SimpleNamespace(data={})
    )
    assert calm["payment_problem"] is False
    assert calm["payment_problem_weeks"] == []


# ---- the Repairs notice ----------------------------------------------------------------------


def _issue(hass, entry_id: str = "entry1"):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"payment_problem_{entry_id}")


async def test_the_notice_names_the_first_box_and_links_to_the_site(hass) -> None:
    problems = [
        {
            "week_id": "2026-W46",
            "delivery_date": "2026-11-16",
            "status": "chargeback",
            "amount": 65.9,
            "currency": "USD",
        },
        {
            "week_id": "2026-W47",
            "delivery_date": "2026-11-23",
            "status": "failed",
            "amount": 70.0,
            "currency": "USD",
        },
    ]

    async_update_payment_problem_issue(
        hass, "entry1", "HelloFresh (US)", problems, "https://www.hellofresh.com"
    )

    issue = _issue(hass)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.is_fixable is False
    assert issue.is_persistent is False
    assert issue.translation_key == "payment_problem"
    assert issue.translation_placeholders == {
        "entry_title": "HelloFresh (US)",
        "date": "2026-11-16",
        "amount": "65.90 USD",
    }
    assert issue.learn_more_url == "https://www.hellofresh.com"

    # The problem cleared: the notice goes.
    async_update_payment_problem_issue(hass, "entry1", "HelloFresh (US)", [], "https://x")
    assert _issue(hass) is None


@pytest.mark.parametrize(
    ("problem", "date_text", "amount_text"),
    [
        ({"week_id": "2026-W48", "delivery_date": None, "amount": None}, "2026-W48", "–"),
        (
            {"week_id": "2026-W48", "delivery_date": "2026-11-30", "amount": 12},
            "2026-11-30",
            "12.00",
        ),
    ],
)
async def test_the_notice_says_what_it_knows(hass, problem, date_text, amount_text) -> None:
    async_update_payment_problem_issue(hass, "entry1", "HelloFresh", [problem], "https://x")
    placeholders = _issue(hass).translation_placeholders
    assert (placeholders["date"], placeholders["amount"]) == (date_text, amount_text)


async def test_the_notice_is_deleted_and_an_orphan_is_cleaned_up(hass) -> None:
    problem = {"week_id": "2026-W47", "delivery_date": "2026-11-23", "amount": 1.0}
    async_update_payment_problem_issue(hass, "entry1", "HelloFresh", [problem], "https://x")
    async_delete_payment_problem_issue(hass, "entry1")
    assert _issue(hass) is None

    # An entry that no longer exists leaves no notice behind.
    async_update_payment_problem_issue(hass, "gone", "HelloFresh", [problem], "https://x")
    async_update_payment_problem_issue(hass, "kept", "HelloFresh", [problem], "https://x")
    async_cleanup_stale_issues(hass, {"kept"})
    assert _issue(hass, "gone") is None
    assert _issue(hass, "kept") is not None
