"""Atlas API helpers: credentials, cluster lookup, and connection strings."""

from __future__ import annotations

import os
import re
import secrets
import string
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote

from pyatlassdk import AtlasApiError, AtlasClient
from pyatlassdk.models.cloud_database_user import CloudDatabaseUser
from pyatlassdk.models.database_user_role import DatabaseUserRole
from pyatlassdk.models.user_scope import UserScope
from pydantic import BaseModel

from sie_atlas_ops.errors import AtlasOperationError, ConfigurationError

if TYPE_CHECKING:
    from pyatlassdk.models.cluster_description20240805 import ClusterDescription20240805

PUBLIC_KEY_ENV = "ATLAS_PUBLIC_KEY"
PRIVATE_KEY_ENV = "ATLAS_PRIVATE_KEY"
PROJECT_ID_ENV = "ATLAS_PROJECT_ID"
CLUSTER_NAME_ENV = "ATLAS_CLUSTER_NAME"
DB_USERNAME_ENV = "ATLAS_DB_USERNAME"
DB_PASSWORD_ENV = "ATLAS_DB_PASSWORD"

TEMPORARY_USER_TTL_HOURS = 6
_TEMPORARY_USER_PREFIX = "sie_atlas_ops_"
_PASSWORD_SYMBOLS = "!@#$%^&*"
_DATABASE_USER_ACCEPT = "application/vnd.atlas.2023-01-01+json"

_CONNECTION_FIELDS = (
    "standard",
    "standard_srv",
    "private",
    "private_srv",
    "aws_private_link",
    "aws_private_link_srv",
    "private_endpoint",
)
_URI_PREFIXES = ("mongodb://", "mongodb+srv://")

_OBJECT_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{24}$")
_PAGE_SIZE = 100


class _ProjectsAPI(Protocol):
    def get_group(self, group_id: str, **kwargs: Any) -> Any: ...


class _ClustersAPI(Protocol):
    def list_group_clusters(self, group_id: str, **kwargs: Any) -> Any: ...

    def get_group_cluster(self, cluster_name: str, group_id: str, **kwargs: Any) -> Any: ...


class AtlasClientLike(Protocol):
    """Subset of :class:`AtlasClient` used by this module (handy for tests)."""

    projects: _ProjectsAPI
    clusters: _ClustersAPI

    def request(self, method: str, path: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class ProjectTarget:
    """An Atlas project resolved by ID."""

    project_id: str
    project_name: str


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


@dataclass(frozen=True, slots=True)
class TemporaryUser:
    """A database user created for one index-creation run."""

    username: str
    password: str
    delete_after: datetime


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


def get_project(client: AtlasClientLike, project_id: str) -> ProjectTarget:
    """Resolve *project_id* to its human-readable name."""
    try:
        group = client.projects.get_group(group_id=project_id)
    except AtlasApiError as exc:
        if exc.status_code == 404:
            raise AtlasOperationError(
                f"Project {project_id!r} was not found or is not accessible."
            ) from exc
        raise
    return ProjectTarget(project_id=project_id, project_name=group.name)


def find_clusters(
    client: AtlasClientLike, project: ProjectTarget, reference: str
) -> list[ClusterTarget]:
    """Find clusters in *project* by 24-hex ID or by name.

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

    for cluster in _iter_clusters(client, project.project_id):
        if not cluster.name:
            continue
        target = ClusterTarget(
            project_id=project.project_id,
            project_name=project.project_name,
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
    """Collect every connection string on *cluster*, de-duplicated by URI.

    Atlas returns some entries as strings but others as lists or dictionaries.
    Strings are collected directly, list items and dictionary values are
    expanded recursively, and only values that look like MongoDB URIs are kept.
    """
    strings = cluster.connection_strings
    if strings is None:
        return []

    options: list[ConnectionOption] = []
    for field_name in _CONNECTION_FIELDS:
        _collect_options(getattr(strings, field_name, None), field_name, options)
    for key, value in (strings.model_extra or {}).items():
        _collect_options(value, str(key), options)
    return _dedupe(options)


def _collect_options(value: Any, label: str, options: list[ConnectionOption]) -> None:
    if isinstance(value, str):
        if value.startswith(_URI_PREFIXES):
            options.append(ConnectionOption(label=label, uri=value))
        return
    if isinstance(value, BaseModel):
        for field_name in type(value).model_fields:
            _collect_options(getattr(value, field_name), f"{label}.{field_name}", options)
        for key, extra in (value.model_extra or {}).items():
            _collect_options(extra, f"{label}[{key}]", options)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _collect_options(item, f"{label}[{key}]", options)
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _collect_options(item, f"{label}[{index}]", options)


def create_temporary_user(
    client: AtlasClientLike,
    project: ProjectTarget,
    cluster_name: str,
    *,
    ttl_hours: int = TEMPORARY_USER_TTL_HOURS,
    username: str | None = None,
    password: str | None = None,
) -> TemporaryUser:
    """Create a cluster-scoped database user that Atlas deletes after *ttl_hours*.

    The user gets ``readWriteAnyDatabase`` on ``admin``. The request body is
    sent as a JSON-ready dict because the SDK serializes pydantic models with
    ``model_dump()``, which leaves ``datetime`` objects that httpx cannot
    encode.
    """
    username = username or _generate_username()
    password = password or _generate_password()
    delete_after = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)

    body = CloudDatabaseUser(
        group_id=project.project_id,
        username=username,
        password=password,
        database_name="admin",
        roles=[
            DatabaseUserRole(
                role_name="readWriteAnyDatabase",
                database_name="admin",
            )
        ],
        scopes=[UserScope(name=cluster_name, type="CLUSTER")],
        delete_after_date=delete_after,
    )
    created = client.request(
        method="POST",
        path=f"/groups/{project.project_id}/databaseUsers",
        json=body.model_dump(by_alias=True, exclude_none=True, mode="json"),
        response_model=CloudDatabaseUser,
        accept=_DATABASE_USER_ACCEPT,
        content_type=_DATABASE_USER_ACCEPT,
    )
    return TemporaryUser(
        username=created.username,
        password=password,
        delete_after=created.delete_after_date or delete_after,
    )


def delete_temporary_user(client: AtlasClientLike, project_id: str, username: str) -> None:
    """Delete a temporary database user; an already-missing user is not an error."""
    try:
        client.request(
            method="DELETE",
            path=(f"/groups/{project_id}/databaseUsers/admin/{quote(username, safe='')}"),
            response_model=None,
            accept=_DATABASE_USER_ACCEPT,
        )
    except AtlasApiError as exc:
        if exc.status_code == 404:
            return
        raise


def _generate_username() -> str:
    return f"{_TEMPORARY_USER_PREFIX}{secrets.token_hex(6)}"


def _generate_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits + _PASSWORD_SYMBOLS
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(char.islower() for char in candidate)
            and any(char.isupper() for char in candidate)
            and any(char.isdigit() for char in candidate)
            and any(char in _PASSWORD_SYMBOLS for char in candidate)
        ):
            return candidate


def describe_atlas_error(exc: AtlasApiError) -> str:
    """Turn an SDK error into a compact message for CLI output."""
    detail = ""
    if isinstance(exc.payload, dict):
        detail = str(exc.payload.get("detail") or exc.payload.get("error") or "").strip()
    elif isinstance(exc.payload, str):
        detail = exc.payload.strip()[:300]
    message = f"Atlas API request failed (HTTP {exc.status_code})"
    return f"{message}: {detail}" if detail else message


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


def _dedupe(options: list[ConnectionOption]) -> list[ConnectionOption]:
    seen: set[str] = set()
    unique: list[ConnectionOption] = []
    for option in options:
        if option.uri in seen:
            continue
        seen.add(option.uri)
        unique.append(option)
    return unique
