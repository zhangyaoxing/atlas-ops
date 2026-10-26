"""Fetch raw Atlas Search indexes for one cluster."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from atlas_ops.atlas import AtlasRequestClient

SEARCH_ACCEPT = "application/vnd.atlas.2024-05-30+json"


def fetch_search_indexes(
    client: AtlasRequestClient,
    project_id: str,
    cluster_name: str,
) -> list[dict[str, Any]]:
    """Return every search index in the cluster as raw API objects."""
    path = (
        f"/groups/{quote(project_id, safe='')}"
        f"/clusters/{quote(cluster_name, safe='')}/search/indexes"
    )
    payload = client.request(
        method="GET",
        path=path,
        response_model=Any,
        accept=SEARCH_ACCEPT,
    )
    return results(payload)


def results(payload: Any) -> list[dict[str, Any]]:
    """Normalize a list or ``{"results": [...]}`` response."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        items = payload.get("results")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []
