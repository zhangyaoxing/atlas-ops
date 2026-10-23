"""Atlas API helpers: credentials, cluster lookup, and connection strings."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from pyatlassdk import AtlasApiError, AtlasClient

from sie_atlas_ops.errors import AtlasOperationError, ConfigurationError

if TYPE_CHECKING:
    from pyatlassdk.models.cluster_description20240805 import ClusterDescription20240805

PUBLIC_KEY_ENV = "ATLAS_PUBLIC_KEY"
PRIVATE_KEY_ENV = "ATLAS_PRIVATE_KEY"

_OBJECT_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{24}$")
_PAGE_SIZE = 100


class _ProjectsAPI(Protocol):
    def list_groups(self, **kwargs: Any) -> Any: ...


class _ClustersAPI(Protocol):
    def list_group_clusters(self, group_id: str, **kwargs: Any) -> Any: ...

    def get_group_cluster(self, cluster_name: str, group_id: str, **kwargs: Any) -> Any: ...


class AtlasClientLike(Protocol):
    """Subset of :class:`AtlasClient` used by this module (handy for tests)."""

    projects: _ProjectsAPI
    clusters: _ClustersAPI


@dataclass(frozen=True, slots=True)
class ClusterTarget:
    """A cluster together with the project that owns it."""

    project_id: str
    project_name: str
    cluster_id: str
    cluster_name: str


@dataclass(frozen=True, slots=True)
class ConnectionOption:
    """One selectable connection string exposed by Atlas."""

    label: str
    uri: str


def is_object_id(value: str) -> bool:
    """Return whether *value* looks like a 24-character hexadecimal Atlas ID."""
    return bool(_OBJECT_ID_PATTERN.fullmatch(value))


def client_from_env(environ: Mapping[str, str] | None = None) -> AtlasClient:
    """Build an Atlas client from ``ATLAS_PUBLIC_KEY``/``ATLAS_PRIVATE_KEY``."""
    env = os.environ if environ is None else environ
    missing = [name for name in (PUBLIC_KEY_ENV, PRIVATE_KEY_ENV) if not env.get(name, "").strip()]
    if missing:
        raise ConfigurationError(
            f"Missing Atlas API credentials: set {', '.join(missing)} "
            "in the environment or in a .env file. "
            "Use the public and private key of an Atlas API key."
        )
    return AtlasClient(
        public_key=env[PUBLIC_KEY_ENV].strip(),
        private_key=env[PRIVATE_KEY_ENV].strip(),
    )


def find_clusters(client: AtlasClientLike, reference: str) -> list[ClusterTarget]:
    """Find clusters by 24-hex ID or by name across every accessible project.

    Names are matched exactly first; when that yields nothing, a
    case-insensitive match is attempted. Multiple matches are returned so the
    caller can disambiguate.
    """
    reference = reference.strip()
    if not reference:
        raise AtlasOperationError("Cluster reference must not be empty.")

    lookup_by_id = is_object_id(reference)
    exact: list[ClusterTarget] = []
    case_insensitive: list[ClusterTarget] = []

    for group in _iter_groups(client):
        if not group.id or not group.name:
            continue
        for cluster in _iter_clusters(client, group.id):
            if not cluster.name:
                continue
            target = ClusterTarget(
                project_id=group.id,
                project_name=group.name,
                cluster_id=cluster.id or "",
                cluster_name=cluster.name,
            )
            if lookup_by_id:
                if (cluster.id or "").lower() == reference.lower():
                    exact.append(target)
            elif cluster.name == reference:
                exact.append(target)
            elif cluster.name.lower() == reference.lower():
                case_insensitive.append(target)

    return exact or case_insensitive


def connection_options(cluster: ClusterDescription20240805) -> list[ConnectionOption]:
    """Collect every connection string on *cluster*, de-duplicated by URI."""
    strings = cluster.connection_strings
    if strings is None:
        return []

    options: list[ConnectionOption] = []

    def add(label: str, uri: str | None) -> None:
        if uri:
            options.append(ConnectionOption(label=label, uri=uri))

    add("standard", strings.standard)
    add("standard_srv", strings.standard_srv)
    add("private", strings.private)
    add("private_srv", strings.private_srv)
    for name, uri in _extra_strings(strings.aws_private_link):
        add(f"aws_private_link[{name}]", uri)
    for name, uri in _extra_strings(strings.aws_private_link_srv):
        add(f"aws_private_link_srv[{name}]", uri)

    for entry in strings.private_endpoint or []:
        regions = _regions(entry)
        add(f"private_endpoint[{regions}]", entry.connection_string)
        add(f"private_endpoint_srv[{regions}]", entry.srv_connection_string)
        add(
            f"private_endpoint_srv_shard_optimized[{regions}]",
            entry.srv_shard_optimized_connection_string,
        )

    return _dedupe(options)


def describe_atlas_error(exc: AtlasApiError) -> str:
    """Turn an SDK error into a compact message for CLI output."""
    detail = ""
    if isinstance(exc.payload, dict):
        detail = str(exc.payload.get("detail") or exc.payload.get("error") or "").strip()
    elif isinstance(exc.payload, str):
        detail = exc.payload.strip()[:300]
    message = f"Atlas API request failed (HTTP {exc.status_code})"
    return f"{message}: {detail}" if detail else message


def _iter_groups(client: AtlasClientLike) -> Iterator[Any]:
    page_number = 1
    seen = 0
    while True:
        page = client.projects.list_groups(items_per_page=_PAGE_SIZE, page_num=page_number)
        results = list(page.results)
        yield from results
        seen += len(results)
        total = getattr(page, "total_count", None)
        if len(results) < _PAGE_SIZE or (total is not None and seen >= total):
            return
        page_number += 1


def _iter_clusters(client: AtlasClientLike, group_id: str) -> Iterator[Any]:
    page_number = 1
    seen = 0
    while True:
        page = client.clusters.list_group_clusters(
            group_id=group_id, items_per_page=_PAGE_SIZE, page_num=page_number
        )
        results = list(page.results)
        yield from results
        seen += len(results)
        total = getattr(page, "total_count", None)
        if len(results) < _PAGE_SIZE or (total is not None and seen >= total):
            return
        page_number += 1


def _extra_strings(model: Any) -> Iterator[tuple[str, str]]:
    """Yield the dynamic (``extra``) string entries of an AWS PrivateLink model."""
    extras = getattr(model, "model_extra", None) or {}
    for name, uri in extras.items():
        if isinstance(uri, str) and uri:
            yield str(name), uri


def _regions(entry: Any) -> str:
    regions = sorted({endpoint.region for endpoint in (entry.endpoints or []) if endpoint.region})
    return ", ".join(regions) or "unknown region"


def _dedupe(options: list[ConnectionOption]) -> list[ConnectionOption]:
    seen: set[str] = set()
    unique: list[ConnectionOption] = []
    for option in options:
        if option.uri in seen:
            continue
        seen.add(option.uri)
        unique.append(option)
    return unique
