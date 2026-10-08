"""Contract tests for the cross-card sync protocol.

HelloFresh cards (several on one dashboard, or one beside the sidebar panel) follow each other
over two `window` CustomEvents plus a localStorage key:

* event names ``hellofresh-week-selected`` / ``hellofresh-data-changed``
* the storage key ``hellofresh:selected-week:<accountKey>``
* ``accountKey`` = ``config_entry_id`` or the literal ``"default"``

Its whole correctness condition is *exact agreement*, and the only symptom of drift is silence:
an event fires, every listener drops it, nothing visibly errors. That is exactly what happened
while the seven classic cards (since removed) each hand-copied the wire format: the Recipes card
broadcast ``detail: {}``, which read as account "default" and was discarded by every card
configured with a ``config_entry_id``. The wire format now lives only in ``hellofresh-shared.js``
(tests/test_card_shared_module.py); these pin that the HelloFresh card goes through it rather
than growing a copy of its own.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

WWW = Path(__file__).resolve().parents[1] / "custom_components" / "hellofresh" / "www"

WEEK_SYNC_EVENT = "hellofresh-week-selected"
DATA_CHANGED_EVENT = "hellofresh-data-changed"

# The card and the view/logic modules it imports.
CARD_MODULES = sorted(p.name for p in WWW.glob("hellofresh-card*.js"))


def _source(filename: str) -> str:
    return (WWW / filename).read_text(encoding="utf-8")


@pytest.mark.parametrize("filename", CARD_MODULES)
def test_the_card_never_builds_a_protocol_event_itself(filename: str) -> None:
    """Broadcasts go through the shared helpers, which always attach the account key."""
    source = _source(filename)
    for name in ("WEEK_SYNC_EVENT", "DATA_CHANGED_EVENT", WEEK_SYNC_EVENT, DATA_CHANGED_EVENT):
        assert not re.search(rf"new CustomEvent\(\s*[\"'`]?[\w.]*{re.escape(name)}", source), (
            f"{filename}: dispatches {name} itself; use broadcastWeek / broadcastDataChanged"
        )


def test_the_card_filters_both_events_through_the_shared_matcher() -> None:
    """Listeners must spell the account filter the way broadcasts do, or events cross accounts."""
    source = _source("hellofresh-card.js")
    for handler in ("_receiveSyncedWeek", "_receiveDataChanged"):
        body = re.search(rf"^  {handler}\(ev\) \{{.*?^  \}}", source, re.S | re.M)
        assert body, f"{handler} not found"
        assert "L.eventMatchesAccount(detail, this._config)" in body.group(0), handler


@pytest.mark.parametrize("filename", CARD_MODULES)
def test_event_names_are_the_protocols(filename: str) -> None:
    """A typo in an event name is undetectable at runtime — nothing errors."""
    for literal in re.findall(r'"(hellofresh-[a-z-]+)"', _source(filename)):
        # Resource paths and DOM ids also start with "hellofresh-"; only the two protocol
        # channels are asserted.
        if literal.endswith("-selected") or literal.endswith("-changed"):
            assert literal in (WEEK_SYNC_EVENT, DATA_CHANGED_EVENT), (
                f"{filename}: unknown cross-card event name {literal!r}"
            )


def test_storage_key_format_is_defined_once_in_the_shared_module() -> None:
    """The week-sync storage key must have exactly one definition.

    It was once spelled out in several classic cards (and one bypassed the accessor with a
    hardcoded literal). It lives only in hellofresh-shared.js; a card module re-introducing its
    own copy is the drift this guards against.
    """
    shared = (WWW / "hellofresh-shared.js").read_text(encoding="utf-8")
    assert "`hellofresh:selected-week:${accountKey(config)}`" in shared, (
        "the shared module must own the week-sync storage key format"
    )
    for filename in CARD_MODULES:
        for literal in re.findall(r"`(hellofresh:[^`]*)`", _source(filename)):
            raise AssertionError(
                f"{filename}: builds the week-sync storage key itself ({literal!r}); "
                "use syncStorageKey() from hellofresh-shared.js"
            )
