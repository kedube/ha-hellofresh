"""Tests for the arrival time on the Schedule card's shipment meta line.

The card already showed carrier and tracking number for each order day. The one fact missing
was *when the box actually arrived* — the same value `sensor.tracked_shipment_date` reports,
from the week's carrier handover timestamp (`delivered_at`).

Two properties matter and are asserted against the real method bodies executed under Node:

* Only DELIVERED weeks carry `delivered_at`, so an upcoming week's line must be byte-identical
  to what it rendered before — the arrival must not displace carrier/tracking/order id.
* `delivered_at` is a full ISO datetime WITH offset, unlike the bare-date `delivery_date`.
  Parsing it with `new Date` is correct precisely because the offset is present; a bare date
  would need `_parseLocalDate` to avoid reading a day early west of UTC.
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
CARD = "hellofresh-schedule-card.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _method(name: str) -> str:
    source = (WWW / CARD).read_text(encoding="utf-8")
    match = re.search(rf"^  {re.escape(name)}\(([^)]*)\) \{{.*?^  \}}", source, re.S | re.M)
    assert match, f"{name} not found in {CARD}"
    return match.group(0).strip()


def _row_tracking(week: dict, tz: str = "America/New_York") -> str:
    """Render a week's meta line with the card's real _rowTracking."""
    script = f"""
    class Card {{
      _esc(v) {{
        return String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
          {{"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}}[c]
        ));
      }}
      _safeUrl(u) {{ return u && /^https?:\\/\\//.test(u) ? u : ""; }}
      {_method("_fmtArrival")}
      {_method("_rowTracking")}
    }}
    console.log(JSON.stringify(new Card()._rowTracking({json.dumps(week)})));
    """
    out = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
        env={"TZ": tz, "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    return json.loads(out.stdout)


DELIVERED = {
    "delivered_at": "2026-08-17T22:53:00+00:00",
    "order": {
        "carrier": "OnTrac",
        "tracking_number": "D10012345678",
        "tracking_url": "https://example.com/track/D10012345678",
        "order_id": "123456789",
    },
}


def test_delivered_week_shows_when_the_box_arrived() -> None:
    """The arrival joins carrier and tracking number on the same meta line."""
    html = _row_tracking(DELIVERED)
    assert "Delivered Aug 17, 6:53 PM" in html
    # The line the card already rendered is still intact alongside it.
    assert "OnTrac" in html
    assert "D10012345678" in html
    assert "Order 123456789" in html


def test_arrival_renders_in_the_viewers_timezone() -> None:
    """The same instant renders in each viewer's local time, from the offset on `delivered_at`."""
    assert "Aug 17, 6:53 PM" in _row_tracking(DELIVERED, tz="America/New_York")
    assert "Aug 17, 10:53 PM" in _row_tracking(DELIVERED, tz="UTC")


def test_late_evening_handover_keeps_its_local_day() -> None:
    """A box handed over at 10:53 PM ET is already the NEXT day in UTC. The card must show the
    viewer's local day — the 17th in ET — not the UTC date embedded in the string."""
    late = {"delivered_at": "2026-08-18T02:53:00+00:00"}
    assert "Aug 17, 10:53 PM" in _row_tracking(late, tz="America/New_York")
    assert "Aug 18, 2:53 AM" in _row_tracking(late, tz="UTC")


def test_upcoming_week_line_is_unchanged() -> None:
    """An upcoming box has no carrier timestamp; the line must look exactly as it did."""
    upcoming = {"order": {"carrier": "OnTrac", "tracking_number": "D1", "order_id": "42"}}
    assert _row_tracking(upcoming) == '<div class="rowtrack">OnTrac · D1 · Order 42</div>'
    assert "Delivered" not in _row_tracking(upcoming)


def test_week_with_no_order_data_renders_nothing() -> None:
    """No tracking and no arrival — the card emits no meta line at all, not an empty one."""
    assert _row_tracking({}) == ""


def test_delivered_with_no_shipment_details_still_shows_arrival() -> None:
    """A delivered week whose order data never carried carrier/tracking still reports when
    it arrived, rather than falling back to a blank line."""
    html = _row_tracking({"delivered_at": "2026-08-17T22:53:00+00:00"})
    assert "Delivered Aug 17, 6:53 PM" in html


def test_malformed_timestamp_is_dropped_not_rendered() -> None:
    """An unparseable value must not reach the DOM as "Invalid Date"."""
    html = _row_tracking({"delivered_at": "not-a-date", "order": {"carrier": "OnTrac"}})
    assert "Invalid Date" not in html
    assert html == '<div class="rowtrack">OnTrac</div>'
