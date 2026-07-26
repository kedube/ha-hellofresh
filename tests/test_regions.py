"""Consistency tests for the per-region country maps.

Region support is spread across three separate maps (``COUNTRY_BASE_URLS`` and
``COUNTRY_API_LOCALES`` in const.py, ``_COUNTRY_CURRENCIES`` in normalizers.py). Adding a market
to one and forgetting the others degrades silently -- a missing locale falls back to a wrong
``en-XX`` guess and a missing currency makes prices unit-less -- so these tests pin them together.
"""

from __future__ import annotations

from custom_components.hellofresh.const import (
    COUNTRY_API_CODES,
    COUNTRY_API_LOCALES,
    COUNTRY_BASE_URLS,
    DEFAULT_COUNTRY,
    api_country_code,
    api_locale,
)
from custom_components.hellofresh.normalizers import _COUNTRY_CURRENCIES

# Markets HelloFresh has exited; these must never be offered in the config flow.
EXITED_MARKETS = ("es", "it", "jp")


def test_every_region_has_a_locale_and_currency() -> None:
    """All three region maps cover exactly the same set of country keys."""
    regions = set(COUNTRY_BASE_URLS)
    assert regions == set(COUNTRY_API_LOCALES), "locale map out of sync with base URLs"
    assert regions == set(_COUNTRY_CURRENCIES), "currency map out of sync with base URLs"


def test_exited_markets_are_not_offered() -> None:
    """Spain, Italy and Japan wound down, so an account there can't be served."""
    for market in EXITED_MARKETS:
        assert market not in COUNTRY_BASE_URLS


def test_default_country_is_supported() -> None:
    assert DEFAULT_COUNTRY in COUNTRY_BASE_URLS


def test_base_urls_are_https_and_unique() -> None:
    urls = list(COUNTRY_BASE_URLS.values())
    assert len(urls) == len(set(urls)), "two regions share a base URL"
    for url in urls:
        assert url.startswith("https://www.hellofresh."), url
        assert not url.endswith("/"), f"trailing slash breaks path joins: {url}"


def test_currencies_are_iso_4217_codes() -> None:
    for country, currency in _COUNTRY_CURRENCIES.items():
        assert len(currency) == 3 and currency.isupper(), f"{country}: {currency}"


def test_locales_match_their_api_country_code() -> None:
    """A locale's region subtag must agree with the ISO code sent to the API.

    This is what caught the original ``uk``/``GB`` bug: the config key is ``uk`` but the API and
    the locale both use ``GB``, so a naive ``en-{key}`` would produce an invalid ``en-UK``.
    """
    for country in COUNTRY_BASE_URLS:
        language, _, region = api_locale(country).partition("-")
        assert language.isalpha() and len(language) == 2
        assert region == api_country_code(country), f"{country}: locale region != API country"


def test_uk_maps_to_gb() -> None:
    """The one config key that is not its own ISO code."""
    assert api_country_code("uk") == "GB"
    assert api_locale("uk") == "en-GB"


def test_api_code_falls_back_to_uppercased_key() -> None:
    """Unlisted keys upper-case as-is, so only genuine exceptions need an entry."""
    assert api_country_code("fr") == "FR"
    assert set(COUNTRY_API_CODES) == {"uk"}


def test_multilingual_markets_use_their_native_locale() -> None:
    """Markets where the obvious English/majority-language guess would be wrong.

    Each value was read from the market's own site rather than assumed.
    """
    assert api_locale("be") == "nl-BE"
    assert api_locale("lu") == "fr-LU"
    assert api_locale("ch") == "de-CH"
    assert api_locale("no") == "nb-NO"  # Bokmal, not the "no-NO" a naive guess gives


def test_eurozone_and_local_currencies() -> None:
    """The Nordics share a "kr" symbol but bill in three distinct currencies."""
    assert _COUNTRY_CURRENCIES["dk"] == "DKK"
    assert _COUNTRY_CURRENCIES["no"] == "NOK"
    assert _COUNTRY_CURRENCIES["se"] == "SEK"
    assert _COUNTRY_CURRENCIES["ch"] == "CHF"  # not EUR
    assert _COUNTRY_CURRENCIES["ie"] == "EUR"  # not GBP
    for country in ("de", "at", "nl", "be", "lu", "fr"):
        assert _COUNTRY_CURRENCIES[country] == "EUR"
