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

Only the auth POSTs use this path -- the high-volume authenticated data calls keep using the
HA-managed ``aiohttp`` session, since they succeed there and ``curl_cffi`` is a heavier,
per-request transport not worth using for every poll.
"""

from __future__ import annotations

from importlib import util
import json as _json
import logging
from typing import Any

from aiohttp import ClientResponse, ClientSession

_LOGGER = logging.getLogger(__name__)

# Chrome impersonation target passed to curl_cffi. "chrome" tracks a recent Chrome build the
# installed curl_cffi knows about; pinning a specific version (e.g. "chrome131") risks breaking
# when the dependency is upgraded and that token is dropped, so the rolling alias is preferred.
_IMPERSONATE_TARGET = "chrome"

# Resolved once at import: is curl_cffi importable in this environment?
_HAS_CURL_CFFI = util.find_spec("curl_cffi") is not None


def tls_impersonation_available() -> bool:
    """Return True when curl_cffi is installed and the TLS-impersonation path is usable."""
    return _HAS_CURL_CFFI


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


async def async_auth_post(
    session: ClientSession,
    url: str,
    *,
    params: dict[str, str] | None = None,
    json_payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> ClientResponse | AuthResponse:
    """POST an auth request, preferring a Chrome-TLS-impersonating transport.

    Uses curl_cffi (real Chrome JA3/JA4 + HTTP2 fingerprint) when available; otherwise falls
    back to the shared aiohttp session and returns its native response. A curl_cffi
    transport-level failure also falls back to aiohttp rather than failing the call outright,
    so a broken optional dependency never makes the integration worse than the aiohttp-only
    baseline. The returned object always exposes ``status``, ``headers`` and awaitable
    ``text()``/``json()``.
    """
    if _HAS_CURL_CFFI:
        try:
            return await _curl_cffi_post(
                url, params=params, json_payload=json_payload, headers=headers
            )
        except Exception as err:  # noqa: BLE001 - any curl_cffi failure should degrade, not crash
            _LOGGER.debug(
                "curl_cffi auth POST to %s failed (%s); falling back to aiohttp", url, err
            )
    return await session.post(url, params=params, json=json_payload, headers=headers)


async def _curl_cffi_post(
    url: str,
    *,
    params: dict[str, str] | None,
    json_payload: dict[str, Any] | None,
    headers: dict[str, str] | None,
) -> AuthResponse:
    """Perform the POST through curl_cffi with Chrome impersonation."""
    # Imported lazily so the module loads even when curl_cffi isn't installed.
    from curl_cffi.requests import AsyncSession  # noqa: PLC0415

    async with AsyncSession() as curl_session:
        response = await curl_session.post(
            url,
            params=params,
            json=json_payload,
            headers=headers,
            impersonate=_IMPERSONATE_TARGET,
        )
    return AuthResponse(
        status=response.status_code,
        headers=dict(response.headers),
        body=response.text,
    )
