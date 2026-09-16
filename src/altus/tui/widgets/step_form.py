"""Adding and editing one step.

Three modals rather than one form that reshapes itself: the kind is chosen
first, and the form is then built for that kind alone. A single form toggling
fields in and out is where this sort of screen usually starts leaking --- a
hidden field keeps its old value, and an approval step quietly saves the prompt
someone typed while it was an agent step.

Everything shown here is text somebody else wrote --- step ids, prompts, tool
arguments --- so every widget that would parse it as Textual markup is either
told not to or handed a ``Content``.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList
from textual.widgets.option_list import Option

from altus.tools.base import dispatches, sensitivity_of
from altus.workflow.models import AgentStep, AnyStep, ApprovalStep, ToolStep, valid_slug

KINDS: tuple[tuple[str, str], ...] = (
    ("tool", "call one registered tool with arguments"),
    ("agent", "give the model a prompt, and optionally a narrowed tool set"),
    ("approval", "stop, and let a human decide whether the rest runs"),
)

#: ``SELF`` stands in for the screen's own class name. Every rule needs the
#: prefix, not just the first --- concatenating a class name onto a block that
#: opens with a bare ``{`` parses one rule and then fails on the next, which is
#: a TokenError at compose time rather than anything visible in review.
MODAL_CSS = """
    SELF { align: center middle; }
    SELF > Vertical {
        width: 82; max-width: 94%; height: auto; max-height: 86%;
        border: round $accent; background: $surface; padding: 1 2;
    }
    SELF .title { text-style: bold; padding-bottom: 1; }
    SELF .hint { color: $text-muted; padding-top: 1; }
    SELF .field { color: $text-muted; padding-top: 1; }
    SELF .problem { color: $error; padding-top: 1; }
    SELF OptionList { height: auto; max-height: 14; }
"""


def modal_css(name: str) -> str:
    return MODAL_CSS.replace("SELF", name)


class KindPicker(ModalScreen[str | None]):
    """Which of the three kinds of step this is."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "close", "Cancel")]
    DEFAULT_CSS = modal_css("KindPicker")

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Add a step", classes="title")
            yield OptionList(
                *(Option(Content(f"{kind:<10} {what}"), id=kind) for kind, what in KINDS),
                id="kinds",
            )
            yield Label("enter to choose · esc to cancel", classes="hint")

    def on_mount(self) -> None:
        self.query_one("#kinds", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id or "") or None)

    def action_close(self) -> None:
        self.dismiss(None)


class ToolPicker(ModalScreen[str | None]):
    """Filter-as-you-type over the registered tools, with what each one costs.

    The answer to "help me define it": a session registers more tools than
    anyone holds in their head, and a designer that makes you type the name
    from memory is a text editor with extra steps.
    """

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "close", "Cancel")]
    DEFAULT_CSS = modal_css("ToolPicker")

    def __init__(self, registry: Any, current: str = "") -> None:
        super().__init__()
        self.registry = registry
        self.current = current
        self.rows: list[tuple[str, str]] = []
        for tool in sorted(registry, key=lambda t: t.name):
            level = sensitivity_of(tool)
            note = f"{level.value}{' — arguments decide' if dispatches(tool) else ''}"
            self.rows.append((tool.name, note))

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Which tool?", classes="title")
            yield Input(placeholder="filter…", id="filter")
            yield OptionList(*self._options(""), id="tools")
            yield Label("enter to choose · esc to cancel", classes="hint")

    def _options(self, needle: str) -> list[Option]:
        needle = needle.casefold()
        return [
            Option(Content(f"{name:<22} {note}"), id=name)
            for name, note in self.rows
            if needle in name.casefold()
        ]

    def on_mount(self) -> None:
        self.query_one("#filter", Input).focus()

    async def on_input_changed(self, event: Input.Changed) -> None:
        options = self.query_one("#tools", OptionList)
        options.clear_options()
        options.add_options(self._options(event.value))

    def on_input_submitted(self) -> None:
        options = self.query_one("#tools", OptionList)
        if options.option_count:
            options.focus()
            options.highlighted = 0

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id or "") or None)

    def action_close(self) -> None:
        self.dismiss(None)


class StepForm(ModalScreen["AnyStep | None"]):
    """The fields for one step of a known kind."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Cancel"),
        Binding("ctrl+s", "submit", "Save step"),
        Binding("f2", "pick_tool", "Pick tool"),
    ]
    DEFAULT_CSS = modal_css("StepForm")

    def __init__(self, kind: str, registry: Any, step: AnyStep | None = None) -> None:
        super().__init__()
        self.kind = kind
        self.registry = registry
        self.step = step

    def compose(self) -> ComposeResult:
        step = self.step
        with Vertical():
            yield Label(Content(f"{'Edit' if step else 'New'} {self.kind} step"), classes="title")
            yield Label("id", classes="field")
            yield Input(value=step.id if step else "", placeholder="deploy", id="id")
            yield Label("needs (comma separated, may be empty)", classes="field")
            yield Input(value=", ".join(step.needs) if step else "", id="needs")

            if self.kind == "tool":
                yield Label("tool  —  f2 to pick from the registry", classes="field")
                yield Input(value=getattr(step, "tool", ""), id="tool")
                yield Label("arguments (JSON object)", classes="field")
                yield Input(value=_args_text(step), id="args")
            elif self.kind == "agent":
                yield Label("prompt", classes="field")
                yield Input(value=getattr(step, "prompt", ""), id="prompt")
                yield Label(
                    "tools it may use (comma separated; empty means all of them)",
                    classes="field",
                )
                yield Input(value=", ".join(getattr(step, "tools", []) or []), id="tools")
            else:
                yield Label("message shown to whoever approves", classes="field")
                yield Input(value=getattr(step, "message", ""), id="message")

            yield Label("", classes="problem", id="problem", markup=False)
            yield Label("ctrl+s to save this step · esc to cancel", classes="hint")

    def on_mount(self) -> None:
        self.query_one("#id", Input).focus()

    def action_pick_tool(self) -> None:
        if self.kind != "tool":
            return
        self.app.push_screen(ToolPicker(self.registry), self._tool_chosen)

    def _tool_chosen(self, chosen: str | None) -> None:
        if chosen:
            self.query_one("#tool", Input).value = chosen

    def _value(self, field: str) -> str:
        return self.query_one(f"#{field}", Input).value.strip()

    def _fail(self, message: str) -> None:
        self.query_one("#problem", Label).update(Content(message))

    def on_input_submitted(self) -> None:
        self.action_submit()

    def action_submit(self) -> None:
        step_id = self._value("id")
        if not valid_slug(step_id):
            self._fail(
                "an id must be lowercase letters, digits, '-' or '_', "
                "starting with a letter or digit"
            )
            return
        needs = [part.strip() for part in self._value("needs").split(",") if part.strip()]

        try:
            built = self._build(step_id, needs)
        except ValueError as exc:
            self._fail(str(exc))
            return
        self.dismiss(built)

    def _build(self, step_id: str, needs: list[str]) -> AnyStep:
        if self.kind == "tool":
            name = self._value("tool")
            if not name:
                raise ValueError("a tool step needs a tool — press f2 to pick one")
            return ToolStep(
                id=step_id, needs=needs, tool=name, args=_parse_args(self._value("args"))
            )
        if self.kind == "agent":
            prompt = self._value("prompt")
            if not prompt:
                raise ValueError("an agent step needs a prompt")
            tools = [part.strip() for part in self._value("tools").split(",") if part.strip()]
            return AgentStep(id=step_id, needs=needs, prompt=prompt, tools=tools)
        return ApprovalStep(id=step_id, needs=needs, message=self._value("message"))

    def action_close(self) -> None:
        self.dismiss(None)


def _args_text(step: AnyStep | None) -> str:
    args = getattr(step, "args", None)
    return json.dumps(args) if args else ""


def _parse_args(text: str) -> dict[str, Any]:
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"arguments must be a JSON object: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("arguments must be a JSON object, not a list or a bare value")
    return parsed
