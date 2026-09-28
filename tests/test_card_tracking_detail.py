"""Schedule card: the carrier's finer status, its scan history, and proof of delivery.

The SCM tracking lookup (HAR 54, 2026-09-28) carries more than the coarse `in_transit` the card
used to show: each scan's finer step (`label_created`, `received_at_origin_facility`, ...), the
full scan history, and — for carriers that provide it — a proof-of-delivery photo and signer.
These run the card's real method bodies under Node against those shapes.
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

METHODS = (
    "_fmtArrival",
    "_titleCase",
    "_sentenceCase",
    "_rowStatus",
    "_statusWithDetail",
    "_rowTracking",
    "_rowProofOfDelivery",
    "_renderTrackingHistory",
)

# Today's in-transit box from HAR 54, as the integration serializes the order.
IN_TRANSIT = {
    "week_id": "2026-W40",
    "order": {
        "order_id": "123",
        "carrier": "Veho",
        "tracking_number": "HF01000043507204",
        "tracking_url": "https://track.shipveho.com/#/trackingId/HF01000043507204",
        "tracking_status": "in_transit",
        "tracking_status_detail": "received_at_origin_facility",
        "tracking_events": [
            {
                "time": "2026-09-28T07:56:00+00:00",
                "status": "in_transit",
                "detail": "received_at_origin_facility",
            },
            {
                "time": "2026-09-27T12:56:10+00:00",
                "status": "in_transit",
                "detail": "received_at_origin_facility",
            },
            {
                "time": "2026-09-25T15:29:28+00:00",
                "status": "pre_transit",
                "detail": "label_created",
            },
        ],
        "delivery_photo_urls": [],
        "delivery_signed_by": None,
    },
}


def _run(expr: str, history_week: str | None = None) -> object:
    source = (WWW / CARD).read_text(encoding="utf-8")
    bodies = []
    for name in METHODS:
        match = re.search(rf"^  {re.escape(name)}\(([^)]*)\) \{{.*?^  \}}", source, re.S | re.M)
        assert match, f"{name} not found in {CARD}"
        bodies.append(match.group(0).strip())
    script = f"""
    import {{ titleCase }} from {json.dumps((WWW / "hellofresh-shared.js").as_uri())};
    class Card {{
      constructor() {{ this._historyWeekId = {json.dumps(history_week)}; }}
      _esc(v) {{
        return String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
          {{"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}}[c]
        ));
      }}
      _safeUrl(u) {{ return u && /^https?:\\/\\//i.test(u) ? this._esc(u) : ""; }}
      {chr(10).join(bodies)}
    }}
    const card = new Card();
    console.log(JSON.stringify({expr}));
    """
    out = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
        env={"TZ": "America/New_York", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
    )
    return json.loads(out.stdout)


def test_row_status_adds_the_finer_step() -> None:
    """ "In transit" alone hid where the box is; the carrier's step now follows it."""
    status = _run(f"card._rowStatus({json.dumps(IN_TRANSIT)}, 'Locked')")
    assert status == "In Transit · Received at origin facility"


def test_row_status_drops_a_detail_that_only_repeats() -> None:
    """Out for delivery / delivered carry a detail identical to the status — say it once."""
    week = {
        "order": {
            "tracking_status": "out_for_delivery",
            "tracking_status_detail": "out_for_delivery",
        }
    }
    assert _run(f"card._rowStatus({json.dumps(week)}, 'Locked')") == "Out For Delivery"
    delivered = {"order": {"tracking_status": "delivered", "tracking_status_detail": "delivered"}}
    assert _run(f"card._rowStatus({json.dumps(delivered)}, 'Delivered')") == ""


def test_summary_status_carries_the_detail() -> None:
    assert (
        _run("card._statusWithDetail('in_transit', 'label_created')")
        == "In Transit · Label created"
    )
    assert _run("card._statusWithDetail('delivered', 'delivered')") == "Delivered"
    assert _run("card._statusWithDetail('RUNNING', null)") == "Running"


def test_history_toggle_and_panel() -> None:
    closed = _run(f"card._rowTracking({json.dumps(IN_TRANSIT)})")
    assert 'data-action="tracking-history"' in closed
    assert "History (3)" in closed and 'aria-expanded="false"' in closed

    opened = _run(f"card._rowTracking({json.dumps(IN_TRANSIT)})", history_week="2026-W40")
    assert "Hide history" in opened and 'aria-expanded="true"' in opened

    panel = _run(f"card._renderTrackingHistory({json.dumps(IN_TRANSIT)})")
    # Newest first, local time, sentence-case steps.
    assert panel.index("Sep 28, 3:56 AM") < panel.index("Sep 25, 11:29 AM")
    assert "Received at origin facility" in panel and "Label created" in panel
    assert panel.count("<li>") == 3


def test_no_history_means_no_toggle() -> None:
    week = {"week_id": "2026-W41", "order": {"order_id": "9", "tracking_events": []}}
    html = _run(f"card._rowTracking({json.dumps(week)})")
    assert "tracking-history" not in html
    assert _run(f"card._renderTrackingHistory({json.dumps(week)})") == ""


def test_history_text_is_escaped() -> None:
    week = {
        "order": {"tracking_events": [{"time": None, "detail": "<img src=x onerror=alert(1)>"}]}
    }
    panel = _run(f"card._renderTrackingHistory({json.dumps(week)})")
    assert "<img" not in panel and "&lt;img" in panel
    assert "—" in panel  # an unreadable time shows a dash, not "Invalid Date"


def test_proof_of_delivery_photo_and_signer() -> None:
    order = {
        "delivery_photo_urls": ["https://pod.example/p1.jpg?sig=a&b=c", "javascript:alert(1)"],
        "delivery_signed_by": "<b>Pat</b>",
    }
    html = _run(f"card._rowProofOfDelivery({json.dumps(order)})")
    assert html.count("<img") == 1  # the unsafe URL never reaches an <img>
    assert 'src="https://pod.example/p1.jpg?sig=a&amp;b=c"' in html
    assert 'alt="Delivery photo"' in html and 'loading="lazy"' in html
    assert "Signed by &lt;b&gt;Pat&lt;/b&gt;" in html
    assert "javascript" not in html


def test_no_proof_of_delivery_renders_nothing() -> None:
    """Veho leaves both empty, so the common case must add nothing to the row."""
    assert (
        _run("card._rowProofOfDelivery({delivery_photo_urls: [], delivery_signed_by: null})") == ""
    )
    assert _run("card._rowProofOfDelivery({})") == ""
