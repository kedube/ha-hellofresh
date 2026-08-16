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
