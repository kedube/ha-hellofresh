"""Tests for the release automation scripts under .github/scripts."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from awesomeversion import AwesomeVersion, AwesomeVersionStrategy

REPO_ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "bump_manifest_version",
    REPO_ROOT / ".github" / "scripts" / "bump_manifest_version.py",
)
bump_manifest_version = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bump_manifest_version)
_NOTES_SPEC = importlib.util.spec_from_file_location(
    "generate_release_notes",
    REPO_ROOT / ".github" / "scripts" / "generate_release_notes.py",
)
generate_release_notes = importlib.util.module_from_spec(_NOTES_SPEC)
_NOTES_SPEC.loader.exec_module(generate_release_notes)
_CHANGELOG_SPEC = importlib.util.spec_from_file_location(
    "update_changelog",
    REPO_ROOT / ".github" / "scripts" / "update_changelog.py",
)
update_changelog = importlib.util.module_from_spec(_CHANGELOG_SPEC)
_CHANGELOG_SPEC.loader.exec_module(update_changelog)


def test_bump_minor() -> None:
    assert bump_manifest_version._bump_minor("2.43") == "2.44"


def test_bump_minor_zero_pads() -> None:
    assert bump_manifest_version._bump_minor("0.08") == "0.09"


def test_bump_minor_rolls_over_to_major() -> None:
    assert bump_manifest_version._bump_minor("2.99") == "3.00"


def test_bump_minor_releases_a_beta_as_its_final_version() -> None:
    """A beta already carries the version it previews; releasing it just drops the suffix."""
    assert bump_manifest_version._bump_minor("3.8b1") == "3.08"
    assert bump_manifest_version._bump_minor("3.8rc2") == "3.08"
    assert bump_manifest_version._bump_minor("3.10b1") == "3.10"
    assert bump_manifest_version._bump_minor("4.0b2") == "4.00"
    assert bump_manifest_version._bump_minor("4.26.0b1") == "4.26"
    assert bump_manifest_version._bump_minor("3.8.0rc2") == "3.08"


def test_a_beta_ranks_between_the_releases_around_it() -> None:
    """HACS offers a beta over the release before it, and the release over the beta.

    Both comparisons use AwesomeVersion, which ranks `4.26b1` equal to `4.26`, so anyone on
    that beta would never be offered 4.26. The `.0` spelling ranks where it should, and a beta
    in the manifest has to as well.
    """
    assert AwesomeVersion("4.25") < AwesomeVersion("4.26.0b1") < AwesomeVersion("4.26")
    assert AwesomeVersion("4.08") < AwesomeVersion("4.9.0b1") < AwesomeVersion("4.09")
    version = json.loads(
        (REPO_ROOT / "custom_components" / "hellofresh" / "manifest.json").read_text()
    )["version"]
    if bump_manifest_version._PRERELEASE.fullmatch(version):
        release = bump_manifest_version._bump_minor(version)
        assert AwesomeVersion(version) < AwesomeVersion(release)


def test_manifest_version_is_one_home_assistant_loads() -> None:
    """HA blocks a custom integration whose version matches none of these formats.

    The trap is a hand-cut beta: `3.08b1` is rejected, so betas are numbered `3.8.0b1`.
    """
    manifest = REPO_ROOT / "custom_components" / "hellofresh" / "manifest.json"
    AwesomeVersion(
        json.loads(manifest.read_text())["version"],
        ensure_strategy=[  # the list homeassistant/loader.py checks custom integrations against
            AwesomeVersionStrategy.CALVER,
            AwesomeVersionStrategy.SEMVER,
            AwesomeVersionStrategy.SIMPLEVER,
            AwesomeVersionStrategy.BUILDVER,
            AwesomeVersionStrategy.PEP440,
        ],
    )


def test_bump_major() -> None:
    assert bump_manifest_version._bump_major("2.43") == "3.00"


def test_bump_major_from_zero() -> None:
    assert bump_manifest_version._bump_major("0.99") == "1.00"


def test_main_bumps_manifest_in_place(tmp_path: Path, capsys, monkeypatch) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"domain": "hellofresh", "version": "2.43"}))
    monkeypatch.setattr("sys.argv", ["bump_manifest_version.py", str(manifest)])
    assert bump_manifest_version.main() == 0
    assert capsys.readouterr().out.strip() == "2.44"
    assert json.loads(manifest.read_text())["version"] == "2.44"


def test_main_major_bump(tmp_path: Path, capsys, monkeypatch) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"domain": "hellofresh", "version": "2.43"}))
    monkeypatch.setattr("sys.argv", ["bump_manifest_version.py", str(manifest), "--bump", "major"])
    assert bump_manifest_version.main() == 0
    assert capsys.readouterr().out.strip() == "3.00"
    assert json.loads(manifest.read_text())["version"] == "3.00"


def test_release_notes_link_to_the_docs_as_the_release_shipped_them() -> None:
    """CHANGELOG links are repository-relative; on a release page they would resolve under
    /releases/tag/, so the notes pin them to the release's own tag."""
    text = (
        "See [Moving](docs/dashboard.md#moving-from-the-classic-cards), [options](./README.md#options), "
        "[the site](https://www.hellofresh.com) and [below](#highlights)."
    )
    assert generate_release_notes._absolute_links(text, "kedube/ha-hellofresh", "4.00") == (
        "See [Moving](https://github.com/kedube/ha-hellofresh/blob/4.00/docs/dashboard.md"
        "#moving-from-the-classic-cards), "
        "[options](https://github.com/kedube/ha-hellofresh/blob/4.00/README.md#options), "
        "[the site](https://www.hellofresh.com) and [below](#highlights)."
    )


_CHANGELOG_INTRO = "# Changelog\n\nNotable changes. Add entries under **Unreleased**.\n\n"


def test_changelog_rotates_unreleased_entries_and_leaves_a_fresh_heading() -> None:
    text = (
        _CHANGELOG_INTRO + "## Unreleased\n- **A fix.** Details.\n\n## 4.25 — 2026-11-03\n- Old.\n"
    )

    updated, outcome = update_changelog.rotate_changelog(text, "4.26", "2026-11-14", ["ignored"])

    assert outcome == "rotated"
    assert updated == (
        _CHANGELOG_INTRO
        + "## Unreleased\n\n## 4.26 — 2026-11-14\n- **A fix.** Details.\n\n"
        + "## 4.25 — 2026-11-03\n- Old.\n"
    )


def test_changelog_gets_a_section_from_the_commits_when_unreleased_is_empty() -> None:
    """A release with nothing under Unreleased used to be left out of the changelog."""
    for unreleased in ("", "## Unreleased\n\n"):
        text = _CHANGELOG_INTRO + unreleased + "## 4.25 — 2026-11-03\n- Old.\n"

        updated, outcome = update_changelog.rotate_changelog(
            text, "4.26", "2026-11-14", ["Fix the thing", "Update CI"]
        )

        assert outcome == "generated"
        assert updated == (
            _CHANGELOG_INTRO
            + "## Unreleased\n\n## 4.26 — 2026-11-14\n- Fix the thing\n- Update CI\n\n"
            + "## 4.25 — 2026-11-03\n- Old.\n"
        )


def test_changelog_says_so_when_a_release_changed_nothing() -> None:
    text = _CHANGELOG_INTRO + "## 4.25 — 2026-11-03\n- Old.\n"

    updated, _ = update_changelog.rotate_changelog(text, "4.26", "2026-11-14", [])

    assert "## 4.26 — 2026-11-14\n- No user-facing changes.\n" in updated


def test_changelog_only_rotates_the_real_unreleased_heading() -> None:
    """Prose that mentions the heading, and a deeper heading, are left alone."""
    text = (
        "# Changelog\n\nSee ## Unreleased below.\n\n### Unreleased ideas\n- keep me\n\n"
        "## Unreleased\n- Real entry.\n\n## 4.25 — 2026-11-03\n- Old.\n"
    )

    updated, _ = update_changelog.rotate_changelog(text, "4.26", "2026-11-14", [])

    assert "See ## Unreleased below." in updated
    assert "### Unreleased ideas\n- keep me" in updated
    assert "## 4.26 — 2026-11-14\n- Real entry.\n" in updated


def test_every_tagged_release_has_a_changelog_section() -> None:
    """The manifest's own version is always in the changelog (the release workflow adds it).

    A beta is cut by hand and previews the Unreleased section, so it needs that one instead.
    """
    version = json.loads(
        (REPO_ROOT / "custom_components" / "hellofresh" / "manifest.json").read_text()
    )["version"]
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    if bump_manifest_version._PRERELEASE.fullmatch(version):
        unreleased = update_changelog.UNRELEASED_RE.search(changelog)
        assert unreleased is not None and unreleased[1].strip()
        return
    assert f"## {version} — " in changelog
    assert generate_release_notes._changelog_section(version)
