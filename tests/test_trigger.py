from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pyatlassdk.app.models.trigger import Trigger

from atlas_ops.trigger import TriggerApp, TriggerExportResult, export_triggers


class FakeApps:
    def __init__(self, apps_by_product: dict[str | None, list[Any]]) -> None:
        self._apps_by_product = apps_by_product
        self.calls: list[tuple[str, str | None]] = []

    def admin_list_applications(self, group_id: str, *, product: str | None = None) -> list[Any]:
        self.calls.append((group_id, product))
        return self._apps_by_product.get(product, [])


class FakeTriggers:
    def __init__(self, triggers_by_app: dict[str, list[Any]]) -> None:
        self._triggers_by_app = triggers_by_app
        self.calls: list[tuple[str, str]] = []

    def admin_list_triggers(self, app_id: str, group_id: str) -> list[Any]:
        self.calls.append((app_id, group_id))
        return self._triggers_by_app.get(app_id, [])


class FakeClient:
    def __init__(
        self,
        apps_by_product: dict[str | None, list[Any]],
        triggers_by_app: dict[str, list[Any]],
    ) -> None:
        self.apps = FakeApps(apps_by_product)
        self.triggers = FakeTriggers(triggers_by_app)


def test_export_writes_one_file_per_app_and_drops_trigger_id(
    tmp_path: Path,
) -> None:
    client = FakeClient(
        {
            None: [
                SimpleNamespace(id="app-1", name="my-app"),
                SimpleNamespace(id="app-2", name="empty-app"),
            ],
            "atlas": [
                SimpleNamespace(id="app-1", name="my-app"),
                SimpleNamespace(id="app-3", name="atlas-app"),
            ],
        },
        {
            "app-1": [
                {
                    "_id": "trigger-2",
                    "name": "on-update",
                    "type": "DATABASE",
                    "disabled": True,
                    "config": {"database": "db", "collection": "coll"},
                },
                Trigger.model_validate(
                    {
                        "_id": "trigger-1",
                        "name": "on-insert",
                        "type": "DATABASE",
                        "config": {"database": "db", "collection": "coll"},
                    }
                ),
            ],
            "app-2": [],
            "app-3": [
                {
                    "_id": "trigger-3",
                    "name": "nightly",
                    "type": "SCHEDULED",
                    "config": {"schedule": "* * * * *"},
                }
            ],
        },
    )

    result = export_triggers(client, "project-1", tmp_path)

    assert result == TriggerExportResult(
        files=2,
        triggers=3,
        apps=(
            TriggerApp(app_id="app-3", app_name="atlas-app", triggers=1),
            TriggerApp(app_id="app-1", app_name="my-app", triggers=2),
        ),
    )
    payload = json.loads((tmp_path / "my-app.json").read_text(encoding="utf-8"))
    assert payload["appId"] == "app-1"
    assert payload["appName"] == "my-app"
    assert [item["name"] for item in payload["triggers"]] == [
        "on-insert",
        "on-update",
    ]
    assert all("_id" not in item for item in payload["triggers"])
    assert payload["triggers"][1]["disabled"] is True
    assert (
        json.loads((tmp_path / "atlas-app.json").read_text(encoding="utf-8"))["triggers"][0]["name"]
        == "nightly"
    )
    assert client.apps.calls == [
        ("project-1", None),
        ("project-1", "atlas"),
    ]
    assert client.triggers.calls == [
        ("app-3", "project-1"),
        ("app-2", "project-1"),
        ("app-1", "project-1"),
    ]


def test_export_skips_apps_without_triggers(tmp_path: Path) -> None:
    client = FakeClient(
        {None: [SimpleNamespace(id="app-1", name="empty")]},
        {"app-1": []},
    )

    result = export_triggers(client, "project-1", tmp_path)

    assert result == TriggerExportResult(files=0, triggers=0, apps=())
    assert list(tmp_path.iterdir()) == []


def test_export_sanitizes_app_filename_and_falls_back_to_app_id(
    tmp_path: Path,
) -> None:
    client = FakeClient(
        {None: [SimpleNamespace(id="app-1", name="my/app")]},
        {"app-1": [{"_id": "trigger-1", "name": "on-insert", "type": "DATABASE"}]},
    )

    result = export_triggers(client, "project-1", tmp_path)

    assert result.files == 1
    assert (tmp_path / "my_app.json").exists()


def test_export_uses_app_id_when_name_is_missing(tmp_path: Path) -> None:
    client = FakeClient(
        {None: [SimpleNamespace(id="app-1")]},
        {"app-1": [{"_id": "trigger-1", "name": "on-insert", "type": "DATABASE"}]},
    )

    export_triggers(client, "project-1", tmp_path)

    assert (tmp_path / "app-1.json").exists()
