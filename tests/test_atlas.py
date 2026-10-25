from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import pytest
from pyatlassdk import AtlasAdminApiError
from pyatlassdk.admin.models.cloud_database_user import CloudDatabaseUser
from pyatlassdk.admin.models.cluster_description20240805 import ClusterDescription20240805

from sie_atlas_ops.atlas import (
    ClusterTarget,
    ConnectionOption,
    ProjectTarget,
    client_from_env,
    connection_options,
    create_temporary_user,
    delete_temporary_user,
    describe_atlas_error,
    find_clusters,
    get_project,
    is_object_id,
)
from sie_atlas_ops.errors import AtlasOperationError, ConfigurationError

GROUP_A = SimpleNamespace(id="a" * 24, name="Project A")
CLUSTER_A = SimpleNamespace(id="b" * 24, name="cluster0")
CLUSTER_B = SimpleNamespace(id="d" * 24, name="Cluster0")

PROJECT_A = ProjectTarget(project_id=GROUP_A.id, project_name=GROUP_A.name)


def _page(results: list[Any], total: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(results=results, total_count=total)


def _fake_client(
    clusters_by_group: dict[str, list[Any]], groups: tuple[Any, ...] = (GROUP_A,)
) -> Any:
    groups_by_id = {group.id: group for group in groups}

    def get_group(group_id: str, **_: Any) -> Any:
        if group_id not in groups_by_id:
            raise AtlasAdminApiError(404, "Not Found", {"detail": "no project"})
        return groups_by_id[group_id]

    def list_group_clusters(group_id: str, **kwargs: Any) -> SimpleNamespace:
        clusters = clusters_by_group.get(group_id, [])
        page = kwargs.get("page_num", 1)
        size = kwargs.get("items_per_page", 100)
        return _page(clusters[(page - 1) * size : page * size], len(clusters))

    return SimpleNamespace(
        projects=SimpleNamespace(get_group=get_group),
        clusters=SimpleNamespace(list_group_clusters=list_group_clusters),
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("a" * 24, True),
        ("ABCDEF0123456789ABCDEF01", True),
        ("a" * 23, False),
        ("a" * 25, False),
        ("g" * 24, False),
        ("my-cluster", False),
        ("", False),
    ],
)
def test_is_object_id(value: str, expected: bool) -> None:
    assert is_object_id(value) is expected


def test_client_from_env_uses_api_keys() -> None:
    client = client_from_env({"ATLAS_PUBLIC_KEY": "public", "ATLAS_PRIVATE_KEY": "private"})
    try:
        assert client.auth_mode == "digest"
    finally:
        client.close()


@pytest.mark.parametrize(
    ("environ", "missing"),
    [
        ({}, "ATLAS_PUBLIC_KEY, ATLAS_PRIVATE_KEY"),
        ({"ATLAS_PUBLIC_KEY": "only-public"}, "ATLAS_PRIVATE_KEY"),
        ({"ATLAS_PRIVATE_KEY": "   "}, "ATLAS_PUBLIC_KEY"),
    ],
)
def test_client_from_env_reports_missing_credentials(environ: dict[str, str], missing: str) -> None:
    with pytest.raises(ConfigurationError, match=missing.replace(", ", ".*")):
        client_from_env(environ)


def test_get_project_returns_name() -> None:
    client = _fake_client({GROUP_A.id: []})

    assert get_project(client, GROUP_A.id) == PROJECT_A


def test_get_project_reports_missing_project() -> None:
    client = _fake_client({})

    with pytest.raises(AtlasOperationError, match="not found or is not accessible"):
        get_project(client, "f" * 24)


def test_get_project_propagates_other_errors() -> None:
    def get_group(**_: Any) -> Any:
        raise AtlasAdminApiError(403, "Forbidden", None)

    client: Any = SimpleNamespace(projects=SimpleNamespace(get_group=get_group))

    with pytest.raises(AtlasAdminApiError):
        get_project(client, GROUP_A.id)


def test_find_clusters_by_name() -> None:
    client = _fake_client({GROUP_A.id: [CLUSTER_A]})

    matches = find_clusters(client, PROJECT_A, "cluster0")

    assert matches == [
        ClusterTarget(
            project_id=GROUP_A.id,
            project_name="Project A",
            cluster_id=CLUSTER_A.id,
            cluster_name="cluster0",
        )
    ]


def test_find_clusters_by_id() -> None:
    client = _fake_client({GROUP_A.id: [CLUSTER_A, CLUSTER_B]})

    matches = find_clusters(client, PROJECT_A, CLUSTER_B.id.upper())

    assert [match.cluster_name for match in matches] == ["Cluster0"]
    assert matches[0].project_id == GROUP_A.id


def test_find_clusters_falls_back_to_case_insensitive_name() -> None:
    client = _fake_client({GROUP_A.id: [CLUSTER_A]})

    matches = find_clusters(client, PROJECT_A, "CLUSTER0")

    assert [match.cluster_name for match in matches] == ["cluster0"]


def test_exact_name_match_wins_over_case_insensitive_match() -> None:
    client = _fake_client({GROUP_A.id: [CLUSTER_A, CLUSTER_B]})

    matches = find_clusters(client, PROJECT_A, "cluster0")

    assert [match.cluster_name for match in matches] == ["cluster0"]


def test_find_clusters_returns_all_duplicate_names() -> None:
    duplicate = SimpleNamespace(id="e" * 24, name="cluster0")
    client = _fake_client({GROUP_A.id: [CLUSTER_A, duplicate]})

    matches = find_clusters(client, PROJECT_A, "cluster0")

    assert [match.cluster_id for match in matches] == [CLUSTER_A.id, duplicate.id]


def test_find_clusters_returns_empty_when_nothing_matches() -> None:
    client = _fake_client({GROUP_A.id: [CLUSTER_A]})

    assert find_clusters(client, PROJECT_A, "missing") == []


def test_find_clusters_paginates_clusters() -> None:
    clusters = [SimpleNamespace(id=f"{index:024x}", name=f"other-{index}") for index in range(100)]
    clusters.append(SimpleNamespace(id="f" * 24, name="cluster0"))
    client = _fake_client({GROUP_A.id: clusters})

    matches = find_clusters(client, PROJECT_A, "cluster0")

    assert len(matches) == 1
    assert matches[0].cluster_id == "f" * 24


def test_find_clusters_rejects_empty_reference() -> None:
    with pytest.raises(AtlasOperationError, match="must not be empty"):
        find_clusters(_fake_client({}), PROJECT_A, "  ")


def _cluster(payload: dict[str, Any]) -> ClusterDescription20240805:
    return ClusterDescription20240805.model_validate(payload)


def _options_from(**overrides: Any) -> list[ConnectionOption]:
    values: dict[str, Any] = {
        "standard": None,
        "standard_srv": None,
        "private": None,
        "private_srv": None,
        "aws_private_link": None,
        "aws_private_link_srv": None,
        "private_endpoint": None,
        "model_extra": {},
    }
    values.update(overrides)
    cluster: Any = SimpleNamespace(connection_strings=SimpleNamespace(**values))
    return connection_options(cluster)


def test_connection_options_cover_every_flavour() -> None:
    cluster = _cluster(
        {
            "connectionStrings": {
                "standard": "mongodb://standard",
                "standardSrv": "mongodb+srv://standard",
                "private": "mongodb://private",
                "privateSrv": "mongodb+srv://private",
                "awsPrivateLink": {"vpce-1": "mongodb://aws"},
                "awsPrivateLinkSrv": {"vpce-2": "mongodb+srv://aws"},
                "privateEndpoint": [
                    {
                        "connectionString": "mongodb://pe",
                        "srvConnectionString": "mongodb+srv://pe",
                        "srvShardOptimizedConnectionString": "mongodb+srv://pe-optimized",
                        "endpoints": [{"region": "eu-west-1"}, {"region": "us-east-1"}],
                    }
                ],
            }
        }
    )

    options = {option.label: option.uri for option in connection_options(cluster)}

    assert options["standard"] == "mongodb://standard"
    assert options["standard_srv"] == "mongodb+srv://standard"
    assert options["private"] == "mongodb://private"
    assert options["private_srv"] == "mongodb+srv://private"
    assert options["aws_private_link[vpce-1]"] == "mongodb://aws"
    assert options["aws_private_link_srv[vpce-2]"] == "mongodb+srv://aws"
    assert options["private_endpoint[0].connection_string"] == "mongodb://pe"
    assert options["private_endpoint[0].srv_connection_string"] == "mongodb+srv://pe"
    assert (
        options["private_endpoint[0].srv_shard_optimized_connection_string"]
        == "mongodb+srv://pe-optimized"
    )


def test_connection_options_expands_list_values() -> None:
    options = _options_from(aws_private_link={"vpce-1": ["mongodb://one", "mongodb://two"]})

    assert [(item.label, item.uri) for item in options] == [
        ("aws_private_link[vpce-1][0]", "mongodb://one"),
        ("aws_private_link[vpce-1][1]", "mongodb://two"),
    ]


def test_connection_options_expands_dict_values() -> None:
    options = _options_from(aws_private_link={"vpce-1": {"primary": "mongodb://primary"}})

    assert [(item.label, item.uri) for item in options] == [
        ("aws_private_link[vpce-1][primary]", "mongodb://primary"),
    ]


def test_connection_options_expands_unknown_list_and_dict_fields() -> None:
    cluster = _cluster(
        {
            "connectionStrings": {
                "customList": ["mongodb://list", "not-a-uri"],
                "customMap": {"key": "mongodb+srv://map"},
            }
        }
    )

    options = connection_options(cluster)

    assert [(item.label, item.uri) for item in options] == [
        ("customList[0]", "mongodb://list"),
        ("customMap[key]", "mongodb+srv://map"),
    ]


def test_connection_options_deduplicate_uris() -> None:
    cluster = _cluster(
        {"connectionStrings": {"standard": "mongodb://same", "private": "mongodb://same"}}
    )

    options = connection_options(cluster)

    assert len(options) == 1
    assert options[0].label == "standard"


def test_connection_options_without_connection_strings() -> None:
    assert connection_options(_cluster({"name": "cluster0"})) == []


def test_describe_atlas_error_prefers_detail() -> None:
    error = AtlasAdminApiError(401, "Unauthorized", {"detail": "invalid credentials"})

    assert describe_atlas_error(error) == (
        "Atlas API request failed (HTTP 401): invalid credentials"
    )


def test_describe_atlas_error_handles_text_payload() -> None:
    error = AtlasAdminApiError(500, "boom", "internal error")

    assert describe_atlas_error(error) == "Atlas API request failed (HTTP 500): internal error"


class _UserRequestClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "path": path, **kwargs})
        return CloudDatabaseUser.model_validate(kwargs["json"])


def test_create_temporary_user_sends_cluster_scoped_role() -> None:
    client = _UserRequestClient()

    user = create_temporary_user(
        client,
        PROJECT_A,
        "cluster0",
        username="tmp_user",
        password="Str0ng!pass",
    )

    assert user.username == "tmp_user"
    assert user.password == "Str0ng!pass"
    assert user.delete_after.tzinfo is not None

    call = client.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == f"/groups/{GROUP_A.id}/databaseUsers"
    assert call["accept"] == "application/vnd.atlas.2023-01-01+json"
    assert call["content_type"] == call["accept"]

    payload = call["json"]
    assert payload["groupId"] == GROUP_A.id
    assert payload["username"] == "tmp_user"
    assert payload["password"] == "Str0ng!pass"
    assert payload["databaseName"] == "admin"
    assert payload["roles"] == [{"databaseName": "admin", "roleName": "readWriteAnyDatabase"}]
    assert payload["scopes"] == [{"name": "cluster0", "type": "CLUSTER"}]
    assert isinstance(payload["deleteAfterDate"], str)

    delete_after = datetime.fromisoformat(payload["deleteAfterDate"].replace("Z", "+00:00"))
    remaining = delete_after - datetime.now(timezone.utc)
    assert timedelta(hours=5, minutes=59) <= remaining <= timedelta(hours=6, minutes=1)


def test_create_temporary_user_generates_credentials() -> None:
    client = _UserRequestClient()

    user = create_temporary_user(client, PROJECT_A, "cluster0", ttl_hours=1)

    assert user.username.startswith("sie_atlas_ops_")
    assert len(user.password) >= 12
    assert any(char.islower() for char in user.password)
    assert any(char.isupper() for char in user.password)
    assert any(char.isdigit() for char in user.password)
    assert any(char in "!@#$%^&*" for char in user.password)
    remaining = user.delete_after - datetime.now(timezone.utc)
    assert timedelta(minutes=59) <= remaining <= timedelta(hours=1, minutes=1)


class _DeleteRequestClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "path": path, **kwargs})
        if self.error is not None:
            raise self.error
        return None


def test_delete_temporary_user_sends_delete_request() -> None:
    client = _DeleteRequestClient()

    delete_temporary_user(client, GROUP_A.id, "user name@example")

    call = client.calls[0]
    assert call["method"] == "DELETE"
    assert call["path"] == (
        f"/groups/{GROUP_A.id}/databaseUsers/admin/{quote('user name@example', safe='')}"
    )
    assert call["response_model"] is None
    assert call["accept"] == "application/vnd.atlas.2023-01-01+json"


def test_delete_temporary_user_ignores_missing_user() -> None:
    client = _DeleteRequestClient(AtlasAdminApiError(404, "Not Found", None))

    delete_temporary_user(client, GROUP_A.id, "already-gone")


def test_delete_temporary_user_propagates_other_errors() -> None:
    client = _DeleteRequestClient(AtlasAdminApiError(500, "boom", None))

    with pytest.raises(AtlasAdminApiError):
        delete_temporary_user(client, GROUP_A.id, "user")
