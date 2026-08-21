"""Unit tests for the HelloFresh config flow's credential vs token paths.

These drive the flow object directly (stubbing the HA flow-manager helpers and the network
validation) to stay consistent with the suite's lightweight, hass-free style.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from urllib.parse import quote

from custom_components.hellofresh.config_flow import HelloFreshConfigFlow
from custom_components.hellofresh.const import (
    CONF_ACCESS_TOKEN,
    CONF_COUNTRY,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN,
    CONF_USERNAME,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def _make_flow() -> HelloFreshConfigFlow:
    """Build a flow with the HA flow-manager helpers stubbed to record calls."""
    flow = HelloFreshConfigFlow()
    flow.hass = SimpleNamespace()  # type: ignore[assignment]
    created: dict = {}

    def async_create_entry(*, title, data, **_kwargs):
        created.update({"title": title, "data": data})
        return {"type": "create_entry", "title": title, "data": data}

    def async_show_form(*, step_id, errors=None, **_kwargs):
        return {"type": "form", "step_id": step_id, "errors": errors or {}}

    def async_show_menu(*, step_id, menu_options, **_kwargs):
        return {"type": "menu", "step_id": step_id, "menu_options": menu_options}

    async def async_set_unique_id(uid):
        flow._uid = uid  # type: ignore[attr-defined]
        return None

    flow.async_create_entry = async_create_entry  # type: ignore[method-assign]
    flow.async_show_form = async_show_form  # type: ignore[method-assign]
    flow.async_show_menu = async_show_menu  # type: ignore[method-assign]
    flow.async_set_unique_id = async_set_unique_id  # type: ignore[method-assign]
    flow._abort_if_unique_id_configured = lambda: None  # type: ignore[method-assign]
    flow._created = created  # type: ignore[attr-defined]
    return flow


def test_user_step_shows_menu_of_both_paths() -> None:
    """The initial step offers the credential and token paths."""
    flow = _make_flow()
    result = _run(flow.async_step_user())
    assert result["type"] == "menu"
    assert result["menu_options"] == ["credentials", "token"]


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


def test_token_path_parses_blob_and_stores_tokens_without_credentials() -> None:
    """The token step parses an apiV2Auth blob, validates, and stores tokens only."""
    flow = _make_flow()

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


def _make_reconfigure_flow(entry_data: dict, *, mismatch: bool = False):
    """Build a flow positioned on ``entry_data`` with the reconfigure helpers stubbed."""
    flow = _make_flow()
    entry = SimpleNamespace(data=entry_data, options={})
    flow.hass = SimpleNamespace(  # type: ignore[assignment]
        config_entries=SimpleNamespace(async_get_entry=lambda _id: entry)
    )
    flow.context = {"entry_id": "e1"}  # type: ignore[attr-defined]

    updated: dict = {}

    def async_update_reload_and_abort(target, *, reason, **kwargs):
        updated.update({"entry": target, "reason": reason, **kwargs})
        return {"type": "abort", "reason": reason, **kwargs}

    def _abort_if_unique_id_mismatch(*, reason):
        if mismatch:
            raise _Aborted(reason)

    flow.async_update_reload_and_abort = async_update_reload_and_abort  # type: ignore[method-assign]
    flow._abort_if_unique_id_mismatch = _abort_if_unique_id_mismatch  # type: ignore[method-assign]
    flow._updated = updated  # type: ignore[attr-defined]
    flow._entry = entry  # type: ignore[attr-defined]
    return flow


class _Aborted(Exception):
    """Stands in for HA's AbortFlow, which the real helper raises on a unique-id mismatch."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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
    """Reconfiguring rewrites credentials and the country, then reloads the entry."""
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
    # The title follows the corrected country.
    assert flow._updated["title"] == "HelloFresh (UK)"


def test_reconfigure_refuses_a_different_account() -> None:
    """Credentials for another account abort rather than repointing the entry."""
    flow = _make_reconfigure_flow(
        {CONF_COUNTRY: "us", CONF_USERNAME: "old@example.com"}, mismatch=True
    )

    async def fake_validate(username, password, country, errors, **_kwargs):
        return {"account_id": "somebody-else"}

    flow._async_validate = fake_validate  # type: ignore[method-assign]

    _run(flow.async_step_reconfigure())
    try:
        _run(
            flow.async_step_reconfigure_confirm(
                {CONF_COUNTRY: "us", CONF_USERNAME: "other@example.com", CONF_PASSWORD: "pw"}
            )
        )
    except _Aborted as err:
        assert err.reason == "account_mismatch"
    else:  # pragma: no cover - guard must fire
        raise AssertionError("expected the mismatch guard to abort the flow")

    # Nothing was written to the entry.
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
    """A valid token paste rewrites the stored tokens for the same account."""
    flow = _make_reconfigure_flow({CONF_COUNTRY: "uk", CONF_ACCESS_TOKEN: "old-token"})

    async def fake_validate_token(country, token_data, errors):
        return {"account_id": "acct-9"}

    flow._async_validate_token = fake_validate_token  # type: ignore[method-assign]

    _run(flow.async_step_reconfigure())
    result = _run(
        flow.async_step_reconfigure_token({CONF_COUNTRY: "uk", CONF_TOKEN: "fresh-token"})
    )

    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert flow._updated["data_updates"][CONF_ACCESS_TOKEN] == "fresh-token"
