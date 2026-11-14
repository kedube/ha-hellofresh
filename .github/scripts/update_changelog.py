"""Rotate the CHANGELOG.md "Unreleased" section into a released version section.

Used by .github/workflows/release.yml. Every release gets a section:

- When "## Unreleased" has entries, its heading becomes "## <version> — <date>".
- When it is empty or missing, the section is written from the commit subjects since the
  previous release tag, so a release is never left out of the changelog. (Four releases
  were, back when an empty Unreleased section meant "leave the file alone".)

Either way a fresh, empty "## Unreleased" heading is left at the top for the next change.
Prints "rotated" or "generated".
"""

from __future__ import annotations

import datetime
from pathlib import Path
import re
import subprocess
import sys

RELEASE_TAG_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")
# The heading on a line of its own. Anchored, so prose that mentions "## Unreleased" and a
# deeper "### Unreleased" heading are never mistaken for it.
UNRELEASED_RE = re.compile(r"^## Unreleased[ \t]*\n(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
FIRST_SECTION_RE = re.compile(r"^## ", re.MULTILINE)
NO_CHANGES = "- No user-facing changes."


def rotate_changelog(
    text: str, version: str, today: str, commit_subjects: list[str]
) -> tuple[str, str]:
    """Return the changelog with a section for ``version``, and what was done to make it.

    ``commit_subjects`` is used only when the Unreleased section has no entries.
    """
    heading = f"## {version} — {today}"
    match = UNRELEASED_RE.search(text)
    if match is not None and match.group(1).strip():
        body = match.group(1).strip("\n")
        outcome = "rotated"
    else:
        body = "\n".join(f"- {subject}" for subject in commit_subjects) or NO_CHANGES
        outcome = "generated"

    section = f"## Unreleased\n\n{heading}\n{body}\n\n"
    if match is not None:
        return text[: match.start()] + section + text[match.end() :], outcome

    first = FIRST_SECTION_RE.search(text)
    if first is None:
        return text.rstrip("\n") + "\n\n" + section.rstrip("\n") + "\n", outcome
    return text[: first.start()] + section + text[first.start() :], outcome


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def commit_subjects_since_last_release(version: str) -> list[str]:
    """Commit subjects since the latest release tag, without merges and release bookkeeping."""
    previous = next(
        (
            tag
            for tag in _git("tag", "--sort=-v:refname").splitlines()
            if RELEASE_TAG_RE.match(tag) and tag != version
        ),
        None,
    )
    log_range = f"{previous}..HEAD" if previous else "HEAD"
    return [
        subject
        for subject in _git("log", "--no-merges", "--format=%s", log_range).splitlines()
        if subject and not subject.startswith("chore(release):")
    ]


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit("usage: update_changelog.py <version> [changelog_path]")
    version = sys.argv[1]
    changelog_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("CHANGELOG.md")

    text = changelog_path.read_text(encoding="utf-8")
    match = UNRELEASED_RE.search(text)
    has_entries = match is not None and bool(match.group(1).strip())
    subjects = [] if has_entries else commit_subjects_since_last_release(version)
    updated, outcome = rotate_changelog(text, version, datetime.date.today().isoformat(), subjects)
    changelog_path.write_text(updated, encoding="utf-8")
    print(outcome)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
