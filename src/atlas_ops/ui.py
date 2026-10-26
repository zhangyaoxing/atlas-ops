"""Shared CLI prompts, warnings, and Markdown tables."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import NoReturn

import typer
from rich.markup import escape

from atlas_ops.atlas import ClusterTarget, ConnectionOption, is_object_id
from atlas_ops.utils import green, red, style_color, yellow

logger = logging.getLogger(__name__)


def _prompt_project() -> str:
    while True:
        value = typer.prompt("Project ID (24 hexadecimal characters)").strip()
        if is_object_id(value):
            return value
        typer.echo(
            yellow("Project ID must be 24 hexadecimal characters."),
            err=True,
        )


def _prompt_cluster() -> str:
    while True:
        value = typer.prompt("Cluster ID or name").strip()
        if value:
            return value
        typer.echo(yellow("Cluster ID or name must not be empty."), err=True)


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
        typer.echo(yellow(f"Enter a number between 1 and {count}."), err=True)


def _change_warning(target: ClusterTarget, resource: str) -> str:
    org = target.org_name or target.org_id or "unknown organization"
    return (
        yellow(f"Heads up! {resource} in ")
        + green(org)
        + yellow(" - ")
        + green(target.project_name)
        + yellow(" - ")
        + green(target.cluster_name)
        + yellow(" will be changed!")
    )


def _print_change_warning(target: ClusterTarget, resource: str) -> None:
    typer.echo(_change_warning(target, resource))
    typer.echo("")


def _confirm_changes() -> bool:
    return typer.confirm("Proceed with these changes?", default=False)


def _fail(message: str) -> NoReturn:
    typer.echo(red(message), err=True)
    raise typer.Exit(code=1)


def _format_markdown_table(
    headers: Sequence[str],
    rows: Sequence[tuple[object, ...]],
    total: Sequence[object] | None = None,
    *,
    numeric_columns: set[int],
    colors: dict[int, str] | None = None,
    text_colors: dict[tuple[int, str], str] | None = None,
) -> str:
    table_rows = [headers, *rows, *([total] if total is not None else [])]
    widths = [max(len(str(row[index])) for row in table_rows) for index in range(len(headers))]

    def render(cells: Sequence[object], *, colorize: bool = False) -> str:
        rendered: list[str] = []
        for index, value in enumerate(cells):
            text = str(value)
            color = None
            if colorize:
                if index in numeric_columns:
                    nonzero = text.replace("*", "").strip() != "0"
                    if colors and index in colors and nonzero:
                        color = colors[index]
                elif text_colors and (index, text) in text_colors:
                    color = text_colors[(index, text)]
            if index in numeric_columns:
                padding = " " * (widths[index] - len(text))
                content = style_color(text, color) if color else text
                rendered.append(padding + content)
            else:
                padding = " " * (widths[index] - len(text))
                content = style_color(text, color) if color else text
                rendered.append(content + padding)
        return f"| {' | '.join(rendered)} |"

    separators = [
        f"{'-' * (width - 1)}:" if index in numeric_columns else "-" * width
        for index, width in enumerate(widths)
    ]
    lines = [render(headers), f"| {' | '.join(separators)} |"]
    lines.extend(render(row, colorize=True) for row in rows)
    if total is not None:
        lines.append(render(total, colorize=True))
    return "\n".join(lines)


def _markdown_cell(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|")


def _format_export_table(
    rows: list[tuple[str, int]],
    first_header: str = "Collection",
    second_header: str = "Exported",
) -> str:
    body = [(_markdown_cell(namespace), count) for namespace, count in rows]
    total = ("**Total**", f"**{sum(count for _, count in rows)}**")
    return _format_markdown_table(
        (first_header, second_header),
        body,
        total,
        numeric_columns={1},
    )
