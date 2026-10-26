from __future__ import annotations

from atlas_ops.utils import green, red, style_color, yellow


def test_named_color_helpers() -> None:
    assert yellow("value") == "\x1b[33mvalue\x1b[0m"
    assert green("value") == "\x1b[32mvalue\x1b[0m"
    assert red("value") == "\x1b[31mvalue\x1b[0m"


def test_style_color_accepts_arbitrary_color() -> None:
    assert style_color("value", "cyan") == "\x1b[36mvalue\x1b[0m"
