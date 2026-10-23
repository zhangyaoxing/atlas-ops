from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pyatlassdk import AtlasApiError
from pyatlassdk.models.cluster_description20240805 import ClusterDescription20240805

from sie_atlas_ops.atlas import (
    ClusterTarget,
    client_from_env,
    connection_options,
    describe_atlas_error,
    find_clusters,
    is_object_id,
)
from sie_atlas_ops.errors import AtlasOperationError, ConfigurationError

GROUP_A = SimpleNamespace(id="a" * 24, name="Project A")
GROUP_B = SimpleNamespace(id="c" * 24, name="Project B")
CLUSTER_A = SimpleNamespace(id="b" * 24, name="cluster0")
CLUSTER_B = SimpleNamespace(id="d" * 24, name="Cluster0")


def _page(results: list[Any], total: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(results=results, total_count=total)


def _fake_client(groups: list[Any], clusters_by_group: dict[str, list[Any]]) -> SimpleNamespace:
    def list_groups(**kwargs: Any) -> SimpleNamespace:
        page = kwargs.get("page_num", 1)
        size = kwargs.get("items_per_page", 100)
        return _page(groups[(page - 1) * size : page * size], len(groups))

    def list_group_clusters(group_id: str, **kwargs: Any) -> SimpleNamespace:
        clusters = clusters_by_group.get(group_id, [])
        page = kwargs.get("page_num", 1)
        size = kwargs.get("items_per_page", 100)
        return _page(clusters[(page - 1) * size : page * size], len(clusters))

    return SimpleNamespace(
        projects=SimpleNamespace(list_groups=list_groups),
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


def test_find_clusters_by_name() -> None:
    client = _fake_client([GROUP_A, GROUP_B], {GROUP_A.id: [CLUSTER_A], GROUP_B.id: []})

    matches = find_clusters(client, "cluster0")

    assert matches == [
        ClusterTarget(
            project_id=GROUP_A.id,
            project_name="Project A",
            cluster_id=CLUSTER_A.id,
            cluster_name="cluster0",
        )
    ]


def test_find_clusters_by_id() -> None:
    client = _fake_client([GROUP_A], {GROUP_A.id: [CLUSTER_A, CLUSTER_B]})

    matches = find_clusters(client, CLUSTER_B.id.upper())

    assert [match.cluster_name for match in matches] == ["Cluster0"]
    assert matches[0].project_id == GROUP_A.id


def test_find_clusters_falls_back_to_case_insensitive_name() -> None:
    client = _fake_client([GROUP_A], {GROUP_A.id: [CLUSTER_A]})

    matches = find_clusters(client, "CLUSTER0")

    assert [match.cluster_name for match in matches] == ["cluster0"]


def test_exact_name_match_wins_over_case_insensitive_match() -> None:
    client = _fake_client([GROUP_A], {GROUP_A.id: [CLUSTER_A, CLUSTER_B]})

    matches = find_clusters(client, "cluster0")

    assert [match.cluster_name for match in matches] == ["cluster0"]


def test_find_clusters_returns_all_duplicate_names() -> None:
    client = _fake_client([GROUP_A, GROUP_B], {GROUP_A.id: [CLUSTER_A], GROUP_B.id: [CLUSTER_A]})

    matches = find_clusters(client, "cluster0")

    assert [match.project_name for match in matches] == ["Project A", "Project B"]


def test_find_clusters_returns_empty_when_nothing_matches() -> None:
    client = _fake_client([GROUP_A], {GROUP_A.id: [CLUSTER_A]})

    assert find_clusters(client, "missing") == []


def test_find_clusters_paginates_projects_and_clusters() -> None:
    groups = [SimpleNamespace(id=f"{index:024x}", name=f"Project {index}") for index in range(101)]
    target_group = groups[-1]
    target_cluster = SimpleNamespace(id="f" * 24, name="cluster0")
    clusters_by_group = {group.id: [] for group in groups[:-1]}
    clusters_by_group[target_group.id] = [
        SimpleNamespace(id=f"{index:024x}", name=f"other-{index}") for index in range(100)
    ] + [target_cluster]

    matches = find_clusters(_fake_client(groups, clusters_by_group), "cluster0")

    assert len(matches) == 1
    assert matches[0].project_id == target_group.id
    assert matches[0].cluster_id == target_cluster.id


def test_find_clusters_rejects_empty_reference() -> None:
    with pytest.raises(AtlasOperationError, match="must not be empty"):
        find_clusters(_fake_client([], {}), "  ")


def _cluster(payload: dict[str, Any]) -> ClusterDescription20240805:
    return ClusterDescription20240805.model_validate(payload)


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
    assert options["private_endpoint[eu-west-1, us-east-1]"] == "mongodb://pe"
    assert options["private_endpoint_srv[eu-west-1, us-east-1]"] == "mongodb+srv://pe"
    assert options["private_endpoint_srv_shard_optimized[eu-west-1, us-east-1]"] == (
        "mongodb+srv://pe-optimized"
    )


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
    error = AtlasApiError(401, "Unauthorized", {"detail": "invalid credentials"})

    assert describe_atlas_error(error) == (
        "Atlas API request failed (HTTP 401): invalid credentials"
    )


def test_describe_atlas_error_handles_text_payload() -> None:
    error = AtlasApiError(500, "boom", "internal error")

    assert describe_atlas_error(error) == "Atlas API request failed (HTTP 500): internal error"
