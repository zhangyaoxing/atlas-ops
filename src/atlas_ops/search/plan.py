"""Compare local Atlas Search definitions with remote indexes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from atlas_ops.search.definitions import SearchCollectionIndexes, SearchIndexSpec


class SearchAction(str, Enum):
    """What to do with one declared search index."""

    CREATE = "create"
    UPDATE = "update"
    IDENTICAL = "identical"


@dataclass(frozen=True, slots=True)
class SearchPlanItem:
    """One declared search index and its decision."""

    database: str
    collection: str
    spec: SearchIndexSpec
    action: SearchAction
    remote_id: str | None = None


@dataclass(frozen=True, slots=True)
class SearchExtra:
    """A remote search index not present in the local definitions."""

    database: str
    collection: str
    name: str
    index_id: str


@dataclass(frozen=True, slots=True)
class SearchPlan:
    """Declared decisions plus remote indexes that are not declared."""

    items: tuple[SearchPlanItem, ...]
    extras: tuple[SearchExtra, ...]


def plan_search_indexes(
    remote_indexes: list[dict[str, Any]],
    definitions: list[SearchCollectionIndexes],
) -> SearchPlan:
    """Classify local search indexes and collect undeclared remote indexes."""
    remote_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    remote_by_namespace: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for remote in remote_indexes:
        database = remote.get("database")
        collection = remote.get("collectionName") or remote.get("collection")
        name = remote.get("name")
        if (
            not isinstance(database, str)
            or not database
            or not isinstance(collection, str)
            or not collection
            or not isinstance(name, str)
            or not name
        ):
            continue
        remote_by_key[(database, collection, name)] = remote
        remote_by_namespace.setdefault((database, collection), []).append(remote)

    items: list[SearchPlanItem] = []
    extras: list[SearchExtra] = []
    for definition in definitions:
        namespace = (definition.database, definition.collection)
        local_names: set[str] = set()
        for spec in definition.indexes:
            local_names.add(spec.name)
            remote = remote_by_key.get((definition.database, definition.collection, spec.name))
            if remote is None:
                items.append(
                    SearchPlanItem(
                        database=definition.database,
                        collection=definition.collection,
                        spec=spec,
                        action=SearchAction.CREATE,
                    )
                )
                continue

            remote_id = remote.get("indexID")
            if not isinstance(remote_id, str) or not remote_id:
                remote_id = None
            remote_type = remote.get("type")
            if not isinstance(remote_type, str) or not remote_type:
                remote_type = "search"
            remote_definition = remote.get("latestDefinition")
            if not isinstance(remote_definition, dict):
                remote_definition = remote.get("definition")

            identical = (
                remote_type == spec.type
                and isinstance(remote_definition, dict)
                and _canonical(remote_definition) == _canonical(spec.definition)
            )
            items.append(
                SearchPlanItem(
                    database=definition.database,
                    collection=definition.collection,
                    spec=spec,
                    action=(SearchAction.IDENTICAL if identical else SearchAction.UPDATE),
                    remote_id=remote_id,
                )
            )

        for remote in remote_by_namespace.get(namespace, []):
            name = remote.get("name")
            index_id = remote.get("indexID")
            if (
                isinstance(name, str)
                and name
                and name not in local_names
                and isinstance(index_id, str)
                and index_id
            ):
                extras.append(
                    SearchExtra(
                        database=definition.database,
                        collection=definition.collection,
                        name=name,
                        index_id=index_id,
                    )
                )

    return SearchPlan(items=tuple(items), extras=tuple(extras))


def _canonical(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
