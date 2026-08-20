"""Static checks on the service registrations in ``__init__.py``.

A malformed ``hass.services.async_register`` call is invisible to the rest of the suite: it
raises at *setup* time, so the whole integration fails to load with a bare TypeError and no
entities appear at all. A stray duplicated argument once shipped this way --

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_RECIPE_COLLECTIONS,
        SERVICE_GET_RECIPE_DETAIL,       # <-- debris from a bad edit
        async_get_recipe_collections,
        schema=...,                      # -> "got multiple values for argument 'schema'"
    )

-- because the extra positional pushed the handler onto the ``schema`` parameter.

These tests parse the module rather than importing it: ``__init__.py`` pulls in Home
Assistant, which is not a test dependency here, and the defect is structural anyway.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

INIT_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "hellofresh" / "__init__.py"


def _registration_calls() -> list[ast.Call]:
    tree = ast.parse(INIT_PATH.read_text())
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "async_register"
    ]


def _defined_functions() -> set[str]:
    tree = ast.parse(INIT_PATH.read_text())
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_services_are_actually_registered() -> None:
    """Guard the guard: if this finds nothing, every test below passes vacuously."""
    assert len(_registration_calls()) >= 20


@pytest.mark.parametrize("call", _registration_calls(), ids=lambda c: f"line{c.lineno}")
def test_every_registration_passes_exactly_domain_service_handler(call: ast.Call) -> None:
    """The three positional args must be exactly (domain, service, handler).

    This is the check that catches the shipped bug: a fourth positional silently binds to
    ``schema``, which then collides with the ``schema=`` keyword.
    """
    positional = len(call.args)
    assert positional == 3, (
        f"async_register at line {call.lineno} takes {positional} positional arguments; "
        "expected exactly 3 (domain, service, handler). An extra positional binds to "
        "'schema' and breaks setup."
    )
    # The same collision can arrive via keyword, e.g. passing func= alongside a positional.
    collisions = {kw.arg for kw in call.keywords} & {"domain", "service", "func"}
    assert not collisions, (
        f"async_register at line {call.lineno} passes {sorted(collisions)} as keywords "
        "as well as positionally."
    )


def test_registered_handlers_all_exist() -> None:
    """Every handler named in a registration must be a function defined in the module."""
    defined = _defined_functions()
    missing = [
        (call.lineno, ast.unparse(call.args[2]))
        for call in _registration_calls()
        if isinstance(call.args[2], ast.Name) and call.args[2].id not in defined
    ]
    assert not missing, f"registrations reference undefined handlers: {missing}"


def test_no_service_name_is_registered_twice() -> None:
    """A duplicate registration silently overwrites the first handler."""
    names = [ast.unparse(call.args[1]) for call in _registration_calls()]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert not duplicates, f"service names registered more than once: {duplicates}"


def test_no_handler_is_bound_to_two_services() -> None:
    """Two services sharing a handler is the signature of a copy-paste registration slip."""
    handlers = [ast.unparse(call.args[2]) for call in _registration_calls()]
    duplicates = sorted({h for h in handlers if handlers.count(h) > 1})
    assert not duplicates, f"handlers bound to more than one service: {duplicates}"


# --- registration <-> services.yaml <-> strings.json parity ------------------------

SERVICES_YAML = INIT_PATH.parent / "services.yaml"
STRINGS_JSON = INIT_PATH.parent / "strings.json"
EN_JSON = INIT_PATH.parent / "translations" / "en.json"


def _registered_service_names() -> set[str]:
    """Service names as registered, resolved from the SERVICE_* constants in const.py."""
    import ast as _ast

    const_path = INIT_PATH.parent / "const.py"
    consts: dict[str, str] = {}
    for node in _ast.walk(_ast.parse(const_path.read_text())):
        if isinstance(node, _ast.Assign) and isinstance(node.value, _ast.Constant):
            for target in node.targets:
                if isinstance(target, _ast.Name) and target.id.startswith("SERVICE_"):
                    consts[target.id] = node.value.value

    names: set[str] = set()
    for call in _registration_calls():
        if len(call.args) < 2:
            continue
        second = call.args[1]
        if isinstance(second, _ast.Name) and second.id in consts:
            names.add(consts[second.id])
    return names


def test_every_registered_service_is_documented_in_services_yaml() -> None:
    """A service missing from services.yaml still runs, but has no UI form in HA.

    That is a silent, user-visible gap: the service appears in Developer Tools with no fields,
    so nobody can discover its arguments.
    """
    import yaml

    documented = set(yaml.safe_load(SERVICES_YAML.read_text()))
    registered = _registered_service_names()
    assert registered, "no service registrations were resolved -- the parser is broken"
    assert registered - documented == set(), (
        f"registered but undocumented in services.yaml: {sorted(registered - documented)}"
    )


def test_services_yaml_has_no_entries_for_unregistered_services() -> None:
    """The reverse: a documented service that no longer exists is a broken UI entry."""
    import yaml

    documented = set(yaml.safe_load(SERVICES_YAML.read_text()))
    registered = _registered_service_names()
    assert documented - registered == set(), (
        f"documented in services.yaml but not registered: {sorted(documented - registered)}"
    )


@pytest.mark.parametrize("path", [STRINGS_JSON, EN_JSON])
def test_translations_cover_every_registered_service(path: Path) -> None:
    """Missing translations render as raw keys in the HA UI."""
    import json

    translated = set(json.loads(path.read_text()).get("services", {}))
    registered = _registered_service_names()
    assert registered - translated == set(), (
        f"{path.name} is missing service translations for: {sorted(registered - translated)}"
    )


@pytest.mark.parametrize("path", [STRINGS_JSON, EN_JSON])
def test_translated_service_fields_match_services_yaml(path: Path) -> None:
    """Each service's documented fields must line up with its translated fields.

    A field present in one but not the other shows up in the UI as an untranslated key or an
    undocumented input -- both are silent, and neither breaks any other test.
    """
    import json

    import yaml

    documented = yaml.safe_load(SERVICES_YAML.read_text())
    translated = json.loads(path.read_text()).get("services", {})

    for name in _registered_service_names():
        yaml_fields = set((documented.get(name) or {}).get("fields") or {})
        json_fields = set((translated.get(name) or {}).get("fields") or {})
        assert yaml_fields == json_fields, (
            f"{name}: services.yaml fields {sorted(yaml_fields)} != "
            f"{path.name} fields {sorted(json_fields)}"
        )
