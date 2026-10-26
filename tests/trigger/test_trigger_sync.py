from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from atlas_ops.errors import TriggerDefinitionError
from atlas_ops.trigger.definitions import (
    TriggerSpec,
    load_trigger_definitions,
)
from atlas_ops.trigger.sync import (
    TriggerAction,
    apply_trigger_sync,
    plan_trigger_sync,
)


class FakeApps:
    def __init__(self, apps: list[Any]) -> None:
        self._apps = apps

    def admin_list_applications(self, group_id: str, *, product: str | None = None) -> list[Any]:
        return self._apps if product == "atlas" else self._apps


class FakeServices:
    def __init__(self, services: list[Any]) -> None:
        self._services = services

    def admin_list_services(self, app_id: str, group_id: str) -> list[Any]:
        return self._services


class FakeTriggers:
    def __init__(self, triggers: list[Any]) -> None:
        self.records = [dict(item) for item in triggers]
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.deleted: list[dict[str, Any]] = []

    def admin_list_triggers(self, app_id: str, group_id: str) -> list[Any]:
        return self.records

    def admin_create_trigger(self, **kwargs: Any) -> Any:
        self.created.append(kwargs["body"])
        return SimpleNamespace(id="created-trigger")

    def admin_update_trigger(self, **kwargs: Any) -> None:
        self.updated.append(kwargs)

    def admin_delete_trigger(self, **kwargs: Any) -> None:
        self.deleted.append(kwargs)
        trigger_id = kwargs["trigger_id"]
        self.records = [item for item in self.records if item.get("_id") != trigger_id]


class FakeFunctions:
    def __init__(self, functions: list[dict[str, Any]]) -> None:
        self.records = [dict(item) for item in functions]
        self.created: list[dict[str, Any]] = []
        self.deleted: list[dict[str, Any]] = []

    def admin_list_functions(self, app_id: str, group_id: str) -> list[Any]:
        return [{"_id": item["_id"], "name": item["name"]} for item in self.records]

    def admin_get_function(self, app_id: str, function_id: str, group_id: str) -> Any:
        return next(item for item in self.records if item["_id"] == function_id)

    def admin_create_function(self, **kwargs: Any) -> Any:
        body = kwargs["body"]
        self.created.append(body)
        return {"_id": "fn-created", "name": body["name"]}

    def admin_delete_function(self, **kwargs: Any) -> None:
        self.deleted.append(kwargs)
        function_id = kwargs["function_id"]
        self.records = [item for item in self.records if item["_id"] != function_id]


class FakeClient:
    def __init__(
        self,
        *,
        triggers: list[Any] | None = None,
        functions: list[dict[str, Any]] | None = None,
        services: list[Any] | None = None,
    ) -> None:
        self.apps = FakeApps([SimpleNamespace(id="app-1", name="Triggers")])
        self.services = FakeServices(services or [])
        self.triggers = FakeTriggers(triggers or [])
        self.functions = FakeFunctions(functions or [])
        self.requests: list[dict[str, Any]] = []

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.requests.append({"method": method, "path": path, **kwargs})
        return None


def _spec(
    name: str = "on-insert",
    *,
    database: str = "db",
    collection: str = "coll",
    trigger_type: str = "DATABASE",
    trigger_config: dict[str, Any] | None = None,
    function_source: str = "exports = function() {};",
) -> TriggerSpec:
    config = trigger_config or {
        "database": database,
        "collection": collection,
        "operation_types": ["INSERT"],
        "full_document": False,
    }
    return TriggerSpec(
        path=Path(f"{name}.json"),
        name=name,
        type=trigger_type,
        database=database if trigger_type == "DATABASE" else None,
        collection=collection if trigger_type == "DATABASE" else None,
        trigger={
            "name": name,
            "type": trigger_type,
            "disabled": False,
            "config": config,
        },
        function={
            "private": True,
            "source": function_source,
        },
    )


def _remote_trigger(
    name: str = "on-insert",
    *,
    function_id: str = "fn-1",
    config: dict[str, Any] | None = None,
    trigger_type: str = "DATABASE",
) -> dict[str, Any]:
    return {
        "_id": f"trigger-{name}",
        "name": name,
        "type": trigger_type,
        "function_id": function_id,
        "function_name": "target-function",
        "disabled": False,
        "config": config
        or {
            "service_id": "svc-target",
            "clusterName": "target-cluster",
            "database": "db",
            "collection": "coll",
            "operation_types": ["INSERT"],
            "full_document": False,
        },
        "event_processors": {
            "FUNCTION": {
                "config": {
                    "function_id": function_id,
                    "function_name": "target-function",
                }
            }
        },
    }


def _remote_function(source: str = "old source") -> dict[str, Any]:
    return {
        "_id": "fn-1",
        "name": "target-function",
        "private": True,
        "source": source,
        "last_modified": 1,
    }


def test_load_trigger_definitions(tmp_path: Path) -> None:
    (tmp_path / "on-insert.json").write_text(
        json.dumps(
            {
                "database": "db",
                "collection": "coll",
                "trigger": {
                    "name": "on-insert",
                    "type": "DATABASE",
                    "config": {"database": "db", "collection": "coll"},
                },
                "function": {"private": True, "source": "exports = function() {};"},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "nightly.json").write_text(
        json.dumps(
            {
                "trigger": {
                    "name": "nightly",
                    "type": "SCHEDULED",
                    "config": {"schedule": "0 0 * * *"},
                },
                "function": {"private": True, "source": "exports = function() {};"},
            }
        ),
        encoding="utf-8",
    )

    specs = load_trigger_definitions(tmp_path)

    assert [spec.name for spec in specs] == ["nightly", "on-insert"]
    assert specs[0].type == "SCHEDULED"
    assert specs[0].database is None
    assert specs[1].type == "DATABASE"


def test_load_trigger_definitions_rejects_duplicate_names(tmp_path: Path) -> None:
    payload = {
        "database": "db",
        "collection": "coll",
        "trigger": {
            "name": "same",
            "type": "DATABASE",
            "config": {"database": "db", "collection": "coll"},
        },
        "function": {"private": True, "source": "exports = function() {};"},
    }
    (tmp_path / "one.json").write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "two.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TriggerDefinitionError, match="duplicate trigger name"):
        load_trigger_definitions(tmp_path)


def test_plan_creates_database_trigger_with_target_service() -> None:
    client = FakeClient(
        services=[{"_id": "svc-target", "name": "target-cluster", "type": "mongodb-atlas"}],
    )

    plan = plan_trigger_sync(
        client,
        "app-1",
        "project-1",
        [_spec()],
        "target-cluster",
        "cluster-id",
    )

    assert plan.items[0].action is TriggerAction.CREATE
    assert plan.items[0].function_name == "Atlas_Triggers_on-insert"
    assert plan.target_service_id == "svc-target"


def test_apply_creates_function_then_trigger() -> None:
    client = FakeClient(
        services=[{"_id": "svc-target", "name": "target-cluster", "type": "mongodb-atlas"}],
    )
    plan = plan_trigger_sync(
        client,
        "app-1",
        "project-1",
        [_spec()],
        "target-cluster",
        "cluster-id",
    )

    result = apply_trigger_sync(
        client,
        "app-1",
        "project-1",
        plan,
        "target-cluster",
    )

    assert result.created == 1
    assert client.functions.created[0]["name"] == "Atlas_Triggers_on-insert"
    body = client.triggers.created[0]
    assert body["function_id"] == "fn-created"
    assert body["config"]["service_id"] == "svc-target"
    assert body["config"]["clusterName"] == "target-cluster"
    assert "function_id" not in body["config"]


def test_plan_and_apply_update_uses_remote_template() -> None:
    client = FakeClient(
        triggers=[
            _remote_trigger(
                config={
                    "service_id": "svc-target",
                    "clusterName": "target-cluster",
                    "database": "db",
                    "collection": "coll",
                    "operation_types": ["INSERT"],
                    "full_document": False,
                }
            )
        ],
        functions=[_remote_function()],
    )
    spec = _spec(
        trigger_config={
            "database": "db",
            "collection": "coll",
            "operation_types": ["INSERT"],
            "full_document": True,
        },
        function_source="new source",
    )

    plan = plan_trigger_sync(
        client,
        "app-1",
        "project-1",
        [spec],
        "target-cluster",
        "cluster-id",
    )

    assert plan.items[0].action is TriggerAction.UPDATE
    assert plan.items[0].update_trigger is True
    assert plan.items[0].update_function is True

    result = apply_trigger_sync(
        client,
        "app-1",
        "project-1",
        plan,
        "target-cluster",
    )

    assert result.updated == 1
    function_request = client.requests[0]
    assert function_request["method"] == "PUT"
    assert function_request["json"]["name"] == "target-function"
    assert function_request["json"]["source"] == "new source"
    trigger_body = client.triggers.updated[0]["body"]
    assert trigger_body["function_id"] == "fn-1"
    assert trigger_body["config"]["service_id"] == "svc-target"
    assert trigger_body["config"]["full_document"] is True


def test_apply_deletes_trigger_and_orphaned_function() -> None:
    client = FakeClient(
        triggers=[_remote_trigger("old")],
        functions=[_remote_function()],
    )
    plan = plan_trigger_sync(
        client,
        "app-1",
        "project-1",
        [],
        "target-cluster",
        "cluster-id",
    )

    assert plan.items[0].action is TriggerAction.DELETE
    result = apply_trigger_sync(
        client,
        "app-1",
        "project-1",
        plan,
        "target-cluster",
    )

    assert result.deleted == 1
    assert result.functions_deleted == 1
    assert client.functions.deleted[0]["function_id"] == "fn-1"


def test_apply_keeps_function_shared_with_remaining_trigger() -> None:
    keep = _remote_trigger("keep")
    old = _remote_trigger("old")
    client = FakeClient(
        triggers=[keep, old],
        functions=[_remote_function()],
    )
    plan = plan_trigger_sync(
        client,
        "app-1",
        "project-1",
        [
            _spec(
                "keep",
                trigger_config=keep["config"],
                function_source="old source",
            )
        ],
        "target-cluster",
        "cluster-id",
    )

    result = apply_trigger_sync(
        client,
        "app-1",
        "project-1",
        plan,
        "target-cluster",
    )

    assert result.deleted == 1
    assert result.functions_deleted == 0
    assert client.functions.deleted == []


def test_scheduled_create_needs_no_service() -> None:
    client = FakeClient()
    spec = _spec(
        "nightly",
        trigger_type="SCHEDULED",
        trigger_config={"schedule": "0 0 * * *"},
    )
    plan = plan_trigger_sync(
        client,
        "app-1",
        "project-1",
        [spec],
        "target-cluster",
        "cluster-id",
    )

    apply_trigger_sync(
        client,
        "app-1",
        "project-1",
        plan,
        "target-cluster",
    )

    body = client.triggers.created[0]
    assert body["type"] == "SCHEDULED"
    assert body["config"] == {"schedule": "0 0 * * *"}
    assert "service_id" not in body["config"]
