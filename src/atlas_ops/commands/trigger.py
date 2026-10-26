"""Trigger command and its presentation helpers."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

import httpx
import typer
from pyatlassdk import AtlasAdminApiError, AtlasAppApiError

from atlas_ops.atlas import (
    CLUSTER_NAME_ENV,
    PROJECT_ID_ENV,
    ClusterTarget,
    app_services_client_from_env,
    client_from_env,
    describe_atlas_error,
    find_clusters,
    get_project,
)
from atlas_ops.errors import AtlasOperationError, OpsError
from atlas_ops.references import _resolve_references
from atlas_ops.trigger.definitions import TriggerSpec, load_trigger_definitions
from atlas_ops.trigger.export import TriggerExportResult, export_triggers
from atlas_ops.trigger.sync import (
    TriggerAction,
    TriggerPlan,
    TriggerSyncResult,
    apply_trigger_sync,
    find_triggers_app,
    plan_trigger_sync,
)
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


def trigger(
    directory: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        dir_okay=True,
        readable=True,
        resolve_path=True,
        help=(
            "Directory with trigger definition JSON files, or the export "
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
        help="Export App Services triggers to DIRECTORY instead of syncing definitions.",
    ),
) -> None:
    """Synchronize or export App Services triggers for one cluster.

    By default, local trigger files are matched by trigger name in the target
    project's ``Triggers`` app. Missing triggers are created after creating their
    function, existing triggers are updated from the remote template, and remote
    triggers missing locally are deleted together with their orphaned function.
    With ``--export``, definitions are written instead.

    Uses the App Services Admin API client and only needs Atlas API credentials,
    the project ID, and the cluster. No MongoDB connection or temporary database
    user is involved.
    """
    definitions: list[TriggerSpec] | None = None
    target: ClusterTarget | None = None
    export_result: TriggerExportResult | None = None
    sync_plan: TriggerPlan | None = None
    sync_result: TriggerSyncResult | None = None
    try:
        if not export:
            definitions = load_trigger_definitions(directory)
        with client_from_env() as client:
            project_id, cluster_ref = _resolve_references(project, cluster)
            target_project = get_project(client, project_id)
            matches = find_clusters(client, target_project, cluster_ref)
            if not matches:
                raise AtlasOperationError(f"No cluster found for {cluster_ref!r}.")
            target = matches[0] if len(matches) == 1 else _choose_cluster(matches)
            _log_found_cluster(target, cluster_ref)
        with app_services_client_from_env() as app_client:
            if export:
                logger.info("Exporting trigger definitions to %s...", directory)
                export_result = export_triggers(
                    app_client,
                    project_id,
                    target.cluster_name,
                    target.cluster_id,
                    directory,
                )
                logger.info(
                    "Exported %d trigger(s) in %d file(s) across %d app(s).",
                    export_result.triggers,
                    export_result.files,
                    export_result.apps,
                )
            else:
                assert definitions is not None
                triggers_app = find_triggers_app(app_client, project_id)
                sync_plan = plan_trigger_sync(
                    app_client,
                    triggers_app.id,
                    project_id,
                    definitions,
                    target.cluster_name,
                    target.cluster_id,
                )
                _print_trigger_sync_preview(sync_plan)
                has_changes = any(
                    item.action is not TriggerAction.IDENTICAL for item in sync_plan.items
                )
                if not has_changes:
                    typer.echo("No changes to apply.")
                else:
                    _print_change_warning(target, "triggers")
                    if not _confirm_changes():
                        typer.echo("Aborted.")
                        raise typer.Exit(code=1)
                sync_result = apply_trigger_sync(
                    app_client,
                    triggers_app.id,
                    project_id,
                    sync_plan,
                    target.cluster_name,
                )
                logger.info(
                    "Trigger sync complete: %d created, %d updated, %d deleted, "
                    "%d function(s) deleted.",
                    sync_result.created,
                    sync_result.updated,
                    sync_result.deleted,
                    sync_result.functions_deleted,
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
    if export:
        assert export_result is not None
        _print_trigger_export_summary(directory, target, export_result)
    else:
        assert sync_plan is not None
        assert sync_result is not None
        _print_trigger_sync_summary(target, directory, sync_plan, sync_result)


def _trigger_plan_rows(plan: TriggerPlan) -> list[tuple[str, str, str]]:
    order = {
        TriggerAction.CREATE.value: 0,
        TriggerAction.UPDATE.value: 1,
        TriggerAction.DELETE.value: 2,
        TriggerAction.IDENTICAL.value: 3,
    }
    items = sorted(
        plan.items,
        key=lambda item: (order[item.action.value], item.name),
    )
    return [(item.name, item.trigger_type, item.action.value) for item in items]


def _format_trigger_plan_table(rows: Sequence[tuple[str, str, str]]) -> str:
    return _format_markdown_table(
        ("Trigger", "Type", "Action"),
        rows,
        numeric_columns=set(),
        text_colors={
            (2, "create"): typer.colors.GREEN,
            (2, "update"): typer.colors.YELLOW,
            (2, "delete"): typer.colors.RED,
        },
    )


def _print_trigger_sync_preview(plan: TriggerPlan) -> None:
    rows = _trigger_plan_rows(plan)
    typer.echo("")
    typer.echo(f"Trigger change preview ({len(rows)} trigger(s)):")
    typer.echo("")
    typer.echo(_format_trigger_plan_table(rows))
    typer.echo("")


def _print_trigger_sync_summary(
    target: ClusterTarget,
    directory: Path,
    plan: TriggerPlan,
    result: TriggerSyncResult,
) -> None:
    rows = _trigger_plan_rows(plan)
    typer.echo("")
    typer.echo(f"Cluster:    {target.cluster_name} ({target.cluster_id or 'unknown ID'})")
    typer.echo(f"Project:    {target.project_name} ({target.project_id})")
    typer.echo("")
    typer.echo(f"Trigger sync summary ({len(rows)} trigger(s) from {directory}):")
    typer.echo("")
    typer.echo(_format_trigger_plan_table(rows))
    typer.echo("")
    typer.echo(
        f"Trigger sync complete: {result.created} created, {result.updated} updated, "
        f"{result.skipped} skipped, {result.deleted} deleted; "
        f"{result.functions_deleted} orphan function(s) deleted."
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
    if result.scheduled:
        rows.append(("scheduled", result.scheduled))
    typer.echo(f"Trigger export summary ({result.files} file(s) from {directory}):")
    typer.echo("")
    typer.echo(_format_export_table(rows, first_header="Scope", second_header="Triggers"))
    typer.echo("")
    typer.echo(
        f"Exported {result.triggers} trigger(s) in {result.files} file(s) "
        f"across {result.apps} app(s) to {directory}."
    )
    if result.removed:
        typer.echo(f"Removed {result.removed} stale file(s) from {directory}.")
