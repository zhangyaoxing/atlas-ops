"""Drop existing indexes that are not present in the definitions."""

from __future__ import annotations

from dataclasses import dataclass

from atlas_ops.index.mongo import MongoCommandClient
from atlas_ops.index.plan import ExtraIndex


@dataclass(frozen=True, slots=True)
class DroppedIndex:
    """One index removed by the tool."""

    database: str
    collection: str
    name: str


def drop_indexes(client: MongoCommandClient, extras: list[ExtraIndex]) -> list[DroppedIndex]:
    """Drop every extra index by name."""
    dropped: list[DroppedIndex] = []
    for extra in extras:
        client[extra.database].command({"dropIndexes": extra.collection, "index": extra.name})
        dropped.append(
            DroppedIndex(
                database=extra.database,
                collection=extra.collection,
                name=extra.name,
            )
        )
    return dropped
