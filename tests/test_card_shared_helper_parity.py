"""Parity tests for helpers that are hand-copied into several cards.

Every bug this file guards was found the same way: a helper exists in N cards, someone fixed or
extended one copy, and the others silently kept the old behaviour. Nothing errors — the cards just
disagree with each other, which is invisible unless you compare them directly.

Covered here:

* ``_isEditable`` — the Market card omitted the ``allowed_actions.mealSwap`` check and defaulted to
  *editable*, so a week HelloFresh had locked (or already delivered) with no recorded deadline was
  labelled "Editable", kept live ± steppers, and let the user build a cart the server would reject.
  ``HelloFreshWeek.is_editable`` in models.py documents that all copies stay in lockstep.
* ``_titleCase`` — the Schedule card was missing ``.toLowerCase()``, so HelloFresh's
  SCREAMING_SNAKE statuses rendered "DELIVERED" there and "Delivered" in the other two cards.

These compare the real function bodies executed under Node, so they assert behaviour rather than
matching source text.
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

EDITABLE_CARDS = (
    "hellofresh-market-card.js",
    "hellofresh-meal-planner-card.js",
    "hellofresh-schedule-card.js",
)
TITLECASE_CARDS = (
    "hellofresh-market-card.js",
    "hellofresh-meal-planner-card.js",
    "hellofresh-schedule-card.js",
)


SHARED = WWW / "hellofresh-shared.js"


def _method(filename: str, name: str) -> str:
    """Lift a method body out of a card source as a standalone function."""
    source = (WWW / filename).read_text(encoding="utf-8")
    match = re.search(rf"^  {re.escape(name)}\(([^)]*)\) \{{.*?^  \}}", source, re.S | re.M)
    assert match, f"{name} not found in {filename}"
    return match.group(0).strip()


def _shared_prelude() -> str:
    """Import the shared module so a card method that DELEGATES to it still runs.

    A migrated card's `_isEditable` is just `return isEditable(week)`, so the lifted method needs
    the shared bindings in scope. Cards not yet migrated ignore these.
    """
    return (
        "import { esc, parseLocalDate, relativeWeek, fmtDate, titleCase, fmtPrice, "
        "isEditable, isPast, accountKey, syncStorageKey, loadSyncedWeekId, resizedImage } "
        f"from {json.dumps(SHARED.as_uri())};"
    )


def _run_editable(filename: str, weeks: list[dict]) -> list[bool]:
    """Evaluate a card's real _isEditable over `weeks`."""
    # The schedule card delegates the skip check to _isSkipped; supply the same semantics the
    # other two express inline so the three are compared on equal terms.
    script = f"""
    {_shared_prelude()}
    class Card {{
      _isSkipped(week) {{ return Boolean(week && week.is_skipped); }}
      {_method(filename, "_isEditable")}
    }}
    const card = new Card();
    console.log(JSON.stringify({json.dumps(weeks)}.map((w) => card._isEditable(w))));
    """
    out = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(out.stdout)


def _run_titlecase(filename: str, values: list[object]) -> list[str]:
    script = f"""
    {_shared_prelude()}
    class Card {{ {_method(filename, "_titleCase")} }}
    const card = new Card();
    console.log(JSON.stringify({json.dumps(values)}.map((v) => card._titleCase(v))));
    """
    out = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(out.stdout)


# A locked week: HelloFresh refuses meal swaps, and no deadline was recorded for it.
LOCKED = {"allowed_actions": {"mealSwap": False}}
# A delivered week from history — carries no allowed_actions at all.
DELIVERED = {"status": "DELIVERED"}
OPEN = {"allowed_actions": {"mealSwap": True}, "selection_deadline": "2099-01-01T00:00:00+00:00"}
SKIPPED = {"allowed_actions": {"mealSwap": True}, "is_skipped": True}
EXPIRED = {"allowed_actions": {"mealSwap": True}, "selection_deadline": "2000-01-01T00:00:00+00:00"}


@pytest.mark.parametrize("filename", EDITABLE_CARDS)
def test_locked_week_is_not_editable(filename: str) -> None:
    """The reported bug: a locked week read as editable, enabling a write that must fail."""
    assert _run_editable(filename, [LOCKED]) == [False], (
        f"{filename}: a week with allowed_actions.mealSwap false is not editable"
    )


@pytest.mark.parametrize("filename", EDITABLE_CARDS)
def test_week_without_allowed_actions_is_not_editable(filename: str) -> None:
    """Absent allowed_actions must fail closed — a delivered week has none."""
    assert _run_editable(filename, [DELIVERED]) == [False], (
        f"{filename}: missing allowed_actions must read as locked, not editable"
    )


@pytest.mark.parametrize("filename", EDITABLE_CARDS)
def test_open_week_is_still_editable(filename: str) -> None:
    """Guards the over-correction: the normal editable case must keep working."""
    assert _run_editable(filename, [OPEN]) == [True]


@pytest.mark.parametrize("filename", EDITABLE_CARDS)
def test_skipped_and_expired_weeks_are_not_editable(filename: str) -> None:
    """Both pre-existing exclusions must survive."""
    assert _run_editable(filename, [SKIPPED, EXPIRED]) == [False, False]


def test_all_cards_agree_on_editability() -> None:
    """The invariant that matters: identical input, identical answer in every card.

    models.py's `HelloFreshWeek.is_editable` docstring states the copies are kept in lockstep,
    so a disagreement here is a contract violation, not a style difference.
    """
    weeks = [LOCKED, DELIVERED, OPEN, SKIPPED, EXPIRED, {}]
    baseline = _run_editable("hellofresh-meal-planner-card.js", weeks)
    for filename in EDITABLE_CARDS:
        assert _run_editable(filename, weeks) == baseline, (
            f"{filename} disagrees with the meal-planner card on week editability"
        )


def test_all_cards_agree_on_status_titlecasing() -> None:
    """HelloFresh sends SCREAMING_SNAKE; every card must render it the same way."""
    values = ["ON_THE_WAY", "DELIVERED", "out_for_delivery", "Shipped", None, ""]
    baseline = _run_titlecase("hellofresh-meal-planner-card.js", values)
    assert baseline[:2] == ["On The Way", "Delivered"], baseline
    for filename in TITLECASE_CARDS:
        assert _run_titlecase(filename, values) == baseline, (
            f"{filename} renders API statuses differently from the meal-planner card"
        )


@pytest.mark.parametrize("filename", TITLECASE_CARDS)
def test_titlecase_is_null_safe(filename: str) -> None:
    """A missing status must render empty, not the literal string "Null"."""
    assert _run_titlecase(filename, [None, ""]) == ["", ""]
