"""Create pending indexes with the MongoDB ``createIndexes`` command."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sie_atlas_ops.index.definitions import IndexSpec
from sie_atlas_ops.index.mongo import MongoClientLike
from sie_atlas_ops.index.plan import IndexAction, IndexPlanItem


@dataclass(frozen=True, slots=True)
class CreateResult:
    """Indexes created by one ``createIndexes`` command."""

    database: str
    collection: str
    names: tuple[str, ...]
    commit_quorum: int | str | None


def create_indexes(client: MongoClientLike, plan: list[IndexPlanItem]) -> list[CreateResult]:
    """Create every index whose plan action is ``CREATE``.

    Indexes that share a database, collection, and ``commitQuorum`` are sent
    in one command. ``commitQuorum`` is command-level in MongoDB, so differing
    values produce separate commands.
    """
    groups: dict[tuple[str, str, int | str | None], list[IndexPlanItem]] = {}
    for item in plan:
        if item.action is not IndexAction.CREATE:
            continue
        key = (item.database, item.collection, item.spec.commit_quorum)
        groups.setdefault(key, []).append(item)

    results: list[CreateResult] = []
    for (database, collection, commit_quorum), items in groups.items():
        documents = [_index_document(item.spec) for item in items]
        command: dict[str, Any] = {
            "createIndexes": collection,
            "indexes": documents,
        }
        if commit_quorum is not None:
            command["commitQuorum"] = commit_quorum

        client[database].command(command)
        results.append(
            CreateResult(
                database=database,
                collection=collection,
                names=tuple(document["name"] for document in documents),
                commit_quorum=commit_quorum,
            )
        )
    return results


def default_index_name(keys: Mapping[str, Any]) -> str:
    """Generate the name MongoDB uses when none is supplied."""
    return "_".join(f"{field}_{direction}" for field, direction in keys.items())


def _index_document(spec: IndexSpec) -> dict[str, Any]:
    options = dict(spec.options or {})
    options.pop("commitQuorum", None)
    options["key"] = dict(spec.keys)
    options["name"] = options.get("name") or default_index_name(spec.keys)
    return options
