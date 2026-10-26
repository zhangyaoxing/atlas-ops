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


class FakeFunctions:
    def __init__(
        self,
        metadata_by_app: dict[str, list[Any]],
        definitions_by_id: dict[str, Any],
    ) -> None:
        self._metadata_by_app = metadata_by_app
        self._definitions_by_id = definitions_by_id
        self.list_calls: list[tuple[str, str]] = []
        self.get_calls: list[tuple[str, str, str]] = []

    def admin_list_functions(self, app_id: str, group_id: str) -> list[Any]:
        self.list_calls.append((app_id, group_id))
        return self._metadata_by_app.get(app_id, [])

    def admin_get_function(self, app_id: str, function_id: str, group_id: str) -> Any:
        self.get_calls.append((app_id, function_id, group_id))
        return self._definitions_by_id[function_id]


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
        functions_by_app: dict[str, list[Any]] | None = None,
        function_definitions: dict[str, Any] | None = None,
    ) -> None:
        self.apps = FakeApps(apps_by_product)
        self.services = FakeServices(services_by_app)
        self.triggers = FakeTriggers(triggers_by_app)
        self.functions = FakeFunctions(
            functions_by_app or {},
            function_definitions or {},
        )


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
                    "function_id": "fn-1",
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
                        "event_processors": {"FUNCTION": {"config": {"function_name": "fn-two"}}},
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
        functions_by_app={
            "app-1": [
                {"_id": "fn-1", "name": "fn-one"},
                {"_id": "fn-2", "name": "fn-two"},
            ],
            "app-3": [],
        },
        function_definitions={
            "fn-1": {
                "_id": "fn-1",
                "name": "fn-one",
                "private": True,
                "source": "exports = function() { return 1; };",
                "last_modified": 123,
            },
            "fn-2": {
                "_id": "fn-2",
                "name": "fn-two",
                "private": False,
                "source": "exports = function() { return 2; };",
                "last_modified": 456,
            },
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
        files=3,
        triggers=3,
        apps=2,
        collections=(
            TriggerCollection(database="db", collection="coll", triggers=2),
            TriggerCollection(database="other_db", collection="other_coll", triggers=1),
        ),
    )
    insert = json.loads((tmp_path / "db.coll.on-insert.json").read_text(encoding="utf-8"))
    assert insert["database"] == "db"
    assert insert["collection"] == "coll"
    assert insert["appId"] == "app-1"
    assert insert["appName"] == "my-app"
    assert insert["trigger"]["name"] == "on-insert"
    assert insert["trigger"]["type"] == "DATABASE"
    assert "_id" not in insert["trigger"]
    assert insert["function"] == {
        "name": "fn-two",
        "private": False,
        "source": "exports = function() { return 2; };",
    }

    update = json.loads((tmp_path / "db.coll.on-update.json").read_text(encoding="utf-8"))
    assert update["trigger"]["name"] == "on-update"
    assert update["trigger"]["disabled"] is True
    assert update["function"] == {
        "name": "fn-one",
        "private": True,
        "source": "exports = function() { return 1; };",
    }

    other = json.loads(
        (tmp_path / "other_db.other_coll.other-trigger.json").read_text(encoding="utf-8")
    )
    assert other["trigger"]["name"] == "other-trigger"
    assert other["function"] is None

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
    assert client.functions.list_calls == [
        ("app-3", "project-1"),
        ("app-2", "project-1"),
        ("app-1", "project-1"),
    ]
    assert client.functions.get_calls == [
        ("app-1", "fn-1", "project-1"),
        ("app-1", "fn-2", "project-1"),
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

    assert (tmp_path / "db.a_b.on-insert.json").exists()


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
    assert (tmp_path / "db.coll.on-insert.json").exists()


def test_export_disambiguates_duplicate_trigger_names_with_app(
    tmp_path: Path,
) -> None:
    client = FakeClient(
        {
            None: [
                SimpleNamespace(id="app-1", name="app-one"),
                SimpleNamespace(id="app-2", name="app-two"),
            ]
        },
        {
            "app-1": [{"_id": "svc-1", "type": "mongodb-atlas", "name": "cluster0"}],
            "app-2": [{"_id": "svc-2", "type": "mongodb-atlas", "name": "cluster0"}],
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
            ],
            "app-2": [
                {
                    "_id": "trigger-2",
                    "name": "on-insert",
                    "type": "DATABASE",
                    "config": {
                        "service_id": "svc-2",
                        "database": "db",
                        "collection": "coll",
                    },
                }
            ],
        },
    )

    result = export_triggers(client, "project-1", "cluster0", "", tmp_path)

    assert result.files == 2
    assert result.triggers == 2
    assert result.collections == (TriggerCollection(database="db", collection="coll", triggers=2),)
    assert (tmp_path / "db.coll.on-insert.app-one.json").exists()
    assert (tmp_path / "db.coll.on-insert.app-two.json").exists()
