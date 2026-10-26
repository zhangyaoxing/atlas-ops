"""Export Atlas Search index definitions from an Atlas cluster."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from atlas_ops.atlas import AtlasRequestClient
from atlas_ops.file_sync import remove_stale_json_files
from atlas_ops.filenames import json_filename
from atlas_ops.search.remote import fetch_search_indexes


@dataclass(frozen=True, slots=True)
class SearchCollection:
    """One collection written during a search-index export."""

    database: str
    collection: str
    indexes: int


@dataclass(frozen=True, slots=True)
class SearchExportResult:
    """Collection files and search indexes written during an export."""

    files: int
    indexes: int
    collections: tuple[SearchCollection, ...]
    removed: int = 0


def export_search_indexes(
    client: AtlasRequestClient,
    project_id: str,
    cluster_name: str,
    directory: Path,
) -> SearchExportResult:
    """Write one JSON file per collection that has Atlas Search indexes.

    The cluster-wide Atlas Search endpoint is used so no MongoDB connection or
    database credentials are required. Only fields needed to recreate or update
    an index are kept: ``name``, ``type``, and ``definition``. Server-managed
    fields such as ``indexID``, ``status``, ``queryable``, ``statusDetail``, and
    ``latestDefinitionVersion`` are omitted.
    """
    directory.mkdir(parents=True, exist_ok=True)

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in fetch_search_indexes(client, project_id, cluster_name):
        database = item.get("database")
        collection = item.get("collectionName") or item.get("collection")
        if (
            not isinstance(database, str)
            or not database
            or not isinstance(collection, str)
            or not collection
        ):
            continue
        definition = _exported_definition(item)
        if definition is None:
            continue
        grouped.setdefault((database, collection), []).append(definition)

    files = 0
    indexes = 0
    exported: list[SearchCollection] = []
    written: set[str] = set()
    for database, collection in sorted(grouped):
        definitions = grouped[(database, collection)]
        definitions.sort(key=lambda item: str(item.get("name") or ""))
        if not definitions:
            continue

        document = {
            "database": database,
            "collection": collection,
            "indexes": definitions,
        }
        file_path = directory / json_filename(database, collection)
        file_path.write_text(
            json.dumps(document, indent=2, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )
        files += 1
        indexes += len(definitions)
        written.add(file_path.name)
        exported.append(
            SearchCollection(
                database=database,
                collection=collection,
                indexes=len(definitions),
            )
        )

    removed = remove_stale_json_files(directory, written)
    return SearchExportResult(
        files=files,
        indexes=indexes,
        collections=tuple(exported),
        removed=len(removed),
    )


def _exported_definition(item: dict[str, Any]) -> dict[str, Any] | None:
    name = item.get("name")
    if not isinstance(name, str) or not name:
        return None

    definition = item.get("latestDefinition")
    if not isinstance(definition, dict):
        definition = item.get("definition")
    if not isinstance(definition, dict):
        definition = {}

    index_type = item.get("type")
    if not isinstance(index_type, str) or not index_type:
        index_type = "search"

    return {
        "name": name,
        "type": index_type,
        "definition": definition,
    }
