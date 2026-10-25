from __future__ import annotations

from pathlib import Path

import pytest
from pymongo.errors import OperationFailure

from atlas_ops.errors import AtlasOperationError
from atlas_ops.index.definitions import CollectionIndexes
from atlas_ops.index.mongo import (
    MissingNamespace,
    build_authenticated_uri,
    find_missing_namespaces,
    wait_for_user,
)


class FakeMongoDatabase:
    def __init__(self, collections: list[str]) -> None:
        self._collections = collections

    def list_collection_names(self) -> list[str]:
        return list(self._collections)


class FakeMongoClient:
    def __init__(self, databases: dict[str, list[str]]) -> None:
        self._databases = databases

    def list_database_names(self) -> list[str]:
        return list(self._databases)

    def __getitem__(self, name: str) -> FakeMongoDatabase:
        return FakeMongoDatabase(self._databases.get(name, []))


def _definition(database: str, collection: str) -> CollectionIndexes:
    return CollectionIndexes(
        path=Path(f"{database}.{collection}.json"),
        database=database,
        collection=collection,
        indexes=(),
    )


def test_build_authenticated_uri_encodes_credentials() -> None:
    uri = build_authenticated_uri(
        "mongodb+srv://cluster0.abc.mongodb.net/?retryWrites=true&w=majority",
        "user",
        "p@ss:w/rd",
    )

    assert uri == (
        "mongodb+srv://user:p%40ss%3Aw%2Frd@cluster0.abc.mongodb.net/"
        "?retryWrites=true&w=majority&authSource=admin"
    )


def test_build_authenticated_uri_adds_auth_source_without_query() -> None:
    uri = build_authenticated_uri("mongodb://host:27017", "user", "password")

    assert uri == "mongodb://user:password@host:27017?authSource=admin"


def test_build_authenticated_uri_supports_multiple_hosts() -> None:
    uri = build_authenticated_uri(
        "mongodb://shard-00-00.example.net:27017,"
        "shard-00-01.example.net:27017,"
        "shard-00-02.example.net:27017/?replicaSet=atlas-abc",
        "user",
        "p@ss",
    )

    assert uri == (
        "mongodb://user:p%40ss@shard-00-00.example.net:27017,"
        "shard-00-01.example.net:27017,"
        "shard-00-02.example.net:27017/?replicaSet=atlas-abc&authSource=admin"
    )


def test_build_authenticated_uri_replaces_existing_credentials() -> None:
    uri = build_authenticated_uri("mongodb://old:secret@host/exampleDB", "user", "password")

    assert uri == "mongodb://user:password@host/exampleDB?authSource=admin"


def test_build_authenticated_uri_requires_a_host() -> None:
    with pytest.raises(ValueError, match="Invalid MongoDB URI"):
        build_authenticated_uri("not-a-uri", "user", "password")


def test_find_missing_namespaces_returns_empty_when_all_exist() -> None:
    client = FakeMongoClient({"exampleDB": ["example"]})

    assert find_missing_namespaces(client, [_definition("exampleDB", "example")]) == []


def test_find_missing_namespaces_detects_missing_collection() -> None:
    client = FakeMongoClient({"exampleDB": []})

    assert find_missing_namespaces(client, [_definition("exampleDB", "example")]) == [
        MissingNamespace(database="exampleDB", collection="example", database_exists=True)
    ]


def test_find_missing_namespaces_detects_missing_database() -> None:
    client = FakeMongoClient({})

    assert find_missing_namespaces(client, [_definition("missingDB", "example")]) == [
        MissingNamespace(database="missingDB", collection="example", database_exists=False)
    ]


def test_find_missing_namespaces_deduplicates_targets() -> None:
    client = FakeMongoClient({})
    definitions = [
        _definition("missingDB", "example"),
        _definition("missingDB", "example"),
    ]

    assert len(find_missing_namespaces(client, definitions)) == 1


def test_find_missing_namespaces_checks_every_definition() -> None:
    client = FakeMongoClient({"a": ["one"]})
    definitions = [
        _definition("a", "one"),
        _definition("a", "two"),
        _definition("b", "three"),
    ]

    missing = find_missing_namespaces(client, definitions)

    assert missing == [
        MissingNamespace(database="a", collection="two", database_exists=True),
        MissingNamespace(database="b", collection="three", database_exists=False),
    ]


class FlakyAuthClient:
    def __init__(self, failures: int, code: int = 18) -> None:
        self.failures = failures
        self.code = code
        self.calls = 0

    def list_database_names(self) -> list[str]:
        self.calls += 1
        if self.failures:
            self.failures -= 1
            raise OperationFailure("Authentication failed.", code=self.code)
        return ["exampleDB"]


def test_wait_for_user_returns_immediately() -> None:
    client = FlakyAuthClient(failures=0)

    wait_for_user(client)

    assert client.calls == 1


@pytest.mark.parametrize("code", [13, 18])
def test_wait_for_user_retries_authentication_errors(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    monkeypatch.setattr("atlas_ops.index.mongo.time.sleep", lambda _: None)
    client = FlakyAuthClient(failures=2, code=code)

    wait_for_user(client, timeout_seconds=5.0, interval_seconds=0.01)

    assert client.calls == 3


def test_wait_for_user_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("atlas_ops.index.mongo.time.sleep", lambda _: None)
    client = FlakyAuthClient(failures=10)

    with pytest.raises(AtlasOperationError, match="did not become active"):
        wait_for_user(client, timeout_seconds=0)


def test_wait_for_user_propagates_other_errors() -> None:
    client = FlakyAuthClient(failures=1, code=2)

    with pytest.raises(OperationFailure):
        wait_for_user(client, timeout_seconds=5.0)
