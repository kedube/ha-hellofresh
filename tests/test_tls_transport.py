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
        def __init__(self, *, curl_options=None):
            self.curl_options = curl_options

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
            accept_encoding=None,
            impersonate=None,
            default_headers=None,
            verify=None,
        ):
            return await post(
                method=method,
                url=url,
                params=params,
                json=json,
                headers=headers,
                accept_encoding=accept_encoding,
                impersonate=impersonate,
                default_headers=default_headers,
                curl_options=self.curl_options,
                verify=verify,
            )

    requests_module = ModuleType("curl_cffi.requests")
    requests_module.AsyncSession = _FakeAsyncSession  # type: ignore[attr-defined]
    curl_module = ModuleType("curl_cffi")
    curl_module.requests = requests_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "curl_cffi", curl_module)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", requests_module)
    monkeypatch.setattr(tls_transport, "_HAS_CURL_CFFI", True)
    monkeypatch.setattr(
        tls_transport, "_CURL_OPT", SimpleNamespace(HTTPHEADER_ORDER="HTTPHEADER_ORDER")
    )
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

    async def fake_post(
        *,
        method,
        url,
        params,
        json,
        headers,
        accept_encoding,
        impersonate,
        default_headers,
        curl_options,
        verify=None,
    ):
        seen.update(
            {
                "method": method,
                "url": url,
                "params": params,
                "json": json,
                "headers": headers,
                "accept_encoding": accept_encoding,
                "impersonate": impersonate,
                "default_headers": default_headers,
                "curl_options": curl_options,
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
    assert seen["default_headers"] is False
    assert seen["accept_encoding"] == "gzip, deflate, br, zstd"
    assert seen["curl_options"] == {"HTTPHEADER_ORDER": tls_transport._CHROME_XHR_HEADER_ORDER}
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

    async def fake_request(
        *,
        method,
        url,
        params,
        json,
        headers,
        accept_encoding,
        impersonate,
        default_headers,
        curl_options,
        verify=None,
    ):
        seen.update(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "impersonate": impersonate,
                "default_headers": default_headers,
                "accept_encoding": accept_encoding,
                "curl_options": curl_options,
                "verify": verify,
            }
        )
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
            headers={"Authorization": "Bearer t", "Accept-Encoding": "gzip, deflate, br"},
        )
    )

    assert session.calls == []  # aiohttp not touched
    assert seen["method"] == "GET"
    assert seen["impersonate"] == tls_transport._IMPERSONATE_TARGET
    assert seen["default_headers"] is False
    assert seen["accept_encoding"] == "gzip, deflate, br, zstd"
    assert seen["headers"] == {"Authorization": "Bearer t"}
    assert seen["curl_options"] == {"HTTPHEADER_ORDER": tls_transport._CHROME_XHR_HEADER_ORDER}
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
            headers={"Accept-Encoding": "gzip, deflate, br"},
        )
    )

    assert len(session.calls) == 1
    assert session.calls[0]["method"] == "PATCH"
    assert session.calls[0]["json"] == {"a": 1}
    assert session.calls[0]["headers"] == {"Accept-Encoding": "gzip, deflate, br"}


@pytest.mark.parametrize("method", ["GET", "POST", "PATCH"])
def test_curl_failure_does_not_replay_data_request(monkeypatch, method) -> None:
    """A failed curl request may already have reached the server, including writes."""

    async def fail(**_kwargs):
        raise RuntimeError("request reached server before disconnect")

    _install_fake_curl_cffi(monkeypatch, post=fail)
    session = _FakeAiohttpSession()
    with pytest.raises(ClientError, match="curl_cffi"):
        _run(async_request(session, method, "https://www.hellofresh.com/gw/api/test"))
    assert session.calls == []


@pytest.mark.parametrize(("method", "auth_post"), [("GET", False), ("POST", True)])
def test_curl_default_header_mode_keeps_only_api_headers(monkeypatch, method, auth_post) -> None:
    """The optional curl mode supplies auth/API fields but leaves browser metadata to curl."""
    seen: dict = {}

    async def fake_request(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(status_code=200, headers={}, text='{"ok": true}')

    _install_fake_curl_cffi(monkeypatch, post=fake_request)
    session = _FakeAiohttpSession()
    headers = {
        "User-Agent": "integration UA",
        "sec-ch-ua": '"Chromium";v="150"',
        "Sec-Fetch-Mode": "cors",
        "Upgrade-Insecure-Requests": "1",
        "Accept": "application/json",
        "Authorization": "Bearer token",
        "Origin": "https://www.hellofresh.com",
        "Referer": "https://www.hellofresh.com/",
        "Content-Type": "application/json",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "nl-BE,nl;q=0.9,en-US;q=0.8,en;q=0.7",
        "DPR": "1",
        "Viewport-Width": "1280",
        "traceparent": "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01",
        "X-Market-API-Version": "2",
    }
    request = async_auth_post if auth_post else async_request
    args = (session, "https://www.hellofresh.com/gw/api/test")
    if not auth_post:
        args = (session, method, args[1])
    _run(request(*args, headers=headers, use_curl_cffi_headers=True))

    assert seen["default_headers"] is True
    assert seen["accept_encoding"] == "gzip, deflate, br, zstd"
    assert seen["curl_options"] is None
    assert seen["headers"] == {
        "Accept": "application/json",
        "Authorization": "Bearer token",
        "Origin": "https://www.hellofresh.com",
        "Referer": "https://www.hellofresh.com/",
        "Content-Type": "application/json",
        "Accept-Language": "nl-BE,nl;q=0.9,en-US;q=0.8,en;q=0.7",
        "DPR": "1",
        "Viewport-Width": "1280",
        "traceparent": "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01",
        "X-Market-API-Version": "2",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-User": None,
        "Upgrade-Insecure-Requests": None,
    }
    assert session.calls == []


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

    def __init__(self, **_kwargs) -> None:
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
    monkeypatch.setattr(
        tls_transport, "_CURL_OPT", SimpleNamespace(HTTPHEADER_ORDER="HTTPHEADER_ORDER")
    )


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
        return tls_transport._shared_curl_session(owner=session)

    pooled = _run(_five_requests())

    assert _CountingAsyncSession.instances == 1  # not 5
    assert pooled.requests == 5
    assert session.calls == []  # aiohttp never touched


def test_curl_sessions_are_isolated_by_owner(monkeypatch) -> None:
    """Two accounts must not share a curl cookie jar even on the same event loop."""
    _use_counting_session(monkeypatch)
    first_owner = _FakeAiohttpSession()
    second_owner = _FakeAiohttpSession()

    async def _request_each():
        await async_request(first_owner, "GET", "https://www.hellofresh.com/gw/api/one")
        await async_request(second_owner, "GET", "https://www.hellofresh.com/gw/api/two")
        first = tls_transport._shared_curl_session(owner=first_owner)
        second = tls_transport._shared_curl_session(owner=second_owner)
        await tls_transport.async_close_shared_session(first_owner)
        return first, second

    first, second = _run(_request_each())
    assert first is not second
    assert first.closed is True
    assert second.closed is False
    assert second.requests == 1


def test_header_modes_use_separate_pooled_sessions_and_both_close(monkeypatch) -> None:
    """One entry's pinned XHR order cannot leak into another entry's curl defaults."""
    _use_counting_session(monkeypatch)
    session = _FakeAiohttpSession()

    async def _request_both_modes():
        await async_request(session, "GET", "https://www.hellofresh.com/gw/api/one")
        await async_request(
            session,
            "GET",
            "https://www.hellofresh.com/gw/api/two",
            use_curl_cffi_headers=True,
        )
        pooled = list(tls_transport._SHARED_SESSIONS.values())
        await tls_transport.async_close_shared_session()
        return pooled

    pooled = _run(_request_both_modes())

    assert _CountingAsyncSession.instances == 2
    assert [item.requests for item in pooled] == [1, 1]
    assert all(item.closed for item in pooled)
    assert tls_transport._SHARED_SESSIONS == {}


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
        first = tls_transport._shared_curl_session(owner=session)
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


# ---- browser identity matches the impersonated Chrome --------------------------------------


@pytest.mark.parametrize(
    ("major", "expected"),
    [
        # Real Chrome 153's own header, from a HAR of hellofresh.com.
        (153, '"Google Chrome";v="153", "Not_A Brand";v="8", "Chromium";v="153"'),
        # What curl_cffi 0.16.3 sends for its chrome150 target.
        (150, '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"'),
        # Earlier stable releases, as Chrome sent them.
        (131, '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"'),
        (124, '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'),
        (120, '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"'),
    ],
)
def test_sec_ch_ua_matches_what_chrome_sends(major, expected) -> None:
    """The brand list rotates with Chrome's major version; a fixed string matches one release."""
    from custom_components.hellofresh.token_manager import _sec_ch_ua  # noqa: PLC0415

    assert _sec_ch_ua(major) == expected


def test_browser_headers_claim_the_impersonated_chrome_version() -> None:
    """User-Agent and Sec-CH-UA must claim the Chrome version the TLS fingerprint is.

    curl_cffi 0.16.3 moved its "chrome" alias to Chrome 150 while the headers still said 138;
    the version now comes from curl_cffi itself (or the fallback when it isn't installed).
    """
    from custom_components.hellofresh import token_manager  # noqa: PLC0415

    impersonated = tls_transport.impersonated_chrome_major()
    major = impersonated or token_manager._FALLBACK_CHROME_MAJOR_VERSION
    assert major == token_manager._CHROME_MAJOR_VERSION
    if impersonated is not None:
        assert f"chrome{major}" == tls_transport._IMPERSONATE_TARGET
    assert f"Chrome/{major}.0.0.0" in token_manager._BROWSER_USER_AGENT
    assert token_manager._BROWSER_CLIENT_HINTS["sec-ch-ua"] == token_manager._sec_ch_ua(major)
    # Real Chrome sends only the low-entropy hints on HelloFresh's /gw XHRs.
    assert {k for k in token_manager._BROWSER_CLIENT_HINTS if k.startswith("sec-ch-ua")} == {
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
    }
