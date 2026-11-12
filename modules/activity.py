"""Terminal narration for the discovery loop. Tests and pipes stay on plain lines."""

from __future__ import annotations

import sys
from contextlib import nullcontext


def rich_enabled() -> bool:
    if not sys.stdout.isatty():
        return False
    command = " ".join(sys.argv)
    return "unittest" not in command


class Narrator:
    def __init__(self) -> None:
        self.console = None
        if not rich_enabled():
            return
        try:
            from rich.console import Console
        except ImportError:
            return
        self.console = Console(highlight=False)

    def line(self, message: str) -> None:
        if self.console is not None:
            self.console.print(message)
            return
        print(message)

    def spin(self, message: str):
        if self.console is None:
            self.line(message)
            return nullcontext()
        return self.console.status(message, spinner="dots")
