"""Food profile card: diet-specific proteins, the site's validation rules, completion prompts
and its option labels.

Runs the real method bodies under Node against the shapes captured from
``/gw/profile-service/v2/profile/options`` (its ``_meta.primaryProteinsGroups``) and
``/profile/completion`` (``incomplete_fields`` as the integration flattens it).
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

CARD = (WWW / "hellofresh-food-profile-card.js").read_text(encoding="utf-8")

CONSTANTS = (
    "FIELD_LABELS",
    "VALUE_LABELS",
    "VALUE_DESCRIPTIONS",
    "PROTEIN_QUESTIONS",
    "DEFAULT_PROTEIN_QUESTION",
    "GOALS_MAX",
    "GOALS_HINT",
    "MEAL_TYPES_REQUIRED_MESSAGE",
    "EXCLUDE_NOTICE",
    "TELL_US_MORE",
    "PANELS",
    "LIKE",
    "DISLIKE",
)
METHODS = (
    "_draftList",
    "_toggleListValue",
    "_listMax",
    "_proteinOptions",
    "_proteinQuestion",
    "_applyDietChange",
    "_validationErrors",
    "_incompleteFields",
    "_needsInput",
    "_seedExpansion",
    "_tellUsMore",
    "_label",
    "_chips",
    "_renderSubCardNotice",
)


def _const(name: str) -> str:
    match = re.search(rf"^const {name} =.*?;\s*$", CARD, re.S | re.M)
    assert match, name
    return match.group(0)


def _method(name: str) -> str:
    match = re.search(rf"^  {name}\(.*?^  \}}", CARD, re.S | re.M)
    assert match, name
    return match.group(0)


# The captured options catalog, trimmed to what these tests exercise.
OPTIONS = {
    "taste": {
        "dietaryPreferences": ["mostly-meat", "flexitarian", "vegetarian", "pescatarian"],
        "primaryProteins": ["beef", "pork", "poultry", "fish", "shellfish", "plant-based-proteins"],
        "mealTypes": ["quick-easy", "batch", "chef-style", "family-style"],
        "exclusions": ["gluten", "pork", "shellfish"],
    },
    "goals": {
        "goals": [
            "save-money",
            "waste-less-food",
            "try-new-recipes",
            "save-time",
            "improve-health",
            "make-cooking-easy",
        ]
    },
    "meta": {
        "fieldsWithNone": ["taste.exclusions"],
        "primaryProteinsGroups": {
            "flexitarian": ["beef", "pork", "poultry", "fish", "shellfish", "plant-based-proteins"],
            "mostly-meat": ["beef", "pork", "poultry", "fish", "shellfish", "plant-based-proteins"],
            "pescatarian": ["tuna", "salmon", "cod", "seabass", "tilapia", "barramundi", "trout"],
            "vegetarian": ["tofu", "halloumi", "legumes", "mushroom-based-proteins"],
        },
    },
}


def _run(body: str, *, options: dict = OPTIONS, draft: dict | None = None, completion=None) -> dict:
    script = f"""
    {chr(10).join(_const(c) for c in CONSTANTS)}
    class Card {{
      constructor() {{
        this._options = {json.dumps(options)};
        this._draft = {json.dumps(draft if draft is not None else {"taste": {}, "household": {}, "goals": {}})};
        this._completion = {json.dumps(completion)};
        this._expanded = new Set();
        this._seededExpansion = false;
        this.renders = 0;
      }}
      _render() {{ this.renders += 1; }}
      _esc(v) {{ return String(v); }}
      {chr(10).join(_method(m) for m in METHODS)}
    }}
    const card = new Card();
    const out = (() => {{ {body} }})();
    console.log(JSON.stringify(out));
    """
    result = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(result.stdout)


def test_protein_options_follow_the_diet() -> None:
    out = _run(
        """
        const byDiet = {};
        for (const diet of ["mostly-meat", "flexitarian", "pescatarian", "vegetarian", "vegan", undefined]) {
          card._draft.taste.dietaryPreferences = diet ? [diet] : [];
          byDiet[diet || "none"] = { options: card._proteinOptions(), question: card._proteinQuestion() };
        }
        byDiet.explicit = card._proteinOptions("pescatarian");
        return byDiet;
        """
    )
    assert out["pescatarian"]["options"] == OPTIONS["meta"]["primaryProteinsGroups"]["pescatarian"]
    assert out["pescatarian"]["question"] == "Which seafood do you enjoy?"
    assert out["vegetarian"]["options"] == OPTIONS["meta"]["primaryProteinsGroups"]["vegetarian"]
    assert out["vegetarian"]["question"] == "Which meat-free proteins do you enjoy?"
    assert out["mostly-meat"]["options"] == OPTIONS["taste"]["primaryProteins"]
    assert out["mostly-meat"]["question"] == "Which proteins do you enjoy?"
    # A diet without a group (vegan is not in the US catalog) and no diet at all both fall
    # back to the catalog's flat list rather than hiding the section.
    assert out["vegan"]["options"] == OPTIONS["taste"]["primaryProteins"]
    assert out["none"]["options"] == OPTIONS["taste"]["primaryProteins"]
    assert out["explicit"] == OPTIONS["meta"]["primaryProteinsGroups"]["pescatarian"]


def test_protein_options_without_groups_metadata_use_the_flat_catalog() -> None:
    options = json.loads(json.dumps(OPTIONS))
    del options["meta"]["primaryProteinsGroups"]
    out = _run(
        """
        card._draft.taste.dietaryPreferences = ["pescatarian"];
        return card._proteinOptions();
        """,
        options=options,
    )
    assert out == OPTIONS["taste"]["primaryProteins"]


def test_changing_diet_reseeds_proteins_only_when_the_set_changes() -> None:
    draft = {
        "taste": {
            "dietaryPreferences": ["mostly-meat"],
            "primaryProteins": {"beef": 100, "pork": -100},
        },
        "household": {},
        "goals": {},
    }
    out = _run(
        """
        const steps = {};
        card._applyDietChange("flexitarian");
        steps.flexitarian = JSON.parse(JSON.stringify(card._draft.taste));
        card._applyDietChange("pescatarian");
        steps.pescatarian = JSON.parse(JSON.stringify(card._draft.taste));
        card._applyDietChange("");
        steps.cleared = JSON.parse(JSON.stringify(card._draft.taste));
        return steps;
        """,
        draft=draft,
    )
    # Same protein list: the user's likes and dislikes survive.
    assert out["flexitarian"]["dietaryPreferences"] == ["flexitarian"]
    assert out["flexitarian"]["primaryProteins"] == {"beef": 100, "pork": -100}
    # Different list: every member of the new group starts liked, as the site seeds it.
    assert out["pescatarian"]["dietaryPreferences"] == ["pescatarian"]
    assert out["pescatarian"]["primaryProteins"] == {
        slug: 100 for slug in OPTIONS["meta"]["primaryProteinsGroups"]["pescatarian"]
    }
    # Clearing the diet falls back to the flat catalog list, again fully liked.
    assert out["cleared"]["dietaryPreferences"] == []
    assert out["cleared"]["primaryProteins"] == {
        slug: 100 for slug in OPTIONS["taste"]["primaryProteins"]
    }


def test_goals_are_capped_at_three_and_blocked_chips_render() -> None:
    out = _run(
        """
        for (const g of ["save-money", "save-time", "improve-health", "try-new-recipes"]) {
          card._toggleListValue("goals", "goals", g);
        }
        const capped = card._draftList("goals", "goals").slice();
        const chips = card._chips("goals", "goals", card._options.goals.goals, capped);
        card._toggleListValue("goals", "goals", "save-money");
        const afterRemove = card._draftList("goals", "goals").slice();
        card._toggleListValue("goals", "goals", "try-new-recipes");
        const styles = card._chips("taste", "mealTypes", card._options.taste.mealTypes, []);
        // Unbounded lists never block.
        for (const e of card._options.taste.exclusions) card._toggleListValue("taste", "exclusions", e);
        return {
          capped, afterRemove, final: card._draftList("goals", "goals"),
          blocked: (chips.match(/blocked/g) || []).length,
          ariaDisabled: (chips.match(/aria-disabled="true"/g) || []).length,
          stylesTitle: /title="Under 20 minutes, minimal prep"/.test(styles),
          exclusions: card._draftList("taste", "exclusions").length,
          max: card._listMax("goals", "goals"), noMax: card._listMax("taste", "exclusions"),
        };
        """
    )
    assert out["capped"] == ["save-money", "save-time", "improve-health"]
    assert out["afterRemove"] == ["save-time", "improve-health"]
    assert out["final"] == ["save-time", "improve-health", "try-new-recipes"]
    # Three unselected goals are blocked while three are chosen.
    assert out["blocked"] == 3
    assert out["ariaDisabled"] == 3
    assert out["stylesTitle"] is True
    assert out["exclusions"] == 3
    assert out["max"] == 3 and out["noMax"] is None


def test_cooking_styles_are_required_and_exclusions_carry_the_heads_up() -> None:
    out = _run(
        """
        const empty = card._validationErrors();
        const emptyNotice = card._renderSubCardNotice("taste", "mealTypes");
        card._toggleListValue("taste", "mealTypes", "batch");
        const ok = card._validationErrors();
        const okNotice = card._renderSubCardNotice("taste", "mealTypes");
        const noExclusions = card._renderSubCardNotice("taste", "exclusions");
        card._toggleListValue("taste", "exclusions", "gluten");
        const withExclusions = card._renderSubCardNotice("taste", "exclusions");
        return { empty, emptyNotice, ok, okNotice, noExclusions, withExclusions };
        """
    )
    assert out["empty"] == ["At least one cooking style is required"]
    assert "At least one cooking style is required" in out["emptyNotice"]
    assert 'class="note error"' in out["emptyNotice"]
    assert out["ok"] == []
    assert out["okNotice"] == ""
    assert out["noExclusions"] == ""
    assert "some recipes in your menu may still include these ingredients" in out["withExclusions"]


def test_validation_is_silent_when_the_catalog_has_no_meal_types() -> None:
    options = json.loads(json.dumps(OPTIONS))
    del options["taste"]["mealTypes"]
    assert _run("return card._validationErrors();", options=options) == []


def test_incomplete_fields_open_their_sub_cards_once_and_get_a_badge() -> None:
    completion = {
        "completed": 7,
        "total": 10,
        "percent": 70,
        "incomplete_fields": ["taste.cuisines", "goals.goals", "household.totalPeople"],
    }
    out = _run(
        """
        card._seedExpansion();
        const first = [...card._expanded].sort();
        card._expanded.delete("taste.cuisines");
        card._seedExpansion();
        const second = [...card._expanded].sort();
        return {
          first, second,
          cuisines: card._tellUsMore("taste.cuisines"),
          household: card._tellUsMore("household.totalPeople"),
          flavors: card._tellUsMore("taste.flavors"),
          needs: card._needsInput("goals.goals"),
        };
        """,
        completion=completion,
    )
    # Only sub-cards can expand; the household prompt goes on its panel title instead.
    assert out["first"] == ["goals.goals", "taste.cuisines"]
    # A second seed (after a save's refetch) respects what the user closed.
    assert out["second"] == ["goals.goals"]
    assert "Tell us more" in out["cuisines"]
    assert "Tell us more" in out["household"]
    assert out["flavors"] == ""
    assert out["needs"] is True


def test_no_completion_means_no_prompts() -> None:
    out = _run(
        """
        card._seedExpansion();
        return { expanded: [...card._expanded], badge: card._tellUsMore("taste.cuisines") };
        """
    )
    assert out == {"expanded": [], "badge": ""}


def test_labels_match_the_hellofresh_site() -> None:
    slugs = [
        "mostly-meat",
        "glp1-support",
        "bake",
        "soups-stews",
        "stir-fry",
        "make-cooking-easy",
        "plant-based-proteins",
        "brussel-sprouts",
        "classic-american",
        "low-calorie",
        "try-new-recipes",
        "waste-less-food",
        "high-protein",
        "mushroom-based-proteins",
        "salmon",
        "exclusions",
        "QUANTITY_1_2",
    ]
    out = _run(f"return {json.dumps(slugs)}.map((s) => card._label(s));")
    assert out == [
        "I eat everything",
        "GLP-1 friendly",
        "Bakes",
        "Soups or Stews",
        "Stir fries",
        "Cook easier",
        "Plant based proteins",
        "Brussels sprouts",
        "Classic American",
        "Low calorie",
        "Try new recipes",
        "Waste less food",
        "High protein",
        "Mushroom-based proteins",
        "Salmon",
        "Exclude",
        "1 2",
    ]
