"""Frontend resource-version tests.

Every card resource URL carries a ``?v=`` cache-bust stamped from the manifest release
version, and registration must *update* a previously registered URL when the version
changes — otherwise existing installs keep loading a stale cached card forever (only
fresh installs would ever see a bump). Diagnostics exposes expected vs. registered URLs
so a stale resource is visible from a redacted diagnostics download.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from custom_components.hellofresh import frontend as frontend_module
from custom_components.hellofresh.frontend import (
    _CARDS,
    CLASSIC_CARD_FILENAMES,
    INTEGRATION_VERSION,
    SCHEDULE_CARD_FILENAME,
    UNIFIED_CARD_FILENAME,
    _async_register_lovelace_resources,
    _async_sync_classic_cards,
    async_classic_cards_in_use,
    async_get_frontend_diagnostics,
)

UNIFIED_URL = next(url for name, _p, url in _CARDS if name == UNIFIED_CARD_FILENAME)


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


class _FakeResources:
    """Storage-mode Lovelace resource collection recording mutations."""

    def __init__(self, urls: list[str]) -> None:
        self.loaded = True
        self.store = object()  # storage mode marker; YAML mode has no store
        self._items = [
            {"id": f"item{i}", "res_type": "module", "url": url} for i, url in enumerate(urls)
        ]
        self.created: list[dict] = []
        self.updated: list[tuple[str, dict]] = []
        self.deleted: list[str] = []

    def async_items(self) -> list[dict]:
        return self._items

    async def async_load(self) -> None:
        pass

    async def async_create_item(self, data: dict) -> None:
        self.created.append(data)

    async def async_update_item(self, item_id: str, updates: dict) -> None:
        self.updated.append((item_id, updates))

    async def async_delete_item(self, item_id: str) -> None:
        self.deleted.append(item_id)


def _make_hass(resources: _FakeResources | None) -> SimpleNamespace:
    lovelace = SimpleNamespace(resources=resources) if resources is not None else None
    return SimpleNamespace(data={"lovelace": lovelace} if lovelace else {})


def test_resource_urls_stamped_with_manifest_version() -> None:
    """All card ?v= stamps must equal the manifest release version (single source of truth)."""
    manifest = json.loads(
        (
            Path(__file__).parent.parent / "custom_components" / "hellofresh" / "manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["version"] == INTEGRATION_VERSION
    for _filename, url_path, resource_url in _CARDS:
        assert resource_url == f"{url_path}?v={INTEGRATION_VERSION}"


def test_a_fresh_install_registers_only_the_hellofresh_card() -> None:
    """The deprecated classic cards stay out of a Home Assistant whose dashboards don't use
    them, so they aren't downloaded on every dashboard load."""
    resources = _FakeResources([])
    _run(_async_register_lovelace_resources(_make_hass(resources)))

    assert not resources.updated and not resources.deleted
    assert [item["url"] for item in resources.created] == [UNIFIED_URL]
    assert all(item["res_type"] == "module" for item in resources.created)


def test_a_classic_card_in_use_is_registered_too() -> None:
    resources = _FakeResources([])
    _run(_async_register_lovelace_resources(_make_hass(resources), {SCHEDULE_CARD_FILENAME}))

    schedule_url = next(url for name, _p, url in _CARDS if name == SCHEDULE_CARD_FILENAME)
    assert [item["url"] for item in resources.created] == [UNIFIED_URL, schedule_url]


def test_unused_classic_resources_are_removed() -> None:
    """An install from before the deprecation has all seven registered; the ones no dashboard
    uses go, the one still in use and the HelloFresh card stay."""
    resources = _FakeResources([resource_url for _f, _p, resource_url in _CARDS])
    ids = {item["url"]: item["id"] for item in resources.async_items()}
    _run(_async_register_lovelace_resources(_make_hass(resources), {SCHEDULE_CARD_FILENAME}))

    removed = {url for url, item_id in ids.items() if item_id in resources.deleted}
    assert removed == {
        url
        for name, _p, url in _CARDS
        if name in CLASSIC_CARD_FILENAMES and name != SCHEDULE_CARD_FILENAME
    }
    assert not resources.created and not resources.updated


def test_stale_resource_updated_in_place() -> None:
    """A resource registered under an old ?v= is updated, not skipped or duplicated."""
    _filename, url_path, resource_url = _CARDS[0]
    resources = _FakeResources([f"{url_path}?v=0.0.1"])
    _run(_async_register_lovelace_resources(_make_hass(resources)))

    assert ("item0", {"url": resource_url}) in resources.updated
    # The stale card was updated in place; with no classic card in use nothing else is added.
    assert not resources.created


def test_current_resources_left_untouched() -> None:
    """Re-running registration on an up-to-date install whose dashboards use every card is a
    no-op."""
    resources = _FakeResources([resource_url for _f, _p, resource_url in _CARDS])
    _run(_async_register_lovelace_resources(_make_hass(resources), set(CLASSIC_CARD_FILENAMES)))

    assert not resources.created
    assert not resources.updated
    assert not resources.deleted


class _Dashboard:
    def __init__(self, config, title=None, missing=False):
        self._config = config
        self.config = {"title": title} if title else None
        self._missing = missing

    async def async_load(self, force):
        if self._missing:
            raise LookupError("ConfigNotFound")
        return self._config


def _lovelace_hass(dashboards, resources=None):
    lovelace = SimpleNamespace(dashboards=dashboards, resources=resources or _FakeResources([]))
    return SimpleNamespace(data={"lovelace": lovelace})


def test_the_scan_finds_nested_classic_cards_and_names_their_dashboards() -> None:
    kitchen = {
        "views": [
            {
                "cards": [
                    {
                        "type": "vertical-stack",
                        "cards": [{"type": "custom:hellofresh-schedule-card"}],
                    },
                    {"type": "custom:hellofresh-card"},
                ]
            },
            {"sections": [{"cards": [{"type": "custom:hellofresh-cost-card", "weeks": 4}]}]},
        ]
    }
    default = {"views": [{"cards": [{"type": "custom:hellofresh-schedule-card"}]}]}
    hass = _lovelace_hass(
        {
            None: _Dashboard(default),
            "kitchen-tablet": _Dashboard(kitchen, title="Kitchen"),
            "auto": _Dashboard(None, missing=True),  # auto-generated: nothing to read
        }
    )
    assert _run(async_classic_cards_in_use(hass)) == {
        "custom:hellofresh-cost-card": ["Kitchen"],
        "custom:hellofresh-schedule-card": ["Kitchen", "Overview"],
    }


def test_a_sync_registers_what_is_used_and_updates_the_notice(monkeypatch) -> None:
    notices: list = []
    monkeypatch.setattr(
        frontend_module,
        "async_update_classic_cards_issue",
        lambda hass, in_use: notices.append(in_use),
    )
    resources = _FakeResources([])
    board = {"views": [{"cards": [{"type": "custom:hellofresh-schedule-card"}]}]}
    hass = _lovelace_hass({"home": _Dashboard(board, title="Home")}, resources)
    _run(_async_sync_classic_cards(hass))

    schedule_url = next(url for name, _p, url in _CARDS if name == SCHEDULE_CARD_FILENAME)
    assert [item["url"] for item in resources.created] == [UNIFIED_URL, schedule_url]
    assert notices == [{"custom:hellofresh-schedule-card": ["Home"]}]
    assert async_get_frontend_diagnostics(hass)["classic_cards_in_use"] == notices[0]

    # The dashboard moves to the HelloFresh card: the notice clears.
    board["views"][0]["cards"] = [{"type": "custom:hellofresh-card"}]
    _run(_async_sync_classic_cards(hass))
    assert notices[-1] == {}


def test_saving_a_dashboard_rechecks_the_classic_cards() -> None:
    listened: list = []
    hass = SimpleNamespace(
        data={},
        bus=SimpleNamespace(async_listen=lambda event, handler: listened.append(event)),
    )
    frontend_module._async_listen_for_dashboard_saves(hass)
    assert listened == ["lovelace_updated"]


def test_diagnostics_reports_expected_vs_registered() -> None:
    """Diagnostics surfaces the release version, expected URLs, and actual registrations."""
    _filename, url_path, _resource_url = _CARDS[0]
    stale_url = f"{url_path}?v=0.0.1"
    resources = _FakeResources([stale_url, "/other/unrelated.js"])

    info = async_get_frontend_diagnostics(_make_hass(resources))

    assert info["integration_version"] == INTEGRATION_VERSION
    assert info["expected_resources"] == {
        filename: resource_url for filename, _p, resource_url in _CARDS
    }
    # Only integration-owned URLs are listed, so the stale ?v= stands out on comparison.
    assert info["registered_resources"] == [stale_url]


def test_diagnostics_survives_missing_lovelace() -> None:
    """Diagnostics must not fail when Lovelace data is absent (e.g. early startup)."""
    info = async_get_frontend_diagnostics(SimpleNamespace(data={}))

    assert info["integration_version"] == INTEGRATION_VERSION
    assert info["registered_resources"] == []


def test_every_registered_card_file_is_shipped() -> None:
    """Each card in _CARDS must exist in www/.

    Registration only guards on the meal-planner card's presence, so a card added to _CARDS
    but missing from www/ would register a Lovelace resource pointing at a 404 with no error.
    """
    www_dir = Path(frontend_module.__file__).parent / "www"
    missing = [filename for filename, _p, _r in _CARDS if not (www_dir / filename).is_file()]

    assert not missing, f"registered cards missing from www/: {missing}"


def test_modules_imported_by_cards_are_shipped() -> None:
    """A card's own imports must exist in www/ too.

    Shared modules (e.g. the recipe-detail sheet) are NOT in _CARDS — they are dependencies,
    not Lovelace resources, and are served by the same static mount. That means nothing else
    checks they were shipped: a missing one would 404 at import time and silently break every
    card that depends on it.
    """
    www_dir = Path(frontend_module.__file__).parent / "www"
    import re

    missing = []
    for js in sorted(www_dir.glob("*.js")):
        source = js.read_text(encoding="utf-8")
        # Both static (`from "./x.js"`) and dynamic (`import(... "./x.js?v=" ...)`) forms.
        for ref in re.findall(r"""["'`]\./([A-Za-z0-9._-]+\.js)""", source):
            if not (www_dir / ref).is_file():
                missing.append(f"{js.name} -> {ref}")

    assert not missing, f"card imports missing from www/: {missing}"


def test_shared_detail_module_is_not_registered_as_a_card() -> None:
    """The shared sheet is a dependency, not a card.

    Registering it as a Lovelace resource would load a module that defines no custom element,
    which does nothing useful and shows up as a phantom card resource for users.
    """
    assert not any("recipe-detail" in filename for filename, _p, _r in _CARDS)


def test_card_file_check_runs_in_the_executor() -> None:
    """The www/ file check must not touch the disk on the event loop.

    ``Path.is_file()`` is blocking I/O. Called directly inside a coroutine it stalls the loop
    and trips Home Assistant's synchronous-I/O detection, which is what a HACS reviewer flagged
    against this function. The check has to go through ``async_add_executor_job``.
    """
    executor_calls: list = []

    async def async_add_executor_job(func, *args):
        executor_calls.append(func)
        return func(*args)

    registered: list = []

    async def async_register_static_paths(configs) -> None:
        registered.append(configs)

    hass = SimpleNamespace(
        data={},
        async_add_executor_job=async_add_executor_job,
        http=SimpleNamespace(async_register_static_paths=async_register_static_paths),
    )

    _run(frontend_module.async_register_meal_planner_card(hass))

    # The real www/ directory ships the card, so registration proceeds to serving it.
    assert executor_calls, "the file check must be handed to the executor"
    assert registered, "static paths should be registered when the card file exists"


def test_missing_card_file_aborts_registration() -> None:
    """A missing card file warns and stops, without registering static paths."""
    executor_calls: list = []

    async def async_add_executor_job(func, *args):
        executor_calls.append(func)
        return False  # pretend the card file is absent

    registered: list = []

    async def async_register_static_paths(configs) -> None:  # pragma: no cover - must not run
        registered.append(configs)

    hass = SimpleNamespace(
        data={},
        async_add_executor_job=async_add_executor_job,
        http=SimpleNamespace(async_register_static_paths=async_register_static_paths),
    )

    _run(frontend_module.async_register_meal_planner_card(hass))

    assert executor_calls, "the file check must still go through the executor"
    assert not registered, "a missing card file must abort before serving assets"


def test_registration_is_idempotent() -> None:
    """A second call is a no-op, so the file check does not run again."""
    executor_calls: list = []

    async def async_add_executor_job(func, *args):
        executor_calls.append(func)
        return func(*args)

    async def async_register_static_paths(configs) -> None:
        pass

    hass = SimpleNamespace(
        data={},
        async_add_executor_job=async_add_executor_job,
        http=SimpleNamespace(async_register_static_paths=async_register_static_paths),
    )

    _run(frontend_module.async_register_meal_planner_card(hass))
    first = len(executor_calls)
    _run(frontend_module.async_register_meal_planner_card(hass))

    assert len(executor_calls) == first, "the guard must short-circuit a repeat call"
