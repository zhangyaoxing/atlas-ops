from __future__ import annotations

from typing import Any

from sie_atlas_ops.index.create import create_indexes, default_index_name
from sie_atlas_ops.index.definitions import IndexSpec
from sie_atlas_ops.index.plan import IndexAction, IndexPlanItem


class FakeDatabase:
    def __init__(self) -> None:
        self.commands: list[dict[str, Any]] = []

    def command(self, command: dict[str, Any]) -> dict[str, Any]:
        self.commands.append(command)
        return {"ok": 1}


class FakeClient:
    def __init__(self) -> None:
        self.databases: dict[str, FakeDatabase] = {}

    def __getitem__(self, name: str) -> FakeDatabase:
        return self.databases.setdefault(name, FakeDatabase())


def _item(
    keys: dict[str, int | float | str],
    options: dict[str, Any] | None = None,
    *,
    database: str = "exampleDB",
    collection: str = "example",
    commit_quorum: int | str | None = None,
    action: IndexAction = IndexAction.CREATE,
) -> IndexPlanItem:
    return IndexPlanItem(
        database=database,
        collection=collection,
        spec=IndexSpec(keys=keys, options=options, commit_quorum=commit_quorum),
        action=action,
    )


def test_default_index_name_matches_mongodb_convention() -> None:
    assert default_index_name({"a": 1}) == "a_1"
    assert default_index_name({"a": 1, "b": -1}) == "a_1_b_-1"
    assert default_index_name({"a": 1.0, "b": -1.0}) == "a_1_b_-1"
    assert default_index_name({"description": "text"}) == "description_text"


def test_create_indexes_sends_key_name_and_options() -> None:
    client = FakeClient()

    results = create_indexes(
        client,
        [_item({"a": 1, "b": -1}, {"name": "custom", "unique": True})],
    )

    command = client.databases["exampleDB"].commands[0]
    assert command == {
        "createIndexes": "example",
        "indexes": [{"unique": True, "key": {"a": 1, "b": -1}, "name": "custom"}],
    }
    assert results[0].names == ("custom",)
    assert results[0].commit_quorum is None


def test_create_indexes_generates_name_when_missing() -> None:
    client = FakeClient()

    results = create_indexes(client, [_item({"a": 1, "b": -1}, {"unique": True})])

    assert client.databases["exampleDB"].commands[0]["indexes"][0]["name"] == "a_1_b_-1"
    assert results[0].names == ("a_1_b_-1",)


def test_create_indexes_batches_same_collection() -> None:
    client = FakeClient()

    results = create_indexes(
        client,
        [
            _item({"a": 1}),
            _item({"b": 1}, {"name": "b_index"}),
        ],
    )

    commands = client.databases["exampleDB"].commands
    assert len(commands) == 1
    assert len(commands[0]["indexes"]) == 2
    assert commands[0]["indexes"][0]["name"] == "a_1"
    assert commands[0]["indexes"][1]["name"] == "b_index"
    assert results[0].names == ("a_1", "b_index")


def test_create_indexes_splits_by_commit_quorum() -> None:
    client = FakeClient()

    results = create_indexes(
        client,
        [
            _item({"a": 1}, commit_quorum=None),
            _item({"b": 1}, commit_quorum=1),
        ],
    )

    commands = client.databases["exampleDB"].commands
    assert len(commands) == 2
    assert "commitQuorum" not in commands[0]
    assert commands[1]["commitQuorum"] == 1
    assert "commitQuorum" not in commands[1]["indexes"][0]
    assert [result.commit_quorum for result in results] == [None, 1]


def test_create_indexes_skips_non_create_actions() -> None:
    client = FakeClient()

    results = create_indexes(
        client,
        [
            _item({"a": 1}, action=IndexAction.IDENTICAL),
            _item({"b": 1}, action=IndexAction.DIFFERENT),
        ],
    )

    assert results == []
    assert client.databases == {}


def test_create_indexes_handles_multiple_collections() -> None:
    client = FakeClient()

    results = create_indexes(
        client,
        [
            _item({"a": 1}, collection="one"),
            _item({"b": 1}, collection="two"),
        ],
    )

    assert set(client.databases["exampleDB"].commands[i]["createIndexes"] for i in range(2)) == {
        "one",
        "two",
    }
    assert [result.collection for result in results] == ["one", "two"]
