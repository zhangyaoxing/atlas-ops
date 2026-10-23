from __future__ import annotations

from pathlib import Path
from typing import Any

from sie_atlas_ops.index.definitions import CollectionIndexes, IndexSpec
from sie_atlas_ops.index.mongo import MissingNamespace
from sie_atlas_ops.index.plan import IndexAction, plan_indexes


class FakeCollection:
    def __init__(self, indexes: list[dict[str, Any]]) -> None:
        self._indexes = indexes

    def list_indexes(self) -> list[dict[str, Any]]:
        return list(self._indexes)


class FakeDatabase:
    def __init__(self, collections: dict[str, list[dict[str, Any]]]) -> None:
        self._collections = collections

    def __getitem__(self, collection: str) -> FakeCollection:
        return FakeCollection(self._collections.get(collection, []))


class FakeClient:
    def __init__(self, databases: dict[str, dict[str, list[dict[str, Any]]]]) -> None:
        self._databases = databases

    def __getitem__(self, name: str) -> FakeDatabase:
        return FakeDatabase(self._databases.get(name, {}))


class ExplodingClient:
    def __getitem__(self, name: str) -> Any:
        raise AssertionError("indexes must not be listed for missing namespaces")


def _spec(keys: dict[str, int | float | str], options: dict[str, Any] | None = None) -> IndexSpec:
    return IndexSpec(keys=keys, options=options)


def _definition(
    *indexes: IndexSpec,
    database: str = "exampleDB",
    collection: str = "example",
) -> CollectionIndexes:
    return CollectionIndexes(
        path=Path("definitions.json"),
        database=database,
        collection=collection,
        indexes=tuple(indexes),
    )


def test_creates_index_when_no_keys_match() -> None:
    client = FakeClient({"exampleDB": {"example": [{"v": 2, "key": {"_id": 1}, "name": "_id_"}]}})

    plan = plan_indexes(client, [_definition(_spec({"field": 1}))], [])

    assert [item.action for item in plan.items] == [IndexAction.CREATE]


def test_identical_when_keys_and_options_match() -> None:
    client = FakeClient(
        {
            "exampleDB": {
                "example": [
                    {
                        "v": 2,
                        "key": {"field": 1},
                        "name": "field_index",
                        "unique": True,
                    }
                ]
            }
        }
    )

    plan = plan_indexes(
        client,
        [_definition(_spec({"field": 1}, {"name": "field_index", "unique": True}))],
        [],
    )

    assert plan.items[0].action is IndexAction.IDENTICAL
    assert plan.items[0].existing_name == "field_index"


def test_identical_ignores_generated_name_when_not_requested() -> None:
    client = FakeClient(
        {"exampleDB": {"example": [{"v": 2, "key": {"field": 1}, "name": "field_1"}]}}
    )

    plan = plan_indexes(client, [_definition(_spec({"field": 1}))], [])

    assert plan.items[0].action is IndexAction.IDENTICAL
    assert plan.items[0].existing_options == {}


def test_different_when_options_differ() -> None:
    client = FakeClient(
        {
            "exampleDB": {
                "example": [{"v": 2, "key": {"field": 1}, "name": "other_index", "unique": True}]
            }
        }
    )

    plan = plan_indexes(client, [_definition(_spec({"field": 1}, {"name": "field_index"}))], [])

    assert plan.items[0].action is IndexAction.DIFFERENT
    assert plan.items[0].existing_name == "other_index"
    assert plan.items[0].existing_options == {"name": "other_index", "unique": True}


def test_different_when_requested_name_differs() -> None:
    client = FakeClient(
        {"exampleDB": {"example": [{"v": 2, "key": {"field": 1}, "name": "other"}]}}
    )

    plan = plan_indexes(client, [_definition(_spec({"field": 1}, {"name": "field_index"}))], [])

    assert plan.items[0].action is IndexAction.DIFFERENT


def test_key_order_must_match() -> None:
    client = FakeClient(
        {"exampleDB": {"example": [{"v": 2, "key": {"b": 1, "a": 1}, "name": "b_1_a_1"}]}}
    )

    plan = plan_indexes(client, [_definition(_spec({"a": 1, "b": 1}))], [])

    assert plan.items[0].action is IndexAction.CREATE


def test_decimal_directions_match_existing_integer_keys() -> None:
    client = FakeClient(
        {"exampleDB": {"example": [{"v": 2, "key": {"field": 1}, "name": "field_1"}]}}
    )

    plan = plan_indexes(client, [_definition(_spec({"field": 1.0}))], [])

    assert plan.items[0].action is IndexAction.IDENTICAL


def test_missing_namespace_is_created_without_listing() -> None:
    missing = [MissingNamespace(database="exampleDB", collection="example", database_exists=False)]

    plan = plan_indexes(ExplodingClient(), [_definition(_spec({"field": 1}))], missing)

    assert [item.action for item in plan.items] == [IndexAction.CREATE]


def test_plan_preserves_definition_order() -> None:
    client = FakeClient({"exampleDB": {"example": [{"v": 2, "key": {"a": 1}, "name": "a_1"}]}})
    definition = CollectionIndexes(
        path=Path("definitions.json"),
        database="exampleDB",
        collection="example",
        indexes=(
            _spec({"a": 1}),
            _spec({"b": 1}),
            _spec({"c": 1}),
        ),
    )

    plan = plan_indexes(client, [definition], [])

    assert [item.spec.keys for item in plan.items] == [
        {"a": 1},
        {"b": 1},
        {"c": 1},
    ]
    assert [item.action for item in plan.items] == [
        IndexAction.IDENTICAL,
        IndexAction.CREATE,
        IndexAction.CREATE,
    ]


def test_identical_candidate_wins_over_different_candidate() -> None:
    client = FakeClient(
        {
            "exampleDB": {
                "example": [
                    {"v": 2, "key": {"field": 1}, "name": "different", "unique": True},
                    {"v": 2, "key": {"field": 1}, "name": "same"},
                ]
            }
        }
    )

    plan = plan_indexes(client, [_definition(_spec({"field": 1}, {"name": "same"}))], [])

    assert plan.items[0].action is IndexAction.IDENTICAL


def test_reports_extra_indexes_but_not_declared_or_id() -> None:
    client = FakeClient(
        {
            "exampleDB": {
                "example": [
                    {"v": 2, "key": {"_id": 1}, "name": "_id_"},
                    {"v": 2, "key": {"field": 1}, "name": "field_1"},
                    {"v": 2, "key": {"extra": 1}, "name": "extra_1"},
                ]
            }
        }
    )

    plan = plan_indexes(client, [_definition(_spec({"field": 1}))], [])

    assert [(extra.name, extra.keys) for extra in plan.extras] == [("extra_1", {"extra": 1})]


def test_different_options_index_is_not_reported_as_extra() -> None:
    client = FakeClient(
        {
            "exampleDB": {
                "example": [{"v": 2, "key": {"field": 1}, "name": "field_1", "unique": True}]
            }
        }
    )

    plan = plan_indexes(client, [_definition(_spec({"field": 1}))], [])

    assert plan.items[0].action is IndexAction.DIFFERENT
    assert plan.extras == ()


def test_missing_namespace_has_no_extra_indexes() -> None:
    missing = [MissingNamespace(database="exampleDB", collection="example", database_exists=False)]

    plan = plan_indexes(ExplodingClient(), [_definition(_spec({"field": 1}))], missing)

    assert plan.extras == ()
