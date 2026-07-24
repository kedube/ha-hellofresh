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
# Config-flow-only field: the raw apiV2Auth blob (or bare access token) pasted in the
# token setup step. It is parsed into the CONF_ACCESS_TOKEN/CONF_REFRESH_TOKEN/timing keys
# and is never itself persisted to the config entry.
CONF_TOKEN = "token"

# Public web-client id used by the HelloFresh frontend for the /gw/auth/token and
# /gw/login calls (observed as ``client_id=senf`` / ``NEXT_PUBLIC_GW_CLIENT_ID``).
GW_CLIENT_ID = "senf"

DEFAULT_COUNTRY = "us"
DEFAULT_SCAN_INTERVAL_MINUTES = 180
DEFAULT_ENABLE_PUBLIC_MENU_FALLBACK = True
MIN_SCAN_INTERVAL_MINUTES = 5
MAX_SCAN_INTERVAL_MINUTES = 1440

# How many weeks of past delivery history to fetch and make browsable. Default is ~6 months,
# which keeps the per-poll deliveries/past-deliveries payload modest; users who want a full year
# or more can raise it (up to ~2 years). Lowering it shrinks the payload further; the minimum
# still keeps the current + a couple of recent weeks. Used for BOTH the ranged display window and
# the past-deliveries pagination floor.
CONF_HISTORY_WEEKS = "history_weeks"
DEFAULT_HISTORY_WEEKS = 26
MIN_HISTORY_WEEKS = 1
MAX_HISTORY_WEEKS = 104

# How many weeks after its delivery date a week keeps its full browsable menu instead of
# collapsing to delivered-meals-only (whole weeks — HelloFresh's natural unit). HelloFresh
# still publishes the real menu for the current and immediately previous weeks, so within
# this window the validated per-week menu stays visible (with the delivered meals overlaid
# as the selection); older weeks show only what shipped, since their "menu" from the API is
# an unusable multi-week aggregate. User option like history_weeks; 0 disables the grace
# entirely (past weeks always delivered-only). The meal-planner card reads the configured
# value from the get_weeks account payload. Values past ~2 weeks buy little: HelloFresh
# stops serving a real menu for older weeks, whose fetch then fails validation and the week
# falls back to delivered-only anyway.
CONF_MENU_GRACE_WEEKS = "menu_grace_weeks"
DEFAULT_MENU_GRACE_WEEKS = 2
MIN_MENU_GRACE_WEEKS = 0
MAX_MENU_GRACE_WEEKS = 3

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CALENDAR,
    Platform.SWITCH,
]

SERVICE_REFRESH_DATA = "refresh_data"
SERVICE_GET_WEEKS = "get_weeks"
SERVICE_SELECT_MEALS = "select_meals"
SERVICE_SELECT_MARKET_ITEMS = "select_market_items"
SERVICE_SKIP_WEEK = "skip_week"
SERVICE_UNSKIP_WEEK = "unskip_week"
SERVICE_RESCHEDULE_WEEK = "reschedule_week"
SERVICE_CHANGE_DELIVERY_WEEKDAY = "change_delivery_weekday"
SERVICE_GET_FOOD_PROFILE = "get_food_profile"
SERVICE_SET_FOOD_PROFILE = "set_food_profile"
SERVICE_GET_ACCOUNT_SUMMARY = "get_account_summary"
SERVICE_GET_DELIVERY_OPTIONS = "get_delivery_options"
SERVICE_GET_PLANS = "get_plans"
SERVICE_GET_PRESETS = "get_presets"
SERVICE_GET_SPENDING = "get_spending"

ATTR_WEEK_ID = "week_id"
ATTR_TASTE = "taste"
ATTR_HOUSEHOLD = "household"
ATTR_GOALS = "goals"
ATTR_RECIPE_IDS = "recipe_ids"
ATTR_QUANTITIES = "quantities"
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
    "dk": "https://www.hellofresh.dk",
    "nl": "https://www.hellofresh.nl",
}

# The config-flow key is not always the ISO 3166 country code HelloFresh's API expects.
# Notably the UK site selects `uk` but the API uses `GB` (confirmed from a HAR:
# `/gw/auth/email/status` posts `{"country":"GB"}`). Sending the wrong code (e.g. `UK`)
# makes /gw/login and /gw/refresh fail, which is why the integration didn't work outside
# the US. Map each config key to the API country code; unlisted keys upper-case as-is.
COUNTRY_API_CODES: dict[str, str] = {
    "uk": "GB",
}

# Default API locale per config key. The frontends use the native locale (e.g. `de-DE`).
# A subscription's own `locale` from the account payload overrides this once loaded; this
# is only the value used for the initial pre-subscription calls (including login/refresh).
COUNTRY_API_LOCALES: dict[str, str] = {
    "us": "en-US",
    "ca": "en-CA",
    "uk": "en-GB",
    "au": "en-AU",
    "de": "de-DE",
    "dk": "da-DK",
    "nl": "nl-NL",
}


def api_country_code(country: str) -> str:
    """Return the ISO country code HelloFresh's API expects for a config-flow key."""
    return COUNTRY_API_CODES.get(country, country.upper())


def api_locale(country: str) -> str:
    """Return the default API locale for a config-flow key."""
    return COUNTRY_API_LOCALES.get(country, f"en-{api_country_code(country)}")
