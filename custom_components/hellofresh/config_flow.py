"""Config flow for HelloFresh."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow, section
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TimeSelector,
)

if TYPE_CHECKING:
    # From 2026.10 Home Assistant validates with probatio and, as it starts, installs it under
    # the name voluptuous, so `vol` is probatio at runtime and the flow signatures name
    # probatio's Schema. mypy cannot follow that swap: it would read the separate voluptuous
    # package, whose Schema is unrelated to the one async_show_form takes.
    import probatio as vol
else:
    import voluptuous as vol

from .api import (
    HelloFreshAuthError,
    HelloFreshBotBlockedError,
    HelloFreshClient,
    HelloFreshError,
    HelloFreshLoginRejectedError,
    HelloFreshPasswordResetRequiredError,
    HelloFreshRateLimitedError,
)
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ALWAYS_SHOW_BREAKFAST,
    CONF_ALWAYS_SHOW_LUNCH,
    CONF_BREAKFAST_MINUTES,
    CONF_BREAKFAST_START,
    CONF_COUNTRY,
    CONF_DELIVERY_TRACKING_REFRESH_INTERVAL_SECONDS,
    CONF_DELIVERY_WATCH_INTERVAL_MINUTES,
    CONF_DINNER_EXTRA_MINUTES,
    CONF_DINNER_MINUTES,
    CONF_DINNER_START,
    CONF_DINNER_USE_COOKING_TIME,
    CONF_ENABLE_FAVORITES,
    CONF_ENABLE_PREP_LISTS,
    CONF_ENABLE_PUBLIC_MENU_FALLBACK,
    CONF_EXPIRES_IN,
    CONF_HISTORY_WEEKS,
    CONF_ISSUED_AT,
    CONF_LOG_AUTH_DIAGNOSTICS,
    CONF_LUNCH_MINUTES,
    CONF_LUNCH_START,
    CONF_MENU_GRACE_WEEKS,
    CONF_PASSWORD,
    CONF_REFRESH_EXPIRES_IN,
    CONF_REFRESH_TOKEN,
    CONF_REFRESH_TOKEN_ISSUED_AT,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_SHOW_DATA_QUALITY_ISSUES,
    CONF_SHOW_SIDEBAR_PANEL,
    CONF_TOKEN,
    CONF_TOKEN_TYPE,
    CONF_USE_CURL_CFFI_HEADERS,
    CONF_USERNAME,
    COUNTRY_BASE_URLS,
    DEFAULT_BREAKFAST_MINUTES,
    DEFAULT_BREAKFAST_START,
    DEFAULT_COUNTRY,
    DEFAULT_DELIVERY_TRACKING_REFRESH_INTERVAL_SECONDS,
    DEFAULT_DELIVERY_WATCH_INTERVAL_MINUTES,
    DEFAULT_DINNER_EXTRA_MINUTES,
    DEFAULT_DINNER_MINUTES,
    DEFAULT_DINNER_START,
    DEFAULT_ENABLE_FAVORITES,
    DEFAULT_ENABLE_PREP_LISTS,
    DEFAULT_ENABLE_PUBLIC_MENU_FALLBACK,
    DEFAULT_HISTORY_WEEKS,
    DEFAULT_LOG_AUTH_DIAGNOSTICS,
    DEFAULT_LUNCH_MINUTES,
    DEFAULT_LUNCH_START,
    DEFAULT_MENU_GRACE_WEEKS,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DEFAULT_SHOW_DATA_QUALITY_ISSUES,
    DEFAULT_SHOW_SIDEBAR_PANEL,
    DEFAULT_USE_CURL_CFFI_HEADERS,
    DOMAIN,
    MAX_DELIVERY_TRACKING_REFRESH_INTERVAL_SECONDS,
    MAX_DELIVERY_WATCH_INTERVAL_MINUTES,
    MAX_EXTRA_MINUTES,
    MAX_HISTORY_WEEKS,
    MAX_MEAL_MINUTES,
    MAX_MENU_GRACE_WEEKS,
    MAX_SCAN_INTERVAL_MINUTES,
    MIN_DELIVERY_TRACKING_REFRESH_INTERVAL_SECONDS,
    MIN_DELIVERY_WATCH_INTERVAL_MINUTES,
    MIN_HISTORY_WEEKS,
    MIN_MEAL_MINUTES,
    MIN_MENU_GRACE_WEEKS,
    MIN_SCAN_INTERVAL_MINUTES,
    OPTIONS_SECTION_WEEK_TAB,
    TRACEY_COUNTRIES,
)
from .models import HelloFreshNoSubscriptionError
from .parsers import token_payload_to_entry_data


def _account_unique_id(country: str, account: dict[str, object]) -> str:
    """Return the unique id of an entry: its country and its HelloFresh account id."""
    account_id = account.get("account_id") or account.get("subscription_id")
    return f"{country}::{account_id}"


def _account_id_of(unique_id: str | None) -> str | None:
    """Return the account part of an entry's unique id, or None when it has no unique id.

    Entries from before the country was part of the id carry the bare account id.
    """
    return unique_id.rpartition("::")[2] if unique_id else None


def _entry_data_from_credentials(
    username: str,
    password: str,
    country: str,
    tokens: dict[str, object] | None = None,
) -> dict[str, object]:
    """Map validated credentials to persisted config-entry data.

    ``tokens`` is the auth object the validation login just obtained. Storing it lets setup
    start with that token instead of a second /gw/login seconds later, and on reauth it
    replaces the dead tokens that would otherwise be tried once more first. The client keeps
    the token cache current at runtime via the token-refresh callback.
    """
    return {
        CONF_USERNAME: username,
        CONF_PASSWORD: password,
        CONF_COUNTRY: country,
        **(tokens or {}),
    }


class HelloFreshConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for HelloFresh."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._reauth_entry: config_entries.ConfigEntry | None = None
        self._reconfigure_entry: config_entries.ConfigEntry | None = None
        self._selected_country = DEFAULT_COUNTRY
        self._initial_log_auth_diagnostics = DEFAULT_LOG_AUTH_DIAGNOSTICS

    async def async_step_user(self, user_input: dict[str, str] | None = None):
        """Present a menu choosing how to authenticate."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["credentials", "token"],
        )

    async def async_step_credentials(self, user_input: dict[str, Any] | None = None):
        """Recommended path: collect country + HelloFresh credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            country = user_input[CONF_COUNTRY]
            self._selected_country = country
            self._initial_log_auth_diagnostics = user_input.get(
                CONF_LOG_AUTH_DIAGNOSTICS, DEFAULT_LOG_AUTH_DIAGNOSTICS
            )
            result = await self._async_finish_user_auth(
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
                country=country,
                errors=errors,
            )
            if result is not None:
                return result

        return self.async_show_form(
            step_id="credentials",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_COUNTRY, default=DEFAULT_COUNTRY): vol.In(
                        sorted(COUNTRY_BASE_URLS)
                    ),
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Optional(
                        CONF_LOG_AUTH_DIAGNOSTICS,
                        default=self._initial_log_auth_diagnostics,
                    ): cv.boolean,
                }
            ),
            errors=errors,
        )

    async def async_step_token(self, user_input: dict[str, Any] | None = None):
        """Advanced path: provide an apiV2Auth token blob (or bare access token).

        Stores the parsed access/refresh tokens directly; no credentials are kept, so the
        entry works until the refresh token expires (~60 days) or HelloFresh rotates it, at
        which point a reauth prompt is raised. This is the backup for when the bot-protection
        bypass cannot complete a credential login.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            country = user_input[CONF_COUNTRY]
            self._selected_country = country
            self._initial_log_auth_diagnostics = user_input.get(
                CONF_LOG_AUTH_DIAGNOSTICS, DEFAULT_LOG_AUTH_DIAGNOSTICS
            )
            token_data = token_payload_to_entry_data(user_input.get(CONF_TOKEN))
            if token_data is None:
                errors["base"] = "invalid_token"
            else:
                result = await self._async_finish_token_auth(country, token_data, errors)
                if result is not None:
                    return result

        return self.async_show_form(
            step_id="token",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_COUNTRY, default=self._selected_country): vol.In(
                        sorted(COUNTRY_BASE_URLS)
                    ),
                    vol.Required(CONF_TOKEN): str,
                    vol.Optional(
                        CONF_LOG_AUTH_DIAGNOSTICS,
                        default=self._initial_log_auth_diagnostics,
                    ): cv.boolean,
                }
            ),
            description_placeholders={
                # Hassfest forbids literal URLs in translation strings, so the example
                # HelloFresh site URL is injected as a placeholder instead.
                "site_example": COUNTRY_BASE_URLS.get(
                    self._selected_country, COUNTRY_BASE_URLS[DEFAULT_COUNTRY]
                ),
            },
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, str] | None = None):
        """Handle reconfiguration of an existing entry.

        Unlike reauth (which the integration raises when tokens die, and which keeps the
        entry's existing auth method), reconfigure is user-initiated and lets the country be
        corrected as well. Credential entries re-collect username/password; token-only
        entries re-collect a token, mirroring :meth:`async_step_reauth`.
        """
        self._reconfigure_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if self._reconfigure_entry is None:
            return self.async_abort(reason="reconfigure_failed")

        self._selected_country = self._reconfigure_entry.data.get(CONF_COUNTRY, DEFAULT_COUNTRY)
        if not self._reconfigure_entry.data.get(CONF_USERNAME):
            return await self.async_step_reconfigure_token()
        return await self.async_step_reconfigure_confirm()

    async def async_step_reconfigure_confirm(self, user_input: dict[str, str] | None = None):
        """Re-collect country + credentials for an existing credential entry."""
        errors: dict[str, str] = {}
        entry = self._reconfigure_entry
        if entry is None:
            return self.async_abort(reason="reconfigure_failed")

        if user_input is not None:
            country = user_input[CONF_COUNTRY]
            self._selected_country = country
            tokens: dict[str, object] = {}
            account = await self._async_validate(
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
                country,
                errors,
                enable_public_menu_fallback=entry.options.get(
                    CONF_ENABLE_PUBLIC_MENU_FALLBACK,
                    DEFAULT_ENABLE_PUBLIC_MENU_FALLBACK,
                ),
                tokens=tokens,
            )
            if account is not None:
                unique_id = await self._async_guard_same_account(
                    entry, country, account, new_unique_id=True
                )
                # Credentials live in entry.data only; never write them into options.
                return self.async_update_reload_and_abort(
                    entry,
                    # The id names the country too, so a corrected country changes it.
                    unique_id=unique_id,
                    data_updates=_entry_data_from_credentials(
                        user_input[CONF_USERNAME], user_input[CONF_PASSWORD], country, tokens
                    ),
                    title=f"HelloFresh ({country.upper()})",
                    reason="reconfigure_successful",
                )

        return self.async_show_form(
            step_id="reconfigure_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_COUNTRY, default=self._selected_country): vol.In(
                        sorted(COUNTRY_BASE_URLS)
                    ),
                    vol.Required(
                        CONF_USERNAME,
                        default=entry.data.get(CONF_USERNAME, ""),
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure_token(self, user_input: dict[str, str] | None = None):
        """Re-collect country + token for an existing token-only entry."""
        errors: dict[str, str] = {}
        entry = self._reconfigure_entry
        if entry is None:
            return self.async_abort(reason="reconfigure_failed")

        if user_input is not None:
            country = user_input[CONF_COUNTRY]
            self._selected_country = country
            token_data = token_payload_to_entry_data(user_input.get(CONF_TOKEN))
            if token_data is None:
                errors["base"] = "invalid_token"
            else:
                account = await self._async_validate_token(country, token_data, errors)
                if account is not None:
                    unique_id = await self._async_guard_same_account(
                        entry, country, account, new_unique_id=True
                    )
                    # Replace the token fields, as reauth does. Merging kept the old refresh
                    # token when a bare access token was pasted, and dated it as new.
                    return self.async_update_reload_and_abort(
                        entry,
                        unique_id=unique_id,
                        data={CONF_COUNTRY: country, **token_data},
                        title=f"HelloFresh ({country.upper()})",
                        reason="reconfigure_successful",
                    )

        return self.async_show_form(
            step_id="reconfigure_token",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_COUNTRY, default=self._selected_country): vol.In(
                        sorted(COUNTRY_BASE_URLS)
                    ),
                    vol.Required(CONF_TOKEN): str,
                }
            ),
            description_placeholders={
                # Hassfest forbids literal URLs in translation strings, so the example
                # HelloFresh site URL is injected as a placeholder instead.
                "site_example": COUNTRY_BASE_URLS.get(
                    self._selected_country, COUNTRY_BASE_URLS[DEFAULT_COUNTRY]
                ),
            },
            errors=errors,
        )

    async def _async_guard_same_account(
        self,
        entry: config_entries.ConfigEntry,
        country: str,
        account: dict[str, object],
        *,
        new_unique_id: bool = False,
    ) -> str:
        """Abort unless the re-validated login is the entry's own account; return its id.

        Reconfigure and reauth must not silently repoint an entry at a different HelloFresh
        account: the entities, history, and unique ID already on disk belong to the original
        one. A genuinely different account is a new config entry, not an edit to this one.
        Raises HA's ``AbortFlow`` via ``_abort_if_unique_id_mismatch``.

        Only the account part of the id is compared. The id also names the country, so
        comparing all of it made every change of country look like another account. An entry
        with no unique id at all (a very old one) has nothing to compare and is let through.

        ``new_unique_id`` says the caller is about to give the entry the returned id
        (reconfigure, where the country can change), so it must not belong to another entry.
        """
        unique_id = _account_unique_id(country, account)
        known_account = _account_id_of(entry.unique_id)
        same_account = known_account is None or known_account == _account_id_of(unique_id)
        # Home Assistant compares the flow's id with the entry's whole id. The same account
        # is therefore presented under the entry's own id, whatever country it now names.
        await self.async_set_unique_id(
            entry.unique_id if same_account else unique_id, raise_on_progress=False
        )
        self._abort_if_unique_id_mismatch(reason="account_mismatch")
        if new_unique_id and unique_id != entry.unique_id:
            other = self.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, unique_id)
            if other is not None and other.entry_id != entry.entry_id:
                raise AbortFlow("already_configured")
        return unique_id

    async def async_step_reauth(self, entry_data: dict[str, str]):
        """Handle reauth flow start.

        Credential entries re-collect username/password; token-only entries (no stored
        username) re-collect a token, since the user never supplied a password.
        """
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if self._reauth_entry is not None:
            self._selected_country = self._reauth_entry.data.get(CONF_COUNTRY, DEFAULT_COUNTRY)
            if not self._reauth_entry.data.get(CONF_USERNAME):
                return await self.async_step_reauth_token()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, str] | None = None):
        """Collect (or re-collect) credentials for an existing entry."""
        errors: dict[str, str] = {}
        if self._reauth_entry is None:
            # The entry was removed while the form was open.
            return self.async_abort(reason="reauth_failed")

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

    async def async_step_reauth_token(self, user_input: dict[str, str] | None = None):
        """Re-collect a token for a token-only entry whose tokens have expired."""
        errors: dict[str, str] = {}
        if self._reauth_entry is None:
            return self.async_abort(reason="reauth_failed")

        if user_input is not None:
            country = self._selected_country
            token_data = token_payload_to_entry_data(user_input.get(CONF_TOKEN))
            if token_data is None:
                errors["base"] = "invalid_token"
            else:
                account = await self._async_validate_token(country, token_data, errors)
                if account is not None:
                    await self._async_guard_same_account(self._reauth_entry, country, account)
                    # Replace the stale token fields; drop any older timing keys not present
                    # in the new payload so expiry math reflects only the fresh token.
                    return self.async_update_reload_and_abort(
                        self._reauth_entry,
                        data={CONF_COUNTRY: country, **token_data},
                        reason="reauth_successful",
                    )

        return self.async_show_form(
            step_id="reauth_token",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): str}),
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
        tokens: dict[str, object] = {}
        account = await self._async_validate(username, password, country, errors, tokens=tokens)
        if account is None:
            return None

        await self.async_set_unique_id(_account_unique_id(country, account))
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"HelloFresh ({country.upper()})",
            data=_entry_data_from_credentials(username, password, country, tokens),
            options={CONF_LOG_AUTH_DIAGNOSTICS: self._initial_log_auth_diagnostics},
        )

    async def _async_finish_token_auth(
        self,
        country: str,
        token_data: dict[str, object],
        errors: dict[str, str],
    ):
        """Validate a pasted token and create a token-only config entry."""
        account = await self._async_validate_token(country, token_data, errors)
        if account is None:
            return None

        await self.async_set_unique_id(_account_unique_id(country, account))
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"HelloFresh ({country.upper()})",
            data={CONF_COUNTRY: country, **token_data},
            options={CONF_LOG_AUTH_DIAGNOSTICS: self._initial_log_auth_diagnostics},
        )

    async def _async_finish_reauth(
        self,
        username: str,
        password: str,
        errors: dict[str, str],
    ):
        """Validate credentials and update the existing config entry."""
        if self._reauth_entry is None:
            return self.async_abort(reason="reauth_failed")

        country = self._selected_country
        tokens: dict[str, object] = {}
        account = await self._async_validate(
            username,
            password,
            country,
            errors,
            enable_public_menu_fallback=self._reauth_entry.options.get(
                CONF_ENABLE_PUBLIC_MENU_FALLBACK,
                DEFAULT_ENABLE_PUBLIC_MENU_FALLBACK,
            ),
            tokens=tokens,
        )
        if account is None:
            return None

        await self._async_guard_same_account(self._reauth_entry, country, account)
        # Credentials live in entry.data only; never write them into options.
        return self.async_update_reload_and_abort(
            self._reauth_entry,
            data_updates=_entry_data_from_credentials(username, password, country, tokens),
            reason="reauth_successful",
        )

    async def _async_validate(
        self,
        username: str,
        password: str,
        country: str,
        errors: dict[str, str],
        enable_public_menu_fallback: bool = DEFAULT_ENABLE_PUBLIC_MENU_FALLBACK,
        tokens: dict[str, object] | None = None,
    ) -> dict | None:
        """Log in with the supplied credentials and return account info, or None on error.

        The auth object the login obtains is written into ``tokens`` in place, so the caller
        can persist it with the credentials.
        """
        return await self._async_validate_sign_in(
            errors,
            username=username,
            country=country,
            password=password,
            enable_public_menu_fallback=enable_public_menu_fallback,
            token_refresh_callback=tokens.update if tokens is not None else None,
        )

    async def _async_validate_token(
        self,
        country: str,
        token_data: dict[str, object],
        errors: dict[str, str],
    ) -> dict | None:
        """Validate pasted tokens against a real account endpoint, or None on error.

        The validation request itself can rotate the pasted refresh token server-side
        (the client proactively refreshes a token past half its life, and HelloFresh
        invalidates the old refresh token on use). Any rotation is captured back into
        ``token_data`` in place, so callers persist the LIVE token pair — persisting the
        pre-rotation pair stored a dead refresh token and 401'd minutes after setup.
        """
        return await self._async_validate_sign_in(
            errors,
            country=country,
            access_token=token_data.get(CONF_ACCESS_TOKEN),
            refresh_token=token_data.get(CONF_REFRESH_TOKEN),
            # Pass the pasted timing so a genuinely fresh token isn't rotated needlessly.
            token_issued_at=token_data.get(CONF_ISSUED_AT),
            token_expires_in=token_data.get(CONF_EXPIRES_IN),
            refresh_expires_in=token_data.get(CONF_REFRESH_EXPIRES_IN),
            refresh_token_issued_at=token_data.get(CONF_REFRESH_TOKEN_ISSUED_AT),
            token_type=token_data.get(CONF_TOKEN_TYPE),
            token_refresh_callback=token_data.update,
        )

    async def _async_validate_sign_in(
        self, errors: dict[str, str], **client_options: Any
    ) -> dict | None:
        """Read the account with a throwaway client; on failure set ``errors`` and return None.

        Shared by the email-and-password and the token paths: one client lifecycle and one
        mapping from what went wrong to the message shown. Only the credentials path passes
        a ``username``, which picks its wording over the token path's.
        """
        username: str | None = client_options.get("username")
        session = async_create_clientsession(self.hass, auto_cleanup=False)
        client = HelloFreshClient(
            session=session,
            use_curl_cffi_headers=self._existing_header_option(),
            log_auth_diagnostics=self._existing_auth_logging_option(),
            **client_options,
        )
        try:
            return await client.async_validate_credentials()
        except HelloFreshNoSubscriptionError:
            # The sign-in worked; there is just no subscription to show.
            errors["base"] = "no_subscription"
        except HelloFreshAuthError as err:
            errors["base"] = await self._async_auth_error_key(client, err, username)
        except HelloFreshBotBlockedError:
            errors["base"] = "bot_blocked" if username is not None else "bot_blocked_token"
        except HelloFreshRateLimitedError:
            errors["base"] = "too_many_attempts"
        except HelloFreshError:
            errors["base"] = "cannot_connect"
        finally:
            # Closes the curl sessions pooled for this one-off aiohttp session.
            await client.async_close()
            session.detach()
        return None

    @staticmethod
    async def _async_auth_error_key(
        client: HelloFreshClient, err: HelloFreshAuthError, username: str | None
    ) -> str:
        """Pick the message for a sign-in HelloFresh refused."""
        if username is None:
            return "invalid_token"
        if isinstance(err, HelloFreshPasswordResetRequiredError):
            return "password_reset_required"
        if isinstance(err, HelloFreshLoginRejectedError):
            # Tell an unknown email (or the wrong country) from a wrong password when HelloFresh
            # answers; asked only now, so a successful sign-in sends no extra request.
            registered = await client.async_email_registered(username)
            if registered is not None:
                return "wrong_password" if registered else "email_not_registered"
        return "invalid_auth"

    def _existing_header_option(self) -> bool:
        """Use an existing entry's header setting during reauth and reconfigure."""
        entry = getattr(self, "_reconfigure_entry", None) or getattr(self, "_reauth_entry", None)
        if entry is None:
            return DEFAULT_USE_CURL_CFFI_HEADERS
        return entry.options.get(CONF_USE_CURL_CFFI_HEADERS, DEFAULT_USE_CURL_CFFI_HEADERS)

    def _existing_auth_logging_option(self) -> bool:
        """Use the selected diagnostics setting during setup, reauth, or reconfigure."""
        entry = getattr(self, "_reconfigure_entry", None) or getattr(self, "_reauth_entry", None)
        if entry is None:
            return self._initial_log_auth_diagnostics
        return entry.options.get(CONF_LOG_AUTH_DIAGNOSTICS, DEFAULT_LOG_AUTH_DIAGNOSTICS)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow."""
        return HelloFreshOptionsFlow()


class HelloFreshOptionsFlow(config_entries.OptionsFlowWithReload):
    """Handle HelloFresh options.

    ``config_entry`` is provided by Home Assistant on the base class; it must not be set
    here (it is a read-only property on current HA versions). Saving changed options
    reloads the entry so they take effect; saving them unchanged does not.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Manage options.

        Options store ONLY user preferences (refresh interval, past-history window, public
        menu fallback toggle). Credentials and the live token cache live in entry.data and
        are owned by the runtime login/refresh flow. The live delivery-tracking interval is
        offered only where that tracking exists (TRACEY_COUNTRIES).
        """
        tracey_supported = self.config_entry.data.get(CONF_COUNTRY) in TRACEY_COUNTRIES

        if user_input is not None:
            data = {
                CONF_SCAN_INTERVAL_MINUTES: user_input[CONF_SCAN_INTERVAL_MINUTES],
                CONF_ENABLE_PUBLIC_MENU_FALLBACK: user_input[CONF_ENABLE_PUBLIC_MENU_FALLBACK],
                CONF_ENABLE_FAVORITES: user_input[CONF_ENABLE_FAVORITES],
                CONF_ENABLE_PREP_LISTS: user_input[CONF_ENABLE_PREP_LISTS],
                CONF_SHOW_DATA_QUALITY_ISSUES: user_input[CONF_SHOW_DATA_QUALITY_ISSUES],
                CONF_SHOW_SIDEBAR_PANEL: user_input[CONF_SHOW_SIDEBAR_PANEL],
                CONF_USE_CURL_CFFI_HEADERS: user_input.get(
                    CONF_USE_CURL_CFFI_HEADERS, DEFAULT_USE_CURL_CFFI_HEADERS
                ),
                CONF_LOG_AUTH_DIAGNOSTICS: user_input.get(
                    CONF_LOG_AUTH_DIAGNOSTICS, DEFAULT_LOG_AUTH_DIAGNOSTICS
                ),
                # NumberSelector yields a float; store a clean int (whole weeks/days).
                CONF_HISTORY_WEEKS: int(user_input[CONF_HISTORY_WEEKS]),
                CONF_MENU_GRACE_WEEKS: int(user_input[CONF_MENU_GRACE_WEEKS]),
                CONF_DELIVERY_WATCH_INTERVAL_MINUTES: int(
                    user_input[CONF_DELIVERY_WATCH_INTERVAL_MINUTES]
                ),
            }
            data.update(_week_tab_options(user_input.get(OPTIONS_SECTION_WEEK_TAB) or {}))
            if tracey_supported:
                data[CONF_DELIVERY_TRACKING_REFRESH_INTERVAL_SECONDS] = int(
                    user_input.get(
                        CONF_DELIVERY_TRACKING_REFRESH_INTERVAL_SECONDS,
                        DEFAULT_DELIVERY_TRACKING_REFRESH_INTERVAL_SECONDS,
                    )
                )
            return self.async_create_entry(title="", data=data)

        options = self.config_entry.options
        schema: dict[vol.Marker, object] = {
            vol.Required(
                CONF_SCAN_INTERVAL_MINUTES,
                default=options.get(CONF_SCAN_INTERVAL_MINUTES, DEFAULT_SCAN_INTERVAL_MINUTES),
            ): vol.All(
                vol.Coerce(int),
                vol.Range(
                    min=MIN_SCAN_INTERVAL_MINUTES,
                    max=MAX_SCAN_INTERVAL_MINUTES,
                ),
            ),
            vol.Required(
                CONF_DELIVERY_WATCH_INTERVAL_MINUTES,
                default=options.get(
                    CONF_DELIVERY_WATCH_INTERVAL_MINUTES,
                    DEFAULT_DELIVERY_WATCH_INTERVAL_MINUTES,
                ),
            ): NumberSelector(
                # 0 turns the delivery-day watch off; otherwise the light-refresh
                # cadence while a box is due or on the road.
                NumberSelectorConfig(
                    min=MIN_DELIVERY_WATCH_INTERVAL_MINUTES,
                    max=MAX_DELIVERY_WATCH_INTERVAL_MINUTES,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="minutes",
                )
            ),
        }
        if tracey_supported:
            schema[
                vol.Required(
                    CONF_DELIVERY_TRACKING_REFRESH_INTERVAL_SECONDS,
                    default=options.get(
                        CONF_DELIVERY_TRACKING_REFRESH_INTERVAL_SECONDS,
                        DEFAULT_DELIVERY_TRACKING_REFRESH_INTERVAL_SECONDS,
                    ),
                )
            ] = NumberSelector(
                NumberSelectorConfig(
                    min=MIN_DELIVERY_TRACKING_REFRESH_INTERVAL_SECONDS,
                    max=MAX_DELIVERY_TRACKING_REFRESH_INTERVAL_SECONDS,
                    step=30,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="seconds",
                )
            )
        schema.update(
            {
                vol.Required(
                    CONF_HISTORY_WEEKS,
                    default=options.get(CONF_HISTORY_WEEKS, DEFAULT_HISTORY_WEEKS),
                ): NumberSelector(
                    # mode=BOX renders an editable number field (like Refresh interval),
                    # not a slider. step=1 keeps it to whole weeks; min/max still validate.
                    NumberSelectorConfig(
                        min=MIN_HISTORY_WEEKS,
                        max=MAX_HISTORY_WEEKS,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="weeks",
                    )
                ),
                vol.Required(
                    CONF_MENU_GRACE_WEEKS,
                    default=options.get(CONF_MENU_GRACE_WEEKS, DEFAULT_MENU_GRACE_WEEKS),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_MENU_GRACE_WEEKS,
                        max=MAX_MENU_GRACE_WEEKS,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="weeks",
                    )
                ),
                vol.Required(
                    CONF_SHOW_SIDEBAR_PANEL,
                    default=options.get(CONF_SHOW_SIDEBAR_PANEL, DEFAULT_SHOW_SIDEBAR_PANEL),
                ): cv.boolean,
                vol.Required(
                    CONF_ENABLE_PUBLIC_MENU_FALLBACK,
                    default=options.get(
                        CONF_ENABLE_PUBLIC_MENU_FALLBACK,
                        DEFAULT_ENABLE_PUBLIC_MENU_FALLBACK,
                    ),
                ): cv.boolean,
                vol.Required(
                    CONF_ENABLE_FAVORITES,
                    default=options.get(CONF_ENABLE_FAVORITES, DEFAULT_ENABLE_FAVORITES),
                ): cv.boolean,
                vol.Required(
                    CONF_ENABLE_PREP_LISTS,
                    default=options.get(CONF_ENABLE_PREP_LISTS, DEFAULT_ENABLE_PREP_LISTS),
                ): cv.boolean,
                vol.Required(
                    CONF_SHOW_DATA_QUALITY_ISSUES,
                    default=options.get(
                        CONF_SHOW_DATA_QUALITY_ISSUES,
                        DEFAULT_SHOW_DATA_QUALITY_ISSUES,
                    ),
                ): cv.boolean,
                vol.Required(
                    CONF_USE_CURL_CFFI_HEADERS,
                    default=options.get(CONF_USE_CURL_CFFI_HEADERS, DEFAULT_USE_CURL_CFFI_HEADERS),
                ): cv.boolean,
                vol.Required(
                    CONF_LOG_AUTH_DIAGNOSTICS,
                    default=options.get(CONF_LOG_AUTH_DIAGNOSTICS, DEFAULT_LOG_AUTH_DIAGNOSTICS),
                ): cv.boolean,
                # The Week tab's settings, folded away: most people never open it.
                vol.Required(OPTIONS_SECTION_WEEK_TAB): section(
                    vol.Schema(_week_tab_schema(options)), {"collapsed": True}
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))


def _minutes_selector(minimum: int, maximum: int) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=5,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement="minutes",
        )
    )


# The Week tab section's fields: (key, default, selector), in the order the form shows them.
def _week_tab_fields() -> list[tuple[str, object, object]]:
    meal_minutes = _minutes_selector(MIN_MEAL_MINUTES, MAX_MEAL_MINUTES)
    return [
        (CONF_DINNER_START, DEFAULT_DINNER_START, TimeSelector()),
        (CONF_DINNER_MINUTES, DEFAULT_DINNER_MINUTES, meal_minutes),
        (CONF_DINNER_USE_COOKING_TIME, False, cv.boolean),
        (
            CONF_DINNER_EXTRA_MINUTES,
            DEFAULT_DINNER_EXTRA_MINUTES,
            _minutes_selector(0, MAX_EXTRA_MINUTES),
        ),
        (CONF_ALWAYS_SHOW_BREAKFAST, False, cv.boolean),
        (CONF_BREAKFAST_START, DEFAULT_BREAKFAST_START, TimeSelector()),
        (CONF_BREAKFAST_MINUTES, DEFAULT_BREAKFAST_MINUTES, meal_minutes),
        (CONF_ALWAYS_SHOW_LUNCH, False, cv.boolean),
        (CONF_LUNCH_START, DEFAULT_LUNCH_START, TimeSelector()),
        (CONF_LUNCH_MINUTES, DEFAULT_LUNCH_MINUTES, meal_minutes),
    ]


def _week_tab_schema(options: Mapping[str, object]) -> dict[vol.Marker, object]:
    return {
        vol.Required(key, default=options.get(key, default)): selector
        for key, default, selector in _week_tab_fields()
    }


def _week_tab_options(values: Mapping[str, Any]) -> dict[str, object]:
    """The Week tab section's submitted values, as flat options (minutes as whole numbers).

    ``values`` is what the form submitted, already checked by the section's schema: a number
    for each minutes field, so ``int`` can take it.
    """
    out: dict[str, object] = {}
    for key, default, _selector in _week_tab_fields():
        value = values.get(key, default)
        out[key] = (
            int(value) if isinstance(default, int) and not isinstance(default, bool) else value
        )
    return out
