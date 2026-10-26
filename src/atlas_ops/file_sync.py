"""Helpers for keeping an export directory in sync with remote state."""

from __future__ import annotations

from pathlib import Path


def remove_stale_json_files(directory: Path, keep: set[str]) -> list[Path]:
    """Delete top-level ``*.json`` files whose names are not in *keep*."""
    removed: list[Path] = []
    for path in sorted(directory.glob("*.json")):
        if path.is_file() and path.name not in keep:
            path.unlink()
            removed.append(path)
    return removed
