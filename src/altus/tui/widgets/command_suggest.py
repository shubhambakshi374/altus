"""Live suggestions for slash commands.

Typing ``/`` should tell you what exists rather than requiring you to already
know. The list filters as you type, and once you are past the command name it
switches to a usage hint for the command you are actually writing.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Label, OptionList
from textual.widgets.option_list import Option

from altus.tui.commands.registry import Command, CommandRegistry

#: A bare `/` is meant to show everything --- the popup exists so you do
#: not have to know the names already. Keep this at or above the number of
#: registered commands; tests/test_tui.py asserts the two stay in step.
MAX_SHOWN = 24
MAX_SIGNATURE = 34
MIN_MATCH = 2
"""Matching anywhere but the start on one character is noise. It ruled out
summaries first --- `/k` would offer `model`, whose summary contains "Pick"
--- and names turn out to need it too: `/k` offered `workflow`, because
"workflow" contains a k. One character matches by prefix only."""


@dataclass(frozen=True)
class Suggestion:
    """What the composer would insert, and what to show for it."""

    command: Command
    completion: str

    @property
    def signature(self) -> str:
        return f"/{self.command.usage or self.command.name}"

    def label(self, width: int) -> str:
        """Aligned against the widest entry currently shown --- a fixed column
        collides with the longer usages like `/key <provider> | rm <provider>`."""
        return f"{self.signature:<{width}}  {self.command.summary}"


def match(registry: CommandRegistry, text: str) -> list[Suggestion]:
    """Commands worth offering for the partially-typed ``text``.

    Empty once a space is typed: past the command name the user is writing
    arguments, and a list of command names is no longer the useful thing.
    """
    if not text.startswith("/"):
        return []
    typed = text[1:]
    if " " in typed or "\n" in text:
        return []

    needle = typed.casefold()
    starts = [c for c in registry.unique if c.name.startswith(needle)]
    contains = [
        c
        for c in registry.unique
        if c not in starts
        and len(needle) >= MIN_MATCH
        and (needle in c.name or needle in c.summary.casefold())
    ]
    return [Suggestion(c, f"/{c.name}") for c in (*starts, *contains)][:MAX_SHOWN]


def usage_hint(registry: CommandRegistry, text: str) -> str:
    """The one-line usage for a command whose arguments are being typed."""
    if not text.startswith("/") or " " not in text:
        return ""
    name = text[1:].split(maxsplit=1)[0].casefold()
    command = registry.get(name)
    return f"/{command.usage or command.name}   —   {command.summary}" if command else ""


class CommandSuggestions(Vertical):
    """A popup above the composer. Hidden unless there is something to say."""

    DEFAULT_CSS = """
    CommandSuggestions {
        height: auto;
        max-height: 12;
        margin: 0 1;
        background: $surface;
        border: round $accent;
        display: none;
    }
    CommandSuggestions.-visible { display: block; }
    CommandSuggestions OptionList {
        height: auto;
        max-height: 9;
        background: transparent;
        border: none;
        padding: 0 1;
    }
    CommandSuggestions > .hint {
        color: $text-muted;
        padding: 0 2;
    }
    """

    def __init__(self) -> None:
        super().__init__(id="command-suggestions")
        self.suggestions: list[Suggestion] = []

    def compose(self) -> ComposeResult:
        yield OptionList(id="suggestion-list")
        yield Label("", classes="hint", id="suggestion-hint")

    @property
    def visible_now(self) -> bool:
        return self.has_class("-visible")

    def update_for(self, registry: CommandRegistry, text: str) -> None:
        self.suggestions = match(registry, text)
        hint = usage_hint(registry, text)
        options = self.query_one("#suggestion-list", OptionList)
        label = self.query_one("#suggestion-hint", Label)

        options.clear_options()
        if self.suggestions:
            width = min(max(len(s.signature) for s in self.suggestions), MAX_SIGNATURE)
            options.add_options(
                [Option(s.label(width), id=str(i)) for i, s in enumerate(self.suggestions)]
            )
            options.highlighted = 0
            options.display = True
            label.update("↑↓ choose · tab or enter complete · esc dismiss")
        else:
            options.display = False
            label.update(hint)
        self.set_class(bool(self.suggestions or hint), "-visible")

    def hide(self) -> None:
        self.remove_class("-visible")
        self.suggestions = []

    def move(self, delta: int) -> None:
        if not self.suggestions:
            return
        options = self.query_one("#suggestion-list", OptionList)
        current = options.highlighted or 0
        options.highlighted = (current + delta) % len(self.suggestions)

    @property
    def highlighted(self) -> Suggestion | None:
        if not self.suggestions:
            return None
        index = self.query_one("#suggestion-list", OptionList).highlighted or 0
        return self.suggestions[min(index, len(self.suggestions) - 1)]
