"""TLS-impersonating transport for the bot-protected ``/gw`` auth POSTs.

All HelloFresh properties sit behind Cloudflare. Some regions (observed: ``www.hellofresh.co.uk``)
enable Cloudflare Bot Management rules that fingerprint the **TLS (JA3/JA4) and HTTP/2**
characteristics of the connection and reject non-browser clients *before* the HTTP headers
are evaluated -- so no ``User-Agent`` or header tweak can get past them. ``aiohttp`` (Python +
OpenSSL) has exactly such a non-browser fingerprint.

This module routes the handful of auth calls (``/gw/auth/token``, ``/gw/login``, ``/gw/refresh``)
through `curl_cffi <https://github.com/lexiforest/curl_cffi>`_ when it is installed, which
performs the request with a real Chrome TLS/HTTP2 fingerprint (``impersonate="chrome"``). When
``curl_cffi`` is **not** available it transparently falls back to the shared ``aiohttp``
session, preserving today's behavior exactly.

The aiohttp fallback returns the native ``aiohttp`` response unchanged; the curl_cffi path
returns a small :class:`AuthResponse` adapter exposing the same slice of that interface the
token manager uses (``status``, ``headers``, awaitable ``text()``/``json()``), so the existing
WAF/bot-block handling in ``token_manager`` needs no branching.

Both the auth POSTs and the authenticated data XHRs go through this path: Cloudflare
fingerprints the TLS/HTTP2 connection, which is identical for both, so both need the
impersonating transport. Sessions are pooled per event loop (see ``_shared_curl_session``)
so connections are reused across a poll instead of re-handshaking on every request.
"""

from __future__ import annotations

import asyncio
from importlib import util
import json as _json
import logging
from typing import Any
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientResponse, ClientSession

from .normalizers import _template_debug_path

_LOGGER = logging.getLogger(__name__)

# Chrome impersonation target passed to curl_cffi. "chrome" tracks a recent Chrome build the
# installed curl_cffi knows about; pinning a specific version (e.g. "chrome131") risks breaking
# when the dependency is upgraded and that token is dropped, so the rolling alias is preferred.
_IMPERSONATE_TARGET = "chrome"

# Resolved once at import: is curl_cffi importable in this environment?
_HAS_CURL_CFFI = util.find_spec("curl_cffi") is not None

# curl_cffi's ``AsyncSession`` class, imported eagerly at module load when available. This module
# is imported off the event loop (Home Assistant loads integration modules via its import
# executor), so doing the curl_cffi import HERE keeps its importlib.metadata disk I/O
# (listdir/read_text/open) off the loop. Previously the import happened lazily on the first
# request, which ran inside the loop and tripped HA's blocking-call detector. ``None`` when
# curl_cffi is unavailable or failed to import, signalling callers to use the aiohttp fallback.
_ASYNC_SESSION_CLS: type | None = None
if _HAS_CURL_CFFI:
    try:
        from curl_cffi.requests import AsyncSession as _ASYNC_SESSION_CLS  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - a broken optional dep degrades to the aiohttp fallback
        _LOGGER.debug("curl_cffi present but failed to import; using aiohttp fallback")
        _ASYNC_SESSION_CLS = None


def tls_impersonation_available() -> bool:
    """Return True when curl_cffi is installed and the TLS-impersonation path is usable."""
    return _HAS_CURL_CFFI and _ASYNC_SESSION_CLS is not None


class AuthResponse:
    """A minimal, transport-agnostic view of a curl_cffi auth HTTP response.

    Adapts curl_cffi's (sync-attribute) ``Response`` to the slice of
    :class:`aiohttp.ClientResponse` the token manager uses, so both transports hand back the
    same shape. The aiohttp path does *not* use this -- it returns its native response.
    """

    def __init__(self, status: int, headers: dict[str, str], body: str) -> None:
        self.status = status
        self.headers = headers
        self._body = body

    async def text(self) -> str:
        """Return the response body as text."""
        return self._body

    async def json(self, content_type: str | None = None) -> Any:
        """Parse the response body as JSON.

        ``content_type`` is accepted for call-site compatibility with aiohttp's
        ``response.json(content_type=None)`` and is otherwise ignored: HelloFresh's auth
        endpoints don't always send an ``application/json`` content type, and the caller
        already validates the decoded payload shape.
        """
        return _json.loads(self._body)


async def async_request(
    session: ClientSession,
    method: str,
    url: str,
    *,
    params: dict[str, str] | None = None,
    json_payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> ClientResponse | AuthResponse:
    """Send an HTTP request, preferring a Chrome-TLS-impersonating transport.

    Uses curl_cffi (real Chrome JA3/JA4 + HTTP2 fingerprint) when available; otherwise falls
    back to the shared aiohttp session and returns its native response. A curl_cffi
    transport-level failure also falls back to aiohttp rather than failing the call outright,
    so a broken optional dependency never makes the integration worse than the aiohttp-only
    baseline. The returned object always exposes ``status``, ``headers`` and awaitable
    ``text()``/``json()`` -- the slice every caller uses -- regardless of which transport ran.

    This backs both the ``/gw`` auth POSTs and the authenticated data XHRs: stricter-region
    Cloudflare fingerprints the TLS/HTTP2 connection, which is identical for both, so both
    need the same impersonating transport to get past it.
    """
    curl = await _try_curl_cffi(method, url, params, json_payload, headers)
    if curl is not None:
        return curl
    return await session.request(method, url, params=params, json=json_payload, headers=headers)


async def async_auth_post(
    session: ClientSession,
    url: str,
    *,
    params: dict[str, str] | None = None,
    json_payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> ClientResponse | AuthResponse:
    """POST an auth request through the impersonating transport.

    Unlike :func:`async_request`, a curl_cffi failure mid-request must NOT retry on
    aiohttp: the auth POSTs are not idempotent (``/gw/refresh`` invalidates the refresh
    token on use), and a timeout after the server processed the request would re-send the
    now-spent token — the retry's 401 then read as "refresh token rejected" and forced a
    needless reauth. aiohttp is used only when curl_cffi never attempted the request; a
    mid-request failure surfaces as ``aiohttp.ClientError`` for callers to treat as
    transient.
    """
    if not tls_impersonation_available():
        return await session.post(url, params=params, json=json_payload, headers=headers)
    try:
        response = await _curl_cffi_request(
            "POST", url, params=params, json_payload=json_payload, headers=headers
        )
    except Exception as err:
        raise ClientError(f"curl_cffi auth POST to {url} failed: {err}") from err
    if response is None:
        # The impersonating session class vanished between the availability check and the
        # call (import raced/broken) — the request was never sent, so aiohttp is safe.
        return await session.post(url, params=params, json=json_payload, headers=headers)
    return response


async def _try_curl_cffi(
    method: str,
    url: str,
    params: dict[str, str] | None,
    json_payload: dict[str, Any] | None,
    headers: dict[str, str] | None,
) -> AuthResponse | None:
    """Attempt the request via curl_cffi; return ``None`` to signal "fall back to aiohttp".

    Returns ``None`` when curl_cffi is unavailable, or when a curl_cffi call raises at the
    transport level -- a broken optional dependency degrades to aiohttp rather than failing
    the call outright.
    """
    if not _HAS_CURL_CFFI:
        return None
    try:
        return await _curl_cffi_request(
            method, url, params=params, json_payload=json_payload, headers=headers
        )
    except Exception as err:  # noqa: BLE001 - any curl_cffi failure should degrade, not crash
        # Path only, ids templated: full URLs can embed subscription/plan ids, and debug
        # logs get attached to public GitHub issues.
        _LOGGER.debug(
            "curl_cffi %s to %s failed (%s); falling back to aiohttp",
            method,
            _template_debug_path(urlsplit(url).path),
            err,
        )
        return None


# One long-lived curl_cffi session per event loop, so requests reuse pooled TCP+TLS
# connections instead of paying a fresh handshake each time. Creating a session per request
# cost ~67 ms extra per call when measured against the live host (~113 ms vs ~46 ms), which
# across a ~35-request poll is ~2.4 s of pure handshake on the critical path.
#
# Keyed by the running loop rather than stored in a single global: Home Assistant can tear
# down and recreate its loop (tests certainly do), and a session bound to a dead loop raises
# on use. Keying by loop means a stale entry is simply never looked up again, and
# ``async_close_shared_session`` clears the current loop's entry on unload.
_SHARED_SESSIONS: dict[Any, Any] = {}


def _shared_curl_session() -> Any | None:
    """Return the pooled curl_cffi session for this event loop, creating it on first use."""
    if _ASYNC_SESSION_CLS is None:
        return None
    loop = asyncio.get_running_loop()
    session = _SHARED_SESSIONS.get(loop)
    if session is None:
        session = _ASYNC_SESSION_CLS()
        _SHARED_SESSIONS[loop] = session
    return session


async def async_close_shared_session() -> None:
    """Close and drop this event loop's pooled curl_cffi session.

    Called from the integration's unload path. Closing is best-effort: a session that already
    failed or whose loop is going away must not turn unload into an error.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - no loop means nothing was ever created
        return
    session = _SHARED_SESSIONS.pop(loop, None)
    if session is None:
        return
    try:
        await session.close()
    except Exception as err:  # noqa: BLE001 - a failed close must never block unload
        _LOGGER.debug("Closing pooled curl_cffi session failed: %s", err)


async def _curl_cffi_request(
    method: str,
    url: str,
    *,
    params: dict[str, str] | None,
    json_payload: dict[str, Any] | None,
    headers: dict[str, str] | None,
) -> AuthResponse | None:
    """Perform the request through curl_cffi with Chrome impersonation.

    Returns ``None`` when the curl_cffi AsyncSession class is unavailable, so the caller falls
    back to aiohttp. The class is imported once at module load (off the event loop), and the
    session itself is pooled per loop (see ``_shared_curl_session``) so connections are reused.
    """
    curl_session = _shared_curl_session()
    if curl_session is None:
        return None

    response = await curl_session.request(
        method,
        url,
        params=params,
        json=json_payload,
        headers=headers,
        impersonate=_IMPERSONATE_TARGET,
        # Verify the server's TLS certificate. curl_cffi defaults to True, but this carries
        # credentials/tokens (the /gw auth POSTs), so the security-critical setting is made
        # explicit rather than relying on a library default that a future version could change.
        verify=True,
    )
    return AuthResponse(
        status=response.status_code,
        headers=dict(response.headers),
        body=response.text,
    )
