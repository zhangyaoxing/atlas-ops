"""Command-line interface for sie-atlas-ops.

Add new functionality as ``@app.command()`` functions, or group related
commands into a sub-application:

    users_app = typer.Typer(help="Manage users.")
    app.add_typer(users_app, name="users")

    @users_app.command("list")
    def list_users() -> None: ...
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

import httpx
import typer
from pyatlassdk import AtlasApiError
from pymongo.errors import PyMongoError
from rich.markup import escape

from sie_atlas_ops import __version__
from sie_atlas_ops.atlas import (
    CLUSTER_NAME_ENV,
    DB_PASSWORD_ENV,
    DB_USERNAME_ENV,
    PROJECT_ID_ENV,
    AtlasClientLike,
    ClusterTarget,
    ConnectionOption,
    TemporaryUser,
    client_from_env,
    connection_options,
    create_temporary_user,
    delete_temporary_user,
    describe_atlas_error,
    find_clusters,
    get_project,
    is_object_id,
)
from sie_atlas_ops.env import load_env
from sie_atlas_ops.errors import AtlasOperationError, ConfigurationError, OpsError
from sie_atlas_ops.index.create import CreateResult, create_indexes
from sie_atlas_ops.index.definitions import CollectionIndexes, load_index_definitions
from sie_atlas_ops.index.mongo import (
    MissingNamespace,
    MongoClientLike,
    build_authenticated_uri,
    create_client,
    find_missing_namespaces,
    wait_for_user,
)
from sie_atlas_ops.index.plan import IndexAction, IndexPlanItem, plan_indexes
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
    project: str | None = typer.Option(
        None,
        "--project-id",
        "-g",
        help=(
            "Atlas project ID (24 hexadecimal characters). "
            f"Falls back to {PROJECT_ID_ENV}, then prompts."
        ),
    ),
    cluster: str | None = typer.Option(
        None,
        "--cluster",
        "-c",
        help=(f"Atlas cluster ID or name. Falls back to {CLUSTER_NAME_ENV}, then prompts."),
    ),
    db_username: str | None = typer.Option(
        None,
        "--db-username",
        "-u",
        help=(
            "Existing database username. Falls back to "
            f"{DB_USERNAME_ENV}; requires a password and skips the "
            "temporary user."
        ),
    ),
    db_password: str | None = typer.Option(
        None,
        "--db-password",
        "-p",
        help=(
            "Existing database password. Falls back to "
            f"{DB_PASSWORD_ENV}; requires a username and skips the "
            "temporary user."
        ),
    ),
) -> None:
    """Load index definitions, locate the cluster, and pick a connection string.

    Every ``*.json`` file in DIRECTORY is parsed and validated. The project is
    resolved from the option, the environment, or an interactive prompt; the
    cluster is looked up inside that project, and one of its connection strings
    is selected interactively. Existing database credentials are used when
    supplied; otherwise a cluster-scoped temporary database user is created,
    expires after six hours, and is deleted on every exit path. Missing
    databases and collections are reported and confirmed, existing indexes are
    compared by key pattern and options, and the remaining indexes are created.
    """
    try:
        definitions = load_index_definitions(directory)
        provided_credentials = _resolve_db_credentials(db_username, db_password)
        with client_from_env() as client:
            project_id, cluster_ref = _resolve_references(project, cluster)
            target_project = get_project(client, project_id)
            matches = find_clusters(client, target_project, cluster_ref)
            if not matches:
                raise AtlasOperationError(f"No cluster found for {cluster_ref!r}.")
            target = matches[0] if len(matches) == 1 else _choose_cluster(matches)
            _log_found_cluster(target, cluster_ref)
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
                'Selected connection string [cyan]%s[/cyan]: "%s"',
                escape(selected.label),
                escape(selected.uri),
            )

            temporary_user: TemporaryUser | None = None
            try:
                if provided_credentials is None:
                    temporary_user = create_temporary_user(
                        client, target_project, target.cluster_name
                    )
                    logger.info(
                        "Created temporary user [cyan]%s[/cyan] (expires %s UTC)",
                        escape(temporary_user.username),
                        _format_utc(temporary_user.delete_after),
                    )
                    db_user = temporary_user.username
                    db_pass = temporary_user.password
                else:
                    db_user, db_pass = provided_credentials
                    logger.info(
                        "Using provided database user [cyan]%s[/cyan].",
                        escape(db_user),
                    )

                mongo_client = _open_mongo_client(selected, db_user, db_pass)
                try:
                    if temporary_user is not None:
                        logger.info(
                            "Waiting for temporary user [cyan]%s[/cyan] to become active...",
                            escape(temporary_user.username),
                        )
                        wait_for_user(mongo_client)
                        logger.info("Temporary user is active.")
                    missing = find_missing_namespaces(mongo_client, definitions)
                    if missing:
                        _report_missing(missing)
                        if not typer.confirm(
                            "Continue and let MongoDB create the missing databases/collections?",
                            default=False,
                        ):
                            typer.echo("Aborted.")
                            raise typer.Exit(code=1)
                    plan = plan_indexes(mongo_client, definitions, missing)
                    _log_index_plan(plan)
                    pending = sum(1 for item in plan if item.action is IndexAction.CREATE)
                    if pending:
                        logger.info("Creating %d index(es)...", pending)
                    created = create_indexes(mongo_client, plan)
                finally:
                    mongo_client.close()

                _log_created(created)
            finally:
                if temporary_user is not None:
                    _delete_temporary_user(client, project_id, temporary_user)
    except OpsError as exc:
        _fail(str(exc))
    except AtlasApiError as exc:
        _fail(describe_atlas_error(exc))
    except PyMongoError as exc:
        _fail(f"MongoDB request failed: {exc}")
    except httpx.HTTPError as exc:
        _fail(f"Could not reach the Atlas API: {exc}")

    _print_summary(
        directory,
        definitions,
        target,
        selected,
        sum(len(result.names) for result in created),
    )


def _resolve_references(project: str | None, cluster: str | None) -> tuple[str, str]:
    """Resolve the project and cluster from options, environment, or prompts."""
    project_ref = (project or "").strip()
    cluster_ref = (cluster or "").strip()

    if not project_ref:
        project_ref = os.environ.get(PROJECT_ID_ENV, "").strip()
        if project_ref:
            logger.info("Using project %s from %s", project_ref, PROJECT_ID_ENV)
    if not cluster_ref:
        cluster_ref = os.environ.get(CLUSTER_NAME_ENV, "").strip()
        if cluster_ref:
            logger.info(
                "Using cluster [green]%s[/green] from %s",
                escape(cluster_ref),
                CLUSTER_NAME_ENV,
            )

    if not project_ref:
        project_ref = _prompt_project()
    if not cluster_ref:
        cluster_ref = _prompt_cluster()

    if not is_object_id(project_ref):
        raise AtlasOperationError(
            f"Invalid project ID {project_ref!r}: expected 24 hexadecimal characters."
        )
    return project_ref, cluster_ref


def _open_mongo_client(selected: ConnectionOption, username: str, password: str) -> MongoClientLike:
    try:
        uri = build_authenticated_uri(selected.uri, username, password)
    except ValueError as exc:
        raise AtlasOperationError(str(exc)) from exc
    return create_client(uri)


def _resolve_db_credentials(username: str | None, password: str | None) -> tuple[str, str] | None:
    """Resolve user-provided database credentials, if any.

    Command-line values take precedence over ``ATLAS_DB_USERNAME`` and
    ``ATLAS_DB_PASSWORD``. Both values must be present or both omitted.
    """
    resolved_username = (username or "").strip() or os.environ.get(DB_USERNAME_ENV, "").strip()
    resolved_password = (password or "") or os.environ.get(DB_PASSWORD_ENV, "")
    if bool(resolved_username) != bool(resolved_password):
        raise ConfigurationError("Provide both a database username and password, or neither.")
    if not resolved_username:
        return None
    return resolved_username, resolved_password


def _report_missing(missing: list[MissingNamespace]) -> None:
    for item in missing:
        database = escape(item.database)
        collection = escape(item.collection)
        if item.database_exists:
            logger.warning('Collection "%s.%s" does not exist.', database, collection)
        else:
            logger.warning(
                'Database "%s" does not exist (collection "%s" would be created).',
                database,
                collection,
            )


def _log_index_plan(plan: list[IndexPlanItem]) -> None:
    for item in plan:
        keys = escape(json.dumps(item.spec.keys))
        if item.action is IndexAction.IDENTICAL:
            logger.info(
                'Index %s on "%s.%s" already exists with identical options; skipping.',
                keys,
                escape(item.database),
                escape(item.collection),
            )
        elif item.action is IndexAction.DIFFERENT:
            existing_options = escape(
                json.dumps(item.existing_options or {}, default=str, sort_keys=True)
            )
            requested_options = escape(
                json.dumps(item.spec.options or {}, default=str, sort_keys=True)
            )
            logger.warning(
                'Index %s on "%s.%s" already exists with different options '
                '(existing name "%s", existing options %s, requested options %s); '
                "skipping.",
                keys,
                escape(item.database),
                escape(item.collection),
                escape(item.existing_name or ""),
                existing_options,
                requested_options,
            )


def _log_created(created: list[CreateResult]) -> None:
    if not created:
        logger.info("No indexes need to be created.")
        return
    for result in created:
        logger.info(
            'Created %d index(es) on "%s.%s": %s',
            len(result.names),
            escape(result.database),
            escape(result.collection),
            escape(", ".join(result.names)),
        )


def _delete_temporary_user(
    client: AtlasClientLike, project_id: str, temporary_user: TemporaryUser
) -> None:
    logger.info("Deleting temporary user [cyan]%s[/cyan]...", escape(temporary_user.username))
    try:
        delete_temporary_user(client, project_id, temporary_user.username)
    except Exception as exc:  # cleanup must not mask the original failure
        logger.warning(
            "Could not delete temporary user [cyan]%s[/cyan]: %s",
            escape(temporary_user.username),
            exc,
        )
    else:
        logger.info("Temporary user deleted.")


def _prompt_project() -> str:
    while True:
        value = typer.prompt("Project ID (24 hexadecimal characters)").strip()
        if is_object_id(value):
            return value
        typer.secho(
            "Project ID must be 24 hexadecimal characters.",
            fg=typer.colors.YELLOW,
            err=True,
        )


def _prompt_cluster() -> str:
    while True:
        value = typer.prompt("Cluster ID or name").strip()
        if value:
            return value
        typer.secho("Cluster ID or name must not be empty.", fg=typer.colors.YELLOW, err=True)


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
        "Found cluster [green]%s[/green] (%s) by %s in project [green]%s[/green] (%s)",
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
            '  %d. [cyan]%s[/cyan]: "%s"',
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
    created_count: int,
) -> None:
    typer.echo("")
    typer.echo(f"Cluster:    {target.cluster_name} ({target.cluster_id or 'unknown ID'})")
    typer.echo(f"Project:    {target.project_name} ({target.project_id})")
    typer.echo(f'Connection: "{selected.uri}"')
    typer.echo(f"            [{selected.label}]")
    typer.echo("")
    typer.echo(f"Loaded {len(definitions)} collection definition(s) from {directory}:")
    for definition in definitions:
        typer.echo(
            f"  {definition.database}.{definition.collection}: "
            f"{len(definition.indexes)} index(es) ({definition.path.name})"
        )
    typer.echo("")
    if created_count:
        typer.echo(f"Index creation complete: {created_count} index(es) created.")
    else:
        typer.echo("Index creation complete: no new indexes.")


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _fail(message: str) -> NoReturn:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)
