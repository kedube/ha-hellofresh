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

# Whether to raise Home Assistant Repairs warnings when HelloFresh data quality degrades
# (public-menu fallback active, unrecognized payload shape, account data unavailable,
# blocked write actions). These are advisory — data keeps flowing either way — so users
# who find them noisy can turn them off; any existing warnings are cleared on the next
# refresh. A single warning can also be silenced with "Ignore" on the Repairs screen.
CONF_SHOW_DATA_QUALITY_ISSUES = "show_data_quality_issues"
DEFAULT_SHOW_DATA_QUALITY_ISSUES = True

# Whether each poll should resolve which meals are bookmarked in the customer's cookbook, so
# the meal-planner card can show a favorite heart. Costs one extra batched request per refresh
# (HelloFresh has no "list my favorites" endpoint — the ids on the menu must be sent to a
# filter endpoint to learn which are bookmarked). Turn off to skip that request entirely; the
# add/remove favorite services keep working either way.
CONF_ENABLE_FAVORITES = "enable_favorites"
DEFAULT_ENABLE_FAVORITES = True

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CALENDAR,
    Platform.SWITCH,
    Platform.TODO,
]

SERVICE_REFRESH_DATA = "refresh_data"
SERVICE_GET_WEEKS = "get_weeks"
SERVICE_SELECT_MEALS = "select_meals"
SERVICE_SELECT_MARKET_ITEMS = "select_market_items"
SERVICE_SKIP_WEEK = "skip_week"
SERVICE_UNSKIP_WEEK = "unskip_week"
SERVICE_RESCHEDULE_WEEK = "reschedule_week"
SERVICE_CHANGE_DELIVERY_WEEKDAY = "change_delivery_weekday"
SERVICE_CHANGE_PLAN = "change_plan"
SERVICE_GET_PLAN_OPTIONS = "get_plan_options"
SERVICE_GET_FOOD_PROFILE = "get_food_profile"
SERVICE_SET_FOOD_PROFILE = "set_food_profile"
SERVICE_GET_ACCOUNT_SUMMARY = "get_account_summary"
SERVICE_GET_DELIVERY_OPTIONS = "get_delivery_options"
SERVICE_GET_PLANS = "get_plans"
SERVICE_GET_PRESETS = "get_presets"
SERVICE_GET_SPENDING = "get_spending"
SERVICE_ADD_FAVORITE = "add_favorite"
SERVICE_REMOVE_FAVORITE = "remove_favorite"
SERVICE_GET_FAVORITES = "get_favorites"
SERVICE_GET_RECIPE_COLLECTIONS = "get_recipe_collections"
SERVICE_GET_CATALOG_RECIPES = "get_catalog_recipes"
SERVICE_PREVIEW_MEAL_PRICE = "preview_meal_price"
SERVICE_GET_RECIPE_DETAIL = "get_recipe_detail"
SERVICE_GET_DELIVERY_TRACKING = "get_delivery_tracking"

ATTR_WEEK_ID = "week_id"
ATTR_TASTE = "taste"
ATTR_HOUSEHOLD = "household"
ATTR_GOALS = "goals"
ATTR_RECIPE_IDS = "recipe_ids"
ATTR_QUANTITIES = "quantities"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_RECIPE_ID = "recipe_id"
ATTR_COLLECTION = "collection"
ATTR_LIMIT = "limit"
ATTR_SEARCH = "search"
ATTR_SERVINGS = "servings"
ATTR_DELIVERY_OPTION = "delivery_option"
ATTR_DELIVERY_INTERVAL = "delivery_interval"
ATTR_PRODUCT_HANDLE = "product_handle"
ATTR_SUBSCRIPTION_ID = "subscription_id"

INTENT_GET_NEXT_DELIVERY = "HelloFreshGetNextDeliveryIntent"
INTENT_GET_MEAL_SELECTION = "HelloFreshGetMealSelectionIntent"
INTENT_REFRESH = "HelloFreshRefreshDataIntent"

# One entry per market HelloFresh actively operates in. Spain (`hellofresh.es`) and Italy
# (`hellofresh.it`) are deliberately absent: both wound down in early 2026 and their sites now
# redirect `/plans` to `/pages/closure`, so an account there can no longer be served. Japan
# (2022) exited earlier and its domain no longer resolves.
COUNTRY_BASE_URLS: dict[str, str] = {
    "us": "https://www.hellofresh.com",
    "ca": "https://www.hellofresh.ca",
    "uk": "https://www.hellofresh.co.uk",
    "au": "https://www.hellofresh.com.au",
    "nz": "https://www.hellofresh.co.nz",
    "de": "https://www.hellofresh.de",
    "at": "https://www.hellofresh.at",
    "ch": "https://www.hellofresh.ch",
    "nl": "https://www.hellofresh.nl",
    "be": "https://www.hellofresh.be",
    "lu": "https://www.hellofresh.lu",
    "fr": "https://www.hellofresh.fr",
    "ie": "https://www.hellofresh.ie",
    "dk": "https://www.hellofresh.dk",
    "no": "https://www.hellofresh.no",
    "se": "https://www.hellofresh.se",
}

# Countries where HelloFresh runs its own last-mile delivery fleet AND the Tracey live
# tracking stack (hftrack.nl + its cloud function) is confirmed to exist. The live-tracking
# sensors, the get_delivery_tracking service payload, and the tracking card are only
# mounted for these markets — elsewhere the underlying data simply does not exist (third
# party carriers expose only coarse shipment status). Currently the Netherlands only, per
# the community capture in issue #6; Belgium/Luxembourg likely also qualify but are
# unverified, so they stay off until a user there confirms.
TRACEY_COUNTRIES = frozenset({"nl"})

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
# Non-English locales were read from each market's own site rather than assumed, so the
# multilingual markets use what HelloFresh actually serves: Belgium is `nl-BE` (not fr-BE),
# Luxembourg `fr-LU`, Switzerland `de-CH`, and Norway the Bokmål tag `nb-NO` (not `no-NO`).
COUNTRY_API_LOCALES: dict[str, str] = {
    "us": "en-US",
    "ca": "en-CA",
    "uk": "en-GB",
    "au": "en-AU",
    "nz": "en-NZ",
    "de": "de-DE",
    "at": "de-AT",
    "ch": "de-CH",
    "nl": "nl-NL",
    "be": "nl-BE",
    "lu": "fr-LU",
    "fr": "fr-FR",
    "ie": "en-IE",
    "dk": "da-DK",
    "no": "nb-NO",
    "se": "sv-SE",
}


def api_country_code(country: str) -> str:
    """Return the ISO country code HelloFresh's API expects for a config-flow key."""
    return COUNTRY_API_CODES.get(country, country.upper())


def api_locale(country: str) -> str:
    """Return the default API locale for a config-flow key."""
    return COUNTRY_API_LOCALES.get(country, f"en-{api_country_code(country)}")
