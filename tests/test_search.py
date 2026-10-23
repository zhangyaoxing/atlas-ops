from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sie_atlas_ops.search import SearchCollection, SearchExportResult, export_search_indexes


class FakeClient:
    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append((method, path, kwargs))
        return self._payload


def test_export_groups_search_indexes_by_collection(tmp_path: Path) -> None:
    client = FakeClient(
        [
            {
                "database": "sample_mflix",
                "collectionName": "movies",
                "indexID": "id-2",
                "name": "vector",
                "type": "vectorSearch",
                "status": "READY",
                "queryable": True,
                "latestDefinitionVersion": {"version": 3},
                "statusDetail": [{"host": "host-1"}],
                "latestDefinition": {"fields": [{"type": "vector"}]},
            },
            {
                "database": "sample_mflix",
                "collectionName": "movies",
                "indexID": "id-1",
                "name": "default",
                "type": "search",
                "latestDefinition": {"mappings": {"dynamic": True}},
            },
            {
                "database": "sample_mflix",
                "collectionName": "users",
                "indexID": "id-3",
                "name": "users",
                "type": "search",
                "latestDefinition": {"mappings": {"dynamic": False}},
            },
        ]
    )

    result = export_search_indexes(client, "project-1", "cluster0", tmp_path)

    assert result == SearchExportResult(
        files=2,
        indexes=3,
        collections=(
            SearchCollection(database="sample_mflix", collection="movies", indexes=2),
            SearchCollection(database="sample_mflix", collection="users", indexes=1),
        ),
    )
    movies = json.loads((tmp_path / "sample_mflix.movies.json").read_text(encoding="utf-8"))
    assert movies["database"] == "sample_mflix"
    assert movies["collection"] == "movies"
    assert movies["indexes"] == [
        {
            "name": "default",
            "type": "search",
            "definition": {"mappings": {"dynamic": True}},
        },
        {
            "name": "vector",
            "type": "vectorSearch",
            "definition": {"fields": [{"type": "vector"}]},
        },
    ]
    assert "indexID" not in json.dumps(movies)
    assert "status" not in json.dumps(movies)
    assert "latestDefinitionVersion" not in json.dumps(movies)
    assert client.calls == [
        (
            "GET",
            "/groups/project-1/clusters/cluster0/search/indexes",
            {
                "response_model": Any,
                "accept": "application/vnd.atlas.2024-05-30+json",
            },
        )
    ]


def test_export_accepts_results_envelope(tmp_path: Path) -> None:
    client = FakeClient(
        {
            "results": [
                {
                    "database": "db",
                    "collectionName": "coll",
                    "indexID": "id-1",
                    "name": "default",
                    "latestDefinition": {},
                }
            ]
        }
    )

    result = export_search_indexes(client, "project-1", "cluster0", tmp_path)

    assert result == SearchExportResult(
        files=1,
        indexes=1,
        collections=(SearchCollection(database="db", collection="coll", indexes=1),),
    )
    assert (tmp_path / "db.coll.json").exists()


def test_export_skips_entries_without_database_or_collection(tmp_path: Path) -> None:
    client = FakeClient(
        [
            {"database": "db", "name": "missing-collection"},
            {"collectionName": "coll", "name": "missing-database"},
            {"database": "db", "collectionName": "coll", "name": "valid"},
        ]
    )

    result = export_search_indexes(client, "project-1", "cluster0", tmp_path)

    assert result.files == 1
    assert result.indexes == 1
    assert (tmp_path / "db.coll.json").exists()


def test_export_sanitizes_filename(tmp_path: Path) -> None:
    client = FakeClient(
        [
            {
                "database": "db",
                "collectionName": "a/b",
                "indexID": "id-1",
                "name": "default",
                "latestDefinition": {},
            }
        ]
    )

    export_search_indexes(client, "project-1", "cluster0", tmp_path)

    assert (tmp_path / "db.a_b.json").exists()


def test_export_without_search_indexes_writes_nothing(tmp_path: Path) -> None:
    client = FakeClient([])

    result = export_search_indexes(client, "project-1", "cluster0", tmp_path)

    assert result == SearchExportResult(files=0, indexes=0, collections=())
    assert list(tmp_path.iterdir()) == []


def test_export_falls_back_to_definition_and_default_type(tmp_path: Path) -> None:
    client = FakeClient(
        [
            {
                "database": "db",
                "collectionName": "coll",
                "name": "legacy",
                "definition": {"mappings": {"dynamic": True}},
            }
        ]
    )

    export_search_indexes(client, "project-1", "cluster0", tmp_path)

    payload = json.loads((tmp_path / "db.coll.json").read_text(encoding="utf-8"))
    assert payload["indexes"] == [
        {
            "name": "legacy",
            "type": "search",
            "definition": {"mappings": {"dynamic": True}},
        }
    ]
