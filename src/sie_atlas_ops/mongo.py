"""MongoDB helpers for checking and applying collection indexes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote, urlsplit, urlunsplit

from pymongo import MongoClient

from sie_atlas_ops.index_definitions import CollectionIndexes

_DEFAULT_TIMEOUT_MS = 10_000


class MongoClientLike(Protocol):
    """Subset of :class:`pymongo.MongoClient` used by this module."""

    def list_database_names(self) -> list[str]: ...

    def __getitem__(self, database: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class MissingNamespace:
    """A collection that does not exist yet."""

    database: str
    collection: str
    database_exists: bool


def build_authenticated_uri(uri: str, username: str, password: str) -> str:
    """Insert credentials into an Atlas URI, forcing ``authSource=admin``."""
    parts = urlsplit(uri)
    if not parts.hostname:
        raise ValueError(f"Invalid MongoDB URI: {uri!r}")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    netloc = f"{quote(username, safe='')}:{quote(password, safe='')}@{host}"
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    authenticated = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
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
        appname="sie-atlas-ops",
    )


def find_missing_namespaces(
    client: MongoClientLike, definitions: list[CollectionIndexes]
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
