"""Startup wordmark. One instant paint, then the caller continues."""

from __future__ import annotations

import shutil
import sys

_TAGLINE = "Find the next growing apps."
_GLYPH_WIDTH = 7
_GLYPHS: dict[str, tuple[str, ...]] = {
    "A": (
        "  ███  ",
        " █   █ ",
        "███████",
        "█     █",
        "█     █",
    ),
    "P": (
        "██████ ",
        "█     █",
        "██████ ",
        "█      ",
        "█      ",
    ),
    "S": (
        "███████",
        "█      ",
        "███████",
        "      █",
        "███████",
    ),
    "C": (
        "███████",
        "█      ",
        "█      ",
        "█      ",
        "███████",
    ),
    "O": (
        "███████",
        "█     █",
        "█     █",
        "█     █",
        "███████",
    ),
    "U": (
        "█     █",
        "█     █",
        "█     █",
        "█     █",
        "███████",
    ),
    "T": (
        "███████",
        "   █   ",
        "   █   ",
        "   █   ",
        "   █   ",
    ),
}
_ROW_STYLES = ("cyan", "cyan", "gold", "magenta", "magenta")
_RICH = {
    "cyan": "bold bright_cyan",
    "magenta": "bold bright_magenta",
    "gold": "bold #FFD76A",
    "rule": "#C9A227",
    "plain": "",
}
_ANSI = {
    "cyan": "\033[1;96m",
    "magenta": "\033[1;95m",
    "gold": "\033[1;38;5;220m",
    "rule": "\033[38;5;178m",
    "plain": "",
}
_shown = False


def _glyphs_fit() -> None:
    for letter, rows in _GLYPHS.items():
        if len(rows) != len(_ROW_STYLES) or any(len(row) != _GLYPH_WIDTH for row in rows):
            raise RuntimeError(f"banner glyph {letter} is not {_GLYPH_WIDTH} columns")


_glyphs_fit()


def show_banner() -> None:
    """Paint the home wordmark. Skips pipes, tests, and a second call in this process."""
    global _shown
    if _shown or not _should_show():
        return
    _shown = True
    lines = _fitted_lines()
    if not lines:
        return
    try:
        painted = _print_rich(lines)
    except Exception:
        painted = False
    if not painted:
        _print_ansi(lines)


def _should_show() -> bool:
    if not sys.stdout.isatty():
        return False
    command = " ".join(sys.argv)
    return "unittest" not in command


def _wordmark() -> list[list[tuple[str, str]]]:
    rows: list[list[tuple[str, str]]] = [[] for _ in _ROW_STYLES]
    for index, letter in enumerate("APPSCOUT"):
        if index:
            for row in rows:
                row.append((" ", "plain"))
        for row_index, part in enumerate(_GLYPHS[letter]):
            rows[row_index].append((part, _ROW_STYLES[row_index]))
    return rows


def _plain_width(segments: list[tuple[str, str]]) -> int:
    return sum(len(text) for text, _style in segments)


def _center(segments: list[tuple[str, str]], width: int) -> list[tuple[str, str]]:
    gap = max(0, (width - _plain_width(segments)) // 2)
    if not gap:
        return segments
    return [(" " * gap, "plain"), *segments]


def _fitted_lines() -> list[list[tuple[str, str]]]:
    """Center inside 80 columns. A narrow terminal gets a one-line wordmark instead."""
    columns = shutil.get_terminal_size(fallback=(80, 24)).columns
    frame = min(columns, 80)
    wordmark = _wordmark()
    art_width = _plain_width(wordmark[0])
    if frame < art_width:
        if frame < len(_TAGLINE):
            return []
        rule = "─" * len(_TAGLINE)
        return [
            [("APPSCOUT", "cyan")],
            [(_TAGLINE, "gold")],
            [(rule, "rule")],
            [("", "plain")],
        ]
    # Center inside an 80-column frame. Left pad only, so the line stays under 80.
    pad_width = min(frame, 80)
    rule = "─" * art_width
    lines = [_center([(rule, "rule")], pad_width)]
    lines.extend(_center(row, pad_width) for row in wordmark)
    lines.append(_center([(_TAGLINE, "gold")], pad_width))
    lines.append(_center([(rule, "rule")], pad_width))
    lines.append([("", "plain")])
    return lines


def _print_rich(lines: list[list[tuple[str, str]]]) -> bool:
    try:
        from rich.console import Console
        from rich.text import Text
    except ImportError:
        return False
    width = max(shutil.get_terminal_size(fallback=(80, 24)).columns, 80)
    console = Console(highlight=False, force_terminal=True, width=width, soft_wrap=False)
    for segments in lines:
        text = Text()
        for value, style in segments:
            text.append(value, style=_RICH.get(style, ""))
        console.print(text, overflow="crop", no_wrap=True)
    return True


def _print_ansi(lines: list[list[tuple[str, str]]]) -> None:
    reset = "\033[0m"
    for segments in lines:
        parts = []
        for value, style in segments:
            code = _ANSI.get(style, "")
            parts.append(f"{code}{value}{reset}" if code else value)
        sys.stdout.write("".join(parts) + "\n")
    sys.stdout.flush()
