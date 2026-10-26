from __future__ import annotations

from pathlib import Path

from atlas_ops.search.definitions import SearchCollectionIndexes, SearchIndexSpec
from atlas_ops.search.plan import (
    SearchAction,
    SearchExtra,
    plan_search_indexes,
)


def _definition(*specs: SearchIndexSpec) -> SearchCollectionIndexes:
    return SearchCollectionIndexes(
        path=Path("search.json"),
        database="db",
        collection="coll",
        indexes=specs,
    )


def _remote(
    name: str,
    definition: dict[str, object],
    *,
    index_id: str = "id",
    index_type: str = "search",
    database: str = "db",
    collection: str = "coll",
) -> dict[str, object]:
    return {
        "database": database,
        "collectionName": collection,
        "name": name,
        "indexID": index_id,
        "type": index_type,
        "latestDefinition": definition,
    }


def test_plan_creates_updates_and_skips() -> None:
    plan = plan_search_indexes(
        [
            _remote("same", {"mappings": {"dynamic": True}}),
            _remote("changed", {"mappings": {"dynamic": True}}),
        ],
        [
            _definition(
                SearchIndexSpec(
                    name="same",
                    type="search",
                    definition={"mappings": {"dynamic": True}},
                ),
                SearchIndexSpec(
                    name="changed",
                    type="search",
                    definition={"mappings": {"dynamic": False}},
                ),
                SearchIndexSpec(
                    name="new",
                    type="vectorSearch",
                    definition={"fields": []},
                ),
            )
        ],
    )

    assert [(item.spec.name, item.action) for item in plan.items] == [
        ("same", SearchAction.IDENTICAL),
        ("changed", SearchAction.UPDATE),
        ("new", SearchAction.CREATE),
    ]
    assert plan.extras == ()


def test_plan_reports_remote_indexes_not_in_local_definitions() -> None:
    plan = plan_search_indexes(
        [
            _remote("local", {}, index_id="id-local"),
            _remote("old", {}, index_id="id-old"),
            _remote("other", {}, index_id="id-other", database="other"),
        ],
        [_definition(SearchIndexSpec(name="local", type="search", definition={}))],
    )

    assert plan.extras == (
        SearchExtra(database="db", collection="coll", name="old", index_id="id-old"),
        SearchExtra(
            database="other",
            collection="coll",
            name="other",
            index_id="id-other",
        ),
    )


def test_plan_includes_remote_namespaces_without_definitions() -> None:
    plan = plan_search_indexes(
        [_remote("old", {}, database="other", index_id="id-old")],
        [_definition(SearchIndexSpec(name="local", type="search", definition={}))],
    )

    assert plan.extras == (
        SearchExtra(
            database="other",
            collection="coll",
            name="old",
            index_id="id-old",
        ),
    )


def test_plan_updates_when_type_differs() -> None:
    plan = plan_search_indexes(
        [_remote("index", {}, index_type="search")],
        [_definition(SearchIndexSpec(name="index", type="vectorSearch", definition={}))],
    )

    assert plan.items[0].action is SearchAction.UPDATE
