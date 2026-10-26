"""Export App Services trigger definitions for one Atlas cluster."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from atlas_ops.errors import TriggerExportError
from atlas_ops.filenames import json_filename


class AppServicesClientLike(Protocol):
    """Subset of the App Services client used for trigger export."""

    apps: Any
    functions: Any
    services: Any
    triggers: Any


@dataclass(frozen=True, slots=True)
class TriggerCollection:
    """One collection in a trigger export summary."""

    database: str
    collection: str
    triggers: int


@dataclass(frozen=True, slots=True)
class TriggerExportResult:
    """Trigger files, apps, collection counts, and scheduled count."""

    files: int
    triggers: int
    apps: int
    collections: tuple[TriggerCollection, ...]
    scheduled: int = 0


def export_triggers(
    client: AppServicesClientLike,
    project_id: str,
    cluster_name: str,
    cluster_id: str,
    directory: Path,
) -> TriggerExportResult:
    """Export database triggers related to *cluster_name* as one file each.

    Each App Services app is inspected for data sources linked to the target
    cluster. Database triggers on those data sources must name both a database
    and a collection; scheduled triggers are always included. Every trigger is
    written to ``<trigger>.json`` together with its associated function.
    Server-managed ``_id`` values are removed.
    """
    directory.mkdir(parents=True, exist_ok=True)

    entries: list[dict[str, Any]] = []
    apps_with_triggers: set[str] = set()

    for app in _list_apps(client, project_id):
        app_id = getattr(app, "id", None)
        if not isinstance(app_id, str) or not app_id:
            continue
        app_name = getattr(app, "name", None)
        if not isinstance(app_name, str) or not app_name:
            app_name = app_id

        service_ids = _matching_service_ids(
            client,
            app_id,
            project_id,
            cluster_name,
            cluster_id,
        )
        functions_by_name = {
            name: function_id
            for function in _results(
                client.functions.admin_list_functions(
                    app_id=app_id,
                    group_id=project_id,
                )
            )
            if isinstance((name := function.get("name")), str)
            and isinstance((function_id := function.get("_id")), str)
            and name
            and function_id
        }
        function_cache: dict[str, dict[str, Any] | None] = {}

        for trigger in client.triggers.admin_list_triggers(
            app_id=app_id,
            group_id=project_id,
        ):
            definition = _trigger_definition(trigger)
            if definition is None:
                continue
            trigger_type = str(definition.get("type") or "").upper()
            if trigger_type not in {"DATABASE", "SCHEDULED"}:
                continue

            database: str | None = None
            collection: str | None = None
            if trigger_type == "DATABASE":
                if not service_ids:
                    continue
                config = definition.get("config")
                if not isinstance(config, dict):
                    continue
                service_id = config.get("service_id")
                if not isinstance(service_id, str) or service_id not in service_ids:
                    continue
                raw_database = config.get("database")
                raw_collection = config.get("collection")
                if (
                    not isinstance(raw_database, str)
                    or not raw_database
                    or not isinstance(raw_collection, str)
                    or not raw_collection
                ):
                    continue
                database = raw_database
                collection = raw_collection

            associated_function = _associated_function(
                client,
                definition,
                app_id,
                project_id,
                functions_by_name,
                function_cache,
            )
            entries.append(
                {
                    "database": database,
                    "collection": collection,
                    "appId": app_id,
                    "appName": app_name,
                    "trigger": _sanitized_trigger(definition),
                    "function": _sanitized_function(associated_function),
                }
            )
            apps_with_triggers.add(app_id)

    collection_counts: dict[tuple[str, str], int] = {}
    scheduled = 0
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        database = entry["database"]
        collection = entry["collection"]
        trigger = entry["trigger"]
        name = trigger.get("name")
        if not isinstance(name, str) or not name:
            continue
        if database is not None and collection is not None:
            collection_counts[(database, collection)] = (
                collection_counts.get((database, collection), 0) + 1
            )
        else:
            scheduled += 1
        grouped.setdefault(name, []).append(entry)

    outputs: list[tuple[str, dict[str, Any]]] = []
    used_filenames: set[str] = set()
    for name in sorted(grouped):
        group = grouped[name]
        if len(group) > 1:
            raise TriggerExportError(f"Duplicate trigger name {name!r}; filenames must be unique.")
        entry = group[0]
        filename = json_filename(name)
        if filename in used_filenames:
            raise TriggerExportError(
                f"Trigger name {name!r} produces a duplicate filename {filename!r}."
            )
        used_filenames.add(filename)
        outputs.append((filename, entry))

    files = 0
    for filename, entry in outputs:
        document: dict[str, Any] = {}
        if entry["database"] is not None and entry["collection"] is not None:
            document["database"] = entry["database"]
            document["collection"] = entry["collection"]
        document["trigger"] = entry["trigger"]
        document["function"] = entry["function"]
        path = directory / filename
        path.write_text(
            json.dumps(document, indent=2, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )
        files += 1

    exported = [
        TriggerCollection(database=database, collection=collection, triggers=count)
        for (database, collection), count in sorted(collection_counts.items())
    ]
    return TriggerExportResult(
        files=files,
        triggers=files,
        apps=len(apps_with_triggers),
        collections=tuple(exported),
        scheduled=scheduled,
    )


def _list_apps(
    client: AppServicesClientLike,
    project_id: str,
) -> list[Any]:
    apps_by_id: dict[str, Any] = {}
    for product in (None, "atlas"):
        for app in client.apps.admin_list_applications(group_id=project_id, product=product):
            app_id = getattr(app, "id", None)
            if isinstance(app_id, str) and app_id and app_id not in apps_by_id:
                apps_by_id[app_id] = app
    return sorted(
        apps_by_id.values(),
        key=lambda app: (
            str(getattr(app, "name", "") or ""),
            str(getattr(app, "id", "") or ""),
        ),
    )


def _matching_service_ids(
    client: AppServicesClientLike,
    app_id: str,
    project_id: str,
    cluster_name: str,
    cluster_id: str,
) -> set[str]:
    services = _results(
        client.services.admin_list_services(
            app_id=app_id,
            group_id=project_id,
        )
    )
    matching: set[str] = set()
    for service in services:
        service_type = service.get("type")
        if isinstance(service_type, str) and service_type != "mongodb-atlas":
            continue

        raw_config = service.get("config")
        config = raw_config if isinstance(raw_config, dict) else {}
        configured_name = (
            config.get("clusterName") or config.get("cluster_name") or service.get("name")
        )
        configured_id = config.get("clusterId") or config.get("cluster_id")
        name_matches = (
            isinstance(configured_name, str) and configured_name.lower() == cluster_name.lower()
        )
        id_matches = (
            bool(cluster_id) and configured_id is not None and str(configured_id) == cluster_id
        )
        if not (name_matches or id_matches):
            continue

        service_id = service.get("_id")
        if isinstance(service_id, str) and service_id:
            matching.add(service_id)
    return matching


def _sanitized_trigger(trigger: dict[str, Any]) -> dict[str, Any]:
    """Remove references that differ between App Services environments."""
    raw = dict(trigger)
    raw.pop("function_id", None)
    raw.pop("function_name", None)
    raw.pop("event_processors", None)
    config = raw.get("config")
    if isinstance(config, dict):
        sanitized = dict(config)
        sanitized.pop("service_id", None)
        sanitized.pop("clusterName", None)
        sanitized.pop("cluster_name", None)
        raw["config"] = sanitized
    return raw


def _sanitized_function(function: dict[str, Any] | None) -> dict[str, Any] | None:
    """Remove identifiers that are specific to the source environment."""
    if function is None:
        return None
    raw = dict(function)
    raw.pop("name", None)
    raw.pop("run_as_user_id", None)
    return raw


def _associated_function(
    client: AppServicesClientLike,
    trigger: dict[str, Any],
    app_id: str,
    project_id: str,
    functions_by_name: dict[str, str],
    cache: dict[str, dict[str, Any] | None],
) -> dict[str, Any] | None:
    function_id, function_name = _function_reference(trigger)
    if not function_id and function_name:
        function_id = functions_by_name.get(function_name)
    if not function_id:
        return None
    if function_id not in cache:
        raw = client.functions.admin_get_function(
            app_id=app_id,
            function_id=function_id,
            group_id=project_id,
        )
        cache[function_id] = _function_definition(raw)
    return cache[function_id]


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


def _function_definition(function: Any) -> dict[str, Any] | None:
    if hasattr(function, "model_dump"):
        raw = function.model_dump(by_alias=True, exclude_none=True, mode="json")
    elif isinstance(function, dict):
        raw = dict(function)
    else:
        return None

    raw.pop("_id", None)
    raw.pop("last_modified", None)
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        return None
    return raw


def _trigger_definition(trigger: Any) -> dict[str, Any] | None:
    if hasattr(trigger, "model_dump"):
        raw = trigger.model_dump(by_alias=True, exclude_none=True, mode="json")
    elif isinstance(trigger, dict):
        raw = dict(trigger)
    else:
        return None

    raw.pop("_id", None)
    raw.pop("last_modified", None)
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        return None
    return raw


def _results(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, list):
            return [item for item in results if isinstance(item, dict)]
    return []
