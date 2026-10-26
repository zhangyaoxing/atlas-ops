"""Command-line interface for atlas-ops."""

from __future__ import annotations

import logging

import typer

from atlas_ops import __version__
from atlas_ops.commands.index import index
from atlas_ops.commands.search import search
from atlas_ops.commands.trigger import trigger
from atlas_ops.env import load_env
from atlas_ops.log import configure_logging

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="atlas-ops",
    help="Command-line tool for MongoDB Atlas operations.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"atlas-ops {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show the version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Command-line tool for MongoDB Atlas operations."""
    env_file = load_env()
    configure_logging()
    if env_file is not None:
        logger.debug("Loaded environment variables from %s", env_file)


@app.command()
def version() -> None:
    """Print the current version."""
    typer.echo(__version__)


app.command()(index)
app.command()(search)
app.command()(trigger)
