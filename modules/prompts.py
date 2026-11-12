"""Question prompts. Tests use ScriptedPrompter instead of the terminal."""

from __future__ import annotations

from typing import Protocol


BACK = "__back__"


class Prompter(Protocol):
    def select(self, message: str, choices: list, default=None): ...

    def checkbox(self, message: str, choices: list, default: list | None = None) -> list: ...

    def text(self, message: str, default: str = "") -> str: ...

    def confirm(self, message: str, default: bool = True) -> bool: ...


class Cancelled(Exception):
    pass


def _pairs(choices: list) -> list[tuple[str, object]]:
    pairs = []
    for item in choices:
        if isinstance(item, tuple) and len(item) == 2:
            pairs.append((str(item[0]), item[1]))
        else:
            pairs.append((str(item), item))
    return pairs


def _style():
    import questionary

    return questionary.Style(
        [
            ("qmark", "fg:cyan bold"),
            ("question", "bold"),
            ("answer", "fg:cyan bold"),
            ("pointer", "fg:cyan bold"),
            ("highlighted", "fg:cyan bold"),
            ("selected", "fg:magenta"),
            ("instruction", "fg:#858585"),
            ("text", ""),
            ("disabled", "fg:#858585 italic"),
        ]
    )


class QuestionaryPrompter:
    def select(self, message: str, choices: list, default=None):
        import questionary

        pairs = _pairs(choices)
        built = [questionary.Choice(label, value=value) for label, value in pairs]
        values = [value for _label, value in pairs]
        kwargs = {}
        if default in values:
            kwargs["default"] = default
        try:
            answer = questionary.select(message, choices=built, style=_style(), qmark="❯", **kwargs).ask()
        except KeyboardInterrupt:
            return None
        return answer

    def checkbox(self, message: str, choices: list, default: list | None = None) -> list | None:
        import questionary

        selected = list(default or [])
        pairs = _pairs(choices)
        built = [
            questionary.Choice(label, value=value, checked=value in selected) for label, value in pairs
        ]
        try:
            answer = questionary.checkbox(
                message,
                choices=built,
                style=_style(),
                qmark="❯",
                instruction="Space toggles, Enter confirms",
            ).ask()
        except KeyboardInterrupt:
            return None
        return answer

    def text(self, message: str, default: str = "") -> str | None:
        import questionary

        try:
            answer = questionary.text(message, default=default or "", style=_style(), qmark="❯").ask()
        except KeyboardInterrupt:
            return None
        return answer

    def confirm(self, message: str, default: bool = True) -> bool | None:
        import questionary

        try:
            return questionary.confirm(message, default=default, style=_style(), qmark="❯").ask()
        except KeyboardInterrupt:
            return None


class ScriptedPrompter:
    """Return queued answers. Each call consumes one answer."""

    def __init__(self, answers: list):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def _next(self, message: str):
        self.prompts.append(message)
        if not self.answers:
            raise AssertionError(f"No scripted answer left for: {message}")
        return self.answers.pop(0)

    def select(self, message: str, choices: list, default=None):
        return self._next(message)

    def checkbox(self, message: str, choices: list, default: list | None = None):
        return self._next(message)

    def text(self, message: str, default: str = ""):
        return self._next(message)

    def confirm(self, message: str, default: bool = True):
        return self._next(message)
