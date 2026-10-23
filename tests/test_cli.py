from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pyatlassdk import AtlasApiError
from pyatlassdk.models.cloud_database_user import CloudDatabaseUser
from pyatlassdk.models.cluster_description20240805 import ClusterDescription20240805
from typer.testing import CliRunner

from sie_atlas_ops import __version__
from sie_atlas_ops.cli import app

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
        self.projects = SimpleNamespace(get_group=self._get_group)
        self.clusters = SimpleNamespace(
            list_group_clusters=self._list_clusters,
            get_group_cluster=self._get_cluster,
        )

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.created_user_payload = kwargs["json"]
        return CloudDatabaseUser.model_validate(kwargs["json"])

    def _get_group(self, group_id: str, **_: Any) -> Any:
        self.get_group_calls.append(group_id)
        group = self._groups.get(group_id)
        if group is None:
            raise AtlasApiError(404, "Not Found", {"detail": "project not found"})
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
    monkeypatch.setattr("sie_atlas_ops.cli.client_from_env", lambda: client)


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
    assert "project" in result.stdout
    assert "cluster" in result.stdout


def test_index_selects_connection_string(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient(
        [GROUP_A],
        {GROUP_A.id: [CLUSTER]},
        _detail(standard="mongodb://direct", standardSrv="mongodb+srv://srv"),
    )
    _patch_client(monkeypatch, client)

    result = runner.invoke(app, ["index", str(tmp_path), GROUP_A.id, "cluster0"], input="2\n")

    assert result.exit_code == 0, result.output
    assert 'Connection: "mongodb+srv://srv"' in result.output
    assert "Project A" in result.output
    assert "exampleDB.example" in result.output
    assert "Index creation is not implemented" in result.output
    assert client.get_group_calls == [GROUP_A.id]
    assert client.get_cluster_calls == [("cluster0", GROUP_A.id)]


def test_index_creates_temporary_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)

    result = runner.invoke(app, ["index", str(tmp_path), GROUP_A.id, "cluster0"])

    assert result.exit_code == 0, result.output
    payload = client.created_user_payload
    assert payload is not None
    assert payload["groupId"] == GROUP_A.id
    assert payload["databaseName"] == "admin"
    assert payload["roles"] == [{"databaseName": "admin", "roleName": "readWriteAnyDatabase"}]
    assert payload["scopes"] == [{"name": "cluster0", "type": "CLUSTER"}]
    assert payload["username"].startswith("sie_atlas_ops_")
    assert len(payload["password"]) >= 8
    json.dumps(payload)  # the body must be JSON-serializable

    delete_after = datetime.fromisoformat(payload["deleteAfterDate"].replace("Z", "+00:00"))
    remaining = delete_after - datetime.now(timezone.utc)
    assert timedelta(hours=5, minutes=59) <= remaining <= timedelta(hours=6, minutes=1)

    assert f"Temporary user: {payload['username']}" in result.output
    assert f"Password:       {payload['password']}" in result.output
    assert "Expires:" in result.output


def test_index_accepts_cluster_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)

    result = runner.invoke(app, ["index", str(tmp_path), GROUP_A.id, CLUSTER.id])

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


def test_index_empty_positionals_fall_back_to_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)
    monkeypatch.setenv("ATLAS_PROJECT_ID", GROUP_A.id)
    monkeypatch.setenv("ATLAS_CLUSTER_NAME", "cluster0")

    result = runner.invoke(app, ["index", str(tmp_path), "", ""])

    assert result.exit_code == 0, result.output
    assert client.get_group_calls == [GROUP_A.id]


def test_index_single_positional_is_treated_as_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)
    monkeypatch.setenv("ATLAS_PROJECT_ID", GROUP_A.id)
    monkeypatch.delenv("ATLAS_CLUSTER_NAME", raising=False)

    result = runner.invoke(app, ["index", str(tmp_path), "cluster0"])

    assert result.exit_code == 0, result.output
    assert client.get_group_calls == [GROUP_A.id]


def test_index_single_cluster_id_with_env_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)
    monkeypatch.setenv("ATLAS_PROJECT_ID", GROUP_A.id)
    monkeypatch.delenv("ATLAS_CLUSTER_NAME", raising=False)

    result = runner.invoke(app, ["index", str(tmp_path), CLUSTER.id])

    assert result.exit_code == 0, result.output
    assert client.get_group_calls == [GROUP_A.id]
    assert client.get_cluster_calls == [("cluster0", GROUP_A.id)]


def test_index_single_project_id_with_env_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)
    monkeypatch.setenv("ATLAS_CLUSTER_NAME", "cluster0")
    monkeypatch.delenv("ATLAS_PROJECT_ID", raising=False)

    result = runner.invoke(app, ["index", str(tmp_path), GROUP_A.id])

    assert result.exit_code == 0, result.output
    assert client.get_group_calls == [GROUP_A.id]


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

    result = runner.invoke(app, ["index", str(tmp_path), GROUP_A.id, "cluster0"])

    assert result.exit_code == 1
    assert "not found or is not accessible" in result.output


def test_index_auto_selects_single_connection_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)

    result = runner.invoke(app, ["index", str(tmp_path), GROUP_A.id, "cluster0"], input="")

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

    result = runner.invoke(app, ["index", str(tmp_path), GROUP_A.id, "cluster0"], input="2\n")

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

    result = runner.invoke(app, ["index", str(tmp_path), GROUP_A.id, "cluster0"], input="9\n2\n")

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
        "sie_atlas_ops.cli.client_from_env",
        lambda: called.append(True) or pytest.fail("Atlas client must not be created"),
    )

    result = runner.invoke(app, ["index", str(tmp_path)])

    assert result.exit_code == 1
    assert "keys" in result.output
    assert called == []


def test_index_reports_empty_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []
    monkeypatch.setattr(
        "sie_atlas_ops.cli.client_from_env",
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

    result = runner.invoke(app, ["index", str(tmp_path), GROUP_A.id, "missing"])

    assert result.exit_code == 1
    assert "No cluster found for 'missing'" in result.output


def test_index_reports_cluster_without_connection_strings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail())
    _patch_client(monkeypatch, client)

    result = runner.invoke(app, ["index", str(tmp_path), GROUP_A.id, "cluster0"])

    assert result.exit_code == 1
    assert "no connection strings" in result.output


def test_index_reports_atlas_api_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_definitions(tmp_path)

    class FailingClient(FakeAtlasClient):
        def _get_group(self, group_id: str, **_: Any) -> Any:
            raise AtlasApiError(401, "Unauthorized", {"detail": "invalid credentials"})

    client = FailingClient([], {}, _detail(standard="mongodb://x"))
    _patch_client(monkeypatch, client)

    result = runner.invoke(app, ["index", str(tmp_path), GROUP_A.id, "cluster0"])

    assert result.exit_code == 1
    assert "HTTP 401" in result.output
    assert "invalid credentials" in result.output


def test_index_rejects_missing_directory(tmp_path: Path) -> None:
    result = runner.invoke(app, ["index", str(tmp_path / "missing"), GROUP_A.id, "cluster0"])

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
    package_logger = logging.getLogger("sie_atlas_ops")
    package_logger.addHandler(handler)
    try:
        result = runner.invoke(
            app,
            ["index", str(tmp_path), GROUP_A.id, "cluster0"],
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
    password = client.created_user_payload["password"]
    assert all(password not in message for message in handler.messages)


def test_index_logs_lookup_by_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)
    handler = _CaptureHandler()
    package_logger = logging.getLogger("sie_atlas_ops")
    package_logger.addHandler(handler)
    try:
        result = runner.invoke(app, ["index", str(tmp_path), GROUP_A.id, CLUSTER.id])
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
    package_logger = logging.getLogger("sie_atlas_ops")
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
