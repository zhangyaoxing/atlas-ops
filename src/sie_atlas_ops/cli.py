"""Command-line interface for sie-atlas-ops.

Add new functionality as ``@app.command()`` functions, or group related
commands into a sub-application:

    users_app = typer.Typer(help="Manage users.")
    app.add_typer(users_app, name="users")

    @users_app.command("list")
    def list_users() -> None: ...
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import NoReturn

import httpx
import typer
from pyatlassdk import AtlasApiError
from rich.markup import escape

from sie_atlas_ops import __version__
from sie_atlas_ops.atlas import (
    ClusterTarget,
    ConnectionOption,
    client_from_env,
    connection_options,
    describe_atlas_error,
    find_clusters,
    is_object_id,
)
from sie_atlas_ops.env import load_env
from sie_atlas_ops.errors import AtlasOperationError, OpsError
from sie_atlas_ops.index_definitions import CollectionIndexes, load_index_definitions
from sie_atlas_ops.log import configure_logging

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="sie-atlas-ops",
    help="Command-line tool for SIE Atlas operations.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"sie-atlas-ops {__version__}")
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
    """Command-line tool for SIE Atlas operations."""
    env_file = load_env()
    configure_logging()
    if env_file is not None:
        logger.debug("Loaded environment variables from %s", env_file)


@app.command()
def version() -> None:
    """Print the current version."""
    typer.echo(__version__)


@app.command()
def index(
    directory: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        dir_okay=True,
        readable=True,
        resolve_path=True,
        help="Directory containing one JSON file per collection.",
    ),
    cluster: str = typer.Argument(
        ...,
        help="Atlas cluster name, or a 24-character hexadecimal cluster ID.",
    ),
) -> None:
    """Load index definitions, locate the cluster, and pick a connection string.

    Every ``*.json`` file in DIRECTORY is parsed and validated. The cluster is
    looked up by ID or by name through the Atlas API, and one of its connection
    strings is selected interactively. Creating the indexes is not implemented
    yet.
    """
    try:
        definitions = load_index_definitions(directory)
        client = client_from_env()
        with client:
            matches = find_clusters(client, cluster)
            if not matches:
                raise AtlasOperationError(f"No cluster found for {cluster!r}.")
            target = matches[0] if len(matches) == 1 else _choose_cluster(matches)
            _log_found_cluster(target, cluster)
            detail = client.clusters.get_group_cluster(
                cluster_name=target.cluster_name, group_id=target.project_id
            )
            options = connection_options(detail)
            if not options:
                raise AtlasOperationError(
                    f"Cluster {target.cluster_name!r} exposes no connection strings."
                )
            _log_connection_strings(target.cluster_name, options)
            selected = _choose_connection(options)
            logger.info(
                "Selected connection string [cyan]%s[/cyan]: %s",
                escape(selected.label),
                escape(selected.uri),
            )
    except OpsError as exc:
        _fail(str(exc))
    except AtlasApiError as exc:
        _fail(describe_atlas_error(exc))
    except httpx.HTTPError as exc:
        _fail(f"Could not reach the Atlas API: {exc}")

    _print_summary(directory, definitions, target, selected)


def _choose_cluster(matches: list[ClusterTarget]) -> ClusterTarget:
    typer.echo("Multiple clusters match; select one:")
    for position, match in enumerate(matches, 1):
        suffix = f" ({match.cluster_id})" if match.cluster_id else ""
        typer.echo(
            f"  [{position}] {match.cluster_name}{suffix} "
            f"in project {match.project_name} ({match.project_id})"
        )
    return matches[_prompt_choice(len(matches), "Cluster")]


def _choose_connection(options: list[ConnectionOption]) -> ConnectionOption:
    if len(options) == 1:
        return options[0]
    return options[_prompt_choice(len(options), f"Select connection string (1-{len(options)})")]


def _log_found_cluster(target: ClusterTarget, reference: str) -> None:
    lookup = "ID" if is_object_id(reference.strip()) else "name"
    logger.info(
        "Found cluster [green]%s[/green] (%s) by %s in project %s (%s)",
        escape(target.cluster_name),
        target.cluster_id or "unknown ID",
        lookup,
        escape(target.project_name),
        target.project_id,
    )


def _log_connection_strings(cluster_name: str, options: list[ConnectionOption]) -> None:
    logger.info(
        "Found %d connection string(s) for cluster [green]%s[/green]",
        len(options),
        escape(cluster_name),
    )
    for position, option in enumerate(options, 1):
        logger.info(
            "  %d. [cyan]%s[/cyan]: %s",
            position,
            escape(option.label),
            escape(option.uri),
        )


def _prompt_choice(count: int, label: str) -> int:
    """Prompt until the user enters a valid 1-based index; return it 0-based."""
    while True:
        choice = typer.prompt(label, type=int)
        if 1 <= choice <= count:
            return choice - 1
        typer.secho(f"Enter a number between 1 and {count}.", fg=typer.colors.YELLOW, err=True)


def _print_summary(
    directory: Path,
    definitions: list[CollectionIndexes],
    target: ClusterTarget,
    selected: ConnectionOption,
) -> None:
    typer.echo("")
    typer.echo(f"Cluster:    {target.cluster_name} ({target.cluster_id or 'unknown ID'})")
    typer.echo(f"Project:    {target.project_name} ({target.project_id})")
    typer.echo(f"Connection: {selected.uri}")
    typer.echo(f"            [{selected.label}]")
    typer.echo("")
    typer.echo(f"Loaded {len(definitions)} collection definition(s) from {directory}:")
    for definition in definitions:
        typer.echo(
            f"  {definition.database}.{definition.collection}: "
            f"{len(definition.indexes)} index(es) ({definition.path.name})"
        )
    typer.echo("")
    typer.echo("Index creation is not implemented yet; stopping after connection selection.")


def _fail(message: str) -> NoReturn:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)
