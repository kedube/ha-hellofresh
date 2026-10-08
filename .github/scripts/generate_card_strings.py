#!/usr/bin/env python3
"""Write the HelloFresh card's English text module from ``strings.json``.

The card's words live in the integration's translation files under ``config_panel.card``
(``strings.json`` is the English source; ``translations/<code>.json`` hold the languages). Home
Assistant serves them to the card in the user's language. English also ships as a JavaScript
module, ``www/hellofresh-i18n-en.js``, so the card shows English without waiting on Home
Assistant.

That module is generated from ``strings.json`` by this script, and CI runs it with ``--check``,
which fails when the module is out of date instead of writing it.

    python .github/scripts/generate_card_strings.py          # rewrite the module
    python .github/scripts/generate_card_strings.py --check  # exit 1 if it is stale
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
COMPONENT = ROOT / "custom_components" / "hellofresh"
STRINGS = COMPONENT / "strings.json"
MODULE = COMPONENT / "www" / "hellofresh-i18n-en.js"

HEADER = """\
// The HelloFresh card's English text, generated from strings.json (config_panel.card) by
// .github/scripts/generate_card_strings.py. Don't edit it: change strings.json, then run the
// script. hellofresh-i18n.js explains how the card uses it.
"""


def flatten(node: dict, prefix: str = "") -> dict[str, str]:
    """Nested translation keys to the card's dotted keys ("menu.add")."""
    out: dict[str, str] = {}
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out |= flatten(value, path)
        else:
            out[path] = value
    return out


def card_strings(strings_path: Path = STRINGS) -> dict[str, str]:
    strings = json.loads(strings_path.read_text(encoding="utf-8"))
    return flatten(strings["config_panel"]["card"])


def render(catalog: dict[str, str]) -> str:
    lines = [
        f"  {json.dumps(key)}: {json.dumps(value, ensure_ascii=False)},"
        for key, value in catalog.items()
    ]
    return HEADER + "\nexport const EN = {\n" + "\n".join(lines) + "\n};\n"


def main(argv: list[str]) -> int:
    expected = render(card_strings())
    if "--check" in argv:
        current = MODULE.read_text(encoding="utf-8") if MODULE.exists() else ""
        if current != expected:
            print(
                f"{MODULE.relative_to(ROOT)} is out of date with strings.json; "
                "run python .github/scripts/generate_card_strings.py",
                file=sys.stderr,
            )
            return 1
        print(f"{MODULE.relative_to(ROOT)} matches strings.json")
        return 0
    MODULE.write_text(expected, encoding="utf-8")
    print(f"wrote {MODULE.relative_to(ROOT)} ({len(expected.splitlines()) - 6} strings)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
