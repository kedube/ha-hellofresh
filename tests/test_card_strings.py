"""The HelloFresh card's words: Home Assistant translations under ``config_panel.card``.

The card (``www/hellofresh-i18n.js``) asks Home Assistant for its text in the user's language
(``frontend/get_translations``, category ``config_panel``) and ships English as a module generated
from ``strings.json`` (``www/hellofresh-i18n-en.js``). What has to agree:

* Home Assistant really serves the text: its own loader returns every card string for a
  language, fills a language without them from English, and keeps a translation only while its
  ``{placeholders}`` match the English ones;
* the generated English module is what ``strings.json`` says;
* the card only names keys that exist;
* in another language the card really reads that language: labels, plurals, countdowns and the
  ``language`` entry that picks the plural rules.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from homeassistant.helpers.translation import async_get_translations
import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hellofresh"
WWW = COMPONENT / "www"
TRANSLATIONS = COMPONENT / "translations"
LOGIC = WWW / "hellofresh-card-logic.js"
I18N = WWW / "hellofresh-i18n.js"
NODE = shutil.which("node")
PREFIX = "component.hellofresh.config_panel.card."

sys.path.insert(0, str(ROOT / ".github" / "scripts"))
import generate_card_strings  # noqa: E402

needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

# The card modules whose text lives in config_panel.card.
CARD_MODULES = (
    "hellofresh-card.js",
    "hellofresh-card-logic.js",
    "hellofresh-card-ui.js",
    "hellofresh-card-deliveries.js",
    "hellofresh-card-menu.js",
    "hellofresh-card-account.js",
    "hellofresh-card-recipes.js",
    "hellofresh-card-profile.js",
    "hellofresh-recipe-detail.js",
    "hellofresh-shared.js",
)


def _card(path: Path) -> dict[str, str]:
    """A translation file's config_panel.card, flattened to the card's dotted keys."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return generate_card_strings.flatten(data["config_panel"]["card"])


EN = _card(COMPONENT / "strings.json")
LANGS = sorted(p.stem for p in TRANSLATIONS.glob("*.json") if p.stem != "en")


# ---- Home Assistant serves it -------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
async def test_home_assistant_serves_the_card_text_in_each_language(
    hass, enable_custom_integrations, caplog, lang: str
) -> None:
    """What frontend/get_translations hands the card: every key, in the language, none dropped.

    Home Assistant drops a translation whose placeholders differ from English (logging an
    error) and shows English instead, so a count here short of English, or that error, is a
    string the card would silently show in English.
    """
    caplog.set_level(logging.ERROR)
    served = await async_get_translations(hass, lang, "config_panel", ["hellofresh"])
    own = _card(TRANSLATIONS / f"{lang}.json")
    card = {
        key.removeprefix(PREFIX): value for key, value in served.items() if key.startswith(PREFIX)
    }
    assert card == own
    assert card["meta.language"] == lang
    assert "Validation of translation placeholders" not in caplog.text


async def test_a_language_without_card_text_gets_english(hass, enable_custom_integrations) -> None:
    """Spanish has no translation file: Home Assistant fills it from English, and the catalog's
    own language entry says so, so the card uses English plural rules for English words."""
    served = await async_get_translations(hass, "es", "config_panel", ["hellofresh"])
    card = {
        key.removeprefix(PREFIX): value for key, value in served.items() if key.startswith(PREFIX)
    }
    assert card == EN
    assert card["meta.language"] == "en"


# ---- the English module -------------------------------------------------------------------------


def test_the_english_module_is_generated_from_strings_json() -> None:
    """Run .github/scripts/generate_card_strings.py after changing strings.json."""
    expected = generate_card_strings.render(generate_card_strings.card_strings())
    assert (WWW / "hellofresh-i18n-en.js").read_text(encoding="utf-8") == expected


def test_every_plural_has_its_other_form() -> None:
    """The card falls back to `other` for any plural category a language lacks."""
    for lang in ("en", *LANGS):
        catalog = EN if lang == "en" else _card(TRANSLATIONS / f"{lang}.json")
        for key in catalog:
            if key.rsplit(".", 1)[-1] in {"zero", "one", "two", "few", "many"}:
                assert f"{key.rsplit('.', 1)[0]}.other" in catalog, f"{lang}: {key} has no .other"


# ---- the card names only keys that exist --------------------------------------------------------


def test_every_key_the_card_names_exists() -> None:
    """Every "group.key" literal in the card modules is a card string (or a plural's base)."""
    groups = sorted({key.split(".")[0] for key in EN})
    literal = re.compile(r'"((?:' + "|".join(groups) + r')\.[a-z0-9_.-]+)"')
    bases = {key.rsplit(".", 1)[0] for key in EN if key.rsplit(".", 1)[-1] in {"one", "other"}}
    missing = []
    for name in CARD_MODULES:
        for key in literal.findall((WWW / name).read_text(encoding="utf-8")):
            if key not in EN and key not in bases:
                missing.append(f"{name}: {key}")
    assert not missing, missing


def _run(body: str) -> object:
    """Run `body` under Node with the card's logic (L) and text layer (I18n) imported."""
    script = f"""
    globalThis.window = {{ localStorage: {{ getItem: () => null, setItem() {{}} }}, dispatchEvent() {{ return true; }} }};
    globalThis.CustomEvent = class {{ constructor(t, i) {{ this.type = t; this.detail = (i || {{}}).detail; }} }};
    const warnings = [];
    console.warn = (...args) => warnings.push(args.join(" "));
    const L = await import({json.dumps(LOGIC.as_uri() + "?v=test")});
    const I18n = await import({json.dumps(I18N.as_uri() + "?v=test")});
    const NB = {json.dumps(_card(TRANSLATIONS / "nb.json"))};
    const out = await (async () => {{ {body} }})();
    console.log(JSON.stringify({{ out, warnings }}));
    """
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "TZ": "America/New_York"},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@needs_node
def test_every_state_filter_and_step_has_its_words() -> None:
    """The labels the logic module builds from its own tables (week states, filter chips,
    tracking steps, live-tracking phases, statuses): none may be missing, in any language."""
    body = """
      const read = () => {
        for (const meta of Object.values(L.STATE_META)) [meta.label, meta.short];
        L.t("state.preselected.label"); L.t("state.preselected.short"); L.t("state.preselected.hint");
        for (const f of [...L.DIET_FILTERS, ...L.TIME_FILTERS, ...L.HIGHLIGHT_FILTERS]) f.label;
        for (const p of L.PROTEIN_FILTERS) L.proteinLabel(p);
        for (const step of L.TRACK_STEPS) L.t(`tracking.step.${step}`);
        for (const step of L.LIVE_STEPS) L.t(`live.step.${step}`);
        for (const phase of Object.keys(L.LIVE_PHASES)) {
          L.liveTracking({ state: "x", attributes: { active: true, phase } }, null);
        }
      };
      read();
      I18n.setStrings(NB);
      read();
      return null;
    """
    assert _run(body)["warnings"] == []


# ---- in Norwegian -------------------------------------------------------------------------------


@needs_node
def test_the_card_reads_norwegian() -> None:
    body = """
      I18n.setStrings(NB);
      const now = Date.now();
      const soon = new Date(now + 2 * 86400000 + 5 * 3600000 + 60000);
      const week = { week_id: "w", allowed_actions: { mealSwap: true }, auto_picked: true,
        selection_deadline: new Date(now + 86400000 * 3).toISOString(), recipes: [] };
      return {
        preselected: L.stateLabel(week),
        countdown: L.countdown(soon, now),
        passed: L.countdown(new Date(now - 1000), now),
        oneMeal: L.t("meals.count", { count: 1 }),
        threeMeals: L.t("meals.count", { count: 3 }),
        hours: L.formatMinutes(75),
        seafood: L.proteinLabel("Seafood"),
        calories: L.DIET_FILTERS.find((f) => f.key === "under-650-cal").label,
        status: L.statusLabel("OUT_FOR_DELIVERY"),
        unknownStatus: L.statusLabel("SOMETHING_NEW"),
        total: L.boxTotal({ order: { total_price: 10, currency: "NOK" } }, null).label,
        escaped: L.ht("recipe.open", { name: "<b>Taco</b>" }),
        markup: L.ht("tracking.delivered_at", { when: L.html("<strong>i dag</strong>") }),
        language: I18n.language(),
      };
    """
    out = _run(body)
    assert out["warnings"] == []
    assert out["out"] == {
        "preselected": "Forhåndsvalgt",
        "countdown": "2 d 5 t igjen",
        "passed": "utløpt",
        "oneMeal": "1 rett",
        "threeMeals": "3 retter",
        "hours": "1 t 15 min",
        "seafood": "Sjømat",
        "calories": "Under 650 kalorier",
        "status": "Ute for levering",
        "unknownStatus": "Something New",
        "total": "Totalt",
        "escaped": "Åpne oppskrift: &lt;b&gt;Taco&lt;/b&gt;",
        "markup": "Levert <strong>i dag</strong>",
        "language": "nb",
    }


@needs_node
def test_a_key_a_language_lacks_reads_in_english() -> None:
    body = """
      const partial = { ...NB };
      delete partial["menu.add"];
      I18n.setStrings(partial);
      return [L.t("menu.add"), L.t("menu.sold_out")];
    """
    assert _run(body)["out"] == ["Add", "Utsolgt"]


@needs_node
def test_the_card_loads_the_users_language_from_home_assistant() -> None:
    """useHass asks Home Assistant once per language (never for English), reports "not ready"
    until the text lands, tells its listeners, and falls back to English if the call fails.
    Dates and numbers follow the profile's language and number format."""
    body = """
      const resources = Object.fromEntries(Object.entries(NB).map(([k, v]) => [`component.hellofresh.config_panel.card.${k}`, v]));
      const calls = [];
      const hass = (language, extra = {}, fail = false) => ({
        language,
        locale: { language, number_format: "language", time_format: "language", ...extra },
        callWS: async (msg) => {
          calls.push(msg);
          if (fail) throw new Error("offline");
          return { resources };
        },
      });
      let heard = 0;
      I18n.onChange(() => (heard += 1));
      const english = I18n.useHass(hass("en"));
      const callsForEnglish = calls.length;
      const first = I18n.useHass(hass("nb", { number_format: "decimal_comma" }));
      const again = I18n.useHass(hass("nb", { number_format: "decimal_comma" }));
      await new Promise((r) => setTimeout(r, 10));
      const ready = I18n.useHass(hass("nb", { number_format: "decimal_comma" }));
      const nb = { tab: L.t("views.overview"), locale: I18n.dateLocale(), numbers: I18n.numberLocale() };
      I18n.useHass(hass("fr", {}, true));
      await new Promise((r) => setTimeout(r, 10));
      return { english, callsForEnglish, first, again, ready, heard, nb, request: calls[0], fallback: L.t("views.overview") };
    """
    out = _run(body)["out"]
    assert out["english"] is True and out["callsForEnglish"] == 0
    assert (out["first"], out["again"], out["ready"]) == (False, False, True)
    assert out["heard"] >= 2
    assert out["request"] == {
        "type": "frontend/get_translations",
        "language": "nb",
        "category": "config_panel",
        "integration": ["hellofresh"],
    }
    assert out["nb"] == {"tab": "Oversikt", "locale": "nb", "numbers": ["de", "es", "it"]}
    assert out["fallback"] == "Overview"
