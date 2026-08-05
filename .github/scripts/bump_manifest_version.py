"""Increment the Home Assistant manifest version."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


def _bump_minor(version: str) -> str:
    """Return the next major.minor version, ignoring any patch segment.

    The minor segment supports 00-99 and is zero-padded to two digits (e.g. ``0.89``).
    Bumping ``0.99`` rolls the minor over to ``00`` and increments the major, yielding
    ``1.00``.
    """
    parts = version.split(".")
    if len(parts) < 2:
        raise ValueError(
            f"Expected semantic version with at least major.minor parts, got: {version}"
        )

    major, minor = (int(part) for part in parts[:2])
    if minor >= 99:
        major += 1
        minor = 0
    else:
        minor += 1
    return f"{major}.{minor:02d}"


def _bump_major(version: str) -> str:
    """Return the next major version with the minor reset to ``00``.

    Used for deliberate breaking-change releases via the release workflow's
    manual dispatch (``bump: major``); the automatic release-on-green path
    always bumps minor.
    """
    parts = version.split(".")
    if not parts or not parts[0].isdigit():
        raise ValueError(
            f"Expected semantic version with at least major.minor parts, got: {version}"
        )
    return f"{int(parts[0]) + 1}.00"


def main() -> int:
    """Update the manifest file in place and print the new version."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "manifest",
        nargs="?",
        type=Path,
        default=Path("custom_components/hellofresh/manifest.json"),
    )
    parser.add_argument(
        "--bump",
        choices=("minor", "major"),
        default="minor",
        help="Bump type: minor (default, the automatic release path) or major",
    )
    args = parser.parse_args()
    manifest_path = args.manifest
    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    current_version = manifest["version"]
    bump = _bump_major if args.bump == "major" else _bump_minor
    next_version = bump(current_version)
    updated_text, replacements = re.subn(
        r'("version"\s*:\s*")([^"]+)(")',
        rf'\g<1>{next_version}\g<3>',
        manifest_text,
        count=1,
    )
    if replacements != 1:
        raise ValueError("Could not locate the manifest version field")

    manifest_path.write_text(updated_text, encoding="utf-8")
    print(next_version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
