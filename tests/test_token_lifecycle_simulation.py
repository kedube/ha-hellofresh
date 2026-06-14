"""End-to-end simulation proving the access/refresh token lifecycle survives 60 days.

This drives the REAL HelloFreshClient refresh logic (proactive half-life refresh, reactive
401 retry, rotation, reboot rehydration) against a fake /gw/refresh server whose behavior
can be configured to model every plausible policy:

  - rotate vs no-rotate (does /gw/refresh return a new refresh_token?)
  - sliding vs absolute refresh-token expiry (does rotation extend the 60-day life?)
  - whether refresh_expires_in is echoed on refresh

For each policy it simulates 60 days of wall-clock time advancing in small steps, issuing
API calls (which trigger the real refresh decision), periodically "rebooting" (rebuilding
the client from persisted data, as Home Assistant does), and injecting concurrent requests.
It asserts the access token is NEVER dead when the client believes it is usable, and that a
true end-of-life only ever surfaces as a clean auth error (reauth), never a silent hang.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

from custom_components.hellofresh.api import HelloFreshAuthError, HelloFreshClient

DAY = 86400
ACCESS_TTL = 1800  # 30 min
REFRESH_TTL = 60 * DAY


class _Clock:
    """Mutable simulated clock; patched into datetime via a module-level holder."""

    def __init__(self, start: float) -> None:
        self.now = start

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class _FakeAuth0:
    """A fake /gw/refresh + resource server with configurable policy."""

    clock: _Clock
    rotate: bool  # return a new refresh_token on refresh?
    sliding: bool  # does issuing a new refresh token reset its 60-day life?
    echo_refresh_expires_in: bool  # include refresh_expires_in in the response?

    # server-side truth:
    valid_access_tokens: set[str] = field(default_factory=set)
    valid_refresh_tokens: dict[str, float] = field(default_factory=dict)  # token -> expires_at
    access_expiry: dict[str, float] = field(default_factory=dict)  # token -> expires_at
    _counter: int = 0
    refresh_calls: int = 0

    def seed_login(self) -> dict:
        """Mint the initial access+refresh token pair, as a fresh login would."""
        at = self._mint_access()
        rt = f"R{self._counter}"
        self._counter += 1
        self.valid_refresh_tokens[rt] = self.clock.now + REFRESH_TTL
        return {"access_token": at, "refresh_token": rt}

    def _mint_access(self) -> str:
        at = f"A{self._counter}"
        self._counter += 1
        self.valid_access_tokens.add(at)
        self.access_expiry[at] = self.clock.now + ACCESS_TTL
        return at

    def access_token_accepted(self, token: str) -> bool:
        return token in self.valid_access_tokens and self.clock.now < self.access_expiry[token]

    async def oauth_token(self, refresh_token: str) -> tuple[int, dict]:
        """Model POST /gw/refresh with a refresh_token body."""
        self.refresh_calls += 1
        exp = self.valid_refresh_tokens.get(refresh_token)
        if exp is None or self.clock.now >= exp:
            return 403, {"error": "invalid_grant"}

        new_at = self._mint_access()
        body: dict = {"access_token": new_at, "expires_in": ACCESS_TTL}

        if self.rotate:
            # Invalidate the used refresh token, issue a new one.
            original_deadline = exp
            del self.valid_refresh_tokens[refresh_token]
            new_rt = f"R{self._counter}"
            self._counter += 1
            new_deadline = (self.clock.now + REFRESH_TTL) if self.sliding else original_deadline
            self.valid_refresh_tokens[new_rt] = new_deadline
            body["refresh_token"] = new_rt
            if self.echo_refresh_expires_in:
                body["refresh_expires_in"] = int(new_deadline - self.clock.now)
        # else: same refresh token stays valid with its original deadline.

        return 200, body


class _SimResponse:
    def __init__(self, status: int, payload: dict) -> None:
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload

    async def text(self):
        return str(self._payload)


class _SimSession:
    """aiohttp-like session wired to the fake Auth0."""

    def __init__(self, auth0: _FakeAuth0) -> None:
        self._auth0 = auth0

    async def post(self, url: str, params=None, json=None, headers=None):
        # The app-token primer (POST /gw/auth/token) carries no refresh token; ignore it.
        if url.endswith("/gw/auth/token"):
            return _SimResponse(200, {"access_token": "app-token"})
        status, body = await self._auth0.oauth_token(json["refresh_token"])
        return _SimResponse(status, body)

    async def request(self, method: str, url: str, params=None, json=None, headers=None):
        bearer = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
        if self._auth0.access_token_accepted(bearer):
            return _SimResponse(200, {"ok": True})
        return _SimResponse(401, {"error": "expired"})


def _build_client(auth0: _FakeAuth0, persisted: dict) -> HelloFreshClient:
    """Rebuild the client from persisted data, exactly as HA does on load/reboot."""
    return HelloFreshClient(
        session=_SimSession(auth0),  # type: ignore[arg-type]
        country="us",
        access_token=persisted.get("access_token"),
        refresh_token=persisted.get("refresh_token"),
        token_issued_at=persisted.get("issued_at"),
        token_expires_in=persisted.get("expires_in"),
        refresh_expires_in=persisted.get("refresh_expires_in"),
        refresh_token_issued_at=persisted.get("refresh_token_issued_at"),
        token_refresh_callback=lambda data: persisted.update(data),
    )


async def _simulate_policy_async(clock, auth0, persisted, start) -> dict:
    """Drive 60+ days of operation on a single event loop. Returns a summary dict."""
    client = _build_client(auth0, persisted)
    summary = {"successful_calls": 0, "reauth_at_day": None}

    step = 600  # advance 10 min per tick
    reboot_every = 12 * 3600  # reboot twice a day
    ticks = int((62 * DAY) / step)
    since_reboot = 0

    for _ in range(ticks):
        clock.advance(step)
        since_reboot += step

        # Periodically reboot: rebuild client from persisted data (like HA restart).
        if since_reboot >= reboot_every:
            since_reboot = 0
            client = _build_client(auth0, persisted)

        # Issue concurrent API calls (the real refresh logic runs inside).
        try:
            results = await asyncio.gather(
                client._async_api_get("/gw/a"),
                client._async_api_get("/gw/b"),
            )
            for r in results:
                assert r.status == 200
                summary["successful_calls"] += 1
        except HelloFreshAuthError:
            summary["reauth_at_day"] = (clock.now - start) / DAY
            break

    return summary


def _simulate_policy(monkeypatch, rotate: bool, sliding: bool, echo: bool) -> dict:
    """Set up the fake server/clock for one policy and run the full simulation."""
    start = 1_700_000_000.0
    clock = _Clock(start)

    # Patch datetime.now to read our simulated clock. The token expiry math lives in the
    # token_manager module (TokenManager); the client module still uses datetime elsewhere,
    # so patch both to keep the simulated clock consistent across the refresh flow.
    import custom_components.hellofresh.client as client_mod
    import custom_components.hellofresh.token_manager as token_mod

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock.now, tz=tz or UTC)

    monkeypatch.setattr(token_mod, "datetime", _FakeDateTime)
    monkeypatch.setattr(client_mod, "datetime", _FakeDateTime)

    auth0 = _FakeAuth0(clock=clock, rotate=rotate, sliding=sliding, echo_refresh_expires_in=echo)
    login = auth0.seed_login()
    persisted = {
        "access_token": login["access_token"],
        "refresh_token": login["refresh_token"],
        "issued_at": int(clock.now),
        "expires_in": ACCESS_TTL,
        "refresh_expires_in": REFRESH_TTL,
        "refresh_token_issued_at": int(clock.now),
    }

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_simulate_policy_async(clock, auth0, persisted, start))
    finally:
        loop.close()


def test_token_lifecycle_survives_60_days_across_all_auth0_policies(monkeypatch) -> None:
    """The token must auto-renew across the full 60 days for every plausible server policy."""
    # Policy matrix: (rotate, sliding, echo_refresh_expires_in)
    policies = [
        (False, False, False),  # no rotation: same refresh token, absolute 60d cap
        (True, True, True),  # rotation, sliding life, echoes refresh_expires_in
        (True, True, False),  # rotation, sliding life, omits refresh_expires_in (Auth0 default)
        (True, False, False),  # rotation, ABSOLUTE life (hard 60d cap despite new tokens)
    ]

    for rotate, sliding, echo in policies:
        summary = _simulate_policy(monkeypatch, rotate, sliding, echo)
        label = f"rotate={rotate} sliding={sliding} echo={echo}"

        # Hard requirement: a usable token is always obtained — no silent failures,
        # and many successful calls were made over the simulated window.
        assert summary["successful_calls"] > 1000, f"{label}: too few successful calls"

        if sliding:
            # Sliding rotation extends the refresh token's life on every rotation, so the
            # client renews INDEFINITELY — never a reauth within the simulated window.
            assert summary["reauth_at_day"] is None, (
                f"{label}: unexpected premature reauth at day {summary['reauth_at_day']}"
            )
        else:
            # Absolute 60-day expiry (whether or not the token rotates): the refresh token
            # genuinely dies at the true deadline, so a clean reauth at/after day 60 is the
            # CORRECT outcome — the one hard requirement is it must never come EARLY.
            assert summary["reauth_at_day"] is not None, f"{label}: expected eventual reauth"
            assert summary["reauth_at_day"] >= 60, (
                f"{label}: reauth came too early at day {summary['reauth_at_day']}"
            )
