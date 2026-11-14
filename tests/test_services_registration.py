"""Checks on the service table in ``services.py`` and the files that describe it.

Services used to be registered by 29 hand-written ``hass.services.async_register`` calls, and a
stray duplicated argument in one of them once shipped: the extra positional pushed the handler
onto the ``schema`` parameter, setup raised a bare TypeError, and the whole integration failed
to load. Registration is now one loop over ``SERVICES``, so that slip cannot be written. What
can still go wrong is the table itself and the two hand-edited files next to it:

* a row naming a service twice, or binding one handler to two services (a copy-paste slip);
* ``services.yaml`` or the translations describing a different set of services or fields than
  the schemas accept, which shows up in the UI as a missing form, a raw key or a field that
  is rejected when used.
"""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from types import SimpleNamespace

from homeassistant.core import SupportsResponse
import pytest
import voluptuous as vol
import yaml

from custom_components.hellofresh import const
from custom_components.hellofresh.const import ATTR_CONFIG_ENTRY_ID, DOMAIN
from custom_components.hellofresh.services import (
    ATTR_INCLUDE_DEBUG,
    SERVICES,
    async_register_services,
)

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "hellofresh"
SERVICES_YAML = COMPONENT / "services.yaml"
STRINGS_JSON = COMPONENT / "strings.json"
EN_JSON = COMPONENT / "translations" / "en.json"

# Fields a schema accepts that are deliberately not offered in the UI.
HIDDEN_FIELDS = {ATTR_INCLUDE_DEBUG}


def _registered() -> dict[str, dict]:
    """Register against a stub hass and return each service's registration arguments."""
    calls: dict[str, dict] = {}

    def async_register(domain, name, handler, **kwargs) -> None:
        assert domain == DOMAIN
        assert name not in calls, f"{name} was registered twice"
        calls[name] = {"handler": handler, **kwargs}

    hass = SimpleNamespace(
        services=SimpleNamespace(has_service=lambda *_a: False, async_register=async_register)
    )
    async_register_services(hass)
    return calls


def _schema_fields(schema: vol.Schema) -> set[str]:
    return {str(marker) for marker in schema.schema}


def _documented() -> dict:
    return yaml.safe_load(SERVICES_YAML.read_text(encoding="utf-8"))


def test_the_table_holds_every_service_constant_exactly_once() -> None:
    """Each SERVICE_* constant is one row, and no row invents a name of its own."""
    declared = {value for name, value in vars(const).items() if name.startswith("SERVICE_")}
    names = [spec.name for spec in SERVICES]
    assert len(names) == 30
    assert set(names) == declared
    duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
    assert not duplicates, f"service names listed more than once: {duplicates}"


def test_no_handler_is_bound_to_two_services() -> None:
    """Two services sharing a handler is the signature of a copy-paste row."""
    handlers = Counter(spec.handler.__name__ for spec in SERVICES)
    duplicates = sorted(name for name, count in handlers.items() if count > 1)
    assert not duplicates, f"handlers bound to more than one service: {duplicates}"


def test_every_row_is_registered_with_its_schema_and_response_mode() -> None:
    registered = _registered()
    assert set(registered) == {spec.name for spec in SERVICES}
    for spec in SERVICES:
        call = registered[spec.name]
        assert call["supports_response"] is spec.supports_response
        # Every service can be pointed at one account.
        assert ATTR_CONFIG_ENTRY_ID in _schema_fields(call["schema"])


def test_services_that_answer_say_so() -> None:
    """A handler that returns data must be registered to return it, and the rest must not.

    Home Assistant refuses ``return_response`` on a service registered without it, and a
    response-only service cannot be called without asking for the response.
    """
    modes = {spec.name: spec.supports_response for spec in SERVICES}
    assert {name for name, mode in modes.items() if mode is SupportsResponse.NONE} == {
        "refresh_data",
        "skip_week",
        "unskip_week",
        "reschedule_week",
        "change_plan",
        "change_delivery_weekday",
    }
    assert {name for name, mode in modes.items() if mode is SupportsResponse.OPTIONAL} == {
        "set_food_profile",
        "select_meals",
        "select_market_items",
        "add_favorite",
        "remove_favorite",
        "set_planned_meal",
        "clear_planned_meal",
        # The card calls it with return_response and reads the new box from get_weeks.
        "change_week_servings",
    }


def test_services_yaml_documents_exactly_the_registered_services() -> None:
    """A service missing from services.yaml still runs, but has no UI form in HA; one that
    is documented but gone is a broken entry in the service picker."""
    assert set(_documented()) == {spec.name for spec in SERVICES}


@pytest.mark.parametrize("spec", SERVICES, ids=lambda spec: spec.name)
def test_services_yaml_fields_match_the_schema(spec) -> None:
    """The form offers every field the schema accepts, and nothing the schema rejects."""
    registered = _registered()[spec.name]
    accepted = _schema_fields(registered["schema"]) - HIDDEN_FIELDS
    offered = set((_documented()[spec.name] or {}).get("fields") or {})
    assert offered == accepted

    required = {
        str(marker) for marker in registered["schema"].schema if isinstance(marker, vol.Required)
    }
    marked_required = {
        name
        for name, field in ((_documented()[spec.name] or {}).get("fields") or {}).items()
        if field.get("required")
    }
    assert marked_required == required


def test_config_entry_id_uses_the_account_picker_everywhere() -> None:
    """The account field is a picker limited to HelloFresh entries, not a box to paste an id
    into."""
    for name, service in _documented().items():
        selector = service["fields"][ATTR_CONFIG_ENTRY_ID]["selector"]
        assert selector == {"config_entry": {"integration": DOMAIN}}, name


@pytest.mark.parametrize("path", [STRINGS_JSON, EN_JSON], ids=lambda path: path.name)
def test_translations_cover_every_service_and_field(path: Path) -> None:
    """Missing translations render as raw keys in the HA UI; extra ones are dead text."""
    translated = json.loads(path.read_text(encoding="utf-8")).get("services", {})
    documented = _documented()
    assert set(translated) == set(documented)
    for name, service in documented.items():
        yaml_fields = set(service.get("fields") or {})
        json_fields = set(translated[name].get("fields") or {})
        assert yaml_fields == json_fields, (
            f"{name}: services.yaml fields {sorted(yaml_fields)} != "
            f"{path.name} fields {sorted(json_fields)}"
        )
