import logging
from io import StringIO

from rich.console import Console
from rich.logging import RichHandler

from sie_atlas_ops.log import LOGGER_NAME, configure_logging


def test_configure_logging_attaches_one_rich_handler() -> None:
    configure_logging()
    configure_logging()

    handlers = [
        handler
        for handler in logging.getLogger(LOGGER_NAME).handlers
        if isinstance(handler, RichHandler)
    ]
    assert len(handlers) == 1


def test_cluster_name_renders_green(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = StringIO()
    console = Console(file=stream, force_terminal=True, color_system="standard", width=120)
    handler = RichHandler(console=console, markup=True, show_time=False, show_path=False)
    logger = logging.getLogger("sie_atlas_ops.test_render")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        logger.info("Found cluster [green]cluster0[/green] in project Project Atlas")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(logging.NOTSET)

    assert "\x1b[32mcluster0\x1b[0m" in stream.getvalue()
