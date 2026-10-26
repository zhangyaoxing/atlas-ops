"""Search index command and its presentation helpers."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

import httpx
import typer
from pyatlassdk import AtlasAdminApiError
from rich.markup import escape

from atlas_ops.atlas import (
    CLUSTER_NAME_ENV,
    PROJECT_ID_ENV,
    ClusterTarget,
    client_from_env,
    describe_atlas_error,
    find_clusters,
    get_project,
)
from atlas_ops.errors import AtlasOperationError, OpsError
from atlas_ops.references import _resolve_references
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
from atlas_ops.ui import (
    _choose_cluster,
    _confirm_changes,
    _fail,
    _format_export_table,
    _format_markdown_table,
    _log_found_cluster,
    _print_change_warning,
)

logger = logging.getLogger(__name__)


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
                else:
                    _print_change_warning(target, "search indexes")
                    if not _confirm_changes():
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
    rows.sort(
        key=lambda row: (
            0 if row[1] or row[2] or row[4] else 1,
            -(row[1] + row[2] + row[4]),
            row[0],
        )
    )
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
