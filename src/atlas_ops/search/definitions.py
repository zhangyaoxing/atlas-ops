"""Load and validate Atlas Search index definitions from JSON files."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from atlas_ops.errors import SearchDefinitionError

_TOP_LEVEL_FIELDS = {"database", "collection", "indexes"}
_INDEX_FIELDS = {"name", "type", "definition"}
_INDEX_TYPES = {"search", "vectorSearch"}


@dataclass(frozen=True, slots=True)
class SearchIndexSpec:
    """One Atlas Search index definition."""

    name: str
    type: str
    definition: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SearchCollectionIndexes:
    """Search index definitions for one database collection."""

    path: Path
    database: str
    collection: str
    indexes: tuple[SearchIndexSpec, ...]


def load_search_index_definitions(directory: Path) -> list[SearchCollectionIndexes]:
    """Read every ``*.json`` file directly inside *directory*."""
    if not directory.is_dir():
        raise SearchDefinitionError(f"Not a directory: {directory}")

    files = sorted(path for path in directory.glob("*.json") if path.is_file())
    if not files:
        raise SearchDefinitionError(f"No .json files found in {directory}")

    definitions: list[SearchCollectionIndexes] = []
    errors: list[str] = []
    namespaces: set[tuple[str, str]] = set()
    for path in files:
        try:
            definition = _load_file(path)
        except SearchDefinitionError as exc:
            errors.append(str(exc))
            continue
        namespace = (definition.database, definition.collection)
        if namespace in namespaces:
            errors.append(f"{path}: duplicate namespace {namespace[0]}.{namespace[1]}")
            continue
        namespaces.add(namespace)
        definitions.append(definition)

    if errors:
        raise SearchDefinitionError("\n".join(errors))
    return definitions


def _load_file(path: Path) -> SearchCollectionIndexes:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SearchDefinitionError(f"{path}: cannot read file: {exc.strerror or exc}") from exc
    except json.JSONDecodeError as exc:
        raise SearchDefinitionError(
            f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc

    if not isinstance(document, dict):
        raise SearchDefinitionError(f"{path}: top-level value must be a JSON object")

    errors: list[str] = []
    _reject_unknown(document, _TOP_LEVEL_FIELDS, str(path), errors)
    database = _read_name(document, "database", str(path), errors)
    collection = _read_name(document, "collection", str(path), errors)

    indexes: list[SearchIndexSpec] = []
    raw_indexes = document.get("indexes")
    if not isinstance(raw_indexes, list) or not raw_indexes:
        errors.append(f"{path}: 'indexes' must be a non-empty array")
    else:
        names: set[str] = set()
        for position, raw in enumerate(raw_indexes):
            spec = _parse_index(raw, f"{path}: indexes[{position}]", errors)
            if spec is None:
                continue
            if spec.name in names:
                errors.append(f"{path}: duplicate index name {spec.name!r}")
                continue
            names.add(spec.name)
            indexes.append(spec)

    if errors:
        raise SearchDefinitionError("\n".join(errors))
    assert database is not None and collection is not None
    return SearchCollectionIndexes(
        path=path,
        database=database,
        collection=collection,
        indexes=tuple(indexes),
    )


def _parse_index(raw: Any, where: str, errors: list[str]) -> SearchIndexSpec | None:
    if not isinstance(raw, dict):
        errors.append(f"{where} must be a JSON object")
        return None

    _reject_unknown(raw, _INDEX_FIELDS, where, errors)
    name = raw.get("name")
    if not isinstance(name, str) or not name or "\x00" in name:
        errors.append(f"{where}.name must be a non-empty string")
        return None

    index_type = raw.get("type", "search")
    if index_type not in _INDEX_TYPES:
        errors.append(f"{where}.type must be one of: {', '.join(sorted(_INDEX_TYPES))}")
        return None

    definition = raw.get("definition")
    if not isinstance(definition, dict):
        errors.append(f"{where}.definition must be a JSON object")
        return None

    return SearchIndexSpec(name=name, type=index_type, definition=definition)


def _read_name(document: dict[str, Any], field: str, where: str, errors: list[str]) -> str | None:
    value = document.get(field)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{where}: '{field}' must be a non-empty string")
        return None
    if "\x00" in value:
        errors.append(f"{where}: '{field}' must not contain NUL characters")
        return None
    return value


def _reject_unknown(
    document: dict[str, Any], allowed: set[str], where: str, errors: list[str]
) -> None:
    unknown = sorted(set(document) - allowed)
    if unknown:
        errors.append(f"{where}: unknown field(s): {', '.join(unknown)}")
