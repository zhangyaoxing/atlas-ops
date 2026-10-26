"""Synchronize local trigger definitions to the target Triggers app."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol
from urllib.parse import quote

from pyatlassdk import AtlasAppApiError

from atlas_ops.errors import TriggerSyncError
from atlas_ops.trigger.definitions import TriggerSpec
from atlas_ops.trigger.export import _matching_service_ids

logger = logging.getLogger(__name__)

TRIGGERS_APP_NAME = "Triggers"
_ENVIRONMENT_CONFIG_KEYS = {"service_id", "clusterName", "cluster_name"}


class TriggerAppClientLike(Protocol):
    """Subset of the App Services client used for trigger sync."""

    apps: Any
    functions: Any
    services: Any
    triggers: Any

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        response_model: Any = None,
        accept: str | None = None,
        content_type: str | None = None,
    ) -> Any: ...


class TriggerAction(str, Enum):
    """What to do with one trigger."""

    CREATE = "create"
    UPDATE = "update"
    IDENTICAL = "identical"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class TriggerPlanItem:
    """One trigger and its synchronization decision."""

    name: str
    trigger_type: str
    action: TriggerAction
    spec: TriggerSpec | None
    remote: dict[str, Any] | None
    remote_function: dict[str, Any] | None = None
    function_name: str | None = None
    update_trigger: bool = False
    update_function: bool = False


@dataclass(frozen=True, slots=True)
class TriggerPlan:
    """Trigger decisions and the target data source for new database triggers."""

    items: tuple[TriggerPlanItem, ...]
    target_service_id: str | None


@dataclass(frozen=True, slots=True)
class TriggerSyncResult:
    """Applied trigger counts."""

    created: int
    updated: int
    skipped: int
    deleted: int
    functions_deleted: int


def find_triggers_app(client: TriggerAppClientLike, project_id: str) -> Any:
    """Return the App Services app named ``Triggers``."""
    apps_by_id: dict[str, Any] = {}
    for product in (None, "atlas"):
        for app in client.apps.admin_list_applications(group_id=project_id, product=product):
            app_id = getattr(app, "id", None)
            if isinstance(app_id, str) and app_id and app_id not in apps_by_id:
                apps_by_id[app_id] = app
    matches = [
        app
        for app in apps_by_id.values()
        if str(getattr(app, "name", "") or "").lower() == TRIGGERS_APP_NAME.lower()
    ]
    if not matches:
        raise TriggerSyncError(f"No App Services app named {TRIGGERS_APP_NAME!r} found.")
    if len(matches) > 1:
        raise TriggerSyncError(f"Multiple App Services apps named {TRIGGERS_APP_NAME!r} found.")
    return matches[0]


def plan_trigger_sync(
    client: TriggerAppClientLike,
    app_id: str,
    project_id: str,
    definitions: list[TriggerSpec],
    cluster_name: str,
    cluster_id: str,
) -> TriggerPlan:
    """Compare local triggers with the target app by trigger name."""
    remote_triggers = [
        raw
        for trigger in client.triggers.admin_list_triggers(
            app_id=app_id,
            group_id=project_id,
        )
        if (raw := _as_dict(trigger))
    ]
    remote_by_name: dict[str, dict[str, Any]] = {}
    for remote in remote_triggers:
        name = remote.get("name")
        if isinstance(name, str) and name:
            if name in remote_by_name:
                raise TriggerSyncError(f"Duplicate trigger name {name!r} in target app.")
            remote_by_name[name] = remote

    function_metadata = _results(
        client.functions.admin_list_functions(app_id=app_id, group_id=project_id)
    )
    functions_by_name: dict[str, str] = {}
    used_function_names: set[str] = set()
    for metadata in function_metadata:
        name = metadata.get("name")
        function_id = metadata.get("_id")
        if isinstance(name, str) and name:
            used_function_names.add(name)
            if isinstance(function_id, str) and function_id:
                functions_by_name[name] = function_id
    function_cache: dict[str, dict[str, Any] | None] = {}

    def get_function(function_id: str) -> dict[str, Any] | None:
        if function_id not in function_cache:
            function_cache[function_id] = _as_dict(
                client.functions.admin_get_function(
                    app_id=app_id,
                    function_id=function_id,
                    group_id=project_id,
                )
            )
        return function_cache[function_id]

    items: list[TriggerPlanItem] = []
    target_service_id: str | None = None
    local_names: set[str] = set()
    for spec in definitions:
        local_names.add(spec.name)
        remote = remote_by_name.get(spec.name)
        if remote is None:
            item = TriggerPlanItem(
                name=spec.name,
                trigger_type=spec.type,
                action=TriggerAction.CREATE,
                spec=spec,
                remote=None,
                function_name=_unique_function_name(spec.name, used_function_names),
            )
            if item.function_name:
                used_function_names.add(item.function_name)
            items.append(item)
            continue

        remote_function = None
        function_id, _ = _function_reference(remote)
        if function_id:
            remote_function = get_function(function_id)
        trigger_matches = _trigger_matches(spec.trigger, remote)
        function_matches = _function_matches(spec.function, remote_function)
        items.append(
            TriggerPlanItem(
                name=spec.name,
                trigger_type=spec.type,
                action=(
                    TriggerAction.IDENTICAL
                    if trigger_matches and function_matches
                    else TriggerAction.UPDATE
                ),
                spec=spec,
                remote=remote,
                remote_function=remote_function,
                update_trigger=not trigger_matches,
                update_function=not function_matches,
            )
        )

    for name, remote in remote_by_name.items():
        if name in local_names:
            continue
        trigger_type = str(remote.get("type") or "")
        items.append(
            TriggerPlanItem(
                name=name,
                trigger_type=trigger_type,
                action=TriggerAction.DELETE,
                spec=None,
                remote=remote,
            )
        )

    if any(
        item.action is TriggerAction.CREATE and item.trigger_type == "DATABASE" for item in items
    ):
        service_ids = _matching_service_ids(
            client,
            app_id,
            project_id,
            cluster_name,
            cluster_id,
        )
        if not service_ids:
            raise TriggerSyncError(
                f"No App Services data source linked to cluster {cluster_name!r}."
            )
        target_service_id = sorted(service_ids)[0]

    return TriggerPlan(items=tuple(items), target_service_id=target_service_id)


def apply_trigger_sync(
    client: TriggerAppClientLike,
    app_id: str,
    project_id: str,
    plan: TriggerPlan,
    cluster_name: str,
) -> TriggerSyncResult:
    """Create, update, and delete triggers, including orphaned functions."""
    created = 0
    updated = 0
    skipped = 0
    deleted = 0
    deleted_function_ids: set[str] = set()

    for item in plan.items:
        if item.action is TriggerAction.IDENTICAL:
            skipped += 1
            continue
        if item.action is TriggerAction.CREATE:
            assert item.spec is not None
            function_id = _create_function(client, app_id, project_id, item)
            try:
                client.triggers.admin_create_trigger(
                    app_id=app_id,
                    group_id=project_id,
                    body=_new_trigger_body(
                        item,
                        function_id,
                        plan.target_service_id,
                        cluster_name,
                    ),
                )
            except Exception:
                _delete_function(client, app_id, project_id, function_id)
                raise
            created += 1
            continue
        if item.action is TriggerAction.UPDATE:
            assert item.spec is not None
            assert item.remote is not None
            if item.update_function:
                function_id, _ = _function_reference(item.remote)
                if not function_id:
                    raise TriggerSyncError(f"Trigger {item.name!r} has no associated function.")
                _update_function(
                    client,
                    app_id,
                    project_id,
                    function_id,
                    _updated_function_body(item.spec, item.remote_function),
                )
            if item.update_trigger:
                client.triggers.admin_update_trigger(
                    app_id=app_id,
                    group_id=project_id,
                    trigger_id=_trigger_id(item.remote),
                    body=_updated_trigger_body(item.spec, item.remote),
                )
            updated += 1
            continue

        assert item.remote is not None
        client.triggers.admin_delete_trigger(
            app_id=app_id,
            group_id=project_id,
            trigger_id=_trigger_id(item.remote),
        )
        deleted += 1
        function_id, _ = _function_reference(item.remote)
        if function_id:
            deleted_function_ids.add(function_id)

    functions_deleted = 0
    if deleted_function_ids:
        remaining = [
            raw
            for trigger in client.triggers.admin_list_triggers(
                app_id=app_id,
                group_id=project_id,
            )
            if (raw := _as_dict(trigger))
        ]
        referenced = {
            function_id for trigger in remaining if (function_id := _function_reference(trigger)[0])
        }
        for function_id in sorted(deleted_function_ids - referenced):
            if _delete_function(client, app_id, project_id, function_id):
                functions_deleted += 1
            else:
                logger.warning(
                    "Function [cyan]%s[/cyan] was left in place because it could not be deleted.",
                    function_id,
                )

    return TriggerSyncResult(
        created=created,
        updated=updated,
        skipped=skipped,
        deleted=deleted,
        functions_deleted=functions_deleted,
    )


def _create_function(
    client: TriggerAppClientLike,
    app_id: str,
    project_id: str,
    item: TriggerPlanItem,
) -> str:
    assert item.spec is not None
    body = dict(item.spec.function)
    body["name"] = item.function_name
    body.setdefault("private", True)
    created = client.functions.admin_create_function(
        app_id=app_id,
        group_id=project_id,
        body=body,
    )
    data = _as_dict(created)
    function_id = data.get("_id") or data.get("id")
    if not isinstance(function_id, str) or not function_id:
        raise TriggerSyncError(f"Could not read the created function ID for {item.name!r}.")
    return function_id


def _update_function(
    client: TriggerAppClientLike,
    app_id: str,
    project_id: str,
    function_id: str,
    body: dict[str, Any],
) -> None:
    # The generated SDK method omits the request body, so issue the PUT directly.
    path = (
        f"/groups/{quote(project_id, safe='')}"
        f"/apps/{quote(app_id, safe='')}"
        f"/functions/{quote(function_id, safe='')}"
    )
    client.request(
        method="PUT",
        path=path,
        json=body,
        response_model=None,
        content_type="application/json",
    )


def _delete_function(
    client: TriggerAppClientLike,
    app_id: str,
    project_id: str,
    function_id: str,
) -> bool:
    try:
        client.functions.admin_delete_function(
            app_id=app_id,
            function_id=function_id,
            group_id=project_id,
        )
        return True
    except AtlasAppApiError:
        return False


def _new_trigger_body(
    item: TriggerPlanItem,
    function_id: str,
    service_id: str | None,
    cluster_name: str,
) -> dict[str, Any]:
    assert item.spec is not None
    body = dict(item.spec.trigger)
    body.pop("function_id", None)
    body.pop("function_name", None)
    body.pop("event_processors", None)
    config = dict(body.get("config") or {})
    config.pop("service_id", None)
    config.pop("clusterName", None)
    config.pop("cluster_name", None)
    if item.trigger_type == "DATABASE":
        if not service_id:
            raise TriggerSyncError(f"No target data source available for trigger {item.name!r}.")
        config["service_id"] = service_id
        config["clusterName"] = cluster_name
    body["config"] = config
    body["function_id"] = function_id
    return body


def _updated_trigger_body(
    spec: TriggerSpec,
    remote: dict[str, Any],
) -> dict[str, Any]:
    body = dict(remote)
    body.pop("_id", None)
    body.pop("last_modified", None)
    local = spec.trigger
    body["name"] = spec.name
    body["type"] = spec.type
    if "disabled" in local:
        body["disabled"] = local["disabled"]
    config = dict(body.get("config") or {})
    for key, value in (local.get("config") or {}).items():
        if key not in _ENVIRONMENT_CONFIG_KEYS:
            config[key] = value
    body["config"] = config
    return body


def _updated_function_body(
    spec: TriggerSpec,
    remote_function: dict[str, Any] | None,
) -> dict[str, Any]:
    if remote_function is None:
        raise TriggerSyncError(f"Trigger {spec.name!r} has no associated function.")
    body = dict(remote_function)
    body.pop("_id", None)
    body.pop("last_modified", None)
    for key, value in spec.function.items():
        if key not in {"_id", "name", "last_modified", "run_as_user_id"}:
            body[key] = value
    if not body.get("name"):
        raise TriggerSyncError(f"Function for trigger {spec.name!r} has no name.")
    return body


def _trigger_matches(local: dict[str, Any], remote: dict[str, Any]) -> bool:
    if str(local.get("name") or "") != str(remote.get("name") or ""):
        return False
    if str(local.get("type") or "").upper() != str(remote.get("type") or "").upper():
        return False
    if bool(local.get("disabled", False)) != bool(remote.get("disabled", False)):
        return False
    remote_config = remote.get("config")
    remote_config = remote_config if isinstance(remote_config, dict) else {}
    for key, value in (local.get("config") or {}).items():
        if key in _ENVIRONMENT_CONFIG_KEYS:
            continue
        if not _same(remote_config.get(key), value):
            return False
    return True


def _function_matches(
    local: dict[str, Any],
    remote: dict[str, Any] | None,
) -> bool:
    if remote is None:
        return False
    for key, value in local.items():
        if key in {"_id", "name", "last_modified", "run_as_user_id"}:
            continue
        if not _same(remote.get(key), value):
            return False
    return True


def _same(left: Any, right: Any) -> bool:
    return _canonical(left) == _canonical(right)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _function_reference(trigger: dict[str, Any]) -> tuple[str | None, str | None]:
    function_id = trigger.get("function_id")
    function_name = trigger.get("function_name")
    processors = trigger.get("event_processors")
    if isinstance(processors, dict):
        processor = processors.get("FUNCTION")
        if isinstance(processor, dict):
            config = processor.get("config")
            if isinstance(config, dict):
                if not isinstance(function_id, str) or not function_id:
                    function_id = config.get("function_id")
                if not isinstance(function_name, str) or not function_name:
                    function_name = config.get("function_name")
    valid_id = function_id if isinstance(function_id, str) and function_id else None
    valid_name = function_name if isinstance(function_name, str) and function_name else None
    return valid_id, valid_name


def _trigger_id(remote: dict[str, Any]) -> str:
    trigger_id = remote.get("_id") or remote.get("id")
    if not isinstance(trigger_id, str) or not trigger_id:
        raise TriggerSyncError(f"Trigger {remote.get('name')!r} has no ID.")
    return trigger_id


def _unique_function_name(trigger_name: str, used: set[str]) -> str:
    base = f"Atlas_Triggers_{trigger_name}"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(by_alias=True, exclude_none=True, mode="json")
    return {}


def _results(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, list):
            return [item for item in results if isinstance(item, dict)]
    return []
