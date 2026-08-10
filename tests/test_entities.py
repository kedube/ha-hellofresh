"""Unit tests for HelloFresh entity mappings."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from custom_components.hellofresh import switch as switch_module
from custom_components.hellofresh.api import (
    HelloFreshAccountData,
    HelloFreshCapabilities,
    HelloFreshMarketItem,
    HelloFreshOrder,
    HelloFreshSubscription,
    HelloFreshWeek,
)
from custom_components.hellofresh.binary_sensor import (
    SENSORS as BINARY_SENSORS,
)
from custom_components.hellofresh.binary_sensor import (
    HelloFreshBinarySensor,
)
from custom_components.hellofresh.sensor import SENSORS as SENSOR_DESCRIPTIONS
from custom_components.hellofresh.sensor import HelloFreshSensor
from custom_components.hellofresh.switch import SWITCHES, HelloFreshSwitch


def _build_coordinator() -> SimpleNamespace:
    """Create a minimal coordinator stand-in for entity unit tests."""
    # Delivery/selection dates are anchored to ``today`` so the upcoming-order selection in
    # ``finalize()`` (which filters orders to ``delivery_date >= today``) stays deterministic
    # regardless of when the suite runs. Payment and holiday dates remain fixed literals --
    # those are asserted by value and aren't gated on today.
    today = date.today()
    next_delivery = today + timedelta(days=2)
    tracked_delivery = today + timedelta(days=5)
    skipped_delivery = today + timedelta(days=9)
    past_delivery = today - timedelta(days=7)
    next_selection_week = HelloFreshWeek(
        week_id="2026-W25",
        display_name="Week 25",
        subscription_id="sub-1",
        delivery_date=next_delivery,
        selection_deadline=datetime(2026, 6, 10, 18, 0),
        meals_required=3,
        meals_selected=1,
        delivery_blocked=True,
        holiday_delivery_date=date(2026, 6, 16),
        holiday_message="Delivery shifted for the holiday",
    )
    skipped_week = HelloFreshWeek(
        week_id="2026-W26",
        display_name="Week 26",
        subscription_id="sub-1",
        delivery_date=skipped_delivery,
        meals_required=3,
        meals_selected=3,
        is_skipped=True,
    )
    next_order = HelloFreshOrder(
        order_id="ord-1",
        week_id="2026-W25",
        status="scheduled",
        subscription_id="sub-1",
        delivery_date=next_delivery,
        total_price=97.5,
        currency="USD",
        slot_label="Mon 8:00 AM - 8:00 PM",
    )
    tracked_order = HelloFreshOrder(
        order_id="ord-2",
        week_id="2026-W24",
        status="in_transit",
        subscription_id="sub-1",
        delivery_date=tracked_delivery,
        tracking_number="TRACK123",
        tracking_status="in_transit",
        carrier="UPS",
        tracking_url="https://track.example.com/TRACK123",
    )
    delivered_week = HelloFreshWeek(
        week_id="2026-W23",
        display_name="Week 23",
        subscription_id="sub-1",
        delivery_date=past_delivery,
        status="delivered",
    )
    data = HelloFreshAccountData(
        weeks=[next_selection_week, skipped_week],
        orders=[tracked_order, next_order],
        past_delivery_weeks=[delivered_week],
        subscriptions=[
            HelloFreshSubscription(
                subscription_id="sub-1",
                display_name="Classic Plan",
                plan_name="Classic Box",
                status="ACTIVE",
                servings=2,
                delivery_address="62 Leonard St, Gloucester, MA, 01930",
                recent_payment_date=date(2026, 6, 4),
                next_payment_date=date(2026, 6, 11),
                coupon_code="WELCOME20",
            )
        ],
        boxes_received=14,
        capabilities=HelloFreshCapabilities(
            supports_meal_selection=True,
            supports_account_menu_api=True,
            supports_one_off_change=True,
            supports_update_delivery_weekday=True,
            payload_shape_changed=True,
        ),
    ).finalize()
    return SimpleNamespace(
        data=data,
        config_entry=SimpleNamespace(entry_id="entry-1", title="HelloFresh"),
        client=SimpleNamespace(
            base_url="https://www.hellofresh.com",
            token_expires_at=datetime(2026, 6, 25, 12, 0, tzinfo=UTC),
            refresh_token_expires_at=datetime(2026, 8, 25, 12, 0, tzinfo=UTC),
        ),
    )


def _sensor_for(key: str) -> HelloFreshSensor:
    """Return a sensor entity for the requested key."""
    description = next(item for item in SENSOR_DESCRIPTIONS if item.key == key)
    return HelloFreshSensor(_build_coordinator(), description)


def test_entity_ids_are_pinned_to_key_not_display_name() -> None:
    """Entity ids derive from the stable key, not the (renamable) display name.

    Regression: renaming a sensor's display name would otherwise change its
    entity_id (has_entity_name derives the id from the name), breaking dashboards
    and the documented <domain>.<title>_<key> ids. Pinning entity_id keeps them stable.
    """
    # Title "HelloFresh" -> slug "hellofresh"; entity_id is "<domain>.<slug>_<key>".
    assert _sensor_for("next_order_status").entity_id == "sensor.hellofresh_next_order_status"
    assert (
        _sensor_for("selected_meal_count").entity_id == "sensor.hellofresh_selected_meal_count"
    )
    # Switch and binary sensor use the same pinning.
    sw_desc = next(d for d in SWITCHES if d.key == "skip_next_modifiable_week")
    switch = HelloFreshSwitch(_build_coordinator(), sw_desc)
    assert switch.entity_id == "switch.hellofresh_skip_next_modifiable_week"


def _binary_sensor_for(key: str) -> HelloFreshBinarySensor:
    """Return a binary sensor entity for the requested key."""
    description = next(item for item in BINARY_SENSORS if item.key == key)
    return HelloFreshBinarySensor(_build_coordinator(), description)


def test_new_sensor_entities_reflect_account_data() -> None:
    """Additional sensors should expose normalized API fields."""
    assert _sensor_for("selected_meal_count").native_value == 1
    assert _sensor_for("required_meal_count").native_value == 3
    assert _sensor_for("selected_plan").native_value == "Classic Box"
    # Status sensors are presented sentence-cased with separators turned to spaces,
    # regardless of the raw API casing/underscores (see humanize_status).
    assert _sensor_for("subscription_status").native_value == "Active"  # raw "ACTIVE"
    assert _sensor_for("next_order_status").native_value == "Scheduled"  # raw "scheduled"
    assert _sensor_for("shipment_tracking_status").native_value == "In transit"  # raw "in_transit"
    assert _sensor_for("next_delivery_slot").native_value == "Mon 8:00 AM - 8:00 PM"
    assert _sensor_for("tracked_shipment_carrier").native_value == "UPS"
    assert _sensor_for("number_of_people").native_value == 2
    assert _sensor_for("delivery_address").native_value == "62 Leonard St, Gloucester, MA, 01930"
    assert _sensor_for("recent_payment_date").native_value == date(2026, 6, 4)
    assert _sensor_for("next_payment_date").native_value == date(2026, 6, 11)
    assert _sensor_for("skipped_week_count").native_value == 1
    assert _sensor_for("next_skipped_week").native_value == "Week 26"
    assert _sensor_for("boxes_received").native_value == 14
    assert _sensor_for("last_delivery_date").native_value == date.today() - timedelta(days=7)
    assert _sensor_for("next_box_coupon").native_value == "WELCOME20"
    assert (
        _sensor_for("next_delivery_tracking_url").native_value
        == "https://track.example.com/TRACK123"
    )
    assert _sensor_for("next_holiday_delivery_date").native_value == date(2026, 6, 16)
    assert _sensor_for("next_holiday_message").native_value == "Delivery shifted for the holiday"
    assert _sensor_for("next_delivery_blocked").native_value is True


def test_access_token_minutes_remaining_sensor() -> None:
    """The access token sensor should report whole minutes and precise attributes."""
    coordinator = _build_coordinator()
    expires_at = datetime.now(UTC) + timedelta(minutes=25, seconds=40)
    coordinator.client.token_expires_at = expires_at

    description = next(
        item for item in SENSOR_DESCRIPTIONS if item.key == "access_token_minutes_remaining"
    )
    sensor = HelloFreshSensor(coordinator, description)

    assert sensor.native_value == 25
    assert sensor.native_unit_of_measurement == "min"
    attributes = sensor.extra_state_attributes
    assert attributes is not None
    assert attributes["expires_at"] == expires_at.isoformat()
    assert isinstance(attributes["seconds_remaining"], int)
    assert attributes["seconds_remaining"] > 25 * 60


def test_refresh_token_days_remaining_sensor() -> None:
    """The refresh token sensor should report whole days and precise attributes."""
    coordinator = _build_coordinator()
    expires_at = datetime.now(UTC) + timedelta(days=42, hours=5)
    coordinator.client.refresh_token_expires_at = expires_at

    description = next(
        item for item in SENSOR_DESCRIPTIONS if item.key == "refresh_token_days_remaining"
    )
    sensor = HelloFreshSensor(coordinator, description)

    assert sensor.native_value == 42
    assert sensor.native_unit_of_measurement == "d"
    attributes = sensor.extra_state_attributes
    assert attributes is not None
    assert attributes["expires_at"] == expires_at.isoformat()
    assert attributes["seconds_remaining"] > 42 * 86400


def test_token_sensors_handle_unknown_expiry() -> None:
    """The token sensors should degrade to None when expiry is unknown."""
    coordinator = _build_coordinator()
    coordinator.client.token_expires_at = None
    coordinator.client.refresh_token_expires_at = None

    for key in ("access_token_minutes_remaining", "refresh_token_days_remaining"):
        description = next(item for item in SENSOR_DESCRIPTIONS if item.key == key)
        sensor = HelloFreshSensor(coordinator, description)
        assert sensor.native_value is None
        assert sensor.extra_state_attributes == {"expires_at": None, "seconds_remaining": None}


def test_sensor_descriptions_have_translation_names() -> None:
    """Every declared sensor should have a matching translation string."""
    strings = json.loads(Path("custom_components/hellofresh/strings.json").read_text())
    translated_sensor_keys = set(strings["entity"]["sensor"])

    assert {description.key for description in SENSOR_DESCRIPTIONS} <= translated_sensor_keys


def test_binary_sensor_descriptions_have_translation_names() -> None:
    """Every declared binary sensor should have a matching translation string."""
    strings = json.loads(Path("custom_components/hellofresh/strings.json").read_text())
    translated_keys = set(strings["entity"].get("binary_sensor", {}))

    assert {description.key for description in BINARY_SENSORS} <= translated_keys


def test_strings_and_english_translations_stay_in_sync() -> None:
    """strings.json (translation source of truth) must match the bundled en.json.

    These two files drift easily when an entity is added to one but not the other;
    keeping them identical avoids missing names and stale keys in the UI.
    """
    strings = json.loads(Path("custom_components/hellofresh/strings.json").read_text())
    english = json.loads(Path("custom_components/hellofresh/translations/en.json").read_text())
    assert strings == english


def test_price_sensor_exposes_precision_on_entity() -> None:
    """Price precision should not rely on newer SensorEntityDescription fields."""
    assert _sensor_for("next_box_total_price").suggested_display_precision == 2


def test_account_credit_sensor_reports_amount_currency_and_precision() -> None:
    """The account credit sensor reads the model amount, currency, and 2-dp precision."""
    description = next(d for d in SENSOR_DESCRIPTIONS if d.key == "account_credit")
    data = HelloFreshAccountData(account_credit=12.5, account_credit_currency="USD").finalize()
    coordinator = SimpleNamespace(
        data=data,
        config_entry=SimpleNamespace(entry_id="entry-1", title="HelloFresh"),
        client=SimpleNamespace(base_url="https://www.hellofresh.com"),
        last_update_success=True,
    )
    sensor = HelloFreshSensor(coordinator, description)
    sensor.coordinator = coordinator

    assert sensor.native_value == 12.5
    assert sensor.native_unit_of_measurement == "USD"
    assert sensor.suggested_display_precision == 2


def test_absence_string_sensors_report_none_text_when_empty() -> None:
    """String sensors whose empty value means "there isn't one" surface the literal "None",

    not HA's "Unknown" (which reads as a data gap). Date/number sensors keep reporting None.
    """
    data = HelloFreshAccountData().finalize()
    coordinator = SimpleNamespace(
        data=data,
        config_entry=SimpleNamespace(entry_id="entry-1", title="HelloFresh"),
        client=SimpleNamespace(base_url="https://www.hellofresh.com"),
        last_update_success=True,
    )

    def _value(key: str):
        description = next(d for d in SENSOR_DESCRIPTIONS if d.key == key)
        sensor = HelloFreshSensor(coordinator, description)
        sensor.coordinator = coordinator
        return sensor.native_value

    assert _value("next_skipped_week") == "None"
    assert _value("next_box_coupon") == "None"
    assert _value("next_holiday_message") == "None"
    # A date sensor with no value stays None (renders as Unknown) — not converted to "None" text.
    assert _value("last_delivery_date") is None
    assert _value("next_holiday_delivery_date") is None


def test_account_credit_sensor_handles_missing_credit() -> None:
    """With no credit data the sensor reports None value and no currency unit."""
    description = next(d for d in SENSOR_DESCRIPTIONS if d.key == "account_credit")
    data = HelloFreshAccountData().finalize()
    coordinator = SimpleNamespace(
        data=data,
        config_entry=SimpleNamespace(entry_id="entry-1", title="HelloFresh"),
        client=SimpleNamespace(base_url="https://www.hellofresh.com"),
        last_update_success=True,
    )
    sensor = HelloFreshSensor(coordinator, description)
    sensor.coordinator = coordinator

    assert sensor.native_value is None
    assert sensor.native_unit_of_measurement is None


def test_selected_plan_total_price_sensor_reports_amount_currency_and_precision() -> None:
    """The plan total price sensor reads the model amount, currency, and 2-dp precision."""
    description = next(d for d in SENSOR_DESCRIPTIONS if d.key == "selected_plan_total_price")
    data = HelloFreshAccountData(
        selected_plan_total_price=76.93,
        selected_plan_total_price_currency="USD",
    ).finalize()
    coordinator = SimpleNamespace(
        data=data,
        config_entry=SimpleNamespace(entry_id="entry-1", title="HelloFresh"),
        client=SimpleNamespace(base_url="https://www.hellofresh.com"),
        last_update_success=True,
    )
    sensor = HelloFreshSensor(coordinator, description)
    sensor.coordinator = coordinator

    assert sensor.native_value == 76.93
    assert sensor.native_unit_of_measurement == "USD"
    assert sensor.suggested_display_precision == 2


def test_selected_plan_total_price_sensor_handles_missing_price() -> None:
    """With no plan price the sensor reports None value and no currency unit."""
    description = next(d for d in SENSOR_DESCRIPTIONS if d.key == "selected_plan_total_price")
    data = HelloFreshAccountData().finalize()
    coordinator = SimpleNamespace(
        data=data,
        config_entry=SimpleNamespace(entry_id="entry-1", title="HelloFresh"),
        client=SimpleNamespace(base_url="https://www.hellofresh.com"),
        last_update_success=True,
    )
    sensor = HelloFreshSensor(coordinator, description)
    sensor.coordinator = coordinator

    assert sensor.native_value is None
    assert sensor.native_unit_of_measurement is None


def test_static_sensor_icons_match_entity_purpose() -> None:
    """Static icons should remain distinct and aligned with entity meaning."""
    assert _sensor_for("required_meal_count").icon == "mdi:numeric"
    assert _sensor_for("selected_plan").icon == "mdi:food-variant"
    assert _sensor_for("upcoming_delivery_count").icon == "mdi:truck-fast-outline"
    assert _sensor_for("next_delivery_subscription").icon == "mdi:account-box-outline"
    assert _sensor_for("tracked_shipment_carrier").icon == "mdi:truck-outline"
    assert _sensor_for("number_of_people").icon == "mdi:account-group-outline"
    assert _sensor_for("boxes_received").icon == "mdi:package-check"


def test_status_entities_use_state_aware_icons() -> None:
    """Status sensors should expose icons that reflect delivery progress."""
    # The fixture's next order is "scheduled" -> being prepared (plus icon).
    assert _sensor_for("next_order_status").icon == "mdi:package-variant-closed-plus"
    assert _sensor_for("shipment_tracking_status").icon == "mdi:truck-delivery-outline"


def test_next_order_status_icons_cover_box_lifecycle_states() -> None:
    """Each box-lifecycle state maps to a meaningful icon."""
    from datetime import date as _date

    from custom_components.hellofresh.sensor_helpers import sensor_icon

    cases = {
        "PREPARING": "mdi:package-variant-closed-plus",
        "RUNNING": "mdi:package-variant-closed-plus",
        "ON_THE_WAY": "mdi:truck-delivery-outline",
        "out_for_delivery": "mdi:truck-delivery-outline",
        "DELIVERED": "mdi:package-variant-closed-check",
        "PAUSED": "mdi:package-variant-closed-remove",
    }
    for state, expected_icon in cases.items():
        data = HelloFreshAccountData(
            orders=[
                HelloFreshOrder(
                    order_id="o",
                    week_id="w",
                    status=state,
                    subscription_id="sub-1",
                    delivery_date=_date.today(),
                )
            ]
        ).finalize()
        assert sensor_icon("next_order_status", data, None) == expected_icon


def test_new_sensor_entities_include_context_attributes() -> None:
    """Additional sensors should keep the broader account context in attributes."""
    # Week sensors expose the next_selection_week detail, but the full `weeks` list lives
    # ONLY on next_selection_deadline (the entity the dashboard's per-week table reads) so
    # the larger payload isn't duplicated onto every week sensor and blow the 16 KB cap.
    selection_attributes = _sensor_for("selected_meal_count").extra_state_attributes
    assert selection_attributes is not None
    assert selection_attributes["next_selection_week"]["selection_progress"] == "1/3"  # type: ignore[index]
    assert "weeks" not in selection_attributes

    deadline_attributes = _sensor_for("next_selection_deadline").extra_state_attributes
    assert deadline_attributes is not None
    assert "weeks" in deadline_attributes

    # Subscription-context sensors now expose lightweight counts only, not the full
    # serialized weeks/orders/subscriptions blobs (those broke the 16 KB recorder cap).
    skipped_attributes = _sensor_for("next_skipped_week").extra_state_attributes
    assert skipped_attributes is not None
    assert "weeks" not in skipped_attributes
    assert skipped_attributes["subscription_count"] == 1  # type: ignore[index]


def test_new_binary_sensors_reflect_api_capabilities() -> None:
    """Additional binary sensors should mirror capability and tracking state."""
    assert _binary_sensor_for("write_actions_available").is_on is True
    assert _binary_sensor_for("tracked_shipment_available").is_on is True
    assert _binary_sensor_for("payload_shape_changed").is_on is True


def test_delivery_sensors_read_subscription_api_fields() -> None:
    """The delivery/selectable sensors source from the subscription's API fields.

    next_delivery_date/week come from nextDelivery/nextDeliveryWeek; the selectable
    pair from nextModifiableDeliveryDate/Week; weeks emit the ISO week label.
    """
    data = HelloFreshAccountData(
        subscriptions=[
            HelloFreshSubscription(
                subscription_id="sub-1",
                next_delivery=date(2026, 6, 19),  # Friday, ISO week 2026-W25
                next_delivery_week="2026-W25",
                next_modifiable_delivery_date=date(2026, 6, 26),  # ISO week 2026-W26
                next_modifiable_delivery_week="2026-W26",
                next_cutoff_date=datetime(2026, 6, 17, 23, 59),
            )
        ],
    ).finalize()
    coordinator = SimpleNamespace(
        data=data,
        config_entry=SimpleNamespace(entry_id="entry-1", title="HelloFresh"),
        client=SimpleNamespace(base_url="https://www.hellofresh.com"),
    )

    def _value(key: str):
        desc = next(item for item in SENSOR_DESCRIPTIONS if item.key == key)
        return HelloFreshSensor(coordinator, desc).native_value

    assert _value("next_delivery_date") == date(2026, 6, 19)
    assert _value("next_delivery_week") == "2026-W25"  # ISO week identifier (a label)
    assert _value("next_selectable_delivery_date") == date(2026, 6, 26)
    assert _value("next_selectable_delivery_week") == "2026-W26"
    # No resolved modifiable week here (no week with id 2026-W26), so the deadline falls
    # back to the subscription's nextCutoffDate. Offset-less deadlines are normalized to
    # UTC-aware so HA's TIMESTAMP state write doesn't reject them.
    assert _value("next_selection_deadline") == datetime(2026, 6, 17, 23, 59, tzinfo=UTC)


def test_required_meal_count_falls_back_to_subscription_plan() -> None:
    """Number of meals should still come from the subscription when no pending week exists."""
    data = HelloFreshAccountData(
        weeks=[],
        subscriptions=[
            HelloFreshSubscription(
                subscription_id="sub-1",
                meals_required=3,
            )
        ],
    ).finalize()
    coordinator = SimpleNamespace(
        data=data,
        config_entry=SimpleNamespace(entry_id="entry-1", title="HelloFresh"),
        client=SimpleNamespace(base_url="https://www.hellofresh.com"),
    )
    description = next(item for item in SENSOR_DESCRIPTIONS if item.key == "required_meal_count")

    assert HelloFreshSensor(coordinator, description).native_value == 3


def test_selected_meal_count_uses_next_upcoming_week_when_selection_complete() -> None:
    """Selected meal count should not fall back to zero once the next box is fully selected."""
    data = HelloFreshAccountData(
        weeks=[
            HelloFreshWeek(
                week_id="2026-W25",
                display_name="Classic Box",
                subscription_id="sub-1",
                delivery_date=date(2026, 6, 15),
                meals_required=3,
                meals_selected=3,
            )
        ],
    ).finalize()
    coordinator = SimpleNamespace(
        data=data,
        config_entry=SimpleNamespace(entry_id="entry-1", title="HelloFresh"),
        client=SimpleNamespace(base_url="https://www.hellofresh.com"),
    )
    description = next(item for item in SENSOR_DESCRIPTIONS if item.key == "selected_meal_count")

    assert HelloFreshSensor(coordinator, description).native_value == 3


def test_delivery_deadlines_track_their_respective_weeks() -> None:
    """Two deadline sensors target two different weeks by their per-week cutoffDate.

    next_selection_deadline -> next delivery week (nextDeliveryWeek) cutoff.
    next_selectable_delivery_selection_deadline -> modifiable week (the week after) cutoff.
    Mirrors the live HAR: W25 ships next, W26 is the editable one (cutoff = Jun 18).
    """
    delivery_cutoff = datetime(2026, 6, 11, 23, 59, 59)
    modifiable_cutoff = datetime(2026, 6, 18, 23, 59, 59)
    data = HelloFreshAccountData(
        weeks=[
            HelloFreshWeek(
                week_id="2026-W25",
                display_name="Week 25",
                subscription_id="sub-1",
                delivery_date=date(2026, 6, 16),
                selection_deadline=delivery_cutoff,
            ),
            HelloFreshWeek(
                week_id="2026-W26",
                display_name="Week 26",
                subscription_id="sub-1",
                delivery_date=date(2026, 6, 23),
                selection_deadline=modifiable_cutoff,
            ),
        ],
        subscriptions=[
            HelloFreshSubscription(
                subscription_id="sub-1",
                next_delivery_week="2026-W25",
                next_modifiable_delivery_week="2026-W26",
            ),
        ],
    ).finalize()
    coordinator = SimpleNamespace(
        data=data,
        config_entry=SimpleNamespace(entry_id="entry-1", title="HelloFresh"),
        client=SimpleNamespace(base_url="https://www.hellofresh.com"),
    )

    def _value(key: str):
        desc = next(item for item in SENSOR_DESCRIPTIONS if item.key == key)
        return HelloFreshSensor(coordinator, desc).native_value

    # Naive per-week cutoffs surface as UTC-aware timestamps (HA requires tz-aware).
    assert _value("next_selection_deadline") == delivery_cutoff.replace(tzinfo=UTC)
    assert _value("next_selectable_delivery_selection_deadline") == modifiable_cutoff.replace(
        tzinfo=UTC
    )


def test_next_selection_deadline_falls_back_to_subscription_cutoff() -> None:
    """When the next delivery week isn't resolved, the deadline uses nextCutoffDate."""
    fallback = datetime(2026, 6, 11, 23, 59, 59)
    data = HelloFreshAccountData(
        subscriptions=[
            HelloFreshSubscription(
                subscription_id="sub-1",
                next_delivery_week="2026-W25",  # no matching week object exists
                next_cutoff_date=fallback,
            ),
        ],
    ).finalize()
    coordinator = SimpleNamespace(
        data=data,
        config_entry=SimpleNamespace(entry_id="entry-1", title="HelloFresh"),
        client=SimpleNamespace(base_url="https://www.hellofresh.com"),
    )
    description = next(
        item for item in SENSOR_DESCRIPTIONS if item.key == "next_selection_deadline"
    )

    assert HelloFreshSensor(coordinator, description).native_value == fallback.replace(
        tzinfo=UTC
    )


def test_next_selectable_delivery_meal_count_reads_modifiable_week() -> None:
    """The sensor reports meals_selected for the resolved modifiable week."""
    data = HelloFreshAccountData(
        weeks=[
            HelloFreshWeek(
                week_id="2026-W26",
                display_name="Week 26",
                subscription_id="sub-1",
                delivery_date=date(2026, 6, 23),
                meals_required=3,
                meals_selected=2,
            )
        ],
        subscriptions=[
            HelloFreshSubscription(
                subscription_id="sub-1",
                next_modifiable_delivery_week="2026-W26",
            ),
        ],
    ).finalize()
    coordinator = SimpleNamespace(
        data=data,
        config_entry=SimpleNamespace(entry_id="entry-1", title="HelloFresh"),
        client=SimpleNamespace(base_url="https://www.hellofresh.com"),
    )
    description = next(
        item for item in SENSOR_DESCRIPTIONS if item.key == "next_selectable_delivery_meal_count"
    )

    assert HelloFreshSensor(coordinator, description).native_value == 2


def test_next_selectable_delivery_meal_count_zero_without_modifiable_week() -> None:
    """With no resolved modifiable week the count is 0."""
    data = HelloFreshAccountData(
        subscriptions=[HelloFreshSubscription(subscription_id="sub-1")],
    ).finalize()
    coordinator = SimpleNamespace(
        data=data,
        config_entry=SimpleNamespace(entry_id="entry-1", title="HelloFresh"),
        client=SimpleNamespace(base_url="https://www.hellofresh.com"),
    )
    description = next(
        item for item in SENSOR_DESCRIPTIONS if item.key == "next_selectable_delivery_meal_count"
    )

    assert HelloFreshSensor(coordinator, description).native_value == 0


def test_selected_market_count_counts_distinct_selected_items() -> None:
    """selected_market_count reports the number of distinct selected market items for the week."""
    data = HelloFreshAccountData(
        weeks=[
            HelloFreshWeek(
                week_id="2026-W26",
                display_name="Week 26",
                subscription_id="sub-1",
                delivery_date=date(2026, 6, 23),
                meals_required=3,
                meals_selected=3,  # selection complete so this becomes the configurable week
                market_items=[
                    HelloFreshMarketItem(
                        item_id="a", name="Gyoza", is_selected=True, selected_quantity=2
                    ),
                    HelloFreshMarketItem(
                        item_id="b", name="Trout", is_selected=True, selected_quantity=1
                    ),
                    HelloFreshMarketItem(item_id="c", name="Cake", is_selected=False),
                ],
            )
        ],
        subscriptions=[
            HelloFreshSubscription(subscription_id="sub-1", next_delivery_week="2026-W26"),
        ],
    ).finalize()
    coordinator = SimpleNamespace(
        data=data,
        config_entry=SimpleNamespace(entry_id="entry-1", title="HelloFresh"),
        client=SimpleNamespace(base_url="https://www.hellofresh.com"),
    )
    description = next(d for d in SENSOR_DESCRIPTIONS if d.key == "selected_market_count")
    # 2 distinct items selected (quantity does not inflate the count).
    assert HelloFreshSensor(coordinator, description).native_value == 2


def test_next_selectable_delivery_market_count_reads_modifiable_week() -> None:
    """next_selectable_delivery_market_count reports selected market items on the modifiable week."""
    data = HelloFreshAccountData(
        weeks=[
            HelloFreshWeek(
                week_id="2026-W26",
                display_name="Week 26",
                subscription_id="sub-1",
                delivery_date=date(2026, 6, 23),
                market_items=[
                    HelloFreshMarketItem(item_id="a", name="Gyoza", is_selected=True),
                ],
            )
        ],
        subscriptions=[
            HelloFreshSubscription(
                subscription_id="sub-1", next_modifiable_delivery_week="2026-W26"
            ),
        ],
    ).finalize()
    coordinator = SimpleNamespace(
        data=data,
        config_entry=SimpleNamespace(entry_id="entry-1", title="HelloFresh"),
        client=SimpleNamespace(base_url="https://www.hellofresh.com"),
    )
    description = next(
        d for d in SENSOR_DESCRIPTIONS if d.key == "next_selectable_delivery_market_count"
    )
    assert HelloFreshSensor(coordinator, description).native_value == 1


def test_market_count_sensors_zero_without_resolved_week() -> None:
    """Both market-count sensors return 0 when no relevant week resolves."""
    data = HelloFreshAccountData(
        subscriptions=[HelloFreshSubscription(subscription_id="sub-1")],
    ).finalize()
    coordinator = SimpleNamespace(
        data=data,
        config_entry=SimpleNamespace(entry_id="entry-1", title="HelloFresh"),
        client=SimpleNamespace(base_url="https://www.hellofresh.com"),
    )
    for key in ("selected_market_count", "next_selectable_delivery_market_count"):
        description = next(d for d in SENSOR_DESCRIPTIONS if d.key == key)
        assert HelloFreshSensor(coordinator, description).native_value == 0


def test_weeks_needing_selection_sensor_counts_auto_picked_weeks() -> None:
    """The sensor counts auto-picked (menu mealsPreselected) weeks that are still EDITABLE.

    Editable mirrors the meal-planner card's banner: meal swaps allowed and the selection
    deadline (when known) still ahead. A locked week ships with its auto-picks no matter
    what, so it stops counting — the sensor is a call to action.
    """
    today = date.today()
    editable = {"mealSwap": True}
    open_deadline = datetime.now(UTC) + timedelta(days=3)
    data = HelloFreshAccountData(
        weeks=[
            # Auto-picked by HelloFresh, upcoming, still editable (counts).
            HelloFreshWeek(
                week_id="next",
                display_name="Next week",
                subscription_id="sub-1",
                delivery_date=today + timedelta(days=7),
                selection_deadline=open_deadline,
                allowed_actions=editable,
                meals_required=3,
                meals_selected=3,
                meals_preselected=True,
            ),
            # Customer-picked, upcoming (does not count).
            HelloFreshWeek(
                week_id="next-plus",
                display_name="Week after",
                subscription_id="sub-1",
                delivery_date=today + timedelta(days=14),
                selection_deadline=open_deadline,
                allowed_actions=editable,
                meals_required=3,
                meals_selected=3,
                meals_preselected=False,
            ),
            # Auto-picked but skipped (excluded — no box ships).
            HelloFreshWeek(
                week_id="skipped",
                display_name="Skipped week",
                subscription_id="sub-1",
                delivery_date=today + timedelta(days=21),
                selection_deadline=open_deadline,
                allowed_actions=editable,
                meals_preselected=True,
                is_skipped=True,
            ),
            # Auto-picked but in the past (excluded — box already shipped).
            HelloFreshWeek(
                week_id="past",
                display_name="Past week",
                subscription_id="sub-1",
                delivery_date=today - timedelta(days=7),
                allowed_actions=editable,
                meals_required=3,
                meals_selected=3,
                meals_preselected=True,
            ),
            # Auto-picked but LOCKED (deadline passed — excluded even though it hasn't
            # delivered yet: nothing is actionable, matching the card's banner).
            HelloFreshWeek(
                week_id="locked",
                display_name="Locked week",
                subscription_id="sub-1",
                delivery_date=today + timedelta(days=2),
                selection_deadline=datetime.now(UTC) - timedelta(hours=2),
                allowed_actions=editable,
                meals_required=3,
                meals_selected=3,
                meals_preselected=True,
            ),
            # Auto-picked but PAUSED (excluded — the box never ships).
            HelloFreshWeek(
                week_id="paused",
                display_name="Paused week",
                subscription_id="sub-1",
                status="PAUSED",
                delivery_date=today + timedelta(days=28),
                selection_deadline=open_deadline,
                allowed_actions=editable,
                meals_required=3,
                meals_selected=3,
                meals_preselected=True,
            ),
        ],
        subscriptions=[HelloFreshSubscription(subscription_id="sub-1")],
    ).finalize()
    coordinator = SimpleNamespace(
        data=data,
        config_entry=SimpleNamespace(entry_id="entry-1", title="HelloFresh"),
        client=SimpleNamespace(base_url="https://www.hellofresh.com"),
    )
    description = next(d for d in SENSOR_DESCRIPTIONS if d.key == "weeks_needing_selection")
    sensor = HelloFreshSensor(coordinator, description)
    assert sensor.native_value == 1  # only the upcoming auto-picked week
    weeks = sensor.extra_state_attributes["weeks"]
    assert [w["week_id"] for w in weeks] == ["next"]
    assert weeks[0]["auto_picked"] is True


def test_delivery_preselected_sensors_read_their_weeks() -> None:
    """The preselected sensors report each week's meals_preselected flag (True/False/None)."""
    data = HelloFreshAccountData(
        weeks=[
            HelloFreshWeek(
                week_id="2026-W30",
                display_name="Week 30",
                subscription_id="sub-1",
                delivery_date=date(2026, 7, 21),
                meals_required=3,
                meals_selected=3,
                meals_preselected=True,  # next delivery week, auto-picked
            ),
            HelloFreshWeek(
                week_id="2026-W31",
                display_name="Week 31",
                subscription_id="sub-1",
                delivery_date=date(2026, 7, 28),
                meals_preselected=False,  # next modifiable week, customer-picked
            ),
        ],
        subscriptions=[
            HelloFreshSubscription(
                subscription_id="sub-1",
                next_delivery_week="2026-W30",
                next_modifiable_delivery_week="2026-W31",
            ),
        ],
    ).finalize()
    coordinator = SimpleNamespace(
        data=data,
        config_entry=SimpleNamespace(entry_id="entry-1", title="HelloFresh"),
        client=SimpleNamespace(base_url="https://www.hellofresh.com"),
    )

    def _value(key: str) -> object:
        description = next(d for d in SENSOR_DESCRIPTIONS if d.key == key)
        return HelloFreshSensor(coordinator, description).native_value

    assert _value("next_delivery_preselected") is True
    assert _value("next_selectable_delivery_preselected") is False


def test_delivery_preselected_sensors_none_without_week() -> None:
    """With no resolved week the preselected sensors are unknown (None), not False."""
    data = HelloFreshAccountData(
        subscriptions=[HelloFreshSubscription(subscription_id="sub-1")],
    ).finalize()
    coordinator = SimpleNamespace(
        data=data,
        config_entry=SimpleNamespace(entry_id="entry-1", title="HelloFresh"),
        client=SimpleNamespace(base_url="https://www.hellofresh.com"),
    )
    for key in ("next_delivery_preselected", "next_selectable_delivery_preselected"):
        description = next(d for d in SENSOR_DESCRIPTIONS if d.key == key)
        assert HelloFreshSensor(coordinator, description).native_value is None


def test_configurable_week_falls_back_to_fully_selected_upcoming_week() -> None:
    """A fully selected upcoming week is still the configurable week (none needs selection)."""
    data = HelloFreshAccountData(
        weeks=[
            HelloFreshWeek(
                week_id="2026-W25",
                display_name="Classic Box",
                subscription_id="sub-1",
                delivery_date=date(2026, 6, 15),
                selection_deadline=datetime(2026, 6, 1, 18, 0),
                meals_required=3,
                meals_selected=3,
            )
        ],
    ).finalize()

    assert data.next_selection_week is None
    assert data.next_configurable_week is not None


def _modifiable_week_data(*, skipped: bool, has_handle: bool = True) -> HelloFreshAccountData:
    """Build account data whose next modifiable week is W26."""
    return HelloFreshAccountData(
        weeks=[
            HelloFreshWeek(
                week_id="2026-W25",
                display_name="Week 25",
                subscription_id="sub-1",
                delivery_date=date(2026, 6, 15),
                status="locked",
            ),
            HelloFreshWeek(
                week_id="2026-W26",
                display_name="Week 26",
                subscription_id="sub-1",
                delivery_date=date(2026, 6, 22),
                is_skipped=skipped,
            ),
        ],
        subscriptions=[
            HelloFreshSubscription(
                subscription_id="sub-1",
                next_modifiable_delivery_week="2026-W26" if has_handle else None,
            )
        ],
    ).finalize()


def test_week_id_lookup_prefers_primary_subscription_on_collision() -> None:
    """Two subscriptions sharing an ISO week id must resolve to the PRIMARY's week.

    Keyed by week_id alone the last-sorted week silently won, so the skip switch and
    service writes could act on the other subscription's box.
    """
    primary_week = HelloFreshWeek(
        week_id="2026-W33",
        display_name="Primary",
        subscription_id="sub-1",
        delivery_date=date(2026, 8, 12),
    )
    other_week = HelloFreshWeek(
        week_id="2026-W33",
        display_name="Other",
        subscription_id="sub-2",
        # Sorts after the primary week, so "last write wins" would have picked it.
        delivery_date=date(2026, 8, 14),
    )
    data = HelloFreshAccountData(
        weeks=[primary_week, other_week],
        subscriptions=[
            HelloFreshSubscription(subscription_id="sub-1"),
            HelloFreshSubscription(subscription_id="sub-2"),
        ],
    ).finalize()

    resolved = data.get_week("2026-W33")
    assert resolved is not None
    assert resolved.subscription_id == "sub-1"


def test_next_modifiable_week_resolves_from_subscription_handle() -> None:
    """The model anchors on the subscription handle, not the next undelivered week."""
    data = _modifiable_week_data(skipped=False)

    week = data.next_modifiable_week
    assert week is not None
    assert week.week_id == "2026-W26"


def test_next_modifiable_week_is_none_without_handle() -> None:
    """No modifiable week when the subscription does not advertise one."""
    assert _modifiable_week_data(skipped=False, has_handle=False).next_modifiable_week is None


def _switch(data: HelloFreshAccountData) -> HelloFreshSwitch:
    """Return the skip switch backed by the given account data."""
    coordinator = SimpleNamespace(
        data=data,
        hass=None,
        config_entry=SimpleNamespace(entry_id="entry-1", title="HelloFresh"),
        client=SimpleNamespace(skipped=[], unskipped=[]),
        last_update_success=True,
    )

    async def _skip(week_id: str) -> None:
        coordinator.client.skipped.append(week_id)

    async def _unskip(week_id: str) -> None:
        coordinator.client.unskipped.append(week_id)

    async def _refresh() -> None:
        coordinator.refreshed = True

    coordinator.client.async_skip_week = _skip
    coordinator.client.async_unskip_week = _unskip
    coordinator.async_request_refresh = _refresh
    description = next(d for d in SWITCHES if d.key == "skip_next_modifiable_week")
    switch = HelloFreshSwitch(coordinator, description)
    switch.coordinator = coordinator
    return switch


def _run(coro) -> None:
    """Run a coroutine on a fresh event loop (matches the repo's async test style)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(coro)


def test_skip_switch_reflects_skipped_state() -> None:
    """is_on tracks whether the next modifiable week is skipped."""
    assert _switch(_modifiable_week_data(skipped=True)).is_on is True
    assert _switch(_modifiable_week_data(skipped=False)).is_on is False


def test_skip_switch_unavailable_without_modifiable_week() -> None:
    """The switch hides when no modifiable week exists."""
    switch = _switch(_modifiable_week_data(skipped=False, has_handle=False))
    assert switch.available is False
    assert switch.is_on is None


def test_skip_switch_turn_on_skips_the_modifiable_week() -> None:
    """Turning on calls skip for the modifiable week's id and clears the write warning."""
    switch = _switch(_modifiable_week_data(skipped=False))
    with mock.patch.object(switch_module, "async_delete_write_actions_issue") as clear:
        _run(switch.async_turn_on())
    assert switch.coordinator.client.skipped == ["2026-W26"]
    assert switch.coordinator.client.unskipped == []
    assert clear.call_count == 1


def test_skip_switch_turn_off_restores_the_modifiable_week() -> None:
    """Turning off calls unskip for the modifiable week's id and clears the write warning."""
    switch = _switch(_modifiable_week_data(skipped=True))
    with mock.patch.object(switch_module, "async_delete_write_actions_issue") as clear:
        _run(switch.async_turn_off())
    assert switch.coordinator.client.unskipped == ["2026-W26"]
    assert switch.coordinator.client.skipped == []
    assert clear.call_count == 1


def test_skip_switch_noop_when_already_in_target_state() -> None:
    """No client call when the week is already in the requested state."""
    switch = _switch(_modifiable_week_data(skipped=True))
    _run(switch.async_turn_on())
    assert switch.coordinator.client.skipped == []


def test_switch_descriptions_have_translation_names() -> None:
    """Every declared switch should have a matching translation string."""
    strings = json.loads(Path("custom_components/hellofresh/strings.json").read_text())
    translated_keys = set(strings["entity"].get("switch", {}))

    assert {description.key for description in SWITCHES} <= translated_keys


def test_humanize_status_normalizes_casing_and_separators() -> None:
    """Status values are sentence-cased with underscores/hyphens turned to spaces."""
    from custom_components.hellofresh.sensor_helpers import humanize_status

    # The exact cases the user called out.
    assert humanize_status("pre_transit") == "Pre transit"
    assert humanize_status("active") == "Active"
    assert humanize_status("PREPARING") == "Preparing"
    # Multi-word + hyphen separators.
    assert humanize_status("out_for_delivery") == "Out for delivery"
    assert humanize_status("on-the-way") == "On the way"
    # Mixed case collapses to sentence case.
    assert humanize_status("In_Transit") == "In transit"
    # Non-strings and empties pass through untouched (numbers, bools, None, blank).
    assert humanize_status(None) is None
    assert humanize_status(3) == 3
    assert humanize_status(True) is True
    assert humanize_status("") == ""
