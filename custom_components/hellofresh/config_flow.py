"""Config flow for HelloFresh."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .api import HelloFreshAuthError, HelloFreshClient, HelloFreshError
from .const import (
    CONF_COUNTRY,
    CONF_ENABLE_PUBLIC_MENU_FALLBACK,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_USERNAME,
    COUNTRY_BASE_URLS,
    DEFAULT_COUNTRY,
    DEFAULT_ENABLE_PUBLIC_MENU_FALLBACK,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    MAX_SCAN_INTERVAL_MINUTES,
    MIN_SCAN_INTERVAL_MINUTES,
)


def _entry_data_from_credentials(username: str, password: str, country: str) -> dict[str, object]:
    """Map validated credentials to persisted config-entry data.

    The live token cache (access/refresh tokens, timing) is populated at runtime by the
    client's login/refresh flow and written back via the token-refresh callback; only the
    credentials and country are stored here.
    """
    return {
        CONF_USERNAME: username,
        CONF_PASSWORD: password,
        CONF_COUNTRY: country,
    }


class HelloFreshConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for HelloFresh."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._reauth_entry: config_entries.ConfigEntry | None = None
        self._selected_country = DEFAULT_COUNTRY

    async def async_step_user(self, user_input: dict[str, str] | None = None):
        """Handle the initial step: collect country + HelloFresh credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            country = user_input[CONF_COUNTRY]
            self._selected_country = country
            result = await self._async_finish_user_auth(
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
                country=country,
                errors=errors,
            )
            if result is not None:
                return result

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_COUNTRY, default=DEFAULT_COUNTRY): vol.In(
                        sorted(COUNTRY_BASE_URLS)
                    ),
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, str]):
        """Handle reauth flow start."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if self._reauth_entry is not None:
            self._selected_country = self._reauth_entry.data.get(CONF_COUNTRY, DEFAULT_COUNTRY)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, str] | None = None):
        """Collect (or re-collect) credentials for an existing entry."""
        errors: dict[str, str] = {}
        if self._reauth_entry is None:
            return self.async_abort(reason="invalid_auth")

        if user_input is not None:
            result = await self._async_finish_reauth(
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
                errors=errors,
            )
            if result is not None:
                return result

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME,
                        default=self._reauth_entry.data.get(CONF_USERNAME, ""),
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def _async_finish_user_auth(
        self,
        username: str,
        password: str,
        country: str,
        errors: dict[str, str],
    ):
        """Validate credentials and create a config entry."""
        account = await self._async_validate(username, password, country, errors)
        if account is None:
            return None

        account_id = account.get("account_id") or account.get("subscription_id")
        await self.async_set_unique_id(f"{country}::{account_id}")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"HelloFresh ({country.upper()})",
            data=_entry_data_from_credentials(username, password, country),
        )

    async def _async_finish_reauth(
        self,
        username: str,
        password: str,
        errors: dict[str, str],
    ):
        """Validate credentials and update the existing config entry."""
        if self._reauth_entry is None:
            return self.async_abort(reason="invalid_auth")

        country = self._selected_country
        account = await self._async_validate(
            username,
            password,
            country,
            errors,
            enable_public_menu_fallback=self._reauth_entry.options.get(
                CONF_ENABLE_PUBLIC_MENU_FALLBACK,
                DEFAULT_ENABLE_PUBLIC_MENU_FALLBACK,
            ),
        )
        if account is None:
            return None

        # Credentials live in entry.data only; never write them into options.
        return self.async_update_reload_and_abort(
            self._reauth_entry,
            data_updates=_entry_data_from_credentials(username, password, country),
            reason="reauth_successful",
        )

    async def _async_validate(
        self,
        username: str,
        password: str,
        country: str,
        errors: dict[str, str],
        enable_public_menu_fallback: bool = DEFAULT_ENABLE_PUBLIC_MENU_FALLBACK,
    ) -> dict | None:
        """Log in with the supplied credentials and return account info, or None on error."""
        session = async_get_clientsession(self.hass)
        client = HelloFreshClient(
            session=session,
            country=country,
            username=username,
            password=password,
            enable_public_menu_fallback=enable_public_menu_fallback,
        )
        try:
            return await client.async_validate_credentials()
        except HelloFreshAuthError:
            errors["base"] = "invalid_auth"
            return None
        except HelloFreshError:
            errors["base"] = "cannot_connect"
            return None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow."""
        return HelloFreshOptionsFlow()


class HelloFreshOptionsFlow(config_entries.OptionsFlow):
    """Handle HelloFresh options.

    ``config_entry`` is provided by Home Assistant on the base class; it must not be set
    here (it is a read-only property on current HA versions).
    """

    async def async_step_init(self, user_input: dict[str, str] | None = None):
        """Manage options.

        Options store ONLY user preferences (scan interval, fallback toggle). Credentials
        and the live token cache live in entry.data and are owned by the runtime
        login/refresh flow.
        """
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_SCAN_INTERVAL_MINUTES: user_input[CONF_SCAN_INTERVAL_MINUTES],
                    CONF_ENABLE_PUBLIC_MENU_FALLBACK: user_input[CONF_ENABLE_PUBLIC_MENU_FALLBACK],
                },
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL_MINUTES,
                        default=self.config_entry.options.get(
                            CONF_SCAN_INTERVAL_MINUTES,
                            DEFAULT_SCAN_INTERVAL_MINUTES,
                        ),
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(
                            min=MIN_SCAN_INTERVAL_MINUTES,
                            max=MAX_SCAN_INTERVAL_MINUTES,
                        ),
                    ),
                    vol.Required(
                        CONF_ENABLE_PUBLIC_MENU_FALLBACK,
                        default=self.config_entry.options.get(
                            CONF_ENABLE_PUBLIC_MENU_FALLBACK,
                            DEFAULT_ENABLE_PUBLIC_MENU_FALLBACK,
                        ),
                    ): cv.boolean,
                }
            ),
        )
