"""Unit tests for the TLS-impersonating auth transport.

These exercise the transport-selection logic (curl_cffi vs aiohttp fallback) without
requiring curl_cffi to be installed: the curl_cffi path is simulated by injecting a fake
``curl_cffi.requests`` module and flipping the module's availability flag.
"""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace

from custom_components.hellofresh import tls_transport
from custom_components.hellofresh.tls_transport import (
    AuthResponse,
    async_auth_post,
    async_request,
)


def _run(coro):
    """Run a coroutine on a fresh event loop (mirrors the suite's async test style).

    The loop is intentionally left open (not closed) to match the rest of the suite and to
    avoid "Event loop is closed" teardown races when a test runs more than one coroutine.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


class _FakeAiohttpResponse:
    """Stand-in for an aiohttp ClientResponse from the fallback path."""

    def __init__(self) -> None:
        self.status = 200
        self.headers = {"Content-Type": "application/json"}

    async def text(self) -> str:
        return '{"access_token": "from-aiohttp"}'

    async def json(self, content_type=None):
        return {"access_token": "from-aiohttp"}


class _FakeAiohttpSession:
    """Records the last aiohttp POST and returns a fake response."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def post(self, url, params=None, json=None, headers=None):
        self.calls.append(
            {"method": "POST", "url": url, "params": params, "json": json, "headers": headers}
        )
        return _FakeAiohttpResponse()

    async def request(self, method, url, params=None, json=None, headers=None):
        self.calls.append(
            {"method": method, "url": url, "params": params, "json": json, "headers": headers}
        )
        return _FakeAiohttpResponse()


def _install_fake_curl_cffi(monkeypatch, *, post):
    """Inject a fake ``curl_cffi.requests`` module whose AsyncSession.post is ``post``."""

    class _FakeAsyncSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(
            self, method, url, params=None, json=None, headers=None, impersonate=None,
            verify=None,
        ):
            return await post(
                method=method,
                url=url,
                params=params,
                json=json,
                headers=headers,
                impersonate=impersonate,
                verify=verify,
            )

    requests_module = ModuleType("curl_cffi.requests")
    requests_module.AsyncSession = _FakeAsyncSession  # type: ignore[attr-defined]
    curl_module = ModuleType("curl_cffi")
    curl_module.requests = requests_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "curl_cffi", curl_module)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", requests_module)
    monkeypatch.setattr(tls_transport, "_HAS_CURL_CFFI", True)
    # AsyncSession is imported once at module load; the transport uses that cached class. Point
    # it at the injected fake for the duration of the test (monkeypatch restores it afterwards).
    monkeypatch.setattr(tls_transport, "_ASYNC_SESSION_CLS", _FakeAsyncSession)


def test_falls_back_to_aiohttp_when_curl_cffi_absent(monkeypatch) -> None:
    """With curl_cffi unavailable, the aiohttp session is used and its native response returned."""
    monkeypatch.setattr(tls_transport, "_HAS_CURL_CFFI", False)
    session = _FakeAiohttpSession()

    response = _run(
        async_auth_post(
            session,  # type: ignore[arg-type]
            "https://www.hellofresh.co.uk/gw/login",
            params={"country": "GB"},
            json_payload={"username": "a", "password": "b"},
            headers={"User-Agent": "x"},
        )
    )

    assert len(session.calls) == 1
    assert session.calls[0]["url"].endswith("/gw/login")
    assert session.calls[0]["json"] == {"username": "a", "password": "b"}
    assert _run(response.json(content_type=None)) == {"access_token": "from-aiohttp"}


def test_uses_curl_cffi_with_chrome_impersonation_when_available(monkeypatch) -> None:
    """When curl_cffi is present, the request goes through it with a Chrome impersonate target."""
    seen: dict = {}

    async def fake_post(*, method, url, params, json, headers, impersonate, verify=None):
        seen.update(
            {
                "method": method,
                "url": url,
                "params": params,
                "json": json,
                "headers": headers,
                "impersonate": impersonate,
                "verify": verify,
            }
        )
        return SimpleNamespace(
            status_code=200,
            headers={"Content-Type": "application/json"},
            text='{"access_token": "from-curl-cffi"}',
        )

    _install_fake_curl_cffi(monkeypatch, post=fake_post)
    session = _FakeAiohttpSession()

    response = _run(
        async_auth_post(
            session,  # type: ignore[arg-type]
            "https://www.hellofresh.co.uk/gw/login",
            params={"country": "GB"},
            json_payload={"username": "a", "password": "b"},
            headers={"User-Agent": "x"},
        )
    )

    # curl_cffi path used; aiohttp session NOT touched.
    assert session.calls == []
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/gw/login")
    assert seen["impersonate"] == tls_transport._IMPERSONATE_TARGET
    # TLS cert verification is explicitly enabled on the credential-carrying auth POST.
    assert seen["verify"] is True
    assert seen["json"] == {"username": "a", "password": "b"}
    assert isinstance(response, AuthResponse)
    assert response.status == 200
    assert _run(response.json()) == {"access_token": "from-curl-cffi"}


def test_curl_cffi_failure_degrades_to_aiohttp(monkeypatch) -> None:
    """A curl_cffi transport error must fall back to aiohttp, not crash the auth call."""

    async def failing_post(**_kwargs):
        raise RuntimeError("curl boom")

    _install_fake_curl_cffi(monkeypatch, post=failing_post)
    session = _FakeAiohttpSession()

    response = _run(
        async_auth_post(
            session,  # type: ignore[arg-type]
            "https://www.hellofresh.co.uk/gw/refresh",
            json_payload={"refresh_token": "r"},
        )
    )

    # Fell through to aiohttp.
    assert len(session.calls) == 1
    assert _run(response.text()) == '{"access_token": "from-aiohttp"}'


def test_auth_response_exposes_status_headers_text_and_json() -> None:
    """AuthResponse mirrors the aiohttp slice the token manager relies on."""
    resp = AuthResponse(status=403, headers={"Content-Type": "text/html"}, body="<html>blocked")
    assert resp.status == 403
    assert resp.headers["Content-Type"] == "text/html"
    assert _run(resp.text()) == "<html>blocked"


def test_data_request_uses_curl_cffi_with_method_when_available(monkeypatch) -> None:
    """async_request routes data XHRs (any verb) through curl_cffi with the right method."""
    seen: dict = {}

    async def fake_request(*, method, url, params, json, headers, impersonate, verify=None):
        seen.update({"method": method, "url": url, "impersonate": impersonate, "verify": verify})
        return SimpleNamespace(
            status_code=200,
            headers={"Content-Type": "application/json"},
            text='{"weeks": []}',
        )

    _install_fake_curl_cffi(monkeypatch, post=fake_request)
    session = _FakeAiohttpSession()

    response = _run(
        async_request(
            session,  # type: ignore[arg-type]
            "GET",
            "https://www.hellofresh.co.uk/gw/api/customers/me/subscriptions",
            headers={"Authorization": "Bearer t"},
        )
    )

    assert session.calls == []  # aiohttp not touched
    assert seen["method"] == "GET"
    assert seen["impersonate"] == tls_transport._IMPERSONATE_TARGET
    assert isinstance(response, AuthResponse)
    assert _run(response.json(content_type=None)) == {"weeks": []}


def test_data_request_falls_back_to_session_request(monkeypatch) -> None:
    """Without curl_cffi, async_request uses aiohttp's session.request (not .post)."""
    monkeypatch.setattr(tls_transport, "_HAS_CURL_CFFI", False)
    session = _FakeAiohttpSession()

    _run(
        async_request(
            session,  # type: ignore[arg-type]
            "PATCH",
            "https://www.hellofresh.com/gw/api/x",
            json_payload={"a": 1},
        )
    )

    assert len(session.calls) == 1
    assert session.calls[0]["method"] == "PATCH"
    assert session.calls[0]["json"] == {"a": 1}
