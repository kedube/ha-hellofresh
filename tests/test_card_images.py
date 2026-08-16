"""Behavioural tests for the recipes card's image URL rewriting.

These run the real ``resizedImage`` from the card source under Node, rather than asserting on
the source text, because the bug they cover was semantic: the function handled two URL shapes
and HelloFresh's actual catalog host uses a third. The result was either a 404 (wrong path) or
a 1.7 MB hero JPEG per tile (no transform applied) -- neither visible to a source-level grep.

Skipped when Node is unavailable so the suite still runs on a bare Python environment.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

WWW = Path(__file__).resolve().parents[1] / "custom_components" / "hellofresh" / "www"
RECIPES_CARD = WWW / "hellofresh-recipes-card.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

S3 = "/hellofresh_s3/image/beef-with-cheddar-gouda-fondue-6aa5702d.jpg"


def _extract_resized_image() -> str:
    """Pull the real `resizedImage` definition out of the card source.

    The card is a browser module that registers custom elements on import, so it cannot be
    imported under Node directly; this lifts out the one pure function under test.
    """
    source = RECIPES_CARD.read_text(encoding="utf-8")
    match = re.search(r"^function resizedImage\(url, width\) \{.*?^\}", source, re.S | re.M)
    assert match, "resizedImage function not found in the recipes card"
    return match.group(0)


def _resize(cases: list[tuple[str, int | None]]) -> list[str]:
    """Run resizedImage over `cases` in Node and return its outputs."""
    script = f"""
    {_extract_resized_image()}
    const cases = {json.dumps(cases)};
    console.log(JSON.stringify(cases.map(([url, width]) => resizedImage(url, width))));
    """
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)


def test_cloudinary_url_without_a_transform_gets_one_inserted() -> None:
    """The expensive case: no transform segment means the full ~1.7 MB asset."""
    (out,) = _resize([(f"https://img.hellofresh.com{S3}", 320)])
    assert "/hellofresh_s3/image/" in out
    assert "w_320" in out
    assert out.startswith("https://img.hellofresh.com/")


def test_existing_width_is_replaced_not_appended() -> None:
    """A second w_ would be ambiguous; the existing one must be rewritten in place."""
    (out,) = _resize([(f"https://img.hellofresh.com/f_auto,fl_lossy,h_300,q_auto,w_450{S3}", 320)])
    assert out.count("w_") == 1
    assert "w_320" in out
    assert "w_450" not in out


def test_transform_without_a_width_gains_one() -> None:
    """`f_auto,q_auto` is a valid transform with no width; append rather than replace."""
    (out,) = _resize([(f"https://img.hellofresh.com/f_auto,q_auto{S3}", 320)])
    assert "w_320" in out
    assert "f_auto" in out and "q_auto" in out
    assert "/hellofresh_s3/image/" in out


def test_the_integration_default_base_is_rewritable() -> None:
    """End-to-end: what the integration actually builds must resize, not just render."""
    base = "https://img.hellofresh.com/f_auto,fl_lossy,q_auto,w_640"
    (out,) = _resize([(f"{base}{S3}", 320)])
    assert out == f"https://img.hellofresh.com/f_auto,fl_lossy,q_auto,w_320{S3}"


def test_cloudfront_crop_form_still_works() -> None:
    """The older `<w>,<h>/image/` shape must keep working."""
    (out,) = _resize([("https://example.cloudfront.net/0,0/image/foo.jpg", 320)])
    assert out == "https://example.cloudfront.net/320,0/image/foo.jpg"


def test_non_http_schemes_are_dropped() -> None:
    """Nothing but plain http(s) may reach an <img src>."""
    out = _resize([("javascript:alert(1)", 320), ("data:image/png;base64,AAAA", 320), ("", 320)])
    assert out == ["", "", ""]


def test_absent_width_returns_the_url_unchanged() -> None:
    """Callers that pass no width want the original asset, not a mangled URL."""
    url = f"https://img.hellofresh.com{S3}"
    (out,) = _resize([(url, None)])
    assert out == url
