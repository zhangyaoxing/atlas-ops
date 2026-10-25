"""Load environment variables from a .env file."""

from __future__ import annotations

from pathlib import Path

from dotenv import find_dotenv, load_dotenv


def load_env() -> Path | None:
    """Load the nearest ``.env`` file, starting at the working directory.

    The search walks up from the current directory. Variables already exported
    in the shell are not overridden, so real environment values take precedence
    over the file. Returns the path that was loaded, or ``None`` when no file
    was found.
    """
    dotenv_path = find_dotenv(usecwd=True)
    if not dotenv_path:
        return None
    load_dotenv(dotenv_path, override=False)
    return Path(dotenv_path)
