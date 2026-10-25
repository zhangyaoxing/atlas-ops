from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from atlas_ops.index.definitions import load_index_definitions
from atlas_ops.index.export import (
    ExportedCollection,
    ExportResult,
    export_indexes,
)


class FakeCollection:
    def __init__(self, indexes: list[dict[str, Any]]) -> None:
        self._indexes = indexes

    def list_indexes(self) -> list[dict[str, Any]]:
        return list(self._indexes)


class FakeDatabase:
    def __init__(self, collections: dict[str, dict[str, Any]]) -> None:
        self._collections = collections

    def list_collections(self) -> list[dict[str, Any]]:
        return [
            {"name": name, "type": spec.get("type", "collection")}
            for name, spec in self._collections.items()
        ]

    def __getitem__(self, collection: str) -> FakeCollection:
        spec = self._collections.get(collection)
        if spec is None or spec.get("type") == "view":
            raise AssertionError(f"must not read {collection!r}")
        return FakeCollection(spec.get("indexes", []))


class FakeClient:
    def __init__(self, databases: dict[str, dict[str, dict[str, Any]]]) -> None:
        self._databases = databases

    def list_database_names(self) -> list[str]:
        return list(self._databases)

    def __getitem__(self, name: str) -> FakeDatabase:
        return FakeDatabase(self._databases[name])


def test_export_writes_one_file_per_collection(tmp_path: Path) -> None:
    client = FakeClient(
        {
            "admin": {"secret": {"indexes": []}},
            "local": {"oplog.rs": {"indexes": []}},
            "config": {"chunks": {"indexes": []}},
            "shop": {
                "users": {
                    "indexes": [
                        {"v": 2, "key": {"_id": 1}, "name": "_id_"},
                        {"v": 2, "key": {"email": 1}, "name": "email_1", "unique": True},
                        {"v": 2, "key": {"age": 1}, "name": "age_1"},
                    ]
                },
                "empty": {"indexes": [{"v": 2, "key": {"_id": 1}, "name": "_id_"}]},
                "orders": {"indexes": [{"v": 2, "key": {"createdAt": 1}, "name": "createdAt_1"}]},
                "system.js": {"indexes": [{"v": 2, "key": {"_id": 1}, "name": "_id_"}]},
                "usersView": {"type": "view", "indexes": []},
            },
        }
    )

    result = export_indexes(client, tmp_path)

    assert result == ExportResult(
        files=2,
        indexes=3,
        collections=(
            ExportedCollection(database="shop", collection="orders", indexes=1),
            ExportedCollection(database="shop", collection="users", indexes=2),
        ),
    )
    assert (tmp_path / "shop.orders.json").exists()
    payload = json.loads((tmp_path / "shop.users.json").read_text(encoding="utf-8"))
    assert payload["database"] == "shop"
    assert payload["collection"] == "users"
    assert [item["options"]["name"] for item in payload["indexes"]] == [
        "age_1",
        "email_1",
    ]
    assert payload["indexes"][1]["keys"] == {"email": 1}
    assert payload["indexes"][1]["options"]["unique"] is True
    assert all("v" not in item["options"] for item in payload["indexes"])
    assert all("key" not in item["options"] for item in payload["indexes"])


def test_export_sanitizes_namespace_filename(tmp_path: Path) -> None:
    client = FakeClient(
        {"db": {"a/b": {"indexes": [{"v": 2, "key": {"field": 1}, "name": "field_1"}]}}}
    )

    result = export_indexes(client, tmp_path)

    assert result.files == 1
    assert (tmp_path / "db.a_b.json").exists()


def test_export_skips_collections_without_user_indexes(tmp_path: Path) -> None:
    client = FakeClient(
        {"db": {"empty": {"indexes": [{"v": 2, "key": {"_id": 1}, "name": "_id_"}]}}}
    )

    assert export_indexes(client, tmp_path) == ExportResult(files=0, indexes=0, collections=())
    assert list(tmp_path.iterdir()) == []


def test_exported_files_round_trip_through_loader(tmp_path: Path) -> None:
    client = FakeClient(
        {
            "db": {
                "users": {
                    "indexes": [
                        {"v": 2, "key": {"_id": 1}, "name": "_id_"},
                        {
                            "v": 2,
                            "key": {"email": 1},
                            "name": "email_1",
                            "unique": True,
                        },
                    ]
                }
            }
        }
    )

    export_indexes(client, tmp_path)
    definitions = load_index_definitions(tmp_path)

    assert len(definitions) == 1
    assert definitions[0].database == "db"
    assert definitions[0].collection == "users"
    assert definitions[0].indexes[0].keys == {"email": 1}
    assert definitions[0].indexes[0].options == {
        "name": "email_1",
        "unique": True,
    }
