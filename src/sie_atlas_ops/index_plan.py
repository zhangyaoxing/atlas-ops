"""Decide which declared indexes already exist and which still need creating."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from sie_atlas_ops.index_definitions import CollectionIndexes, IndexSpec
from sie_atlas_ops.mongo import MissingNamespace, MongoClientLike


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


def plan_indexes(
    client: MongoClientLike,
    definitions: list[CollectionIndexes],
    missing: list[MissingNamespace],
) -> list[IndexPlanItem]:
    """Classify every declared index against the indexes already in MongoDB.

    Indexes are matched by their ordered key pattern only, never by name.
    ``options`` are compared after removing the server fields ``v`` and ``key``;
    ``name`` is ignored when the declaration does not specify one.
    """
    missing_namespaces = {(item.database, item.collection) for item in missing}
    existing_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
    plan: list[IndexPlanItem] = []

    for definition in definitions:
        key = (definition.database, definition.collection)
        if key in missing_namespaces:
            existing: list[dict[str, Any]] = []
        elif key in existing_cache:
            existing = existing_cache[key]
        else:
            collection = client[definition.database][definition.collection]
            existing = [dict(index) for index in collection.list_indexes()]
            existing_cache[key] = existing

        for spec in definition.indexes:
            plan.append(_decide(definition.database, definition.collection, spec, existing))
    return plan


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
