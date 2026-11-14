"""Tests for the token manager: what counts as a bot block, and when sign-in is retried.

Each test scripts HelloFresh's answers with a fake session and asserts on what the manager
posts. The curl transport is off for the suite (see conftest.py), so the fake session receives
exactly the calls the real one would. Answers are ``AuthResponse`` objects, the shape the curl
transport hands back, lower-case header names included.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import gc
import logging
import traceback

from aiohttp import ClientError
import pytest

from custom_components.hellofresh import token_manager
from custom_components.hellofresh.api import (
    HelloFreshAuthError,
    HelloFreshBotBlockedError,
    HelloFreshClient,
    HelloFreshError,
    HelloFreshLoginRejectedError,
    HelloFreshPasswordResetRequiredError,
    HelloFreshRateLimitedError,
)
from custom_components.hellofresh.tls_transport import AuthResponse
from custom_components.hellofresh.token_manager import TokenManager, _retry_after_seconds

JSON = {"content-type": "application/json"}
HTML = {"content-type": "text/html; charset=UTF-8"}
GATEWAY_ERROR_PAGE = (
    "<html><head><title>502 Bad Gateway</title></head><body><center><h1>502 Bad Gateway</h1>"
    "</center><hr><center>nginx</center></body></html>"
)
CHALLENGE_PAGE = (
    "<!DOCTYPE html><html><head><title>Just a moment...</title></head><body><script>"
    "window._cf_chl_opt={cType:'managed'};var s=document.createElement('script');"
    "s.src='/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1';</script></body></html>"
)
AUTH_OBJECT = '{"access_token":"A2","refresh_token":"R2","expires_in":1800}'


def _ok(body: str = AUTH_OBJECT) -> AuthResponse:
    return AuthResponse(200, JSON, body)


def _json(status: int, body: str = '{"error":"invalid_grant"}') -> AuthResponse:
    return AuthResponse(status, JSON, body)


def _html(status: int, body: str = GATEWAY_ERROR_PAGE) -> AuthResponse:
    return AuthResponse(status, HTML, body)


class _Session:
    """An aiohttp-like session answering from a script, and recording what was sent.

    ``answers`` maps an endpoint ("refresh", "auth/token", "login", "auth/email/status", or
    "data" for every data request) to a response, an exception to raise, or a callable
    returning either. An endpoint without an answer gets a plain success.
    """

    def __init__(self, **answers) -> None:
        self.answers = {name.replace("__", "/"): answer for name, answer in answers.items()}
        self.posts: list[str] = []
        self.sent: list[dict] = []
        self.data_requests: list[str] = []

    async def _answer(self, endpoint: str):
        answer = self.answers.get(endpoint, _ok())
        if callable(answer):
            answer = answer()
        if asyncio.iscoroutine(answer):
            answer = await answer
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def post(self, url, params=None, json=None, headers=None, **kwargs):
        endpoint = url.rsplit("/gw/", 1)[1]
        self.posts.append(endpoint)
        self.sent.append({"endpoint": endpoint, "url": url, "json": json, **kwargs})
        return await self._answer(endpoint)

    async def request(self, method, url, params=None, json=None, headers=None, **kwargs):
        self.data_requests.append((headers or {}).get("Authorization", ""))
        return await self._answer("data")

    def count(self, endpoint: str) -> int:
        return self.posts.count(endpoint)


class _Clock:
    """Both clocks the manager reads: wall time for token expiry, monotonic for pauses."""

    def __init__(self, monkeypatch) -> None:
        self.wall = 1_790_000_000.0
        self.monotonic = 1000.0
        clock = self

        class _FakeDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromtimestamp(clock.wall, tz=tz or UTC)

        monkeypatch.setattr(token_manager, "datetime", _FakeDateTime)
        monkeypatch.setattr(token_manager, "monotonic", lambda: clock.monotonic)

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.monotonic += seconds


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    return _Clock(monkeypatch)


def _manager(session, clock: _Clock, *, token_age: int = 0, **kwargs) -> TokenManager:
    """A manager holding an access token issued ``token_age`` seconds ago (30-minute life)."""
    options = {
        "access_token": "A1",
        "refresh_token": "R1",
        "token_issued_at": int(clock.wall) - token_age,
        "token_expires_in": 1800,
        "refresh_expires_in": 5184000,
        **kwargs,
    }
    return TokenManager(session=session, **options)  # type: ignore[arg-type]


def _client(session, clock: _Clock, *, token_age: int = 0, **kwargs) -> HelloFreshClient:
    options = {
        "access_token": "A1",
        "refresh_token": "R1",
        "token_issued_at": int(clock.wall) - token_age,
        "token_expires_in": 1800,
        "refresh_expires_in": 5184000,
        **kwargs,
    }
    return HelloFreshClient(session=session, **options)  # type: ignore[arg-type]


CREDENTIALS = {"username": "user@example.com", "password": "stale-password"}


# ---- what counts as a bot block ------------------------------------------------------------


@pytest.mark.parametrize("status", [404, 500, 502, 503, 504])
async def test_html_error_page_on_a_data_call_is_not_a_bot_block(clock, status) -> None:
    """A gateway's HTML error page is a failed request, not Cloudflare blocking the client.

    Every HTML response used to start the shared cooldown, so one 502 page paused every
    HelloFresh entry for five minutes and the rest of the poll failed without being sent.
    """
    session = _Session(data=_html(status))
    client = _client(session, clock)

    with pytest.raises(HelloFreshError) as raised:
        await client._async_api_request("GET", "/gw/api/customers/me/subscriptions")
    assert not isinstance(raised.value, HelloFreshBotBlockedError)
    assert raised.value.status == status
    assert GATEWAY_ERROR_PAGE[:20] not in str(raised.value)

    client._tokens.raise_if_bot_blocked()  # no cooldown was started
    assert client._tokens._block_backoff.consecutive_blocks == 0
    with pytest.raises(HelloFreshError):
        await client._async_api_request("GET", "/gw/api/other")
    assert len(session.data_requests) == 2  # the next call was really sent


@pytest.mark.parametrize("status", [400, 404, 500, 502, 503])
async def test_html_error_page_on_refresh_is_a_transient_failure(clock, status) -> None:
    """An HTML error page from /gw/refresh says nothing about the refresh token.

    Not a block (no cooldown), and not a rejection either: the token is kept and the stored
    password is not sent. An HTML 400 used to count as a block; without this it would now
    read as a dead refresh token.
    """
    session = _Session(refresh=_html(status))
    manager = _manager(session, clock, **CREDENTIALS)

    with pytest.raises(HelloFreshError) as raised:
        await manager._async_refresh_access_token(force=True)

    assert not isinstance(raised.value, HelloFreshBotBlockedError | HelloFreshAuthError)
    assert session.posts == ["refresh"]
    assert manager.has_refresh_token
    manager.raise_if_bot_blocked()


async def test_html_error_page_on_login_is_a_transient_failure(clock) -> None:
    """A 502 page from /gw/login is neither a block nor a wrong password."""
    session = _Session(login=_html(502))
    manager = _manager(session, clock, access_token=None, refresh_token=None, **CREDENTIALS)

    for _attempt in range(2):
        with pytest.raises(HelloFreshError) as raised:
            await manager._async_login(force=True)
        assert not isinstance(raised.value, HelloFreshBotBlockedError | HelloFreshAuthError)

    assert session.count("login") == 2  # nothing paused or latched the second attempt
    manager.raise_if_bot_blocked()


@pytest.mark.parametrize("status", [200, 401, 403, 429])
async def test_html_page_on_a_blocking_status_is_still_a_bot_block(clock, status) -> None:
    """Cloudflare answers in the API's place with these statuses; that is still a block."""
    session = _Session(data=_html(status, "<html>Access denied</html>"))
    client = _client(session, clock)

    with pytest.raises(HelloFreshBotBlockedError):
        await client._async_api_request("GET", "/gw/api/customers/me/subscriptions")
    with pytest.raises(HelloFreshBotBlockedError, match="retry in"):
        await client._async_api_request("GET", "/gw/api/other")
    assert len(session.data_requests) == 1
    assert session.posts == []  # a blocked 401 is not a reason to spend the refresh token


@pytest.mark.parametrize(
    "response",
    [
        _html(503, CHALLENGE_PAGE),  # Cloudflare's challenge page, as its older 503
        AuthResponse(503, {**HTML, "cf-mitigated": "challenge"}, GATEWAY_ERROR_PAGE),
        AuthResponse(502, {"content-type": "text/plain"}, "error code: 1020"),
    ],
    ids=["challenge-page", "cf-mitigated-header", "error-1020"],
)
async def test_cloudflares_own_markers_are_a_block_on_any_status(clock, response) -> None:
    """An explicit marker outranks the status: a 5xx challenge still pauses requests."""
    session = _Session(refresh=response)
    manager = _manager(session, clock, **CREDENTIALS)

    with pytest.raises(HelloFreshBotBlockedError):
        await manager._async_refresh_access_token(force=True)

    assert session.posts == ["refresh"]  # no login behind a block
    assert manager.has_refresh_token
    with pytest.raises(HelloFreshBotBlockedError):
        manager.raise_if_bot_blocked()


async def test_cloudflares_detection_script_on_an_error_page_is_not_a_challenge(clock) -> None:
    """Cloudflare adds a script from /cdn-cgi/challenge-platform/ to ordinary pages too."""
    page = (
        "<html><body><h1>503 Service Unavailable</h1><script>(function(){var a=document."
        "createElement('script');a.src='/cdn-cgi/challenge-platform/scripts/jsd/main.js';})();"
        "</script></body></html>"
    )
    session = _Session(data=_html(503, page))
    client = _client(session, clock)

    with pytest.raises(HelloFreshError) as raised:
        await client._async_api_request("GET", "/gw/api/customers/me/subscriptions")

    assert not isinstance(raised.value, HelloFreshBotBlockedError)
    client._tokens.raise_if_bot_blocked()


async def test_a_block_on_one_countrys_site_does_not_pause_another(clock) -> None:
    """The cooldown is per HelloFresh site: Cloudflare blocks one host at a time."""
    session = _Session(refresh=_html(403, "<html>Access denied</html>"))
    us = _manager(session, clock, country="us")
    us_too = _manager(session, clock, country="us")
    uk = _manager(session, clock, country="uk")

    with pytest.raises(HelloFreshBotBlockedError):
        await us._async_refresh_with_token()

    with pytest.raises(HelloFreshBotBlockedError):
        us_too.raise_if_bot_blocked()  # another entry for the same site shares the pause
    uk.raise_if_bot_blocked()  # the other site is unaffected
    session.answers["refresh"] = _ok()
    await uk._async_refresh_with_token()
    assert session.sent[-1]["url"].startswith("https://www.hellofresh.co.uk/")


# ---- a refused password or refresh token is final ------------------------------------------


async def test_a_refused_password_is_not_posted_again(clock) -> None:
    """After /gw/login refuses the stored password, later calls fail without any request.

    The delivery-day watch and service calls keep using the client after a poll has asked
    for reauthentication. Each one re-posted the dead refresh token and the stale password:
    one sign-in attempt every 15 minutes, which is how an account gets locked.
    """
    session = _Session(refresh=_json(401), login=_json(401), data=_json(401))
    client = _client(session, clock, token_age=7200, **CREDENTIALS)

    errors = []
    for _tick in range(8):  # eight delivery-watch ticks, 15 minutes apart
        with pytest.raises(HelloFreshLoginRejectedError) as raised:
            await client._async_api_request("GET", "/gw/api/customers/me/deliveries")
        errors.append(raised.value)
        clock.advance(900)

    assert session.posts == ["refresh", "auth/token", "login"]
    assert session.data_requests == []  # nor a data request that could only get a 401
    assert all(error is errors[0] for error in errors)  # the same error, every time


async def test_an_expired_password_is_not_posted_again_either(clock) -> None:
    """ "Your password has expired" is as final as "wrong password" until the user acts."""
    session = _Session(login=_json(401, '{"error":"access_denied_password_must_be_rotated"}'))
    manager = _manager(session, clock, access_token=None, refresh_token=None, **CREDENTIALS)

    with pytest.raises(HelloFreshPasswordResetRequiredError) as first:
        await manager._async_login(force=True)
    clock.advance(3600)
    with pytest.raises(HelloFreshPasswordResetRequiredError) as second:
        await manager._async_login(force=True)

    assert second.value is first.value
    assert session.count("login") == 1


async def test_a_new_manager_may_try_a_new_password(clock) -> None:
    """The refusal belongs to the stored password; reauth builds a manager with the new one."""
    session = _Session(login=_json(401))
    old = _manager(session, clock, access_token=None, refresh_token=None, **CREDENTIALS)
    with pytest.raises(HelloFreshLoginRejectedError):
        await old._async_login(force=True)

    session.answers["login"] = _ok()
    new = _manager(
        session,
        clock,
        access_token=None,
        refresh_token=None,
        username="user@example.com",
        password="new-password",
    )
    await new._async_login(force=True)

    assert session.count("login") == 2
    assert session.sent[-1]["json"] == {"username": "user@example.com", "password": "new-password"}
    assert new.access_token == "A2"


async def test_a_login_that_merely_failed_is_tried_again(clock) -> None:
    """Only HelloFresh's refusal is final; a 503 from /gw/login is retried next time."""
    session = _Session(login=_json(503, '{"error":"unavailable"}'))
    manager = _manager(session, clock, access_token=None, refresh_token=None, **CREDENTIALS)

    with pytest.raises(HelloFreshError):
        await manager._async_login(force=True)
    session.answers["login"] = _ok()
    await manager._async_login(force=True)

    assert manager.access_token == "A2"


async def test_a_refused_refresh_token_is_forgotten(clock) -> None:
    """A refresh token HelloFresh rejected is dropped, so nothing posts it a second time."""
    session = _Session(refresh=_json(403), data=_ok("{}"))
    client = _client(session, clock, token_age=1000)  # past half-life, 800 s of life left

    # The access token still works, so the request goes ahead on it.
    response = await client._async_api_request("GET", "/gw/api/x")
    assert response.status == 200
    assert not client.has_refresh_token

    clock.advance(120)
    await client._async_api_request("GET", "/gw/api/x")
    assert session.posts == ["refresh"]  # the dead token was posted once only

    # Once the access token has run out too there is nothing left to try, and no request.
    clock.advance(1800)
    sent_before = len(session.data_requests)
    with pytest.raises(HelloFreshAuthError, match="token refresh failed"):
        await client._async_api_request("GET", "/gw/api/x")
    assert len(session.data_requests) == sent_before
    assert session.posts == ["refresh"]


async def test_a_refresh_that_merely_failed_keeps_its_token(clock) -> None:
    """A 5xx is not a rejection: the same refresh token is offered again later."""
    session = _Session(refresh=_json(503, '{"error":"unavailable"}'))
    manager = _manager(session, clock, token_age=1000)

    with pytest.raises(HelloFreshError):
        await manager._async_refresh_with_token()
    assert manager.has_refresh_token

    session.answers["refresh"] = _ok()
    await manager._async_refresh_with_token()
    assert [sent["json"] for sent in session.sent] == [{"refresh_token": "R1"}] * 2


async def test_a_refused_password_stops_a_held_refresh_token_being_posted_too(clock) -> None:
    """Once the password is refused nothing is posted, not even a refresh token still held.

    A /gw/refresh answer without an access token does not say the refresh token is dead, so
    the token is kept and the manager falls back to the password, which HelloFresh refuses.
    That refresh token was then posted again on every later attempt.
    """
    session = _Session(refresh=_ok("{}"), login=_json(401))
    manager = _manager(session, clock, token_age=7200, **CREDENTIALS)

    for _tick in range(4):
        with pytest.raises(HelloFreshLoginRejectedError):
            await manager.async_ensure_fresh()
        clock.advance(900)

    assert manager.has_refresh_token  # HelloFresh never said it was dead
    assert session.posts == ["refresh", "auth/token", "login"]


@pytest.mark.parametrize("refused", ["password", "refresh_token"])
async def test_an_error_raised_until_reauth_does_not_grow(clock, refused) -> None:
    """The kept error is raised on every attempt until the user reauthenticates.

    Raising an error again adds that raise to its traceback, and the traceback keeps every
    frame in it alive. Over days of delivery-watch ticks and service calls, it only grew.
    """
    if refused == "password":
        manager = _manager(
            _Session(refresh=_json(401), login=_json(401)), clock, token_age=7200, **CREDENTIALS
        )
    else:  # a token-only entry whose refresh token HelloFresh refused
        manager = _manager(_Session(refresh=_json(401)), clock, token_age=7200)

    lengths = []
    for _tick in range(10):
        with pytest.raises(HelloFreshAuthError) as raised:
            await manager.async_ensure_fresh()
        lengths.append(len(traceback.extract_tb(raised.value.__traceback__)))
        clock.advance(900)

    # The first failure came from the network; every later one is the kept error.
    assert lengths[2:] == [lengths[1]] * 8


# ---- the 429 sign-in lock ------------------------------------------------------------------


def test_retry_after_is_read_whatever_the_header_case() -> None:
    """The curl transport's headers are a plain dict with lower-case names."""
    assert _retry_after_seconds(AuthResponse(429, {"retry-after": "900"}, "{}")) == 900
    assert _retry_after_seconds(AuthResponse(429, {"Retry-After": " 120 "}, "{}")) == 120
    assert _retry_after_seconds(AuthResponse(429, {"retry-after": "soon"}, "{}")) is None
    assert _retry_after_seconds(AuthResponse(429, {}, "{}")) is None


async def test_a_long_retry_after_from_curl_is_honoured(clock) -> None:
    """Retry-After: 900 used to be lost on the curl transport, leaving the 300 s minimum."""
    session = _Session(login=AuthResponse(429, {**JSON, "retry-after": "900"}, "{}"))
    manager = _manager(session, clock, access_token=None, refresh_token=None, **CREDENTIALS)

    with pytest.raises(HelloFreshRateLimitedError, match="retry in 900 seconds"):
        await manager._async_login(force=True)

    clock.advance(899)
    with pytest.raises(HelloFreshRateLimitedError, match="not retrying"):
        await manager._async_login(force=True)
    assert session.count("login") == 1

    clock.advance(1)
    session.answers["login"] = _ok()
    await manager._async_login(force=True)
    assert session.count("login") == 2


async def test_the_sign_in_lock_outlives_the_client_that_hit_it(clock) -> None:
    """A new client for the same account still waits out HelloFresh's lockout.

    Each config-flow submit and each entry reload builds a new client. The lock lived on the
    old one, so the password was posted again while HelloFresh was still locking the account.
    """
    session = _Session(login=_json(429, "{}"))
    first = _manager(session, clock, access_token=None, refresh_token=None, **CREDENTIALS)
    with pytest.raises(HelloFreshRateLimitedError):
        await first._async_login(force=True)

    session.answers["login"] = _ok()
    same_account = _manager(
        session,
        clock,
        access_token=None,
        refresh_token=None,
        username=" User@Example.com ",  # however the address was typed
        password="another-try",
    )
    with pytest.raises(HelloFreshRateLimitedError, match="not retrying"):
        await same_account._async_login(force=True)
    assert session.count("login") == 1

    # The lock is for that account on that site: nobody else is held back.
    other_account = _manager(
        session,
        clock,
        access_token=None,
        refresh_token=None,
        username="someone-else@example.com",
        password="pw",
    )
    await other_account._async_login(force=True)
    other_site = _manager(
        session, clock, country="uk", access_token=None, refresh_token=None, **CREDENTIALS
    )
    await other_site._async_login(force=True)
    assert session.count("login") == 3

    clock.advance(300)
    await same_account._async_login(force=True)
    assert session.count("login") == 4


def test_the_sign_in_lock_does_not_keep_the_email_address(clock) -> None:
    """The shared registry identifies an account by a digest, not by its address."""
    manager = _manager(_Session(), clock, **CREDENTIALS)
    manager._auth_state.lock_login(manager._login_lock_key, clock.monotonic + 300)

    assert "user@example.com" not in repr(manager._auth_state)


# ---- a refresh is never lost to a cancelled caller -----------------------------------------


class _RotatingServer:
    """A /gw/refresh that spends the refresh token before its answer arrives."""

    def __init__(self) -> None:
        self.valid_refresh = "R1"
        self.exchanges = 0
        self.release = asyncio.Event()

    async def refresh(self) -> AuthResponse:
        self.exchanges += 1
        self.valid_refresh = "R2"  # the old token is dead from here on
        await self.release.wait()  # the answer is still on the wire
        return _ok()


async def _settle() -> None:
    """Let tasks that are ready to run finish."""
    for _turn in range(20):
        await asyncio.sleep(0)


async def test_a_rotated_refresh_token_survives_the_cancelling_of_its_request(clock) -> None:
    """Cancelling the request that is waiting on /gw/refresh must not lose the new token.

    Home Assistant cancels the poll when an entry unloads. HelloFresh had already spent the
    old refresh token, and the new one was never applied or saved: the reloaded entry started
    with a dead token, and a token-only entry needed a new one pasted in.
    """
    server = _RotatingServer()
    persisted: dict = {}
    session = _Session(refresh=server.refresh, data=_ok("{}"))
    client = _client(session, clock, token_age=1000, token_refresh_callback=persisted.update)

    poll = asyncio.create_task(client._async_api_request("GET", "/gw/api/x"))
    await asyncio.sleep(0.01)
    assert server.exchanges == 1
    poll.cancel()
    with pytest.raises(asyncio.CancelledError):
        await poll

    server.release.set()
    await _settle()

    assert client._refresh_token == server.valid_refresh == "R2"
    assert persisted["refresh_token"] == "R2"
    assert persisted["access_token"] == "A2"


async def test_closing_the_client_waits_for_a_refresh_in_flight(clock, monkeypatch) -> None:
    """Unload closes the client; the refresh gets to finish and save before sessions close.

    Closing the curl sessions under a /gw/refresh that HelloFresh has already answered would
    lose the new refresh token just as a cancelled caller did.
    """
    from custom_components.hellofresh import client as client_module  # noqa: PLC0415

    server = _RotatingServer()
    persisted: dict = {}
    session = _Session(refresh=server.refresh, data=_ok("{}"))
    client = _client(session, clock, token_age=1000, token_refresh_callback=persisted.update)
    sessions_closed_with: list[dict] = []

    async def close_sessions(_owner) -> None:
        sessions_closed_with.append(dict(persisted))

    monkeypatch.setattr(client_module, "async_close_shared_session", close_sessions)

    poll = asyncio.create_task(client._async_api_request("GET", "/gw/api/x"))
    await asyncio.sleep(0.01)
    poll.cancel()
    with pytest.raises(asyncio.CancelledError):
        await poll

    closing = asyncio.create_task(client.async_close())
    await asyncio.sleep(0.01)
    assert not closing.done()  # still waiting for HelloFresh's answer
    assert sessions_closed_with == []

    server.release.set()
    await closing

    # The new tokens were already saved when the sessions were closed.
    assert [saved["refresh_token"] for saved in sessions_closed_with] == ["R2"]


async def test_a_request_arriving_during_an_orphaned_refresh_joins_it(clock) -> None:
    """Whoever asks next waits on the refresh in flight; the spent token is not posted again."""
    server = _RotatingServer()
    session = _Session(refresh=server.refresh, data=_ok("{}"))
    client = _client(session, clock, token_age=1000)

    cancelled = asyncio.create_task(client._async_api_request("GET", "/gw/api/x"))
    await asyncio.sleep(0.01)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    # Both the half-life path and the 401 path find the refresh already running.
    proactive = asyncio.create_task(client._async_api_request("GET", "/gw/api/y"))
    reactive = asyncio.create_task(client._tokens.async_force_refresh_if_unchanged("A1"))
    await asyncio.sleep(0.01)
    assert not proactive.done() and not reactive.done()
    server.release.set()
    response = await proactive
    await reactive

    assert response.status == 200
    assert server.exchanges == 1
    assert session.data_requests == ["Bearer A2"]


async def test_closing_does_not_wait_forever_for_a_stuck_refresh(clock, monkeypatch) -> None:
    """The wait at unload is bounded; a refresh that never answers cannot block it."""
    monkeypatch.setattr(token_manager, "_CLOSE_WAITS_FOR_REFRESH_SECONDS", 0.01)
    server = _RotatingServer()
    manager = _manager(_Session(refresh=server.refresh), clock, token_age=1000)

    waiting = asyncio.create_task(manager.async_ensure_fresh())
    await asyncio.sleep(0.01)
    await manager.async_close()  # returns although the refresh is still in flight
    assert not waiting.done()

    server.release.set()
    await waiting
    assert manager.access_token == "A2"


async def test_closing_with_no_refresh_in_flight_returns_at_once(clock) -> None:
    await _manager(_Session(), clock).async_close()


async def test_a_refresh_that_fails_after_its_caller_left_is_not_reported_as_a_bug(clock) -> None:
    """A failed refresh nobody is waiting on must not surface as an unretrieved task error."""
    release = asyncio.Event()

    async def failing_refresh() -> AuthResponse:
        await release.wait()
        return _json(503, '{"error":"unavailable"}')

    reported: list[dict] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    try:
        manager = _manager(_Session(refresh=failing_refresh), clock, token_age=1700)
        caller = asyncio.create_task(manager.async_ensure_fresh())
        await asyncio.sleep(0.01)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        release.set()
        await manager.async_close()
        del manager, caller
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert reported == []


async def test_one_failed_refresh_is_one_warning_for_a_whole_poll(clock, caplog) -> None:
    """Every request of a poll waits on the same failed refresh; the log says it once."""
    session = _Session(refresh=_json(503, '{"error":"unavailable"}'), data=_ok("{}"))
    client = _client(session, clock, token_age=1000)

    with caplog.at_level(logging.WARNING):
        responses = await asyncio.gather(
            *(client._async_api_request("GET", f"/gw/api/x{i}") for i in range(6))
        )

    assert [response.status for response in responses] == [200] * 6
    assert session.posts == ["refresh"]
    assert caplog.text.count("continuing with the existing access token") == 1


# ---- the refresh timer leaves an idle token alone ------------------------------------------


async def test_the_timer_leaves_a_token_nobody_used_to_lapse(clock) -> None:
    """A token no request has carried since it was issued is not renewed by the timer.

    The timer used to renew around the clock: about 64 to 96 /gw/refresh posts a day, each
    rotating the refresh token and rewriting the config entry, for an account polled 8 times.
    """
    session = _Session(data=_ok("{}"))
    client = _client(session, clock, token_age=1000)  # in the refresh window

    for _tick in range(6):
        await client.async_ensure_token_fresh()
        clock.advance(450)

    assert session.posts == []

    # The next request finds the token expired and renews it on demand, as it always did.
    response = await client._async_api_request("GET", "/gw/api/x")
    assert response.status == 200
    assert session.posts == ["refresh"]
    assert session.data_requests == ["Bearer A2"]


async def test_the_timer_keeps_a_token_that_is_in_use_fresh(clock) -> None:
    """Once a request has used the token, the timer renews it ahead of expiry."""
    session = _Session(data=_ok("{}"))
    client = _client(session, clock)

    await client._async_api_request("GET", "/gw/api/x")  # uses A1
    clock.advance(450)
    await client.async_ensure_token_fresh()
    assert session.posts == []  # still in the first half of its life

    clock.advance(450)
    await client.async_ensure_token_fresh()
    assert session.posts == ["refresh"]

    # The new token has not been used yet, so the timer does not renew it in turn.
    for _tick in range(6):
        clock.advance(450)
        await client.async_ensure_token_fresh()
    assert session.posts == ["refresh"]


async def test_a_token_refused_on_use_still_stops_the_timer_with_an_auth_error(clock) -> None:
    """The timer's caller turns an auth failure into a reauth prompt; that must still surface."""
    session = _Session(refresh=_json(403), data=_ok("{}"))
    client = _client(session, clock)

    await client._async_api_request("GET", "/gw/api/x")
    clock.advance(1790)  # in use, and too close to expiry to carry on with
    with pytest.raises(HelloFreshAuthError):
        await client.async_ensure_token_fresh()


# ---- redirects on the auth endpoints -------------------------------------------------------


async def test_no_auth_post_follows_a_redirect(clock) -> None:
    """Every auth POST carries a secret in its body, so none may follow a redirect."""
    session = _Session(auth__email__status=_ok('{"registered":true}'))
    manager = _manager(session, clock, **CREDENTIALS)

    await manager._async_refresh_with_token()
    await manager._async_login(force=True)
    assert await manager.async_email_registered("user@example.com") is True

    assert [sent["endpoint"] for sent in session.sent] == [
        "refresh",
        "auth/token",
        "login",
        "auth/email/status",
    ]
    assert [sent["allow_redirects"] for sent in session.sent] == [False] * 4


@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_a_redirect_from_refresh_is_a_transient_failure(clock, status) -> None:
    """The unfollowed 3xx is a failed request: no rejection, no login, the token kept."""
    redirect = AuthResponse(status, {"location": "https://elsewhere.example/gw/refresh"}, "")
    session = _Session(refresh=redirect)
    manager = _manager(session, clock, **CREDENTIALS)

    with pytest.raises(HelloFreshError, match=f"HTTP {status}") as raised:
        await manager._async_refresh_access_token(force=True)

    assert not isinstance(raised.value, HelloFreshAuthError | HelloFreshBotBlockedError)
    assert "elsewhere.example" not in str(raised.value)
    assert session.posts == ["refresh"]
    assert manager.has_refresh_token
    assert manager.access_token == "A1"


async def test_a_redirect_from_login_is_a_transient_failure(clock) -> None:
    """A 3xx is not a wrong password: nothing is latched and the next attempt is made."""
    redirect = AuthResponse(307, {"location": "https://elsewhere.example/gw/login"}, "")
    session = _Session(login=redirect)
    manager = _manager(session, clock, access_token=None, refresh_token=None, **CREDENTIALS)

    with pytest.raises(HelloFreshError, match="HTTP 307") as raised:
        await manager._async_login(force=True)
    assert not isinstance(raised.value, HelloFreshAuthError)
    assert not manager.has_token

    session.answers["login"] = _ok()
    await manager._async_login(force=True)
    assert manager.access_token == "A2"


async def test_a_redirect_is_no_answer_to_the_email_check(clock) -> None:
    redirect = AuthResponse(302, {"location": "https://elsewhere.example/"}, '{"registered":true}')
    manager = _manager(_Session(auth__email__status=redirect), clock)

    assert await manager.async_email_registered("user@example.com") is None


async def test_a_redirected_app_token_request_does_not_stop_the_login(clock, caplog) -> None:
    """The app token only primes the gateway; a 3xx there is logged and the login goes on."""
    redirect = AuthResponse(302, {"location": "https://elsewhere.example/"}, "")
    session = _Session(auth__token=redirect)
    manager = _manager(session, clock, access_token=None, refresh_token=None, **CREDENTIALS)

    with caplog.at_level(logging.DEBUG):
        await manager._async_login(force=True)

    assert manager.access_token == "A2"
    assert "app-token request returned HTTP 302 (non-fatal)" in caplog.text
    assert "authentication diagnostic: /gw/auth/token returned" not in caplog.text


# ---- small things --------------------------------------------------------------------------


def test_what_the_entry_can_sign_in_with_is_public(clock) -> None:
    """Diagnostics reads these instead of reaching into private token state."""
    full = _client(_Session(), clock, **CREDENTIALS)
    assert full.has_refresh_token is True and full.has_credentials is True

    bare = _client(_Session(), clock, refresh_token=None)
    assert bare.has_refresh_token is False and bare.has_credentials is False

    no_password = _client(_Session(), clock, username="user@example.com")
    assert no_password.has_credentials is False


async def test_a_transport_error_is_logged_with_the_error_that_caused_it(clock, caplog) -> None:
    """The transport wraps every curl failure as ClientError; the cause is the useful part."""

    def failing_post():
        try:
            raise TimeoutError("curl: (28) Operation timed out with secret-url?token=abc")
        except TimeoutError as cause:
            error = ClientError("curl_cffi auth POST failed: TimeoutError")
            error.__cause__ = cause
            return error

    session = _Session(refresh=failing_post)
    manager = _manager(session, clock, log_auth_diagnostics=True)

    with caplog.at_level(logging.WARNING), pytest.raises(HelloFreshError) as raised:
        await manager._async_refresh_with_token()

    assert "/gw/refresh transport error: ClientError (TimeoutError)" in caplog.text
    assert "ClientError (TimeoutError)" in str(raised.value)
    assert "secret-url" not in caplog.text and "secret-url" not in str(raised.value)
    assert manager.has_refresh_token  # a request that never got an answer rejects nothing
