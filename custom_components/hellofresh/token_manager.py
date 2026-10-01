"""Access/refresh token lifecycle for the HelloFresh integration.

Extracted from ``client.py`` so the security-sensitive auth code lives in one focused,
independently testable unit. ``HelloFreshClient`` *composes* a ``TokenManager`` (rather than
inheriting auth behaviour) and delegates to it: the manager owns all token state, the
``/gw`` login/refresh HTTP calls, expiry math, and the single concurrency lock.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
from importlib import util
import json
import logging
import math
import re
import secrets
from time import monotonic
from typing import Any
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
from .models import HelloFreshAuthError, HelloFreshBotBlockedError, HelloFreshError
from .parsers import coerce_int, decode_jwt_claims
from .tls_transport import (
    AuthResponse,
    async_auth_post,
    impersonated_chrome_major,
    tls_impersonation_available,
)

_LOGGER = logging.getLogger(__name__)

# HTTP status thresholds used when interpreting HelloFresh responses.
_HTTP_BAD_REQUEST = 400
_AUTH_FAILURE_STATUSES = frozenset({401, 403})
_CF_ERROR_CODE_RE = re.compile(
    r'(?:\bError(?:\s+code)?\s*[:#]?\s*|cf-error-code[^>]*>\s*|["\']code["\']\s*:\s*["\']?)(10\d{2})\b',
    re.IGNORECASE,
)
_CF_RAY_RE = re.compile(r"[0-9a-fA-F]{16,32}(?:-[A-Za-z0-9]{2,8})?")
_CF_RAY_IN_BODY_RE = re.compile(r"Ray\s+ID\s*[:#]\s*([0-9A-Za-z-]{16,48})", re.IGNORECASE)
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
    }
)


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
    """One cooldown for accounts on the same Home Assistant event loop."""

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


_SESSION_BLOCK_BACKOFF: WeakKeyDictionary[ClientSession, _AuthBlockBackoff] = WeakKeyDictionary()
_LOOP_BLOCK_BACKOFF: WeakKeyDictionary[asyncio.AbstractEventLoop, _AuthBlockBackoff] = (
    WeakKeyDictionary()
)


def _backoff_for_session(session: ClientSession) -> _AuthBlockBackoff:
    """Share an IP-level cooldown across HA entries on the same event loop."""
    try:
        owner = getattr(session, "_loop", None) or session
        registry = (
            _LOOP_BLOCK_BACKOFF
            if isinstance(owner, asyncio.AbstractEventLoop)
            else _SESSION_BLOCK_BACKOFF
        )
        backoff = registry.get(owner)
        if backoff is None:
            backoff = _AuthBlockBackoff()
            registry[owner] = backoff
        return backoff
    except TypeError:
        # Test stubs and unusual sessions may not support weak references.
        return _AuthBlockBackoff()


# Proactively refresh the access token once it has passed this fraction of its lifetime.
# A wide refresh window (e.g. the back half of a 30-min token) means the periodic refresh
# timer reliably catches the token before it expires, regardless of tick phase. The
# absolute floor guards against refreshing pointlessly often for unusually long tokens.
_TOKEN_REFRESH_AT_LIFETIME_FRACTION = 0.5
_TOKEN_MIN_REMAINING_BEFORE_REFRESH = 300  # always refresh within 5 min of expiry

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


# Cache-busting + safe fallback encoding headers. Accept-Encoding is computed once at import
# from the Python decoders available to aiohttp (see above).
_BROWSER_FETCH_HEADERS = {
    "Accept-Encoding": _browser_accept_encoding(),
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


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
    code_name = {"1010": "browser signature block", "1020": "firewall rule"}.get(code)
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
    """Return True when an auth response looks like a WAF/bot-protection block page.

    HelloFresh's login/refresh API answers with JSON. An HTML response on a 401/403 is
    almost always the edge bot-protection layer (Cloudflare/Akamai-style challenge page)
    rejecting a non-browser request *before* it reaches the login API -- not the API
    rejecting the credentials. Distinguishing the two keeps a transient block from being
    surfaced to the user as "wrong password" (which would trigger a pointless reauth loop).
    """
    if content_type and "html" in content_type.lower():
        return True
    head = body[:512].lstrip().lower()
    return head.startswith("<!doctype html") or head.startswith("<html")


def _looks_like_auth_block(response: Any, body: str) -> bool:
    """Detect a WAF response from its explicit markers or HTML shape."""
    return (
        _has_cf_challenge(response)
        or _cf_error_code(body) in {"1010", "1020"}
        or _looks_like_bot_block(_response_content_type(response), body)
    )


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
        self._refresh_lock = asyncio.Lock()
        self._block_backoff = _backoff_for_session(session)
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
        """Return the ``Authorization`` header value for an authenticated request."""
        return f"{self._token_type} {self._access_token}"

    def raise_if_bot_blocked(self) -> None:
        """Pause new API calls while this HTTP session is cooling down from a block."""
        self._block_backoff.raise_if_blocked()

    async def async_raise_if_data_blocked(self, response: ClientResponse | AuthResponse) -> None:
        """Record a Cloudflare block before a data 401/403 can trigger token refresh."""
        if (
            response.status not in {401, 403, 429}
            and not _has_cf_challenge(response)
            and "html" not in (_response_content_type(response) or "").lower()
        ):
            return
        try:
            body = await response.text()
        except (ClientError, TimeoutError, UnicodeDecodeError):
            body = ""
        if not _looks_like_auth_block(response, body):
            return
        delay = self._block_backoff.record_block()
        _LOGGER.warning(
            "HelloFresh data request BLOCKED by bot protection (HTTP %s); "
            "requests paused for %s seconds%s",
            response.status,
            delay,
            self._auth_log_details(response, body),
        )
        raise HelloFreshBotBlockedError(
            f"HelloFresh data request blocked by bot protection: HTTP {response.status}"
        )

    @property
    def can_obtain_token(self) -> bool:
        """Return True when a token can be (re)obtained via refresh or login."""
        return bool(self._refresh_token) or self._has_credentials

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
        """Proactively refresh the access token when it is near expiry.

        Safe to call on a timer independent of data polling. Does nothing when no
        refresh token is configured or the current token still has comfortable life.
        """
        # Nothing to renew with: no refresh token to exchange and no credentials to log in.
        if not self._refresh_token and not self._has_credentials:
            return
        if not self._access_token or self._token_expiring_soon():
            async with self._refresh_lock:
                # Re-check inside the lock -- another waiter may have already refreshed.
                if not self._access_token:
                    await self._async_refresh_access_token(force=True)
                elif self._token_expiring_soon():
                    # Proactive (half-life) refresh. If it fails but the current access
                    # token is still genuinely valid -- e.g. right after a reboot, when the
                    # stored token has life left but the refresh token was already rotated
                    # in a prior session -- keep using the existing token rather than failing
                    # the whole setup. The reactive 401 path surfaces a real expiry later.
                    try:
                        await self._async_refresh_access_token(force=False)
                    except HelloFreshAuthError:
                        if self._access_token_still_valid():
                            _LOGGER.warning(
                                "HelloFresh proactive token refresh failed; continuing with "
                                "the existing access token until it actually expires"
                            )
                            return
                        raise

    async def async_force_refresh_if_unchanged(self, token_before: str) -> None:
        """Force a refresh under the lock, unless another waiter already rotated the token.

        Called from the reactive 401 path. When several requests 401 at once (the
        coordinator fetches many endpoints concurrently), only the first should rotate the
        refresh token: HelloFresh invalidates the old refresh token on use, so a second
        forced rotation would burn the token the first waiter just obtained. If the access
        token already changed, another waiter refreshed -- the caller just retries with it.
        """
        async with self._refresh_lock:
            if self._access_token == token_before:
                await self._async_refresh_access_token(force=True)

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

    def _auth_headers(self, *, refresh: bool = False) -> dict[str, str]:
        """Return browser-like headers for the /gw auth POSTs.

        Includes ``Origin``/``Referer`` derived from the regional base URL so the request
        resembles the web app's XHR and is less likely to trip bot protection.
        """
        trace_id = secrets.token_hex(16)
        span_id = secrets.token_hex(8)
        return {
            "Accept": "*/*" if refresh else "application/json, text/plain, */*",
            "Accept-Language": _browser_accept_language(api_locale(self._country)),
            "Content-Type": "text/plain;charset=UTF-8" if refresh else "application/json",
            "User-Agent": _AUTH_USER_AGENT,
            "Origin": self._base_url,
            "Referer": f"{self._base_url}/login",
            "Priority": "u=1, i",
            **_BROWSER_FETCH_HEADERS,
            **_BROWSER_CLIENT_HINTS,
            "DPR": "1",
            "Viewport-Width": "1280",
            "traceparent": f"00-{trace_id}-{span_id}-01",
            "x-b3-traceid": trace_id,
            "x-b3-spanid": span_id,
            "x-b3-sampled": "1",
            "x-request-id": str(uuid4()),
        }

    def _auth_log_details(self, response: ClientResponse | AuthResponse, body: str) -> str:
        """Return opt-in Cloudflare details for a failed auth response."""
        return _cloudflare_diagnostics(response, body) if self._log_auth_diagnostics else ""

    def _log_auth_success(self, endpoint: str, status: int) -> None:
        """Record successful auth steps when the diagnostics option is enabled."""
        if self._log_auth_diagnostics and status < _HTTP_BAD_REQUEST:
            _LOGGER.warning(
                "HelloFresh authentication diagnostic: %s returned HTTP %s", endpoint, status
            )

    async def _async_refresh_access_token(self, force: bool) -> None:
        """Obtain a fresh access token.

        Mirrors the HelloFresh web app: renew via ``POST /gw/refresh`` when a live refresh
        token is available, otherwise (no/expired refresh token, or a rejected refresh)
        fall back to a full username/password login.
        """
        self._block_backoff.raise_if_blocked()
        if self._refresh_token and not self._refresh_token_expired():
            try:
                await self._async_refresh_with_token()
                return
            except HelloFreshAuthError as err:
                # Refresh token was rejected (dead/reused/rotated-away). Fall through to a
                # fresh login if credentials are available; otherwise surface the failure.
                _LOGGER.debug(
                    "HelloFresh /gw/refresh rejected (refresh_token fp=%s): %s; "
                    "falling back to login",
                    _token_fingerprint(self._refresh_token),
                    err,
                )
                if not self._has_credentials:
                    raise
        await self._async_login(force=force)

    @property
    def _has_credentials(self) -> bool:
        """Return True when a username and password are configured for login."""
        return bool(self._username and self._password)

    async def _async_refresh_with_token(self) -> None:
        """Renew the access token via ``POST {base}/gw/refresh``."""
        self._block_backoff.raise_if_blocked()
        try:
            response = await async_auth_post(
                self._session,
                f"{self._base_url}/gw/refresh",
                params=self._auth_query(),
                json_payload={"refresh_token": self._refresh_token},
                headers=self._auth_headers(refresh=True),
                use_curl_cffi_headers=self._use_curl_cffi_headers,
            )
        except (ClientError, TimeoutError) as err:
            # Network-level failure (DNS, connect, mid-request timeout): transient, NOT a
            # rejected refresh token. Wrapping it keeps callers' error handling intact —
            # unwrapped it escaped the config flow ("Unknown error" instead of
            # "cannot_connect") and the proactive-refresh timer (unhandled traceback).
            if self._log_auth_diagnostics:
                _LOGGER.warning(
                    "HelloFresh authentication /gw/refresh transport error: %s", type(err).__name__
                )
            raise HelloFreshError(
                f"HelloFresh token refresh could not connect: {type(err).__name__}"
            ) from err
        if response.status >= _HTTP_BAD_REQUEST or _has_cf_challenge(response):
            try:
                error_body = await response.text()
            except (ClientError, UnicodeDecodeError):  # pragma: no cover - defensive
                error_body = ""
            if _looks_like_auth_block(response, error_body):
                # Keep the refresh token and pause auth requests after an edge block.
                delay = self._block_backoff.record_block()
                _LOGGER.warning(
                    "HelloFresh token refresh BLOCKED by bot protection (HTTP %s); "
                    "requests paused for %s seconds (refresh_token fp=%s)%s",
                    response.status,
                    delay,
                    _token_fingerprint(self._refresh_token),
                    self._auth_log_details(response, error_body),
                )
                raise HelloFreshBotBlockedError(
                    f"HelloFresh token refresh blocked by bot protection: HTTP {response.status}"
                )
            if response.status in _AUTH_FAILURE_STATUSES:
                _LOGGER.warning(
                    "HelloFresh token refresh REJECTED HTTP %s: %s (refresh_token fp=%s, "
                    "refresh_token_issued_at=%s)%s",
                    response.status,
                    # Never the raw body: it is server-controlled free text and logs are not
                    # redacted the way diagnostics exports are.
                    _safe_auth_error_summary(error_body),
                    _token_fingerprint(self._refresh_token),
                    self._refresh_token_issued_at,
                    self._auth_log_details(response, error_body),
                )
                raise HelloFreshAuthError(
                    f"HelloFresh token refresh failed: HTTP {response.status}"
                )
            # Treat non-auth server errors as transient: the current access token may still
            # work until it expires, at which point the reactive 401-retry path surfaces a
            # proper auth failure. Raising HelloFreshError (not Auth) avoids a spurious login.
            _LOGGER.warning(
                "HelloFresh token refresh failed (transient); will retry on next poll: "
                "HTTP %s (%s)%s",
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

        self._block_backoff.raise_if_blocked()
        await self._async_fetch_app_token()

        try:
            response = await async_auth_post(
                self._session,
                f"{self._base_url}/gw/login",
                params=self._auth_query(),
                json_payload={"username": self._username, "password": self._password},
                headers=self._auth_headers(),
                use_curl_cffi_headers=self._use_curl_cffi_headers,
            )
        except (ClientError, TimeoutError) as err:
            # Transient network failure — not a credential rejection. See the refresh path.
            if self._log_auth_diagnostics:
                _LOGGER.warning(
                    "HelloFresh authentication /gw/login transport error: %s", type(err).__name__
                )
            raise HelloFreshError(
                f"HelloFresh login could not connect: {type(err).__name__}"
            ) from err
        if response.status >= _HTTP_BAD_REQUEST or _has_cf_challenge(response):
            try:
                error_body = await response.text()
            except (ClientError, UnicodeDecodeError):  # pragma: no cover - defensive
                error_body = ""
            if _looks_like_auth_block(response, error_body):
                # An edge block is transient, but retrying each poll risks another block.
                delay = self._block_backoff.record_block()
                _LOGGER.warning(
                    "HelloFresh login BLOCKED by bot protection (HTTP %s); this is not a "
                    "password error -- the request was rejected before reaching the login API. "
                    "Requests paused for %s seconds.%s",
                    response.status,
                    delay,
                    self._auth_log_details(response, error_body),
                )
                raise HelloFreshBotBlockedError(
                    f"HelloFresh login blocked by bot protection: HTTP {response.status}"
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
                raise HelloFreshAuthError(f"HelloFresh login failed: HTTP {response.status}")
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

    async def _async_fetch_app_token(self) -> None:
        """Fetch the anonymous app token the web app obtains before login.

        Observed as ``POST /gw/auth/token?grant_type=client_credentials&client_id=senf``.
        The response is not retained -- the app token only primes the gateway; the
        user-scoped token comes from /gw/login. A failure here is non-fatal: log and
        continue, since /gw/login was observed to succeed without an Authorization header.
        """
        self._block_backoff.raise_if_blocked()
        try:
            response = await async_auth_post(
                self._session,
                f"{self._base_url}/gw/auth/token",
                params={"grant_type": "client_credentials", "client_id": GW_CLIENT_ID},
                headers=self._auth_headers(),
                use_curl_cffi_headers=self._use_curl_cffi_headers,
            )
        except (ClientError, TimeoutError) as err:  # pragma: no cover - defensive
            if self._log_auth_diagnostics:
                _LOGGER.warning(
                    "HelloFresh authentication /gw/auth/token transport error: %s",
                    type(err).__name__,
                )
            _LOGGER.debug(
                "HelloFresh app-token request errored (non-fatal): %s", type(err).__name__
            )
            return
        if response.status >= _HTTP_BAD_REQUEST or _has_cf_challenge(response):
            try:
                details = await response.text()
            except (ClientError, UnicodeDecodeError):
                details = ""
            if _looks_like_auth_block(response, details):
                delay = self._block_backoff.record_block()
                _LOGGER.warning(
                    "HelloFresh app-token request BLOCKED by bot protection (HTTP %s); "
                    "requests paused for %s seconds%s",
                    response.status,
                    delay,
                    self._auth_log_details(response, details),
                )
                raise HelloFreshBotBlockedError(
                    f"HelloFresh app-token request blocked by bot protection: HTTP {response.status}"
                )
            if self._log_auth_diagnostics:
                _LOGGER.warning(
                    "HelloFresh app-token request returned HTTP %s (non-fatal)%s",
                    response.status,
                    self._auth_log_details(response, details),
                )
            else:
                _LOGGER.debug(
                    "HelloFresh app-token request returned HTTP %s (non-fatal)", response.status
                )
        else:
            try:
                body = await response.text()
            except (ClientError, TimeoutError, UnicodeDecodeError):
                body = ""
            if _looks_like_auth_block(response, body):
                delay = self._block_backoff.record_block()
                _LOGGER.warning(
                    "HelloFresh app-token request BLOCKED by bot protection (HTTP %s); "
                    "requests paused for %s seconds%s",
                    response.status,
                    delay,
                    self._auth_log_details(response, body),
                )
                raise HelloFreshBotBlockedError(
                    f"HelloFresh app-token request blocked by bot protection: HTTP {response.status}"
                )
            self._log_auth_success("/gw/auth/token", response.status)

    async def _async_auth_payload(
        self, response: ClientResponse | AuthResponse, context: str
    ) -> dict[str, Any]:
        """Decode and minimally validate an auth-endpoint JSON response."""
        if "html" in (_response_content_type(response) or "").lower():
            try:
                body = await response.text()
            except (ClientError, TimeoutError, UnicodeDecodeError):
                body = ""
            delay = self._block_backoff.record_block()
            _LOGGER.warning(
                "HelloFresh %s BLOCKED by bot protection (HTTP %s); requests paused "
                "for %s seconds%s",
                context,
                response.status,
                delay,
                self._auth_log_details(response, body),
            )
            raise HelloFreshBotBlockedError(
                f"HelloFresh {context} blocked by bot protection: HTTP {response.status}"
            )
        try:
            payload = await response.json(content_type=None)
        except (ClientError, ValueError) as err:
            try:
                body = await response.text()
            except (ClientError, TimeoutError, UnicodeDecodeError):
                body = ""
            if _looks_like_auth_block(response, body):
                delay = self._block_backoff.record_block()
                _LOGGER.warning(
                    "HelloFresh %s BLOCKED by bot protection (HTTP %s); requests paused "
                    "for %s seconds%s",
                    context,
                    response.status,
                    delay,
                    self._auth_log_details(response, body),
                )
                raise HelloFreshBotBlockedError(
                    f"HelloFresh {context} blocked by bot protection: HTTP {response.status}"
                ) from err
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

        new_refresh = payload.get("refresh_token")
        old_refresh_fp = _token_fingerprint(self._refresh_token)
        rotated = isinstance(new_refresh, str) and bool(new_refresh.strip())
        if isinstance(new_refresh, str) and new_refresh.strip():
            # A new refresh token resets its own 60-day clock to now. If none is returned,
            # the existing one (and its original issue time) is retained unchanged.
            self._refresh_token = new_refresh.strip()
            self._refresh_token_issued_at = now
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
