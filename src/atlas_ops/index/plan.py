"""Decide which declared indexes exist and which existing indexes are extra."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from atlas_ops.index.definitions import CollectionIndexes, IndexSpec
from atlas_ops.index.mongo import MissingNamespace, MongoReadClient

_SYSTEM_DATABASES = {"admin", "local", "config"}


class IndexAction(str, Enum):
    """What the tool should do with one declared index."""

    CREATE = "create"
    IDENTICAL = "identical"
    DIFFERENT = "different"


@dataclass(frozen=True, slots=True)
class IndexPlanItem:
    """One declared index together with the decision about it."""

    database: str
    collection: str
    spec: IndexSpec
    action: IndexAction
    existing_name: str | None = None
    existing_options: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ExtraIndex:
    """An existing index whose key pattern is not declared."""

    database: str
    collection: str
    name: str
    keys: dict[str, Any]


@dataclass(frozen=True, slots=True)
class IndexPlan:
    """Declared-index decisions plus indexes that are not declared."""

    items: tuple[IndexPlanItem, ...]
    extras: tuple[ExtraIndex, ...]


def plan_indexes(
    client: MongoReadClient,
    definitions: list[CollectionIndexes],
    missing: list[MissingNamespace],
    *,
    include_unmanaged: bool = False,
) -> IndexPlan:
    """Classify declared indexes and collect existing indexes that are extra.

    Indexes are matched by their ordered key pattern only, never by name.
    ``options`` are compared after removing the server fields ``v`` and ``key``;
    ``name`` is ignored when the declaration does not specify one. The ``_id_``
    index is never reported as extra.
    """
    missing_namespaces = {(item.database, item.collection) for item in missing}
    specs_by_namespace: dict[tuple[str, str], list[IndexSpec]] = {}
    namespace_order: list[tuple[str, str]] = []

    for definition in definitions:
        key = (definition.database, definition.collection)
        if key not in specs_by_namespace:
            specs_by_namespace[key] = []
            namespace_order.append(key)
        specs_by_namespace[key].extend(definition.indexes)

    items: list[IndexPlanItem] = []
    extras: list[ExtraIndex] = []
    for database, collection in namespace_order:
        key = (database, collection)
        if key in missing_namespaces:
            existing: list[dict[str, Any]] = []
        else:
            existing = [dict(index) for index in client[database][collection].list_indexes()]

        specs = specs_by_namespace[key]
        for spec in specs:
            items.append(_decide(database, collection, spec, existing))

        declared = {tuple(spec.keys.items()) for spec in specs}
        for index in existing:
            name = str(index.get("name") or "")
            index_key = index.get("key") or {}
            pattern = tuple(index_key.items())
            if not name or name == "_id_" or pattern == (("_id", 1),) or pattern in declared:
                continue
            extras.append(
                ExtraIndex(
                    database=database,
                    collection=collection,
                    name=name,
                    keys=dict(index_key),
                )
            )

    if include_unmanaged:
        declared_namespaces = set(specs_by_namespace)
        for database in sorted(client.list_database_names()):
            if database in _SYSTEM_DATABASES:
                continue
            database_handle = client[database]
            collections = sorted(
                database_handle.list_collections(),
                key=lambda item: item.get("name", ""),
            )
            for collection_info in collections:
                collection = collection_info.get("name")
                if (
                    not collection
                    or collection.startswith("system.")
                    or collection_info.get("type") == "view"
                    or (database, collection) in declared_namespaces
                ):
                    continue
                for index in database_handle[collection].list_indexes():
                    name = str(index.get("name") or "")
                    index_key = index.get("key") or {}
                    pattern = tuple(index_key.items())
                    if not name or name == "_id_" or pattern == (("_id", 1),):
                        continue
                    extras.append(
                        ExtraIndex(
                            database=database,
                            collection=collection,
                            name=name,
                            keys=dict(index_key),
                        )
                    )

    return IndexPlan(items=tuple(items), extras=tuple(extras))


def _decide(
    database: str,
    collection: str,
    spec: IndexSpec,
    existing: list[dict[str, Any]],
) -> IndexPlanItem:
    candidates = [index for index in existing if _keys_match(spec.keys, index.get("key") or {})]
    if not candidates:
        return IndexPlanItem(
            database=database,
            collection=collection,
            spec=spec,
            action=IndexAction.CREATE,
        )

    for candidate in candidates:
        if _options_match(spec.options, candidate):
            return IndexPlanItem(
                database=database,
                collection=collection,
                spec=spec,
                action=IndexAction.IDENTICAL,
                existing_name=candidate.get("name"),
                existing_options=_existing_options(candidate, spec.options),
            )

    candidate = candidates[0]
    return IndexPlanItem(
        database=database,
        collection=collection,
        spec=spec,
        action=IndexAction.DIFFERENT,
        existing_name=candidate.get("name"),
        existing_options=_existing_options(candidate, spec.options),
    )


def _keys_match(declared: Mapping[str, Any], existing: Mapping[str, Any]) -> bool:
    """Key patterns must match in order; dict equality would ignore order."""
    return list(declared.items()) == list(existing.items())


def _existing_options(
    index: Mapping[str, Any], requested: Mapping[str, Any] | None
) -> dict[str, Any]:
    options = {key: value for key, value in index.items() if key not in ("v", "key")}
    if "name" not in (requested or {}):
        options.pop("name", None)
    return options


def _options_match(requested: Mapping[str, Any] | None, index: Mapping[str, Any]) -> bool:
    return _existing_options(index, requested) == dict(requested or {})
