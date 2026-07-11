# HelloFresh API Notes

This document describes the reverse-engineered HelloFresh HTTP API surface used by this repository's Home Assistant integration. It is an implementation reference for this repo, not an official specification — HelloFresh does not publish a stable public contract for this consumer account API, and the surface can change at any time.

It is derived from the integration source and its normalization tests:

- HTTP client and orchestration — [client.py](custom_components/hellofresh/client.py)
- Data models and exceptions — [models.py](custom_components/hellofresh/models.py)
- Pure parsing/coercion helpers — [parsers.py](custom_components/hellofresh/parsers.py)
- Payload normalization helpers — [normalizers.py](custom_components/hellofresh/normalizers.py)
- Token refresh scheduling — [coordinator.py](custom_components/hellofresh/coordinator.py)
- Setup / reauth (email + password, or pasted token) — [config_flow.py](custom_components/hellofresh/config_flow.py)
- Normalization tests — [tests/test_api.py](tests/test_api.py)

> **Module layout note:** the integration was originally a single `api.py`. It is now split across the modules above, with [api.py](custom_components/hellofresh/api.py) kept as a thin re-export shim so `from .api import ...` keeps working. Import from the specific modules in new code.

## Overview

- Authentication uses a short-lived bearer access token plus a long-lived refresh token.
- The integration refreshes the bearer token on a dedicated timer, decoupled from data polling, so it never lapses between polls (see [Token lifecycle](#token-lifecycle-and-refresh)).
- The client prefers authenticated account endpoints, and also queries authenticated profile and delivery-history endpoints when available.
- If account menu data is unavailable, it can fall back to scraping the public `/menus` page.
- Read endpoints are normalized into stable internal models:
  - `HelloFreshSubscription`
  - `HelloFreshWeek`
  - `HelloFreshRecipe`
  - `HelloFreshMarketItem`
  - `HelloFreshOrder`
  - `HelloFreshAccountData`
- Home Assistant entities are derived from normalized account data rather than directly exposing raw API payloads.
- Write actions for meal selection and HelloFresh Market add-ons (both `PUT /gw/v1/carts/{week}`) and skip/unskip (`PATCH …/delivery_dates/{week}`) are HAR-verified; each keeps a set of older guessed endpoints as a fallback only.

## Regional Base URLs

The integration supports these regions:

| Country code | Base URL |
| --- | --- |
| `us` | `https://www.hellofresh.com` |
| `ca` | `https://www.hellofresh.ca` |
| `uk` | `https://www.hellofresh.co.uk` |
| `au` | `https://www.hellofresh.com.au` |
| `de` | `https://www.hellofresh.de` |
| `nl` | `https://www.hellofresh.nl` |

Default region: `us`.

## Authentication

The integration authenticates the same way the HelloFresh web app does: it logs in with the account **email and password** through HelloFresh's own `/gw` auth gateway and then renews the resulting short-lived access token with a long-lived refresh token. There is no OAuth app or Auth0 `/oauth/token` exchange — the access/refresh tokens are obtained and maintained entirely at runtime.

Setup offers **two paths** (a menu in [config_flow.py](custom_components/hellofresh/config_flow.py)):

1. **Credentials (recommended)** — store email + password; the runtime logs in and self-heals across token expiry/rotation indefinitely.
2. **Token (advanced backup)** — paste the website's `apiV2Auth` value (a JSON auth object, plain or URL-encoded) or a bare access token. No credentials are stored, so the entry works only until the refresh token expires (~60 days) or HelloFresh rotates/invalidates it, at which point a reauth prompt is raised. This path exists as a fallback for when the Cloudflare bot-protection bypass cannot complete a credential `/gw/login` (see [Bot-protection handling](#bot-protection-waf-handling)). The paste is parsed by `parsers.token_payload_to_entry_data` (URL-decoded if needed; missing timing backfilled from the access token's JWT `iat`/`exp`).

Either way, `async_setup_entry` requires an entry to carry **either** credentials **or** a token; a stale entry with neither triggers reauth. Token-only entries are intentional and are never forced to supply a password — reauth for them re-collects a token.

Authenticated API calls send a full Chrome-on-Windows-11 header set (`_DEFAULT_HEADERS` + the shared `_BROWSER_CLIENT_HINTS`):

```http
Authorization: Bearer <access_token>
Accept: application/json, text/plain, */*
Accept-Language: en-US,en;q=0.9
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36
Priority: u=1, i
Accept-Encoding: gzip, deflate, br, zstd
Cache-Control: no-cache
Pragma: no-cache
sec-ch-ua: "Google Chrome";v="138", "Chromium";v="138", "Not)A;Brand";v="24"
sec-ch-ua-mobile: ?0
sec-ch-ua-platform: "Windows"
sec-ch-ua-platform-version: "15.0.0"
Sec-Fetch-Dest: empty
Sec-Fetch-Mode: cors
Sec-Fetch-Site: same-origin
Origin: <regional base URL>
Referer: <regional base URL>/
```

The `<token_type>` from the auth object is used in place of `Bearer` when the server returns a different one.

The integration presents as **Google Chrome on Windows 11** rather than a headless identifier — HelloFresh's bot protection challenges recognizable non-browser clients (see [Bot-protection handling](#bot-protection-waf-handling)). A real Chrome emits all of the above on every XHR, and the *absence* of the Client Hints / `Sec-Fetch-*` metadata is itself a fingerprint tell, so they are sent alongside the `User-Agent`. Notes on internal consistency (mismatched fields are exactly what fingerprinting looks for):

- **Windows 11 is invisible in the legacy UA string** — it still reports `Windows NT 10.0; Win64; x64`, the same as Windows 10, by design. The only header that distinguishes Windows 11 is the high-entropy `sec-ch-ua-platform-version` client hint (`"15.0.0"`; Windows 11 maps to `13.0.0`+, Windows 10 stays at `10.0.0` or below).
- **The Chrome major version is a single source of truth** (`_CHROME_MAJOR_VERSION` in `token_manager.py`): both the `User-Agent` `Chrome/NNN` token and the `sec-ch-ua` brand versions derive from it, so they can never drift apart. Bump it periodically to track Chrome stable.
- **`Accept-Encoding` is computed from the decoders actually installed** (`_browser_accept_encoding`): `br`/`zstd` are only advertised when their decoder is importable, because aiohttp would otherwise hand back undecodable bytes. A "Chrome" UA that omits `br` is itself a tell, so the integration's `manifest.json` pins `Brotli` to guarantee `br` is advertised in production. `zstd` is advertised only if a zstandard module is present.
- `Origin`/`Referer` point at the regional base URL so `Sec-Fetch-Site: same-origin` is consistent with an in-page XHR.

> **These changes only address the application (HTTP) layer.** They do **not** change the TLS or HTTP/2 fingerprint. All HelloFresh regional properties sit behind **Cloudflare**, and `aiohttp` (Python + OpenSSL) produces a non-browser **JA3/JA4 TLS fingerprint** and HTTP/2 settings/header-order that do not match Chrome. A region with stricter Cloudflare Bot Management — observed on **`www.hellofresh.co.uk`**, which returns an HTML `403` to the integration while the US property accepts the identical headers — rejects the request on the TLS/transport fingerprint *before the headers are even evaluated*, so no header change can fix it. Defeating that requires a browser-impersonating TLS stack. The integration now does this for **both the auth POSTs and the authenticated data XHRs**: when `curl_cffi` is installed it routes those requests through a real Chrome TLS/HTTP2 fingerprint (see [TLS-impersonating transport](#tls-impersonating-transport) below), falling back to `aiohttp` when it is not.

All `/gw` auth endpoints take the same regional query string (built by `_auth_query`):

```text
?country=<CC>&locale=<locale>
```

where `<CC>` is the **API country code** and `<locale>` the **API locale** for the
configured region — *not* simply the uppercased config-flow key. The config key is the
base-URL selector and is not always the ISO 3166 code the API wants. The mapping
(`api_country_code` / `api_locale` in `const.py`):

| Config key | Country code | Locale |
| --- | --- | --- |
| `us` | `US` | `en-US` |
| `ca` | `CA` | `en-CA` |
| `uk` | **`GB`** | **`en-GB`** |
| `au` | `AU` | `en-AU` |
| `de` | `DE` | `de-DE` |
| `nl` | `NL` | `nl-NL` |

The `uk → GB` mapping is essential: sending `country=UK` makes `/gw/login` and
`/gw/refresh` fail, which is why the integration previously only worked in the US.
Confirmed from a UK HAR where the site posts `{"country":"GB"}` to `/gw/auth/email/status`.
A subscription's own `locale` from the account payload overrides the default locale once
loaded.

All three auth POSTs (`_auth_headers`) present the same **Chrome-on-Windows-11 header set** — the current Chrome `User-Agent`, `Accept-Language`, `Priority`, the `sec-ch-ua*` Client Hints and `Sec-Fetch-*` metadata (shared `_BROWSER_CLIENT_HINTS`), and `Origin`/`Referer` derived from the regional base URL (the `Referer` is `<base>/login`, matching the login page the web app sends these from). HelloFresh fronts its endpoints with bot protection that fingerprints non-browser clients; a recognizable headless `User-Agent` is challenged with an HTML block page instead of a JSON API response. Presenting a full browser header set is a best-effort way past that layer and can break whenever the protection is retuned.

### Bot-protection (WAF) handling

A `401`/`403` whose body is **HTML** (or whose `Content-Type` contains `html`) is treated as an edge bot-protection block, **not** an API credential rejection (`_looks_like_bot_block`):

- on `/gw/login` and `/gw/refresh`, an HTML `401`/`403` raises a **transient `HelloFreshError`**, not `HelloFreshAuthError`
- this keeps a block from surfacing to the user as "wrong password": the coordinator treats it as `UpdateFailed` / a skipped proactive refresh and retries on the next poll, rather than raising `ConfigEntryAuthFailed` and prompting for reauthentication
- because the block raises `HelloFreshError` (not `HelloFreshAuthError`), the refresh-then-login fallback does **not** fire — the integration will not hammer the same WAF with a credential login, and the existing refresh token is preserved
- a `401`/`403` with a JSON body is still a genuine credential/refresh-token rejection and raises `HelloFreshAuthError` as before

**Regional Cloudflare differences (why some regions block even with perfect headers).** Every HelloFresh property is fronted by **Cloudflare** (confirmed by `server: cloudflare` / `cf-ray` on all six regional HARs). The bot-management *aggressiveness*, however, differs per region. The US property accepts the integration's requests; **`www.hellofresh.co.uk` returns an HTML `403`** to the same header set. Because Cloudflare evaluates the **TLS (JA3/JA4) and HTTP/2 fingerprint** of the connection *before* the application headers, and `aiohttp` (Python + OpenSSL) has a fingerprint no `User-Agent` can disguise, **no header change fixes a region tuned to block on transport fingerprint** — the request is rejected before the headers matter. Options if a region stays blocked:

- **TLS-impersonating transport (implemented)** — the auth POSTs **and the data XHRs** now go through `curl_cffi` when it is installed, giving the whole request flow a real Chrome JA3/JA4 + HTTP/2 fingerprint. See below.
- **Accept the retry behavior** — if `curl_cffi` is unavailable (or still blocked), the block is treated as transient (above), so the integration keeps retrying; it will succeed in windows where the region's rules are relaxed, but may stay blocked indefinitely if they are not.
- **Front the auth through a real/headless browser** (e.g. Playwright) to obtain the tokens, then continue with `aiohttp` for the data calls. Highest fidelity, highest weight — not implemented.

### TLS-impersonating transport

[tls_transport.py](custom_components/hellofresh/tls_transport.py) routes **both** the three `/gw` auth POSTs (`/gw/auth/token`, `/gw/login`, `/gw/refresh`) **and the authenticated data XHRs** (every call through `_async_api_request` / `_async_api_get` — subscriptions, deliveries, menus, billing, cart pricing, meal-selection, etc.) through [`curl_cffi`](https://github.com/lexiforest/curl_cffi) with `impersonate="chrome"`, which performs the TLS handshake and HTTP/2 framing with a **real Chrome fingerprint** — the part `aiohttp` cannot fake and the part stricter-region Cloudflare blocks on. `curl_cffi` is pinned in [manifest.json](custom_components/hellofresh/manifest.json) so Home Assistant installs it.

The data calls need the same impersonation as the auth calls: Cloudflare fingerprints the **connection**, which is identical whether the request is a login POST or a menu GET, so a region tuned to block the transport would otherwise let login through (now impersonated) but still 403 the very next data poll.

Two entry points share one curl_cffi core:

- `async_request(session, method, url, …)` — the general path, used by `_async_api_request` for any verb (GET/POST/PATCH/…). Its aiohttp fallback uses `session.request`.
- `async_auth_post(session, url, …)` — a POST convenience for the auth calls. Its aiohttp fallback uses `session.post`, exactly preserving the original auth-POST behavior.

Design notes:

- **Graceful degradation.** Both entry points fall back to the `aiohttp` session when `curl_cffi` is not importable **or** if a `curl_cffi` call raises at the transport level, so a missing/broken optional dependency never makes things worse than the `aiohttp`-only baseline. `TokenManager` logs (at debug) which transport is active on startup.
- **Uniform response.** The `aiohttp` path returns its native response; the `curl_cffi` path returns a small `AuthResponse` adapter exposing the same `status` / `headers` / awaitable `text()` / `json()` slice that callers and `_async_response_json` use, so the WAF/bot-block handling and JSON decoding are identical regardless of transport.
- **Per-request curl session.** Each impersonated call opens and closes its own `curl_cffi` `AsyncSession`. This is simple and correct; if data-call volume makes that overhead matter, a single long-lived `AsyncSession` could be pooled on the client.
- **Impersonation target** is the rolling `"chrome"` alias rather than a pinned `chromeNNN`, so a `curl_cffi` upgrade that drops an old version token doesn't break the integration.
- **Certificate verification stays on.** The `curl_cffi` request passes `verify=True` explicitly (`tls_transport.py`), so impersonating Chrome's *fingerprint* never silently disables TLS certificate validation — the connection is still authenticated against the CA store like the `aiohttp` path.

### Login flow (`/gw/auth/token` → `/gw/login`)

A full login runs in two steps, mirroring the web app:

| Step | Purpose | Method | Path | Body / params |
| --- | --- | --- | --- | --- |
| 1 | Prime the gateway with an anonymous app token | `POST` | `/gw/auth/token` | `?grant_type=client_credentials&client_id=senf` |
| 2 | Exchange credentials for a user-scoped auth object | `POST` | `/gw/login` | `{"username": "<email>", "password": "<password>"}` |

Notes:

- The `client_id` is `senf` (the web app's `NEXT_PUBLIC_GW_CLIENT_ID`), defined as `GW_CLIENT_ID` in [const.py](custom_components/hellofresh/const.py).
- Step 1's response is **not retained** — the app token only primes the gateway. The request is best-effort: any failure is logged and ignored. A later fresh-login capture showed the web app reaching `/gw/login` **without** a preceding `/gw/auth/token` call and **without** an `Authorization` header, so step 1 appears optional; it is kept as harmless defensive priming.
- Step 2 is HAR-verified: `POST /gw/login?country=<CC>&locale=<locale>` with body exactly `{"username", "password"}`, no `Authorization` header. It returns the auth object below. A `401`/`403` raises `HelloFreshAuthError` (bad credentials); any other `>= 400` raises `HelloFreshError`. (The login *response* body was redacted from the capture, but its field shape is the same auth object returned by `/gw/refresh`, which we do have.)

### Auth object

Both `/gw/login` and `/gw/refresh` return the same JSON auth object shape:

```json
{
  "access_token": "<jwt>",
  "expires_in": 1800,
  "refresh_token": "<refresh-token>",
  "refresh_expires_in": 5184000,
  "token_type": "Bearer"
}
```

Observed behavior:

- `access_token` lifetime is often only 30 minutes (`expires_in: 1800`)
- `refresh_token` lifetime can be much longer, for example 60 days (`refresh_expires_in: 5184000`)

`_apply_auth_object` adopts the response:

- `access_token` and (when present) `token_type` replace the cached values
- `_token_issued_at` is reset to **now** (the integration's own clock), and `expires_in` / `refresh_expires_in` are taken from the response with explicit `None` checks so a real `0` or a smaller server value is honored rather than masked by a stale local value
- if the response includes a `refresh_token`, it is stored and `_refresh_token_issued_at` is reset to now (a rotated refresh token starts its own ~60-day clock); if none is returned, the existing refresh token and its original issue time are kept
- the new credentials are pushed to the config entry via the `token_refresh_callback` so they survive a restart

The JWT `access_token`'s own `iat`/`exp` claims are read **only** when explicit timing metadata is absent, and only to surface expiry for diagnostics (`_apply_jwt_token_timing`); they are never trusted for authorization.

### Refresh-token exchange (`/gw/refresh`)

When a live (non-expired) refresh token is available, the access token is renewed without a full login:

| Purpose | Method | Path | Body |
| --- | --- | --- | --- |
| Renew expired bearer token | `POST` | `/gw/refresh` | `{"refresh_token": "<refresh_token>"}` |

The `/gw` auth POSTs (login, refresh, app-token) send the browser-like header set built by `_auth_headers()`:

```http
Accept: application/json, text/plain, */*
Accept-Language: en-US,en;q=0.9
Content-Type: application/json
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36
Priority: u=1, i
Accept-Encoding: gzip, deflate, br, zstd
Cache-Control: no-cache
Pragma: no-cache
sec-ch-ua: "Google Chrome";v="138", "Chromium";v="138", "Not)A;Brand";v="24"
sec-ch-ua-mobile: ?0
sec-ch-ua-platform: "Windows"
sec-ch-ua-platform-version: "15.0.0"
Sec-Fetch-Dest: empty
Sec-Fetch-Mode: cors
Sec-Fetch-Site: same-origin
Origin: <base_url>
Referer: <base_url>/login
```

Refresh-response handling (`_async_refresh_with_token`):

- a `401`/`403` raises `HelloFreshAuthError` — the refresh token is dead, reused, or rotated away
- any other `>= 400` raises `HelloFreshError` and is treated as **transient**: it is logged, the current access token keeps being used until it actually expires, and the reactive 401-retry path surfaces a real auth failure later (raising `HelloFreshError` rather than `HelloFreshAuthError` here avoids triggering a spurious login)
- a successful response is fed through `_apply_auth_object` (access token replaced, refresh token rotation honored, `issued_at` reset to now)

### Refresh-then-login fallback

`_async_refresh_access_token` chooses the cheapest path that can work:

1. If a refresh token exists and has not passed its known lifetime, try `POST /gw/refresh`.
2. If that refresh is **rejected** (`HelloFreshAuthError`), or there is no usable refresh token, fall back to a full `/gw/auth/token` → `/gw/login` login using the stored credentials.
3. If the refresh was rejected and **no credentials are configured**, the auth error is re-raised so the coordinator can prompt for reauthentication.

Login itself is only possible when both a username and password are stored (`_has_credentials`). A forced refresh with no credentials and no usable refresh token raises `HelloFreshAuthError`.

### Token lifecycle and refresh

Access tokens are short-lived (≈30 min) while the data poll interval can be hours, so token renewal cannot be tied to data polling. The integration manages token freshness in three coordinated places:

1. **Proactive refresh decision** ([client.py](custom_components/hellofresh/client.py), `_token_expiring_soon`): the token is considered "due for refresh" once it has passed **half its lifetime**, or is within `_TOKEN_MIN_REMAINING_BEFORE_REFRESH` (300 s) of expiry, whichever comes first. The half-life window is intentionally wide so a periodically-firing timer reliably lands inside it before expiry. (Missing expiry metadata is treated as "refresh now.")
2. **Dedicated refresh timer** ([coordinator.py](custom_components/hellofresh/coordinator.py), `async_start_token_refresh`): an `async_track_time_interval` timer, independent of the data poll, calls `client.async_ensure_token_fresh()`. Its cadence is derived from the token lifetime (`TOKEN_REFRESH_LIFETIME_FRACTION = 0.25`, i.e. a quarter of the lifetime), clamped to 2–10 minutes. A quarter-lifetime tick guarantees at least one firing inside the back-half refresh window.
3. **Reactive retry** ([client.py](custom_components/hellofresh/client.py), `_async_api_request`): after a single `401`/`403` on an authenticated call, the client forces one refresh and retries the request once. The forced refresh happens under `_token_refresh_lock` and re-checks whether the access token already changed first, so when many concurrent requests `401` together only the first rotates the refresh token (HelloFresh invalidates a refresh token on use, so a second rotation would burn the token the first waiter just obtained).

> **Why both a wide window and a sub-lifetime tick?** An earlier version refreshed only within 5 min of expiry on a 20-min timer; the timer stepped over that narrow window and the token died for ~10 min each cycle. Refreshing at half-life with a quarter-lifetime tick removes that gap. See the regression test `test_token_refresh_timer_never_lets_token_expire`.

A concurrency lock (`_token_refresh_lock`) ensures only one refresh runs at a time; both the proactive and reactive paths re-check expiry inside the lock so simultaneous callers don't refresh twice.

**Persisting refreshed tokens without a reload:** when a token is refreshed, the new token cache is written back to the config entry so it survives a restart. Because the live client already holds the new token in memory, the integration flags this as a token-only update (`TOKEN_ONLY_UPDATE_KEY` in [\_\_init\_\_.py](custom_components/hellofresh/__init__.py)) so the config-entry update listener skips the otherwise-costly full integration reload. The user's email and password live in `entry.data` and are owned by the runtime login/refresh flow; options store only user preferences (scan interval, public-menu fallback toggle, and the past-history window — `history_weeks`). Changing the `history_weeks` option triggers a full integration reload (it is consumed when the client is constructed), whereas a token-only update deliberately skips the reload.

**Refresh-token expiry:** `_refresh_token_expired` compares `refresh_token_issued_at + refresh_expires_in` against now (anchored to when the *refresh token* was issued — login or the last rotation — not the access token's issue time). When the refresh token has expired, the client skips `/gw/refresh` and goes straight to a credential login. Only if that login also fails (or no credentials are stored) does it raise `HelloFreshAuthError`, which the coordinator turns into a Home Assistant reauthentication prompt.

**Recovering a partially-valid token after a reboot:** a proactive (half-life) refresh that fails with `HelloFreshAuthError` is tolerated when the current access token has not yet hard-expired (`_access_token_still_valid`) — e.g. just after a reboot when the stored access token still has life but the refresh token was already rotated in a prior session. The integration logs a warning and keeps using the existing token; the reactive 401 path surfaces a genuine expiry later.

The logged-in web app sends these feature/versioning headers on its authenticated account and menu XHRs, and the integration now sends them on **every** authenticated request (`_FEATURE_HEADERS`):

```http
X-Market-API-Version: 2
X-Food-Categorization: v1
x-sort-variations-by-quantity: true
```

They pin the API/categorization variant the server replies with, so the integration's traffic matches the browser's and is guarded against payload-shape drift from an un-negotiated default. Per-endpoint headers (e.g. `x-requested-by: client-platform` / `shopping-experience-web` / `shipping-and-tracking`) are layered on top of these for the specific calls that the HAR showed using them.

## Request Efficiency

Because the read surface is reverse-engineered, several account flows probe a list of candidate endpoints in order until one returns a usable payload. To avoid re-running the doomed probes on every poll, the client keeps two persistent caches:

- **Preferred endpoints** (`_preferred_endpoints`): once a candidate succeeds, its identity — `(path, sorted-param-keys)`, ignoring param *values* so it still matches when week ids/ranges change — is remembered per `(category, subscription_id)`. The next poll reorders that category's candidate list to try the winner first, falling back to the full list only if it stops working. Applies to the **deliveries**, **menu**, and **past-delivery history** probes, and to the best-effort **write** probes (skip/unskip/select), where the winning `(method, path-template, payload-shape)` combo is remembered so a confirmed write path stops re-probing dead combinations.
- **Cart pricing** (`_cart_price_cache`): the exact box total is a deterministic function of the cart-pricing request body (selection, week, address, box size), so the response is cached by a hash of `(path, params, body)`. An unchanged cart is not re-`POST`ed on the next poll; changing the meal selection changes the body and naturally invalidates the entry.

> ETag / `If-None-Match` conditional GETs are **not** implemented: the existing HAR captures don't confirm the server sends `ETag`s on these endpoints, and a correct implementation would require the request layer to own response decoding (a `304` has no body). This is worth revisiting if a fresh capture shows `ETag` headers.

## Endpoint Matrix

### Verified auth check

This endpoint is the integration's primary token validation target:

| Purpose | Method | Path |
| --- | --- | --- |
| Validate token and load subscriptions | `GET` | `/gw/api/customers/me/subscriptions` |

Expected top-level shape (`{count, total, take, skip, items[]}`, simplified):

```json
{
  "items": [
    {
      "id": "sub-123",
      "isActive": true,
      "pausedAt": null,
      "canceledAt": null,
      "customer": {
        "id": "acct-123",
        "locale": "en-US"
      },
      "plan": {
        "name": "Classic",
        "numberOfRecipes": 3,
        "numberOfPersons": 2
      }
    }
  ]
}
```

> **Status is derived, not a field.** The live payload carries **no** `status` / `subscriptionStatus` / `state` key (confirmed from US HARs). Plan-level status is reconstructed by `_derive_subscription_status`: `canceledAt` → `cancelled`, else `pausedAt` → `paused`, else `isActive` → `active`/`inactive`. An explicit `status`/`state` field, if a region ever provides one, still wins. `endlessPausedAt` is deliberately ignored — it carries a stale historical date even on active accounts. This backs the `subscription_status` sensor.

### Upcoming deliveries

The client tries these read endpoints in order for each subscription until one returns a payload that can be normalized into weeks. The **ranged `/gw/api/customers/me/deliveries`** is the HAR-verified endpoint the live US site uses (it returns past + future weeks in one call) and is tried first; the rest got zero hits in the capture and are retained only as drift/other-region fallbacks. The sticky-endpoint cache (see [Request Efficiency](#request-efficiency)) means the winner is reused on later polls instead of re-walking this list.

| Priority | Method | Path | Params | Status |
| --- | --- | --- | --- | --- |
| 1 | `GET` | `/gw/api/customers/me/deliveries` | `rangeStart=<YYYY-Www>&rangeEnd=<YYYY-Www>` | HAR-verified (US) |
| 2 | `GET` | `/gw/my-deliveries/upcoming-deliveries` | `subscription=<id>` | fallback |
| 3 | `GET` | `/gw/my-deliveries/upcoming-deliveries` | `subscription=<id>&from=<YYYY-Www>` | fallback |
| 4 | `GET` | `/gw/my-deliveries/deliveries` | `subscription=<id>` | fallback |
| 5 | `GET` | `/gw/api/customers/me/deliveries` | `subscription=<id>` | fallback |
| 6 | `GET` | `/gw/api/customers/me/subscriptions/{subscription_id}/deliveries` | none | fallback |

`rangeStart`/`rangeEnd` span **the configured history window back to 6 weeks ahead** (`_build_delivery_history_range`). The history depth is `self._history_weeks` — the user-set `history_weeks` option (default **26**, range **1–104**; see [History window](#past-deliveries--delivered-history)) — rather than a fixed value. The forward window must reach beyond the next box so the upcoming-delivery sensors see subsequent scheduled weeks — an earlier `+1 week` end capped `upcoming_delivery_count` at the current delivery only.

Recognized top-level arrays:

- `weeks`
- `items`
- `deliveries`

The ranged deliveries payload carries per-week **counts, dates, deadlines, `allowedActions`, and tracking** but **no recipe list** — the chosen recipes are not in this response. Recipe data and the per-recipe selection state come from the authenticated menu endpoint instead (see [Selection-state resolution](#selection-state-resolution)). When a delivery payload *does* list recipes (some account shapes), they are still parsed as a fallback.

**Actual delivered timestamp.** Each week's `tracking` node carries `delivery_date` / `estimated_delivery_time` — once the box has arrived (effective status `DELIVERED`), that is the **real carrier delivery moment** in UTC (HAR-verified, e.g. `2026-06-29T22:20:50+0000`), unlike the week's `deliveryDate`, which is a scheduled-noon anchor. Before delivery the same field holds a scheduled placeholder, so `_delivered_at_from_raw` only trusts it on DELIVERED weeks (respecting the stale-status guard: `status="DELIVERED"` with a live non-delivered `state` doesn't count). It is stored as `HelloFreshWeek.delivered_at` and serialized with its full offset, so the cards render the delivered **date in the viewer's timezone** — an evening ET delivery is already the next day in UTC (`test_delivered_at_extracted_from_tracking_for_delivered_weeks_only`). The meal-planner card's order-strip "Delivered" field and the schedule card's past rows and calendar marks all prefer `delivered_at` over the scheduled date. (The same node also appears on `GET /gw/api/subscriptions/{id}/delivery_dates/{week}`.)

### Order history and payment dates

The logged-in US site also calls a separate order-history endpoint:

| Purpose | Method | Path | Params |
| --- | --- | --- | --- |
| Read order history and resolve payment dates | `GET` | `/gw/api/customers/me/orders` | `country=<cc>&locale=<locale>&limit=<n>` |

Observed request example:

```text
/gw/api/customers/me/orders?country=us&locale=en-US&limit=200
```

Observed response shape:

```json
{
  "count": 200,
  "items": [
    {
      "orderNr": "29236713642",
      "createdAt": "2026-06-04T00:13:06-0700",
      "grandTotal": 76.93,
      "shippingAmount": 0,
      "orderLines": [
        {
          "deliveryDate": "2026-06-15T00:00:00-0700",
          "deliveryTime": "US-1-0800-2000",
          "subscription": {
            "id": "6959884"
          }
        }
      ]
    }
  ]
}
```

The integration uses this endpoint to populate `recent_payment_date` and `next_payment_date` on each subscription, and to compute the next-box total:

- `recent_payment_date` is the `createdAt` date of the most recently created order **that has already been charged** (`createdAt <= today`) for that subscription — i.e. the customer's last actual charge. HelloFresh bills a box several days before its delivery date, so an upcoming box can already be the most recent charge; filtering on `deliveryDate` (an earlier approach) instead reported the *prior* delivered box and left this date ~a week behind the real last charge.
- `next_payment_date` is the `deliveryDate` of the soonest upcoming order (delivery date on or after today) for that subscription
- if no upcoming order is found in the orders response, `next_payment_date` falls back to `next_cutoff_date + 1 second` from the subscription payload (a provisional estimate that the billing-API value overrides whenever it is available)
- `next_delivery_total` (the `next_box_total_price` sensor) is the **sum of `grandTotal` across all order items sharing the earliest upcoming delivery date** — a single delivery can have multiple charges (box + add-ons + fees), so they are accumulated rather than deduplicated
- `recent_order_id` (the `Next delivery order ID` sensor) is the `orderNr` of that earliest upcoming order

The subscription id is extracted from `orderLines[0].subscription.id`. If that field is absent or null, the order item is skipped.

### Payment date fallback

If the orders endpoint fails or returns no usable data, the integration falls back to the account balance transactions feed:

| Purpose | Method | Path | Params |
| --- | --- | --- | --- |
| Fallback recent-payment lookup | `GET` | `/gw/payments/balance/transactions` | `customerUUID=<uuid>&types=DEBIT` |

The customer UUID is extracted from nested `uuid` fields on the subscription's raw payload. The integration reads `createdAt` from `DEBIT` transactions and uses the latest date as `recent_payment_date` for subscriptions where the orders endpoint produced no result.

### Account credit balance

The integration reads the account credit balance — the amount the website surfaces as "$X that will apply automatically to your next order" — from a dedicated payments endpoint:

| Purpose | Method | Path | Params |
| --- | --- | --- | --- |
| Account credit balance | `GET` | `/gw/payments/customers/{customerUUID}/balance` | `business_unit=<CC>&country=<CC>` |

The customer UUID is extracted from nested `uuid` fields on the subscription's raw payload (same source as the transactions feed above). The response is a flat object:

```json
{
  "amount": 0,
  "cash": 0,
  "bonus": 0,
  "currencyCode": "USD",
  "restrictedAmount": 0,
  "cancellableCredits": 0
}
```

`amount` is the spendable credit that applies to the next order and backs the `account_credit` sensor; `currencyCode` becomes the sensor's unit. The lookup is best-effort — a missing UUID or a failed/non-object response leaves `account_credit` unset rather than raising.

### Account profile / customer attributes

The integration now probes authenticated account-profile endpoints for long-lived account metrics that are not present in the subscription or upcoming-delivery payloads:

| Priority | Method | Path | Purpose |
| --- | --- | --- | --- |
| 1 | `GET` | `/gw/api/customers/me/info` | Read account-level profile fields such as delivered box counts |
| 2 | `GET` | `/gw/customer-attributes-service/attributes` | Fallback source for account attributes |

Observed params for `/gw/api/customers/me/info`:

```text
/gw/api/customers/me/info?country=US&locale=en-US
```

Observed request header (confirmed from HAR capture):

```http
x-requested-by: client-platform
```

Current normalization is intentionally narrow. The client extracts stable account-level metrics such as:

- `boxesReceived`
- `boxes_received`
- nested `deliveredBoxes`

Those values are normalized into `HelloFreshAccountData.boxes_received` and exposed through the Home Assistant `boxes_received` sensor. A value of `0` is treated as a valid result (new subscribers who have not yet received a box); the fallback to the second candidate path only runs when the first path returns `None`.

### Past deliveries / delivered history

The integration now also probes authenticated history endpoints that carry delivered-week summaries and recipe history:

| Priority | Method | Path | Params |
| --- | --- | --- | --- |
| 1 | `GET` | `/gw/customer-complaints/users/me/deliveries` | none |
| 2 | `GET` | `/gw/api/customers/me/deliveries` | `country=<CC>`, `locale=<locale>`, `rangeStart=<YYYY-Www>`, `rangeEnd=<YYYY-Www>` |
| 3 | `GET` | `/gw/my-deliveries/past-deliveries` | `country=<CC>`, `locale=<locale>`, `rating-scale=5`, `subscription=<id>`, `from=<YYYY-Www>` (pagination cursor) |

These endpoints are **not interchangeable**, and this matters for getting past-week meal selection right:

- `/gw/customer-complaints/users/me/deliveries` only knows the most recent couple of weeks.
- `/gw/api/customers/me/deliveries` (ranged) returns ~a year of weeks but as **metadata-only shells with no recipe list**.
- `/gw/my-deliveries/past-deliveries` is the comprehensive source that carries the **actually-delivered recipes per week** (`weeks[].meals[]` with stable recipe ids). It is **paginated**: each page returns ~4 delivered weeks plus a `nextWeek` cursor, and only weeks that actually shipped appear (paused/skipped weeks are absent, so the cursor jumps over them).

Rather than returning the first endpoint that answers, the integration **accumulates recipe-bearing weeks from every candidate keyed by week id, with `past-deliveries` winning per week**, and follows the `nextWeek` cursor back to the history floor. A previous version returned whichever endpoint answered first; because the recipe-less candidates often win, an old week never received its delivered meals and the dashboard showed a fabricated selection (regression test `test_past_delivery_history_prefers_recipe_bearing_endpoint`).

**History window.** Both the ranged display range and the `past-deliveries` pagination floor are driven by the **configurable** history depth — the `history_weeks` option (`CONF_HISTORY_WEEKS` in [const.py](custom_components/hellofresh/const.py)), default **`DEFAULT_HISTORY_WEEKS` = 26** (~6 months), range **1–104** (`MIN_HISTORY_WEEKS`/`MAX_HISTORY_WEEKS`). The client reads it as `self._history_weeks` (the `history_weeks` constructor arg, falling back to the `_HISTORY_LOOKBACK_WEEKS = 26` class default when unset). Lowering it shrinks the per-poll deliveries payload; raising it browses further back.

> Set the window to a bit more than a calendar year (**~56**) to safely include the box from ~12 months ago: `today − 52 weeks` lands 364 days back, so a 12-month-old box sits exactly on the boundary and ISO-week rounding dropped it (`get_weeks` returned `[]` for the ~370-days-ago week) when the window was exactly 52. The pagination floor is set **two weeks past** `self._history_weeks` (`weeks=self._history_weeks + 2`) so the oldest *visible* week always has its delivered recipes fetched, and the cursor floor is compared by `(year, week)` (not raw string) so a year boundary like `2026-W01` vs `2025-W52` is ordered chronologically.

Recognized top-level arrays include:

- `data`
- `items`
- `deliveries`
- `weeks` (the `past-deliveries` shape: `{ "weeks": [...], "nextWeek": "YYYY-Www" }`)

Recent US HAR captures showed `/gw/api/customers/me/deliveries` returning both delivered and future weeks in a single ranged response. Useful fields on those records include:

- `tracking.tracking_link`
- `tracking.tracking_code`
- `state`
- `subStatus`
- `availableOneOffOptions` — normalized into `HelloFreshWeek.available_one_off_options` (a small `handle` + `delivery_date` list of alternative delivery dates for the week) and surfaced in the recorder-safe week summary attributes
- `holidayDelivery`
- `allowedActions`

The goal is not to rebuild a full historical order ledger. Instead, the integration extracts a stable delivered-week history that can support summary entities and recent recipe context. Those records are normalized into `HelloFreshWeek` objects with `source = "past_deliveries"`.

### Account menu endpoints

The integration now prefers the same authenticated endpoint that backs the logged-in delivery menu page for a specific subscribed week:

| Priority | Method | Path | Params |
| --- | --- | --- | --- |
| 1 | `GET` | `/gw/my-deliveries/menu` | `customerPlanId`, `delivery-option`, `locale`, `postcode`, `preference`, `product-sku`, `servings`, `subscription`, `week` |

The current US site has been observed using query strings like:

```text
/gw/my-deliveries/menu?customerPlanId=<uuid>&delivery-option=<slot>&exclude=&exclude-feedback=true&include-filters=true&include-future-feedback=false&locale=en-US&postcode=<postcode>&preference=<planPreference>&product-sku=<sku>&servings=2&subscription=<id>&week=2026-W25
```

Those values can be sourced from the authenticated subscription and delivery payloads:

- `customerPlanId` from the subscription object
- `delivery-option` from `deliveryOption.handle`
- `postcode` from `shippingAddress.postcode`
- `preference` from `GET /gw/api/subscriptions/{subscription_id}/product_options`, specifically `unifiedPreferences.plans[customerPlanId].planPreference`
- `product-sku` from `product.sku`
- `servings` from `productType.specs.size` or the normalized subscription servings
- `subscription` from the subscription id
- `week` from the normalized delivery week id

### Product options / plan preference

The current US account UI also calls:

| Purpose | Method | Path | Params |
| --- | --- | --- | --- |
| Resolve current plan preference | `GET` | `/gw/api/subscriptions/{subscription_id}/product_options` | `country=<CC>&locale=<locale>` |

Observed payload fragment:

```json
{
  "unifiedPreferences": {
    "plans": {
      "<customerPlanId>": {
        "planPreference": "quick"
      }
    }
  }
}
```

This value is more reliable than the older subscription `preset` field when building the `/gw/my-deliveries/menu` request.

### Exact cart pricing

HAR captures from the logged-in US site also showed a dedicated cart-pricing request for the subscribed week:

| Purpose | Method | Path | Params |
| --- | --- | --- | --- |
| Calculate exact box total | `POST` | `/gw/v1/carts/{week}/price` | `isFutureWeek=<true|false>` |

Observed request shape:

```json
{
  "boxSize": 2,
  "isFirstOrder": false,
  "customerID": 15259216,
  "isRecurring": true,
  "subscriptionID": 6959884,
  "planID": "<customerPlanId>",
  "products": [
    {
      "handle": "US-CBU-3-2-0",
      "deliveryOption": "US-1-0800-2000",
      "hfWeek": "2026-W25",
      "unitPrice": 65.94
    },
    {
      "boxSku": "US-CBU-3-2-0",
      "handle": "US-CHARGE-0-0-0",
      "hfWeek": "2026-W25",
      "quantityPerCourse": [
        {"index": 68, "quantity": 1}
      ],
      "recipeIndexes": ["68"]
    }
  ],
  "shippingAddress": {
    "address1": "62 Leonard St",
    "postcode": "01930",
    "region": "MA"
  },
  "locale": "en-US",
  "country": "US"
}
```

Observed response fields:

```json
{
  "grandTotal": 97.5,
  "subTotal": 96.5,
  "shippingAmount": 10.99,
  "discountAmount": 9.99
}
```

The integration now uses this endpoint when it has enough authenticated delivery and menu metadata to build the request body. This allows `next_box_total_price` to reflect the exact total, including shipping and discounts, instead of relying only on partially populated delivery payloads.

### Lighter box-total fallback (`/gw/calculate`)

When the cart-price request above cannot be built or returns no recognizable total, the client falls back to the lighter pricing endpoint the web app also uses:

| Purpose | Method | Path |
| --- | --- | --- |
| Lightweight box total | `POST` | `/gw/calculate` |

HAR-confirmed request shape:

```json
{
  "isFirstOrder": false,
  "products": [{"handle": "US-CBU-3-2-0", "deliveryOption": "US-1-0800-2000"}],
  "skipOneOffCalculation": true,
  "isRecurring": true,
  "subscriptionID": 6959884,
  "customerID": 15259216,
  "shippingAddress": {"postcode": "01930"},
  "planID": "<customerPlanId>",
  "couponCode": null,
  "locale": "en-US",
  "country": "US"
}
```

HAR-confirmed response shape (the total is the top-level `grandTotal`, which `_extract_total_price` reads first):

```json
{
  "products": [{"handle": "US-CBU-3-2-0", "unitPrice": 65.94, "shippingAmount": 10.99, "currency": "USD"}],
  "grandTotal": 76.93,
  "subTotal": 65.94,
  "shippingAmount": 10.99,
  "discountAmount": 0.0,
  "currency": "USD"
}
```

Responses are cached by request fingerprint like the cart-price endpoint. (Richer fields — `subTotal`, `shippingAmount`, `discountAmount` — are available here but not surfaced as separate entities.)

`/gw/calculate` is used two ways, sharing `_build_calculate_payload`:

1. **Per-week fallback** (above): priced for a specific delivery week, overlaying `next_box_total_price`.
2. **Plan-level recurring price**: called once per refresh for the primary subscription with **no week** (`_build_calculate_payload(subscription, week=None)`), drawing the product handle and delivery option from the subscription itself. This is the standing weekly plan price shown in plan settings; its `grandTotal` (shipping included) backs the **`selected_plan_total_price`** sensor (`HelloFreshAccountData.selected_plan_total_price` / `_currency`, populated by `_async_enrich_selected_plan_price`). Best-effort: an unbuildable payload or missing total leaves the sensor unavailable.

If that endpoint cannot be built or does not return a recognizable payload, the client still probes older candidate menu endpoints before the structured-JSON and public-HTML menu fallbacks. These got **zero hits** in the US HAR (the live site uses `/gw/my-deliveries/menu`), so they are retained only as drift/other-region fallbacks and are tried last:

| Priority | Method | Path | Params |
| --- | --- | --- | --- |
| 2 | `GET` | `/gw/my-menu/weeks` | `subscription=<id>` |
| 3 | `GET` | `/gw/my-menu` | `subscription=<id>` |
| 4 | `GET` | `/gw/api/customers/me/menu` | `subscription=<id>` |
| 5 | `GET` | `/gw/api/customers/me/subscriptions/{subscription_id}/menu` | none |
| 6 | `GET` | `/gw/api/customers/me/subscriptions/{subscription_id}/weeks` | none |
| 7 | `GET` | `/gw/api/customers/me/subscriptions/{subscription_id}/menus` | none |

Recognized top-level arrays:

- `weeks`
- `items`
- `menus`

Menu week payloads may also wrap their recipe lists inside nested containers such as:

- top-level `meals`
- `recipes.items`
- `entries.nodes`
- `data.items`

The observed `/gw/my-deliveries/menu` response is a single week-like object rather than a `weeks` array. Relevant top-level keys have included:

- `id`
- `week`
- `meals`
- `mealsReady`
- `menuCollections`
- `categories`
- `filters`
- `sorting`
- `addOns`
- `modularity`

Each `meals[]` entry may wrap the actual recipe in a nested `recipe` object and may also include menu-only metadata such as `index`, `selection`, and `charge`.

#### `meals[]` selection and variants

- `meals[].selection.quantity` — when present and `> 0`, the meal is **currently chosen** (this is how `is_selected` is resolved for recipes). `meals[].selection.selected` (a bool) is honored when present.
- `meals[].charge` — `{label, unitAmount, totalAmount, reason, strategy}` for premium/variant meals; `label` (e.g. `+7.99/serving`) and `unitAmount` (cents) drive a tile's surcharge display.
- `meals[].recipe.label.text` — a badge such as `Premium Picks`.
- **`modularity[]`** — the variant system. Each group has a `defaultCourseIndex` (the base meal) plus `variations[]` / `addOns[]` whose `{index, title}` name how each variant differs (e.g. `"2x Bacon"`, `"Ground Turkey"`). The `index` equals a meal's own `index`, so the integration joins them to attach a human-readable `variation_title` to each recipe. This is what distinguishes same-named meals whose price/nutrition can otherwise look identical. In the meal-planner card, a variant tile shows its `variation_title`; the base meal in such a set (the one with **no** modifier, i.e. `defaultCourseIndex`) carries no modifier label. The integration also stamps each member of a group (base + every variation) with a `variation_group` equal to the group's `defaultCourseIndex`, so the card can cluster a dish's variants together **even when a variant carries a different name** (e.g. a Salmon dish whose group includes an "Icelandic Cod" swap) — grouping by name alone would scatter the renamed swap.

#### `addOns` — HelloFresh Market catalog

The week's Market add-ons (extras: appetizers, breakfast, desserts, proteins, sides, …) live under `addOns`:

```jsonc
"addOns": {
  "selectionLimit": 100,
  "groups": [
    {
      "groupType": "protein",            // appetizer | breakfast | dessert | lunch | protein | sides | goodchop | petstable | donation | lowprices
      "sku": "US-APR-0-0-0",
      "selectionLimit": 8,
      "addOns": [
        {
          "index": 10089,                 // cart selection unit for this extra
          "sku": "US-APR-0-0-0",
          "isLocked": false,
          "isSoldOut": false,
          "maxQuantity": 6,
          "recipe": { "id": "…", "name": "Steelhead Trout", "image": "…", "nutrition": { … } },
          "priceCatalog": { "basePrice": 899, "pricePerQuantity": [ … ] },   // cents
          "quantityOptions": [ { "quantity": 1, "totalAmount": 899 }, … ],
          // present ONLY when this add-on is currently selected:
          "selection": { "skipped": false, "oneOffQuantity": 1, "preselectedQuantity": 0 }
        }
      ]
    }
  ]
}
```

Key points (HAR-verified):

- An add-on is **selected** when it has a `selection` object; unselected add-ons have `selection: null`. The chosen quantity is `oneOffQuantity + preselectedQuantity` — **not** `quantity` (which is the meals field). `preselectedQuantity` is the recurring portion (carried week to week); `oneOffQuantity` is the this-week addition.
- `priceCatalog.basePrice` is the single-unit price in **cents**.
- The `modularity` pseudo-group inside `addOns.groups` carries no orderable add-on entries (it backs the meal-variation system above) and is ignored when building the market catalog.

### Structured menu catalog (`/gw/menus-service/menus`)

Before scraping HTML, the integration tries the structured-JSON menu catalog the live web app uses (HAR-confirmed):

| Purpose | Method | Path | Params |
| --- | --- | --- | --- |
| Structured regional menu catalog | `GET` | `/gw/menus-service/menus` | `country=<CC>`, `locale=<locale>`, `weeks=<W,…>`, `exclude=` |

Response shape: `{count, items[], skip, take, total}`. Each `items[]` entry is a week whose recipes live under a **`courses`** list, where each course wraps the recipe in a nested `recipe` object (the normalizer recognizes `courses` and unwraps `recipe`). **Caveat:** this catalog is large — a single week's response was observed at ~6.6 MB — so it is used only as a fallback when the per-week authenticated menu endpoints return nothing.

### Public menu fallback

If the authenticated and structured-JSON menu sources are both unavailable and fallback is enabled:

| Purpose | Method | Path |
| --- | --- | --- |
| Fetch public menu HTML | `GET` | `/menus` |

This is HTML, not JSON. Recipe names and visible menu labels are extracted from the page. Because it is a **top-level document load** rather than a CORS XHR, this request overrides the shared XHR headers with the navigation values a real browser sends for a page (`Accept: text/html,…`, `Sec-Fetch-Dest: document`, `Sec-Fetch-Mode: navigate`, `Sec-Fetch-Site: none`, `Sec-Fetch-User: ?1`, `Upgrade-Insecure-Requests: 1`) while keeping the same `User-Agent` and Client Hints.

## Normalized Data Model

### Subscription

`HelloFreshSubscription` fields:

| Field | Source keys |
| --- | --- |
| `subscription_id` | `id` |
| `account_id` | `customer.id` |
| `locale` | `customer.locale` |
| `status` | `status` |
| `display_name` | `name`, `displayName`, `plan.name`, `plan.displayName`, nested plan equivalents |
| `plan_name` | `plan.name`, `plan.displayName`, nested plan equivalents |
| `meals_required` | `plan.numberOfRecipes`, `plan.recipesPerWeek`, `mealsPerWeek`, `recipesPerWeek`, nested plan equivalents, `productType.specs.meals` |
| `servings` | `plan.numberOfPersons`, `plan.servings`, `numberOfPersons`, `servings`, nested plan equivalents, `productType.specs.size` |
| `delivery_address` | formatted from `shippingAddress`: `address1`, `city`, `region.code` (or `region.name`), `postcode` joined as a single line |
| `box_size` | `boxSize`, `size` |
| `shipping_method` | `shippingMethod`, `deliveryType` |
| `status` | `status` |
| `next_cutoff_date` | `nextCutoffDate` |
| `loyalty_boxes_received` | `loyaltyBoxesReceived`, `totalBoxesReceived`, nested profile fields |
| `loyalty_boxes_until_next_freebie` | `loyaltyBoxesUntilNextFreebie`, `boxesUntilNextFreebie`, nested profile fields |
| `recent_payment_date` | populated from order history (`createdAt` of the most recent order already charged, i.e. `createdAt <= today`) |
| `next_payment_date` | populated from order history (`deliveryDate` of soonest upcoming order), falls back to `next_cutoff_date + 1s` |

Nested plan-like objects such as `activePlan` or `subscriptionPlan` are also searched when the top-level `plan` object is absent.

`delivery_address` is redacted in Home Assistant diagnostics exports.

The raw subscription payload also contains useful next-week fallback fields that are not part of the normalized `HelloFreshSubscription` model directly but are used to synthesize an account week when delivery payloads are sparse:

- `nextDelivery`
- `nextDeliveryWeek`
- `nextModifiableDeliveryDate`
- `nextModifiableDeliveryWeek`
- `nextCutoffDate`
- `nextDeliveryOption.deliveryName`
- `productType.productName`
- `productType.specs.meals`

### Week / delivery

`HelloFreshWeek` is built from delivery-like payloads using these key fallbacks:

| Field | Source keys |
| --- | --- |
| `week_id` | `id`, `week`, `deliveryWeek`, `calendarWeek` |
| `display_name` | `label`, `title`, `displayName`, nested `name`, nested `displayName`, `deliveryName` |
| `delivery_date` | `deliveryDate`, `date`, `shipmentDate`, `expectedDeliveryDate` |
| `selection_deadline` | `selectionDeadline`, `cutoffDate`, `deadline` |
| `status` | `status`, `deliveryStatus` |
| `meals_required` | `mealsRequired`, `requiredMealCount`, `recipeCount`, `numberOfRecipes`, nested `meals`, subscription default |
| `meals_selected` | `mealsSelected`, `selectedMealCount`, `selectedRecipesCount`, `mealCountSelected`, counted selected recipes |
| `is_skipped` | `skipped`, `isSkipped`, `status == "skipped"` |
| `recipes` | `meals`, `recipes`, `selectedMeals`, `menuItems`, nested recipe collections under `menu`, `selection`, `box`, `delivery`, or wrapped containers |
| `menu_title` | `menuTitle`, `title`, nested `name`, nested `displayName` |
| `slot_label` | `timeSlot`, `slotLabel`, `deliveryName`, `deliveryFrom`, `deliveryTo` |
| `shipping_method` | `shippingMethod`, subscription default |
| `box_size` | `boxSize`, subscription default |
| `meals_preselected` | `mealsPreselected` — HelloFresh auto-picked the week's meals from the food profile; stays `true` until the customer swaps in different meals (it does **not** clear just because meals are present). Drives `auto_picked` / `needs_selection`. |
| `delivery_blocked` | `deliveryBlocked`, `isBlocked` — HelloFresh blocked delivery for the week (area out of zone, carrier/weather disruption, no-delivery holiday). Imposed by HelloFresh, distinct from a customer skip. |
| `holiday_delivery_date` | `holidayDelivery` — rescheduled date when the week's box is shifted for a holiday; `null` when no shift applies. |
| `holiday_message` | `holidayMessage` — HelloFresh's holiday-shift notice; `null` when none. |

Delivery recipe payloads may be wrapped in nested containers such as:

- `selection.entries.nodes`
- `recipes.items`
- `menu.items`

Verified authenticated delivery payloads for the US site also expose useful delivery metadata under nested objects such as:

- `product.displayName`
- `product.specs.meals`
- `product.price`
- `product.specialFee`
- `product.shippingPrice`
- `deliveryOption.deliveryName`

When available, cart-pricing responses can override those derived totals with exact `grandTotal` data.

When `/gw/my-deliveries/menu` succeeds, its recipe catalog is merged back into the corresponding normalized delivery week so the account week keeps its delivery date, deadline, and selected-meal counts while gaining the full set of available recipes.

#### Selection-state resolution

Which recipes you have *chosen* for a week (`is_selected`) is resolved differently for **upcoming/current** weeks and **past** weeks. Getting this right took several iterations, all worth recording.

**Upcoming / current weeks** (selection still editable):

- The **deliveries** endpoint returns no recipe list, so the account week starts with no recipes and no selection.
- The authenticated **menu** (`/gw/my-deliveries/menu`) returns the full catalog; each chosen meal arrives with `selection.quantity > 0` (unchosen meals carry only `selection.limit`). `_recipe_from_raw_meal` turns `quantity > 0` into `is_selected = True`, so the menu week's recipes are the authoritative selection source.
- `_merge_menu_weeks_into_account_weeks` combines them: **if the account week independently lists selected recipes**, that set is projected onto the catalog by `recipe_id`; **otherwise the menu week's own `is_selected` flags are preserved as-is.** The merge must *not* recompute selection from an empty account week, or every recipe collapses to unselected even though the menu said which were chosen (regression test `test_merge_preserves_menu_selection_when_account_week_has_no_recipes`).
- It must also **not fabricate** a selection. An earlier "if nothing is flagged, treat every account recipe as selected" fallback marked a whole catalog selected on past weeks. The fallback now only applies when the account week is a genuine *selection-sized* list (fewer recipes than the menu catalog), never when it holds a full catalog (`test_merge_does_not_fabricate_selection_on_catalog_sized_account_week`).
- The result: `is_selected` reflects your real picks, and each recipe carries its `course_index` for round-tripping a new selection back through `select_meals`.

**Past weeks** (selection no longer editable):

The planning menu's per-meal flags for a past week reflect the system's *default/auto-fill* picks, not what you actually received — so for past weeks the **delivered meals from `past-deliveries` are authoritative**. `_merge_past_delivery_recipes_into_account_weeks` applies them in two tiers, split by the **menu grace window** — the `menu_grace_weeks` user option ("Full menu history"; whole weeks since HelloFresh is weekly, default `DEFAULT_MENU_GRACE_WEEKS = 2`, range 0–3 where 0 disables the grace, in `const.py`), plumbed like `history_weeks` (options flow → client constructor → `menu_grace_weeks` property) and exposed to the meal-planner card on the `get_weeks` **account payload** so the card's `_isPast` gating uses the same configured value (`test_menu_grace_weeks_option_is_honored`):

- **Only weeks dated strictly before today** are touched at all. Once a week's cutoff passes, `past-deliveries` can start reporting the *current, still-editable* week as "delivered"; replacing its full browsable menu with just the 2–3 delivered meals would strip the customer's ability to see or change their options. The merge skips any account week with no `delivery_date` or one dated today-or-later, leaving its live menu intact (`test_merge_past_delivery_leaves_current_week_menu_intact`). "Past" is gated on **UTC** to match the rest of the module's date logic.
- **Inside the grace window** (delivered less than `menu_grace_weeks` ago) the week **keeps its full browsable catalog**: HelloFresh still publishes the real menu for the immediately previous week, and the per-week fetch validated the payload's week id, so the catalog is trustworthy. The delivered history stays the source of truth for the *selection* and is **overlaid** by `_overlay_delivered_selection`: every menu selection flag is cleared, catalog entries matching a delivered meal are re-selected (by recipe id, falling back to a case-insensitive name match, carrying over `selected_quantity`), and a delivered meal with no catalog match is appended (`test_merge_past_delivery_grace_window_keeps_catalog_with_delivered_overlay`, `test_merge_past_delivery_grace_window_appends_unmatched_delivered_meal`). A recent week whose menu fetch was rejected has no catalog and falls through to the delivered-only fill (`test_merge_past_delivery_grace_window_without_catalog_shows_delivered_only`).
- **Beyond the grace window** the delivered set from `past-deliveries` **replaces** whatever catalog the planning-menu endpoint attached — it is not merged onto it. For an old week HelloFresh has no real per-week menu, so `/gw/my-deliveries/menu` returns a bloated multi-week **aggregate** (~1000+ dishes spanning many weeks). There is **no reliable structural signal** (only fragile size heuristics) to tell that apart from a genuine per-week menu, so rather than risk flooding a past week with meals that were never available, the delivered recipes (which carry their images) win outright (`test_merge_past_delivery_shows_only_delivered_replacing_any_catalog`, `test_merge_past_delivery_older_than_grace_replaces_catalog`). The tradeoff — you can't browse "what else was available" for an *old* week — is deliberate; it is not worth an arbitrary threshold that would misclassify aggregates as real menus. The meal-planner card mirrors the same grace window in its `_isPast` gating (filter bar, past-week rendering).
- A delivered **market add-on** (appetizer/side/dessert) must *not* leak into the meal list. `_delivered_meals_only` drops delivered items that are known market items for the week (matched by id or name against the week's `addOns` catalog) before the replacement (`test_merge_past_delivery_does_not_leak_market_items_into_meals`). Market selection is tracked separately on `market_items`.
- A past week's `meals_selected` / `meals_required` prefer the **delivered values** over the *current* subscription plan — otherwise a 4-meal box delivered under today's 3-meal plan would be capped at 3.
- The stale `mealsPreselected` flag on a long-past week is cleared: once a week has delivery history, what shipped **is** your selection, so it must not badge as "Preselected" (`test_merge_past_delivery_clears_preselected_flag`).

The comprehensive history source is `/gw/my-deliveries/past-deliveries` (paginated ~4 weeks/page via a `nextWeek` cursor, ~16 weeks back), whose recipes carry images. The narrower `/gw/customer-complaints/...` endpoint knows only the last ~2 weeks and returns image-less recipes; because all history endpoints normalize to `source="past_deliveries"`, a `filled_by_path` map ensures the authoritative endpoint **wins** per week over the narrower one, never the reverse (`test_past_deliveries_overwrites_customer_complaints_recipes`).

**Paused / skipped weeks** (`status`/`state` = `PAUSED`, or `is_skipped`): a paused/skipped box never shipped, so any "selected" meals are pure auto-fill placeholders. A universal post-merge pass (`_clear_paused_week_selection`) sets `meals_selected = 0` and:

- For a **future/undated** paused week it clears `is_selected` and `selected_quantity` on every recipe but **keeps the catalog** visible for browsing (the customer can still un-pause). A paused/skipped week still **inside the menu grace window** gets the same treatment — its catalog is the real published menu, matching the grace handling of shipped weeks (`test_paused_week_in_grace_window_keeps_catalog_unselected`).
- For a paused/skipped week **older than the grace window** it **drops the recipes entirely** (`recipes = []`) — the week never shipped and can't be edited, so it must show an empty meal list, not the (potentially ~1000-recipe aggregate) menu catalog that would otherwise flood it. "Past" is gated on UTC (`test_paused_week_has_no_selected_meals`).

Market items are left untouched in both cases — pausing meals is independent of add-ons.

**Menu catalog keying.** `/gw/menus-service/menus` items carry both an internal Mongo `id` and the ISO `week` (and return ~16 product/preset variants per week). Menu weeks are keyed by the **ISO `week`** (not the `id`, which never matched account weeks and mis-attached a week's catalog), and when several variants share a week the **richest (most recipes)** one wins so the week gets its full browsable catalog (`test_menu_week_id_prefers_iso_week_over_object_id`, `test_merge_keeps_richest_menu_variant_per_week`).

Delivered-history payloads use a smaller subset of the week model:

- `week_id` typically comes from `week` or `id`
- `delivery_date` comes from `delivery_date`, `deliveryDate`, or `date`
- `subscription_id` comes from `subscription_id`, `subscriptionId`, or the primary subscription fallback
- `recipes` comes from `recipes`, `items`, `meals`, or `selectedMeals`
- `status` defaults to `delivered` when no explicit field is present

This keeps historical recipe information available without mixing delivered weeks into the active-selection workflow.

When the deliveries endpoint does not return a usable week for the next modifiable box, the integration now backfills a synthetic `HelloFreshWeek` from the subscription payload. This is primarily used to keep Home Assistant entities such as:

- next selection deadline
- selected meal count / number of meals

populated even when the dedicated delivery payload is sparse.

Derived behavior:

- `needs_selection` is `true` only when the week is active and `meals_selected < meals_required`.
- `source` is one of `account`, `account_menu_api`, `past_deliveries`, or `public_menu`.

Backfill notes:

- the synthetic week uses `nextModifiableDeliveryWeek` or `nextDeliveryWeek` as `week_id`
- `selection_deadline` comes from `nextCutoffDate`
- `delivery_date` comes from `nextModifiableDeliveryDate` or `nextDelivery`
- `slot_label` comes from `nextDeliveryOption.deliveryName`
- `meals_required` comes from `productType.specs.meals` or subscription defaults
- after the authenticated menu payload is merged, its real `meals_selected` value wins over the synthetic placeholder count

### Recipe

`HelloFreshRecipe` fields are derived as follows:

| Field | Source keys |
| --- | --- |
| `recipe_id` | `id`, `slug`, slugified name |
| `name` | `name`, `title`, `slug` |
| `preference` | `preference`, `category`; falls back to `Veggie` when the recipe is untyped but tagged `Veggie`/`Vegan` (a meatless dish carries no protein category). Drives the card's protein color dot and protein filter. |
| `is_selected` | `selection.selected` (bool), `selection.quantity > 0`, or `selected` field. In the authenticated menu (`/gw/my-deliveries/menu`), chosen meals carry `selection.quantity > 0` and unchosen ones a bare `selection.limit` — this is how the integration learns the current selection (see [Selection-state resolution](#selection-state-resolution)). For directly-listed delivery/account recipes the default is `true` (their presence is the selection). |
| `course_index` | `index` — the meal's course index within the week's menu. This, not `recipe_id`, is the cart's selection unit (`recipeIndexes` / `quantityPerCourse`); the same dish can recur under several ids/indexes (portion variants), so the index is the robust key for `select_meals` writes and for a dashboard card to round-trip selections. |
| `image_url` | `imagePath`, `image`, `imageUrl` |
| `description` | `description`, `headline` |
| `ingredients` | `ingredients`, `ingredientLines`, `ingredientNames` |
| `allergens` | `allergens` |
| `tags` | `tags`, `labels` |
| `cook_time_minutes` | `cookTime`, `cookTimeMinutes` |
| `prep_time_minutes` | `prepTime`, `prepTimeMinutes` |
| `total_time_minutes` | `totalTime`, `totalTimeMinutes`, or `cook + prep` |
| `calories_kcal` | `caloriesKcal`, `calories`, `nutrition.calories`, `nutrition.kcal` |
| `protein_g` | `nutrition.protein`, `protein` |
| `difficulty` | `difficulty`, `skillLevel` |
| `selected_quantity` | `selection.quantity` (servings of this meal; `1` when selected without an explicit count) |
| `surcharge_label`, `surcharge_cents` | `charge.label` (e.g. `+7.99/serving`), `charge.unitAmount` (cents) |
| `badge` | `recipe.label.text` (e.g. `Premium Picks`) |
| `variation_title` | resolved from the week's `modularity` block by `course_index` (e.g. `2x Bacon`) — names how a same-named variant differs |
| `variation_group` | the `defaultCourseIndex` of the `modularity` group this recipe belongs to (base meal + every variation/add-on member share it); `null` for a meal that has no variants. A recipe is the group's **base ("Default")** meal when its own `course_index == variation_group`. Lets the card cluster a dish's variants — including renamed protein swaps — and hide variants down to the default. |

Authenticated menu payloads may wrap these fields under `meal.recipe`, so the normalizer unwraps nested recipe objects before mapping fields. `surcharge_*`, `badge`, `protein_g`, `selected_quantity`, `variation_title`, and `variation_group` exist to differentiate same-named meal variants on a dashboard (the meal-planner card collapses truly-identical duplicate listings and calls out the rest). `preference` (one of `Beef`, `Poultry`, `Pork`, `Seafood`, `Lamb`, `Veggie`, falling back to `Veggie` for meals tagged `Veggie`/`Vegan`) and `variation_group` also drive the card's **protein** and **hide-variants** filters on current/upcoming weeks.

Nutrition handling:

- if `nutrition` is already an object, it is converted to a string map
- if `nutrition` is a list of `{name|label, value}` items, it is folded into a string map

### Order / shipment

`HelloFreshOrder` is synthesized from each delivery week:

| Field | Source keys |
| --- | --- |
| `order_id` | `orderId`, `shipmentId`, `deliveryId`, fallback `week_id` |
| `week_id` | normalized week id |
| `status` | normalized week status |
| `delivery_date` | normalized week delivery date |
| `total_price` | `price`, `totalPrice`, `amount`; later overlaid with an exact per-week cart/calculate estimate |
| `billed_total_price` | the **authoritative** sum of all billing charges for the delivery (per `(subscription, delivery_date)` from `/gw/api/customers/me/orders`), the same figure `next_box_total_price` reports. Kept separate from `total_price` so a later cart estimate can't clobber it. |
| `currency`, `billed_total_currency` | `currency`, `currencyCode` |
| `slot_label` | normalized week slot label |

Tracking is searched across several nested objects:

- the raw week object itself
- `tracking`
- `shipment`
- `delivery`
- `box`
- `carrierTracking`

Recognized tracking keys:

| Normalized field | Source keys |
| --- | --- |
| `tracking_url` | `trackingUrl`, `trackingURL`, `trackingLink`, `trackUrl`, `trackURL`, `url` |
| `tracking_number` | `trackingNumber`, `trackingCode`, `parcelNumber`, `waybill`, `consignmentNumber` |
| `tracking_status` | `trackingStatus`, `shipmentStatus`, `parcelStatus`, `carrierStatus` |
| `carrier` | `carrier`, `carrierName`, `deliveryPartner`, `provider`, `shippingProvider` |

### Market item

`HelloFreshMarketItem` is parsed from each `addOns.groups[].addOns[]` entry (see [`addOns`](#addons--hellofresh-market-catalog)):

| Field | Source keys |
| --- | --- |
| `item_id` | `recipe.id`, `sku`, `index` |
| `name` | `recipe.name`, `recipe.title` |
| `index` | `index` — the cart selection unit for this extra |
| `sku`, `group_type` | `sku`, the parent group's `groupType` |
| `image_url`, `description`, `category`, `tags`, `nutrition`, `calories_kcal` | from `recipe` |
| `price_cents`, `price` | `priceCatalog.basePrice` (cents) → major units |
| `max_quantity` | `maxQuantity` |
| `is_selected`, `selected_quantity` | true when `selection` is present; quantity = `selection.oneOffQuantity + selection.preselectedQuantity` |
| `preselected_quantity` | `selection.preselectedQuantity` (recurring portion, preserved on writes) |
| `is_locked`, `is_sold_out` | `isLocked`, `isSoldOut` |

Market items are attached to each week (`HelloFreshWeek.market_items`) by a normalization pass that reads `addOns` from the week's payload or its merged `_menu_payload`, mirroring how `variation_title` is resolved.

The US authenticated deliveries HAR also exposed snake_case tracking fields on delivered weeks:

| Normalized field | Additional source keys |
| --- | --- |
| `tracking_url` | `tracking_link`, `tracking_link_url`, `carrier_tracking_url`, `public_url`, `hf_tracking_url` |
| `tracking_number` | `tracking_code`, `tracking_id` |
| `tracking_status` | `tracking_status`, `internal_status`, `state` |

Carrier names are not inferred from `tracking_link_type`. In practice, the most reliable carrier value comes from explicit carrier fields in the delivery payload or the SCM tracking response.

Pricing notes:

- finalized order-like payloads may expose `grandTotal`, `shippingAmount`, or cent-based variants directly
- upcoming delivery payloads may omit a total entirely but still include `product.price` plus a fee field such as `product.specialFee`
- when no direct total exists, the integration derives the next box total from `product.price` plus the best available fee field so the upcoming box sensor can still show a realistic amount
- all `product.price` and `product.unitPrice` values are treated as integer cents and divided by 100 to produce the currency-unit total; the variable is named `product_price_cents` in the source to make this explicit

Tracking enrichment:

- delivered-week payloads may include a HelloFresh-hosted tracking page URL such as `tracking.tracking_link`
- when the URL path matches `/delivery-tracking/{public_id}`, the integration can call:
  - `GET /gw/scm/tracking-ids/track/public-id/{public_id}?country=US&locale=en-US`
  - request header seen in the HAR: `x-requested-by: shipping-and-tracking`
- the SCM response returns `boxes[]` entries with richer shipment details such as:
  - `tracking_code`
  - `carrier`
  - `carrier_tracking_url`
  - `last_status.status`
  - `internal_status`
  - `last_status.internal_status`

The integration now uses that SCM payload to improve shipment-related Home Assistant entities when the base delivery payload only exposes a HelloFresh tracking link or tracking code.

Normalization notes:

- tracking enrichment prefers `last_status.status` before `last_status.internal_status`
- `box.status` is preferred before `box.internal_status` as a fallback shipment label
- carrier codes are normalized into friendlier names when recognized: `DDASH` → `DoorDash`, `FEDEX` → `FedEx`, `UPS` → `UPS`, `USPS` → `USPS`, `ONTRAC` → `OnTrac`, `LASERSHIP` → `LaserShip`; unrecognized codes pass through unchanged (the map lives in [parsers.py](custom_components/hellofresh/parsers.py))
- `tracked_order` is **not** simply the most recent order. It is selected with a sort key that prefers orders carrying *concrete* tracking detail: orders with a tracking number or URL rank highest, then carrier, then a tracking status, and delivery date is used only as the final tiebreaker. This avoids surfacing a generic state-only record when a fully-tracked shipment exists.

## Home Assistant Exposure

The integration does not mirror every reverse-engineered endpoint as a separate entity. It first normalizes account state into `HelloFreshAccountData`, then exposes a small set of stable Home Assistant entities.

**Attribute size policy.** Home Assistant's recorder drops any state attribute payload over 16 KB. A single week's recipe catalog (from the authenticated menu API) can exceed that on its own, so sensor attributes never embed it: single-week context objects use `HelloFreshWeek.as_summary_dict()` (scalar metadata only — dates, deadline, counts, slot), and the per-week `weeks` list on `next_selection_deadline` / `weeks_needing_selection` uses `summarized_weeks_needing_selection`. The full recipe-bearing `as_dict()` is reserved for the diagnostics export and the live week objects that the write actions read. No consumer reads recipes out of a sensor attribute.

**On-demand recipe access (`hellofresh.get_weeks`).** Because recipes (and market items) are deliberately kept out of attributes, the integration exposes a read-only, response-returning service (`SupportsResponse.ONLY`) so a dashboard or automation can fetch per-week detail when needed. It returns `{"weeks": [...], "account": {...}}`, optionally filtered to one `week_id`, resolved against a single coordinator (requires `config_entry_id` when multiple accounts are configured). Each week dict adds, beyond `HelloFreshWeek.as_dict()`:

- `recipes[]` — full recipe list with `is_selected`, `selected_quantity`, `course_index`, surcharge, `variation_title`, etc.
- `market_items[]` — the week's HelloFresh Market add-on catalog with selection state and prices
- `order` — the week's matching order (tracking, status, carrier, `billed_total_price`), or `null`

The top-level `account` object carries fallbacks like `selected_plan_total_price` (used by the meal card's order strip when a week isn't billed yet) and `next_payment_date` (the primary subscription's next charge date, shown in the schedule card's next-box summary), plus two configured-options echoes the cards align themselves with: `menu_grace_weeks` (the meal-planner's past-week gating) and `refresh_interval_minutes` (the schedule card auto-refetches on the integration's own poll cadence — fetching more often would only re-read the coordinator's identical cache). Passing `include_debug: true` adds diagnostic sections (`variation_debug` per week, including market-selection field probing). This is the recorder-safe data path behind the packaged Lovelace cards that render week data — `custom:hellofresh-meal-planner-card`, `custom:hellofresh-market-card`, and `custom:hellofresh-schedule-card` — which call the service via `hass.callService(..., return_response=true)` (the Food Profile card uses `get_food_profile`/`set_food_profile` instead, and the Subscription card uses `get_account_summary` — a read-only response service returning the account/subscription headline values through the same `sensor_native_value` dispatcher that backs the sensors, so card and entities can never disagree; it normalizes the sensors' literal `"None"` placeholders back to null and echoes `refresh_interval_minutes` for the card's auto-refresh). The cards are bundled in the integration's `www/` directory and auto-registered as Lovelace resources (see [Frontend assets](#frontend-assets)).

Sensors backed by authenticated profile and history endpoints:

| Sensor key | Backing data | Notes |
| --- | --- | --- |
| `boxes_received` | `HelloFreshAccountData.boxes_received` | Long-lived account metric from authenticated profile endpoints |
| `last_delivery_date` | `HelloFreshAccountData.last_delivery_week.delivery_date` | Most recent delivered week date from normalized history |

Sensors backed by subscription data (primary subscription):

| Sensor key | Backing data | Notes |
| --- | --- | --- |
| `selected_plan` | `HelloFreshSubscription.plan_name` or `display_name` | Plan/product name |
| `selected_plan_total_price` | `HelloFreshAccountData.selected_plan_total_price` (+ `_currency`) | Standing weekly plan price incl. shipping, from recurring `/gw/calculate` `grandTotal`; 2-dp, currency unit |
| `number_of_people` | `HelloFreshSubscription.servings` | Box serving size |
| `delivery_address` | `HelloFreshSubscription.delivery_address` | Single-line formatted shipping address; redacted in diagnostics |
| `recent_payment_date` | `HelloFreshSubscription.recent_payment_date` | Date of most recent charge |
| `next_payment_date` | `HelloFreshSubscription.next_payment_date` | Estimated date of next charge |

Recent delivered-history records are also included in sensor attributes through `serialized_past_delivery_weeks`, while upcoming-delivery, selection, and shipment entities continue to use the active account week/order models.

Delivery/selection sensors backed by the **primary subscription's** API fields:

| Sensor key | UI label | Backing data | Notes |
| --- | --- | --- | --- |
| `next_delivery_date` | Next delivery date | `subscription.next_delivery` (`nextDelivery`) | Date of the subscription's next delivery |
| `next_delivery_week` | Next delivery week | `iso_week_label(subscription.next_delivery_week, …next_delivery)` (`nextDeliveryWeek`) | **ISO week identifier** (e.g. `2026-W25`) of the next delivery, normalized/validated against `next_delivery` as a fallback — a week label, deliberately distinct from `next_delivery_date` |
| `next_selectable_delivery_date` | Next selectable delivery date | `subscription.next_modifiable_delivery_date` (`nextModifiableDeliveryDate`) | The next delivery the customer can still modify |
| `next_selectable_delivery_week` | Next selectable delivery week | `iso_week_label(subscription.next_modifiable_delivery_week, …next_modifiable_delivery_date)` (`nextModifiableDeliveryWeek`) | ISO week identifier of the next modifiable delivery |
| `next_selection_deadline` | Next delivery selection deadline | `next_delivery_week_obj.selection_deadline` (the **next delivery** week's `cutoffDate`), falling back to `subscription.next_cutoff_date` (`nextCutoffDate`) | Cutoff for the next delivery week (`nextDeliveryWeek`). The per-week `cutoffDate` (from `/gw/api/subscriptions/{id}/delivery_dates/{week}`) is authoritative; `nextCutoffDate` is a fallback for accounts that populate it but where the week isn't resolved |
| `next_selectable_delivery_selection_deadline` | Next selectable delivery selection deadline | `next_modifiable_week.selection_deadline` (the **modifiable** week's `cutoffDate`) | Cutoff for the next *modifiable* delivery week (`nextModifiableDeliveryWeek`, typically the week after) — the "Edit delivery by …" deadline the web UI shows for the soonest box the customer can still change |

Sensors backed by the next configurable week:

| Sensor key | Backing data | Notes |
| --- | --- | --- |
| `selected_meal_count` | `HelloFreshAccountData.next_configurable_week.meals_selected` | Meals chosen so far for the next upcoming week (shown as **Next delivery meal count**) |
| `next_selectable_delivery_meal_count` | `HelloFreshAccountData.next_modifiable_week.meals_selected` | Meals chosen so far for the next *modifiable* week (`nextModifiableDeliveryWeek`); `0` when no modifiable week is resolved |
| `required_meal_count` | `HelloFreshAccountData.next_configurable_week.meals_required` | Meals required for the next upcoming week, falls back to subscription default |

The `next_selection_deadline` sensor still carries per-week context in its attributes (the `next_selection_week` summary and the `weeks` list the example dashboard reads), even though its **state** comes from the next delivery week's `cutoffDate` (with `nextCutoffDate` as a fallback). The separate `next_selectable_delivery_selection_deadline` sensor tracks the later *modifiable* week's `cutoffDate`.

Current UI-facing labels that differ from the raw entity ids:

- `sensor.required_meal_count` is shown as `Number of meals`
- `sensor.public_menu_recipe_count` is shown as `Available menu recipe count`
- `sensor.next_delivery_subscription` is shown as `Account delivery subscription ID`

Entity behavior notes:

- `sensor.selected_meal_count` reads only weeks that still need selection (`needs_selection = True`) and returns 0 when no such week exists; it does not include market or add-on item quantities
- `sensor.required_meal_count` uses the next pending week's `meals_required` value and falls back to the subscription plan meal count when the delivery payload is sparse
- `sensor.next_payment_date` is the delivery date of the soonest upcoming order, not the order creation date; it falls back to `next_cutoff_date + 1s` if no upcoming order is found
- `sensor.selected_plan` is sourced from normalized subscription plan/display fields
- when `using_public_menu_fallback` is `True` the coordinator raises a Repairs issue, which is the primary user-facing signal for menu fallback state

### Frontend assets

The integration ships five custom Lovelace cards (`www/hellofresh-meal-planner-card.js`, `www/hellofresh-market-card.js`, `www/hellofresh-food-profile-card.js`, `www/hellofresh-schedule-card.js`, and `www/hellofresh-subscription-card.js`) and registers them at startup ([frontend.py](custom_components/hellofresh/frontend.py)):

- the `www/` directory is served via `hass.http.async_register_static_paths` at `/hellofresh/`, so each card is reachable at `/hellofresh/<filename>` and any other asset (e.g. the bundled `hellofresh-logo.png`) under the same mount
- every card resource URL carries a `?v=` cache-bust stamped with the **integration release version** read from `manifest.json` (`INTEGRATION_VERSION`), so the release workflow's automatic manifest bump invalidates cached card JS on every release — there are no per-card version constants to maintain
- in storage-mode dashboards each card URL is added to the Lovelace resource list automatically, and a resource already registered under an older `?v=` is **updated in place** so upgrades reach existing installs; YAML-mode dashboards get a log line with the exact URLs to add manually
- each card's console startup banner reads its version from its own script URL (`import.meta.url` `?v=` param), so the banner reports exactly which build the browser loaded — the cards must remain ES modules for this to work
- registration is best-effort and never blocks integration setup — the sensors/calendar/services work without the cards
- because this uses `hass.http`, `http` is declared in `manifest.json`'s `dependencies`

The cards are pure read+write clients of existing services: they call `get_weeks` to render and `select_meals` / `select_market_items` / `skip_week` / `unskip_week` to act. They define no new entities or endpoints.

### Diagnostics redaction

The config-entry diagnostics export ([diagnostics.py](custom_components/hellofresh/diagnostics.py)) serializes the redacted config entry, non-sensitive **token timing/health** (expiry timestamps and boolean `has_refresh_token` / `has_credentials` flags — **never the tokens themselves**), a **`frontend` block** (the integration release version, the card resource URLs this build expects, and the URLs actually registered in Lovelace — a `?v=` mismatch means the user is loading a stale cached card), and the normalized account views (subscriptions, orders, weeks, public-menu weeks, capabilities, and the `debug_trace` of attempted endpoints).

Sensitive values are stripped by `async_redact_data(diagnostics, TO_REDACT)`, which redacts by **key name at any nesting depth**, so a redacted key is removed wherever it appears — including inside the `debug_trace` request params, which record full query strings. The redacted set covers:

- **Secrets:** `access_token`, `refresh_token`, `username`, `password`.
- **Account identifiers:** `account_id`, `subscription_id` (and its param-name form `subscription`), `customerPlanId`, `customerId` / `customer_id`.
- **Location / PII:** `delivery_address`, `postcode` / `postalCode`, `region`, `address1`, `address2`.
- **Tracking:** `tracking_number`, `tracking_url`.

The `debug_trace` params motivated several of these: PII like the user's postcode and the per-account `customerPlanId` ride along in the captured `/gw/my-deliveries/menu` query string even though they aren't fields on the serialized models, so they are redacted by key name (regression coverage in [tests/test_diagnostics.py](tests/test_diagnostics.py): `test_debug_trace_params_redact_pii_and_identifiers`, `test_tokens_and_credentials_still_redacted`). Box codes that are **not** PII — `product-sku`, `delivery-option` — are intentionally left unredacted so endpoint behavior stays diagnosable.

## Public Menu Scraping

The public menu fallback is intentionally shallow:

- fetches `<base_url>/menus`
- reads the first visible `<h1>` or `<title>` as the main menu label
- scans `h2`, `h3`, and `h4` headings for strings that look like recipe titles
- de-duplicates recipe names
- slugifies each public recipe name into a synthetic `recipe_id`

Visible menu labels are also extracted from page text with regex patterns such as:

- `Menu for <date-range>`
- `<Mon dd-dd>`
- `<Mon-Mon dd-dd>`

The fallback returns a single `HelloFreshWeek` with:

- `source = "public_menu"`
- no delivery metadata
- recipe names only

## Mutation Endpoints

Write operations are still conservative in the integration code, but HAR captures from the live US site do show one verified cart update endpoint family.

### Select meals

Candidate paths:

| Method(s) | Path |
| --- | --- |
| `POST`, fallback `PATCH` | `/gw/my-menu/weeks/{week_id}/selection` |
| `POST`, fallback `PATCH` | `/gw/my-menu/weeks/{week_id}/recipes` |
| `POST`, fallback `PATCH` | `/gw/my-menu/{week_id}/selection` |
| `POST`, fallback `PATCH` | `/gw/api/customers/me/subscriptions/{subscription_id}/weeks/{week_id}/selection` |

Payload variants:

```json
{"weekId":"<week_id>","recipes":["<recipe_id>"]}
{"week":"<week_id>","recipeIds":["<recipe_id>"]}
{"subscriptionId":"<subscription_id>","weekId":"<week_id>","selectedRecipeIds":["<recipe_id>"]}
```

**HAR-verified** live site request for changing weekly selections (a later capture confirmed the exact request body and query params the integration sends — `{"extras":[],"meals":[{"index":N,"quantity":1},...]}` matched byte-for-byte):

| Method | Path |
| --- | --- |
| `PUT` | `/gw/v1/carts/{week}` |

Observed query params include:

- `customer`
- `cutoff_time`
- `ignore_addons=false`
- `preference`
- `product-sku` — the box SKU, and **the meal digit resizes with the selection** (see [Box resizing](#box-resizing-via-product-sku) below)
- `subscription`
- `update_quantity=true`
- `week`

Observed request body shape (`meals[].quantity` is the per-meal serving count — `2` for a doubled portion; `extras` carries Market add-ons, see [Select market items](#select-market-items)):

```json
{
  "extras": [],
  "meals": [
    {"index": 32, "quantity": 1},
    {"index": 75, "quantity": 1},
    {"index": 12, "quantity": 1}
  ]
}
```

Observed success response:

```json
{"hasSeamlessDowngraded":false}
```

The integration now uses this `PUT /gw/v1/carts/{week}` request as the primary meal-selection write path when the authenticated menu payload includes stable meal `index` values.

#### Box resizing via `product-sku`

A week's box is **not fixed to the base plan's meal count** — choosing more or fewer distinct meals resizes the box for that delivery, and the `product-sku` query param must reflect the new size or the endpoint rejects the write:

```
HTTP 400 (MEAL_SIZE_MISMATCH: Product 'US-CBU-3-2-0' requires 3 meals but 4 meals were selected)
```

Box SKUs encode the plan as `<PREFIX>-<MEALS>-<SERVINGS>-<N>` (e.g. `US-CBU-3-2-0` = **3 meals × 2 servings**). The web app resizes by swapping the **meal digit** to the number of **distinct meals** selected, leaving the servings digit alone — HAR-confirmed in both directions:

| Selection on a 3-meal plan | `product-sku` sent | Observed box price |
| --- | --- | --- |
| 2 distinct meals | `US-CBU-2-2-0` | $49.96 |
| 3 distinct meals (base) | `US-CBU-3-2-0` | $65.94 |
| 4 distinct meals | `US-CBU-4-2-0` | $87.92 |

Key points:

- The digit tracks **distinct meals**, not total servings. A meal at `quantity: 2` fills extra servings of one box slot; it does **not** add a meal or change the meal digit.
- The integration adjusts the SKU in `HelloFreshClient._sku_for_meal_count()` from `len(meals)`. It only rewrites the known `<prefix>-<meals>-<servings>-<n>` shape, never touches zero-meal add-on/charge SKUs (e.g. `US-CHARGE-0-0-0`), and **clamps** below the minimum: a meal count under 2 keeps the base plan SKU (this guards market-only writes, which reuse this builder with the week's existing — possibly empty — meal list; see [Select market items](#select-market-items)).
- **Minimum box is 2 meals.** This is HelloFresh's smallest box and the only lower bound confirmed; the integration rejects fewer than 2 client-side.

Current implementation notes:

- meal indexes are preserved from the authenticated `/gw/my-deliveries/menu` payload
- the request uses the observed browser query params such as `customer`, `cutoff_time`, `preference`, `product-sku`, `subscription`, and `week`
- the request body is `{"meals": [{"index": <n>, "quantity": <q>}, ...], "extras": [...]}` — `quantity` defaults to 1 but honors a caller-supplied per-recipe count, and any currently-selected Market add-ons are carried in `extras` so a meal write doesn't clear them
- if the cart-style request cannot be built, older candidate selection endpoints are still available as conservative fallbacks

Validation rules before sending:

- `week_id` must exist in previously loaded account data
- at least one `recipe_id` is required
- duplicate recipe ids are removed
- the number of **distinct meals** must be **at least 2** (`MIN_MEALS_PER_WEEK`) — HelloFresh's smallest box; fewer is rejected. There is **no upper cap**: choosing more distinct meals than the base plan resizes the box up (the cart endpoint reprices and is the authority on acceptance). See [Box resizing via `product-sku`](#box-resizing-via-product-sku). Note this floor is on distinct meals, not total servings — a doubled portion adds servings, not a meal
- if the selected recipe set (with quantities) already matches the current state, no request is sent

### Select market items

**HAR-verified** (single- and multi-item writes captured). Market add-ons (extras) are written through the **same** `PUT /gw/v1/carts/{week}` cart endpoint and query params as meals, but populate the `extras` array. The integration preserves the week's existing `meals` selection in the same request.

The `extras` array is **grouped by `groupType` + `sku`**, each group carrying a `selection` list of `{index, oneOffQuantity, preselectedQuantity, courses}`. Two selected items in different groups produce two separate entries:

```json
{
  "extras": [
    {
      "groupType": "appetizer",
      "sku": "US-AAB-0-0-0",
      "selection": [
        {"index": 10773, "oneOffQuantity": 1, "preselectedQuantity": 0, "courses": []}
      ]
    },
    {
      "groupType": "protein",
      "sku": "US-APR-0-0-0",
      "selection": [
        {"index": 10089, "oneOffQuantity": 2, "preselectedQuantity": 0, "courses": []}
      ]
    }
  ],
  "meals": [
    {"index": 63, "quantity": 1},
    {"index": 44, "quantity": 1},
    {"index": 77, "quantity": 1}
  ]
}
```

Implementation notes:

- the caller passes a map of market item id/sku/index → desired **total** quantity; the integration resolves each against the week's market catalog, keeps any existing recurring (`preselectedQuantity`) portion, and applies the remainder as `oneOffQuantity`
- quantity is clamped to the item's `maxQuantity`; quantity `0` (or omitting an item) removes it
- because this reuses the shared cart builder, the `product-sku` still reflects the week's **existing** meal selection. That meal list can be under the minimum (0 meals on an unconfirmed/preselected week, or 1), so the SKU-resize step **clamps to the base plan SKU** below 2 meals rather than emitting an invalid box like `US-CBU-0-2-0` — see [Box resizing via `product-sku`](#box-resizing-via-product-sku)
- success response mirrors the meal write (`{"hasSeamlessDowngraded": false}`)

### Skip / unskip week

**HAR-verified.** A later capture showed that HelloFresh models skip/unskip as setting a week's **delivery status**, not a dedicated `/skip` verb:

| Action | Method | Path | Body `status` |
| --- | --- | --- | --- |
| Skip | `PATCH` | `/gw/api/subscriptions/{subscription_id}/delivery_dates/{week_id}` | `PAUSED` |
| Unskip | `PATCH` | `/gw/api/subscriptions/{subscription_id}/delivery_dates/{week_id}` | `RUNNING` |

Query params: `country=<CC>&locale=<locale>`. Both return `201`. Verified request body:

```json
{
  "delivery": {
    "cutoffDate": "2026-07-15T23:59:59-0700",
    "deliveryDate": "2026-07-20T12:00:00-0700",
    "status": "PAUSED",
    "subscriptionId": "<subscription_id>",
    "id": "<week_id>"
  }
}
```

`cutoffDate` / `deliveryDate` are taken from the week's raw delivery payload (preserving the exact server timestamp format), falling back to the normalized `selection_deadline` / `delivery_date`. `_async_patch_delivery_status` builds this request; `is_skipped` is the live `status == "PAUSED"` state, so a no-op skip/unskip is short-circuited before any request.

#### Legacy fallback paths

If the verified PATCH can't be built (the week lacks both raw and normalized date fields) or is rejected, the client falls back to the older guessed endpoints below. These were never observed in a HAR and remain only as a safety net:

| Method(s) | Skip path | Unskip path |
| --- | --- | --- |
| `POST`, fallback `PATCH` | `/gw/my-deliveries/weeks/{week_id}/skip` | `…/unskip` |
| `POST`, fallback `PATCH` | `/gw/my-menu/weeks/{week_id}/skip` | `…/unskip` |
| `POST`, fallback `PATCH` | `/gw/api/customers/me/subscriptions/{subscription_id}/weeks/{week_id}/skip` | `…/unskip` |

Fallback payload variants (skip shown; unskip mirrors with `skip:false` / `status:"active"` / `action:"unskip"`):

```json
{"weekId":"<week_id>","skip":true}
{"week":"<week_id>","status":"skipped"}
{"subscriptionId":"<subscription_id>","weekId":"<week_id>","action":"skip"}
```

If all candidates fail, the client raises `HelloFreshNotImplementedError` with a short list of attempted endpoints.

### Reschedule a single week (one-off delivery change)

**HAR-verified.** Moves one week's delivery to a different delivery option without changing the recurring schedule. Maps to the `oneOffChange` capability.

| Method | Path | Params | Body |
| --- | --- | --- | --- |
| `POST` | `/gw/api/subscriptions/{subscription_id}/oneoff` | `country=<CC>&locale=<locale>` | `{"id":"<subscription_id>","delivery_option":"<handle>","week":"<week_id>","source":"reschedule-delivery-feature"}` |

`async_change_one_off_delivery(week_id, delivery_option)` gates on the week's `allowed_actions["oneOffChange"]` before sending. Exposed as the `hellofresh.reschedule_week` service.

### Change recurring delivery weekday/interval

**HAR-verified.** Changes the recurring delivery option and interval for a plan — affects **all** future deliveries. Maps to the `updateDeliveryWeekday` capability.

| Method | Path | Params | Body |
| --- | --- | --- | --- |
| `POST` | `/gw/api/plans/{customerPlanId}/changePlanDeliveryDetails` | `country=<CC>` | `{"deliveryOption":"<handle>","deliveryInterval":<weeks>}` |

`async_change_delivery_weekday(delivery_option, delivery_interval, subscription_id)` resolves `customerPlanId` from the subscription. Exposed as the `hellofresh.change_delivery_weekday` service.

### Update delivery address — not implemented

The web app updates the shipping address via `PATCH /gw/api/addresses/{addressId}` (HAR-verified request), **but** the full address object — 20+ fields including numeric `country`/`region` codes (e.g. `"country":"231","region":"17"`) — is only present in that PATCH's own response. There is **no GET** that returns the current address object, and the delivery/subscription payloads don't carry it, so the integration cannot safely fetch-modify-resend it. Because a wrong write here changes a real shipping destination, this action is intentionally **left unimplemented** pending a read endpoint that exposes the address object.

## Error Handling

The client uses three main exception types:

| Exception | Meaning |
| --- | --- |
| `HelloFreshError` | generic request, parsing, or payload problem |
| `HelloFreshAuthError` | rejected login/refresh, `401/403` response, or no way to obtain a token |
| `HelloFreshNotImplementedError` | write flow could not be safely mapped to a working endpoint |

HTTP behavior:

- `401` and `403` on an authenticated call trigger one refresh-and-retry (which may renew via `/gw/refresh` or fall back to a credential login); if the retry still fails — or there is neither a usable refresh token nor stored credentials — the cached subscriptions are cleared and `HelloFreshAuthError` is raised
- a `401`/`403` on `/gw/login` (bad credentials) or on `/gw/refresh` (dead/rotated refresh token) raises `HelloFreshAuthError`
- any other `4xx` or `5xx` raises `HelloFreshError`
- malformed JSON raises `HelloFreshError` (the decode path catches `aiohttp.ClientError` and `ValueError`/`JSONDecodeError`, not bare `Exception`)
- malformed JSON on a `/gw/login` or `/gw/refresh` response, or one missing an `access_token`, raises `HelloFreshAuthError` (not `HelloFreshError`), so the coordinator surfaces it as an auth failure rather than a soft account-data warning
- a non-auth `>= 400` on the `/gw/refresh` call is treated as transient (logged, raised as `HelloFreshError`), so the current access token keeps being used rather than forcing a spurious login

Write-action error scoping:

- The `button` and `todo` write handlers catch `HelloFreshError` (the integration's own error type), not bare `Exception`. A known write failure raises a Repairs issue and surfaces a clean `HomeAssistantError`; unexpected exceptions are allowed to propagate so genuine bugs are not masked as "write unavailable."
- When no write candidate succeeds, the client raises `HelloFreshNotImplementedError` listing the attempted endpoints.

## Account Aggregation Behavior

`async_get_account_data()` aggregates data across all subscriptions returned by `/gw/api/customers/me/subscriptions`.

It also computes capability flags on `HelloFreshCapabilities`:

- `supports_meal_selection`
- `supports_account_menu_api`
- `supports_update_delivery_address`
- `supports_update_delivery_weekday`
- `supports_pause`
- `supports_one_off_change`
- `supports_update_payment_method`
- `supports_donation`
- `using_public_menu_fallback`
- `payload_shape_changed`

`supports_write_actions` is a derived property: `True` when any individual write capability is set. (The previously vestigial `supports_skip_actions` and `supports_multi_subscription` flags — never derived from payloads — were removed.)

And it derives summary views such as:

- `next_order` — the soonest **non-skipped** order whose delivery date is **today or later** (the deliveries endpoint returns a wide past+future window, so the future filter keeps `next_order` off the oldest historical delivery; the skip filter steps over skipped weeks to the next box that actually ships)
- `upcoming_orders` — all **non-skipped** orders with a delivery date today or later, sorted ascending; backs `upcoming_delivery_count`. Skip state is read from the order's resolved week (`is_skipped`), so a skipped future week — which ships no box — is excluded from the count
- `tracked_order`
- `weeks_needing_selection`
- `next_selection_week` — the next week that still needs meal selection; used by selection-related sensors and the `next_selection_deadline` attribute context
- `next_configurable_week` — broader fallback: returns `next_selection_week` when one exists, otherwise the soonest non-skipped upcoming week with any selection-related context; used by `selected_meal_count` and `required_meal_count`
- `next_modifiable_week` — the next delivery week the customer can still modify, resolved from the subscription's `nextModifiableDeliveryWeek` handle via `get_week`; used by the `skip_next_modifiable_week` switch, `next_selectable_delivery_selection_deadline`, and `next_selectable_delivery_meal_count`
- `next_delivery_week_obj` — the week for the subscription's `nextDeliveryWeek` handle, resolved via `get_week`; used by `next_selection_deadline` to read that week's `cutoffDate`
- `primary_subscription` — first entry in the subscriptions list; source for plan, servings, address, payment-date, and the delivery/selectable-delivery sensors (`next_delivery_date`/`week`, `next_selectable_delivery_date`/`week`)
- `next_skipped_week`
- `delivery_count_this_week`
- `boxes_received`
- `past_delivery_weeks`
- `past_delivery_count`
- `last_delivery_week`

For diagnostics and entity attributes, the account aggregate also serializes:

- `serialized_orders`
- `serialized_weeks`
- `serialized_weeks_needing_selection`
- `serialized_public_menu_weeks`
- `serialized_past_delivery_weeks`
- `serialized_subscriptions`

## Practical Caveats

- The read surface is more trustworthy than the write surface.
- Menu and delivery payloads may differ by region or account type.
- **Past-week selection comes from `past-deliveries`, not the menu.** A past week's menu still carries auto-fill picks; only the `past-deliveries` `meals[]` reflect what actually shipped. Paused weeks shipped nothing and must show no selection. See [Selection-state resolution](#selection-state-resolution).
- Public menu scraping does not expose personal selections, dates, or shipment data.
- Because the API is reverse-engineered, adding new regions or supporting future payload drift will likely require updating the key fallback lists in [client.py](custom_components/hellofresh/client.py) / [normalizers.py](custom_components/hellofresh/normalizers.py) and the region map in [const.py](custom_components/hellofresh/const.py).

## Related Files

| File | Role |
| --- | --- |
| [client.py](custom_components/hellofresh/client.py) | HTTP requests, endpoint orchestration, write actions (composes a `TokenManager`) |
| [token_manager.py](custom_components/hellofresh/token_manager.py) | `TokenManager`: access/refresh token state, the `/gw` login/refresh calls, expiry math, bot-block handling |
| [models.py](custom_components/hellofresh/models.py) | Dataclasses (`HelloFreshSubscription/Week/Recipe/Order/Capabilities/AccountData`) and exceptions |
| [parsers.py](custom_components/hellofresh/parsers.py) | Pure parsing/coercion helpers (dates, numbers, tracking, recursive payload search) |
| [normalizers.py](custom_components/hellofresh/normalizers.py) | Payload-to-model normalization helpers |
| [coordinator.py](custom_components/hellofresh/coordinator.py) | Data update coordinator and the dedicated token-refresh timer |
| [config_flow.py](custom_components/hellofresh/config_flow.py) | Setup (email/password **or** pasted token), options, and reauthentication flows |
| [frontend.py](custom_components/hellofresh/frontend.py) | Serves the `www/` assets and auto-registers the four Lovelace cards, stamping each resource URL with the manifest release version (`?v=` cache-bust) and updating stale registrations |
| [diagnostics.py](custom_components/hellofresh/diagnostics.py) | Config-entry diagnostics export with `TO_REDACT` key-name redaction (secrets, account IDs, PII) |
| [tls_transport.py](custom_components/hellofresh/tls_transport.py) | `curl_cffi` Chrome-fingerprint transport for auth POSTs and data XHRs (with `verify=True`), `aiohttp` fallback |
| [www/hellofresh-meal-planner-card.js](custom_components/hellofresh/www/hellofresh-meal-planner-card.js) | Packaged Lovelace card: browse weeks, view/edit the selection via `get_weeks` + `select_meals`, skip/unskip weeks |
| [www/hellofresh-market-card.js](custom_components/hellofresh/www/hellofresh-market-card.js) | Packaged Lovelace card: browse and order Market add-ons via `get_weeks` + `select_market_items` |
| [www/hellofresh-food-profile-card.js](custom_components/hellofresh/www/hellofresh-food-profile-card.js) | Packaged Lovelace card: view/edit meal-preselection preferences via `get_food_profile` + `set_food_profile` |
| [www/hellofresh-schedule-card.js](custom_components/hellofresh/www/hellofresh-schedule-card.js) | Packaged Lovelace card: next-box summary, month calendar of delivery days, and past + upcoming timeline via `get_weeks` (per-week skip/unskip; calendar/row clicks broadcast the week-sync event) |
| [www/hellofresh-subscription-card.js](custom_components/hellofresh/www/hellofresh-subscription-card.js) | Packaged Lovelace card: condensed account/subscription overview with built-in holiday notice via `get_account_summary` (read-only) |
| [const.py](custom_components/hellofresh/const.py) | Regional base URLs, config keys (`username`/`password`), `GW_CLIENT_ID`, scan-interval bounds, history-window bounds (`DEFAULT/MIN/MAX_HISTORY_WEEKS`) |
| [api.py](custom_components/hellofresh/api.py) | Backwards-compatible re-export shim |
| [services.yaml](custom_components/hellofresh/services.yaml) | Service definitions |
| [tests/test_api.py](tests/test_api.py), [tests/test_parsers.py](tests/test_parsers.py) | Normalization and parser unit tests |
