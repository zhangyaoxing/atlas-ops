from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pyatlassdk.app.models.trigger import Trigger

from atlas_ops.trigger.export import (
    TriggerCollection,
    TriggerExportResult,
    export_triggers,
)


class FakeApps:
    def __init__(self, apps_by_product: dict[str | None, list[Any]]) -> None:
        self._apps_by_product = apps_by_product
        self.calls: list[tuple[str, str | None]] = []

    def admin_list_applications(self, group_id: str, *, product: str | None = None) -> list[Any]:
        self.calls.append((group_id, product))
        return self._apps_by_product.get(product, [])


class FakeServices:
    def __init__(self, services_by_app: dict[str, list[Any]]) -> None:
        self._services_by_app = services_by_app
        self.calls: list[tuple[str, str]] = []

    def admin_list_services(self, app_id: str, group_id: str) -> list[Any]:
        self.calls.append((app_id, group_id))
        return self._services_by_app.get(app_id, [])


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
        services_by_app: dict[str, list[Any]],
        triggers_by_app: dict[str, list[Any]],
    ) -> None:
        self.apps = FakeApps(apps_by_product)
        self.services = FakeServices(services_by_app)
        self.triggers = FakeTriggers(triggers_by_app)


def test_export_filters_by_cluster_and_groups_by_collection(
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
                    "_id": "svc-1",
                    "type": "mongodb-atlas",
                    "config": {"clusterName": "cluster0"},
                },
                {
                    "_id": "svc-2",
                    "type": "mongodb-atlas",
                    "config": {"clusterName": "other-cluster"},
                },
            ],
            "app-2": [
                {
                    "_id": "svc-3",
                    "type": "mongodb-atlas",
                    "config": {"clusterName": "cluster0"},
                }
            ],
            "app-3": [
                {
                    "_id": "svc-4",
                    "type": "mongodb-atlas",
                    "config": {"clusterName": "Cluster0"},
                }
            ],
        },
        {
            "app-1": [
                {
                    "_id": "trigger-2",
                    "name": "on-update",
                    "type": "DATABASE",
                    "disabled": True,
                    "config": {
                        "service_id": "svc-1",
                        "database": "db",
                        "collection": "coll",
                    },
                },
                Trigger.model_validate(
                    {
                        "_id": "trigger-1",
                        "name": "on-insert",
                        "type": "DATABASE",
                        "config": {
                            "service_id": "svc-1",
                            "database": "db",
                            "collection": "coll",
                        },
                    }
                ),
                {
                    "_id": "trigger-wrong-cluster",
                    "name": "wrong-cluster",
                    "type": "DATABASE",
                    "config": {
                        "service_id": "svc-2",
                        "database": "db",
                        "collection": "coll",
                    },
                },
                {
                    "_id": "trigger-scheduled",
                    "name": "nightly",
                    "type": "SCHEDULED",
                    "config": {"schedule": "* * * * *"},
                },
                {
                    "_id": "trigger-no-collection",
                    "name": "database-level",
                    "type": "DATABASE",
                    "config": {"service_id": "svc-1", "database": "db"},
                },
            ],
            "app-2": [],
            "app-3": [
                {
                    "_id": "trigger-3",
                    "name": "other-trigger",
                    "type": "DATABASE",
                    "config": {
                        "service_id": "svc-4",
                        "database": "other_db",
                        "collection": "other_coll",
                    },
                }
            ],
        },
    )

    result = export_triggers(
        client,
        "project-1",
        "cluster0",
        "cluster-id",
        tmp_path,
    )

    assert result == TriggerExportResult(
        files=2,
        triggers=3,
        apps=2,
        collections=(
            TriggerCollection(database="db", collection="coll", triggers=2),
            TriggerCollection(database="other_db", collection="other_coll", triggers=1),
        ),
    )
    payload = json.loads((tmp_path / "db.coll.json").read_text(encoding="utf-8"))
    assert payload["database"] == "db"
    assert payload["collection"] == "coll"
    assert [entry["trigger"]["name"] for entry in payload["triggers"]] == [
        "on-insert",
        "on-update",
    ]
    assert [entry["appName"] for entry in payload["triggers"]] == [
        "my-app",
        "my-app",
    ]
    assert all("_id" not in entry["trigger"] for entry in payload["triggers"])
    assert payload["triggers"][1]["trigger"]["disabled"] is True
    assert (tmp_path / "other_db.other_coll.json").exists()
    assert client.apps.calls == [
        ("project-1", None),
        ("project-1", "atlas"),
    ]
    assert client.services.calls == [
        ("app-3", "project-1"),
        ("app-2", "project-1"),
        ("app-1", "project-1"),
    ]
    assert client.triggers.calls == [
        ("app-3", "project-1"),
        ("app-2", "project-1"),
        ("app-1", "project-1"),
    ]


def test_export_skips_apps_without_matching_data_source(tmp_path: Path) -> None:
    client = FakeClient(
        {None: [SimpleNamespace(id="app-1", name="my-app")]},
        {
            "app-1": [
                {
                    "_id": "svc-1",
                    "type": "mongodb-atlas",
                    "config": {"clusterName": "other-cluster"},
                }
            ]
        },
        {"app-1": []},
    )

    result = export_triggers(
        client,
        "project-1",
        "cluster0",
        "cluster-id",
        tmp_path,
    )

    assert result == TriggerExportResult(files=0, triggers=0, apps=0, collections=())
    assert list(tmp_path.iterdir()) == []
    assert client.triggers.calls == []


def test_export_sanitizes_namespace_filename(tmp_path: Path) -> None:
    client = FakeClient(
        {None: [SimpleNamespace(id="app-1", name="my-app")]},
        {
            "app-1": [
                {
                    "_id": "svc-1",
                    "name": "cluster0",
                    "type": "mongodb-atlas",
                }
            ]
        },
        {
            "app-1": [
                {
                    "_id": "trigger-1",
                    "name": "on-insert",
                    "type": "DATABASE",
                    "config": {
                        "service_id": "svc-1",
                        "database": "db",
                        "collection": "a/b",
                    },
                }
            ]
        },
    )

    export_triggers(client, "project-1", "cluster0", "cluster-id", tmp_path)

    assert (tmp_path / "db.a_b.json").exists()


def test_export_matches_service_by_cluster_id(tmp_path: Path) -> None:
    client = FakeClient(
        {None: [SimpleNamespace(id="app-1", name="my-app")]},
        {
            "app-1": [
                {
                    "_id": "svc-1",
                    "type": "mongodb-atlas",
                    "config": {"clusterName": "renamed", "clusterId": 12345},
                }
            ]
        },
        {
            "app-1": [
                {
                    "_id": "trigger-1",
                    "name": "on-insert",
                    "type": "DATABASE",
                    "config": {
                        "service_id": "svc-1",
                        "database": "db",
                        "collection": "coll",
                    },
                }
            ]
        },
    )

    result = export_triggers(
        client,
        "project-1",
        "cluster0",
        "12345",
        tmp_path,
    )

    assert result.triggers == 1
    assert (tmp_path / "db.coll.json").exists()
