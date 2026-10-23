from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_working_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run every test in its own directory so a developer's .env is never read."""
    monkeypatch.chdir(tmp_path)
