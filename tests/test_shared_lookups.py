"""Lookups shared by concurrent callers: a caller's cancellation stays its own.

The poll's menu fetches and a cart write share one plan-preference lookup, and the callers of
a poll share each identical GET. Cancelling one caller (an automation stopping a meal change)
used to cancel the shared task under the others, failing a poll nobody had cancelled.
"""

from __future__ import annotations

import asyncio
import gc
from typing import Any

import pytest

from custom_components.hellofresh.api import (
    HelloFreshClient,
    HelloFreshError,
    HelloFreshSubscription,
)

PATH = "/gw/api/customers/me/deliveries"
PARAMS = {"rangeStart": "2026-W40", "rangeEnd": "2026-W50"}


def _client() -> HelloFreshClient:
    return HelloFreshClient(session=None, access_token="token")  # type: ignore[arg-type]


class _Fetch:
    """A stand-in request that answers once released, counting calls and cancellations."""

    def __init__(self, result: Any = "chefs-choice", error: Exception | None = None) -> None:
        self.release = asyncio.Event()
        self.result = result
        self.error = error
        self.calls = 0
        self.cancelled = 0

    async def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
        self.calls += 1
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if self.error is not None:
            raise self.error
        return self.result


async def _stop(*tasks: asyncio.Task[Any]) -> None:
    """Cancel ``tasks`` and let each one, and what it was waiting on, finish."""
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)


# ---- the plan-preference lookup --------------------------------------------------------------


async def test_a_stopped_write_does_not_fail_the_poll_sharing_its_preference_lookup() -> None:
    client = _client()
    fetch = _Fetch()
    client._async_fetch_subscription_plan_preference = fetch  # type: ignore[method-assign]
    subscription = HelloFreshSubscription(subscription_id="sub-1")

    poll = asyncio.create_task(client._async_get_subscription_plan_preference(subscription))
    write = asyncio.create_task(client._async_get_subscription_plan_preference(subscription))
    await asyncio.sleep(0)
    await _stop(write)
    fetch.release.set()

    assert await poll == "chefs-choice"
    assert write.cancelled()
    assert (fetch.calls, fetch.cancelled) == (1, 0)


async def test_a_preference_lookup_every_caller_left_is_cancelled() -> None:
    client = _client()
    fetch = _Fetch()
    client._async_fetch_subscription_plan_preference = fetch  # type: ignore[method-assign]
    subscription = HelloFreshSubscription(subscription_id="sub-1")

    callers = [
        asyncio.create_task(client._async_get_subscription_plan_preference(subscription))
        for _ in range(2)
    ]
    await asyncio.sleep(0)
    await _stop(*callers)

    assert fetch.cancelled == 1, "a lookup nobody waits for kept running"
    fetch.release.set()
    assert await client._async_get_subscription_plan_preference(subscription) == "chefs-choice"
    assert fetch.calls == 2


async def test_a_caller_arriving_while_an_abandoned_lookup_winds_down_starts_its_own() -> None:
    client = _client()
    fetch = _Fetch()
    client._async_fetch_subscription_plan_preference = fetch  # type: ignore[method-assign]
    subscription = HelloFreshSubscription(subscription_id="sub-1")

    caller = asyncio.create_task(client._async_get_subscription_plan_preference(subscription))
    await asyncio.sleep(0)
    caller.cancel()
    # One step: the caller has left and cancelled the lookup, which has not yet stopped.
    await asyncio.sleep(0)
    assert caller.cancelled()
    late = asyncio.create_task(client._async_get_subscription_plan_preference(subscription))
    await asyncio.sleep(0)
    fetch.release.set()

    assert await late == "chefs-choice"
    assert (fetch.calls, fetch.cancelled) == (2, 1)


async def test_a_failure_landing_after_every_caller_left_is_not_reported_as_lost() -> None:
    loop = asyncio.get_running_loop()
    reported: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    try:
        client = _client()
        fetch = _Fetch(error=HelloFreshError("preference lookup failed"))
        client._async_fetch_subscription_plan_preference = fetch  # type: ignore[method-assign]
        subscription = HelloFreshSubscription(subscription_id="sub-1")

        caller = asyncio.create_task(client._async_get_subscription_plan_preference(subscription))
        await asyncio.sleep(0)
        # The lookup fails in the same step its only caller is cancelled.
        fetch.release.set()
        await _stop(caller)
        for _ in range(3):
            await asyncio.sleep(0)
        del caller
        gc.collect()
    finally:
        loop.set_exception_handler(previous_handler)

    assert fetch.calls == 1
    assert [context["message"] for context in reported] == []


# ---- the poll's shared GET --------------------------------------------------------------------


async def test_a_cancelled_caller_does_not_fail_the_others_sharing_a_get() -> None:
    client = _client()
    fetch = _Fetch(result={"weeks": []})
    client._async_get_json = fetch  # type: ignore[method-assign]

    first = asyncio.create_task(client._async_api_get_json_shared(PATH, PARAMS))
    second = asyncio.create_task(client._async_api_get_json_shared(PATH, PARAMS))
    await asyncio.sleep(0)
    await _stop(first)
    fetch.release.set()

    assert await second == {"weeks": []}
    assert (fetch.calls, fetch.cancelled) == (1, 0)


async def test_a_get_every_caller_left_is_cancelled_and_asked_again() -> None:
    client = _client()
    fetch = _Fetch(result={"weeks": []})
    client._async_get_json = fetch  # type: ignore[method-assign]

    first = asyncio.create_task(client._async_api_get_json_shared(PATH, PARAMS))
    await asyncio.sleep(0)
    await _stop(first)
    assert fetch.cancelled == 1

    # Later in the same poll: a fresh fetch, not the cancelled one's CancelledError.
    fetch.release.set()
    assert await client._async_api_get_json_shared(PATH, PARAMS) == {"weeks": []}
    assert fetch.calls == 2


async def test_a_shared_get_failure_still_reaches_every_caller() -> None:
    client = _client()
    fetch = _Fetch(error=HelloFreshError("HTTP 503"))
    client._async_get_json = fetch  # type: ignore[method-assign]

    callers = [
        asyncio.create_task(client._async_api_get_json_shared(PATH, PARAMS)) for _ in range(2)
    ]
    await asyncio.sleep(0)
    fetch.release.set()
    results = await asyncio.gather(*callers, return_exceptions=True)

    assert [str(result) for result in results] == ["HTTP 503", "HTTP 503"]
    assert fetch.calls == 1
    # Within the poll the failure is reused rather than asked again.
    with pytest.raises(HelloFreshError):
        await client._async_api_get_json_shared(PATH, PARAMS)
    assert fetch.calls == 1
