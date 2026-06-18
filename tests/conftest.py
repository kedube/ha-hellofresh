"""Shared pytest fixtures for the HelloFresh test suite."""

from __future__ import annotations

import pytest

from custom_components.hellofresh import tls_transport


@pytest.fixture(autouse=True)
def _force_aiohttp_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the aiohttp transport path for every test by default.

    The auth/data calls go through ``tls_transport``, which prefers ``curl_cffi`` whenever it
    is importable. Most unit tests drive the request layer by injecting a fake aiohttp
    ``session``; if ``curl_cffi`` happens to be installed in the dev/CI environment, those
    fakes would be bypassed and the code would attempt a *real* network call. Pinning the
    availability flag off here makes the suite deterministic regardless of whether
    ``curl_cffi`` is present.

    Tests that specifically exercise the ``curl_cffi`` path (``test_tls_transport.py``) flip
    this flag back on themselves via their own ``monkeypatch`` after this fixture runs, so they
    are unaffected.
    """
    monkeypatch.setattr(tls_transport, "_HAS_CURL_CFFI", False)
