"""Contract tests for the cross-card sync protocol.

Six cards talk to each other over two `window` CustomEvents plus a localStorage key, and every
piece of the wire format is hand-copied into each card:

* event names ``hellofresh-week-selected`` / ``hellofresh-data-changed``
* the storage key ``hellofresh:selected-week:<accountKey>``
* ``accountKey`` = ``config_entry_id`` or the literal ``"default"``

Its whole correctness condition is *exact agreement*, and there is no shared module enforcing
it — so a card can drift and the only symptom is silence: an event fires, every listener drops
it, nothing visibly errors. That is exactly what happened. The Recipes card broadcast
``detail: {}``, and since every listener filters on
``(detail.accountKey || "default") !== this._accountKey()``, the event read as account
"default" and was discarded by any card configured with a ``config_entry_id`` — so favoriting a
recipe never refreshed the meal planner's hearts on a multi-account setup.

These tests assert the protocol across all cards at once rather than per-card behaviour, since
the failure mode is disagreement between files.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

WWW = Path(__file__).resolve().parents[1] / "custom_components" / "hellofresh" / "www"

WEEK_SYNC_EVENT = "hellofresh-week-selected"
DATA_CHANGED_EVENT = "hellofresh-data-changed"
STORAGE_KEY_TEMPLATE = "hellofresh:selected-week:"

# Cards that dispatch at least one cross-card event.
BROADCASTERS = (
    "hellofresh-market-card.js",
    "hellofresh-meal-planner-card.js",
    "hellofresh-schedule-card.js",
    "hellofresh-subscription-card.js",
    "hellofresh-food-profile-card.js",
    "hellofresh-recipes-card.js",
)

# Cards that listen and therefore filter on accountKey.
LISTENERS = (
    "hellofresh-market-card.js",
    "hellofresh-meal-planner-card.js",
    "hellofresh-schedule-card.js",
    "hellofresh-subscription-card.js",
    "hellofresh-food-profile-card.js",
    "hellofresh-cost-card.js",
)


def _source(filename: str) -> str:
    return (WWW / filename).read_text(encoding="utf-8")


def _dispatch_blocks(source: str) -> list[str]:
    """Return the argument text of each cross-card CustomEvent construction."""
    blocks: list[str] = []
    for match in re.finditer(r"new CustomEvent\(", source):
        start = match.end()
        depth = 1
        i = start
        while i < len(source) and depth:
            if source[i] == "(":
                depth += 1
            elif source[i] == ")":
                depth -= 1
            i += 1
        text = source[start : i - 1]
        # Only the cross-card channel; skip HA's own editor events.
        if "WEEK_SYNC_EVENT" in text or "DATA_CHANGED_EVENT" in text or "hellofresh-" in text:
            blocks.append(text)
    return blocks


@pytest.mark.parametrize("filename", BROADCASTERS)
def test_every_broadcast_carries_an_account_key(filename: str) -> None:
    """The reported bug: an event with no accountKey is dropped by every listener.

    Listeners default a missing key to "default", so an empty detail is not "broadcast to all"
    — it is "broadcast to the single-account case only", and silently invisible elsewhere.
    """
    for block in _dispatch_blocks(_source(filename)):
        assert "accountKey" in block, (
            f"{filename}: dispatches a cross-card event with no accountKey in its detail, "
            "so every card configured with a config_entry_id will silently drop it"
        )


@pytest.mark.parametrize("filename", LISTENERS)
def test_listeners_agree_on_the_account_key_fallback(filename: str) -> None:
    """All listeners must spell the filter identically, or events cross accounts."""
    source = _source(filename)
    assert '(detail.accountKey || "default") !== this._accountKey()' in source, (
        f"{filename}: account filter is spelled differently from the other cards"
    )


@pytest.mark.parametrize("filename", BROADCASTERS + LISTENERS)
def test_account_key_is_derived_the_same_way_everywhere(filename: str) -> None:
    """`_accountKey` must be config_entry_id or "default" — no other spelling.

    A card may either delegate to the shared module (preferred — one definition) or, until it is
    migrated, spell out the same expression inline. Anything else is drift.
    """
    source = _source(filename)
    match = re.search(r"_accountKey\(\) \{\s*return ([^;]+);", source)
    assert match, f"{filename}: no _accountKey() helper"
    body = " ".join(match.group(1).split())
    accepted = {
        "accountKey(this._config)",  # delegates to hellofresh-shared.js
        '(this._config && this._config.config_entry_id) || "default"',  # not yet migrated
    }
    assert body in accepted, (
        f"{filename}: _accountKey() derives the key differently: {body!r}"
    )


def test_event_names_are_identical_across_every_card() -> None:
    """A typo in one card's event name is undetectable at runtime — nothing errors."""
    for filename in BROADCASTERS + LISTENERS:
        source = _source(filename)
        for literal in re.findall(r'"(hellofresh-[a-z-]+)"', source):
            # Card resource paths and DOM ids also start with "hellofresh-"; only the two
            # protocol channels are asserted.
            if literal.endswith("-selected") or literal.endswith("-changed"):
                assert literal in (WEEK_SYNC_EVENT, DATA_CHANGED_EVENT), (
                    f"{filename}: unknown cross-card event name {literal!r}"
                )


def test_storage_key_format_is_defined_once_in_the_shared_module() -> None:
    """The week-sync storage key must have exactly one definition.

    It was previously spelled out in several cards (and one bypassed the accessor with a
    hardcoded literal). It now lives only in hellofresh-shared.js; a card re-introducing its own
    copy is the drift this guards against.
    """
    shared = (WWW / "hellofresh-shared.js").read_text(encoding="utf-8")
    assert "`hellofresh:selected-week:${accountKey(config)}`" in shared, (
        "the shared module must own the week-sync storage key format"
    )
    for filename in BROADCASTERS + LISTENERS:
        for literal in re.findall(r"`(hellofresh:[^`]*)`", _source(filename)):
            raise AssertionError(
                f"{filename}: builds the week-sync storage key itself ({literal!r}); "
                "use syncStorageKey() from hellofresh-shared.js"
            )
