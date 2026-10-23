from __future__ import annotations

from typing import Any

from sie_atlas_ops.index.drop import drop_indexes
from sie_atlas_ops.index.plan import ExtraIndex


class FakeDatabase:
    def __init__(self) -> None:
        self.commands: list[dict[str, Any]] = []

    def command(self, command: dict[str, Any]) -> dict[str, Any]:
        self.commands.append(command)
        return {"ok": 1}


class FakeClient:
    def __init__(self) -> None:
        self.databases: dict[str, FakeDatabase] = {}

    def __getitem__(self, database: str) -> FakeDatabase:
        return self.databases.setdefault(database, FakeDatabase())


def _extra(name: str, keys: dict[str, Any]) -> ExtraIndex:
    return ExtraIndex(database="exampleDB", collection="example", name=name, keys=keys)


def test_drop_indexes_sends_one_command_per_index() -> None:
    client = FakeClient()

    dropped = drop_indexes(
        client,
        [_extra("a_1", {"a": 1}), _extra("b_1", {"b": 1})],
    )

    assert client.databases["exampleDB"].commands == [
        {"dropIndexes": "example", "index": "a_1"},
        {"dropIndexes": "example", "index": "b_1"},
    ]
    assert [item.name for item in dropped] == ["a_1", "b_1"]


def test_drop_indexes_is_a_noop_without_extras() -> None:
    client = FakeClient()

    assert drop_indexes(client, []) == []
    assert client.databases == {}
