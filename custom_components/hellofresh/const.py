"""Constants for the HelloFresh integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "hellofresh"

CONF_ACCESS_TOKEN = "access_token"
CONF_COUNTRY = "country"
CONF_ENABLE_PUBLIC_MENU_FALLBACK = "enable_public_menu_fallback"
CONF_EXPIRES_IN = "expires_in"
CONF_ISSUED_AT = "issued_at"
CONF_PASSWORD = "password"
CONF_REFRESH_EXPIRES_IN = "refresh_expires_in"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_REFRESH_TOKEN_ISSUED_AT = "refresh_token_issued_at"
CONF_SCAN_INTERVAL_MINUTES = "scan_interval_minutes"
CONF_TOKEN_TYPE = "token_type"
CONF_USERNAME = "username"

# Public web-client id used by the HelloFresh frontend for the /gw/auth/token and
# /gw/login calls (observed as ``client_id=senf`` / ``NEXT_PUBLIC_GW_CLIENT_ID``).
GW_CLIENT_ID = "senf"

DEFAULT_COUNTRY = "us"
DEFAULT_SCAN_INTERVAL_MINUTES = 180
DEFAULT_ENABLE_PUBLIC_MENU_FALLBACK = True
MIN_SCAN_INTERVAL_MINUTES = 5
MAX_SCAN_INTERVAL_MINUTES = 1440

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CALENDAR,
    Platform.TODO,
]

SERVICE_REFRESH_DATA = "refresh_data"
SERVICE_SELECT_MEALS = "select_meals"
SERVICE_SKIP_WEEK = "skip_week"
SERVICE_UNSKIP_WEEK = "unskip_week"
SERVICE_RESCHEDULE_WEEK = "reschedule_week"
SERVICE_CHANGE_DELIVERY_WEEKDAY = "change_delivery_weekday"

ATTR_WEEK_ID = "week_id"
ATTR_RECIPE_IDS = "recipe_ids"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_DELIVERY_OPTION = "delivery_option"
ATTR_DELIVERY_INTERVAL = "delivery_interval"
ATTR_SUBSCRIPTION_ID = "subscription_id"

INTENT_GET_NEXT_DELIVERY = "HelloFreshGetNextDeliveryIntent"
INTENT_GET_MEAL_SELECTION = "HelloFreshGetMealSelectionIntent"
INTENT_REFRESH = "HelloFreshRefreshDataIntent"

COUNTRY_BASE_URLS: dict[str, str] = {
    "us": "https://www.hellofresh.com",
    "ca": "https://www.hellofresh.ca",
    "uk": "https://www.hellofresh.co.uk",
    "au": "https://www.hellofresh.com.au",
    "de": "https://www.hellofresh.de",
    "nl": "https://www.hellofresh.nl",
}
