from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import pytest
from pyatlassdk import AtlasAdminApiError
from pyatlassdk.admin.models.cloud_database_user import CloudDatabaseUser
from pyatlassdk.admin.models.cluster_description20240805 import ClusterDescription20240805
from pymongo.errors import PyMongoError
from typer.testing import CliRunner

from atlas_ops import __version__
from atlas_ops.cli import (
    _format_export_table,
    _format_index_table,
    _format_search_sync_table,
    app,
)

runner = CliRunner()

GROUP_A = SimpleNamespace(id="a" * 24, name="Project A")
CLUSTER = SimpleNamespace(id="b" * 24, name="cluster0")


class FakeAtlasClient:
    def __init__(
        self,
        groups: list[Any],
        clusters_by_group: dict[str, list[Any]],
        detail: ClusterDescription20240805,
    ) -> None:
        self._groups = {group.id: group for group in groups}
        self._clusters = clusters_by_group
        self._detail = detail
        self.get_group_calls: list[str] = []
        self.get_cluster_calls: list[tuple[str, str]] = []
        self.created_user_payload: dict[str, Any] | None = None
        self.deleted_user_paths: list[str] = []
        self.search_index_payload: list[dict[str, Any]] = []
        self.search_index_requests: list[tuple[str, str, dict[str, Any]]] = []
        self.search_create_calls: list[dict[str, Any]] = []
        self.search_update_calls: list[dict[str, Any]] = []
        self.search_delete_calls: list[dict[str, Any]] = []
        self.atlas_search = SimpleNamespace(
            create_group_cluster_fts_index=self._create_search,
            update_group_cluster_fts_index=self._update_search,
            delete_group_cluster_fts_index=self._delete_search,
        )
        self.projects = SimpleNamespace(get_group=self._get_group)
        self.clusters = SimpleNamespace(
            list_group_clusters=self._list_clusters,
            get_group_cluster=self._get_cluster,
        )

    def _create_search(self, **kwargs: Any) -> Any:
        self.search_create_calls.append(kwargs)
        return kwargs["body"]

    def _update_search(self, **kwargs: Any) -> Any:
        self.search_update_calls.append(kwargs)
        return kwargs["body"]

    def _delete_search(self, **kwargs: Any) -> Any:
        self.search_delete_calls.append(kwargs)
        return {"ok": 1}

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        if method == "DELETE":
            self.deleted_user_paths.append(path)
            return None
        if path.endswith("/search/indexes"):
            self.search_index_requests.append((method, path, kwargs))
            return self.search_index_payload
        self.created_user_payload = kwargs["json"]
        return CloudDatabaseUser.model_validate(kwargs["json"])

    def _get_group(self, group_id: str, **_: Any) -> Any:
        self.get_group_calls.append(group_id)
        group = self._groups.get(group_id)
        if group is None:
            raise AtlasAdminApiError(404, "Not Found", {"detail": "project not found"})
        return group

    def _list_clusters(self, group_id: str, **_: Any) -> SimpleNamespace:
        clusters = self._clusters.get(group_id, [])
        return SimpleNamespace(results=clusters, total_count=len(clusters))

    def _get_cluster(self, cluster_name: str, group_id: str, **_: Any) -> Any:
        self.get_cluster_calls.append((cluster_name, group_id))
        return self._detail

    def __enter__(self) -> FakeAtlasClient:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _detail(**connection_strings: str) -> ClusterDescription20240805:
    return ClusterDescription20240805.model_validate(
        {"name": "cluster0", "connectionStrings": connection_strings}
    )


class FakeMongoCollection:
    def __init__(self, indexes: list[dict[str, Any]]) -> None:
        self._indexes = indexes

    def list_indexes(self) -> list[dict[str, Any]]:
        return list(self._indexes)


class FakeMongoDatabase:
    def __init__(
        self,
        collections: list[str],
        indexes: dict[tuple[str, str], list[dict[str, Any]]],
        database: str,
        commands: list[dict[str, Any]],
    ) -> None:
        self._collections = collections
        self._indexes = indexes
        self._database = database
        self._commands = commands

    def list_collection_names(self) -> list[str]:
        return list(self._collections)

    def list_collections(self) -> list[dict[str, Any]]:
        return [{"name": collection, "type": "collection"} for collection in self._collections]

    def __getitem__(self, collection: str) -> FakeMongoCollection:
        return FakeMongoCollection(self._indexes.get((self._database, collection), []))

    def command(self, command: dict[str, Any]) -> dict[str, Any]:
        self._commands.append(command)
        return {"ok": 1}


class FakeMongoClient:
    def __init__(
        self,
        databases: dict[str, list[str]],
        indexes: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
    ) -> None:
        self._databases = databases
        self._indexes = indexes or {}
        self.commands: list[dict[str, Any]] = []
        self.closed = False

    def list_database_names(self) -> list[str]:
        return list(self._databases)

    def __getitem__(self, name: str) -> FakeMongoDatabase:
        return FakeMongoDatabase(
            self._databases.get(name, []),
            self._indexes,
            name,
            self.commands,
        )

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def mongo(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Replace the MongoDB client with one where exampleDB.example exists."""
    monkeypatch.delenv("ATLAS_DB_USERNAME", raising=False)
    monkeypatch.delenv("ATLAS_DB_PASSWORD", raising=False)
    client = FakeMongoClient({"exampleDB": ["example"]})
    uris: list[str] = []

    def factory(uri: str) -> FakeMongoClient:
        uris.append(uri)
        return client

    monkeypatch.setattr("atlas_ops.cli.create_client", factory)
    return SimpleNamespace(client=client, uris=uris)


@pytest.fixture(autouse=True)
def confirm_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Approve the change preview unless a test overrides it."""
    monkeypatch.setattr("atlas_ops.cli._confirm_changes", lambda: True)


def _write_definitions(directory: Path, name: str = "indexes.json") -> Path:
    path = directory / name
    path.write_text(
        json.dumps(
            {
                "database": "exampleDB",
                "collection": "example",
                "indexes": [{"keys": {"field": 1}, "options": {"name": "field_index"}}],
            }
        ),
        encoding="utf-8",
    )
    return path


def _patch_client(monkeypatch: pytest.MonkeyPatch, client: FakeAtlasClient) -> None:
    monkeypatch.setattr("atlas_ops.cli.client_from_env", lambda: client)


def _clear_target_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATLAS_PROJECT_ID", raising=False)
    monkeypatch.delenv("ATLAS_CLUSTER_NAME", raising=False)


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "index" in result.stdout
    assert "version" in result.stdout


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Usage" in result.stdout


def test_index_help_documents_arguments() -> None:
    result = runner.invoke(app, ["index", "--help"])
    assert result.exit_code == 0
    assert "directory" in result.stdout
    assert "--project-id" in result.stdout
    assert "--cluster" in result.stdout
    assert "-g" in result.stdout
    assert "-c" in result.stdout
    assert "-u" in result.stdout
    assert "-p" in result.stdout
    assert "--export" in result.stdout
    assert "-e" in result.stdout


def test_index_selects_connection_string(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient(
        [GROUP_A],
        {GROUP_A.id: [CLUSTER]},
        _detail(standard="mongodb://direct", standardSrv="mongodb+srv://srv"),
    )
    _patch_client(monkeypatch, client)

    result = runner.invoke(
        app,
        ["index", str(tmp_path), "--project-id", GROUP_A.id, "--cluster", "cluster0"],
        input="2\n",
    )

    assert result.exit_code == 0, result.output
    assert 'Connection: "mongodb+srv://srv"' in result.output
    assert "Project A" in result.output
    assert "exampleDB.example" in result.output
    assert "Index creation complete: 1 index(es) created." in result.output
    assert client.get_group_calls == [GROUP_A.id]
    assert client.get_cluster_calls == [("cluster0", GROUP_A.id)]


def test_index_creates_temporary_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)

    result = runner.invoke(
        app, ["index", str(tmp_path), "--project-id", GROUP_A.id, "--cluster", "cluster0"]
    )

    assert result.exit_code == 0, result.output
    payload = client.created_user_payload
    assert payload is not None
    assert payload["groupId"] == GROUP_A.id
    assert payload["databaseName"] == "admin"
    assert payload["roles"] == [{"databaseName": "admin", "roleName": "readWriteAnyDatabase"}]
    assert payload["scopes"] == [{"name": "cluster0", "type": "CLUSTER"}]
    assert payload["username"].startswith("atlas_ops_")
    assert len(payload["password"]) >= 8
    json.dumps(payload)  # the body must be JSON-serializable

    delete_after = datetime.fromisoformat(payload["deleteAfterDate"].replace("Z", "+00:00"))
    remaining = delete_after - datetime.now(timezone.utc)
    assert timedelta(hours=5, minutes=59) <= remaining <= timedelta(hours=6, minutes=1)

    assert client.deleted_user_paths == [
        f"/groups/{GROUP_A.id}/databaseUsers/admin/{payload['username']}"
    ]
    assert payload["password"] not in result.output


def test_index_uses_provided_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mongo: SimpleNamespace
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)

    result = runner.invoke(
        app,
        [
            "index",
            str(tmp_path),
            "--project-id",
            GROUP_A.id,
            "--cluster",
            "cluster0",
            "--db-username",
            "appUser",
            "--db-password",
            "S3cret!",
        ],
    )

    assert result.exit_code == 0, result.output
    assert client.created_user_payload is None
    assert client.deleted_user_paths == []
    assert "Using provided database user" in result.output
    assert "Created temporary user" not in result.output
    assert "Waiting for temporary user" not in result.output
    assert f"{quote('appUser', safe='')}:{quote('S3cret!', safe='')}@" in mongo.uris[0]
    assert "S3cret!" not in result.output


def test_index_accepts_short_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mongo: SimpleNamespace
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)

    result = runner.invoke(
        app,
        [
            "index",
            str(tmp_path),
            "-g",
            GROUP_A.id,
            "-c",
            "cluster0",
            "-u",
            "shortUser",
            "-p",
            "shortPass!",
        ],
    )

    assert result.exit_code == 0, result.output
    assert client.created_user_payload is None
    assert client.deleted_user_paths == []
    assert f"{quote('shortUser', safe='')}:{quote('shortPass!', safe='')}@" in mongo.uris[0]


def test_index_uses_provided_credentials_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mongo: SimpleNamespace
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)
    monkeypatch.setenv("ATLAS_DB_USERNAME", "envUser")
    monkeypatch.setenv("ATLAS_DB_PASSWORD", "envPass")

    result = runner.invoke(
        app, ["index", str(tmp_path), "--project-id", GROUP_A.id, "--cluster", "cluster0"]
    )

    assert result.exit_code == 0, result.output
    assert client.created_user_payload is None
    assert client.deleted_user_paths == []
    assert f"{quote('envUser', safe='')}:{quote('envPass', safe='')}@" in mongo.uris[0]


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--db-username", "only-user"],
        ["--db-password", "only-password"],
    ],
)
def test_index_requires_both_provided_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra_args: list[str],
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)

    result = runner.invoke(
        app,
        ["index", str(tmp_path), "--project-id", GROUP_A.id, "--cluster", "cluster0", *extra_args],
    )

    assert result.exit_code == 1
    assert "Provide both a database username and password" in result.output
    assert client.created_user_payload is None
    assert client.deleted_user_paths == []


def test_index_creates_pending_indexes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mongo: SimpleNamespace
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)

    result = runner.invoke(
        app, ["index", str(tmp_path), "--project-id", GROUP_A.id, "--cluster", "cluster0"]
    )

    assert result.exit_code == 0, result.output
    assert mongo.client.commands == [
        {
            "createIndexes": "example",
            "indexes": [{"name": "field_index", "key": {"field": 1}}],
        }
    ]
    assert "Index creation complete: 1 index(es) created." in result.output


def test_index_creation_honours_commit_quorum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mongo: SimpleNamespace
) -> None:
    (tmp_path / "indexes.json").write_text(
        json.dumps(
            {
                "database": "exampleDB",
                "collection": "example",
                "indexes": [{"keys": {"field": 1}, "commitQuorum": "majority"}],
            }
        ),
        encoding="utf-8",
    )
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)

    result = runner.invoke(
        app, ["index", str(tmp_path), "--project-id", GROUP_A.id, "--cluster", "cluster0"]
    )

    assert result.exit_code == 0, result.output
    assert mongo.client.commands[0]["commitQuorum"] == "majority"
    assert "commitQuorum" not in mongo.client.commands[0]["indexes"][0]


def test_index_prompts_to_delete_extra_indexes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)
    existing = [
        {"v": 2, "key": {"_id": 1}, "name": "_id_"},
        {"v": 2, "key": {"field": 1}, "name": "field_index"},
        {"v": 2, "key": {"extra": 1}, "name": "extra_1"},
    ]
    mongo_client = FakeMongoClient({"exampleDB": ["example"]}, {("exampleDB", "example"): existing})
    monkeypatch.setattr("atlas_ops.cli.create_client", lambda uri: mongo_client)
    handler = _CaptureHandler()
    package_logger = logging.getLogger("atlas_ops")
    package_logger.addHandler(handler)
    try:
        result = runner.invoke(
            app,
            [
                "index",
                str(tmp_path),
                "--project-id",
                GROUP_A.id,
                "--cluster",
                "cluster0",
            ],
            input="y\n",
        )
    finally:
        package_logger.removeHandler(handler)

    assert result.exit_code == 0, result.output
    assert any("not present in the definitions" in message for message in handler.messages)
    assert "Index change preview" in result.output
    assert re.search(
        r"^\| exampleDB\.example \|\s+0 \|\s+1 \|\s+1 \|$",
        result.output,
        re.MULTILINE,
    )
    assert "Delete the 1 index(es)" not in result.output
    assert mongo_client.commands == [{"dropIndexes": "example", "index": "extra_1"}]
    assert "Index deletion complete: 1 index(es) deleted." in result.output


def test_index_aborts_when_preview_declined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)
    existing = [
        {"v": 2, "key": {"_id": 1}, "name": "_id_"},
        {"v": 2, "key": {"field": 1}, "name": "field_index"},
        {"v": 2, "key": {"extra": 1}, "name": "extra_1"},
    ]
    mongo_client = FakeMongoClient({"exampleDB": ["example"]}, {("exampleDB", "example"): existing})
    monkeypatch.setattr("atlas_ops.cli.create_client", lambda uri: mongo_client)
    monkeypatch.setattr("atlas_ops.cli._confirm_changes", lambda: False)

    result = runner.invoke(
        app,
        [
            "index",
            str(tmp_path),
            "--project-id",
            GROUP_A.id,
            "--cluster",
            "cluster0",
        ],
    )

    assert result.exit_code == 1
    assert "Index change preview" in result.output
    assert "Aborted." in result.output
    assert mongo_client.commands == []
    assert "Index deletion complete" not in result.output


def test_index_creates_before_dropping_extra_indexes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)
    existing = [
        {"v": 2, "key": {"_id": 1}, "name": "_id_"},
        {"v": 2, "key": {"extra": 1}, "name": "extra_1"},
    ]
    mongo_client = FakeMongoClient({"exampleDB": ["example"]}, {("exampleDB", "example"): existing})
    monkeypatch.setattr("atlas_ops.cli.create_client", lambda uri: mongo_client)

    result = runner.invoke(
        app,
        [
            "index",
            str(tmp_path),
            "--project-id",
            GROUP_A.id,
            "--cluster",
            "cluster0",
        ],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    assert mongo_client.commands == [
        {
            "createIndexes": "example",
            "indexes": [{"name": "field_index", "key": {"field": 1}}],
        },
        {"dropIndexes": "example", "index": "extra_1"},
    ]
    assert "Index creation complete: 1 index(es) created." in result.output
    assert "Index deletion complete: 1 index(es) deleted." in result.output


def test_index_summary_table_counts_per_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "one.json").write_text(
        json.dumps(
            {
                "database": "exampleDB",
                "collection": "one",
                "indexes": [{"keys": {"a": 1}}, {"keys": {"b": 1}}],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "two.json").write_text(
        json.dumps(
            {
                "database": "exampleDB",
                "collection": "two",
                "indexes": [{"keys": {"c": 1}}],
            }
        ),
        encoding="utf-8",
    )
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)
    mongo_client = FakeMongoClient(
        {"exampleDB": ["one", "two"]},
        {
            ("exampleDB", "one"): [
                {"v": 2, "key": {"_id": 1}, "name": "_id_"},
                {"v": 2, "key": {"a": 1}, "name": "a_1"},
            ],
            ("exampleDB", "two"): [
                {"v": 2, "key": {"_id": 1}, "name": "_id_"},
                {"v": 2, "key": {"c": 1}, "name": "c_1"},
                {"v": 2, "key": {"old": 1}, "name": "old_1"},
            ],
        },
    )
    monkeypatch.setattr("atlas_ops.cli.create_client", lambda uri: mongo_client)

    result = runner.invoke(
        app,
        [
            "index",
            str(tmp_path),
            "--project-id",
            GROUP_A.id,
            "--cluster",
            "cluster0",
        ],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    assert "Index summary (2 collection(s)" in result.output
    assert re.search(
        r"^\| exampleDB\.one\s+\|\s+1 \|\s+1 \|\s+0 \|$",
        result.output,
        re.MULTILINE,
    )
    assert re.search(
        r"^\| exampleDB\.two\s+\|\s+0 \|\s+1 \|\s+1 \|$",
        result.output,
        re.MULTILINE,
    )
    assert re.search(
        r"^\| \*\*Total\*\*\s+\|\s+\*\*1\*\* "
        r"\|\s+\*\*2\*\* \|\s+\*\*1\*\* \|$",
        result.output,
        re.MULTILINE,
    )


def test_index_export_writes_files_without_mutating_indexes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)
    index_docs = [
        {"v": 2, "key": {"_id": 1}, "name": "_id_"},
        {"v": 2, "key": {"field": 1}, "name": "field_1", "unique": True},
    ]
    mongo_client = FakeMongoClient(
        {"exampleDB": ["example"]}, {("exampleDB", "example"): index_docs}
    )
    monkeypatch.setattr("atlas_ops.cli.create_client", lambda uri: mongo_client)

    result = runner.invoke(
        app,
        [
            "index",
            str(tmp_path),
            "-g",
            GROUP_A.id,
            "-c",
            "cluster0",
            "-e",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads((tmp_path / "exampleDB.example.json").read_text(encoding="utf-8"))
    assert payload["database"] == "exampleDB"
    assert payload["collection"] == "example"
    assert payload["indexes"] == [
        {
            "keys": {"field": 1},
            "options": {"name": "field_1", "unique": True},
        }
    ]
    assert mongo_client.commands == []
    assert "Export summary (1 collection(s)" in result.output
    assert re.search(r"^\| Collection\s+\| Exported \|$", result.output, re.MULTILINE)
    assert re.search(r"^\| exampleDB\.example\s+\|\s+1 \|$", result.output, re.MULTILINE)
    assert re.search(
        r"^\| \*\*Total\*\*\s+\|\s+\*\*1\*\* \|$",
        result.output,
        re.MULTILINE,
    )
    assert "Exported 1 index(es) from 1 collection(s)" in result.output
    assert "Index creation complete" not in result.output
    assert client.created_user_payload is not None
    assert len(client.deleted_user_paths) == 1


def test_index_prompts_when_collection_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)
    missing_client = FakeMongoClient({"exampleDB": []})
    monkeypatch.setattr("atlas_ops.cli.create_client", lambda uri: missing_client)

    result = runner.invoke(
        app,
        ["index", str(tmp_path), "--project-id", GROUP_A.id, "--cluster", "cluster0"],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    assert 'Collection "exampleDB.example" does not exist.' in result.output
    assert "Continue and let MongoDB create" in result.output
    assert "Index creation complete: 1 index(es) created." in result.output


def test_index_prompts_when_database_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)
    missing_client = FakeMongoClient({})
    monkeypatch.setattr("atlas_ops.cli.create_client", lambda uri: missing_client)

    result = runner.invoke(
        app,
        ["index", str(tmp_path), "--project-id", GROUP_A.id, "--cluster", "cluster0"],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    assert 'Database "exampleDB" does not exist' in result.output
    assert "Continue and let MongoDB create" in result.output


def test_index_aborts_when_missing_namespace_declined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)
    missing_client = FakeMongoClient({"exampleDB": []})
    monkeypatch.setattr("atlas_ops.cli.create_client", lambda uri: missing_client)

    result = runner.invoke(
        app,
        ["index", str(tmp_path), "--project-id", GROUP_A.id, "--cluster", "cluster0"],
        input="n\n",
    )

    assert result.exit_code == 1
    assert "Aborted." in result.output
    assert "Index creation complete" not in result.output
    assert len(client.deleted_user_paths) == 1


def test_index_connects_with_temporary_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mongo: SimpleNamespace
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)

    result = runner.invoke(
        app, ["index", str(tmp_path), "--project-id", GROUP_A.id, "--cluster", "cluster0"]
    )

    assert result.exit_code == 0, result.output
    payload = client.created_user_payload
    assert payload is not None
    assert len(mongo.uris) == 1
    credentials = f"{quote(payload['username'], safe='')}:{quote(payload['password'], safe='')}"
    assert f"{credentials}@" in mongo.uris[0]
    assert "authSource=admin" in mongo.uris[0]


def test_index_reports_mongo_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)

    def factory(uri: str) -> FakeMongoClient:
        raise PyMongoError("boom")

    monkeypatch.setattr("atlas_ops.cli.create_client", factory)

    result = runner.invoke(
        app, ["index", str(tmp_path), "--project-id", GROUP_A.id, "--cluster", "cluster0"]
    )

    assert result.exit_code == 1
    assert "MongoDB request failed: boom" in result.output
    assert len(client.deleted_user_paths) == 1


def test_index_warns_when_temporary_user_deletion_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_definitions(tmp_path)

    class FailingDeleteClient(FakeAtlasClient):
        def request(self, method: str, path: str, **kwargs: Any) -> Any:
            if method == "DELETE":
                raise AtlasAdminApiError(500, "boom", {"detail": "delete failed"})
            return super().request(method, path, **kwargs)

    client = FailingDeleteClient(
        [GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only")
    )
    _patch_client(monkeypatch, client)

    result = runner.invoke(
        app, ["index", str(tmp_path), "--project-id", GROUP_A.id, "--cluster", "cluster0"]
    )

    assert result.exit_code == 0, result.output
    assert "Could not delete temporary user" in result.output


def test_index_skips_identical_existing_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)
    existing = [{"v": 2, "key": {"field": 1}, "name": "field_index"}]
    mongo_client = FakeMongoClient({"exampleDB": ["example"]}, {("exampleDB", "example"): existing})
    monkeypatch.setattr("atlas_ops.cli.create_client", lambda uri: mongo_client)
    handler = _CaptureHandler()
    package_logger = logging.getLogger("atlas_ops")
    package_logger.addHandler(handler)
    try:
        result = runner.invoke(
            app, ["index", str(tmp_path), "--project-id", GROUP_A.id, "--cluster", "cluster0"]
        )
    finally:
        package_logger.removeHandler(handler)

    assert result.exit_code == 0, result.output
    assert any(
        "already exists with identical options; skipping." in message
        for message in handler.messages
    )
    assert not any("different options" in message for message in handler.messages)
    assert mongo_client.commands == []


def test_index_warns_about_existing_index_with_different_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)
    existing = [
        {
            "v": 2,
            "key": {"field": 1},
            "name": "other_index",
            "unique": True,
        }
    ]
    mongo_client = FakeMongoClient({"exampleDB": ["example"]}, {("exampleDB", "example"): existing})
    monkeypatch.setattr("atlas_ops.cli.create_client", lambda uri: mongo_client)
    handler = _CaptureHandler()
    package_logger = logging.getLogger("atlas_ops")
    package_logger.addHandler(handler)
    try:
        result = runner.invoke(
            app, ["index", str(tmp_path), "--project-id", GROUP_A.id, "--cluster", "cluster0"]
        )
    finally:
        package_logger.removeHandler(handler)

    assert result.exit_code == 0, result.output
    warning = next(message for message in handler.messages if "different options" in message)
    assert "other_index" in warning
    assert "skipping" in warning
    assert not any("identical options" in message for message in handler.messages)
    assert mongo_client.commands == []


def test_index_accepts_cluster_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)

    result = runner.invoke(
        app, ["index", str(tmp_path), "--project-id", GROUP_A.id, "--cluster", CLUSTER.id]
    )

    assert result.exit_code == 0, result.output
    assert "mongodb://only" in result.output


def test_index_uses_environment_fallbacks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)
    monkeypatch.setenv("ATLAS_PROJECT_ID", GROUP_A.id)
    monkeypatch.setenv("ATLAS_CLUSTER_NAME", "cluster0")

    result = runner.invoke(app, ["index", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert client.get_group_calls == [GROUP_A.id]
    assert "mongodb://only" in result.output


def test_index_options_override_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)
    monkeypatch.setenv("ATLAS_PROJECT_ID", "f" * 24)
    monkeypatch.setenv("ATLAS_CLUSTER_NAME", "other-cluster")

    result = runner.invoke(
        app,
        [
            "index",
            str(tmp_path),
            "--project-id",
            GROUP_A.id,
            "--cluster",
            "cluster0",
        ],
    )

    assert result.exit_code == 0, result.output
    assert client.get_group_calls == [GROUP_A.id]
    assert client.get_cluster_calls == [("cluster0", GROUP_A.id)]


def test_index_prompts_for_missing_project_and_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)
    _clear_target_env(monkeypatch)

    result = runner.invoke(
        app,
        ["index", str(tmp_path)],
        input=f"{GROUP_A.id}\ncluster0\n",
    )

    assert result.exit_code == 0, result.output
    assert "Project ID (24 hexadecimal characters)" in result.output
    assert "Cluster ID or name" in result.output
    assert client.get_group_calls == [GROUP_A.id]


def test_index_reprompts_for_invalid_project_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)
    _clear_target_env(monkeypatch)

    result = runner.invoke(
        app,
        ["index", str(tmp_path)],
        input=f"not-an-id\n{GROUP_A.id}\ncluster0\n",
    )

    assert result.exit_code == 0, result.output
    assert "Project ID must be 24 hexadecimal characters." in result.output
    assert client.get_group_calls == [GROUP_A.id]


def test_index_rejects_invalid_project_id_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)
    monkeypatch.setenv("ATLAS_PROJECT_ID", "not-an-id")
    monkeypatch.setenv("ATLAS_CLUSTER_NAME", "cluster0")

    result = runner.invoke(app, ["index", str(tmp_path)])

    assert result.exit_code == 1
    assert "Invalid project ID" in result.output
    assert client.get_group_calls == []


def test_index_reports_project_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([], {}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)

    result = runner.invoke(
        app, ["index", str(tmp_path), "--project-id", GROUP_A.id, "--cluster", "cluster0"]
    )

    assert result.exit_code == 1
    assert "not found or is not accessible" in result.output
    assert client.deleted_user_paths == []


def test_index_auto_selects_single_connection_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)

    result = runner.invoke(
        app, ["index", str(tmp_path), "--project-id", GROUP_A.id, "--cluster", "cluster0"], input=""
    )

    assert result.exit_code == 0, result.output
    assert "Available connection strings" not in result.output
    assert "mongodb://only" in result.output


def test_index_asks_when_multiple_clusters_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_definitions(tmp_path)
    duplicate = SimpleNamespace(id="e" * 24, name="cluster0")
    client = FakeAtlasClient(
        [GROUP_A],
        {GROUP_A.id: [CLUSTER, duplicate]},
        _detail(standard="mongodb://only"),
    )
    _patch_client(monkeypatch, client)

    result = runner.invoke(
        app,
        ["index", str(tmp_path), "--project-id", GROUP_A.id, "--cluster", "cluster0"],
        input="2\n",
    )

    assert result.exit_code == 0, result.output
    assert "Multiple clusters match" in result.output
    assert client.get_cluster_calls == [("cluster0", GROUP_A.id)]


def test_index_reprompts_on_invalid_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient(
        [GROUP_A],
        {GROUP_A.id: [CLUSTER]},
        _detail(standard="mongodb://direct", standardSrv="mongodb+srv://srv"),
    )
    _patch_client(monkeypatch, client)

    result = runner.invoke(
        app,
        ["index", str(tmp_path), "--project-id", GROUP_A.id, "--cluster", "cluster0"],
        input="9\n2\n",
    )

    assert result.exit_code == 0, result.output
    assert "Enter a number between 1 and 2" in result.output
    assert "mongodb+srv://srv" in result.output


def test_index_reports_missing_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_definitions(tmp_path)
    monkeypatch.delenv("ATLAS_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("ATLAS_PRIVATE_KEY", raising=False)
    _clear_target_env(monkeypatch)

    result = runner.invoke(app, ["index", str(tmp_path)])

    assert result.exit_code == 1
    assert "ATLAS_PUBLIC_KEY" in result.output
    assert "ATLAS_PRIVATE_KEY" in result.output
    assert "Project ID (24 hexadecimal characters)" not in result.output


def test_index_validates_definitions_before_calling_atlas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "broken.json").write_text(
        json.dumps({"database": "db", "collection": "coll", "indexes": [{}]}),
        encoding="utf-8",
    )
    called: list[bool] = []
    monkeypatch.setattr(
        "atlas_ops.cli.client_from_env",
        lambda: called.append(True) or pytest.fail("Atlas client must not be created"),
    )

    result = runner.invoke(app, ["index", str(tmp_path)])

    assert result.exit_code == 1
    assert "keys" in result.output
    assert called == []


def test_index_reports_empty_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []
    monkeypatch.setattr(
        "atlas_ops.cli.client_from_env",
        lambda: called.append(True) or pytest.fail("Atlas client must not be created"),
    )

    result = runner.invoke(app, ["index", str(tmp_path)])

    assert result.exit_code == 1
    assert "No .json files found" in result.output
    assert called == []


def test_index_reports_unknown_cluster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://x"))
    _patch_client(monkeypatch, client)

    result = runner.invoke(
        app, ["index", str(tmp_path), "--project-id", GROUP_A.id, "--cluster", "missing"]
    )

    assert result.exit_code == 1
    assert "No cluster found for 'missing'" in result.output


def test_index_reports_cluster_without_connection_strings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail())
    _patch_client(monkeypatch, client)

    result = runner.invoke(
        app, ["index", str(tmp_path), "--project-id", GROUP_A.id, "--cluster", "cluster0"]
    )

    assert result.exit_code == 1
    assert "no connection strings" in result.output


def test_index_reports_atlas_api_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_definitions(tmp_path)

    class FailingClient(FakeAtlasClient):
        def _get_group(self, group_id: str, **_: Any) -> Any:
            raise AtlasAdminApiError(401, "Unauthorized", {"detail": "invalid credentials"})

    client = FailingClient([], {}, _detail(standard="mongodb://x"))
    _patch_client(monkeypatch, client)

    result = runner.invoke(
        app, ["index", str(tmp_path), "--project-id", GROUP_A.id, "--cluster", "cluster0"]
    )

    assert result.exit_code == 1
    assert "HTTP 401" in result.output
    assert "invalid credentials" in result.output


def test_index_rejects_missing_directory(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["index", str(tmp_path / "missing"), "--project-id", GROUP_A.id, "--cluster", "cluster0"],
    )

    assert result.exit_code == 2
    assert "does not exist" in result.output


class _CaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_index_logs_found_cluster_and_connection_strings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient(
        [GROUP_A],
        {GROUP_A.id: [CLUSTER]},
        _detail(standard="mongodb://direct", standardSrv="mongodb+srv://srv"),
    )
    _patch_client(monkeypatch, client)
    handler = _CaptureHandler()
    package_logger = logging.getLogger("atlas_ops")
    package_logger.addHandler(handler)
    try:
        result = runner.invoke(
            app,
            ["index", str(tmp_path), "--project-id", GROUP_A.id, "--cluster", "cluster0"],
            input="2\n",
        )
    finally:
        package_logger.removeHandler(handler)

    assert result.exit_code == 0, result.output
    assert any("Found cluster [green]cluster0[/green]" in message for message in handler.messages)
    assert any("in project [green]Project A[/green]" in message for message in handler.messages)
    assert any("by name" in message for message in handler.messages)
    assert any("Found 2 connection string(s)" in message for message in handler.messages)
    assert any(
        '[cyan]standard_srv[/cyan]: "mongodb+srv://srv"' in message for message in handler.messages
    )
    assert any(
        'Selected connection string [cyan]standard_srv[/cyan]: "mongodb+srv://srv"' in message
        for message in handler.messages
    )
    assert any("Created temporary user [cyan]" in message for message in handler.messages)
    assert any("Waiting for temporary user [cyan]" in message for message in handler.messages)
    assert any("Temporary user is active." in message for message in handler.messages)
    assert any("Deleting temporary user [cyan]" in message for message in handler.messages)
    assert any("Temporary user deleted." in message for message in handler.messages)
    assert client.created_user_payload is not None
    password = client.created_user_payload["password"]
    assert all(password not in message for message in handler.messages)


def test_index_logs_lookup_by_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)
    handler = _CaptureHandler()
    package_logger = logging.getLogger("atlas_ops")
    package_logger.addHandler(handler)
    try:
        result = runner.invoke(
            app, ["index", str(tmp_path), "--project-id", GROUP_A.id, "--cluster", CLUSTER.id]
        )
    finally:
        package_logger.removeHandler(handler)

    assert result.exit_code == 0, result.output
    assert any("by ID" in message for message in handler.messages)


def test_index_logs_when_values_come_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)
    monkeypatch.setenv("ATLAS_PROJECT_ID", GROUP_A.id)
    monkeypatch.setenv("ATLAS_CLUSTER_NAME", "cluster0")
    handler = _CaptureHandler()
    package_logger = logging.getLogger("atlas_ops")
    package_logger.addHandler(handler)
    try:
        result = runner.invoke(app, ["index", str(tmp_path)])
    finally:
        package_logger.removeHandler(handler)

    assert result.exit_code == 0, result.output
    assert any(
        f"Using project {GROUP_A.id} from ATLAS_PROJECT_ID" in message
        for message in handler.messages
    )
    assert any(
        "Using cluster [green]cluster0[/green] from ATLAS_CLUSTER_NAME" in message
        for message in handler.messages
    )


def test_export_table_is_padded_for_bash() -> None:
    table = _format_export_table([("exampleDB.orders", 1), ("exampleDB.users", 2)])

    assert table == (
        "| Collection       | Exported |\n"
        "| ---------------- | -------: |\n"
        "| exampleDB.orders |        1 |\n"
        "| exampleDB.users  |        2 |\n"
        "| **Total**        |    **3** |"
    )


def test_export_table_escapes_pipes() -> None:
    table = _format_export_table([("db.a|b", 1)])

    assert "| db.a\\|b" in table


def test_search_help_documents_arguments() -> None:
    result = runner.invoke(app, ["search", "--help"])

    assert result.exit_code == 0
    assert "directory" in result.stdout
    assert "--project-id" in result.stdout
    assert "-g" in result.stdout
    assert "--cluster" in result.stdout
    assert "-c" in result.stdout
    assert "--export" in result.stdout
    assert "-e" in result.stdout


def test_search_exports_search_indexes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail())
    client.search_index_payload = [
        {
            "database": "sample_mflix",
            "collectionName": "movies",
            "indexID": "id-1",
            "name": "default",
            "type": "search",
            "status": "READY",
            "queryable": True,
            "latestDefinition": {"mappings": {"dynamic": True}},
        }
    ]
    _patch_client(monkeypatch, client)

    result = runner.invoke(
        app,
        [
            "search",
            str(tmp_path),
            "-g",
            GROUP_A.id,
            "-c",
            "cluster0",
            "-e",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads((tmp_path / "sample_mflix.movies.json").read_text(encoding="utf-8"))
    assert payload == {
        "database": "sample_mflix",
        "collection": "movies",
        "indexes": [
            {
                "name": "default",
                "type": "search",
                "definition": {"mappings": {"dynamic": True}},
            }
        ],
    }
    assert client.search_index_requests[0][0:2] == (
        "GET",
        f"/groups/{GROUP_A.id}/clusters/cluster0/search/indexes",
    )
    assert client.search_index_requests[0][2]["accept"] == "application/vnd.atlas.2024-05-30+json"
    assert client.created_user_payload is None
    assert client.deleted_user_paths == []
    assert "Search export summary (1 collection(s)" in result.output
    assert re.search(
        r"^\| sample_mflix\.movies\s+\|\s+1 \|$",
        result.output,
        re.MULTILINE,
    )
    assert re.search(
        r"^\| \*\*Total\*\*\s+\|\s+\*\*1\*\* \|$",
        result.output,
        re.MULTILINE,
    )
    assert "Exported 1 search index(es) from 1 collection(s)" in result.output


def test_search_sync_requires_definitions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []
    monkeypatch.setattr(
        "atlas_ops.cli.client_from_env",
        lambda: called.append(True) or pytest.fail("Atlas client must not be created"),
    )

    result = runner.invoke(
        app,
        [
            "search",
            str(tmp_path),
            "-g",
            GROUP_A.id,
            "-c",
            "cluster0",
        ],
    )

    assert result.exit_code == 1
    assert "No .json files found" in result.output
    assert called == []


def test_search_reports_missing_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ATLAS_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("ATLAS_PRIVATE_KEY", raising=False)
    _clear_target_env(monkeypatch)

    result = runner.invoke(app, ["search", str(tmp_path), "--export"])

    assert result.exit_code == 1
    assert "ATLAS_PUBLIC_KEY" in result.output
    assert "ATLAS_PRIVATE_KEY" in result.output
    assert "Project ID (24 hexadecimal characters)" not in result.output


def test_search_uses_environment_targets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail())
    _patch_client(monkeypatch, client)
    monkeypatch.setenv("ATLAS_PROJECT_ID", GROUP_A.id)
    monkeypatch.setenv("ATLAS_CLUSTER_NAME", "cluster0")

    result = runner.invoke(app, ["search", str(tmp_path), "--export"])

    assert result.exit_code == 0, result.output
    assert client.get_group_calls == [GROUP_A.id]
    assert client.search_index_requests[0][1] == (
        f"/groups/{GROUP_A.id}/clusters/cluster0/search/indexes"
    )
    assert "Exported 0 search index(es) from 0 collection(s)" in result.output


class FakeAppServicesClient:
    def __init__(
        self,
        apps_by_product: dict[str | None, list[Any]],
        services_by_app: dict[str, list[Any]],
        triggers_by_app: dict[str, list[Any]],
        functions_by_app: dict[str, list[Any]] | None = None,
        function_definitions: dict[str, Any] | None = None,
    ) -> None:
        self._apps_by_product = apps_by_product
        self._services_by_app = services_by_app
        self._triggers_by_app = triggers_by_app
        self._functions_by_app = functions_by_app or {}
        self._function_definitions = function_definitions or {}
        self.app_calls: list[tuple[str, str | None]] = []
        self.service_calls: list[tuple[str, str]] = []
        self.trigger_calls: list[tuple[str, str]] = []
        self.function_list_calls: list[tuple[str, str]] = []
        self.function_get_calls: list[tuple[str, str, str]] = []
        self.apps = SimpleNamespace(admin_list_applications=self._list_apps)
        self.services = SimpleNamespace(admin_list_services=self._list_services)
        self.triggers = SimpleNamespace(admin_list_triggers=self._list_triggers)
        self.functions = SimpleNamespace(
            admin_list_functions=self._list_functions,
            admin_get_function=self._get_function,
        )

    def _list_apps(self, group_id: str, *, product: str | None = None) -> list[Any]:
        self.app_calls.append((group_id, product))
        return self._apps_by_product.get(product, [])

    def _list_services(self, app_id: str, group_id: str) -> list[Any]:
        self.service_calls.append((app_id, group_id))
        return self._services_by_app.get(app_id, [])

    def _list_triggers(self, app_id: str, group_id: str) -> list[Any]:
        self.trigger_calls.append((app_id, group_id))
        return self._triggers_by_app.get(app_id, [])

    def _list_functions(self, app_id: str, group_id: str) -> list[Any]:
        self.function_list_calls.append((app_id, group_id))
        return self._functions_by_app.get(app_id, [])

    def _get_function(self, app_id: str, function_id: str, group_id: str) -> Any:
        self.function_get_calls.append((app_id, function_id, group_id))
        return self._function_definitions[function_id]

    def __enter__(self) -> FakeAppServicesClient:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def test_trigger_help_documents_arguments() -> None:
    result = runner.invoke(app, ["trigger", "--help"])

    assert result.exit_code == 0
    assert "directory" in result.stdout
    assert "--project-id" in result.stdout
    assert "-g" in result.stdout
    assert "--cluster" in result.stdout
    assert "-c" in result.stdout
    assert "--export" in result.stdout
    assert "-e" in result.stdout


def test_trigger_exports_definitions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail())
    _patch_client(monkeypatch, client)
    app_client = FakeAppServicesClient(
        {
            None: [SimpleNamespace(id="app-1", name="my-app")],
            "atlas": [],
        },
        {
            "app-1": [
                {
                    "_id": "svc-1",
                    "name": "cluster0",
                    "type": "mongodb-atlas",
                }
            ]
        },
        {
            "app-1": [
                {
                    "_id": "trigger-1",
                    "name": "on-insert",
                    "type": "DATABASE",
                    "function_id": "fn-1",
                    "config": {
                        "service_id": "svc-1",
                        "database": "db",
                        "collection": "coll",
                    },
                }
            ]
        },
        functions_by_app={"app-1": [{"_id": "fn-1", "name": "on-insert-function"}]},
        function_definitions={
            "fn-1": {
                "_id": "fn-1",
                "name": "on-insert-function",
                "private": True,
                "source": "exports = function() { return 1; };",
                "last_modified": 123,
            }
        },
    )
    monkeypatch.setattr("atlas_ops.cli.app_services_client_from_env", lambda: app_client)

    result = runner.invoke(
        app,
        [
            "trigger",
            str(tmp_path),
            "-g",
            GROUP_A.id,
            "-c",
            "cluster0",
            "-e",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads((tmp_path / "db.coll.json").read_text(encoding="utf-8"))
    assert payload == {
        "database": "db",
        "collection": "coll",
        "triggers": [
            {
                "appId": "app-1",
                "appName": "my-app",
                "trigger": {
                    "name": "on-insert",
                    "type": "DATABASE",
                    "function_id": "fn-1",
                    "config": {
                        "service_id": "svc-1",
                        "database": "db",
                        "collection": "coll",
                    },
                },
                "function": {
                    "name": "on-insert-function",
                    "private": True,
                    "source": "exports = function() { return 1; };",
                },
            }
        ],
    }
    assert app_client.app_calls == [(GROUP_A.id, None), (GROUP_A.id, "atlas")]
    assert app_client.service_calls == [("app-1", GROUP_A.id)]
    assert app_client.trigger_calls == [("app-1", GROUP_A.id)]
    assert app_client.function_list_calls == [("app-1", GROUP_A.id)]
    assert app_client.function_get_calls == [("app-1", "fn-1", GROUP_A.id)]
    assert client.created_user_payload is None
    assert "Trigger export summary (1 collection(s)" in result.output
    assert re.search(r"^\| db\.coll\s+\|\s+1 \|$", result.output, re.MULTILINE)
    assert re.search(
        r"^\| \*\*Total\*\*\s+\|\s+\*\*1\*\* \|$",
        result.output,
        re.MULTILINE,
    )
    assert "Exported 1 trigger(s) from 1 collection(s) across 1 app(s)" in result.output


def test_trigger_requires_export(tmp_path: Path) -> None:
    result = runner.invoke(app, ["trigger", str(tmp_path), "-g", GROUP_A.id])

    assert result.exit_code == 1
    assert "only --export/-e" in result.output


def test_trigger_reports_missing_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ATLAS_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("ATLAS_PRIVATE_KEY", raising=False)
    _clear_target_env(monkeypatch)

    result = runner.invoke(app, ["trigger", str(tmp_path), "--export"])

    assert result.exit_code == 1
    assert "ATLAS_PUBLIC_KEY" in result.output
    assert "ATLAS_PRIVATE_KEY" in result.output
    assert "Project ID (24 hexadecimal characters)" not in result.output


def test_trigger_uses_environment_targets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail())
    _patch_client(monkeypatch, client)
    app_client = FakeAppServicesClient({None: [], "atlas": []}, {}, {})
    monkeypatch.setattr("atlas_ops.cli.app_services_client_from_env", lambda: app_client)
    monkeypatch.setenv("ATLAS_PROJECT_ID", GROUP_A.id)
    monkeypatch.setenv("ATLAS_CLUSTER_NAME", "cluster0")

    result = runner.invoke(app, ["trigger", str(tmp_path), "--export"])

    assert result.exit_code == 0, result.output
    assert client.get_group_calls == [GROUP_A.id]
    assert app_client.app_calls == [(GROUP_A.id, None), (GROUP_A.id, "atlas")]
    assert "Exported 0 trigger(s) from 0 collection(s) across 0 app(s)" in result.output


def test_search_syncs_definitions_and_deletes_extras(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "search.json").write_text(
        json.dumps(
            {
                "database": "exampleDB",
                "collection": "example",
                "indexes": [
                    {
                        "name": "same",
                        "type": "search",
                        "definition": {"mappings": {"dynamic": True}},
                    },
                    {
                        "name": "changed",
                        "type": "search",
                        "definition": {"mappings": {"dynamic": False}},
                    },
                    {
                        "name": "new",
                        "type": "vectorSearch",
                        "definition": {"fields": [{"type": "vector"}]},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail())
    client.search_index_payload = [
        {
            "database": "exampleDB",
            "collectionName": "example",
            "name": "same",
            "indexID": "id-same",
            "type": "search",
            "latestDefinition": {"mappings": {"dynamic": True}},
        },
        {
            "database": "exampleDB",
            "collectionName": "example",
            "name": "changed",
            "indexID": "id-changed",
            "type": "search",
            "latestDefinition": {"mappings": {"dynamic": True}},
        },
        {
            "database": "exampleDB",
            "collectionName": "example",
            "name": "old",
            "indexID": "id-old",
            "type": "search",
            "latestDefinition": {"mappings": {"dynamic": True}},
        },
    ]
    _patch_client(monkeypatch, client)

    result = runner.invoke(
        app,
        [
            "search",
            str(tmp_path),
            "-g",
            GROUP_A.id,
            "-c",
            "cluster0",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Search change preview" in result.output
    assert "Will create search index" not in result.output
    assert "Delete the 1 search index(es)" not in result.output
    assert [call["body"].name for call in client.search_create_calls] == ["new"]
    created = client.search_create_calls[0]["body"].model_dump(by_alias=True, exclude_none=True)
    assert created == {
        "database": "exampleDB",
        "collectionName": "example",
        "name": "new",
        "type": "vectorSearch",
        "fields": [{"type": "vector"}],
    }
    assert client.search_update_calls[0]["index_id"] == "id-changed"
    assert client.search_update_calls[0]["body"].name == "changed"
    assert client.search_delete_calls == [
        {
            "cluster_name": "cluster0",
            "group_id": GROUP_A.id,
            "index_id": "id-old",
        }
    ]
    assert "Search sync summary (1 collection(s)" in result.output
    assert re.search(
        r"^\| exampleDB\.example \|\s+1 \|\s+1 \|\s+1 \|\s+1 \|$",
        result.output,
        re.MULTILINE,
    )
    assert re.search(
        r"^\| \*\*Total\*\*\s+\|\s+\*\*1\*\* \|\s+\*\*1\*\* "
        r"\|\s+\*\*1\*\* \|\s+\*\*1\*\* \|$",
        result.output,
        re.MULTILINE,
    )
    assert "Search sync complete: 1 created, 1 updated, 1 skipped, 1 deleted." in result.output
    assert client.created_user_payload is None


def test_search_aborts_when_preview_declined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "search.json").write_text(
        json.dumps(
            {
                "database": "exampleDB",
                "collection": "example",
                "indexes": [
                    {
                        "name": "new",
                        "type": "search",
                        "definition": {"mappings": {"dynamic": True}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail())
    _patch_client(monkeypatch, client)
    monkeypatch.setattr("atlas_ops.cli._confirm_changes", lambda: False)

    result = runner.invoke(
        app,
        [
            "search",
            str(tmp_path),
            "-g",
            GROUP_A.id,
            "-c",
            "cluster0",
        ],
    )

    assert result.exit_code == 1
    assert "Search change preview" in result.output
    assert "Aborted." in result.output
    assert client.search_create_calls == []
    assert client.search_update_calls == []
    assert client.search_delete_calls == []


def test_summary_numbers_use_change_colors() -> None:
    index_table = _format_index_table([("exampleDB.example", 1, 2, 3)])
    assert "\x1b[32m1\x1b[0m" in index_table
    assert "\x1b[31m3\x1b[0m" in index_table
    assert "\x1b[32mCreated" not in index_table

    search_table = _format_search_sync_table([("exampleDB.example", 1, 2, 3, 4)])
    assert "\x1b[32m1\x1b[0m" in search_table
    assert "\x1b[33m2\x1b[0m" in search_table
    assert "\x1b[31m4\x1b[0m" in search_table
    assert "\x1b[33mUpdated" not in search_table


def test_summary_zero_counts_are_not_colored() -> None:
    index_table = _format_index_table([("exampleDB.example", 0, 0, 0)])
    assert "\x1b[" not in index_table

    search_table = _format_search_sync_table([("exampleDB.example", 0, 2, 0, 0)])
    assert "\x1b[32m" not in search_table
    assert "\x1b[31m" not in search_table
    assert "\x1b[33m2\x1b[0m" in search_table
