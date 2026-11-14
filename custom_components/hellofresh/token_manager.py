"""Access/refresh token lifecycle for the HelloFresh integration.

Extracted from ``client.py`` so the security-sensitive auth code lives in one focused,
independently testable unit. ``HelloFreshClient`` *composes* a ``TokenManager`` (rather than
inheriting auth behaviour) and delegates to it: the manager owns all token state, the
``/gw`` login/refresh HTTP calls, expiry math, and the single concurrency lock.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
from importlib import util
import json
import logging
import math
import re
import secrets
from time import monotonic
from typing import Any, NoReturn
from uuid import uuid4
from weakref import WeakKeyDictionary

from aiohttp import ClientError, ClientResponse, ClientSession

from .const import (
    COUNTRY_BASE_URLS,
    DEFAULT_COUNTRY,
    DEFAULT_LOG_AUTH_DIAGNOSTICS,
    GW_CLIENT_ID,
    api_country_code,
    api_locale,
)
from .models import (
    HelloFreshAuthError,
    HelloFreshBotBlockedError,
    HelloFreshError,
    HelloFreshLoginRejectedError,
    HelloFreshPasswordResetRequiredError,
    HelloFreshRateLimitedError,
)
from .parsers import coerce_int, decode_jwt_claims
from .tls_transport import (
    AuthResponse,
    async_auth_post,
    impersonated_chrome_major,
    tls_impersonation_available,
)

_LOGGER = logging.getLogger(__name__)

# HTTP status thresholds used when interpreting HelloFresh responses.
_HTTP_REDIRECT = 300
_HTTP_TOO_MANY_REQUESTS = 429
_AUTH_FAILURE_STATUSES = frozenset({401, 403})
# An HTML page with one of these statuses is Cloudflare answering in the API's place: a
# challenge served as 200, or a block served as 401, 403 or 429. HTML with any other status
# (a gateway's 502 page, a 404) is an ordinary failed request and starts no cooldown.
_HTML_BLOCK_STATUSES = frozenset({200, 401, 403, 429})
# A dead refresh token. HelloFresh's Android app treats 400, 401 and 403 from /refresh as a
# rejected refresh token and signs out (NewApiAuthenticator); 400 is OAuth 2's answer for an
# invalid grant (RFC 6749 section 5.2). A JSON 4xx naming one of these errors counts too.
_REFRESH_REJECTED_STATUSES = frozenset({400, 401, 403})
_REFRESH_REJECTED_ERRORS = frozenset({"invalid_grant", "invalid_token", "expired_token"})
# /gw/login's answer when the account's password has expired. HelloFresh emails the customer
# instructions to update it (its app's `login.error.revokePassword`).
_PASSWORD_MUST_BE_ROTATED = "access_denied_password_must_be_rotated"
# HelloFresh locks sign-in for "a few minutes" after too many attempts (HTTP 429). No password
# is posted for at least this long, or for the server's Retry-After when it is longer.
_LOGIN_RATE_LIMIT_MIN_SECONDS = 300
_LOGIN_RATE_LIMIT_MAX_SECONDS = 3600
_CF_ERROR_CODE_RE = re.compile(
    r'(?:\bError(?:\s+code)?\s*[:#]?\s*|cf-error-code[^>]*>\s*|["\']code["\']\s*:\s*["\']?)(10\d{2})\b',
    re.IGNORECASE,
)
_CF_RAY_RE = re.compile(r"[0-9a-fA-F]{16,32}(?:-[A-Za-z0-9]{2,8})?")
_CF_RAY_IN_BODY_RE = re.compile(r"Ray\s+ID\s*[:#]\s*([0-9A-Za-z-]{16,48})", re.IGNORECASE)
# What Cloudflare's own challenge page carries, whatever status it comes with (its older
# challenges were served as 503): the challenge options object and the orchestration script.
# The script Cloudflare adds to ordinary pages for JavaScript detections sits under the same
# /cdn-cgi/challenge-platform/ path, so that path alone is not a challenge.
_CF_CHALLENGE_BODY_RE = re.compile(
    r"_cf_chl_opt|/cdn-cgi/challenge-platform/[^\"'\s>]{0,80}orchestrate"
)
_CF_CHALLENGE_BODY_SCAN_CHARS = 65536
# How long closing the manager waits for a refresh that is still in flight (see async_close).
_CLOSE_WAITS_FOR_REFRESH_SECONDS = 10
_BLOCK_BACKOFF_INITIAL_SECONDS = 300
_BLOCK_BACKOFF_MAX_SECONDS = 3600
_BLOCK_BACKOFF_MAX_STEPS = 5
_KNOWN_AUTH_ERRORS = frozenset(
    {
        "access_denied",
        "expired_token",
        "invalid_client",
        "invalid_grant",
        "invalid_request",
        "invalid_token",
        "unauthorized",
        _PASSWORD_MUST_BE_ROTATED,
    }
)


def _auth_error_code(body: str) -> str | None:
    """Return the ``error`` code of a JSON auth error body, or None."""
    try:
        payload = json.loads(body) if body else None
    except ValueError:
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    return error if isinstance(error, str) else None


def _retry_after_seconds(response: Any) -> int | None:
    """Return a ``Retry-After`` header given in seconds, or None.

    The name is matched whatever its case: the curl transport hands its headers over as a
    plain dict with lower-case names, where a ``headers.get("Retry-After")`` finds nothing.
    """
    value = _response_header(response, "Retry-After")
    seconds = coerce_int(value.strip()) if value is not None else None
    return seconds if seconds is not None and seconds >= 0 else None


def _raised_again[ErrorT: BaseException](err: ErrorT) -> ErrorT:
    """Prepare an error that is kept and raised again, possibly for days.

    Each raise adds its frames to the error's traceback, and the traceback keeps those frames
    alive. A refused password or refresh token is raised on every attempt until the user
    reauthenticates, so the traceback is started afresh each time instead of growing.
    """
    return err.with_traceback(None)


def _error_name(err: BaseException) -> str:
    """Name an error for a log line, with the error that caused it when there is one.

    The transport wraps every curl failure as ``ClientError``; the cause is what tells a
    timeout from a refused connection. Type names only, never the message.
    """
    cause = err.__cause__
    if cause is None:
        return type(err).__name__
    return f"{type(err).__name__} ({type(cause).__name__})"


def _safe_auth_error_summary(body: str) -> str:
    """Keep only bounded machine codes from a server-controlled auth error."""
    if not body:
        return "empty body"
    try:
        payload = json.loads(body)
    except ValueError:
        return f"unparseable body ({len(body)} bytes)"
    if not isinstance(payload, dict):
        return f"unparseable body ({len(body)} bytes)"
    error = payload.get("error")
    if isinstance(error, str) and error in _KNOWN_AUTH_ERRORS:
        return f"error={error}"
    return f"JSON body ({len(body)} bytes)"


@dataclass
class _AuthBlockBackoff:
    """One cooldown for the accounts of one HelloFresh site on a Home Assistant event loop."""

    consecutive_blocks: int = 0
    next_attempt_at: float = 0.0

    def raise_if_blocked(self) -> None:
        remaining = math.ceil(self.next_attempt_at - monotonic())
        if remaining > 0:
            raise HelloFreshBotBlockedError(
                f"HelloFresh authentication paused after bot protection block; "
                f"retry in {remaining} seconds"
            )

    def record_block(self) -> int:
        now = monotonic()
        if now < self.next_attempt_at:
            # Concurrent requests already in flight when the first block arrived belong
            # to the same burst; do not multiply the delay for each response.
            return math.ceil(self.next_attempt_at - now)
        self.consecutive_blocks = min(self.consecutive_blocks + 1, _BLOCK_BACKOFF_MAX_STEPS)
        delay = min(
            _BLOCK_BACKOFF_INITIAL_SECONDS * 2 ** (self.consecutive_blocks - 1),
            _BLOCK_BACKOFF_MAX_SECONDS,
        )
        self.next_attempt_at = now + delay
        return delay

    def reset_after_success(self) -> None:
        # An older request that completes during a cooldown must not cancel it.
        if monotonic() >= self.next_attempt_at:
            self.consecutive_blocks = 0
            self.next_attempt_at = 0.0


@dataclass
class _SharedAuthState:
    """The sign-in pauses every client on one Home Assistant event loop has to honour.

    Each config-flow submit and each entry reload builds a new client. A pause kept on the
    client alone would be forgotten at exactly those moments, and the next attempt would go
    out while HelloFresh is still blocking or locking the last one.
    """

    # Bot-protection cooldowns by regional base URL. Cloudflare blocks one site at a time, so
    # a block on one country's host is no reason to stop talking to another's.
    block_backoffs: dict[str, _AuthBlockBackoff] = field(default_factory=dict)
    # monotonic() time before which no password is posted after a 429 lockout, by base URL
    # and a digest of the account's email (the address itself is not kept here).
    login_locks: dict[tuple[str, str], float] = field(default_factory=dict)

    def block_backoff(self, base_url: str) -> _AuthBlockBackoff:
        """Return the cooldown for one HelloFresh site, creating it on first use."""
        backoff = self.block_backoffs.get(base_url)
        if backoff is None:
            backoff = self.block_backoffs[base_url] = _AuthBlockBackoff()
        return backoff

    def lock_login(self, key: tuple[str, str], until: float) -> None:
        """Record a sign-in lockout, dropping the ones that have already run out."""
        now = monotonic()
        for stale in [known for known, deadline in self.login_locks.items() if deadline <= now]:
            del self.login_locks[stale]
        self.login_locks[key] = until


_SESSION_AUTH_STATE: WeakKeyDictionary[ClientSession, _SharedAuthState] = WeakKeyDictionary()
_LOOP_AUTH_STATE: WeakKeyDictionary[asyncio.AbstractEventLoop, _SharedAuthState] = (
    WeakKeyDictionary()
)


def _shared_auth_state(session: ClientSession) -> _SharedAuthState:
    """Return the pauses shared by every client on this session's event loop."""
    try:
        owner = getattr(session, "_loop", None) or session
        # Keyed by the event loop when the session names one, else by the session itself.
        registry: WeakKeyDictionary[Any, _SharedAuthState] = (
            _LOOP_AUTH_STATE
            if isinstance(owner, asyncio.AbstractEventLoop)
            else _SESSION_AUTH_STATE
        )
        state = registry.get(owner)
        if state is None:
            state = _SharedAuthState()
            registry[owner] = state
        return state
    except TypeError:
        # Test stubs and unusual sessions may not support weak references.
        return _SharedAuthState()


def _backoff_for_session(session: ClientSession, base_url: str) -> _AuthBlockBackoff:
    """Share one site's IP-level cooldown across HA entries on the same event loop."""
    return _shared_auth_state(session).block_backoff(base_url)


def _login_lock_key(base_url: str, username: str) -> tuple[str, str]:
    """Identify one account's sign-in on one site without keeping its email address."""
    return base_url, hashlib.sha256(username.strip().lower().encode()).hexdigest()


# Proactively refresh the access token once it has passed this fraction of its lifetime.
# A wide refresh window (e.g. the back half of a 30-min token) means the periodic refresh
# timer reliably catches the token before it expires, regardless of tick phase. The
# absolute floor guards against refreshing pointlessly often for unusually long tokens.
_TOKEN_REFRESH_AT_LIFETIME_FRACTION = 0.5
_TOKEN_MIN_REMAINING_BEFORE_REFRESH = 300  # always refresh within 5 min of expiry

# How long a failed refresh/login is reused instead of retried. A poll fans out dozens of
# requests; the ones that arrive after a failed attempt would each repeat it in turn -- with
# stored credentials, a burst of password logins. Shorter than the 2-10 min refresh timer, so
# every timer tick still makes a real attempt.
_REFRESH_FAILURE_REUSE_SECONDS = 60

# A browser-like User-Agent. HelloFresh fronts its endpoints with bot protection that
# fingerprints non-browser clients; a recognizable headless UA (e.g. "HomeAssistant-...")
# gets challenged with an HTML block page instead of a JSON API response. Presenting a
# current browser UA -- together with the Client Hints and Sec-Fetch metadata a real
# Chrome always emits (see _BROWSER_CLIENT_HINTS below) -- is a best-effort way to pass
# that layer. It can break whenever the protection is retuned: see _looks_like_bot_block
# for how a block is handled when it does.
#
# We present as Google Chrome on Windows. Notes on staying internally consistent, since
# mismatched fields are exactly what bot-protection fingerprinting looks for:
#   * The Chrome major version MUST match across the TLS/HTTP2 fingerprint, the UA string and
#     the Sec-CH-UA brand list. It is the version curl_cffi impersonates (resolved at import,
#     so a curl_cffi upgrade moves all three together), else the fallback below.
#   * Only the low-entropy client hints are sent: on HelloFresh's /gw XHRs real Chrome sends
#     no others (HAR, Chrome 153), despite the site's Accept-CH asking for more.
# Without curl_cffi the TLS fingerprint is aiohttp's, not Chrome's, so the fallback only needs
# to be a plausible current Chrome; bump it when raising the curl_cffi requirement.
_FALLBACK_CHROME_MAJOR_VERSION = 150
_CHROME_MAJOR_VERSION = impersonated_chrome_major() or _FALLBACK_CHROME_MAJOR_VERSION


def _sec_ch_ua(major: int) -> str:
    """Return the Sec-CH-UA brand list Google Chrome sends at this major version.

    Chrome rotates the list with its major version (Chromium's GREASE algorithm): the "Not A
    Brand" spelling, its version and the order of the three brands all change, so a fixed string
    matches one release only. Checked against Chrome 153 itself and curl_cffi's chrome150.
    """
    chars = " (:-./);=?_"
    grease = f'"Not{chars[major % 11]}A{chars[(major + 1) % 11]}Brand";v="{("8", "99", "24")[major % 3]}"'
    order = ((0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0))[major % 6]
    brands = [""] * 3
    brands[order[0]] = grease
    brands[order[1]] = f'"Chromium";v="{major}"'
    brands[order[2]] = f'"Google Chrome";v="{major}"'
    return ", ".join(brands)


_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{_CHROME_MAJOR_VERSION}.0.0.0 Safari/537.36"
)

_AUTH_USER_AGENT = _BROWSER_USER_AGENT

# User-Agent Client Hints and Sec-Fetch metadata that current Chrome sends on every XHR.
# Their absence is a strong "not a real browser" tell, so we send them alongside the UA.
# Sec-Fetch-Site is "same-origin" because every request targets the same regional host the
# Origin/Referer point at.
_BROWSER_CLIENT_HINTS = {
    "sec-ch-ua": _sec_ch_ua(_CHROME_MAJOR_VERSION),
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


def _browser_accept_language(locale: str) -> str:
    """Build a browser language preference from the account's regional locale."""
    language = locale.split("-", 1)[0]
    if language == "en":
        return f"{locale},en;q=0.9" if locale != "en-US" else "en-US,en;q=0.9"
    return f"{locale},{language};q=0.9,en-US;q=0.8,en;q=0.7"


def _browser_accept_encoding() -> str:
    """Build an Accept-Encoding that the aiohttp fallback can decode.

    The curl transport overrides this with libcurl's ``gzip, deflate, br, zstd`` option,
    which also decodes responses. aiohttp needs matching Python decoders, so this list is
    gated by what's installed (Brotli is pinned in the integration manifest).
    """
    encodings = ["gzip", "deflate"]
    if util.find_spec("brotli") or util.find_spec("brotlicffi"):
        encodings.append("br")
    if util.find_spec("zstandard") or util.find_spec("zstd"):
        encodings.append("zstd")
    return ", ".join(encodings)


# Safe fallback encoding header, computed once at import from the Python decoders available to
# aiohttp (see above). No Cache-Control/Pragma: the captures that showed them had DevTools'
# "Disable cache" on (static assets carried them too); Chrome's normal XHRs send neither.
_BROWSER_FETCH_HEADERS = {
    "Accept-Encoding": _browser_accept_encoding(),
}

# The viewport client hints Chrome sends on every same-origin XHR once the site has asked for
# them (HelloFresh's pages do). A common Windows desktop, matching the User-Agent.
_BROWSER_DEVICE_HINTS = {
    "DPR": "1",
    "Viewport-Width": "1280",
}


def _tracing_headers(*, request_id: bool = True) -> dict[str, str]:
    """Return fresh distributed-tracing headers, as the web app's API client sets per request.

    The sign-in page's own HTTP client sends no ``x-request-id`` (HAR 59).
    """
    trace_id = secrets.token_hex(16)
    span_id = secrets.token_hex(8)
    headers = {
        "traceparent": f"00-{trace_id}-{span_id}-01",
        "x-b3-traceid": trace_id,
        "x-b3-spanid": span_id,
        "x-b3-sampled": "1",
    }
    if request_id:
        headers["x-request-id"] = str(uuid4())
    return headers


def _token_fingerprint(token: str | None) -> str:
    """Return a short, non-reversible fingerprint of a token for debug logging.

    Logs the first 8 hex chars of a SHA-256 digest -- enough to tell whether the
    refresh token *changed* between rotations without ever writing the secret to
    logs. Never log the token itself.
    """
    if not token:
        return "none"
    return hashlib.sha256(token.encode()).hexdigest()[:8]


def _response_header(response: Any, name: str) -> str | None:
    """Read a response header across aiohttp's and curl_cffi's header containers."""
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        for key, value in headers.items():
            if str(key).lower() == name.lower() and isinstance(value, str):
                return value
    except AttributeError:  # pragma: no cover - defensive
        return None
    return None


def _response_content_type(response: Any) -> str | None:
    """Return a response's Content-Type header, tolerating stubs without ``headers``."""
    return _response_header(response, "content-type")


def _cf_error_code(body: str) -> str | None:
    """Extract a Cloudflare 10xx error number without returning any response text."""
    match = _CF_ERROR_CODE_RE.search(body[:8192])
    return match[1] if match else None


def _has_cf_challenge(response: Any) -> bool:
    """Return whether Cloudflare marked this response as a challenge."""
    return (_response_header(response, "cf-mitigated") or "").strip().lower() == "challenge"


def _cloudflare_diagnostics(response: Any, body: str) -> str:
    """Format only bounded Cloudflare markers; never log HTML or arbitrary headers."""
    challenged = _has_cf_challenge(response)
    code = _cf_error_code(body)
    code_name = (
        {"1010": "browser signature block", "1020": "firewall rule"}.get(code)
        if code is not None
        else None
    )
    code_label = f"{code} ({code_name})" if code_name else code or "none"
    ray_candidate = _response_header(response, "cf-ray") or ""
    if not ray_candidate and (match := _CF_RAY_IN_BODY_RE.search(body[:8192])) is not None:
        ray_candidate = match[1]
    ray = ray_candidate if _CF_RAY_RE.fullmatch(ray_candidate) else "none"
    return (
        f"; cf-mitigated={'challenge' if challenged else 'none'}, "
        f"cf-error={code_label}, ray-id={ray}"
    )


def _looks_like_bot_block(content_type: str | None, body: str) -> bool:
    """Return True when a response is an HTML page instead of the API's JSON.

    HelloFresh's API answers with JSON. An HTML response on a 401/403 is almost always the
    edge bot-protection layer (a Cloudflare challenge page) rejecting a non-browser request
    *before* it reaches the login API -- not the API rejecting the credentials. Telling the
    two apart keeps a transient block from being surfaced to the user as "wrong password"
    (which would trigger a pointless reauth loop). Whether an HTML page is a block depends on
    its status too: see ``_looks_like_auth_block``.
    """
    if content_type and "html" in content_type.lower():
        return True
    head = body[:512].lstrip().lower()
    return head.startswith("<!doctype html") or head.startswith("<html")


def _has_cf_challenge_page(body: str) -> bool:
    """Return whether a body is Cloudflare's challenge page."""
    return _CF_CHALLENGE_BODY_RE.search(body[:_CF_CHALLENGE_BODY_SCAN_CHARS]) is not None


def _looks_like_auth_block(response: Any, body: str) -> bool:
    """Tell a bot-protection block from a request that merely failed.

    Cloudflare marks its own answers: the ``cf-mitigated: challenge`` header, error 1010 or
    1020, or its challenge page. Without a marker, an HTML page counts only on the statuses a
    block comes with. Every HTML response used to count, so a gateway's ordinary 502 page
    paused all requests for five minutes to an hour.
    """
    if (
        _has_cf_challenge(response)
        or _cf_error_code(body) in {"1010", "1020"}
        or _has_cf_challenge_page(body)
    ):
        return True
    if getattr(response, "status", None) not in _HTML_BLOCK_STATUSES:
        return False
    return _looks_like_bot_block(_response_content_type(response), body)


async def _async_body_text(response: Any) -> str:
    """Read a response body to classify it; one that cannot be read counts as empty."""
    try:
        return await response.text()
    except (ClientError, TimeoutError, UnicodeDecodeError):
        return ""


class TokenManager:
    """Owns the HelloFresh access/refresh token lifecycle.

    State and the ``/gw`` auth HTTP calls live here. ``HelloFreshClient`` reads the current
    access token / authorization header from this manager for each request and asks it to
    refresh proactively (on a timer) and reactively (after a 401).
    """

    def __init__(
        self,
        session: ClientSession,
        country: str = DEFAULT_COUNTRY,
        access_token: str | None = None,
        refresh_token: str | None = None,
        token_issued_at: int | str | None = None,
        token_expires_in: int | str | None = None,
        refresh_expires_in: int | str | None = None,
        refresh_token_issued_at: int | str | None = None,
        token_type: str | None = None,
        username: str | None = None,
        password: str | None = None,
        token_refresh_callback: Callable[[dict[str, Any]], None] | None = None,
        use_curl_cffi_headers: bool = False,
        log_auth_diagnostics: bool = DEFAULT_LOG_AUTH_DIAGNOSTICS,
    ) -> None:
        """Initialize token state from the persisted config-entry values."""
        self._session = session
        self._use_curl_cffi_headers = use_curl_cffi_headers
        self._log_auth_diagnostics = log_auth_diagnostics
        self._country = country
        self._access_token = (access_token or "").strip()
        self._token_type = (token_type or "Bearer").strip() or "Bearer"
        self._refresh_token = (refresh_token or "").strip()
        self._token_issued_at = coerce_int(token_issued_at)
        self._token_expires_in = coerce_int(token_expires_in)
        self._refresh_expires_in = coerce_int(refresh_expires_in)
        # When the login wrapper didn't supply explicit timing (e.g. a bare access
        # token was pasted), fall back to the JWT's own iat/exp claims so expiry can
        # still be surfaced. These claims are read for diagnostics only, never trusted
        # for authorization. This MUST run before deriving _refresh_token_issued_at below,
        # which falls back to _token_issued_at.
        if self._token_issued_at is None or self._token_expires_in is None:
            self._apply_jwt_token_timing(self._access_token)
        # The refresh token's 60-day clock is anchored to when the *refresh token* was
        # issued, NOT to the short-lived access token's issue time (which resets on every
        # access-token refresh). Older entries only stored a single ``issued_at`` from the
        # original login -- which was also the refresh token's issue time -- so fall back to
        # it (now JWT-populated above) for back-compat.
        self._refresh_token_issued_at = (
            coerce_int(refresh_token_issued_at)
            if refresh_token_issued_at is not None
            else self._token_issued_at
        )
        self._username = (username or "").strip()
        self._password = password or ""
        self._base_url = COUNTRY_BASE_URLS.get(country, COUNTRY_BASE_URLS[DEFAULT_COUNTRY])
        self._token_refresh_callback = token_refresh_callback
        # The one refresh in flight, which every caller waits on (see _async_refresh).
        self._refresh_task: asyncio.Task[HelloFreshError | None] | None = None
        # The last failed refresh/login and when it may be retried (see _async_refresh), and
        # the failure already reported in the log, so one failure is one warning.
        self._refresh_failure: HelloFreshError | None = None
        self._refresh_failure_until = 0.0
        self._warned_refresh_failure: HelloFreshError | None = None
        # Set once /gw/login has refused the stored email and password. They cannot start
        # working again on their own, so nothing posts them a second time; reauth and
        # reconfigure build a new manager with whatever the user enters.
        self._credentials_rejected: HelloFreshLoginRejectedError | None = None
        # Set once /gw/refresh has refused the refresh token, which is then forgotten.
        self._refresh_rejection: HelloFreshAuthError | None = None
        # Whether the current access token has been sent on a request since it was issued.
        # The refresh timer renews only a token that is in use (see async_refresh_if_in_use).
        self._token_in_use = False
        # The bot-block cooldown and the 429 sign-in lock outlive this manager.
        self._auth_state = _shared_auth_state(session)
        self._block_backoff = self._auth_state.block_backoff(self._base_url)
        self._login_lock_key = _login_lock_key(self._base_url, self._username)
        # Auth POSTs use a Chrome-TLS-impersonating transport when curl_cffi is installed,
        # which is what gets past Cloudflare bot-management on stricter regions (e.g. UK).
        # Log it once so a blocked user can confirm whether the impersonation path is active.
        _LOGGER.debug(
            "HelloFresh auth transport: %s",
            "curl_cffi (Chrome TLS impersonation)"
            if tls_impersonation_available()
            else "aiohttp (no TLS impersonation; install curl_cffi to bypass strict WAFs)",
        )

    # ------------------------------------------------------------------
    # Public surface used by the client
    # ------------------------------------------------------------------

    @property
    def access_token(self) -> str:
        """Return the current access token (may be empty)."""
        return self._access_token

    @property
    def has_token(self) -> bool:
        """Return True when an access token is currently held."""
        return bool(self._access_token)

    def authorization_header(self) -> str:
        """Return the ``Authorization`` header value for an authenticated request.

        Building the header is what puts the token on a request, so it also marks the token
        as in use for the refresh timer.
        """
        self._token_in_use = True
        return f"{self._token_type} {self._access_token}"

    def access_token_valid(self) -> bool:
        """Return True while the current access token has comfortable life left."""
        return self._access_token_still_valid()

    def raise_if_bot_blocked(self) -> None:
        """Pause new API calls while this HelloFresh site is cooling down from a block."""
        self._block_backoff.raise_if_blocked()

    async def async_raise_if_data_blocked(
        self, response: ClientResponse | AuthResponse, *, public_page: bool = False
    ) -> None:
        """Record a Cloudflare block before a data 401/403 can trigger token refresh.

        ``public_page`` marks a website page or Next.js route, which is HTML by design: only a
        blocking status or an explicit challenge marker counts there. Treating every HTML
        response as a block made each build-id scrape and public-menu load pause all requests.
        An HTML error page on a data call (a gateway's 502, a 404) is left for the caller to
        report as the failed request it is.
        """
        if (
            response.status not in {401, 403, 429}
            and not _has_cf_challenge(response)
            and (public_page or "html" not in (_response_content_type(response) or "").lower())
        ):
            return
        self._raise_if_bot_block("data request", response, await _async_body_text(response))

    def _raise_if_bot_block(
        self,
        context: str,
        response: ClientResponse | AuthResponse,
        body: str,
        *,
        hint: str = "",
        cause: BaseException | None = None,
    ) -> None:
        """Start the shared cooldown and raise when a response is a bot-protection block.

        Every request that can be blocked reports it here, so one rule decides what a block
        is and one line in the log says so. ``hint`` adds what only that request knows.
        """
        if _looks_like_auth_block(response, body):
            self._raise_bot_block(context, response, body, hint=hint, cause=cause)

    def _raise_bot_block(
        self,
        context: str,
        response: ClientResponse | AuthResponse,
        body: str,
        *,
        hint: str = "",
        cause: BaseException | None = None,
    ) -> NoReturn:
        """Start (or keep) the shared cooldown for a blocked request and raise the block."""
        delay = self._block_backoff.record_block()
        _LOGGER.warning(
            "HelloFresh %s BLOCKED by bot protection (HTTP %s); requests paused for %s seconds%s%s",
            context,
            response.status,
            delay,
            hint,
            self._auth_log_details(response, body),
        )
        raise HelloFreshBotBlockedError(
            f"HelloFresh {context} blocked by bot protection: HTTP {response.status}"
        ) from cause

    @property
    def can_obtain_token(self) -> bool:
        """Return True when a token can be (re)obtained via refresh or login."""
        return bool(self._refresh_token) or self._has_credentials

    @property
    def has_refresh_token(self) -> bool:
        """Return True when a refresh token is held."""
        return bool(self._refresh_token)

    @property
    def has_credentials(self) -> bool:
        """Return True when an email and password are stored for signing in."""
        return self._has_credentials

    @property
    def token_lifetime_seconds(self) -> int | None:
        """Return the access token's configured lifetime in seconds, if known."""
        return self._token_expires_in

    @property
    def token_expires_at(self) -> datetime | None:
        """Return the access token's UTC expiry time, if known."""
        if self._token_issued_at is None or self._token_expires_in is None:
            return None
        return datetime.fromtimestamp(self._token_issued_at + self._token_expires_in, tz=UTC)

    @property
    def refresh_token_expires_at(self) -> datetime | None:
        """Return the refresh token's UTC expiry time, if known."""
        if self._refresh_token_issued_at is None or self._refresh_expires_in is None:
            return None
        return datetime.fromtimestamp(
            self._refresh_token_issued_at + self._refresh_expires_in, tz=UTC
        )

    async def async_ensure_fresh(self) -> None:
        """Refresh the access token ahead of a request when it is missing or near expiry.

        Does nothing when there is nothing to renew with, or the current token still has
        comfortable life.
        """
        if not self._refresh_token and not self._has_credentials:
            # Nothing to renew with: no refresh token to exchange and no credentials to log
            # in. Once HelloFresh has refused the refresh token and the access token has run
            # out too, a request could only collect a 401, so it is not sent.
            if self._refresh_rejection is not None and self._access_token_expired():
                raise _raised_again(self._refresh_rejection)
            return
        if not self._access_token:
            await self._async_refresh(force=True)
            return
        if not self._token_expiring_soon():
            return
        if self._refresh_recently_failed() and self._access_token_still_valid():
            # A refresh failed moments ago and the token still works; the next attempt is
            # made once the reuse window has passed.
            return
        # Half-life refresh. If it fails but the current access token is still genuinely
        # valid -- e.g. right after a reboot, when the stored token has life left but the
        # refresh token was already rotated in a prior session, or /gw/refresh briefly
        # answered 5xx -- keep using the existing token rather than failing the request. The
        # reactive 401 path surfaces a real expiry later. A bot block still propagates: its
        # backoff pauses every request, not just this refresh.
        try:
            await self._async_refresh(force=False)
        except HelloFreshBotBlockedError:
            raise
        except HelloFreshError as err:
            if not self._access_token_still_valid():
                raise
            if self._warned_refresh_failure is not err:
                # Every request waiting on the failed refresh lands here; say it once.
                self._warned_refresh_failure = err
                _LOGGER.warning(
                    "HelloFresh proactive token refresh failed; continuing with "
                    "the existing access token until it actually expires"
                )

    async def async_refresh_if_in_use(self) -> None:
        """Keep the access token fresh for the refresh timer, while something is using it.

        Safe to call on a timer independent of data polling. A token that no request has
        used since it was issued is left to lapse, and the next request renews it on demand
        (``async_ensure_fresh``). Renewing unconditionally meant a /gw/refresh every 15 to 22
        minutes around the clock, each one rotating the refresh token and rewriting the
        config entry, for an account that is polled a few times a day.
        """
        if self._token_in_use:
            await self.async_ensure_fresh()

    async def async_force_refresh_if_unchanged(self, token_before: str) -> None:
        """Force a refresh, unless the token has already been rotated since ``token_before``.

        Called from the reactive 401 path. When several requests 401 at once (the
        coordinator fetches many endpoints concurrently), only the first should rotate the
        refresh token: HelloFresh invalidates the old refresh token on use, so a second
        forced rotation would burn the token the first one just obtained. They all wait on
        the same refresh; if the access token already changed, the caller just retries with it.
        """
        if self._access_token == token_before:
            await self._async_refresh(force=True)

    async def async_close(self) -> None:
        """Give a refresh that is still in flight a moment to finish and save its tokens.

        Call this before closing the HTTP sessions. Closing them under a /gw/refresh that
        HelloFresh has already answered would lose the new refresh token, and the old one no
        longer works.
        """
        task = self._refresh_task
        if task is not None and not task.done():
            await asyncio.wait({task}, timeout=_CLOSE_WAITS_FOR_REFRESH_SECONDS)

    def _refresh_recently_failed(self) -> bool:
        """Return True while the last failed refresh/login is still being reused."""
        return self._refresh_failure is not None and monotonic() < self._refresh_failure_until

    async def _async_refresh(self, force: bool) -> None:
        """Wait for a refresh: the one already in flight, or else a new one.

        The refresh runs as a task of its own and every caller waits on it through a shield.
        A caller that is cancelled (Home Assistant cancels the poll when an entry unloads)
        leaves the refresh running: HelloFresh spends the old refresh token as it answers, so
        the new one must still be adopted and saved, and whoever asks next joins that same
        task instead of posting the spent token again.

        A refresh that failed moments ago is re-raised rather than repeated. Joining covers
        the requests that were waiting; this covers the rest of the poll, which would
        otherwise repeat the whole refresh-then-login chain, a burst of password logins.
        """
        failure = self._refresh_failure
        if failure is not None and monotonic() < self._refresh_failure_until:
            raise failure
        task = self._refresh_task
        if task is None or task.done():
            task = asyncio.get_running_loop().create_task(self._async_refresh_outcome(force))
            # The event loop keeps only a weak reference to a task; this one keeps it alive.
            self._refresh_task = task
        outcome = await asyncio.shield(task)
        if outcome is not None:
            raise outcome

    async def _async_refresh_outcome(self, force: bool) -> HelloFreshError | None:
        """Run one refresh to its end and return its failure, if any, instead of raising it.

        Returned, not raised: when every caller has been cancelled nobody collects the
        task's result, and asyncio reports an exception left on a task as a bug.
        """
        try:
            await self._async_refresh_access_token(force=force)
        except HelloFreshBotBlockedError as err:
            return err  # the bot-block backoff already paces these
        except HelloFreshError as err:
            self._refresh_failure = err
            self._refresh_failure_until = monotonic() + _REFRESH_FAILURE_REUSE_SECONDS
            return err
        self._refresh_failure = None
        return None

    # ------------------------------------------------------------------
    # Expiry math
    # ------------------------------------------------------------------

    def _access_token_still_valid(self) -> bool:
        """Return True when the current access token has not yet hard-expired."""
        if not self._access_token:
            return False
        if self._token_issued_at is None or self._token_expires_in is None:
            # No timing metadata: assume the token is worth trying rather than discarding it.
            return True
        now = datetime.now(UTC).timestamp()
        expires_at = self._token_issued_at + self._token_expires_in
        # Require a small safety margin so we don't hand out a token about to expire mid-request.
        return now < expires_at - _TOKEN_MIN_REMAINING_BEFORE_REFRESH

    def _access_token_expired(self) -> bool:
        """Return True when there is no access token, or it is known to be past its expiry."""
        if not self._access_token:
            return True
        if self._token_issued_at is None or self._token_expires_in is None:
            return False
        return datetime.now(UTC).timestamp() >= self._token_issued_at + self._token_expires_in

    def _apply_jwt_token_timing(self, access_token: str) -> None:
        """Derive issued-at/expires-in from the access token's JWT claims when absent."""
        claims = decode_jwt_claims(access_token)
        if claims is None:
            return
        issued_at = coerce_int(claims.get("iat"))
        expires_at = coerce_int(claims.get("exp"))
        if self._token_issued_at is None and issued_at is not None:
            self._token_issued_at = issued_at
        if self._token_expires_in is None and expires_at is not None:
            # Prefer measuring against the JWT's own issued-at so the lifetime is exact.
            base = issued_at if issued_at is not None else self._token_issued_at
            if base is not None and expires_at > base:
                self._token_expires_in = expires_at - base

    def _token_expiring_soon(self) -> bool:
        """Return True when the access token should be refreshed (or metadata is absent).

        Refreshes once the token is past half its lifetime, OR within
        ``_TOKEN_MIN_REMAINING_BEFORE_REFRESH`` seconds of expiry (whichever comes first).
        The half-life window is deliberately wide so the periodic refresh timer always
        lands inside it before the token can actually expire.
        """
        if self._token_issued_at is None or self._token_expires_in is None:
            return True
        now = datetime.now(UTC).timestamp()
        expires_at = self._token_issued_at + self._token_expires_in
        half_life_at = (
            self._token_issued_at + self._token_expires_in * _TOKEN_REFRESH_AT_LIFETIME_FRACTION
        )
        refresh_at = min(half_life_at, expires_at - _TOKEN_MIN_REMAINING_BEFORE_REFRESH)
        return now >= refresh_at

    def _refresh_token_expired(self) -> bool:
        """Return True when the refresh token has passed its known lifetime.

        Anchored to when the *refresh token* was issued (login, or the last rotation that
        returned a new refresh token), not the access token's issue time.
        """
        if not self._refresh_token:
            return True
        if self._refresh_token_issued_at is None or self._refresh_expires_in is None:
            return False
        refresh_expires_at = self._refresh_token_issued_at + self._refresh_expires_in
        return datetime.now(UTC).timestamp() >= refresh_expires_at

    # ------------------------------------------------------------------
    # /gw auth HTTP calls
    # ------------------------------------------------------------------

    def _auth_query(self) -> dict[str, str]:
        """Return the ``country``/``locale`` query the /gw auth endpoints expect."""
        return {"country": api_country_code(self._country), "locale": api_locale(self._country)}

    def _auth_headers(self, *, content_type: str | None, login: bool = False) -> dict[str, str]:
        """Return browser-like headers for the /gw auth POSTs.

        Includes ``Origin``/``Referer`` derived from the regional base URL so the request
        resembles the web app's XHR and is less likely to trip bot protection.
        ``content_type`` is None for a POST without a body, which a browser sends without one.

        The app token and refresh come from the web app's fetch() client: ``Accept: */*`` and
        an ``x-request-id``. The sign-in page posts ``/gw/login`` with its own axios client,
        which sets the axios ``Accept`` and no ``x-request-id`` (HAR 59).
        """
        return {
            "Accept": "application/json, text/plain, */*" if login else "*/*",
            "Accept-Language": _browser_accept_language(api_locale(self._country)),
            **({"Content-Type": content_type} if content_type else {}),
            "User-Agent": _AUTH_USER_AGENT,
            "Origin": self._base_url,
            "Referer": f"{self._base_url}/login",
            "Priority": "u=1, i",
            **_BROWSER_FETCH_HEADERS,
            **_BROWSER_CLIENT_HINTS,
            **_BROWSER_DEVICE_HINTS,
            **_tracing_headers(request_id=not login),
        }

    def _auth_log_details(self, response: ClientResponse | AuthResponse, body: str) -> str:
        """Return opt-in Cloudflare details for a failed auth response."""
        return _cloudflare_diagnostics(response, body) if self._log_auth_diagnostics else ""

    def _log_auth_success(self, endpoint: str, status: int) -> None:
        """Record a successful auth step at debug level.

        Never a warning: the token refresh timer succeeds every few minutes, and with the
        diagnostics option on by default that flooded the HA log with HTTP 200 lines (#13).
        """
        _LOGGER.debug("HelloFresh authentication diagnostic: %s returned HTTP %s", endpoint, status)

    def _log_transport_error(self, endpoint: str, err: BaseException) -> None:
        """Report an auth request that never got an answer, when diagnostics are on."""
        if self._log_auth_diagnostics:
            _LOGGER.warning(
                "HelloFresh authentication %s transport error: %s", endpoint, _error_name(err)
            )

    async def _async_refresh_access_token(self, force: bool) -> None:
        """Obtain a fresh access token.

        Mirrors the HelloFresh web app: renew via ``POST /gw/refresh`` when a live refresh
        token is available, otherwise (no/expired refresh token, or a rejected refresh)
        fall back to a full username/password login.

        Once HelloFresh has refused the stored password, nothing is sent at all, a refresh
        token this manager still holds included. The entry is waiting for the user to
        reauthenticate by then, and reauth builds a new manager.
        """
        if self._credentials_rejected is not None:
            raise _raised_again(self._credentials_rejected)
        self._block_backoff.raise_if_blocked()
        if self._refresh_token and not self._refresh_token_expired():
            refresh_token_fingerprint = _token_fingerprint(self._refresh_token)
            try:
                await self._async_refresh_with_token()
                return
            except HelloFreshAuthError as err:
                # Refresh token was rejected (dead/reused/rotated-away). Fall through to a
                # fresh login if credentials are available; otherwise surface the failure.
                _LOGGER.debug(
                    "HelloFresh /gw/refresh rejected (refresh_token fp=%s): %s; "
                    "falling back to login",
                    refresh_token_fingerprint,
                    err,
                )
                if not self._has_credentials:
                    raise
        await self._async_login(force=force)

    @property
    def _has_credentials(self) -> bool:
        """Return True when a username and password are configured for login."""
        return bool(self._username and self._password)

    @staticmethod
    def _refresh_was_rejected(status: int, error_body: str) -> bool:
        """Return True when a failed ``/gw/refresh`` means the refresh token is dead.

        400, 401 and 403 count, as in HelloFresh's Android app, and so does any other JSON
        4xx naming an invalid grant or token. A 429 is a rate limit, never a rejection.
        Bot-protection blocks are recognised before this is asked.
        """
        if status == _HTTP_TOO_MANY_REQUESTS:
            return False
        if status in _REFRESH_REJECTED_STATUSES:
            return True
        return 400 <= status < 500 and _auth_error_code(error_body) in _REFRESH_REJECTED_ERRORS

    async def _async_refresh_with_token(self) -> None:
        """Renew the access token via ``POST {base}/gw/refresh``."""
        self._block_backoff.raise_if_blocked()
        refresh_token = self._refresh_token
        try:
            response = await async_auth_post(
                self._session,
                f"{self._base_url}/gw/refresh",
                params=self._auth_query(),
                json_payload={"refresh_token": refresh_token},
                headers=self._auth_headers(content_type="text/plain;charset=UTF-8"),
                use_curl_cffi_headers=self._use_curl_cffi_headers,
            )
        except (ClientError, TimeoutError) as err:
            # Network-level failure (DNS, connect, mid-request timeout): transient, NOT a
            # rejected refresh token. Wrapping it keeps callers' error handling intact —
            # unwrapped it escaped the config flow ("Unknown error" instead of
            # "cannot_connect") and the proactive-refresh timer (unhandled traceback).
            self._log_transport_error("/gw/refresh", err)
            raise HelloFreshError(
                f"HelloFresh token refresh could not connect: {_error_name(err)}"
            ) from err
        # A redirect is not followed (the body is the refresh token), so it is a failure too.
        if response.status >= _HTTP_REDIRECT or _has_cf_challenge(response):
            error_body = await _async_body_text(response)
            # An edge block keeps the refresh token and pauses auth requests.
            self._raise_if_bot_block(
                "token refresh",
                response,
                error_body,
                hint=f" (refresh_token fp={_token_fingerprint(refresh_token)})",
            )
            # The auth API answers in JSON. A web page that is not a block (a gateway's error
            # page) did not come from it, and says nothing about the refresh token.
            from_api = not _looks_like_bot_block(_response_content_type(response), error_body)
            if from_api and self._refresh_was_rejected(response.status, error_body):
                _LOGGER.warning(
                    "HelloFresh token refresh REJECTED HTTP %s: %s (refresh_token fp=%s, "
                    "refresh_token_issued_at=%s)%s",
                    response.status,
                    # Never the raw body: it is server-controlled free text and logs are not
                    # redacted the way diagnostics exports are.
                    _safe_auth_error_summary(error_body),
                    _token_fingerprint(refresh_token),
                    self._refresh_token_issued_at,
                    self._auth_log_details(response, error_body),
                )
                rejection = HelloFreshAuthError(
                    f"HelloFresh token refresh failed: HTTP {response.status}"
                )
                if self._refresh_token == refresh_token:
                    # HelloFresh has said this refresh token is dead. Forgetting it keeps the
                    # next attempt from posting it again; only this manager forgets it, so a
                    # restart reads the stored one and asks once more.
                    self._refresh_token = ""
                    self._refresh_rejection = rejection
                raise rejection
            # Treat everything else (5xx, 408, 429) as transient: the current access token may
            # still work until it expires, at which point the reactive 401-retry path surfaces a
            # proper auth failure. Raising HelloFreshError (not Auth) avoids a spurious login.
            _LOGGER.warning(
                "HelloFresh token refresh failed (transient); will retry: HTTP %s (%s)%s",
                response.status,
                _safe_auth_error_summary(error_body),
                self._auth_log_details(response, error_body),
            )
            raise HelloFreshError(
                f"HelloFresh token refresh transient failure: HTTP {response.status}"
            )

        payload = await self._async_auth_payload(response, context="token refresh")
        self._apply_auth_object(payload, context="refresh")
        self._log_auth_success("/gw/refresh", response.status)
        self._block_backoff.reset_after_success()

    async def _async_login(self, force: bool) -> None:
        """Authenticate with username/password via the /gw auth gateway.

        Mirrors the web app: fetch an anonymous app token, then POST credentials to
        ``/gw/login``. The returned auth object carries a fresh access + refresh token.
        """
        if not self._has_credentials:
            if force:
                raise HelloFreshAuthError(
                    "HelloFresh credentials are required to reauthenticate, but none are configured"
                )
            return
        if self._credentials_rejected is not None:
            # HelloFresh has already refused this email and password. Posting them again
            # cannot succeed, and repeated attempts are what lock an account.
            raise _raised_again(self._credentials_rejected)

        self._block_backoff.raise_if_blocked()
        locked_for = math.ceil(self._login_locked_until - monotonic())
        if locked_for > 0:
            raise HelloFreshRateLimitedError(
                "HelloFresh sign-in is temporarily locked after too many attempts; "
                f"not retrying for {locked_for} seconds"
            )
        await self._async_fetch_app_token()

        try:
            response = await async_auth_post(
                self._session,
                f"{self._base_url}/gw/login",
                params=self._auth_query(),
                json_payload={"username": self._username, "password": self._password},
                headers=self._auth_headers(content_type="application/json", login=True),
                use_curl_cffi_headers=self._use_curl_cffi_headers,
                login=True,
            )
        except (ClientError, TimeoutError) as err:
            # Transient network failure — not a credential rejection. See the refresh path.
            self._log_transport_error("/gw/login", err)
            raise HelloFreshError(
                f"HelloFresh login could not connect: {_error_name(err)}"
            ) from err
        # A redirect is not followed (the body is the email and password), so it fails too.
        if response.status >= _HTTP_REDIRECT or _has_cf_challenge(response):
            error_body = await _async_body_text(response)
            # An edge block is transient, but retrying each poll risks another block.
            self._raise_if_bot_block(
                "login",
                response,
                error_body,
                hint="; this is not a password error: the request was rejected before "
                "reaching the login API",
            )
            if response.status in _AUTH_FAILURE_STATUSES:
                # Never the raw body: a login rejection can echo the submitted email, and logs
                # are shared on GitHub issues without redaction.
                _LOGGER.warning(
                    "HelloFresh login REJECTED HTTP %s: %s%s",
                    response.status,
                    _safe_auth_error_summary(error_body),
                    self._auth_log_details(response, error_body),
                )
                if _auth_error_code(error_body) == _PASSWORD_MUST_BE_ROTATED:
                    self._credentials_rejected = HelloFreshPasswordResetRequiredError(
                        "HelloFresh says this account's password has expired; follow the "
                        "instructions HelloFresh emailed to update it"
                    )
                else:
                    self._credentials_rejected = HelloFreshLoginRejectedError(
                        f"HelloFresh login failed: HTTP {response.status}"
                    )
                raise self._credentials_rejected
            if response.status == _HTTP_TOO_MANY_REQUESTS:
                # HelloFresh locks sign-in for a few minutes after too many attempts, and each
                # new attempt can extend the lock, so stop posting the password until it ends.
                # The lock is kept where the next client for this account will find it.
                lock = min(
                    max(_retry_after_seconds(response) or 0, _LOGIN_RATE_LIMIT_MIN_SECONDS),
                    _LOGIN_RATE_LIMIT_MAX_SECONDS,
                )
                self._auth_state.lock_login(self._login_lock_key, monotonic() + lock)
                _LOGGER.warning(
                    "HelloFresh login RATE-LIMITED (HTTP 429): sign-in is temporarily locked "
                    "after too many attempts; not retrying for %s seconds%s",
                    lock,
                    self._auth_log_details(response, error_body),
                )
                raise HelloFreshRateLimitedError(
                    f"HelloFresh sign-in is temporarily locked; retry in {lock} seconds"
                )
            if self._log_auth_diagnostics:
                _LOGGER.warning(
                    "HelloFresh login failed HTTP %s%s",
                    response.status,
                    self._auth_log_details(response, error_body),
                )
            raise HelloFreshError(
                f"HelloFresh login failed: HTTP {response.status} "
                f"({_safe_auth_error_summary(error_body)})"
            )

        payload = await self._async_auth_payload(response, context="login")
        self._apply_auth_object(payload, context="login")
        self._log_auth_success("/gw/login", response.status)
        self._block_backoff.reset_after_success()

    @property
    def _login_locked_until(self) -> float:
        """The monotonic() time before which no password is posted after a 429 lockout."""
        return self._auth_state.login_locks.get(self._login_lock_key, 0.0)

    async def async_email_registered(self, email: str) -> bool | None:
        """Ask HelloFresh whether an email has an account in this country, best-effort.

        The sign-in page posts ``{"email", "country"}`` to ``/gw/auth/email/status`` before
        ``/gw/login`` to pick its form, and gets ``{"registered": bool}`` (HAR 59). Setup asks
        only after a login was rejected, to tell an unknown email (or the wrong country) from
        a wrong password. None means no answer; any failure leaves the caller with its generic
        error. A bot-protection block still starts the usual cooldown.
        """
        try:
            self._block_backoff.raise_if_blocked()
            response = await async_auth_post(
                self._session,
                f"{self._base_url}/gw/auth/email/status",
                json_payload={"email": email.strip(), "country": api_country_code(self._country)},
                headers=self._auth_headers(content_type="application/json", login=True),
                use_curl_cffi_headers=self._use_curl_cffi_headers,
                login=True,
            )
            body = await response.text()
            self._raise_if_bot_block("email check", response, body)
        except (HelloFreshBotBlockedError, ClientError, TimeoutError, UnicodeDecodeError):
            return None
        # Only a plain success is an answer; an error or an unfollowed redirect is not.
        if response.status >= _HTTP_REDIRECT:
            return None
        try:
            registered = json.loads(body).get("registered")
        except (ValueError, AttributeError):
            return None
        _LOGGER.debug("HelloFresh email check answered registered=%s", registered)
        return registered if isinstance(registered, bool) else None

    async def _async_fetch_app_token(self) -> None:
        """Fetch the anonymous app token the web app obtains before login.

        Observed as ``POST /gw/auth/token?grant_type=client_credentials&client_id=senf``.
        The response is not retained -- the app token only primes the gateway; the
        user-scoped token comes from /gw/login. A failure here is non-fatal: log and
        continue, since /gw/login was observed to succeed without an Authorization header.
        Only a bot-protection block stops the login that would follow.
        """
        self._block_backoff.raise_if_blocked()
        try:
            response = await async_auth_post(
                self._session,
                f"{self._base_url}/gw/auth/token",
                # In the web app's order (HAR 59).
                params={"client_id": GW_CLIENT_ID, "grant_type": "client_credentials"},
                headers=self._auth_headers(content_type=None),
                use_curl_cffi_headers=self._use_curl_cffi_headers,
            )
        except (ClientError, TimeoutError) as err:
            self._log_transport_error("/gw/auth/token", err)
            _LOGGER.debug("HelloFresh app-token request errored (non-fatal): %s", _error_name(err))
            return
        # A challenge page can come with any status, 200 included, so every answer is checked.
        body = await _async_body_text(response)
        self._raise_if_bot_block("app-token request", response, body)
        if response.status < _HTTP_REDIRECT:
            self._log_auth_success("/gw/auth/token", response.status)
            return
        _LOGGER.log(
            logging.WARNING if self._log_auth_diagnostics else logging.DEBUG,
            "HelloFresh app-token request returned HTTP %s (non-fatal)%s",
            response.status,
            self._auth_log_details(response, body),
        )

    async def _async_auth_payload(
        self, response: ClientResponse | AuthResponse, context: str
    ) -> dict[str, Any]:
        """Decode and minimally validate an auth-endpoint JSON response."""
        if "html" in (_response_content_type(response) or "").lower():
            # A web page where the auth object should be: Cloudflare's challenge when it
            # comes as a 200, and otherwise still not an answer about the credentials.
            self._raise_if_bot_block(context, response, await _async_body_text(response))
            raise HelloFreshError(
                f"HelloFresh {context} answered with a web page: HTTP {response.status}"
            )
        try:
            payload = await response.json(content_type=None)
        except (ClientError, ValueError) as err:
            self._raise_if_bot_block(context, response, await _async_body_text(response), cause=err)
            raise HelloFreshAuthError(f"HelloFresh {context} response was not valid JSON") from err
        if not isinstance(payload, dict):
            raise HelloFreshAuthError(f"HelloFresh {context} response was not a JSON object")
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise HelloFreshAuthError(
                f"HelloFresh {context} response did not include an access token"
            )
        return payload

    def _apply_auth_object(self, payload: dict[str, Any], context: str) -> None:
        """Adopt the access/refresh tokens from a /gw auth object and persist them.

        Shared by the /gw/refresh and /gw/login paths -- both return the same auth object
        shape (access_token, refresh_token, expires_in, refresh_expires_in, token_type).
        """
        now = int(datetime.now(UTC).timestamp())
        self._access_token = str(payload["access_token"]).strip()
        # No request has carried this token yet (see async_refresh_if_in_use).
        self._token_in_use = False

        new_refresh = payload.get("refresh_token")
        old_refresh_fp = _token_fingerprint(self._refresh_token)
        rotated = isinstance(new_refresh, str) and bool(new_refresh.strip())
        if isinstance(new_refresh, str) and new_refresh.strip():
            # A new refresh token resets its own 60-day clock to now. If none is returned,
            # the existing one (and its original issue time) is retained unchanged.
            self._refresh_token = new_refresh.strip()
            self._refresh_token_issued_at = now
            self._refresh_rejection = None
        _LOGGER.debug(
            "HelloFresh %s OK: refresh_token %s -> %s (rotated=%s)",
            context,
            old_refresh_fp,
            _token_fingerprint(self._refresh_token),
            rotated,
        )

        token_type = payload.get("token_type")
        if isinstance(token_type, str) and token_type.strip():
            self._token_type = token_type.strip()
        # Use explicit None checks, not ``or``: a real 0 or a smaller server value must not
        # be silently replaced by the stale local value.
        refreshed_expires_in = coerce_int(payload.get("expires_in"))
        if refreshed_expires_in is not None:
            self._token_expires_in = refreshed_expires_in
        refreshed_refresh_expires_in = coerce_int(payload.get("refresh_expires_in"))
        if refreshed_refresh_expires_in is not None:
            self._refresh_expires_in = refreshed_refresh_expires_in
        self._token_issued_at = now
        if self._token_refresh_callback is not None:
            self._token_refresh_callback(
                {
                    "access_token": self._access_token,
                    "refresh_token": self._refresh_token,
                    "issued_at": self._token_issued_at,
                    "expires_in": self._token_expires_in,
                    "refresh_expires_in": self._refresh_expires_in,
                    "refresh_token_issued_at": self._refresh_token_issued_at,
                    "token_type": self._token_type,
                }
            )
