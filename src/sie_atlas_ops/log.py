"""Logging configuration for the CLI.

Logs are written to stderr through Rich so that stdout stays reserved for
command results. Rich honours ``NO_COLOR``/``FORCE_COLOR`` and colourises only
when the stream is a terminal.
"""

from __future__ import annotations

import logging

from rich.console import Console
from rich.logging import RichHandler

LOGGER_NAME = "sie_atlas_ops"


def configure_logging(level: int = logging.INFO) -> None:
    """Attach a Rich handler to the package logger once."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    if any(isinstance(handler, RichHandler) for handler in logger.handlers):
        return

    handler = RichHandler(
        console=Console(stderr=True),
        show_time=True,
        show_level=True,
        show_path=False,
        markup=True,
        rich_tracebacks=True,
        log_time_format="%H:%M:%S",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
