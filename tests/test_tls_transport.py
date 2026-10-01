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
    # Auth POSTs get their own pooled session, pinned to Chrome's order for the auth calls.
    assert seen["curl_options"] == {"HTTPHEADER_ORDER": tls_transport._CHROME_AUTH_HEADER_ORDER}
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


_NAVIGATION_REQUEST_HEADERS = {
    "sec-ch-ua": '"Chromium";v="150"',
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": "integration UA",
    "Accept": "text/html,*/*;q=0.8",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "nl-BE,nl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Priority": "u=0, i",
}


def test_navigation_is_sent_in_chrome_navigation_order_from_its_own_pool(monkeypatch) -> None:
    """A page load keeps its navigation headers and gets the navigation order, not the XHR one."""
    seen: list[dict] = []

    async def fake_request(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(status_code=200, headers={}, text="<html></html>")

    _install_fake_curl_cffi(monkeypatch, post=fake_request)
    session = _FakeAiohttpSession()

    async def _page_then_xhr():
        await async_request(
            session,
            "GET",
            "https://www.hellofresh.be/menus",
            headers=_NAVIGATION_REQUEST_HEADERS,
            navigation=True,
        )
        await async_request(session, "GET", "https://www.hellofresh.be/gw/api/test")
        return len(tls_transport._SHARED_SESSIONS)

    assert _run(_page_then_xhr()) == 2
    page, xhr = seen
    assert page["curl_options"] == {
        "HTTPHEADER_ORDER": tls_transport._CHROME_NAVIGATION_HEADER_ORDER
    }
    assert page["default_headers"] is False
    assert page["accept_encoding"] == "gzip, deflate, br, zstd"
    assert page["headers"] == {
        name: value
        for name, value in _NAVIGATION_REQUEST_HEADERS.items()
        if name != "Accept-Encoding"
    }
    assert xhr["curl_options"] == {"HTTPHEADER_ORDER": tls_transport._CHROME_XHR_HEADER_ORDER}
    assert session.calls == []


@pytest.mark.parametrize(
    ("method", "json_payload", "suppressed"),
    [("POST", None, True), ("PUT", None, True), ("POST", {"a": 1}, False), ("DELETE", None, False)],
)
def test_bodiless_post_sends_no_content_type(monkeypatch, method, json_payload, suppressed) -> None:
    """libcurl labels an empty POST as a form; Chrome's bodiless fetch() has no Content-Type."""
    seen: dict = {}

    async def fake_request(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(status_code=200, headers={}, text="{}")

    _install_fake_curl_cffi(monkeypatch, post=fake_request)
    _run(
        async_request(
            _FakeAiohttpSession(),  # type: ignore[arg-type]
            method,
            "https://www.hellofresh.com/gw/auth/token",
            json_payload=json_payload,
            headers={"Accept": "*/*"},
        )
    )

    assert ("Content-Type" in seen["headers"]) is suppressed
    if suppressed:
        assert seen["headers"]["Content-Type"] is None


def test_curl_default_mode_navigation_keeps_the_presets_navigation_headers(monkeypatch) -> None:
    """curl_cffi's Chrome preset already is a page load: only the locale is added to it."""
    seen: dict = {}

    async def fake_request(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(status_code=200, headers={}, text="<html></html>")

    _install_fake_curl_cffi(monkeypatch, post=fake_request)
    session = _FakeAiohttpSession()

    async def _page_load():
        await async_request(
            session,
            "GET",
            "https://www.hellofresh.be/menus",
            headers=_NAVIGATION_REQUEST_HEADERS,
            use_curl_cffi_headers=True,
            navigation=True,
        )
        # The curl-defaults pool serves every kind of request.
        return tls_transport._shared_curl_session(
            True, session, kind="navigation"
        ) is tls_transport._shared_curl_session(True, session, kind="auth")

    assert _run(_page_load()) is True
    assert seen["default_headers"] is True
    assert seen["curl_options"] is None
    assert seen["headers"] == {"Accept-Language": "nl-BE,nl;q=0.9,en-US;q=0.8,en;q=0.7"}


def test_an_entrys_pooled_sessions_share_one_cookie_jar() -> None:
    """The page load sends the cookies its XHRs received, as a browser does; accounts don't mix."""
    if tls_transport._ASYNC_SESSION_CLS is None:
        pytest.skip("curl_cffi is not installed")
    owner = _FakeAiohttpSession()
    other_owner = _FakeAiohttpSession()

    async def _sessions():
        xhr = tls_transport._shared_curl_session(owner=owner)
        page = tls_transport._shared_curl_session(owner=owner, kind="navigation")
        auth = tls_transport._shared_curl_session(owner=owner, kind="auth")
        other = tls_transport._shared_curl_session(owner=other_owner, kind="navigation")
        result = (xhr.cookies.jar, page.cookies.jar, auth.cookies.jar, other.cookies.jar)
        await tls_transport.async_close_shared_session()
        return result

    xhr_jar, page_jar, auth_jar, other_jar = _run(_sessions())
    assert page_jar is xhr_jar
    assert auth_jar is xhr_jar
    assert other_jar is not xhr_jar
    assert tls_transport._SHARED_SESSIONS == {}


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


# ---- header order ------------------------------------------------------------------------

# Header order Chrome 154 sent for each request shape the integration imitates, captured by
# running the web app's fetch() calls (and a page load) in Chrome against a local server that
# asked for DPR/Viewport-Width and set a cookie. HTTP/2 keeps this order and adds priority
# last. The middle block follows Blink's header hash map, so it changes with the header set.
_CHROME_154_CAPTURES = {
    # The sign-in page's axios client sets Accept itself, which moves it before content-type.
    "login": "content-length, x-b3-spanid, sec-ch-ua-platform, viewport-width, x-b3-sampled, "
    "sec-ch-ua, sec-ch-ua-mobile, traceparent, dpr, x-b3-traceid, user-agent, accept, "
    "content-type, origin, sec-fetch-site, sec-fetch-mode, sec-fetch-dest, referer, "
    "accept-encoding, accept-language, cookie",
    "refresh": "content-length, x-b3-spanid, x-request-id, sec-ch-ua-platform, viewport-width, "
    "x-b3-sampled, sec-ch-ua, sec-ch-ua-mobile, traceparent, dpr, x-b3-traceid, user-agent, "
    "content-type, accept, origin, sec-fetch-site, sec-fetch-mode, sec-fetch-dest, referer, "
    "accept-encoding, accept-language, cookie",
    "app_token": "content-length, x-b3-spanid, x-request-id, sec-ch-ua-platform, viewport-width, "
    "x-b3-sampled, sec-ch-ua, sec-ch-ua-mobile, traceparent, dpr, x-b3-traceid, user-agent, "
    "accept, origin, sec-fetch-site, sec-fetch-mode, sec-fetch-dest, referer, accept-encoding, "
    "accept-language, cookie",
    "data_get": "x-b3-spanid, x-market-api-version, authorization, x-request-id, x-b3-sampled, "
    "sec-ch-ua, sec-ch-ua-mobile, traceparent, dpr, x-b3-traceid, x-food-categorization, "
    "sec-ch-ua-platform, x-sort-variations-by-quantity, user-agent, viewport-width, accept, "
    "sec-fetch-site, sec-fetch-mode, sec-fetch-dest, referer, accept-encoding, accept-language, "
    "cookie",
    "data_get_requested_by": "x-b3-spanid, x-market-api-version, authorization, x-request-id, "
    "viewport-width, sec-ch-ua, sec-ch-ua-mobile, traceparent, sec-ch-ua-platform, "
    "x-sort-variations-by-quantity, x-b3-sampled, dpr, x-b3-traceid, x-food-categorization, "
    "user-agent, x-requested-by, accept, sec-fetch-site, sec-fetch-mode, sec-fetch-dest, referer, "
    "accept-encoding, accept-language, cookie",
    "data_post": "content-length, x-b3-spanid, x-market-api-version, authorization, x-request-id, "
    "sec-ch-ua, sec-ch-ua-mobile, traceparent, sec-ch-ua-platform, x-sort-variations-by-quantity, "
    "content-type, viewport-width, x-b3-sampled, dpr, x-b3-traceid, x-food-categorization, "
    "user-agent, accept, origin, sec-fetch-site, sec-fetch-mode, sec-fetch-dest, referer, "
    "accept-encoding, accept-language, cookie",
    "next_data": "x-nextjs-data, sec-ch-ua-platform, dpr, viewport-width, user-agent, sec-ch-ua, "
    "sec-ch-ua-mobile, accept, sec-fetch-site, sec-fetch-mode, sec-fetch-dest, referer, "
    "accept-encoding, accept-language, cookie",
    "navigation": "sec-ch-ua, sec-ch-ua-mobile, sec-ch-ua-platform, upgrade-insecure-requests, "
    "user-agent, accept, sec-fetch-site, sec-fetch-mode, sec-fetch-user, sec-fetch-dest, "
    "accept-encoding, accept-language, cookie",
}
_CHROME_FIXED_TAIL = [
    "accept",
    "origin",
    "sec-fetch-site",
    "sec-fetch-mode",
    "sec-fetch-dest",
    "referer",
    "accept-encoding",
    "accept-language",
    "cookie",
    "priority",
]


def _as_sent(order: str, capture: str) -> list[str]:
    """Return the captured headers in the order a pinned curl order would send them."""
    names = [*capture.split(", "), "priority"]
    return [name for name in order.split(",") if name in names]


@pytest.mark.parametrize(
    ("shape", "order_name"),
    [
        ("login", "_CHROME_LOGIN_HEADER_ORDER"),
        ("refresh", "_CHROME_AUTH_HEADER_ORDER"),
        ("app_token", "_CHROME_AUTH_HEADER_ORDER"),
        ("data_get", "_CHROME_XHR_HEADER_ORDER"),
        ("navigation", "_CHROME_NAVIGATION_HEADER_ORDER"),
    ],
)
def test_pinned_header_order_reproduces_chrome_exactly(shape: str, order_name: str) -> None:
    """The auth calls, the common data GET and the page load go out in Chrome's exact order."""
    capture = _CHROME_154_CAPTURES[shape]
    assert _as_sent(getattr(tls_transport, order_name), capture) == [
        *capture.split(", "),
        "priority",
    ]


@pytest.mark.parametrize("shape", ["data_get_requested_by", "data_post", "next_data"])
def test_xhr_order_keeps_chromes_fixed_prefix_and_tail(shape: str) -> None:
    """Data calls whose middle block differs still get content-length first and Chrome's tail."""
    capture = [*_CHROME_154_CAPTURES[shape].split(", "), "priority"]
    sent = _as_sent(tls_transport._CHROME_XHR_HEADER_ORDER, _CHROME_154_CAPTURES[shape])
    assert sorted(sent) == sorted(capture)
    assert (sent[0] == "content-length") == (capture[0] == "content-length")
    assert [n for n in sent if n in _CHROME_FIXED_TAIL] == [
        n for n in capture if n in _CHROME_FIXED_TAIL
    ]
    assert sent[-len([n for n in capture if n in _CHROME_FIXED_TAIL]) :] == [
        n for n in capture if n in _CHROME_FIXED_TAIL
    ]


def test_every_header_the_integration_sends_has_a_pinned_position() -> None:
    """A header missing from the order list would be appended after priority, unlike Chrome."""
    from custom_components.hellofresh.client import HelloFreshClient  # noqa: PLC0415

    client = HelloFreshClient(session=object(), country="be", access_token="t")  # type: ignore[arg-type]
    wire_added = {"content-length", "content-type", "cookie", "accept-encoding"}
    kinds = {
        "_CHROME_AUTH_HEADER_ORDER": [
            client._tokens._auth_headers(content_type="text/plain;charset=UTF-8"),
            client._tokens._auth_headers(content_type=None),
        ],
        "_CHROME_LOGIN_HEADER_ORDER": [
            client._tokens._auth_headers(content_type="application/json", login=True),
        ],
        "_CHROME_XHR_HEADER_ORDER": [
            client._api_request_headers("POST", {"x-requested-by": "gateway"}),
            client._website_request_headers("/_next/data/build/recipes.json"),
        ],
        "_CHROME_NAVIGATION_HEADER_ORDER": [client._website_request_headers("/recipes")],
    }
    for order_name, header_sets in kinds.items():
        pinned = set(getattr(tls_transport, order_name).split(","))
        for headers in header_sets:
            sent = {name.lower() for name in headers}
            assert sent - pinned == set(), order_name
        if order_name != "_CHROME_NAVIGATION_HEADER_ORDER":
            assert wire_added <= pinned, order_name


def test_login_and_app_token_each_use_their_pinned_chrome_order(monkeypatch) -> None:
    """The sign-in POST and the app-token POST go out from sessions pinned to their own order."""
    from custom_components.hellofresh.token_manager import TokenManager  # noqa: PLC0415

    seen: list[dict] = []

    async def fake_request(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(
            status_code=200,
            headers={"Content-Type": "application/json"},
            text='{"access_token": "a", "refresh_token": "r", "expires_in": 1800}',
        )

    _install_fake_curl_cffi(monkeypatch, post=fake_request)
    tokens = TokenManager(
        session=_FakeAiohttpSession(),  # type: ignore[arg-type]
        username="user@example.com",
        password="pw",
    )
    _run(tokens._async_login(force=True))

    app_token, login = seen
    assert app_token["url"].endswith("/gw/auth/token")
    assert list(app_token["params"]) == ["client_id", "grant_type"]  # the web app's order
    assert app_token["curl_options"] == {
        "HTTPHEADER_ORDER": tls_transport._CHROME_AUTH_HEADER_ORDER
    }
    assert login["url"].endswith("/gw/login")
    assert login["curl_options"] == {"HTTPHEADER_ORDER": tls_transport._CHROME_LOGIN_HEADER_ORDER}
    assert login["headers"]["Accept"] == "application/json, text/plain, */*"
