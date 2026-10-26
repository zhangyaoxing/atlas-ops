"""Resolve project and cluster references from options or the environment."""

from __future__ import annotations

import logging
import os

from rich.markup import escape

from atlas_ops.atlas import CLUSTER_NAME_ENV, PROJECT_ID_ENV, is_object_id
from atlas_ops.errors import AtlasOperationError
from atlas_ops.ui import _prompt_cluster, _prompt_project

logger = logging.getLogger(__name__)


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
