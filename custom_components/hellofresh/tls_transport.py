"""TLS-impersonating transport for ``/gw`` auth and data requests.

All HelloFresh properties sit behind Cloudflare. Some regions (observed: ``www.hellofresh.co.uk``)
enable Cloudflare Bot Management rules that fingerprint the **TLS (JA3/JA4) and HTTP/2**
characteristics of the connection and reject non-browser clients *before* the HTTP headers
are evaluated -- so no ``User-Agent`` or header tweak can get past them. ``aiohttp`` (Python +
OpenSSL) has exactly such a non-browser fingerprint.

This module routes auth and data calls
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
impersonating transport. Sessions are pooled per entry (see ``_shared_curl_session``)
so connections are reused across a poll instead of re-handshaking on every request.
"""

from __future__ import annotations

import asyncio
from importlib import util
import json as _json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientResponse, ClientSession

from .normalizers import _template_debug_path

_LOGGER = logging.getLogger(__name__)

# Chrome impersonation target passed to curl_cffi. "chrome" tracks a recent Chrome build the
# installed curl_cffi knows about; pinning a specific version (e.g. "chrome131") risks breaking
# when the dependency is upgraded and that token is dropped, so the rolling alias is preferred.
# At import it is resolved to the concrete target (e.g. "chrome150") so the request headers can
# claim the same Chrome version the TLS fingerprint does (see impersonated_chrome_major).
_IMPERSONATE_TARGET = "chrome"
_IMPERSONATE_TARGET_RE = re.compile(r"chrome(\d+)")

# Resolved once at import: is curl_cffi importable in this environment?
_HAS_CURL_CFFI = util.find_spec("curl_cffi") is not None

# curl_cffi's ``AsyncSession`` class, imported eagerly at module load when available. This module
# is imported off the event loop (Home Assistant loads integration modules via its import
# executor), so doing the curl_cffi import HERE keeps its importlib.metadata disk I/O
# (listdir/read_text/open) off the loop. Previously the import happened lazily on the first
# request, which ran inside the loop and tripped HA's blocking-call detector. ``None`` when
# curl_cffi is unavailable or failed to import, signalling callers to use the aiohttp fallback.
_ASYNC_SESSION_CLS: type | None = None
_CURL_OPT: Any | None = None
if _HAS_CURL_CFFI:
    try:
        from curl_cffi import CurlOpt as _CURL_OPT  # noqa: PLC0415
        from curl_cffi.requests import AsyncSession as _ASYNC_SESSION_CLS  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - a broken optional dep degrades to the aiohttp fallback
        _LOGGER.debug("curl_cffi present but failed to import; using aiohttp fallback")
        _ASYNC_SESSION_CLS = None
        _CURL_OPT = None

# Chrome puts client hints before the usual XHR headers. curl_cffi's built-in header order is
# for a page navigation, so list every header our auth and data XHRs may send, including the
# optional account/feature headers. Names absent from a given request are simply skipped.
_CHROME_XHR_HEADER_ORDER = ",".join(
    (
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
        "dpr",
        "viewport-width",
        "user-agent",
        "accept",
        "content-type",
        "authorization",
        "x-market-api-version",
        "x-food-categorization",
        "x-sort-variations-by-quantity",
        "x-requested-by",
        "traceparent",
        "x-b3-traceid",
        "x-b3-spanid",
        "x-b3-sampled",
        "x-request-id",
        "origin",
        "sec-fetch-site",
        "sec-fetch-mode",
        "sec-fetch-dest",
        "referer",
        "accept-encoding",
        "accept-language",
        "cache-control",
        "pragma",
        "priority",
    )
)

# The default-header mode leaves curl_cffi in charge of browser metadata, while retaining
# headers needed by HelloFresh's API and by the caller's account-specific requests.
_API_HEADERS_WITH_CURL_DEFAULTS = frozenset(
    {
        "accept",
        "accept-language",
        "authorization",
        "content-type",
        "dpr",
        "origin",
        "priority",
        "referer",
        "traceparent",
        "viewport-width",
    }
)

_CURL_ACCEPT_ENCODING = "gzip, deflate, br, zstd"

# The Chrome major version the "chrome" alias resolves to in the installed curl_cffi, or None
# without curl_cffi. A User-Agent claiming a different Chrome than the TLS/HTTP2 fingerprint is
# a bot tell, and it drifted silently before: curl_cffi 0.16.3 moved the alias to Chrome 150
# while the headers still said 138.
_IMPERSONATED_CHROME_MAJOR: int | None = None
if _ASYNC_SESSION_CLS is not None:
    try:
        from curl_cffi.requests.impersonate import DEFAULT_CHROME  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - keep the rolling alias and the fallback headers
        _LOGGER.debug(
            "curl_cffi has no DEFAULT_CHROME; keeping impersonate=%r", _IMPERSONATE_TARGET
        )
    else:
        if (match := _IMPERSONATE_TARGET_RE.fullmatch(DEFAULT_CHROME)) is not None:
            _IMPERSONATE_TARGET = DEFAULT_CHROME
            _IMPERSONATED_CHROME_MAJOR = int(match[1])


def tls_impersonation_available() -> bool:
    """Return True when curl_cffi is installed and the TLS-impersonation path is usable."""
    return _HAS_CURL_CFFI and _ASYNC_SESSION_CLS is not None


def impersonated_chrome_major() -> int | None:
    """Return the Chrome major version curl_cffi impersonates, or None without curl_cffi."""
    return _IMPERSONATED_CHROME_MAJOR


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
    use_curl_cffi_headers: bool = False,
) -> ClientResponse | AuthResponse:
    """Send an HTTP request, preferring a Chrome-TLS-impersonating transport.

    Uses curl_cffi (real Chrome JA3/JA4 + HTTP2 fingerprint) when available; otherwise falls
    back to the entry's aiohttp session and returns its native response. A failed curl_cffi
    request may already have reached the server, so it is never replayed through aiohttp.
    The returned object always exposes ``status``, ``headers`` and awaitable
    ``text()``/``json()`` -- the slice every caller uses -- regardless of which transport ran.

    This backs both the ``/gw`` auth POSTs and the authenticated data XHRs: stricter-region
    Cloudflare fingerprints the TLS/HTTP2 connection, which is identical for both, so both
    need the same impersonating transport to get past it.
    """
    if not tls_impersonation_available():
        return await session.request(method, url, params=params, json=json_payload, headers=headers)
    try:
        response = await _curl_cffi_request(
            method,
            url,
            params=params,
            json_payload=json_payload,
            headers=headers,
            use_curl_cffi_headers=use_curl_cffi_headers,
            owner=session,
        )
    except Exception as err:
        _LOGGER.debug(
            "curl_cffi %s to %s failed (%s)",
            method,
            _template_debug_path(urlsplit(url).path),
            type(err).__name__,
        )
        raise ClientError(f"curl_cffi {method} request failed: {type(err).__name__}") from err
    if response is None:
        return await session.request(method, url, params=params, json=json_payload, headers=headers)
    return response


async def async_auth_post(
    session: ClientSession,
    url: str,
    *,
    params: dict[str, str] | None = None,
    json_payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    use_curl_cffi_headers: bool = False,
) -> ClientResponse | AuthResponse:
    """POST an auth request through the impersonating transport.

    A curl_cffi failure mid-request must NOT retry on aiohttp: the auth POSTs are not
    idempotent (``/gw/refresh`` invalidates the refresh
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
            "POST",
            url,
            params=params,
            json_payload=json_payload,
            headers=headers,
            use_curl_cffi_headers=use_curl_cffi_headers,
            owner=session,
        )
    except Exception as err:
        raise ClientError(f"curl_cffi auth POST failed: {type(err).__name__}") from err
    if response is None:
        # The impersonating session class vanished between the availability check and the
        # call (import raced/broken) — the request was never sent, so aiohttp is safe.
        return await session.post(url, params=params, json=json_payload, headers=headers)
    return response


# One long-lived curl_cffi session per entry and header mode, so requests reuse pooled TCP+TLS
# connections instead of paying a fresh handshake each time. Creating a session per request
# cost ~67 ms extra per call when measured against the live host (~113 ms vs ~46 ms), which
# across a ~35-request poll is ~2.4 s of pure handshake on the critical path.
#
# Keyed by the loop, header mode and owning aiohttp session. Each entry owns its HTTP
# session, so cookies cannot flow between accounts while connections are reused within
# one entry. Home Assistant can also tear down and recreate its loop (tests do).
_SHARED_SESSIONS: dict[tuple[Any, bool, ClientSession | None], Any] = {}


def _shared_curl_session(
    use_curl_cffi_headers: bool = False, owner: ClientSession | None = None
) -> Any | None:
    """Return the pooled curl_cffi session for this entry and event loop."""
    if _ASYNC_SESSION_CLS is None:
        return None
    loop = asyncio.get_running_loop()
    key = (loop, use_curl_cffi_headers, owner)
    session = _SHARED_SESSIONS.get(key)
    if session is None:
        options = (
            {}
            if use_curl_cffi_headers
            else {"curl_options": {_CURL_OPT.HTTPHEADER_ORDER: _CHROME_XHR_HEADER_ORDER}}
        )
        session = _ASYNC_SESSION_CLS(**options)
        _SHARED_SESSIONS[key] = session
    return session


async def async_close_shared_session(owner: ClientSession | None = None) -> None:
    """Close this entry's curl sessions, or all sessions when no owner is given.

    Called from the integration's unload path. Closing is best-effort: a session that already
    failed or whose loop is going away must not turn unload into an error.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - no loop means nothing was ever created
        return
    keys = [
        key for key in _SHARED_SESSIONS if key[0] is loop and (owner is None or key[2] is owner)
    ]
    for key in keys:
        session = _SHARED_SESSIONS.pop(key, None)
        if session is None:
            continue
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
    use_curl_cffi_headers: bool = False,
    owner: ClientSession | None = None,
) -> AuthResponse | None:
    """Perform the request through curl_cffi with Chrome impersonation.

    Returns ``None`` when the curl_cffi AsyncSession class is unavailable, so the caller falls
    back to aiohttp. The class is imported once at module load (off the event loop), and the
    session itself is pooled per entry (see ``_shared_curl_session``) so connections are reused.
    """
    curl_session = _shared_curl_session(use_curl_cffi_headers, owner)
    if curl_session is None:
        return None

    # Let libcurl advertise and decode zstd itself. The integration's header only lists
    # decoders available to aiohttp, which lacks zstd on many Home Assistant installs.
    curl_headers = {
        name: value for name, value in (headers or {}).items() if name.lower() != "accept-encoding"
    }
    if use_curl_cffi_headers:
        curl_headers = {
            name: value
            for name, value in curl_headers.items()
            if name.lower() in _API_HEADERS_WITH_CURL_DEFAULTS or name.lower().startswith("x-")
        }
        # curl_cffi's Chrome preset describes a page navigation. Keep its UA, client
        # hints and other defaults, but override the fetch metadata for an API XHR.
        # None suppresses these two navigation-only headers in curl_cffi 0.16.3.
        curl_headers.update(
            {
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-User": None,
                "Upgrade-Insecure-Requests": None,
            }
        )

    response = await curl_session.request(
        method,
        url,
        params=params,
        json=json_payload,
        headers=curl_headers,
        accept_encoding=_CURL_ACCEPT_ENCODING,
        impersonate=_IMPERSONATE_TARGET,
        # Off by default: the integration supplies its complete browser XHR headers.
        default_headers=use_curl_cffi_headers,
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
