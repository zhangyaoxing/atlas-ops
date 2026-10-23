"""Load and validate MongoDB index definitions from JSON files.

One file describes the indexes of one collection::

    {
        "database": "exampleDB",
        "collection": "example",
        "indexes": [
            {
                "keys": {"field": 1},
                "options": {"name": "field_index"},
                "commitQuorum": 1
            }
        ]
    }

``keys`` is required for every index; ``options`` and ``commitQuorum`` are
optional. Validation is strict so typos in field names surface immediately.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sie_atlas_ops.errors import IndexDefinitionError

_TOP_LEVEL_FIELDS = {"database", "collection", "indexes"}
_INDEX_FIELDS = {"keys", "options", "commitQuorum"}


@dataclass(frozen=True, slots=True)
class IndexSpec:
    """One entry of a MongoDB ``createIndexes`` command."""

    keys: dict[str, int | str]
    options: dict[str, Any] | None = None
    commit_quorum: int | str | None = None


@dataclass(frozen=True, slots=True)
class CollectionIndexes:
    """Index definitions for a single database collection."""

    path: Path
    database: str
    collection: str
    indexes: tuple[IndexSpec, ...]


def load_index_definitions(directory: Path) -> list[CollectionIndexes]:
    """Read every ``*.json`` file directly inside *directory*.

    All validation errors are collected and reported together so a single run
    surfaces every broken file. Raises :class:`IndexDefinitionError` if no
    files are found or any file is invalid.
    """
    if not directory.is_dir():
        raise IndexDefinitionError(f"Not a directory: {directory}")

    files = sorted(path for path in directory.glob("*.json") if path.is_file())
    if not files:
        raise IndexDefinitionError(f"No .json files found in {directory}")

    definitions: list[CollectionIndexes] = []
    errors: list[str] = []
    for path in files:
        try:
            definitions.append(_load_file(path))
        except IndexDefinitionError as exc:
            errors.append(str(exc))
    if errors:
        raise IndexDefinitionError("\n".join(errors))
    return definitions


def _load_file(path: Path) -> CollectionIndexes:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise IndexDefinitionError(f"{path}: cannot read file: {exc.strerror or exc}") from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IndexDefinitionError(
            f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc

    if not isinstance(document, dict):
        raise IndexDefinitionError(f"{path}: top-level value must be a JSON object")

    errors: list[str] = []
    _reject_unknown_fields(document, _TOP_LEVEL_FIELDS, str(path), errors)
    database = _read_name(document, "database", str(path), errors)
    collection = _read_name(document, "collection", str(path), errors)

    indexes: list[IndexSpec] = []
    raw_indexes = document.get("indexes")
    if not isinstance(raw_indexes, list) or not raw_indexes:
        errors.append(f"{path}: 'indexes' must be a non-empty array")
    else:
        for position, raw_index in enumerate(raw_indexes):
            spec = _parse_index(raw_index, f"{path}: indexes[{position}]", errors)
            if spec is not None:
                indexes.append(spec)

    if errors:
        raise IndexDefinitionError("\n".join(errors))
    assert database is not None and collection is not None  # guaranteed when errors is empty
    return CollectionIndexes(
        path=path,
        database=database,
        collection=collection,
        indexes=tuple(indexes),
    )


def _parse_index(raw: Any, where: str, errors: list[str]) -> IndexSpec | None:
    error_count = len(errors)
    if not isinstance(raw, dict):
        errors.append(f"{where} must be a JSON object")
        return None

    _reject_unknown_fields(raw, _INDEX_FIELDS, where, errors)
    keys = _parse_keys(raw.get("keys"), f"{where}.keys", errors)

    options = raw.get("options")
    if options is not None and not isinstance(options, dict):
        errors.append(f"{where}.options must be a JSON object when present")
        options = None

    commit_quorum: int | str | None = raw.get("commitQuorum")
    if commit_quorum is not None:
        if isinstance(commit_quorum, bool) or not isinstance(commit_quorum, (int, str)):
            errors.append(f"{where}.commitQuorum must be an integer or a string when present")
            commit_quorum = None
        elif isinstance(commit_quorum, str) and not commit_quorum:
            errors.append(f"{where}.commitQuorum must not be an empty string")
            commit_quorum = None

    if len(errors) > error_count or keys is None:
        return None
    return IndexSpec(keys=keys, options=options, commit_quorum=commit_quorum)


def _parse_keys(raw: Any, where: str, errors: list[str]) -> dict[str, int | str] | None:
    if not isinstance(raw, dict) or not raw:
        errors.append(f"{where} must be a non-empty object")
        return None

    keys: dict[str, int | str] = {}
    for field, direction in raw.items():
        if not isinstance(field, str) or not field:
            errors.append(f"{where} contains an invalid field name")
            continue
        if isinstance(direction, bool) or not isinstance(direction, (int, str)):
            errors.append(f"{where}.{field} must be 1, -1, or a string index type")
            continue
        if isinstance(direction, str) and not direction:
            errors.append(f"{where}.{field} must not be an empty string")
            continue
        keys[field] = direction
    return keys or None


def _read_name(document: dict[str, Any], field: str, where: str, errors: list[str]) -> str | None:
    value = document.get(field)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{where}: '{field}' must be a non-empty string")
        return None
    if "\x00" in value:
        errors.append(f"{where}: '{field}' must not contain NUL characters")
        return None
    return value


def _reject_unknown_fields(
    document: dict[str, Any], allowed: set[str], where: str, errors: list[str]
) -> None:
    unknown = sorted(set(document) - allowed)
    if unknown:
        errors.append(f"{where}: unknown field(s): {', '.join(unknown)}")
