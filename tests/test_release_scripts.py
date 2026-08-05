"""Tests for the release automation scripts under .github/scripts."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "bump_manifest_version",
    REPO_ROOT / ".github" / "scripts" / "bump_manifest_version.py",
)
bump_manifest_version = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bump_manifest_version)


def test_bump_minor() -> None:
    assert bump_manifest_version._bump_minor("2.43") == "2.44"


def test_bump_minor_zero_pads() -> None:
    assert bump_manifest_version._bump_minor("0.08") == "0.09"


def test_bump_minor_rolls_over_to_major() -> None:
    assert bump_manifest_version._bump_minor("2.99") == "3.00"


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
    monkeypatch.setattr(
        "sys.argv", ["bump_manifest_version.py", str(manifest), "--bump", "major"]
    )
    assert bump_manifest_version.main() == 0
    assert capsys.readouterr().out.strip() == "3.00"
    assert json.loads(manifest.read_text())["version"] == "3.00"
