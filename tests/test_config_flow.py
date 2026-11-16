"""Unit tests for the HelloFresh config flow's credential vs token paths.

These drive the flow object directly (stubbing the HA flow-manager helpers and the network
validation) to stay consistent with the suite's lightweight, hass-free style.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
from pathlib import Path
import re
import time
from types import SimpleNamespace
from urllib.parse import quote

from homeassistant.data_entry_flow import AbortFlow
import pytest

from custom_components.hellofresh.config_flow import HelloFreshConfigFlow, HelloFreshOptionsFlow
from custom_components.hellofresh.const import (
    CONF_ACCESS_TOKEN,
    CONF_BREAKFAST_MINUTES,
    CONF_COUNTRY,
    CONF_DELIVERY_TRACKING_REFRESH_INTERVAL_SECONDS,
    CONF_DELIVERY_WATCH_INTERVAL_MINUTES,
    CONF_DINNER_MINUTES,
    CONF_DINNER_START,
    CONF_ENABLE_FAVORITES,
    CONF_ENABLE_PREP_LISTS,
    CONF_ENABLE_PUBLIC_MENU_FALLBACK,
    CONF_HISTORY_WEEKS,
    CONF_LOG_AUTH_DIAGNOSTICS,
    CONF_MENU_GRACE_WEEKS,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_SHOW_DATA_QUALITY_ISSUES,
    CONF_SHOW_SIDEBAR_PANEL,
    CONF_TOKEN,
    CONF_USE_CURL_CFFI_HEADERS,
    CONF_USERNAME,
    DEFAULT_DELIVERY_TRACKING_REFRESH_INTERVAL_SECONDS,
    DEFAULT_DELIVERY_WATCH_INTERVAL_MINUTES,
    DEFAULT_ENABLE_FAVORITES,
    DEFAULT_ENABLE_PREP_LISTS,
    DEFAULT_ENABLE_PUBLIC_MENU_FALLBACK,
    DEFAULT_HISTORY_WEEKS,
    DEFAULT_LOG_AUTH_DIAGNOSTICS,
    DEFAULT_MENU_GRACE_WEEKS,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DEFAULT_SHOW_DATA_QUALITY_ISSUES,
    DEFAULT_SHOW_SIDEBAR_PANEL,
    DEFAULT_USE_CURL_CFFI_HEADERS,
    OPTIONS_SECTION_WEEK_TAB,
)
from custom_components.hellofresh.tls_transport import AuthResponse


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def _make_flow() -> HelloFreshConfigFlow:
    """Build a flow with the HA flow-manager helpers stubbed to record calls."""
    flow = HelloFreshConfigFlow()
    flow.hass = SimpleNamespace()  # type: ignore[assignment]
    flow.context = {"source": "user"}  # type: ignore[assignment]
    created: dict = {}

    def async_create_entry(*, title, data, options=None, **_kwargs):
        created.update({"title": title, "data": data, "options": options})
        return {"type": "create_entry", "title": title, "data": data, "options": options}

    def async_show_form(*, step_id, errors=None, data_schema=None, **_kwargs):
        result = {"type": "form", "step_id": step_id, "errors": errors or {}}
        if data_schema is not None:
            result["data_schema"] = data_schema
        return result

    def async_show_menu(*, step_id, menu_options, **_kwargs):
        return {"type": "menu", "step_id": step_id, "menu_options": menu_options}

    async def async_set_unique_id(uid, **_kwargs):
        # Where Home Assistant keeps it, so its own unique-id checks read what was set.
        flow.context["unique_id"] = uid  # type: ignore[typeddict-unknown-key]
        return None

    flow.async_create_entry = async_create_entry  # type: ignore[method-assign]
    flow.async_show_form = async_show_form  # type: ignore[method-assign]
    flow.async_show_menu = async_show_menu  # type: ignore[method-assign]
    flow.async_set_unique_id = async_set_unique_id  # type: ignore[method-assign]
    flow._abort_if_unique_id_configured = lambda: None  # type: ignore[method-assign]
    flow._created = created  # type: ignore[attr-defined]
    return flow


def _make_options_flow(country: str, options: dict | None = None) -> HelloFreshOptionsFlow:
    """Build an options flow with a stub config entry."""
    flow = HelloFreshOptionsFlow()
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={CONF_COUNTRY: country},
        options=options or {},
    )
    flow.hass = SimpleNamespace(  # type: ignore[assignment]
        config_entries=SimpleNamespace(async_get_known_entry=lambda _id: entry)
    )
    flow.handler = entry.entry_id

    def async_create_entry(*, title, data, **_kwargs):
        return {"type": "create_entry", "title": title, "data": data}

    def async_show_form(*, step_id, errors=None, data_schema=None, **_kwargs):
        return {
            "type": "form",
            "step_id": step_id,
            "errors": errors or {},
            "data_schema": data_schema,
        }

    flow.async_create_entry = async_create_entry  # type: ignore[method-assign]
    flow.async_show_form = async_show_form  # type: ignore[method-assign]
    return flow


def _schema_keys(schema) -> set[str]:
    """Return the option names exposed by a voluptuous schema."""
    return {key.schema for key in schema.schema}


def _schema_default(schema, field: str):
    """Return a voluptuous field's default value."""
    for key in schema.schema:
        if key.schema == field:
            return key.default()
    raise AssertionError(f"{field} not found in schema")


def _default_options_payload() -> dict:
    """A complete options submission for tests."""
    return {
        CONF_SCAN_INTERVAL_MINUTES: DEFAULT_SCAN_INTERVAL_MINUTES,
        CONF_DELIVERY_WATCH_INTERVAL_MINUTES: DEFAULT_DELIVERY_WATCH_INTERVAL_MINUTES,
        CONF_HISTORY_WEEKS: DEFAULT_HISTORY_WEEKS,
        CONF_MENU_GRACE_WEEKS: DEFAULT_MENU_GRACE_WEEKS,
        CONF_ENABLE_PUBLIC_MENU_FALLBACK: DEFAULT_ENABLE_PUBLIC_MENU_FALLBACK,
        CONF_ENABLE_FAVORITES: DEFAULT_ENABLE_FAVORITES,
        CONF_ENABLE_PREP_LISTS: DEFAULT_ENABLE_PREP_LISTS,
        CONF_SHOW_DATA_QUALITY_ISSUES: DEFAULT_SHOW_DATA_QUALITY_ISSUES,
        CONF_SHOW_SIDEBAR_PANEL: DEFAULT_SHOW_SIDEBAR_PANEL,
        CONF_USE_CURL_CFFI_HEADERS: DEFAULT_USE_CURL_CFFI_HEADERS,
        CONF_LOG_AUTH_DIAGNOSTICS: DEFAULT_LOG_AUTH_DIAGNOSTICS,
    }


def test_user_step_shows_menu_of_both_paths() -> None:
    """The initial step offers the credential and token paths."""
    flow = _make_flow()
    result = _run(flow.async_step_user())
    assert result["type"] == "menu"
    assert result["menu_options"] == ["credentials", "token"]


def test_options_hide_delivery_tracking_interval_outside_netherlands() -> None:
    """The Tracey endpoint option is not exposed for unsupported countries."""
    flow = _make_options_flow("us")
    result = _run(flow.async_step_init())
    assert CONF_DELIVERY_TRACKING_REFRESH_INTERVAL_SECONDS not in _schema_keys(
        result["data_schema"]
    )


def test_options_can_select_curl_cffi_browser_headers() -> None:
    """The header source is a per-entry option, defaulting to integration XHR headers."""
    flow = _make_options_flow("us")
    form = _run(flow.async_step_init())
    assert _schema_default(form["data_schema"], CONF_USE_CURL_CFFI_HEADERS) is False

    result = _run(
        flow.async_step_init({**_default_options_payload(), CONF_USE_CURL_CFFI_HEADERS: True})
    )
    assert result["data"][CONF_USE_CURL_CFFI_HEADERS] is True

    saved = _make_options_flow("us", result["data"])
    assert (
        _schema_default(_run(saved.async_step_init())["data_schema"], CONF_USE_CURL_CFFI_HEADERS)
        is True
    )


def test_options_can_disable_auth_diagnostics() -> None:
    """Auth response diagnostics default on and can be disabled per entry."""
    flow = _make_options_flow("us")
    form = _run(flow.async_step_init())
    assert _schema_default(form["data_schema"], CONF_LOG_AUTH_DIAGNOSTICS) is True

    result = _run(
        flow.async_step_init({**_default_options_payload(), CONF_LOG_AUTH_DIAGNOSTICS: False})
    )
    assert result["data"][CONF_LOG_AUTH_DIAGNOSTICS] is False


def _week_tab_section(schema):
    """The options form's collapsible Week tab section."""
    for key, value in schema.schema.items():
        if key.schema == OPTIONS_SECTION_WEEK_TAB:
            return value
    raise AssertionError("no Week tab section")


def test_options_week_tab_settings_are_folded_away_and_saved_flat() -> None:
    """The Week tab's settings sit in one collapsed section and are stored flat."""
    flow = _make_options_flow("us")
    section = _week_tab_section(_run(flow.async_step_init())["data_schema"])
    assert section.options["collapsed"] is True
    assert _schema_default(section.schema, CONF_DINNER_START) == "17:30:00"
    assert _schema_default(section.schema, CONF_DINNER_MINUTES) == 90

    result = _run(
        flow.async_step_init(
            {
                **_default_options_payload(),
                OPTIONS_SECTION_WEEK_TAB: {
                    CONF_DINNER_START: "18:00:00",
                    CONF_DINNER_MINUTES: 75.0,  # NumberSelector hands back a float
                },
            }
        )
    )
    data = result["data"]
    assert OPTIONS_SECTION_WEEK_TAB not in data
    assert data[CONF_DINNER_START] == "18:00:00"
    assert data[CONF_DINNER_MINUTES] == 75 and isinstance(data[CONF_DINNER_MINUTES], int)
    assert data[CONF_BREAKFAST_MINUTES] == 30  # a field left alone keeps its default

    saved = _make_options_flow("us", data)
    section = _week_tab_section(_run(saved.async_step_init())["data_schema"])
    assert _schema_default(section.schema, CONF_DINNER_START) == "18:00:00"


def test_options_show_delivery_tracking_interval_for_netherlands() -> None:
    """Netherlands entries can tune the Tracey endpoint poll cadence in seconds."""
    flow = _make_options_flow("nl")
    result = _run(flow.async_step_init())
    assert CONF_DELIVERY_TRACKING_REFRESH_INTERVAL_SECONDS in _schema_keys(result["data_schema"])
    assert (
        _schema_default(result["data_schema"], CONF_DELIVERY_TRACKING_REFRESH_INTERVAL_SECONDS)
        == DEFAULT_DELIVERY_TRACKING_REFRESH_INTERVAL_SECONDS
    )


def test_options_show_delivery_tracking_interval_for_germany() -> None:
    """German entries get the same live-tracking cadence option (issue #12)."""
    flow = _make_options_flow("de")
    result = _run(flow.async_step_init())
    assert CONF_DELIVERY_TRACKING_REFRESH_INTERVAL_SECONDS in _schema_keys(result["data_schema"])


def test_options_persist_delivery_tracking_interval_only_for_netherlands() -> None:
    """Crafted submissions for other countries cannot store the NL-only option."""
    nl_flow = _make_options_flow("nl")
    payload = {
        **_default_options_payload(),
        CONF_DELIVERY_TRACKING_REFRESH_INTERVAL_SECONDS: 120,
    }
    nl_result = _run(nl_flow.async_step_init(payload))
    assert nl_result["data"][CONF_DELIVERY_TRACKING_REFRESH_INTERVAL_SECONDS] == 120

    us_flow = _make_options_flow("us")
    us_result = _run(us_flow.async_step_init(payload))
    assert CONF_DELIVERY_TRACKING_REFRESH_INTERVAL_SECONDS not in us_result["data"]


def test_credentials_path_creates_entry_with_credentials_only() -> None:
    """The credentials step stores username/password/country, no tokens."""
    flow = _make_flow()

    async def fake_validate(username, password, country, errors, **_kwargs):
        return {"account_id": "acct-1"}

    flow._async_validate = fake_validate  # type: ignore[method-assign]

    result = _run(
        flow.async_step_credentials(
            {CONF_COUNTRY: "us", CONF_USERNAME: "u@example.com", CONF_PASSWORD: "pw"}
        )
    )
    assert result["type"] == "create_entry"
    data = result["data"]
    assert data == {CONF_USERNAME: "u@example.com", CONF_PASSWORD: "pw", CONF_COUNTRY: "us"}
    assert CONF_ACCESS_TOKEN not in data
    assert result["options"] == {CONF_LOG_AUTH_DIAGNOSTICS: True}


def test_credentials_setup_defaults_diagnostics_on_before_validation() -> None:
    """An initial sign-in block is diagnosed before an entry exists."""
    flow = _make_flow()
    assert (
        _schema_default(
            _run(flow.async_step_credentials())["data_schema"], CONF_LOG_AUTH_DIAGNOSTICS
        )
        is True
    )

    async def fake_validate(username, password, country, errors, **_kwargs):
        assert flow._existing_auth_logging_option() is True
        return {"account_id": "acct-1"}

    flow._async_validate = fake_validate  # type: ignore[method-assign]
    result = _run(
        flow.async_step_credentials(
            {
                CONF_COUNTRY: "us",
                CONF_USERNAME: "u@example.com",
                CONF_PASSWORD: "pw",
            }
        )
    )
    assert result["options"] == {CONF_LOG_AUTH_DIAGNOSTICS: True}


def test_credentials_bot_block_gets_its_own_error(monkeypatch) -> None:
    """A Cloudflare block on login says so (and where to go next), not "could not connect"."""
    from custom_components.hellofresh import config_flow  # noqa: PLC0415
    from custom_components.hellofresh.api import (  # noqa: PLC0415
        HelloFreshBotBlockedError,
        HelloFreshError,
    )

    raised: list[Exception] = []

    class _Client:
        def __init__(self, **_kwargs) -> None:
            pass

        async def async_validate_credentials(self):
            raise raised[-1]

        async def async_close(self) -> None:
            pass

    monkeypatch.setattr(config_flow, "HelloFreshClient", _Client)
    monkeypatch.setattr(
        config_flow,
        "async_create_clientsession",
        lambda _hass, **_kwargs: type("Session", (), {"detach": lambda self: None})(),
    )
    flow = _make_flow()
    user_input = {CONF_COUNTRY: "be", CONF_USERNAME: "u@example.com", CONF_PASSWORD: "pw"}

    raised.append(HelloFreshBotBlockedError("HelloFresh login blocked by bot protection: HTTP 403"))
    result = _run(flow.async_step_credentials(user_input))
    assert result["type"] == "form"
    assert result["errors"] == {"base": "bot_blocked"}

    result = _run(flow.async_step_credentials({**user_input, CONF_LOG_AUTH_DIAGNOSTICS: False}))
    assert result["errors"] == {"base": "bot_blocked"}
    assert _schema_default(result["data_schema"], CONF_LOG_AUTH_DIAGNOSTICS) is False

    raised.append(HelloFreshError("timeout"))
    result = _run(flow.async_step_credentials(user_input))
    assert result["errors"] == {"base": "cannot_connect"}


_LOGIN_TOKENS = {
    "access_token": "access-1",
    "refresh_token": "refresh-1",
    "issued_at": 1_790_000_000,
    "expires_in": 1800,
    "refresh_expires_in": 5184000,
    "refresh_token_issued_at": 1_790_000_000,
    "token_type": "Bearer",
}


def _stub_login_client(
    monkeypatch,
    *,
    raises: Exception | None = None,
    registered: bool | None = None,
    account_id: str = "acct-1",
    lifecycle: list[str] | None = None,
) -> list[str]:
    """Replace the flow's client with one whose validation login reports ``_LOGIN_TOKENS``.

    Returns the emails the flow asked ``async_email_registered`` about (answered ``registered``).
    ``lifecycle`` collects, in order, what the flow did with the client and its session.
    """
    from custom_components.hellofresh import config_flow  # noqa: PLC0415

    email_checks: list[str] = []
    events = lifecycle if lifecycle is not None else []

    class _Client:
        def __init__(self, *, token_refresh_callback=None, **_kwargs) -> None:
            self._token_refresh_callback = token_refresh_callback

        async def async_validate_credentials(self):
            events.append("validate")
            if raises is not None:
                raise raises
            if self._token_refresh_callback is not None:
                self._token_refresh_callback(dict(_LOGIN_TOKENS))
            return {"account_id": account_id}

        async def async_email_registered(self, email):
            events.append("email_check")
            email_checks.append(email)
            return registered

        async def async_close(self) -> None:
            events.append("client_closed")

    class _Session:
        def detach(self) -> None:
            events.append("session_detached")

    monkeypatch.setattr(config_flow, "HelloFreshClient", _Client)
    monkeypatch.setattr(config_flow, "async_create_clientsession", lambda _hass, **_kw: _Session())
    return email_checks


@pytest.mark.parametrize(
    ("registered", "error"),
    [(False, "email_not_registered"), (True, "wrong_password"), (None, "invalid_auth")],
)
def test_rejected_login_says_whether_the_email_or_the_password_is_wrong(
    monkeypatch, registered, error
) -> None:
    """After a rejected login, HelloFresh's email check picks the message; no answer, generic."""
    from custom_components.hellofresh.api import HelloFreshLoginRejectedError  # noqa: PLC0415

    checks = _stub_login_client(
        monkeypatch,
        raises=HelloFreshLoginRejectedError("HelloFresh login failed: HTTP 401"),
        registered=registered,
    )
    flow = _make_flow()

    result = _run(
        flow.async_step_credentials(
            {CONF_COUNTRY: "be", CONF_USERNAME: "u@example.com", CONF_PASSWORD: "pw"}
        )
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": error}
    assert checks == ["u@example.com"]


def test_other_auth_failures_skip_the_email_check(monkeypatch) -> None:
    """Only a rejected email/password is explained; other auth failures stay generic."""
    from custom_components.hellofresh.api import HelloFreshAuthError  # noqa: PLC0415

    checks = _stub_login_client(
        monkeypatch, raises=HelloFreshAuthError("login response did not include an access token")
    )
    flow = _make_flow()

    result = _run(
        flow.async_step_credentials(
            {CONF_COUNTRY: "us", CONF_USERNAME: "u@example.com", CONF_PASSWORD: "pw"}
        )
    )

    assert result["errors"] == {"base": "invalid_auth"}
    assert checks == []


@pytest.mark.parametrize(
    ("exception", "error"),
    [
        ("HelloFreshPasswordResetRequiredError", "password_reset_required"),
        ("HelloFreshRateLimitedError", "too_many_attempts"),
    ],
)
def test_password_reset_and_lockout_get_their_own_messages(monkeypatch, exception, error) -> None:
    """A required password reset or a sign-in lockout is explained, with no email check."""
    from custom_components.hellofresh import api  # noqa: PLC0415

    checks = _stub_login_client(monkeypatch, raises=getattr(api, exception)("HTTP 4xx"))
    flow = _make_flow()

    result = _run(
        flow.async_step_credentials(
            {CONF_COUNTRY: "us", CONF_USERNAME: "u@example.com", CONF_PASSWORD: "pw"}
        )
    )

    assert result["errors"] == {"base": error}
    assert checks == []


def test_reauth_explains_a_rejected_login_too(monkeypatch) -> None:
    """The reauth form runs the same check: an unregistered email is named as such."""
    from custom_components.hellofresh.api import HelloFreshLoginRejectedError  # noqa: PLC0415

    checks = _stub_login_client(
        monkeypatch, raises=HelloFreshLoginRejectedError("HTTP 401"), registered=False
    )
    flow = _make_reconfigure_flow(
        {CONF_COUNTRY: "uk", CONF_USERNAME: "old@example.com"}, source="reauth"
    )

    _run(flow.async_step_reauth({}))
    result = _run(
        flow.async_step_reauth_confirm({CONF_USERNAME: "new@example.com", CONF_PASSWORD: "pw"})
    )

    assert result["errors"] == {"base": "email_not_registered"}
    assert checks == ["new@example.com"]
    assert flow._updated == {}


def test_credentials_entry_keeps_the_validation_login_tokens(monkeypatch) -> None:
    """Setup starts from the token the validation login obtained, not a second login."""
    _stub_login_client(monkeypatch)
    flow = _make_flow()

    result = _run(
        flow.async_step_credentials(
            {CONF_COUNTRY: "us", CONF_USERNAME: "u@example.com", CONF_PASSWORD: "pw"}
        )
    )

    assert result["type"] == "create_entry"
    assert result["data"] == {
        CONF_USERNAME: "u@example.com",
        CONF_PASSWORD: "pw",
        CONF_COUNTRY: "us",
        **_LOGIN_TOKENS,
    }


def test_reauth_replaces_dead_tokens_with_the_validation_login_tokens(monkeypatch) -> None:
    """A password reauth stores the fresh tokens, so the reload doesn't retry the dead ones."""
    _stub_login_client(monkeypatch)
    flow = _make_reconfigure_flow(
        {
            CONF_COUNTRY: "us",
            CONF_USERNAME: "u@example.com",
            CONF_ACCESS_TOKEN: "dead-access",
            CONF_REFRESH_TOKEN: "dead-refresh",
        },
        source="reauth",
    )

    _run(flow.async_step_reauth({}))
    result = _run(
        flow.async_step_reauth_confirm({CONF_USERNAME: "u@example.com", CONF_PASSWORD: "pw"})
    )

    assert result["reason"] == "reauth_successful"
    assert flow._updated["data_updates"][CONF_ACCESS_TOKEN] == "access-1"
    assert flow._updated["data_updates"][CONF_REFRESH_TOKEN] == "refresh-1"


def test_token_path_bot_block_gets_a_token_specific_error(monkeypatch) -> None:
    """A Cloudflare block while checking a pasted token is not reported as "could not connect"."""
    from custom_components.hellofresh.api import HelloFreshBotBlockedError  # noqa: PLC0415

    _stub_login_client(monkeypatch, raises=HelloFreshBotBlockedError("blocked: HTTP 403"))
    flow = _make_flow()

    result = _run(flow.async_step_token({CONF_COUNTRY: "be", CONF_TOKEN: "just-a-token"}))

    assert result["type"] == "form"
    assert result["errors"] == {"base": "bot_blocked_token"}


def test_token_path_parses_blob_and_stores_tokens_without_credentials() -> None:
    """The token step parses an apiV2Auth blob, validates, and stores tokens only."""
    flow = _make_flow()
    assert _schema_default(_run(flow.async_step_token())["data_schema"], CONF_LOG_AUTH_DIAGNOSTICS)

    captured: dict = {}

    async def fake_validate_token(country, token_data, errors):
        captured["country"] = country
        captured["token_data"] = token_data
        return {"account_id": "acct-9"}

    flow._async_validate_token = fake_validate_token  # type: ignore[method-assign]

    blob = json.dumps(
        {
            "access_token": "a.b.c",
            "refresh_token": "refresh-xyz",
            "expires_in": 1800,
            "refresh_expires_in": 5184000,
            "token_type": "Bearer",
        }
    )
    result = _run(flow.async_step_token({CONF_COUNTRY: "uk", CONF_TOKEN: quote(blob)}))

    assert result["type"] == "create_entry"
    data = result["data"]
    assert data[CONF_COUNTRY] == "uk"
    assert data[CONF_ACCESS_TOKEN] == "a.b.c"
    assert data[CONF_REFRESH_TOKEN] == "refresh-xyz"
    # No credentials are persisted for the token path.
    assert CONF_USERNAME not in data
    assert CONF_PASSWORD not in data
    # The validated token_data is what was stored.
    assert captured["country"] == "uk"
    assert result["options"] == {CONF_LOG_AUTH_DIAGNOSTICS: True}


def test_token_path_accepts_bare_access_token() -> None:
    """A non-JSON paste is treated as a bare access token."""
    flow = _make_flow()

    async def fake_validate_token(country, token_data, errors):
        return {"account_id": "acct-2"}

    flow._async_validate_token = fake_validate_token  # type: ignore[method-assign]

    result = _run(flow.async_step_token({CONF_COUNTRY: "us", CONF_TOKEN: "just-a-token"}))
    assert result["type"] == "create_entry"
    assert result["data"][CONF_ACCESS_TOKEN] == "just-a-token"
    assert CONF_REFRESH_TOKEN not in result["data"]


def test_token_path_rejects_unparseable_input() -> None:
    """Blank/garbage token input re-shows the form with an invalid_token error."""
    flow = _make_flow()

    called = False

    async def fake_validate_token(country, token_data, errors):
        nonlocal called
        called = True
        return {"account_id": "x"}

    flow._async_validate_token = fake_validate_token  # type: ignore[method-assign]

    result = _run(flow.async_step_token({CONF_COUNTRY: "us", CONF_TOKEN: "   "}))
    assert result["type"] == "form"
    assert result["step_id"] == "token"
    assert result["errors"]["base"] == "invalid_token"
    assert called is False  # never reached validation


def test_reauth_routes_token_entry_to_token_step() -> None:
    """A token-only entry (no username) reauths into the token step, not credentials."""
    flow = _make_flow()
    entry = SimpleNamespace(data={CONF_COUNTRY: "uk", CONF_ACCESS_TOKEN: "a.b.c"})
    flow.hass = SimpleNamespace(  # type: ignore[assignment]
        config_entries=SimpleNamespace(async_get_entry=lambda _id: entry)
    )
    flow.context = {"entry_id": "e1"}  # type: ignore[attr-defined]

    result = _run(flow.async_step_reauth({}))
    assert result["type"] == "form"
    assert result["step_id"] == "reauth_token"


def test_reauth_routes_credential_entry_to_confirm_step() -> None:
    """A credential entry reauths into the username/password confirm step."""
    flow = _make_flow()
    entry = SimpleNamespace(data={CONF_COUNTRY: "us", CONF_USERNAME: "u@example.com"})
    flow.hass = SimpleNamespace(  # type: ignore[assignment]
        config_entries=SimpleNamespace(async_get_entry=lambda _id: entry)
    )
    flow.context = {"entry_id": "e1"}  # type: ignore[attr-defined]

    result = _run(flow.async_step_reauth({}))
    assert result["type"] == "form"
    assert result["step_id"] == "reauth_confirm"


def _make_reconfigure_flow(
    entry_data: dict,
    *,
    source: str = "reconfigure",
    unique_id: str | None = "us::acct-1",
    other_entries: dict[str, str] | None = None,
):
    """Build a reconfigure (or reauth) flow positioned on an entry holding ``entry_data``.

    Home Assistant's own ``_abort_if_unique_id_mismatch`` runs for real: it reads the flow's
    source and the entry from the context, so both are set as Home Assistant sets them.
    ``other_entries`` maps the unique ids of other configured entries to their entry ids.
    """
    flow = _make_flow()
    entry = SimpleNamespace(entry_id="e1", unique_id=unique_id, data=entry_data, options={})
    others = {
        uid: SimpleNamespace(entry_id=entry_id, unique_id=uid)
        for uid, entry_id in (other_entries or {}).items()
    }
    flow.hass = SimpleNamespace(  # type: ignore[assignment]
        config_entries=SimpleNamespace(
            async_get_entry=lambda _id: entry,
            async_get_known_entry=lambda _id: entry,
            async_entry_for_domain_unique_id=lambda _domain, uid: (
                entry if uid == entry.unique_id else others.get(uid)
            ),
        )
    )
    flow.context = {"source": source, "entry_id": "e1"}  # type: ignore[assignment]

    updated: dict = {}

    def async_update_reload_and_abort(target, *, reason, **kwargs):
        updated.update({"entry": target, "reason": reason, **kwargs})
        return {"type": "abort", "reason": reason, **kwargs}

    flow.async_update_reload_and_abort = async_update_reload_and_abort  # type: ignore[method-assign]
    flow._updated = updated  # type: ignore[attr-defined]
    flow._entry = entry  # type: ignore[attr-defined]
    return flow


def test_reconfigure_routes_token_entry_to_token_step() -> None:
    """A token-only entry reconfigures into the token step, not credentials."""
    flow = _make_reconfigure_flow({CONF_COUNTRY: "uk", CONF_ACCESS_TOKEN: "a.b.c"})
    result = _run(flow.async_step_reconfigure())
    assert result["type"] == "form"
    assert result["step_id"] == "reconfigure_token"


def test_reconfigure_routes_credential_entry_to_confirm_step() -> None:
    """A credential entry reconfigures into the username/password step."""
    flow = _make_reconfigure_flow({CONF_COUNTRY: "us", CONF_USERNAME: "u@example.com"})
    result = _run(flow.async_step_reconfigure())
    assert result["type"] == "form"
    assert result["step_id"] == "reconfigure_confirm"


def test_reconfigure_updates_entry_and_allows_country_change() -> None:
    """Reconfiguring rewrites credentials and the country, then reloads the entry.

    The unique id names the country, so Home Assistant's own mismatch check (run for real
    here) refused every change of country as "a different account".
    """
    flow = _make_reconfigure_flow({CONF_COUNTRY: "us", CONF_USERNAME: "old@example.com"})

    async def fake_validate(username, password, country, errors, **_kwargs):
        return {"account_id": "acct-1"}

    flow._async_validate = fake_validate  # type: ignore[method-assign]

    _run(flow.async_step_reconfigure())
    result = _run(
        flow.async_step_reconfigure_confirm(
            {CONF_COUNTRY: "uk", CONF_USERNAME: "new@example.com", CONF_PASSWORD: "pw"}
        )
    )

    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert flow._updated["data_updates"] == {
        CONF_USERNAME: "new@example.com",
        CONF_PASSWORD: "pw",
        CONF_COUNTRY: "uk",
    }
    # The title and the unique id follow the corrected country.
    assert flow._updated["title"] == "HelloFresh (UK)"
    assert flow._updated["unique_id"] == "uk::acct-1"


def test_reconfigure_refuses_a_different_account() -> None:
    """Credentials for another account abort rather than repointing the entry."""
    flow = _make_reconfigure_flow({CONF_COUNTRY: "us", CONF_USERNAME: "old@example.com"})

    async def fake_validate(username, password, country, errors, **_kwargs):
        return {"account_id": "somebody-else"}

    flow._async_validate = fake_validate  # type: ignore[method-assign]

    _run(flow.async_step_reconfigure())
    with pytest.raises(AbortFlow) as aborted:
        _run(
            flow.async_step_reconfigure_confirm(
                {CONF_COUNTRY: "us", CONF_USERNAME: "other@example.com", CONF_PASSWORD: "pw"}
            )
        )

    assert aborted.value.reason == "account_mismatch"
    # Nothing was written to the entry.
    assert flow._updated == {}


@pytest.mark.parametrize("legacy_unique_id", [None, "acct-1"])
def test_reconfigure_accepts_an_entry_with_a_legacy_unique_id(legacy_unique_id) -> None:
    """An old entry has no unique id, or the bare account id; it is the same account still."""
    flow = _make_reconfigure_flow(
        {CONF_COUNTRY: "us", CONF_USERNAME: "u@example.com"}, unique_id=legacy_unique_id
    )

    async def fake_validate(username, password, country, errors, **_kwargs):
        return {"account_id": "acct-1"}

    flow._async_validate = fake_validate  # type: ignore[method-assign]

    _run(flow.async_step_reconfigure())
    result = _run(
        flow.async_step_reconfigure_confirm(
            {CONF_COUNTRY: "us", CONF_USERNAME: "u@example.com", CONF_PASSWORD: "pw"}
        )
    )

    assert result["reason"] == "reconfigure_successful"
    assert flow._updated["unique_id"] == "us::acct-1"  # the entry gets a current id


def test_reconfigure_refuses_a_different_account_on_a_legacy_unique_id() -> None:
    """A bare account id is still compared: another account is refused."""
    flow = _make_reconfigure_flow(
        {CONF_COUNTRY: "us", CONF_USERNAME: "u@example.com"}, unique_id="acct-1"
    )

    async def fake_validate(username, password, country, errors, **_kwargs):
        return {"account_id": "somebody-else"}

    flow._async_validate = fake_validate  # type: ignore[method-assign]

    _run(flow.async_step_reconfigure())
    with pytest.raises(AbortFlow) as aborted:
        _run(
            flow.async_step_reconfigure_confirm(
                {CONF_COUNTRY: "us", CONF_USERNAME: "u@example.com", CONF_PASSWORD: "pw"}
            )
        )

    assert aborted.value.reason == "account_mismatch"
    assert flow._updated == {}


def test_reconfigure_will_not_take_the_unique_id_of_another_entry() -> None:
    """Changing the country must not give two entries the same unique id."""
    flow = _make_reconfigure_flow(
        {CONF_COUNTRY: "us", CONF_USERNAME: "u@example.com"},
        other_entries={"uk::acct-1": "e2"},
    )

    async def fake_validate(username, password, country, errors, **_kwargs):
        return {"account_id": "acct-1"}

    flow._async_validate = fake_validate  # type: ignore[method-assign]

    _run(flow.async_step_reconfigure())
    with pytest.raises(AbortFlow) as aborted:
        _run(
            flow.async_step_reconfigure_confirm(
                {CONF_COUNTRY: "uk", CONF_USERNAME: "u@example.com", CONF_PASSWORD: "pw"}
            )
        )

    assert aborted.value.reason == "already_configured"
    assert flow._updated == {}


def test_reconfigure_token_step_rejects_unparseable_token() -> None:
    """Garbage token input re-shows the reconfigure token form with an error."""
    flow = _make_reconfigure_flow({CONF_COUNTRY: "uk", CONF_ACCESS_TOKEN: "a.b.c"})
    _run(flow.async_step_reconfigure())
    result = _run(flow.async_step_reconfigure_token({CONF_COUNTRY: "uk", CONF_TOKEN: "  "}))
    assert result["type"] == "form"
    assert result["step_id"] == "reconfigure_token"
    assert result["errors"]["base"] == "invalid_token"
    assert flow._updated == {}


def test_reconfigure_token_step_updates_entry_on_success() -> None:
    """A valid token paste replaces the stored tokens for the same account.

    Replaces, not merges: pasting a bare access token used to leave the old refresh token in
    the entry, dated as if it had just been issued, and the client then posted that dead token.
    """
    flow = _make_reconfigure_flow(
        {
            CONF_COUNTRY: "uk",
            CONF_ACCESS_TOKEN: "old-token",
            CONF_REFRESH_TOKEN: "old-refresh",
            "refresh_expires_in": 5184000,
        },
        unique_id="uk::acct-9",
    )

    async def fake_validate_token(country, token_data, errors):
        return {"account_id": "acct-9"}

    flow._async_validate_token = fake_validate_token  # type: ignore[method-assign]

    _run(flow.async_step_reconfigure())
    result = _run(
        flow.async_step_reconfigure_token({CONF_COUNTRY: "uk", CONF_TOKEN: "fresh-token"})
    )

    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert "data_updates" not in flow._updated
    assert flow._updated["data"] == {CONF_COUNTRY: "uk", CONF_ACCESS_TOKEN: "fresh-token"}
    assert flow._updated["unique_id"] == "uk::acct-9"


def test_reconfigure_token_step_refuses_a_different_account() -> None:
    """A token for another account aborts rather than repointing the entry."""
    flow = _make_reconfigure_flow(
        {CONF_COUNTRY: "uk", CONF_ACCESS_TOKEN: "old-token"}, unique_id="uk::acct-9"
    )

    async def fake_validate_token(country, token_data, errors):
        return {"account_id": "somebody-else"}

    flow._async_validate_token = fake_validate_token  # type: ignore[method-assign]

    _run(flow.async_step_reconfigure())
    with pytest.raises(AbortFlow) as aborted:
        _run(flow.async_step_reconfigure_token({CONF_COUNTRY: "uk", CONF_TOKEN: "fresh-token"}))

    assert aborted.value.reason == "account_mismatch"
    assert flow._updated == {}


# ---- reauth keeps the entry on its own account -------------------------------------------


def test_reauth_with_another_accounts_password_is_refused(monkeypatch) -> None:
    """Reauth used to repoint the entry at whichever account the new details belonged to."""
    _stub_login_client(monkeypatch, account_id="somebody-else")
    flow = _make_reconfigure_flow(
        {CONF_COUNTRY: "us", CONF_USERNAME: "u@example.com"}, source="reauth"
    )

    _run(flow.async_step_reauth({}))
    with pytest.raises(AbortFlow) as aborted:
        _run(
            flow.async_step_reauth_confirm(
                {CONF_USERNAME: "other@example.com", CONF_PASSWORD: "pw"}
            )
        )

    assert aborted.value.reason == "account_mismatch"
    assert flow._updated == {}


def test_reauth_with_another_accounts_token_is_refused(monkeypatch) -> None:
    """The token path has the same guard."""
    _stub_login_client(monkeypatch, account_id="somebody-else")
    flow = _make_reconfigure_flow(
        {CONF_COUNTRY: "us", CONF_ACCESS_TOKEN: "dead-token"}, source="reauth"
    )

    _run(flow.async_step_reauth({}))
    with pytest.raises(AbortFlow) as aborted:
        _run(flow.async_step_reauth_token({CONF_TOKEN: "another-accounts-token"}))

    assert aborted.value.reason == "account_mismatch"
    assert flow._updated == {}


@pytest.mark.parametrize("unique_id", ["us::acct-1", "acct-1", None])
@pytest.mark.parametrize("token_entry", [False, True])
def test_reauth_of_the_same_account_succeeds(monkeypatch, unique_id, token_entry) -> None:
    """The entry's own account passes, on a current unique id and on either legacy form."""
    _stub_login_client(monkeypatch)
    entry_data = {CONF_COUNTRY: "us"} | (
        {CONF_ACCESS_TOKEN: "dead-token"} if token_entry else {CONF_USERNAME: "u@example.com"}
    )
    flow = _make_reconfigure_flow(entry_data, source="reauth", unique_id=unique_id)

    _run(flow.async_step_reauth({}))
    if token_entry:
        result = _run(flow.async_step_reauth_token({CONF_TOKEN: "fresh-token"}))
    else:
        result = _run(
            flow.async_step_reauth_confirm({CONF_USERNAME: "u@example.com", CONF_PASSWORD: "pw"})
        )

    assert result["reason"] == "reauth_successful"
    # Reauth changes the sign-in, never the entry's identity.
    assert "unique_id" not in flow._updated


# ---- an account without a subscription ---------------------------------------------------


def _flow_for(path: str):
    """Build a flow positioned on one of the paths that validate a sign-in."""
    if path in ("credentials", "token"):
        return _make_flow()
    token_entry = path.endswith("token")
    entry_data = {CONF_COUNTRY: "us"} | (
        {CONF_ACCESS_TOKEN: "old-token"} if token_entry else {CONF_USERNAME: "u@example.com"}
    )
    source = "reauth" if path.startswith("reauth") else "reconfigure"
    flow = _make_reconfigure_flow(entry_data, source=source)
    _run(flow.async_step_reauth({}) if source == "reauth" else flow.async_step_reconfigure())
    return flow


_SIGN_IN = {CONF_USERNAME: "u@example.com", CONF_PASSWORD: "pw"}


@pytest.mark.parametrize(
    ("path", "step", "user_input"),
    [
        ("credentials", "async_step_credentials", {CONF_COUNTRY: "us", **_SIGN_IN}),
        ("token", "async_step_token", {CONF_COUNTRY: "us", CONF_TOKEN: "a-token"}),
        ("reauth", "async_step_reauth_confirm", _SIGN_IN),
        ("reauth_token", "async_step_reauth_token", {CONF_TOKEN: "a-token"}),
        ("reconfigure", "async_step_reconfigure_confirm", {CONF_COUNTRY: "us", **_SIGN_IN}),
        (
            "reconfigure_token",
            "async_step_reconfigure_token",
            {CONF_COUNTRY: "us", CONF_TOKEN: "a-token"},
        ),
    ],
)
def test_an_account_without_a_subscription_is_not_an_authentication_error(
    monkeypatch, path, step, user_input
) -> None:
    """A sign-in that works but finds no subscription says so, on every path.

    It used to read "Invalid authentication" (or "token rejected"), for details that were
    correct, and no amount of re-entering them could help.
    """
    from custom_components.hellofresh.models import (  # noqa: PLC0415
        HelloFreshNoSubscriptionError,
    )

    checks = _stub_login_client(
        monkeypatch, raises=HelloFreshNoSubscriptionError("no subscription")
    )
    flow = _flow_for(path)

    result = _run(getattr(flow, step)(user_input))

    assert result["type"] == "form"
    assert result["errors"] == {"base": "no_subscription"}
    assert checks == []  # the email and password were right; nothing to explain


def test_validation_closes_its_client_before_letting_go_of_the_session(monkeypatch) -> None:
    """Each check uses a one-off client and session; both are released, pass or fail."""
    from custom_components.hellofresh.api import HelloFreshLoginRejectedError  # noqa: PLC0415

    events: list[str] = []
    _stub_login_client(monkeypatch, lifecycle=events)
    flow = _make_flow()
    _run(flow.async_step_credentials({CONF_COUNTRY: "us", **_SIGN_IN}))
    assert events == ["validate", "client_closed", "session_detached"]

    events.clear()
    _stub_login_client(
        monkeypatch, raises=HelloFreshLoginRejectedError("HTTP 401"), lifecycle=events
    )
    _run(_make_flow().async_step_credentials({CONF_COUNTRY: "us", **_SIGN_IN}))
    # The email check still needs the client's connection, so it runs before the close.
    assert events == ["validate", "email_check", "client_closed", "session_detached"]


# ---- a pasted token that validation renews -----------------------------------------------

_JSON = {"content-type": "application/json"}


def _jwt(issued_at: int, expires_at: int) -> str:
    """An unsigned access token whose claims say when it was issued and when it expires."""

    def segment(claims: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()

    return f"{segment({'alg': 'none'})}.{segment({'iat': issued_at, 'exp': expires_at})}.sig"


def _paste_past_half_life() -> str:
    """A token copied from the browser 20 minutes after it was issued.

    Still valid, but past half its life, so the client renews it before the account read.
    """
    now = int(time.time())
    return json.dumps(
        {
            "access_token": _jwt(now - 1200, now + 600),
            "refresh_token": "refresh-1",
            "expires_in": 1800,
            "refresh_expires_in": 5184000,
            "token_type": "Bearer",
        }
    )


class _RotatingHelloFresh:
    """HelloFresh's sign-in as the API reference describes it: a refresh token works once.

    ``/gw/refresh`` answers a live refresh token with a new pair and refuses a spent one with
    400 ``invalid_grant``. The account read lists one subscription, or answers 503 while
    ``failing_reads`` lasts. The real client and token manager run against it.
    """

    def __init__(self, live_refresh: str, *, failing_reads: int = 0) -> None:
        self.live_refresh = {live_refresh}
        self.failing_reads = failing_reads
        self.refresh_posts: list[str] = []
        self._issued = 1

    def session(self) -> _RotatingHelloFreshSession:
        """A new session, as Home Assistant creates one for each validation."""
        return _RotatingHelloFreshSession(self)

    def refresh(self, refresh_token: str) -> AuthResponse:
        self.refresh_posts.append(refresh_token)
        if refresh_token not in self.live_refresh:
            return AuthResponse(400, _JSON, json.dumps({"error": "invalid_grant"}))
        self.live_refresh.discard(refresh_token)
        self._issued += 1
        new_refresh = f"refresh-{self._issued}"
        self.live_refresh.add(new_refresh)
        now = int(time.time())
        auth = {
            "access_token": _jwt(now, now + 1800),
            "refresh_token": new_refresh,
            "expires_in": 1800,
            "refresh_expires_in": 5184000,
            "token_type": "Bearer",
        }
        return AuthResponse(200, _JSON, json.dumps(auth))

    def read_account(self) -> AuthResponse:
        if self.failing_reads:
            self.failing_reads -= 1
            return AuthResponse(503, _JSON, json.dumps({"error": "unavailable"}))
        subscriptions = {"items": [{"id": "sub-1", "customer": {"id": "acct-1"}}]}
        return AuthResponse(200, _JSON, json.dumps(subscriptions))


class _RotatingHelloFreshSession:
    def __init__(self, server: _RotatingHelloFresh) -> None:
        self._server = server

    async def post(self, url, **kwargs):
        assert url.endswith("/gw/refresh")  # no credentials, so never a login
        return self._server.refresh(kwargs["json"]["refresh_token"])

    async def request(self, method, url, **_kwargs):
        return self._server.read_account()

    def detach(self) -> None:
        pass


@pytest.mark.parametrize(
    ("path", "step", "user_input"),
    [
        ("token", "async_step_token", {CONF_COUNTRY: "us"}),
        ("reauth_token", "async_step_reauth_token", {}),
        ("reconfigure_token", "async_step_reconfigure_token", {CONF_COUNTRY: "us"}),
    ],
)
def test_a_pasted_refresh_token_that_hellofresh_refuses_is_not_stored(
    monkeypatch, path, step, user_input
) -> None:
    """The browser had already spent the pasted refresh token (it renews on its own).

    Validation renewed the access token first and HelloFresh refused the refresh token; the
    account read still passed on the access token's last minutes, and the entry was saved with
    the dead refresh token, asking for reauth within the quarter hour.
    """
    from custom_components.hellofresh import config_flow  # noqa: PLC0415

    server = _RotatingHelloFresh(live_refresh="refresh-the-browser-holds")
    monkeypatch.setattr(
        config_flow, "async_create_clientsession", lambda _hass, **_kw: server.session()
    )
    flow = _flow_for(path)

    result = _run(getattr(flow, step)({**user_input, CONF_TOKEN: _paste_past_half_life()}))

    assert server.refresh_posts == ["refresh-1"]
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_token"}
    assert getattr(flow, "_updated", {}) == {}


def test_a_token_renewed_during_validation_is_stored_in_its_place(monkeypatch) -> None:
    """The pasted refresh token is spent by the renewal; the entry keeps the new one."""
    from custom_components.hellofresh import config_flow  # noqa: PLC0415

    server = _RotatingHelloFresh(live_refresh="refresh-1")
    monkeypatch.setattr(
        config_flow, "async_create_clientsession", lambda _hass, **_kw: server.session()
    )

    result = _run(
        _make_flow().async_step_token({CONF_COUNTRY: "us", CONF_TOKEN: _paste_past_half_life()})
    )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_REFRESH_TOKEN] == "refresh-2"
    assert server.live_refresh == {"refresh-2"}


def test_submitting_the_same_token_again_starts_from_the_one_it_was_renewed_to(
    monkeypatch,
) -> None:
    """The renewal worked, then the account read failed (a 503), and the user submits again.

    The new refresh token went with the failed attempt, so the second one posted the spent
    paste, was refused, passed on the old access token, and stored the dead refresh token.
    """
    from custom_components.hellofresh import config_flow  # noqa: PLC0415

    server = _RotatingHelloFresh(live_refresh="refresh-1", failing_reads=1)
    monkeypatch.setattr(
        config_flow, "async_create_clientsession", lambda _hass, **_kw: server.session()
    )
    flow = _make_flow()
    paste = _paste_past_half_life()

    first = _run(flow.async_step_token({CONF_COUNTRY: "us", CONF_TOKEN: paste}))
    assert first["errors"] == {"base": "cannot_connect"}
    assert server.live_refresh == {"refresh-2"}  # HelloFresh honours only the new one now

    second = _run(flow.async_step_token({CONF_COUNTRY: "us", CONF_TOKEN: paste}))

    assert second["type"] == "create_entry"
    assert second["data"][CONF_REFRESH_TOKEN] == "refresh-2"
    assert server.refresh_posts == ["refresh-1"]  # the spent token is not posted again


# ---- every message the flow can show exists ----------------------------------------------

_STRINGS = Path(__file__).resolve().parents[1] / "custom_components" / "hellofresh" / "strings.json"


def test_every_error_and_abort_the_flow_can_show_has_a_message() -> None:
    """A key without a string reaches the user as the bare key.

    The flow mapped ``no_subscription`` before any string file had it, and the reauth steps
    aborted with ``invalid_auth``, which exists only as a form error.
    """
    from custom_components.hellofresh import config_flow  # noqa: PLC0415

    source = inspect.getsource(config_flow)
    strings = json.loads(_STRINGS.read_text(encoding="utf-8"))["config"]
    errors = {
        key
        for value in re.findall(r'errors\["base"\] = (.+)', source)
        for key in re.findall(r'"(\w+)"', value)
    }
    # The keys _async_auth_error_key returns, read from its code below the docstring.
    auth_error_code = inspect.getsource(config_flow.HelloFreshConfigFlow._async_auth_error_key)
    errors |= set(re.findall(r'"(\w+)"', auth_error_code.split('"""')[-1]))
    aborts = set(re.findall(r'reason="(\w+)"', source))
    aborts |= set(re.findall(r'AbortFlow\("(\w+)"', source))

    # The scan finds what it is meant to find.
    assert {"no_subscription", "wrong_password", "bot_blocked_token"} <= errors
    assert {"account_mismatch", "already_configured", "reauth_failed"} <= aborts
    assert not errors - set(strings["error"]), "errors without a string"
    assert not aborts - set(strings["abort"]), "aborts without a string"


def test_reauth_of_an_entry_that_was_removed_aborts_with_a_message() -> None:
    """The entry went away while its reauth form was open."""
    flow = _make_flow()
    flow.hass = SimpleNamespace(  # type: ignore[assignment]
        config_entries=SimpleNamespace(async_get_entry=lambda _id: None)
    )
    flow.context = {"source": "reauth", "entry_id": "gone"}  # type: ignore[assignment]
    flow.flow_id = "flow-1"
    flow.handler = "hellofresh"

    result = _run(flow.async_step_reauth({}))

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_failed"
    assert "reauth_failed" in json.loads(_STRINGS.read_text(encoding="utf-8"))["config"]["abort"]
