import json
from pathlib import Path

import pytest

from atlas_ops.errors import IndexDefinitionError
from atlas_ops.index.definitions import load_index_definitions


def _write(directory: Path, name: str, payload: object) -> Path:
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "database": "exampleDB",
        "collection": "example",
        "indexes": [{"keys": {"field": 1}, "options": {"name": "field_index"}, "commitQuorum": 1}],
    }
    payload.update(overrides)
    return payload


def test_loads_every_json_file(tmp_path: Path) -> None:
    _write(tmp_path, "b.json", _payload(collection="second"))
    _write(tmp_path, "a.json", _payload(collection="first"))
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")

    definitions = load_index_definitions(tmp_path)

    assert [definition.collection for definition in definitions] == ["first", "second"]
    spec = definitions[0].indexes[0]
    assert spec.keys == {"field": 1}
    assert spec.options == {"name": "field_index"}
    assert spec.commit_quorum == 1


def test_optional_fields_may_be_omitted(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "indexes.json",
        {
            "database": "db",
            "collection": "coll",
            "indexes": [{"keys": {"a": 1, "b": -1}}, {"keys": {"c": "text"}}],
        },
    )

    definitions = load_index_definitions(tmp_path)

    assert len(definitions[0].indexes) == 2
    assert definitions[0].indexes[0].options is None
    assert definitions[0].indexes[0].commit_quorum is None
    assert definitions[0].indexes[1].keys == {"c": "text"}


def test_key_directions_accept_decimals(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "indexes.json",
        _payload(indexes=[{"keys": {"ascending": 1.0, "descending": -1.0}}]),
    )

    keys = load_index_definitions(tmp_path)[0].indexes[0].keys

    assert keys == {"ascending": 1.0, "descending": -1.0}


@pytest.mark.parametrize("direction", [0, 2, 0.0, 0.5, 1.5, -1.5])
def test_invalid_key_direction_is_rejected(tmp_path: Path, direction: int | float) -> None:
    _write(
        tmp_path,
        "indexes.json",
        _payload(indexes=[{"keys": {"a": direction}}]),
    )

    with pytest.raises(
        IndexDefinitionError,
        match=r"indexes\[0\]\.keys\.a must be 1, -1, 1\.0, -1\.0",
    ):
        load_index_definitions(tmp_path)


def test_commit_quorum_accepts_strings(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "indexes.json",
        _payload(indexes=[{"keys": {"a": 1}, "commitQuorum": "majority"}]),
    )

    assert load_index_definitions(tmp_path)[0].indexes[0].commit_quorum == "majority"


def test_no_json_files_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "readme.md").write_text("not a definition", encoding="utf-8")

    with pytest.raises(IndexDefinitionError, match="No .json files found"):
        load_index_definitions(tmp_path)


def test_invalid_json_reports_position(tmp_path: Path) -> None:
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(IndexDefinitionError, match=r"invalid JSON at line 1, column 2"):
        load_index_definitions(tmp_path)


def test_top_level_must_be_an_object(tmp_path: Path) -> None:
    _write(tmp_path, "list.json", [])

    with pytest.raises(IndexDefinitionError, match="top-level value must be a JSON object"):
        load_index_definitions(tmp_path)


def test_missing_keys_is_an_error(tmp_path: Path) -> None:
    _write(tmp_path, "indexes.json", _payload(indexes=[{"options": {"name": "x"}}]))

    with pytest.raises(
        IndexDefinitionError, match=r"indexes\[0\]\.keys must be a non-empty object"
    ):
        load_index_definitions(tmp_path)


def test_empty_keys_is_an_error(tmp_path: Path) -> None:
    _write(tmp_path, "indexes.json", _payload(indexes=[{"keys": {}}]))

    with pytest.raises(IndexDefinitionError, match="keys must be a non-empty object"):
        load_index_definitions(tmp_path)


def test_unknown_top_level_field_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "indexes.json", _payload(indexies=[]))

    with pytest.raises(IndexDefinitionError, match="unknown field"):
        load_index_definitions(tmp_path)


def test_unknown_index_field_is_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "indexes.json",
        _payload(indexes=[{"keys": {"a": 1}, "option": {"name": "typo"}}]),
    )

    with pytest.raises(IndexDefinitionError, match=r"indexes\[0\]: unknown field"):
        load_index_definitions(tmp_path)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"database": ""}, "'database' must be a non-empty string"),
        ({"collection": 42}, "'collection' must be a non-empty string"),
        ({"indexes": []}, "'indexes' must be a non-empty array"),
        ({"indexes": {}}, "'indexes' must be a non-empty array"),
        (
            {"indexes": [{"keys": {"a": True}}]},
            r"indexes\[0\]\.keys\.a must be 1, -1, 1\.0, -1\.0, or a string index type",
        ),
        (
            {"indexes": [{"keys": {"a": 1}, "options": "name"}]},
            r"indexes\[0\]\.options must be a JSON object",
        ),
        (
            {"indexes": [{"keys": {"a": 1}, "commitQuorum": True}]},
            r"indexes\[0\]\.commitQuorum must be an integer or a string",
        ),
    ],
)
def test_invalid_documents(tmp_path: Path, overrides: dict[str, object], message: str) -> None:
    _write(tmp_path, "indexes.json", _payload(**overrides))

    with pytest.raises(IndexDefinitionError, match=message):
        load_index_definitions(tmp_path)


def test_all_broken_files_are_reported_together(tmp_path: Path) -> None:
    _write(tmp_path, "a.json", {"collection": "only-collection"})
    _write(tmp_path, "b.json", {"database": "d", "collection": "c", "indexes": []})

    with pytest.raises(IndexDefinitionError) as error_info:
        load_index_definitions(tmp_path)

    message = str(error_info.value)
    assert "a.json" in message
    assert "b.json" in message
    assert "'database' must be a non-empty string" in message
    assert "'indexes' must be a non-empty array" in message
