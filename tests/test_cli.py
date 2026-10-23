from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pyatlassdk import AtlasApiError
from pyatlassdk.models.cluster_description20240805 import ClusterDescription20240805
from typer.testing import CliRunner

from sie_atlas_ops import __version__
from sie_atlas_ops.cli import app

runner = CliRunner()

GROUP_A = SimpleNamespace(id="a" * 24, name="Project A")
GROUP_B = SimpleNamespace(id="c" * 24, name="Project B")
CLUSTER = SimpleNamespace(id="b" * 24, name="cluster0")


class FakeAtlasClient:
    def __init__(
        self,
        groups: list[Any],
        clusters_by_group: dict[str, list[Any]],
        detail: ClusterDescription20240805,
    ) -> None:
        self._groups = groups
        self._clusters = clusters_by_group
        self._detail = detail
        self.get_cluster_calls: list[tuple[str, str]] = []
        self.projects = SimpleNamespace(list_groups=self._list_groups)
        self.clusters = SimpleNamespace(
            list_group_clusters=self._list_clusters,
            get_group_cluster=self._get_cluster,
        )

    def _list_groups(self, **_: Any) -> SimpleNamespace:
        return SimpleNamespace(results=self._groups, total_count=len(self._groups))

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
    assert "cluster" in result.stdout


def test_index_selects_connection_string(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient(
        [GROUP_A],
        {GROUP_A.id: [CLUSTER]},
        _detail(standard="mongodb://direct", standardSrv="mongodb+srv://srv"),
    )
    _patch_client(monkeypatch, client)

    result = runner.invoke(app, ["index", str(tmp_path), "cluster0"], input="2\n")

    assert result.exit_code == 0, result.output
    assert "mongodb+srv://srv" in result.output
    assert "Project A" in result.output
    assert "exampleDB.example" in result.output
    assert "Index creation is not implemented" in result.output
    assert client.get_cluster_calls == [("cluster0", GROUP_A.id)]


def test_index_accepts_cluster_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)

    result = runner.invoke(app, ["index", str(tmp_path), CLUSTER.id])

    assert result.exit_code == 0, result.output
    assert "mongodb://only" in result.output


def test_index_auto_selects_single_connection_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)

    result = runner.invoke(app, ["index", str(tmp_path), "cluster0"], input="")

    assert result.exit_code == 0, result.output
    assert "Available connection strings" not in result.output
    assert "mongodb://only" in result.output


def test_index_asks_when_multiple_clusters_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient(
        [GROUP_A, GROUP_B],
        {GROUP_A.id: [CLUSTER], GROUP_B.id: [CLUSTER]},
        _detail(standard="mongodb://only"),
    )
    _patch_client(monkeypatch, client)

    result = runner.invoke(app, ["index", str(tmp_path), "cluster0"], input="2\n")

    assert result.exit_code == 0, result.output
    assert "Multiple clusters match" in result.output
    assert client.get_cluster_calls == [("cluster0", GROUP_B.id)]


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

    result = runner.invoke(app, ["index", str(tmp_path), "cluster0"], input="9\n2\n")

    assert result.exit_code == 0, result.output
    assert "Enter a number between 1 and 2" in result.output
    assert "mongodb+srv://srv" in result.output


def test_index_reports_missing_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_definitions(tmp_path)
    monkeypatch.delenv("ATLAS_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("ATLAS_PRIVATE_KEY", raising=False)

    result = runner.invoke(app, ["index", str(tmp_path), "cluster0"])

    assert result.exit_code == 1
    assert "ATLAS_PUBLIC_KEY" in result.output
    assert "ATLAS_PRIVATE_KEY" in result.output


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

    result = runner.invoke(app, ["index", str(tmp_path), "cluster0"])

    assert result.exit_code == 1
    assert "keys" in result.output
    assert called == []


def test_index_reports_empty_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []
    monkeypatch.setattr(
        "sie_atlas_ops.cli.client_from_env",
        lambda: called.append(True) or pytest.fail("Atlas client must not be created"),
    )

    result = runner.invoke(app, ["index", str(tmp_path), "cluster0"])

    assert result.exit_code == 1
    assert "No .json files found" in result.output
    assert called == []


def test_index_reports_unknown_cluster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://x"))
    _patch_client(monkeypatch, client)

    result = runner.invoke(app, ["index", str(tmp_path), "missing"])

    assert result.exit_code == 1
    assert "No cluster found for 'missing'" in result.output


def test_index_reports_cluster_without_connection_strings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail())
    _patch_client(monkeypatch, client)

    result = runner.invoke(app, ["index", str(tmp_path), "cluster0"])

    assert result.exit_code == 1
    assert "no connection strings" in result.output


def test_index_reports_atlas_api_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_definitions(tmp_path)

    class FailingClient(FakeAtlasClient):
        def _list_groups(self, **_: Any) -> SimpleNamespace:
            raise AtlasApiError(401, "Unauthorized", {"detail": "invalid credentials"})

    client = FailingClient([], {}, _detail(standard="mongodb://x"))
    _patch_client(monkeypatch, client)

    result = runner.invoke(app, ["index", str(tmp_path), "cluster0"])

    assert result.exit_code == 1
    assert "HTTP 401" in result.output
    assert "invalid credentials" in result.output


def test_index_rejects_missing_directory(tmp_path: Path) -> None:
    result = runner.invoke(app, ["index", str(tmp_path / "missing"), "cluster0"])

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
        result = runner.invoke(app, ["index", str(tmp_path), "cluster0"], input="2\n")
    finally:
        package_logger.removeHandler(handler)

    assert result.exit_code == 0, result.output
    assert any("Found cluster [green]cluster0[/green]" in message for message in handler.messages)
    assert any("by name" in message for message in handler.messages)
    assert any("Found 2 connection string(s)" in message for message in handler.messages)
    assert any(
        "standard_srv" in message and "mongodb+srv://srv" in message for message in handler.messages
    )
    assert any(
        "Selected connection string [cyan]standard_srv[/cyan]" in message
        for message in handler.messages
    )


def test_index_logs_lookup_by_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_definitions(tmp_path)
    client = FakeAtlasClient([GROUP_A], {GROUP_A.id: [CLUSTER]}, _detail(standard="mongodb://only"))
    _patch_client(monkeypatch, client)
    handler = _CaptureHandler()
    package_logger = logging.getLogger("sie_atlas_ops")
    package_logger.addHandler(handler)
    try:
        result = runner.invoke(app, ["index", str(tmp_path), CLUSTER.id])
    finally:
        package_logger.removeHandler(handler)

    assert result.exit_code == 0, result.output
    assert any("by ID" in message for message in handler.messages)
