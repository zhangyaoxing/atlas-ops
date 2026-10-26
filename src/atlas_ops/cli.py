"""Command-line interface for atlas-ops.

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
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

import httpx
import typer
from pyatlassdk import AtlasAdminApiError, AtlasAppApiError
from pymongo.errors import PyMongoError
from rich.markup import escape

from atlas_ops import __version__
from atlas_ops.atlas import (
    CLUSTER_NAME_ENV,
    DB_PASSWORD_ENV,
    DB_USERNAME_ENV,
    PROJECT_ID_ENV,
    AtlasClientLike,
    ClusterTarget,
    ConnectionOption,
    TemporaryUser,
    app_services_client_from_env,
    client_from_env,
    connection_options,
    create_temporary_user,
    delete_temporary_user,
    describe_atlas_error,
    find_clusters,
    get_project,
    is_object_id,
)
from atlas_ops.env import load_env
from atlas_ops.errors import AtlasOperationError, ConfigurationError, OpsError
from atlas_ops.index.create import CreateResult, create_indexes
from atlas_ops.index.definitions import CollectionIndexes, load_index_definitions
from atlas_ops.index.drop import DroppedIndex, drop_indexes
from atlas_ops.index.export import ExportResult, export_indexes
from atlas_ops.index.mongo import (
    MissingNamespace,
    MongoClientLike,
    build_authenticated_uri,
    create_client,
    find_missing_namespaces,
    wait_for_user,
)
from atlas_ops.index.plan import (
    ExtraIndex,
    IndexAction,
    IndexPlan,
    IndexPlanItem,
    plan_indexes,
)
from atlas_ops.log import configure_logging
from atlas_ops.search.apply import SearchApplyResult, apply_search_plan
from atlas_ops.search.definitions import (
    SearchCollectionIndexes,
    load_search_index_definitions,
)
from atlas_ops.search.export import SearchExportResult, export_search_indexes
from atlas_ops.search.plan import (
    SearchAction,
    SearchExtra,
    SearchPlan,
    plan_search_indexes,
)
from atlas_ops.search.remote import fetch_search_indexes
from atlas_ops.trigger.export import TriggerExportResult, export_triggers

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


@app.command()
def index(
    directory: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        dir_okay=True,
        readable=True,
        resolve_path=True,
        help=(
            "Directory with index definition JSON files, or the export "
            "destination when --export is used."
        ),
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
    export: bool = typer.Option(
        False,
        "--export",
        "-e",
        help=(
            "Export existing indexes from MongoDB into DIRECTORY instead of applying definitions."
        ),
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

    In the default mode, every ``*.json`` file in DIRECTORY is parsed and
    validated. The project is resolved from the option, the environment, or an
    interactive prompt; the cluster is looked up inside that project, and one of
    its connection strings is selected interactively. Existing database
    credentials are used when supplied; otherwise a cluster-scoped temporary
    database user is created, expires after six hours, and is deleted on every
    exit path. Missing databases and collections are reported and confirmed,
    existing indexes are compared by key pattern and options, and a preview
    table is printed. After the preview is confirmed, missing indexes are
    created and undeclared indexes are dropped. With ``--export``, existing
    indexes are instead exported to DIRECTORY and no index is created or dropped.
    """
    try:
        provided_credentials = _resolve_db_credentials(db_username, db_password)
        definitions: list[CollectionIndexes] | None = None
        created: list[CreateResult] = []
        dropped: list[DroppedIndex] = []
        plan: IndexPlan | None = None
        export_result: ExportResult | None = None
        if not export:
            definitions = load_index_definitions(directory)
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

                    if export:
                        logger.info("Exporting index definitions to %s...", directory)
                        export_result = export_indexes(mongo_client, directory)
                        logger.info(
                            "Exported %d index(es) from %d collection(s).",
                            export_result.indexes,
                            export_result.files,
                        )
                    else:
                        assert definitions is not None
                        missing = find_missing_namespaces(mongo_client, definitions)
                        if missing:
                            _report_missing(missing)
                            if not typer.confirm(
                                "Continue and let MongoDB create the missing "
                                "databases/collections?",
                                default=False,
                            ):
                                typer.echo("Aborted.")
                                raise typer.Exit(code=1)
                        plan = plan_indexes(mongo_client, definitions, missing)
                        _log_index_plan(plan.items)
                        if plan.extras:
                            _log_extra_indexes(plan.extras)
                        _print_index_preview(directory, definitions, plan)
                        has_changes = bool(plan.extras) or any(
                            item.action is IndexAction.CREATE for item in plan.items
                        )
                        if not has_changes:
                            typer.echo("No changes to apply.")
                        elif not _confirm_changes():
                            typer.echo("Aborted.")
                            raise typer.Exit(code=1)
                        pending = sum(1 for item in plan.items if item.action is IndexAction.CREATE)
                        if pending:
                            logger.info("Creating %d index(es)...", pending)
                        if plan.extras:
                            logger.info("Deleting %d index(es)...", len(plan.extras))
                        created = create_indexes(mongo_client, plan.items)
                        dropped = drop_indexes(mongo_client, list(plan.extras))
                finally:
                    mongo_client.close()

                if not export:
                    _log_created(created)
                    _log_dropped(dropped)
            finally:
                if temporary_user is not None:
                    _delete_temporary_user(client, project_id, temporary_user)
    except OpsError as exc:
        _fail(str(exc))
    except AtlasAdminApiError as exc:
        _fail(describe_atlas_error(exc))
    except PyMongoError as exc:
        _fail(f"MongoDB request failed: {exc}")
    except OSError as exc:
        _fail(f"Could not write exported indexes: {exc}")
    except httpx.HTTPError as exc:
        _fail(f"Could not reach the Atlas API: {exc}")

    if export:
        assert export_result is not None
        _print_export_summary(directory, target, selected, export_result)
    else:
        assert definitions is not None
        assert plan is not None
        _print_summary(directory, definitions, plan, dropped, target, selected)


@app.command()
def search(
    directory: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        dir_okay=True,
        readable=True,
        resolve_path=True,
        help=(
            "Directory with search index definition JSON files, or the export "
            "destination when --export is used."
        ),
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
    export: bool = typer.Option(
        False,
        "--export",
        "-e",
        help="Export Atlas Search indexes to DIRECTORY instead of syncing definitions.",
    ),
) -> None:
    """Synchronize or export Atlas Search indexes for one cluster.

    By default, local JSON definitions in DIRECTORY are compared with the
    cluster: missing indexes are created, changed definitions are updated,
    unchanged indexes are skipped, and undeclared remote indexes are planned for
    deletion. A preview table is printed and must be confirmed before any change
    is applied. With ``--export``, definitions are written instead.

    The cluster-wide Search index endpoint is used, so the command needs only
    Atlas API credentials and the project/cluster. No MongoDB connection or
    temporary database user is created.
    """
    definitions: list[SearchCollectionIndexes] | None = None
    target: ClusterTarget | None = None
    export_result: SearchExportResult | None = None
    sync_plan: SearchPlan | None = None
    sync_result: SearchApplyResult | None = None
    try:
        if not export:
            definitions = load_search_index_definitions(directory)
        with client_from_env() as client:
            project_id, cluster_ref = _resolve_references(project, cluster)
            target_project = get_project(client, project_id)
            matches = find_clusters(client, target_project, cluster_ref)
            if not matches:
                raise AtlasOperationError(f"No cluster found for {cluster_ref!r}.")
            target = matches[0] if len(matches) == 1 else _choose_cluster(matches)
            _log_found_cluster(target, cluster_ref)

            if export:
                logger.info("Exporting search index definitions to %s...", directory)
                export_result = export_search_indexes(
                    client,
                    project_id,
                    target.cluster_name,
                    directory,
                )
                logger.info(
                    "Exported %d search index(es) from %d collection(s).",
                    export_result.indexes,
                    export_result.files,
                )
            else:
                assert definitions is not None
                remote = fetch_search_indexes(client, project_id, target.cluster_name)
                sync_plan = plan_search_indexes(remote, definitions)
                _log_search_plan(sync_plan)
                if sync_plan.extras:
                    _log_search_extras(sync_plan.extras)
                _print_search_preview(directory, definitions, sync_plan)
                has_changes = bool(sync_plan.extras) or any(
                    item.action is not SearchAction.IDENTICAL for item in sync_plan.items
                )
                if not has_changes:
                    typer.echo("No changes to apply.")
                elif not _confirm_changes():
                    typer.echo("Aborted.")
                    raise typer.Exit(code=1)
                sync_result = apply_search_plan(
                    client,
                    project_id,
                    target.cluster_name,
                    sync_plan,
                    delete_extras=True,
                )
                logger.info(
                    "Search sync complete: %d created, %d updated, %d deleted.",
                    sync_result.created,
                    sync_result.updated,
                    sync_result.deleted,
                )
    except OpsError as exc:
        _fail(str(exc))
    except AtlasAdminApiError as exc:
        _fail(describe_atlas_error(exc))
    except OSError as exc:
        _fail(f"Could not write exported search indexes: {exc}")
    except httpx.HTTPError as exc:
        _fail(f"Could not reach the Atlas API: {exc}")

    assert target is not None
    if export:
        assert export_result is not None
        _print_search_export_summary(directory, target, export_result)
    else:
        assert definitions is not None
        assert sync_plan is not None
        assert sync_result is not None
        _print_search_sync_summary(
            directory,
            target,
            definitions,
            sync_plan,
            sync_result,
        )


@app.command()
def trigger(
    directory: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        dir_okay=True,
        readable=True,
        resolve_path=True,
        help="Directory for exported trigger JSON files.",
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
    export: bool = typer.Option(
        False,
        "--export",
        "-e",
        help="Export all App Services trigger definitions to DIRECTORY.",
    ),
) -> None:
    """Export App Services trigger definitions for one cluster.

    Uses the App Services Admin API client and only needs Atlas API
    credentials, the project ID, and the cluster. Triggers are filtered to
    database triggers on data sources linked to that cluster and grouped by
    database collection. No MongoDB connection or temporary database user is
    involved.
    """
    target: ClusterTarget | None = None
    result: TriggerExportResult | None = None
    try:
        if not export:
            raise AtlasOperationError("The trigger command currently supports only --export/-e.")
        with client_from_env() as client:
            project_id, cluster_ref = _resolve_references(project, cluster)
            target_project = get_project(client, project_id)
            matches = find_clusters(client, target_project, cluster_ref)
            if not matches:
                raise AtlasOperationError(f"No cluster found for {cluster_ref!r}.")
            target = matches[0] if len(matches) == 1 else _choose_cluster(matches)
            _log_found_cluster(target, cluster_ref)
        with app_services_client_from_env() as app_client:
            logger.info("Exporting trigger definitions to %s...", directory)
            result = export_triggers(
                app_client,
                project_id,
                target.cluster_name,
                target.cluster_id,
                directory,
            )
            logger.info(
                "Exported %d trigger(s) from %d collection(s) across %d app(s).",
                result.triggers,
                result.files,
                result.apps,
            )
    except OpsError as exc:
        _fail(str(exc))
    except (AtlasAdminApiError, AtlasAppApiError) as exc:
        _fail(describe_atlas_error(exc))
    except OSError as exc:
        _fail(f"Could not write exported triggers: {exc}")
    except httpx.HTTPError as exc:
        _fail(f"Could not reach the Atlas API: {exc}")

    assert target is not None
    assert result is not None
    _print_trigger_export_summary(directory, target, result)


def _resolve_project(project: str | None) -> str:
    """Resolve the project from the option, environment, or a prompt."""
    project_ref = (project or "").strip()
    if not project_ref:
        project_ref = os.environ.get(PROJECT_ID_ENV, "").strip()
        if project_ref:
            logger.info("Using project %s from %s", project_ref, PROJECT_ID_ENV)
    if not project_ref:
        project_ref = _prompt_project()
    if not is_object_id(project_ref):
        raise AtlasOperationError(
            f"Invalid project ID {project_ref!r}: expected 24 hexadecimal characters."
        )
    return project_ref


def _resolve_references(project: str | None, cluster: str | None) -> tuple[str, str]:
    """Resolve the project and cluster from options, environment, or prompts."""
    project_ref = _resolve_project(project)
    cluster_ref = (cluster or "").strip()
    if not cluster_ref:
        cluster_ref = os.environ.get(CLUSTER_NAME_ENV, "").strip()
        if cluster_ref:
            logger.info(
                "Using cluster [green]%s[/green] from %s",
                escape(cluster_ref),
                CLUSTER_NAME_ENV,
            )
    if not cluster_ref:
        cluster_ref = _prompt_cluster()
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


def _log_index_plan(plan: Sequence[IndexPlanItem]) -> None:
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
            logger.warning("[yellow]Altering index is not currently implemented.[/yellow]")
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


def _log_extra_indexes(extras: tuple[ExtraIndex, ...]) -> None:
    for extra in extras:
        logger.warning(
            'Index "%s" on "%s.%s" is not present in the definitions (keys %s).',
            escape(extra.name),
            escape(extra.database),
            escape(extra.collection),
            escape(json.dumps(extra.keys)),
        )


def _log_dropped(dropped: list[DroppedIndex]) -> None:
    for item in dropped:
        logger.info(
            'Dropped index "%s" on "%s.%s".',
            escape(item.name),
            escape(item.database),
            escape(item.collection),
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


def _confirm_changes() -> bool:
    return typer.confirm("Proceed with these changes?", default=False)


def _print_index_preview(
    directory: Path,
    definitions: list[CollectionIndexes],
    plan: IndexPlan,
) -> None:
    rows = _collection_summary(definitions, plan, plan.extras)
    typer.echo("")
    typer.echo(f"Index change preview ({len(rows)} collection(s) from {directory}):")
    typer.echo("")
    typer.echo(_format_index_table(rows))
    typer.echo("")


def _print_summary(
    directory: Path,
    definitions: list[CollectionIndexes],
    plan: IndexPlan,
    dropped: list[DroppedIndex],
    target: ClusterTarget,
    selected: ConnectionOption,
) -> None:
    typer.echo("")
    typer.echo(f"Cluster:    {target.cluster_name} ({target.cluster_id or 'unknown ID'})")
    typer.echo(f"Project:    {target.project_name} ({target.project_id})")
    typer.echo(f'Connection: "{selected.uri}"')
    typer.echo(f"            [{selected.label}]")
    typer.echo("")

    rows = _collection_summary(definitions, plan, dropped)
    typer.echo(f"Index summary ({len(rows)} collection(s) from {directory}):")
    typer.echo("")
    typer.echo(_format_index_table(rows))
    typer.echo("")
    typer.echo("Skipped = identical or options-conflict indexes.")
    typer.echo("")

    created_count = sum(row[1] for row in rows)
    deleted_count = sum(row[3] for row in rows)
    if created_count:
        typer.echo(f"Index creation complete: {created_count} index(es) created.")
    else:
        typer.echo("Index creation complete: no new indexes.")
    if deleted_count:
        typer.echo(f"Index deletion complete: {deleted_count} index(es) deleted.")


def _collection_summary(
    definitions: list[CollectionIndexes],
    plan: IndexPlan,
    dropped: Sequence[ExtraIndex | DroppedIndex],
) -> list[tuple[str, int, int, int]]:
    counts: dict[tuple[str, str], list[int]] = {}
    order: list[tuple[str, str]] = []

    def bucket(key: tuple[str, str]) -> list[int]:
        if key not in counts:
            counts[key] = [0, 0, 0]
            order.append(key)
        return counts[key]

    for definition in definitions:
        bucket((definition.database, definition.collection))
    for item in plan.items:
        counts_for_item = bucket((item.database, item.collection))
        if item.action is IndexAction.CREATE:
            counts_for_item[0] += 1
        else:
            counts_for_item[1] += 1
    for item in dropped:
        bucket((item.database, item.collection))[2] += 1

    summary: list[tuple[str, int, int, int]] = []
    for database, collection in order:
        created_count, skipped_count, deleted_count = counts[(database, collection)]
        summary.append(
            (
                f"{database}.{collection}",
                created_count,
                skipped_count,
                deleted_count,
            )
        )
    return summary


def _format_index_table(rows: list[tuple[str, int, int, int]]) -> str:
    headers = ("Collection", "Created", "Skipped", "Deleted")
    body = [
        (_markdown_cell(database), created, skipped, deleted)
        for database, created, skipped, deleted in rows
    ]
    total = (
        "**Total**",
        f"**{sum(row[1] for row in rows)}**",
        f"**{sum(row[2] for row in rows)}**",
        f"**{sum(row[3] for row in rows)}**",
    )
    return _format_markdown_table(
        headers,
        body,
        total,
        numeric_columns={1, 2, 3},
        colors={1: typer.colors.GREEN, 3: typer.colors.RED},
    )


def _print_export_summary(
    directory: Path,
    target: ClusterTarget,
    selected: ConnectionOption,
    result: ExportResult,
) -> None:
    typer.echo("")
    typer.echo(f"Cluster:    {target.cluster_name} ({target.cluster_id or 'unknown ID'})")
    typer.echo(f"Project:    {target.project_name} ({target.project_id})")
    typer.echo(f'Connection: "{selected.uri}"')
    typer.echo(f"            [{selected.label}]")
    typer.echo("")

    rows = [(f"{item.database}.{item.collection}", item.indexes) for item in result.collections]
    typer.echo(f"Export summary ({result.files} collection(s) from {directory}):")
    typer.echo("")
    typer.echo(_format_export_table(rows))
    typer.echo("")
    typer.echo(
        f"Exported {result.indexes} index(es) from {result.files} collection(s) to {directory}."
    )


def _format_export_table(rows: list[tuple[str, int]], first_header: str = "Collection") -> str:
    body = [(_markdown_cell(namespace), count) for namespace, count in rows]
    total = ("**Total**", f"**{sum(count for _, count in rows)}**")
    return _format_markdown_table(
        (first_header, "Exported"),
        body,
        total,
        numeric_columns={1},
    )


def _format_markdown_table(
    headers: Sequence[str],
    rows: Sequence[tuple[object, ...]],
    total: Sequence[object],
    *,
    numeric_columns: set[int],
    colors: dict[int, str] | None = None,
) -> str:
    table_rows = [headers, *rows, total]
    widths = [max(len(str(row[index])) for row in table_rows) for index in range(len(headers))]

    def render(cells: Sequence[object], *, colorize: bool = False) -> str:
        rendered: list[str] = []
        for index, value in enumerate(cells):
            text = str(value)
            if index in numeric_columns:
                padding = " " * (widths[index] - len(text))
                if colorize and colors and index in colors:
                    rendered.append(padding + typer.style(text, fg=colors[index]))
                else:
                    rendered.append(padding + text)
            else:
                rendered.append(text.ljust(widths[index]))
        return f"| {' | '.join(rendered)} |"

    separators = [
        f"{'-' * (width - 1)}:" if index in numeric_columns else "-" * width
        for index, width in enumerate(widths)
    ]
    lines = [render(headers), f"| {' | '.join(separators)} |"]
    lines.extend(render(row, colorize=True) for row in rows)
    lines.append(render(total, colorize=True))
    return "\n".join(lines)


def _markdown_cell(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|")


def _log_search_plan(plan: SearchPlan) -> None:
    for item in plan.items:
        if item.action is SearchAction.UPDATE:
            logger.info(
                'Will update search index "%s" on "%s.%s".',
                escape(item.spec.name),
                escape(item.database),
                escape(item.collection),
            )
        else:
            logger.info(
                'Search index "%s" on "%s.%s" is unchanged; skipping.',
                escape(item.spec.name),
                escape(item.database),
                escape(item.collection),
            )


def _log_search_extras(extras: tuple[SearchExtra, ...]) -> None:
    for extra in extras:
        logger.warning(
            'Search index "%s" on "%s.%s" is not present in the definitions.',
            escape(extra.name),
            escape(extra.database),
            escape(extra.collection),
        )


def _search_sync_rows(
    definitions: list[SearchCollectionIndexes],
    plan: SearchPlan,
    deleted_extras: Sequence[SearchExtra],
) -> list[tuple[str, int, int, int, int]]:
    counts: dict[tuple[str, str], list[int]] = {}
    order: list[tuple[str, str]] = []
    for definition in definitions:
        key = (definition.database, definition.collection)
        if key not in counts:
            counts[key] = [0, 0, 0, 0]
            order.append(key)

    for item in plan.items:
        key = (item.database, item.collection)
        if item.action is SearchAction.CREATE:
            counts[key][0] += 1
        elif item.action is SearchAction.UPDATE:
            counts[key][1] += 1
        else:
            counts[key][2] += 1
    for extra in deleted_extras:
        counts[(extra.database, extra.collection)][3] += 1

    rows: list[tuple[str, int, int, int, int]] = []
    for database, collection in order:
        created, updated, unchanged, deleted = counts[(database, collection)]
        rows.append((f"{database}.{collection}", created, updated, unchanged, deleted))
    return rows


def _format_search_sync_table(rows: Sequence[tuple[str, int, int, int, int]]) -> str:
    total = (
        "**Total**",
        f"**{sum(row[1] for row in rows)}**",
        f"**{sum(row[2] for row in rows)}**",
        f"**{sum(row[3] for row in rows)}**",
        f"**{sum(row[4] for row in rows)}**",
    )
    return _format_markdown_table(
        ("Collection", "Created", "Updated", "Skipped", "Deleted"),
        rows,
        total,
        numeric_columns={1, 2, 3, 4},
        colors={
            1: typer.colors.GREEN,
            2: typer.colors.YELLOW,
            4: typer.colors.RED,
        },
    )


def _print_search_preview(
    directory: Path,
    definitions: list[SearchCollectionIndexes],
    plan: SearchPlan,
) -> None:
    rows = _search_sync_rows(definitions, plan, plan.extras)
    typer.echo("")
    typer.echo(f"Search change preview ({len(rows)} collection(s) from {directory}):")
    typer.echo("")
    typer.echo(_format_search_sync_table(rows))
    typer.echo("")


def _print_search_sync_summary(
    directory: Path,
    target: ClusterTarget,
    definitions: list[SearchCollectionIndexes],
    plan: SearchPlan,
    result: SearchApplyResult,
) -> None:
    rows = _search_sync_rows(definitions, plan, result.deleted_extras)
    skipped = sum(row[3] for row in rows)

    typer.echo("")
    typer.echo(f"Cluster:    {target.cluster_name} ({target.cluster_id or 'unknown ID'})")
    typer.echo(f"Project:    {target.project_name} ({target.project_id})")
    typer.echo("")
    typer.echo(f"Search sync summary ({len(rows)} collection(s) from {directory}):")
    typer.echo("")
    typer.echo(_format_search_sync_table(rows))
    typer.echo("")
    typer.echo(
        f"Search sync complete: {result.created} created, {result.updated} updated, "
        f"{skipped} skipped, {result.deleted} deleted."
    )


def _print_search_export_summary(
    directory: Path,
    target: ClusterTarget,
    result: SearchExportResult,
) -> None:
    typer.echo("")
    typer.echo(f"Cluster:    {target.cluster_name} ({target.cluster_id or 'unknown ID'})")
    typer.echo(f"Project:    {target.project_name} ({target.project_id})")
    typer.echo("")

    rows = [(f"{item.database}.{item.collection}", item.indexes) for item in result.collections]
    typer.echo(f"Search export summary ({result.files} collection(s) from {directory}):")
    typer.echo("")
    typer.echo(_format_export_table(rows))
    typer.echo("")
    typer.echo(
        f"Exported {result.indexes} search index(es) from {result.files} "
        f"collection(s) to {directory}."
    )


def _print_trigger_export_summary(
    directory: Path,
    target: ClusterTarget,
    result: TriggerExportResult,
) -> None:
    typer.echo("")
    typer.echo(f"Cluster:    {target.cluster_name} ({target.cluster_id or 'unknown ID'})")
    typer.echo(f"Project:    {target.project_name} ({target.project_id})")
    typer.echo("")

    rows = [(f"{item.database}.{item.collection}", item.triggers) for item in result.collections]
    typer.echo(f"Trigger export summary ({result.files} collection(s) from {directory}):")
    typer.echo("")
    typer.echo(_format_export_table(rows))
    typer.echo("")
    typer.echo(
        f"Exported {result.triggers} trigger(s) from {result.files} "
        f"collection(s) across {result.apps} app(s) to {directory}."
    )


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _fail(message: str) -> NoReturn:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)
