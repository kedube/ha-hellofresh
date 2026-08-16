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
    INTEGRATION_VERSION,
    _async_register_lovelace_resources,
    async_get_frontend_diagnostics,
)


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
            {"id": f"item{i}", "res_type": "module", "url": url}
            for i, url in enumerate(urls)
        ]
        self.created: list[dict] = []
        self.updated: list[tuple[str, dict]] = []

    def async_items(self) -> list[dict]:
        return self._items

    async def async_load(self) -> None:
        pass

    async def async_create_item(self, data: dict) -> None:
        self.created.append(data)

    async def async_update_item(self, item_id: str, updates: dict) -> None:
        self.updated.append((item_id, updates))


def _make_hass(resources: _FakeResources | None) -> SimpleNamespace:
    lovelace = SimpleNamespace(resources=resources) if resources is not None else None
    return SimpleNamespace(data={"lovelace": lovelace} if lovelace else {})


def test_resource_urls_stamped_with_manifest_version() -> None:
    """All card ?v= stamps must equal the manifest release version (single source of truth)."""
    manifest = json.loads(
        (
            Path(__file__).parent.parent
            / "custom_components"
            / "hellofresh"
            / "manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["version"] == INTEGRATION_VERSION
    for _filename, url_path, resource_url in _CARDS:
        assert resource_url == f"{url_path}?v={INTEGRATION_VERSION}"


def test_missing_resources_are_created() -> None:
    """A fresh install registers every shipped card with the current version stamp."""
    resources = _FakeResources([])
    _run(_async_register_lovelace_resources(_make_hass(resources)))

    assert not resources.updated
    assert [item["url"] for item in resources.created] == [
        resource_url for _f, _p, resource_url in _CARDS
    ]
    assert all(item["res_type"] == "module" for item in resources.created)


def test_stale_resource_updated_in_place() -> None:
    """A resource registered under an old ?v= is updated, not skipped or duplicated."""
    _filename, url_path, resource_url = _CARDS[0]
    resources = _FakeResources([f"{url_path}?v=0.0.1"])
    _run(_async_register_lovelace_resources(_make_hass(resources)))

    assert ("item0", {"url": resource_url}) in resources.updated
    # The stale card was updated; the other cards were created fresh.
    created_urls = [item["url"] for item in resources.created]
    assert resource_url not in created_urls
    assert len(created_urls) == len(_CARDS) - 1


def test_current_resources_left_untouched() -> None:
    """Re-running registration on an up-to-date install is a no-op."""
    resources = _FakeResources([resource_url for _f, _p, resource_url in _CARDS])
    _run(_async_register_lovelace_resources(_make_hass(resources)))

    assert not resources.created
    assert not resources.updated


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
