"""Unit tests for HelloFresh entity mappings."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

from custom_components.hellofresh.api import (
    HelloFreshAccountData,
    HelloFreshCapabilities,
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


def _build_coordinator() -> SimpleNamespace:
    """Create a minimal coordinator stand-in for entity unit tests."""
    next_selection_week = HelloFreshWeek(
        week_id="2026-W25",
        display_name="Week 25",
        subscription_id="sub-1",
        delivery_date=date(2026, 6, 15),
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
        delivery_date=date(2026, 6, 22),
        meals_required=3,
        meals_selected=3,
        is_skipped=True,
    )
    next_order = HelloFreshOrder(
        order_id="ord-1",
        week_id="2026-W25",
        status="scheduled",
        subscription_id="sub-1",
        delivery_date=date(2026, 6, 15),
        total_price=97.5,
        currency="USD",
        slot_label="Mon 8:00 AM - 8:00 PM",
    )
    tracked_order = HelloFreshOrder(
        order_id="ord-2",
        week_id="2026-W24",
        status="in_transit",
        subscription_id="sub-1",
        delivery_date=date(2026, 6, 18),
        tracking_number="TRACK123",
        tracking_status="in_transit",
        carrier="UPS",
        tracking_url="https://track.example.com/TRACK123",
    )
    delivered_week = HelloFreshWeek(
        week_id="2026-W23",
        display_name="Week 23",
        subscription_id="sub-1",
        delivery_date=date(2026, 6, 8),
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
                servings=2,
                delivery_address="100 Main St, Anytown, MA, 01103",
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


def _binary_sensor_for(key: str) -> HelloFreshBinarySensor:
    """Return a binary sensor entity for the requested key."""
    description = next(item for item in BINARY_SENSORS if item.key == key)
    return HelloFreshBinarySensor(_build_coordinator(), description)


def test_new_sensor_entities_reflect_account_data() -> None:
    """Additional sensors should expose normalized API fields."""
    assert _sensor_for("selected_meal_count").native_value == 1
    assert _sensor_for("required_meal_count").native_value == 3
    assert _sensor_for("selected_plan").native_value == "Classic Box"
    assert _sensor_for("next_delivery_slot").native_value == "Mon 8:00 AM - 8:00 PM"
    assert _sensor_for("tracked_shipment_carrier").native_value == "UPS"
    assert _sensor_for("number_of_people").native_value == 2
    assert _sensor_for("delivery_address").native_value == "100 Main St, Anytown, MA, 01103"
    assert _sensor_for("recent_payment_date").native_value == date(2026, 6, 4)
    assert _sensor_for("next_payment_date").native_value == date(2026, 6, 11)
    assert _sensor_for("skipped_week_count").native_value == 1
    assert _sensor_for("next_skipped_week").native_value == "Week 26"
    assert _sensor_for("boxes_received").native_value == 14
    assert _sensor_for("last_delivery_date").native_value == date(2026, 6, 8)
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
    assert _sensor_for("next_order_status").icon == "mdi:package-variant-closed"
    assert _sensor_for("shipment_tracking_status").icon == "mdi:truck-delivery-outline"


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
    assert _binary_sensor_for("account_menu_api_available").is_on is True
    assert _binary_sensor_for("write_actions_available").is_on is True
    assert _binary_sensor_for("reschedule_available").is_on is True
    assert _binary_sensor_for("delivery_weekday_change_available").is_on is True
    assert _binary_sensor_for("tracked_shipment_available").is_on is True
    assert _binary_sensor_for("payload_shape_changed").is_on is True


def test_next_delivery_week_is_iso_week_label_not_the_delivery_date() -> None:
    """next_delivery_week emits the ISO week id, distinct from next_delivery_date's date."""
    # Delivery on Friday 2026-06-19 (ISO week 2026-W25).
    week = HelloFreshWeek(
        week_id="2026-W25",
        display_name="Classic Box",
        subscription_id="sub-1",
        delivery_date=date(2026, 6, 19),
        meals_required=3,
        meals_selected=1,
    )
    order = HelloFreshOrder(
        order_id="ord-1",
        week_id="2026-W25",
        status="scheduled",
        subscription_id="sub-1",
        delivery_date=date(2026, 6, 19),
    )
    data = HelloFreshAccountData(weeks=[week], orders=[order]).finalize()
    coordinator = SimpleNamespace(
        data=data,
        config_entry=SimpleNamespace(entry_id="entry-1", title="HelloFresh"),
        client=SimpleNamespace(base_url="https://www.hellofresh.com"),
    )

    def _value(key: str):
        desc = next(item for item in SENSOR_DESCRIPTIONS if item.key == key)
        return HelloFreshSensor(coordinator, desc).native_value

    assert _value("next_delivery_week") == "2026-W25"  # ISO week identifier (a label)
    assert _value("next_delivery_date") == date(2026, 6, 19)  # the actual delivery date


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


def test_next_selection_deadline_uses_next_upcoming_week_when_selection_complete() -> None:
    """Next selection deadline should remain available for a fully selected upcoming week."""
    deadline = datetime(2026, 6, 10, 18, 0)
    data = HelloFreshAccountData(
        weeks=[
            HelloFreshWeek(
                week_id="2026-W25",
                display_name="Classic Box",
                subscription_id="sub-1",
                delivery_date=date(2026, 6, 15),
                selection_deadline=deadline,
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
    description = next(
        item for item in SENSOR_DESCRIPTIONS if item.key == "next_selection_deadline"
    )
    sensor = HelloFreshSensor(coordinator, description)

    assert sensor.native_value == deadline
    assert sensor.extra_state_attributes is not None
    assert (
        sensor.extra_state_attributes["next_selection_week"]["selection_deadline"]
        == deadline.isoformat()
    )  # type: ignore[index]


def test_selection_deadline_passed_is_true_when_deadline_passed_and_meals_selected() -> None:
    """Deadline should report passed even when meals are already fully selected."""
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
    assert data.selection_deadline_passed is True
