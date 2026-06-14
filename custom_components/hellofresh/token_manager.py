"""Access/refresh token lifecycle for the HelloFresh integration.

Extracted from ``client.py`` so the security-sensitive auth code lives in one focused,
independently testable unit. ``HelloFreshClient`` *composes* a ``TokenManager`` (rather than
inheriting auth behaviour) and delegates to it: the manager owns all token state, the
``/gw`` login/refresh HTTP calls, expiry math, and the single concurrency lock.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
import hashlib
import logging
from typing import Any

from aiohttp import ClientError, ClientResponse, ClientSession

from .const import COUNTRY_BASE_URLS, DEFAULT_COUNTRY, GW_CLIENT_ID
from .models import HelloFreshAuthError, HelloFreshError
from .parsers import coerce_int, decode_jwt_claims

_LOGGER = logging.getLogger(__name__)

# HTTP status thresholds used when interpreting HelloFresh responses.
_HTTP_BAD_REQUEST = 400
_AUTH_FAILURE_STATUSES = frozenset({401, 403})

# Proactively refresh the access token once it has passed this fraction of its lifetime.
# A wide refresh window (e.g. the back half of a 30-min token) means the periodic refresh
# timer reliably catches the token before it expires, regardless of tick phase. The
# absolute floor guards against refreshing pointlessly often for unusually long tokens.
_TOKEN_REFRESH_AT_LIFETIME_FRACTION = 0.5
_TOKEN_MIN_REMAINING_BEFORE_REFRESH = 300  # always refresh within 5 min of expiry

# A browser-like User-Agent. HelloFresh fronts its endpoints with bot protection that
# fingerprints non-browser clients; a recognizable headless UA (e.g. "HomeAssistant-...")
# gets challenged with an HTML block page instead of a JSON API response. Presenting a
# current browser UA is a best-effort way to pass that layer. It can break whenever the
# protection is retuned -- see _looks_like_bot_block for how a block is handled when it does.
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_AUTH_USER_AGENT = _BROWSER_USER_AGENT


def _token_fingerprint(token: str | None) -> str:
    """Return a short, non-reversible fingerprint of a token for debug logging.

    Logs the first 8 hex chars of a SHA-256 digest -- enough to tell whether the
    refresh token *changed* between rotations without ever writing the secret to
    logs. Never log the token itself.
    """
    if not token:
        return "none"
    return hashlib.sha256(token.encode()).hexdigest()[:8]


def _response_content_type(response: Any) -> str | None:
    """Return a response's Content-Type header, tolerating stubs without ``headers``."""
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        return headers.get("Content-Type")
    except AttributeError:  # pragma: no cover - defensive
        return None


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
    ) -> None:
        """Initialize token state from the persisted config-entry values."""
        self._session = session
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
        return {"country": self._country.upper(), "locale": f"en-{self._country.upper()}"}

    def _auth_headers(self) -> dict[str, str]:
        """Return browser-like headers for the /gw auth POSTs.

        Includes ``Origin``/``Referer`` derived from the regional base URL so the request
        resembles the web app's XHR and is less likely to trip bot protection.
        """
        return {
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "User-Agent": _AUTH_USER_AGENT,
            "Origin": self._base_url,
            "Referer": f"{self._base_url}/",
        }

    async def _async_refresh_access_token(self, force: bool) -> None:
        """Obtain a fresh access token.

        Mirrors the HelloFresh web app: renew via ``POST /gw/refresh`` when a live refresh
        token is available, otherwise (no/expired refresh token, or a rejected refresh)
        fall back to a full username/password login.
        """
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
        response = await self._session.post(
            f"{self._base_url}/gw/refresh",
            params=self._auth_query(),
            json={"refresh_token": self._refresh_token},
            headers=self._auth_headers(),
        )
        if response.status in _AUTH_FAILURE_STATUSES:
            try:
                error_body = await response.text()
            except (ClientError, UnicodeDecodeError):  # pragma: no cover - defensive
                error_body = ""
            if _looks_like_bot_block(_response_content_type(response), error_body):
                # Blocked by edge bot protection, not the refresh API. Transient: keep the
                # current access token and retry on the next poll rather than discarding the
                # refresh token and forcing a login (which would hit the same block).
                _LOGGER.warning(
                    "HelloFresh token refresh BLOCKED by bot protection (HTTP %s); will retry "
                    "on next poll (refresh_token fp=%s)",
                    response.status,
                    _token_fingerprint(self._refresh_token),
                )
                raise HelloFreshError(
                    f"HelloFresh token refresh blocked by bot protection: HTTP {response.status}"
                )
            _LOGGER.warning(
                "HelloFresh token refresh REJECTED HTTP %s: %s (refresh_token fp=%s, "
                "refresh_token_issued_at=%s)",
                response.status,
                error_body[:300],
                _token_fingerprint(self._refresh_token),
                self._refresh_token_issued_at,
            )
            raise HelloFreshAuthError(f"HelloFresh token refresh failed: HTTP {response.status}")
        if response.status >= _HTTP_BAD_REQUEST:
            details = await response.text()
            # Treat non-auth server errors as transient: the current access token may still
            # work until it expires, at which point the reactive 401-retry path surfaces a
            # proper auth failure. Raising HelloFreshError (not Auth) avoids a spurious login.
            _LOGGER.warning(
                "HelloFresh token refresh failed (transient); will retry on next poll: "
                "HTTP %s (%s)",
                response.status,
                details[:200],
            )
            raise HelloFreshError(
                f"HelloFresh token refresh transient failure: HTTP {response.status}"
            )

        payload = await self._async_auth_payload(response, context="token refresh")
        self._apply_auth_object(payload, context="refresh")

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

        await self._async_fetch_app_token()

        response = await self._session.post(
            f"{self._base_url}/gw/login",
            params=self._auth_query(),
            json={"username": self._username, "password": self._password},
            headers=self._auth_headers(),
        )
        if response.status in _AUTH_FAILURE_STATUSES:
            try:
                error_body = await response.text()
            except (ClientError, UnicodeDecodeError):  # pragma: no cover - defensive
                error_body = ""
            if _looks_like_bot_block(_response_content_type(response), error_body):
                # An HTML 401/403 is the edge bot-protection layer, not the login API saying
                # the password is wrong. Raise a transient error so HA does not prompt the
                # user to re-enter correct credentials; the next poll retries the login.
                _LOGGER.warning(
                    "HelloFresh login BLOCKED by bot protection (HTTP %s) for %s; this is not a "
                    "password error -- the request was rejected before reaching the login API. "
                    "Will retry on the next poll.",
                    response.status,
                    self._username,
                )
                raise HelloFreshError(
                    f"HelloFresh login blocked by bot protection: HTTP {response.status}"
                )
            _LOGGER.warning(
                "HelloFresh login REJECTED HTTP %s for %s: %s",
                response.status,
                self._username,
                error_body[:300],
            )
            raise HelloFreshAuthError(f"HelloFresh login failed: HTTP {response.status}")
        if response.status >= _HTTP_BAD_REQUEST:
            details = await response.text()
            raise HelloFreshError(
                f"HelloFresh login failed: HTTP {response.status} ({details[:200]})"
            )

        payload = await self._async_auth_payload(response, context="login")
        self._apply_auth_object(payload, context="login")

    async def _async_fetch_app_token(self) -> None:
        """Fetch the anonymous app token the web app obtains before login.

        Observed as ``POST /gw/auth/token?grant_type=client_credentials&client_id=senf``.
        The response is not retained -- the app token only primes the gateway; the
        user-scoped token comes from /gw/login. A failure here is non-fatal: log and
        continue, since /gw/login was observed to succeed without an Authorization header.
        """
        try:
            response = await self._session.post(
                f"{self._base_url}/gw/auth/token",
                params={"grant_type": "client_credentials", "client_id": GW_CLIENT_ID},
                headers=self._auth_headers(),
            )
        except ClientError as err:  # pragma: no cover - defensive
            _LOGGER.debug("HelloFresh app-token request errored (non-fatal): %s", err)
            return
        if response.status >= _HTTP_BAD_REQUEST:
            _LOGGER.debug(
                "HelloFresh app-token request returned HTTP %s (non-fatal)", response.status
            )

    async def _async_auth_payload(self, response: ClientResponse, context: str) -> dict[str, Any]:
        """Decode and minimally validate an auth-endpoint JSON response."""
        try:
            payload = await response.json(content_type=None)
        except (ClientError, ValueError) as err:
            raise HelloFreshAuthError(
                f"HelloFresh {context} response was not valid JSON: {err}"
            ) from err
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
