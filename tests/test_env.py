from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sie_atlas_ops.atlas import client_from_env
from sie_atlas_ops.cli import app
from sie_atlas_ops.env import load_env


def _write_env(directory: Path, content: str) -> Path:
    path = directory / ".env"
    path.write_text(content, encoding="utf-8")
    return path


def _clear_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATLAS_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("ATLAS_PRIVATE_KEY", raising=False)


def test_loads_dotenv_from_working_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_credentials(monkeypatch)
    path = _write_env(Path.cwd(), "ATLAS_PUBLIC_KEY=public\nATLAS_PRIVATE_KEY=private\n")

    assert load_env() == path
    assert os.environ["ATLAS_PUBLIC_KEY"] == "public"
    assert os.environ["ATLAS_PRIVATE_KEY"] == "private"


def test_finds_dotenv_in_parent_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_credentials(monkeypatch)
    path = _write_env(Path.cwd(), "ATLAS_PUBLIC_KEY=parent\n")
    nested = Path.cwd() / "nested" / "deeper"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    assert load_env() == path.resolve()
    assert os.environ["ATLAS_PUBLIC_KEY"] == "parent"


def test_shell_environment_takes_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_credentials(monkeypatch)
    monkeypatch.setenv("ATLAS_PUBLIC_KEY", "from-shell")
    monkeypatch.setenv("ATLAS_PRIVATE_KEY", "from-shell")
    _write_env(Path.cwd(), "ATLAS_PUBLIC_KEY=from-file\nATLAS_PRIVATE_KEY=from-file\n")

    load_env()

    assert os.environ["ATLAS_PUBLIC_KEY"] == "from-shell"
    assert os.environ["ATLAS_PRIVATE_KEY"] == "from-shell"


def test_returns_none_when_no_dotenv_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_credentials(monkeypatch)
    monkeypatch.setattr("sie_atlas_ops.env.find_dotenv", lambda **_: "")

    assert load_env() is None
    assert "ATLAS_PUBLIC_KEY" not in os.environ


def test_loaded_values_are_accepted_by_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_credentials(monkeypatch)
    _write_env(Path.cwd(), "ATLAS_PUBLIC_KEY=public\nATLAS_PRIVATE_KEY=private\n")

    assert load_env() is not None
    client = client_from_env()
    try:
        assert client.auth_mode == "digest"
    finally:
        client.close()


def test_cli_loads_dotenv_before_running_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr("sie_atlas_ops.cli.load_env", lambda: calls.append(True) or None)

    result = CliRunner().invoke(app, ["version"])

    assert result.exit_code == 0
    assert calls == [True]
