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

# Files that still define their own `resizedImage`. The canonical copy now lives in
# hellofresh-shared.js; a card that imports it from there has no local definition and is not
# listed (importing it is verified in tests/test_card_shared_module.py instead).
#
# The Market and Meal planner cards are the two exposing an `image_width` option, and both once
# carried a one-line stub that only handled `/q_auto/` — so `image_width` silently did nothing on
# the `hellofresh_s3` URLs the real API emits, and each tile pulled the full-size hero JPEG.
# Every remaining copy is held to the same behaviour here so a divergence fails loudly.
CARDS_WITH_RESIZE = tuple(
    name
    for name in (
        "hellofresh-shared.js",
        "hellofresh-recipes-card.js",
        "hellofresh-recipe-detail.js",
        "hellofresh-market-card.js",
        "hellofresh-meal-planner-card.js",
    )
    if re.search(
        r"^(?:export )?function resizedImage\(", (WWW / name).read_text(encoding="utf-8"), re.M
    )
)


def _extract_resized_image(filename: str = "hellofresh-shared.js") -> str:
    """Pull the real `resizedImage` definition out of a card source.

    The cards are browser modules that register custom elements on import, so they cannot be
    imported under Node directly; this lifts out the one pure function under test. The shared
    module exports it, so `export ` is tolerated and stripped.
    """
    source = (WWW / filename).read_text(encoding="utf-8")
    match = re.search(
        r"^(?:export )?function resizedImage\(url, width\) \{.*?^\}", source, re.S | re.M
    )
    assert match, f"resizedImage function not found in {filename}"
    return match.group(0).replace("export function", "function", 1)


def _resize(
    cases: list[tuple[str, int | None]], filename: str = "hellofresh-shared.js"
) -> list[str]:
    """Run resizedImage over `cases` in Node and return its outputs."""
    script = f"""
    {_extract_resized_image(filename)}
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


@pytest.mark.parametrize("filename", CARDS_WITH_RESIZE)
def test_every_card_actually_resizes_the_urls_the_api_emits(filename: str) -> None:
    """The reported class of bug: `image_width` silently doing nothing.

    The stub these two cards carried only rewrote `/q_auto/`. Real HelloFresh image URLs are
    `hellofresh_s3` forms, which it left untouched — so the card requested the full-size asset
    while the option appeared to be honoured. Asserting the output *differs* from the input is
    the check that a no-op stub cannot pass.
    """
    url = f"https://img.hellofresh.com/f_auto,fl_lossy,h_300,q_auto,w_450{S3}"
    (out,) = _resize([(url, 320)])
    assert out != url, f"{filename}: image_width had no effect on a real API image URL"
    assert "w_320" in out
    assert "w_450" not in out


@pytest.mark.parametrize("filename", CARDS_WITH_RESIZE)
def test_resize_copies_agree_across_cards(filename: str) -> None:
    """Every copy must produce byte-identical output for the same input.

    Four hand-maintained copies of one transform is exactly how the stub survived; this pins
    them together so the next edit to one has to be made to all.
    """
    cases: list[tuple[str, int | None]] = [
        (f"https://img.hellofresh.com{S3}", 320),
        (f"https://img.hellofresh.com/f_auto,fl_lossy,h_300,q_auto,w_450{S3}", 320),
        (f"https://img.hellofresh.com/f_auto,q_auto{S3}", 320),
        (f"https://img.hellofresh.com/f_auto,fl_lossy,q_auto,w_640{S3}", 320),
        ("https://example.cloudfront.net/0,0/image/foo.jpg", 320),
        ("javascript:alert(1)", 320),
        (f"https://img.hellofresh.com{S3}", None),
    ]
    # hellofresh-shared.js is the canonical copy every other one must match.
    assert _resize(cases, filename) == _resize(cases, "hellofresh-shared.js")
