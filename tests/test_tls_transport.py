"""Unit tests for the TLS-impersonating auth transport.

These exercise the transport-selection logic (curl_cffi vs aiohttp fallback) without
requiring curl_cffi to be installed: the curl_cffi path is simulated by injecting a fake
``curl_cffi.requests`` module and flipping the module's availability flag.
"""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace

from aiohttp import ClientError
import pytest

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
            self,
            method,
            url,
            params=None,
            json=None,
            headers=None,
            impersonate=None,
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


def test_curl_cffi_mid_request_failure_never_resends_auth_post(monkeypatch) -> None:
    """A curl_cffi failure DURING an auth POST must raise, not retry via aiohttp.

    The auth POSTs are non-idempotent: /gw/refresh burns the refresh token on use. A
    timeout after the server processed the request meant the aiohttp retry re-sent the
    now-spent token, whose 401 read as "refresh token rejected" and forced a needless
    reauth. The failure must surface as a transient ClientError with NO resend.
    """

    async def failing_post(**_kwargs):
        raise RuntimeError("curl boom")

    _install_fake_curl_cffi(monkeypatch, post=failing_post)
    session = _FakeAiohttpSession()

    with pytest.raises(ClientError):
        _run(
            async_auth_post(
                session,  # type: ignore[arg-type]
                "https://www.hellofresh.co.uk/gw/refresh",
                json_payload={"refresh_token": "r"},
            )
        )

    # The one-time token was NOT re-sent through aiohttp.
    assert session.calls == []


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


# ---- pooled session ---------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_pooled_sessions():
    """Never let a pooled session leak between tests (each test builds its own loop)."""
    tls_transport._SHARED_SESSIONS.clear()
    yield
    tls_transport._SHARED_SESSIONS.clear()


class _CountingAsyncSession:
    """Counts how many sessions get constructed and how many requests each one serves."""

    instances = 0

    def __init__(self) -> None:
        type(self).instances += 1
        self.requests = 0
        self.closed = False

    async def request(self, method, url, **kwargs):
        self.requests += 1
        return SimpleNamespace(status_code=200, headers={}, text='{"ok": true}')

    async def close(self) -> None:
        self.closed = True


def _use_counting_session(monkeypatch):
    _CountingAsyncSession.instances = 0
    monkeypatch.setattr(tls_transport, "_HAS_CURL_CFFI", True)
    monkeypatch.setattr(tls_transport, "_ASYNC_SESSION_CLS", _CountingAsyncSession)


def test_curl_session_is_reused_across_requests(monkeypatch) -> None:
    """Many requests on one loop share ONE session, so connections stay pooled.

    This is the whole point of the pooling change: a session per request meant a fresh
    TCP+TLS handshake on every call (~67 ms measured against the live host).
    """
    _use_counting_session(monkeypatch)
    session = _FakeAiohttpSession()

    async def _five_requests():
        for index in range(5):
            await async_request(
                session,  # type: ignore[arg-type]
                "GET",
                f"https://www.hellofresh.com/gw/api/thing/{index}",
            )
        return tls_transport._shared_curl_session()

    pooled = _run(_five_requests())

    assert _CountingAsyncSession.instances == 1  # not 5
    assert pooled.requests == 5
    assert session.calls == []  # aiohttp never touched


def test_close_shared_session_closes_and_drops_it(monkeypatch) -> None:
    """Unload closes the pooled session and forgets it, so a later load builds a fresh one."""
    _use_counting_session(monkeypatch)
    session = _FakeAiohttpSession()

    async def _request_then_close():
        await async_request(
            session,  # type: ignore[arg-type]
            "GET",
            "https://www.hellofresh.com/gw/api/thing",
        )
        first = tls_transport._shared_curl_session()
        await tls_transport.async_close_shared_session()
        return first

    first = _run(_request_then_close())

    assert first.closed is True
    assert tls_transport._SHARED_SESSIONS == {}
    assert _CountingAsyncSession.instances == 1


def test_close_shared_session_is_a_noop_without_one(monkeypatch) -> None:
    """Closing when nothing was ever created must not raise (unload runs unconditionally)."""
    _use_counting_session(monkeypatch)

    _run(tls_transport.async_close_shared_session())

    assert _CountingAsyncSession.instances == 0


def test_close_shared_session_survives_a_failing_close(monkeypatch) -> None:
    """A session whose close() raises must not turn a successful unload into a failure."""

    class _BadCloseSession(_CountingAsyncSession):
        async def close(self) -> None:
            raise RuntimeError("curl handle already gone")

    _CountingAsyncSession.instances = 0
    monkeypatch.setattr(tls_transport, "_HAS_CURL_CFFI", True)
    monkeypatch.setattr(tls_transport, "_ASYNC_SESSION_CLS", _BadCloseSession)
    session = _FakeAiohttpSession()

    async def _request_then_close():
        await async_request(
            session,  # type: ignore[arg-type]
            "GET",
            "https://www.hellofresh.com/gw/api/thing",
        )
        await tls_transport.async_close_shared_session()

    _run(_request_then_close())  # must not raise

    assert tls_transport._SHARED_SESSIONS == {}
