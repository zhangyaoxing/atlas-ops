"""Index command and its presentation helpers."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

import httpx
import typer
from pyatlassdk import AtlasAdminApiError
from pymongo.errors import PyMongoError
from rich.markup import escape

from atlas_ops.atlas import (
    CLUSTER_NAME_ENV,
    CONNECTION_TYPE_ENV,
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
)
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
from atlas_ops.references import _resolve_references
from atlas_ops.ui import (
    _choose_cluster,
    _choose_connection,
    _choose_connection_by_type,
    _confirm_changes,
    _fail,
    _format_export_table,
    _format_markdown_table,
    _log_connection_strings,
    _log_found_cluster,
    _markdown_cell,
    _print_change_warning,
)

logger = logging.getLogger(__name__)


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
    connection_type: str | None = typer.Option(
        None,
        "--connection-type",
        "-t",
        help=(
            "Connection type such as standard_srv or private_endpoint. "
            f"Falls back to {CONNECTION_TYPE_ENV}; skips the prompt."
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
            requested_type = (connection_type or os.environ.get(CONNECTION_TYPE_ENV, "")).strip()
            if requested_type:
                selected = _choose_connection_by_type(options, requested_type)
            else:
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
                        plan = plan_indexes(
                            mongo_client,
                            definitions,
                            missing,
                            include_unmanaged=True,
                        )
                        _log_index_plan(plan.items)
                        _print_index_preview(directory, definitions, plan)
                        has_changes = bool(plan.extras) or any(
                            item.action is IndexAction.CREATE for item in plan.items
                        )
                        if not has_changes:
                            typer.echo("No changes to apply.")
                        else:
                            _print_change_warning(target, "indexes")
                            if not _confirm_changes():
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
    summary.sort(
        key=lambda row: (
            0 if row[1] or row[3] else 1,
            -(row[1] + row[3]),
            row[0],
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
    if result.removed:
        typer.echo(f"Removed {result.removed} stale file(s) from {directory}.")


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
