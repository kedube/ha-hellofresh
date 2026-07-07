"""Increment the Home Assistant manifest version."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys


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


def main() -> int:
    """Update the manifest file in place and print the new version."""
    manifest_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("custom_components/hellofresh/manifest.json")
    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    current_version = manifest["version"]
    next_version = _bump_minor(current_version)
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
