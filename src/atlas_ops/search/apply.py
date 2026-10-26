"""Apply an Atlas Search index plan to a cluster."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from pyatlassdk.admin.models.cluster_search_index import ClusterSearchIndex

from atlas_ops.errors import AtlasOperationError
from atlas_ops.search.plan import SearchAction, SearchExtra, SearchPlan


class AtlasSearchClientLike(Protocol):
    """Client exposing the generated ``atlas_search`` resource."""

    atlas_search: Any


@dataclass(frozen=True, slots=True)
class SearchApplyResult:
    """Counts and extras affected by one sync run."""

    created: int
    updated: int
    deleted: int
    deleted_extras: tuple[SearchExtra, ...]


def apply_search_plan(
    client: AtlasSearchClientLike,
    project_id: str,
    cluster_name: str,
    plan: SearchPlan,
    *,
    delete_extras: bool,
) -> SearchApplyResult:
    """Create/update declared indexes, then delete confirmed extras."""
    created = 0
    updated = 0
    for item in plan.items:
        if item.action is SearchAction.IDENTICAL:
            continue
        body = _body(item)
        if item.action is SearchAction.CREATE:
            client.atlas_search.create_group_cluster_fts_index(
                cluster_name=cluster_name,
                group_id=project_id,
                body=body,
            )
            created += 1
        else:
            if not item.remote_id:
                raise AtlasOperationError(
                    f"Search index {item.spec.name!r} has no remote index ID."
                )
            client.atlas_search.update_group_cluster_fts_index(
                cluster_name=cluster_name,
                group_id=project_id,
                index_id=item.remote_id,
                body=body,
            )
            updated += 1

    deleted_extras: list[SearchExtra] = []
    if delete_extras:
        for extra in plan.extras:
            client.atlas_search.delete_group_cluster_fts_index(
                cluster_name=cluster_name,
                group_id=project_id,
                index_id=extra.index_id,
            )
            deleted_extras.append(extra)

    return SearchApplyResult(
        created=created,
        updated=updated,
        deleted=len(deleted_extras),
        deleted_extras=tuple(deleted_extras),
    )


def _body(item: Any) -> ClusterSearchIndex:
    document = dict(item.spec.definition)
    document.update(
        {
            "database": item.database,
            "collectionName": item.collection,
            "name": item.spec.name,
            "type": item.spec.type,
        }
    )
    return ClusterSearchIndex.model_validate(document)
