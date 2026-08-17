"""Behavioural tests for the cards' `hellofresh-data-changed` mid-fetch race handling.

When one card writes (saves a selection, skips a week), it broadcasts `hellofresh-data-changed`
so sibling cards re-read. The subtle case is an event arriving while the sibling already has a
fetch in flight: that response was issued *before* the write, so it carries pre-write data.
Simply ignoring the event while `_loading` leaves stale data on screen until the next refresh
interval — up to 3 hours.

The schedule and cost cards fixed this by queueing one follow-up fetch (`_refetchQueued`) and
draining it when the in-flight fetch settles. The **subscription** card had the drain but nothing
ever set the flag, so the drain was dead code and the card still had the original bug. These
tests run each card's real `_receiveDataChanged` body against a stub so the queue is asserted
behaviourally, not by grepping for the flag name.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

WWW = Path(__file__).resolve().parents[1] / "custom_components" / "hellofresh" / "www"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

# Cards that re-fetch on a sibling's write and therefore must survive the mid-fetch race.
QUEUEING_CARDS = (
    "hellofresh-schedule-card.js",
    "hellofresh-cost-card.js",
    "hellofresh-subscription-card.js",
)


def _handler_body(filename: str) -> str:
    """Extract the real `_receiveDataChanged` body from a card source."""
    source = (WWW / filename).read_text(encoding="utf-8")
    match = re.search(r"^  _receiveDataChanged\(ev\) \{.*?^  \}", source, re.S | re.M)
    assert match, f"_receiveDataChanged not found in {filename}"
    return match.group(0)


def _run(filename: str, *, loading: bool, fetched: bool = True) -> dict:
    """Fire a matching data-changed event at the card and report what it did."""
    script = f"""
    class Card {{
      constructor(loading, fetched) {{
        this._loading = loading;
        this._fetched = fetched;
        this._refetchQueued = false;
        this._fetchCount = 0;
        this._instanceId = "self";
      }}
      _accountKey() {{ return "default"; }}
      _fetch() {{ this._fetchCount++; }}
      {_handler_body(filename)}
    }}
    const card = new Card({json.dumps(loading)}, {json.dumps(fetched)});
    // An event from a DIFFERENT card instance, for this same account.
    card._receiveDataChanged({{ detail: {{ accountKey: "default", source: "other" }} }});
    console.log(JSON.stringify({{
      fetched: card._fetchCount,
      queued: card._refetchQueued === true,
    }}));
    """
    result = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("filename", QUEUEING_CARDS)
def test_event_during_an_inflight_fetch_queues_a_followup(filename: str) -> None:
    """The reported bug: the event is dropped and stale pre-write data stays on screen.

    The in-flight response predates the write, so the card must queue one more fetch rather
    than treat the event as already handled.
    """
    outcome = _run(filename, loading=True)
    assert outcome["queued"] is True, (
        f"{filename}: a data-changed event during an in-flight fetch was dropped, "
        "so pre-write data stays on screen until the next refresh interval"
    )


@pytest.mark.parametrize("filename", QUEUEING_CARDS)
def test_event_while_idle_fetches_immediately(filename: str) -> None:
    """With nothing in flight there is nothing to wait for — fetch now, don't queue."""
    outcome = _run(filename, loading=False)
    assert outcome["fetched"] == 1
    assert outcome["queued"] is False


@pytest.mark.parametrize("filename", QUEUEING_CARDS)
def test_event_before_the_first_fetch_is_ignored(filename: str) -> None:
    """A card that has never loaded has nothing to refresh; `hass` drives the first fetch."""
    outcome = _run(filename, loading=False, fetched=False)
    assert outcome["fetched"] == 0


@pytest.mark.parametrize("filename", QUEUEING_CARDS)
def test_queue_flag_is_actually_drained(filename: str) -> None:
    """Structural backstop: setting the flag is useless if nothing consumes it.

    The subscription card had the inverse of this bug — a drain in `_fetch` with no setter,
    making it dead code. Assert both halves exist so neither half can go missing again.
    """
    source = (WWW / filename).read_text(encoding="utf-8")
    assert "this._refetchQueued = true" in source, f"{filename}: never queues"
    assert re.search(r"if \(this\._refetchQueued\)", source), f"{filename}: never drains"
