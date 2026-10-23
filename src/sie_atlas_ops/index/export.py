"""Export existing index definitions from MongoDB to JSON files."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sie_atlas_ops.filenames import json_filename
from sie_atlas_ops.index.mongo import MongoReadClient

_SYSTEM_DATABASES = {"admin", "local", "config"}
_SERVER_FIELDS = {"v", "key", "ns"}


@dataclass(frozen=True, slots=True)
class ExportedCollection:
    """One collection written during an export."""

    database: str
    collection: str
    indexes: int


@dataclass(frozen=True, slots=True)
class ExportResult:
    """Collection files and indexes written during an export."""

    files: int
    indexes: int
    collections: tuple[ExportedCollection, ...]


def export_indexes(client: MongoReadClient, directory: Path) -> ExportResult:
    """Write one definition file per collection that has user indexes.

    System databases (``admin``, ``local``, ``config``), ``system.*``
    collections, and views are skipped. The ``_id_`` index is not exported
    because it cannot be recreated. Files are named ``<database>.<collection>.json``.
    """
    directory.mkdir(parents=True, exist_ok=True)
    files = 0
    indexes = 0
    exported: list[ExportedCollection] = []

    for database in sorted(client.list_database_names()):
        if database in _SYSTEM_DATABASES:
            continue
        database_handle = client[database]
        collections = sorted(
            database_handle.list_collections(), key=lambda item: item.get("name", "")
        )
        for collection_info in collections:
            collection = collection_info.get("name")
            if not collection or collection.startswith("system."):
                continue
            if collection_info.get("type") == "view":
                continue

            definitions = _definitions(database_handle[collection].list_indexes())
            if not definitions:
                continue

            payload = {
                "database": database,
                "collection": collection,
                "indexes": definitions,
            }
            path = directory / json_filename(database, collection)
            path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
                encoding="utf-8",
            )
            files += 1
            indexes += len(definitions)
            exported.append(
                ExportedCollection(
                    database=database,
                    collection=collection,
                    indexes=len(definitions),
                )
            )

    return ExportResult(
        files=files,
        indexes=indexes,
        collections=tuple(exported),
    )


def _definitions(indexes: Any) -> list[dict[str, Any]]:
    definitions: list[dict[str, Any]] = []
    for index in indexes:
        name = index.get("name")
        key = index.get("key") or {}
        if name == "_id_" or list(key.items()) == [("_id", 1)]:
            continue
        options = {
            key_name: value for key_name, value in index.items() if key_name not in _SERVER_FIELDS
        }
        definitions.append({"keys": dict(key), "options": options})
    definitions.sort(key=lambda item: item["options"].get("name", ""))
    return definitions
