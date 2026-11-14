"""Behavioural tests for the card's image URL rewriting (``resizedImage``).

These run the real ``resizedImage`` from the shipped shared module under Node, rather than
asserting on the source text, because the bug they cover was semantic: the function handled two
URL shapes and HelloFresh's actual catalog host uses a third. The result was either a 404 (wrong
path) or a 1.7 MB hero JPEG per tile (no transform applied) -- neither visible to a source-level
grep.

There is one ``resizedImage``, in ``hellofresh-shared.js``. The recipe sheet used to carry a
copy of its own, and a test here held the two to the same output; the sheet now imports the
shared one, which is checked below instead.

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
SHARED = WWW / "hellofresh-shared.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

S3 = "/hellofresh_s3/image/beef-with-cheddar-gouda-fondue-6aa5702d.jpg"


def _resize(cases: list[tuple[str | None, int | None]]) -> list[str]:
    """Run the shared module's resizedImage over `cases` in Node and return its outputs."""
    script = f"""
    const {{ resizedImage }} = await import({json.dumps(SHARED.as_uri())});
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
    out = _resize(
        [("javascript:alert(1)", 320), ("data:image/png;base64,AAAA", 320), ("", 320), (None, 320)]
    )
    assert out == ["", "", "", ""]


def test_absent_width_returns_the_url_unchanged() -> None:
    """Callers that pass no width want the original asset, not a mangled URL."""
    url = f"https://img.hellofresh.com{S3}"
    (out,) = _resize([(url, None)])
    assert out == url


def test_image_width_actually_resizes_the_urls_the_api_emits() -> None:
    """The reported class of bug: `image_width` silently doing nothing.

    The stub two classic cards carried only rewrote `/q_auto/`. Real HelloFresh image URLs are
    `hellofresh_s3` forms, which it left untouched — so the card requested the full-size asset
    while the option appeared to be honoured. Asserting the output *differs* from the input is
    the check that a no-op stub cannot pass.
    """
    url = f"https://img.hellofresh.com/f_auto,fl_lossy,h_300,q_auto,w_450{S3}"
    (out,) = _resize([(url, 320)])
    assert out != url, "image_width had no effect on a real API image URL"
    assert "w_320" in out
    assert "w_450" not in out


def test_a_url_cannot_carry_markup_into_an_img() -> None:
    """The URL comes back as a URL: the characters that could end an attribute are encoded."""
    (out,) = _resize([('https://img.hellofresh.com/q_auto/a".jpg', 320)])
    assert '"' not in out and "%22" in out
    assert "/q_auto,w_320/" in out


def test_no_module_keeps_its_own_copy_of_the_resizer() -> None:
    """One definition, in the shared module. A second copy is how the no-op stub survived: the
    fix went into one copy and the other kept shipping."""
    definition = re.compile(r"^(?:export )?function resizedImage\(", re.M)
    owners = [
        path.name
        for path in sorted(WWW.glob("*.js"))
        if definition.search(path.read_text(encoding="utf-8"))
    ]
    assert owners == ["hellofresh-shared.js"]
    detail = (WWW / "hellofresh-recipe-detail.js").read_text(encoding="utf-8")
    assert "resizedImage" in detail and "hellofresh-shared.js" in detail
