"""Helpers for safe JSON export filenames."""

from __future__ import annotations

import re

_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def json_filename(*parts: str) -> str:
    """Join *parts* with dots and replace characters unsafe in filenames."""
    namespace = ".".join(part for part in parts if part)
    return f"{_UNSAFE_FILENAME.sub('_', namespace)}.json"
