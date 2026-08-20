#!/usr/bin/env python3
"""Syntax-check every Lovelace card in ``custom_components/hellofresh/www``.

The cards are ~8k lines of hand-written JavaScript that nothing else in CI looks at, so a
stray typo used to ship and surface only as a blank panel in a user's dashboard. This is the
cheapest possible guard: it does not lint style or run the cards, it only proves each file
PARSES as an ES module.

``node --check`` resolves bare specifiers, so a card importing ``./hellofresh-shared.js`` would
fail on module resolution rather than syntax. Import lines are therefore blanked (not deleted --
blanking preserves line numbers so any reported error points at the real line) before checking a
throwaway copy. The originals are never modified.

Exits non-zero listing every file that failed, so one bad card does not mask another.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

CARD_DIR = pathlib.Path("custom_components/hellofresh/www")

# Blank a whole `import ...;` statement, including the multi-line form the cards use:
#   import {
#     parseLocalDate,
#   } from "./hellofresh-shared.js";
IMPORT_RE = re.compile(r"^\s*import\s[\s\S]*?;\s*$", re.MULTILINE)


def _blank_imports(source: str) -> str:
    """Blank import statements, preserving line count so error line numbers stay accurate."""
    return IMPORT_RE.sub(lambda m: "\n" * m.group(0).count("\n"), source)


def main() -> int:
    if shutil.which("node") is None:
        print("node not found on PATH; cannot syntax-check cards", file=sys.stderr)
        return 1

    cards = sorted(CARD_DIR.glob("*.js"))
    if not cards:
        print(f"No cards found under {CARD_DIR}", file=sys.stderr)
        return 1

    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        for card in cards:
            # .mjs so node parses it as an ES module (the cards use import/export).
            scratch = pathlib.Path(tmpdir) / f"{card.stem}.mjs"
            scratch.write_text(_blank_imports(card.read_text(encoding="utf-8")), encoding="utf-8")
            result = subprocess.run(
                ["node", "--check", str(scratch)],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                print(f"ok    {card.name}")
            else:
                failures.append(card.name)
                print(f"FAIL  {card.name}")
                # node reports against the scratch path; point at the real file instead. It
                # resolves symlinks (/tmp -> /private/tmp on macOS), so map the resolved path
                # too or the substitution silently misses and prints a confusing temp path.
                stderr = result.stderr.replace(str(scratch.resolve()), str(card)).replace(
                    str(scratch), str(card)
                )
                print(stderr, file=sys.stderr)

    if failures:
        print(f"\n{len(failures)} card(s) failed to parse: {', '.join(failures)}", file=sys.stderr)
        return 1

    print(f"\nAll {len(cards)} cards parsed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
