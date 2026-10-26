"""Load local trigger definition files."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from atlas_ops.errors import TriggerDefinitionError

_TOP_LEVEL_FIELDS = {"database", "collection", "trigger", "function"}
_TRIGGER_TYPES = {"DATABASE", "SCHEDULED"}


@dataclass(frozen=True, slots=True)
class TriggerSpec:
    """One local trigger and its function."""

    path: Path
    name: str
    type: str
    database: str | None
    collection: str | None
    trigger: dict[str, Any]
    function: dict[str, Any]


def load_trigger_definitions(directory: Path) -> list[TriggerSpec]:
    """Read every ``*.json`` trigger file directly inside *directory*."""
    if not directory.is_dir():
        raise TriggerDefinitionError(f"Not a directory: {directory}")

    files = sorted(path for path in directory.glob("*.json") if path.is_file())
    if not files:
        raise TriggerDefinitionError(f"No .json files found in {directory}")

    specs: list[TriggerSpec] = []
    errors: list[str] = []
    names: set[str] = set()
    for path in files:
        try:
            spec = _load_file(path)
        except TriggerDefinitionError as exc:
            errors.append(str(exc))
            continue
        if spec.name in names:
            errors.append(f"{path}: duplicate trigger name {spec.name!r}")
            continue
        names.add(spec.name)
        specs.append(spec)

    if errors:
        raise TriggerDefinitionError("\n".join(errors))
    return specs


def _load_file(path: Path) -> TriggerSpec:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TriggerDefinitionError(f"{path}: cannot read file: {exc.strerror or exc}") from exc
    except json.JSONDecodeError as exc:
        raise TriggerDefinitionError(
            f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc

    if not isinstance(document, dict):
        raise TriggerDefinitionError(f"{path}: top-level value must be a JSON object")

    errors: list[str] = []
    unknown = sorted(set(document) - _TOP_LEVEL_FIELDS)
    if unknown:
        errors.append(f"{path}: unknown field(s): {', '.join(unknown)}")

    trigger = document.get("trigger")
    if not isinstance(trigger, dict):
        errors.append(f"{path}: 'trigger' must be a JSON object")
        trigger = {}
    name = trigger.get("name")
    if not isinstance(name, str) or not name or "\x00" in name:
        errors.append(f"{path}: trigger.name must be a non-empty string")
        name = ""
    trigger_type = trigger.get("type")
    if not isinstance(trigger_type, str) or trigger_type.upper() not in _TRIGGER_TYPES:
        errors.append(f"{path}: trigger.type must be DATABASE or SCHEDULED")
        trigger_type = ""
    trigger_type = trigger_type.upper()
    config = trigger.get("config")
    if not isinstance(config, dict):
        errors.append(f"{path}: trigger.config must be a JSON object")
        config = {}

    database: str | None = None
    collection: str | None = None
    if trigger_type == "DATABASE":
        raw_database = document.get("database")
        raw_collection = document.get("collection")
        if (
            not isinstance(raw_database, str)
            or not raw_database
            or not isinstance(raw_collection, str)
            or not raw_collection
        ):
            errors.append(f"{path}: DATABASE triggers require non-empty database and collection")
        else:
            database = raw_database
            collection = raw_collection
    elif "database" in document or "collection" in document:
        errors.append(f"{path}: SCHEDULED triggers must not define database or collection")

    function = document.get("function")
    if not isinstance(function, dict):
        errors.append(f"{path}: 'function' must be a JSON object")
        function = {}
    source = function.get("source")
    if not isinstance(source, str) or not source:
        errors.append(f"{path}: function.source must be a non-empty string")
    private = function.get("private")
    if private is not None and not isinstance(private, bool):
        errors.append(f"{path}: function.private must be a boolean")

    if errors:
        raise TriggerDefinitionError("\n".join(errors))

    local_trigger = dict(trigger)
    local_trigger["name"] = name
    local_trigger["type"] = trigger_type
    local_trigger["config"] = config
    local_function = dict(function)
    local_function.setdefault("private", True)
    return TriggerSpec(
        path=path,
        name=name,
        type=trigger_type,
        database=database,
        collection=collection,
        trigger=local_trigger,
        function=local_function,
    )
