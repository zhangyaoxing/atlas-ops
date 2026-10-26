"""Terminal text styling helpers."""

from __future__ import annotations

from typer import colors, style


def yellow(value: str) -> str:
    """Style *value* in yellow."""
    return style(value, fg=colors.YELLOW)


def green(value: str) -> str:
    """Style *value* in green."""
    return style(value, fg=colors.GREEN)


def red(value: str) -> str:
    """Style *value* in red."""
    return style(value, fg=colors.RED)


def style_color(value: str, color: str) -> str:
    """Style *value* with an arbitrary Typer color."""
    return style(value, fg=color)
