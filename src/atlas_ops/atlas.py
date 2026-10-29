"""Atlas API helpers: credentials, cluster lookup, and connection strings."""

from __future__ import annotations

import logging
import os
import re
import secrets
import string
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from pyatlassdk import AtlasAdminApiError, AtlasAdminClient, AtlasAppAdminClient
from pyatlassdk.admin.client import DEFAULT_OAUTH_TOKEN_URL
from pyatlassdk.admin.models.cloud_database_user import CloudDatabaseUser
from pyatlassdk.admin.models.database_user_role import DatabaseUserRole
from pyatlassdk.admin.models.user_scope import UserScope
from pydantic import BaseModel

from atlas_ops.errors import AtlasOperationError, ConfigurationError

logger = logging.getLogger(__name__)

PUBLIC_KEY_ENV = "ATLAS_PUBLIC_KEY"
PRIVATE_KEY_ENV = "ATLAS_PRIVATE_KEY"
PROJECT_ID_ENV = "ATLAS_PROJECT_ID"
CLUSTER_NAME_ENV = "ATLAS_CLUSTER_NAME"
CONNECTION_TYPE_ENV = "ATLAS_CONN_TYPE"
DB_USERNAME_ENV = "ATLAS_DB_USERNAME"
DB_PASSWORD_ENV = "ATLAS_DB_PASSWORD"
CLIENT_ID_ENV = "ATLAS_CLIENT_ID"
CLIENT_SECRET_ENV = "ATLAS_CLIENT_SECRET"
ACCESS_TOKEN_ENV = "ATLAS_ACCESS_TOKEN"

TEMPORARY_USER_TTL_HOURS = 6
_TEMPORARY_USER_PREFIX = "atlas_ops_"
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


class AtlasProjectClient(Protocol):
    """Client that can resolve projects."""

    projects: Any


class AtlasClusterClient(Protocol):
    """Client that can list and fetch clusters."""

    clusters: Any


class AtlasRequestClient(Protocol):
    """Client that can send raw Atlas API requests."""

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any | None = None,
        response_model: Any | None = None,
        accept: str | None = None,
        content_type: str | None = None,
    ) -> Any: ...


class AtlasClientLike(AtlasProjectClient, AtlasClusterClient, AtlasRequestClient, Protocol):
    """Subset of :class:`AtlasAdminClient` used by this module (handy for tests)."""


class ConnectionStringsCluster(Protocol):
    """Cluster-like object exposing Atlas connection strings."""

    connection_strings: Any


class AtlasErrorLike(Protocol):
    """Shared shape of Atlas admin and App Services API errors."""

    status_code: int
    payload: Any


@dataclass(frozen=True, slots=True)
class ProjectTarget:
    """An Atlas project resolved by ID."""

    project_id: str
    project_name: str
    org_id: str = ""
    org_name: str = ""


@dataclass(frozen=True, slots=True)
class ClusterTarget:
    """A cluster together with the project that owns it."""

    project_id: str
    project_name: str
    cluster_id: str
    cluster_name: str
    org_id: str = ""
    org_name: str = ""


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


def _api_credentials(
    environ: Mapping[str, str] | None = None,
) -> tuple[str, str] | None:
    env = os.environ if environ is None else environ
    public_key = env.get(PUBLIC_KEY_ENV, "").strip()
    private_key = env.get(PRIVATE_KEY_ENV, "").strip()
    if not public_key and not private_key:
        return None
    if not public_key or not private_key:
        missing = PUBLIC_KEY_ENV if not public_key else PRIVATE_KEY_ENV
        raise ConfigurationError(
            f"Missing Atlas API credential: set {missing}. "
            f"Both {PUBLIC_KEY_ENV} and {PRIVATE_KEY_ENV} are required."
        )
    return public_key, private_key


def _service_account_credentials(
    environ: Mapping[str, str] | None = None,
) -> tuple[str, str] | None:
    env = os.environ if environ is None else environ
    client_id = env.get(CLIENT_ID_ENV, "").strip()
    client_secret = env.get(CLIENT_SECRET_ENV, "").strip()
    if not client_id and not client_secret:
        return None
    if not client_id or not client_secret:
        missing = CLIENT_ID_ENV if not client_id else CLIENT_SECRET_ENV
        raise ConfigurationError(
            f"Missing OAuth service account credential: set {missing}. "
            f"Both {CLIENT_ID_ENV} and {CLIENT_SECRET_ENV} are required."
        )
    return client_id, client_secret


def _access_token(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    token = env.get(ACCESS_TOKEN_ENV, "").strip()
    return token or None


def _missing_auth_error() -> ConfigurationError:
    return ConfigurationError(
        "No Atlas credentials found. Authenticate with OAuth 2.0 instead of an "
        "Atlas API key pair:\n"
        f"  - Service account: set {CLIENT_ID_ENV} and {CLIENT_SECRET_ENV}.\n"
        f"  - Existing OAuth access token: set {ACCESS_TOKEN_ENV}.\n"
        "Create a service account in Atlas under Organization Access Manager > "
        "Service Accounts, or obtain an access token from your OAuth login."
    )


def _service_account_access_token(client_id: str, client_secret: str) -> str:
    try:
        response = httpx.post(
            DEFAULT_OAUTH_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise ConfigurationError(f"Could not reach the Atlas OAuth token endpoint: {exc}") from exc
    if response.status_code >= 300:
        raise ConfigurationError(
            f"Atlas OAuth token request failed with HTTP {response.status_code}: {response.text}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise ConfigurationError("Atlas OAuth token endpoint returned non-JSON.") from exc
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise ConfigurationError("Atlas OAuth response did not contain an access_token.")
    return token


def client_from_env(environ: Mapping[str, str] | None = None) -> AtlasAdminClient:
    """Build an Atlas Administration client from API keys or OAuth credentials."""
    api_credentials = _api_credentials(environ)
    if api_credentials is not None:
        public_key, private_key = api_credentials
        logger.info("Authentication: Atlas API key")
        return AtlasAdminClient(public_key=public_key, private_key=private_key)

    service_account = _service_account_credentials(environ)
    if service_account is not None:
        client_id, client_secret = service_account
        logger.info("Authentication: OAuth service account")
        return AtlasAdminClient(client_id=client_id, client_secret=client_secret)

    token = _access_token(environ)
    if token is not None:
        logger.info("Authentication: OAuth access token")
        client = AtlasAdminClient()
        # The SDK does not expose an access-token constructor; seed its OAuth cache.
        client._oauth_token = token
        client._oauth_expires_at = time.monotonic() + 3600.0
        return client

    raise _missing_auth_error()


def app_services_client_from_env(
    environ: Mapping[str, str] | None = None,
) -> AtlasAppAdminClient:
    """Log in to the App Services Admin API with API keys or OAuth."""
    api_credentials = _api_credentials(environ)
    if api_credentials is not None:
        public_key, private_key = api_credentials
        return AtlasAppAdminClient.login(
            username=public_key,
            api_key=private_key,
        )

    service_account = _service_account_credentials(environ)
    if service_account is not None:
        client_id, client_secret = service_account
        return AtlasAppAdminClient(
            access_token=_service_account_access_token(client_id, client_secret)
        )

    token = _access_token(environ)
    if token is not None:
        return AtlasAppAdminClient(access_token=token)

    raise _missing_auth_error()


def get_project(client: AtlasProjectClient, project_id: str) -> ProjectTarget:
    """Resolve *project_id* to its human-readable name."""
    try:
        group = client.projects.get_group(group_id=project_id)
    except AtlasAdminApiError as exc:
        if exc.status_code == 404:
            raise AtlasOperationError(
                f"Project {project_id!r} was not found or is not accessible."
            ) from exc
        raise
    org_id = getattr(group, "org_id", None)
    if not isinstance(org_id, str):
        org_id = ""
    org_name = ""
    organizations = getattr(client, "organizations", None)
    if org_id and organizations is not None:
        try:
            organization = organizations.get_org(org_id=org_id)
            org_name = organization.name
        except AtlasAdminApiError:
            org_name = ""
    return ProjectTarget(
        project_id=project_id,
        project_name=group.name,
        org_id=org_id,
        org_name=org_name,
    )


def find_clusters(
    client: AtlasClusterClient, project: ProjectTarget, reference: str
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
            org_id=project.org_id,
            org_name=project.org_name,
        )
        if lookup_by_id:
            if (cluster.id or "").lower() == reference.lower():
                exact.append(target)
        elif cluster.name == reference:
            exact.append(target)
        elif cluster.name.lower() == reference.lower():
            case_insensitive.append(target)

    return exact or case_insensitive


def connection_options(cluster: ConnectionStringsCluster) -> list[ConnectionOption]:
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
    client: AtlasRequestClient,
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
        groupId=project.project_id,
        username=username,
        password=password,
        databaseName="admin",
        roles=[
            DatabaseUserRole(
                roleName="readWriteAnyDatabase",
                databaseName="admin",
            )
        ],
        scopes=[UserScope(name=cluster_name, type="CLUSTER")],
        deleteAfterDate=delete_after,
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


def delete_temporary_user(client: AtlasRequestClient, project_id: str, username: str) -> None:
    """Delete a temporary database user; an already-missing user is not an error."""
    try:
        client.request(
            method="DELETE",
            path=(f"/groups/{project_id}/databaseUsers/admin/{quote(username, safe='')}"),
            response_model=None,
            accept=_DATABASE_USER_ACCEPT,
        )
    except AtlasAdminApiError as exc:
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


def describe_atlas_error(exc: AtlasErrorLike) -> str:
    """Turn an SDK error into a compact message for CLI output."""
    detail = ""
    if isinstance(exc.payload, dict):
        detail = str(exc.payload.get("detail") or exc.payload.get("error") or "").strip()
    elif isinstance(exc.payload, str):
        detail = exc.payload.strip()[:300]
    message = f"Atlas API request failed (HTTP {exc.status_code})"
    return f"{message}: {detail}" if detail else message


def _iter_clusters(client: AtlasClusterClient, group_id: str) -> Iterator[Any]:
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
