"""MongoDB helpers for checking and applying collection indexes."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote, urlsplit, urlunsplit

from pymongo import MongoClient
from pymongo.errors import OperationFailure

from atlas_ops.errors import AtlasOperationError
from atlas_ops.index.definitions import CollectionIndexes

_DEFAULT_TIMEOUT_MS = 10_000
_USER_READY_TIMEOUT_SECONDS = 120.0
_USER_READY_INTERVAL_SECONDS = 2.0
_RETRYABLE_AUTH_CODES = frozenset({13, 18})

logger = logging.getLogger(__name__)


class MongoDatabaseNamesClient(Protocol):
    """Client that can list database names."""

    def list_database_names(self) -> list[str]: ...


class MongoCommandClient(Protocol):
    """Client that can address databases and issue commands."""

    def __getitem__(self, name: str) -> Any: ...


class MongoReadClient(MongoDatabaseNamesClient, MongoCommandClient, Protocol):
    """Client that can list databases and address collections."""


class MongoClientLike(MongoReadClient, Protocol):
    """Subset of :class:`pymongo.MongoClient` used by this module."""

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class MissingNamespace:
    """A collection that does not exist yet."""

    database: str
    collection: str
    database_exists: bool


def build_authenticated_uri(uri: str, username: str, password: str) -> str:
    """Insert credentials into an Atlas URI, forcing ``authSource=admin``.

    Standard Atlas URIs list several comma-separated hosts, so only ``netloc``
    is rewritten. Parsing host and port separately would reject those URIs.
    """
    parts = urlsplit(uri)
    if not parts.netloc:
        raise ValueError(f"Invalid MongoDB URI: {uri!r}")
    netloc = parts.netloc.rsplit("@", 1)[-1]
    credentials = f"{quote(username, safe='')}:{quote(password, safe='')}"
    authenticated = urlunsplit(
        (parts.scheme, f"{credentials}@{netloc}", parts.path, parts.query, parts.fragment)
    )
    if "authSource=" not in authenticated:
        separator = "&" if "?" in authenticated else "?"
        authenticated = f"{authenticated}{separator}authSource=admin"
    return authenticated


def create_client(uri: str, *, timeout_ms: int = _DEFAULT_TIMEOUT_MS) -> MongoClient:
    """Create a MongoDB client with fail-fast timeouts."""
    return MongoClient(
        uri,
        serverSelectionTimeoutMS=timeout_ms,
        connectTimeoutMS=timeout_ms,
        appname="atlas-ops",
    )


def wait_for_user(
    client: MongoDatabaseNamesClient,
    *,
    timeout_seconds: float = _USER_READY_TIMEOUT_SECONDS,
    interval_seconds: float = _USER_READY_INTERVAL_SECONDS,
) -> None:
    """Wait until the temporary user can authenticate against the cluster.

    Atlas creates database users asynchronously, so authentication can fail
    with codes 13/18 shortly after creation. Retry those errors until
    *timeout_seconds* elapse.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            client.list_database_names()
            return
        except OperationFailure as exc:
            if exc.code not in _RETRYABLE_AUTH_CODES:
                raise
            if time.monotonic() >= deadline:
                raise AtlasOperationError(
                    "Temporary user did not become active within "
                    f"{timeout_seconds:.0f} seconds: {exc}"
                ) from exc
            logger.debug("Temporary user is not active yet (%s); retrying.", exc)
            time.sleep(interval_seconds)


def find_missing_namespaces(
    client: MongoReadClient, definitions: list[CollectionIndexes]
) -> list[MissingNamespace]:
    """Return the database/collection pairs from *definitions* that do not exist."""
    existing_databases = set(client.list_database_names())
    missing: list[MissingNamespace] = []
    seen: set[tuple[str, str]] = set()
    for definition in definitions:
        key = (definition.database, definition.collection)
        if key in seen:
            continue
        seen.add(key)
        if definition.database not in existing_databases:
            missing.append(
                MissingNamespace(
                    database=definition.database,
                    collection=definition.collection,
                    database_exists=False,
                )
            )
        elif definition.collection not in client[definition.database].list_collection_names():
            missing.append(
                MissingNamespace(
                    database=definition.database,
                    collection=definition.collection,
                    database_exists=True,
                )
            )
    return missing
