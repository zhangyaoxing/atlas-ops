from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from atlas_ops.search.apply import apply_search_plan
from atlas_ops.search.definitions import SearchIndexSpec
from atlas_ops.search.plan import (
    SearchAction,
    SearchExtra,
    SearchPlan,
    SearchPlanItem,
)


class FakeSearch:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.deleted: list[dict[str, Any]] = []

    def create_group_cluster_fts_index(self, **kwargs: Any) -> Any:
        self.created.append(kwargs)
        return kwargs["body"]

    def update_group_cluster_fts_index(self, **kwargs: Any) -> Any:
        self.updated.append(kwargs)
        return kwargs["body"]

    def delete_group_cluster_fts_index(self, **kwargs: Any) -> Any:
        self.deleted.append(kwargs)
        return {"ok": 1}


def _item(
    name: str,
    action: SearchAction,
    *,
    index_type: str = "search",
    definition: dict[str, Any] | None = None,
    remote_id: str | None = None,
) -> SearchPlanItem:
    return SearchPlanItem(
        database="db",
        collection="coll",
        spec=SearchIndexSpec(
            name=name,
            type=index_type,
            definition=definition or {},
        ),
        action=action,
        remote_id=remote_id,
    )


def test_apply_creates_updates_and_deletes() -> None:
    search = FakeSearch()
    one = _item(
        "new",
        SearchAction.CREATE,
        index_type="vectorSearch",
        definition={"fields": [{"type": "vector"}]},
    )
    two = _item(
        "changed",
        SearchAction.UPDATE,
        definition={"mappings": {"dynamic": False}},
        remote_id="id-changed",
    )
    three = _item("same", SearchAction.IDENTICAL)
    plan = SearchPlan(
        items=(one, two, three),
        extras=(
            SearchExtra(
                database="db",
                collection="coll",
                name="old",
                index_id="id-old",
            ),
        ),
    )

    client: Any = SimpleNamespace(atlas_search=search)
    result = apply_search_plan(
        client,
        "project",
        "cluster0",
        plan,
        delete_extras=True,
    )

    assert result.created == 1
    assert result.updated == 1
    assert result.deleted == 1
    assert search.created[0]["body"].model_dump(by_alias=True, exclude_none=True) == {
        "database": "db",
        "collectionName": "coll",
        "name": "new",
        "type": "vectorSearch",
        "fields": [{"type": "vector"}],
    }
    assert search.updated[0]["index_id"] == "id-changed"
    assert search.deleted == [
        {
            "cluster_name": "cluster0",
            "group_id": "project",
            "index_id": "id-old",
        }
    ]


def test_apply_can_skip_extra_deletion() -> None:
    search = FakeSearch()
    plan = SearchPlan(
        items=(_item("same", SearchAction.IDENTICAL),),
        extras=(
            SearchExtra(
                database="db",
                collection="coll",
                name="old",
                index_id="id-old",
            ),
        ),
    )

    client: Any = SimpleNamespace(atlas_search=search)
    result = apply_search_plan(
        client,
        "project",
        "cluster0",
        plan,
        delete_extras=False,
    )

    assert result.deleted == 0
    assert search.deleted == []
