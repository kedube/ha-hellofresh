"""Guards for metadata that must stay in step with the code.

None of these caught a live bug when written except the HACS country list, which had drifted to
6 entries while the integration supported 16 -- users in ten markets could not discover the
integration. The rest (translations, services, card registration) were all correct and are pinned
here so they stay that way: each is the kind of file that is edited by hand, in a separate step
from the code it describes, and therefore silently falls behind.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest
import yaml

from custom_components.hellofresh.const import COUNTRY_API_CODES, COUNTRY_BASE_URLS

REPO = pathlib.Path(__file__).resolve().parent.parent
COMPONENT = REPO / "custom_components" / "hellofresh"
TRANSLATIONS = COMPONENT / "translations"


def _load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _flatten(node: object, prefix: str = "") -> set[str]:
    """Return every dotted key path in a nested dict, so missing leaves are caught."""
    keys: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            keys.add(path)
            keys |= _flatten(value, path)
    return keys


# ---- HACS metadata ---------------------------------------------------------------------


def test_hacs_country_list_matches_supported_countries() -> None:
    """Every country the integration supports must be advertised to HACS.

    HACS filters the store by this list, so a country missing here is invisible to those users
    even though the integration works for them. It drifted to 6 of 16 once already.

    HACS expects ISO 3166-1 alpha-2, which is NOT always the config key: the UK is configured as
    ``uk`` but its ISO/API code is ``GB``, so the mapping in COUNTRY_API_CODES has to be applied
    rather than upper-casing the key.
    """
    expected = {(COUNTRY_API_CODES.get(key) or key.upper()) for key in COUNTRY_BASE_URLS}
    declared = set(_load(REPO / "hacs.json")["country"])

    assert declared == expected, (
        f"hacs.json country list is out of step.\n"
        f"  missing: {sorted(expected - declared)}\n"
        f"  unknown: {sorted(declared - expected)}"
    )


def test_hacs_country_codes_are_iso_alpha2() -> None:
    """Guards the uk/GB trap specifically -- a config key must never leak in as-is."""
    for code in _load(REPO / "hacs.json")["country"]:
        assert re.fullmatch(r"[A-Z]{2}", code), f"{code!r} is not an ISO alpha-2 code"
    assert "UK" not in _load(REPO / "hacs.json")["country"], "the UK's ISO code is GB, not UK"


# ---- translations ----------------------------------------------------------------------


def test_strings_and_en_translation_match() -> None:
    """``strings.json`` and ``translations/en.json`` must expose identical keys.

    Home Assistant reads strings.json for the config/options flow and en.json at runtime; a key
    in one but not the other surfaces as a raw translation key in the UI.
    """
    strings = _flatten(_load(COMPONENT / "strings.json"))
    english = _flatten(_load(TRANSLATIONS / "en.json"))
    assert strings == english, (
        f"strings.json / en.json drift.\n"
        f"  only in strings.json: {sorted(strings - english)[:10]}\n"
        f"  only in en.json     : {sorted(english - strings)[:10]}"
    )


@pytest.mark.parametrize(
    "locale_file",
    sorted(p.name for p in TRANSLATIONS.glob("*.json") if p.name != "en.json"),
)
def test_translation_locales_are_complete(locale_file: str) -> None:
    """Every shipped locale must cover all of en.json -- a missing key renders as a raw key."""
    english = _flatten(_load(TRANSLATIONS / "en.json"))
    locale = _flatten(_load(TRANSLATIONS / locale_file))
    missing = english - locale
    extra = locale - english
    assert not missing, f"{locale_file} is missing {len(missing)} keys, e.g. {sorted(missing)[:5]}"
    assert not extra, f"{locale_file} has {len(extra)} unknown keys, e.g. {sorted(extra)[:5]}"


# ---- services --------------------------------------------------------------------------


def test_services_yaml_matches_registered_services() -> None:
    """Every registered service needs a services.yaml entry, and vice versa.

    A service missing from services.yaml still works when called from YAML but is invisible in
    the UI service picker and has no field descriptions.
    """
    declared = set(yaml.safe_load((COMPONENT / "services.yaml").read_text(encoding="utf-8")))

    const_src = (COMPONENT / "const.py").read_text(encoding="utf-8")
    service_consts = dict(re.findall(r'^(SERVICE_[A-Z_0-9]+)\s*=\s*"([^"]+)"', const_src, re.M))

    init_src = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
    registered_consts = set(re.findall(r"async_register\w*\(\s*DOMAIN,\s*([A-Z_0-9]+)", init_src))
    registered = {service_consts.get(name, name) for name in registered_consts}

    assert registered, "no registered services found -- the detection regex needs updating"
    assert declared == registered, (
        f"services.yaml / registration drift.\n"
        f"  documented but not registered: {sorted(declared - registered)}\n"
        f"  registered but undocumented  : {sorted(registered - declared)}"
    )


# ---- frontend cards --------------------------------------------------------------------


def test_registered_cards_exist_on_disk() -> None:
    """A card named in frontend.py but absent from www/ would 404 as a blank dashboard panel."""
    frontend_src = (COMPONENT / "frontend.py").read_text(encoding="utf-8")
    referenced = set(re.findall(r'"(hellofresh-[a-z0-9-]+\.js)"', frontend_src))
    on_disk = {path.name for path in (COMPONENT / "www").glob("*.js")}

    assert referenced, "no card filenames found in frontend.py -- the regex needs updating"
    missing = referenced - on_disk
    assert not missing, f"frontend.py registers cards that do not exist: {sorted(missing)}"


def test_every_card_registers_in_the_card_picker() -> None:
    """Each Lovelace card must push itself onto window.customCards, or it silently never
    appears in the dashboard's card picker (the subscription card shipped that way)."""
    frontend_src = (COMPONENT / "frontend.py").read_text(encoding="utf-8")
    for name in re.findall(r'"(hellofresh-[a-z0-9-]+\.js)"', frontend_src):
        card_src = (COMPONENT / "www" / name).read_text(encoding="utf-8")
        card_type = name.removesuffix(".js")
        assert "window.customCards.push" in card_src, f"{name} never registers in the picker"
        assert f'type: "{card_type}"' in card_src, f"{name} registers under the wrong type"


def test_shared_modules_are_not_registered_as_cards() -> None:
    """The shared ES modules are imported BY cards, not registered as Lovelace resources.

    Registering them would add dead resources to every user's dashboard config. This pins the
    intended split so a future card rename does not accidentally promote one.
    """
    frontend_src = (COMPONENT / "frontend.py").read_text(encoding="utf-8")
    referenced = set(re.findall(r'"(hellofresh-[a-z0-9-]+\.js)"', frontend_src))
    for module in ("hellofresh-shared.js", "hellofresh-recipe-detail.js"):
        assert (COMPONENT / "www" / module).exists(), f"{module} is missing from www/"
        assert module not in referenced, f"{module} is a shared import, not a Lovelace resource"


# ---- documentation links ---------------------------------------------------------------


def _heading_anchors(text: str) -> set[str]:
    """GitHub's anchor slugs for every heading in a Markdown document."""
    anchors: set[str] = set()
    for heading in re.findall(r"^#{1,6}\s+(.+)$", text, re.M):
        slug = re.sub(r"[`*\[\]()]", "", heading).strip().lower()
        slug = re.sub(r"[^\w\s-]", "", slug).replace(" ", "-")
        anchors.add(slug)
    return anchors


DOCS = [
    "README.md",
    "CONTRIBUTING.md",
    "docs/HELLOFRESH_API.md",
    "docs/entities.md",
    "docs/dashboard.md",
    "docs/services.md",
]


def test_documentation_links_resolve() -> None:
    """Every relative Markdown link must point at a real file and a real heading.

    The README was split into docs/ reference files; that move silently broke a dozen anchors
    (README's own ``#market-card`` links, and docs/HELLOFRESH_API.md pointing at README anchors that
    had moved). Cross-document links are exactly the thing nobody re-checks by hand.
    """
    texts = {name: (REPO / name).read_text(encoding="utf-8") for name in DOCS}
    anchors = {name: _heading_anchors(text) for name, text in texts.items()}

    broken: list[str] = []
    for name, text in texts.items():
        base = (REPO / name).parent
        for match in re.finditer(r"\[[^\]]*\]\((?!https?:|mailto:)([^)]+)\)", text):
            target, _, fragment = match.group(1).partition("#")
            if not target:  # same-document anchor
                if fragment and fragment not in anchors[name]:
                    broken.append(f"{name}: #{fragment}")
                continue
            resolved = (base / target).resolve()
            if not resolved.exists():
                broken.append(f"{name}: missing file {target}")
                continue
            rel = str(resolved.relative_to(REPO))
            if fragment and rel in anchors and fragment not in anchors[rel]:
                broken.append(f"{name}: {target}#{fragment}")

    assert not broken, "broken documentation links:\n  " + "\n  ".join(broken)


def test_readme_stays_browsable() -> None:
    """The README is the landing page; detail belongs in docs/.

    It reached 581 lines before the card and service references were split out. This is a smoke
    alarm, not a style rule -- if it trips, move the newest reference material into docs/ rather
    than raising the number.
    """
    length = len((REPO / "README.md").read_text(encoding="utf-8").splitlines())
    assert length < 500, (
        f"README.md is {length} lines. Move reference detail into docs/ "
        f"(see docs/dashboard.md and docs/services.md for the pattern)."
    )
