from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas_ops.errors import SearchDefinitionError
from atlas_ops.search.definitions import load_search_index_definitions


def _write(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "database": "db",
        "collection": "coll",
        "indexes": [
            {
                "name": "default",
                "type": "search",
                "definition": {"mappings": {"dynamic": True}},
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_loads_search_definitions(tmp_path: Path) -> None:
    _write(tmp_path / "search.json", _payload())

    definitions = load_search_index_definitions(tmp_path)

    assert len(definitions) == 1
    assert definitions[0].database == "db"
    assert definitions[0].collection == "coll"
    assert definitions[0].indexes[0].name == "default"
    assert definitions[0].indexes[0].definition == {"mappings": {"dynamic": True}}


def test_rejects_duplicate_index_names(tmp_path: Path) -> None:
    _write(
        tmp_path / "search.json",
        _payload(
            indexes=[
                {"name": "same", "type": "search", "definition": {}},
                {"name": "same", "type": "search", "definition": {}},
            ]
        ),
    )

    with pytest.raises(SearchDefinitionError, match="duplicate index name"):
        load_search_index_definitions(tmp_path)


def test_rejects_unknown_index_fields(tmp_path: Path) -> None:
    _write(
        tmp_path / "search.json",
        _payload(
            indexes=[
                {
                    "name": "default",
                    "type": "search",
                    "definition": {},
                    "extra": True,
                }
            ]
        ),
    )

    with pytest.raises(SearchDefinitionError, match="unknown field"):
        load_search_index_definitions(tmp_path)


def test_rejects_invalid_type(tmp_path: Path) -> None:
    _write(
        tmp_path / "search.json",
        _payload(indexes=[{"name": "default", "type": "other", "definition": {}}]),
    )

    with pytest.raises(SearchDefinitionError, match="type must be one of"):
        load_search_index_definitions(tmp_path)


def test_rejects_duplicate_namespaces_across_files(tmp_path: Path) -> None:
    _write(tmp_path / "one.json", _payload())
    _write(tmp_path / "two.json", _payload())

    with pytest.raises(SearchDefinitionError, match="duplicate namespace"):
        load_search_index_definitions(tmp_path)
