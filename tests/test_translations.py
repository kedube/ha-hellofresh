"""Structural tests for the shipped translation files.

Translations rot in a specific way: someone adds a sensor or reworks a config step in en.json,
and the other files keep the old key set. Nothing errors — Home Assistant just silently falls
back to English for the missing keys, and a stale key lingers forever because nothing reads it.

These tests compare every language against en.json as the source of truth. They deliberately do
NOT check wording (no test can), only the things a machine can verify:

* identical key structure — no missing keys, no keys en.json has since dropped;
* placeholder parity — `{entry_title}` / `{site_example}` must survive translation, or Home
  Assistant raises when it formats the string;
* preserved literals — `apiV2Auth` is a cookie name, not a word to translate;
* no accidental English — a value byte-identical to en.json means an untranslated string
  masquerading as a translation (proper nouns and short labels are exempt).
"""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

TRANSLATIONS = (
    Path(__file__).resolve().parents[1] / "custom_components" / "hellofresh" / "translations"
)
EN = json.loads((TRANSLATIONS / "en.json").read_text(encoding="utf-8"))
LANGS = sorted(p.stem for p in TRANSLATIONS.glob("*.json") if p.stem != "en")


def _flatten(node, path: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            out |= _flatten(value, f"{path}.{key}" if path else key)
    elif isinstance(node, str):
        out[path] = node
    return out


EN_FLAT = _flatten(EN)


def _load(lang: str) -> dict[str, str]:
    return _flatten(json.loads((TRANSLATIONS / f"{lang}.json").read_text(encoding="utf-8")))


def test_at_least_one_translation_ships() -> None:
    """Guard the guard: if the glob broke, every other test would vacuously pass."""
    assert LANGS


@pytest.mark.parametrize("lang", LANGS)
def test_key_structure_matches_english(lang: str) -> None:
    """Every language covers exactly the English key set — nothing missing, nothing stale."""
    keys = set(_load(lang))
    assert not set(EN_FLAT) - keys, f"{lang}.json is missing keys"
    assert not keys - set(EN_FLAT), f"{lang}.json has keys en.json does not"


@pytest.mark.parametrize("lang", LANGS)
def test_placeholders_survive_translation(lang: str) -> None:
    """`{entry_title}` and friends are formatted by HA — losing or inventing one raises."""
    for key, value in _load(lang).items():
        assert set(re.findall(r"\{(\w+)\}", value)) == set(
            re.findall(r"\{(\w+)\}", EN_FLAT[key])
        ), f"{lang}.json placeholder mismatch at {key}"


@pytest.mark.parametrize("lang", LANGS)
def test_code_literals_are_preserved(lang: str) -> None:
    """`apiV2Auth` is the literal cookie name the user must find in devtools."""
    for key, value in _load(lang).items():
        for literal in re.findall(r"`(\w+)`", EN_FLAT[key]):
            assert literal in value, f"{lang}.json lost literal `{literal}` at {key}"


@pytest.mark.parametrize("lang", LANGS)
def test_no_untranslated_prose(lang: str) -> None:
    """A multi-word value identical to English is an untranslated string, not a translation.

    Short labels are exempt: "Market", "Login" and product names legitimately match.
    """
    untranslated = [
        key
        for key, value in _load(lang).items()
        if value == EN_FLAT[key] and len(value.split()) > 2
    ]
    assert not untranslated, f"{lang}.json has untranslated English at: {untranslated[:5]}"
