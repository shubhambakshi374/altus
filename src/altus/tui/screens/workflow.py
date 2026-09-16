"""The workflow designer.

A workflow is a queued set of actions against real infrastructure, so the two
things this screen must never get wrong are what a step will do and how much it
could break. Both come from ``altus.workflow`` rather than being worked out
here: the same ``blast_radius`` the ``/workflow`` text commands print, and the
same ``check`` that will gate the engine when there is one.

Nothing on this screen runs a step. It edits a file.
"""

from __future__ import annotations

from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.content import Content
from textual.screen import ModalScreen, Screen
from textual.widgets import Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from altus.tui.widgets.step_form import KindPicker, StepForm
from altus.workflow import blast_radius, check, describe, list_workflows, save, step_level
from altus.workflow.models import AnyStep, Workflow, valid_slug

#: See ``step_form.modal_css`` --- every rule carries the prefix, not just the
#: first one.
PICKER_CSS = """
    SELF { align: center middle; }
    SELF > Vertical {
        width: 76; max-width: 92%; height: auto; max-height: 80%;
        border: round $accent; background: $surface; padding: 1 2;
    }
    SELF .title { text-style: bold; padding-bottom: 1; }
    SELF .hint { color: $text-muted; padding-top: 1; }
    SELF .problem { color: $error; padding-top: 1; }
    SELF OptionList { height: auto; max-height: 16; }
"""


def picker_css(name: str) -> str:
    return PICKER_CSS.replace("SELF", name)


NEW = "\x00new"
"""Sentinel id for the "new workflow" row. Not a legal slug, so it can never
collide with a real workflow's name."""


class WorkflowPicker(ModalScreen[str | None]):
    """Which workflow to open, or a new one."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "close", "Cancel")]
    DEFAULT_CSS = picker_css("WorkflowPicker")

    def __init__(self, names: list[str]) -> None:
        super().__init__()
        self.names = names

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Workflows", classes="title")
            options = [Option(Content(name), id=name) for name in self.names]
            options.append(Option(Content("+ new workflow"), id=NEW))
            yield OptionList(*options, id="workflows")
            yield Label("enter to open · esc to cancel", classes="hint")

    def on_mount(self) -> None:
        self.query_one("#workflows", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id or "") or None)

    def action_close(self) -> None:
        self.dismiss(None)


class NamePrompt(ModalScreen[str | None]):
    """A name for a new workflow. Also the filename, hence the slug rule."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "close", "Cancel")]
    DEFAULT_CSS = picker_css("NamePrompt")

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Name this workflow", classes="title")
            yield Input(placeholder="deploy-api", id="name")
            yield Label("", classes="problem", id="problem", markup=False)
            yield Label(
                "lowercase letters, digits, '-' and '_' — it is the filename too",
                classes="hint",
            )

    def on_mount(self) -> None:
        self.query_one("#name", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        name = event.value.strip()
        if not valid_slug(name):
            self.query_one("#problem", Label).update(
                Content("lowercase letters, digits, '-' and '_' only")
            )
            return
        self.dismiss(name)

    def action_close(self) -> None:
        self.dismiss(None)


class WorkflowScreen(Screen[None]):
    """One workflow: its steps, what each costs, and what is wrong with it."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("a", "add", "Add step"),
        Binding("e", "edit", "Edit"),
        Binding("d", "delete", "Delete"),
        Binding("K", "move_up", "Move up"),
        Binding("J", "move_down", "Move down"),
        Binding("s", "save", "Save"),
        Binding("escape", "close", "Back"),
        Binding("q", "close", "Back"),
    ]

    DEFAULT_CSS = """
    WorkflowScreen { background: $surface; }
    WorkflowScreen > Vertical { height: 1fr; padding: 1 2; }
    WorkflowScreen .title { text-style: bold; }
    WorkflowScreen .hint { color: $text-muted; }
    WorkflowScreen .note { color: $warning; }
    WorkflowScreen .problem { color: $error; }
    WorkflowScreen OptionList { height: 1fr; min-height: 6; }
    """

    def __init__(self, workflow: Workflow, *, settings: Any = None) -> None:
        super().__init__()
        self.workflow = workflow
        self.settings = settings
        self.dirty = False
        self.leaving = False
        """Set by a first escape with unsaved changes, cleared by any edit."""

    # ------------------------------------------------------------- rendering

    @property
    def registry(self) -> Any:
        return getattr(self.app, "registry", None) or _EmptyRegistry()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(Content(self._heading()), classes="title", id="heading")
            yield OptionList(id="steps")
            yield Static("", classes="hint", id="radius", markup=False)
            yield Static("", classes="problem", id="problems", markup=False)
            yield Label(
                "a add · e edit · d delete · J/K move · s save · esc back",
                classes="hint",
            )

    def on_mount(self) -> None:
        self.refresh_view()

    def _heading(self) -> str:
        count = len(self.workflow.steps)
        steps = "1 step" if count == 1 else f"{count} steps"
        return f"{self.workflow.name} · {steps}{' · unsaved' if self.dirty else ''}"

    def _row(self, index: int, step: AnyStep) -> Option:
        level, caveat = step_level(step, self.registry)
        shown = "?" if caveat == "unknown" else level.value
        if caveat in {"dispatch", "open"}:
            shown = f"{shown}+"
        return Option(
            Content(f"{index + 1:>2}  {step.id:<16} {step.kind:<9} {describe(step):<42} {shown}"),
            id=f"step-{index}",
        )

    def refresh_view(self) -> None:
        options = self.query_one("#steps", OptionList)
        keep = options.highlighted
        options.clear_options()
        options.add_options([self._row(i, s) for i, s in enumerate(self.workflow.steps)])
        if self.workflow.steps:
            options.highlighted = min(keep or 0, len(self.workflow.steps) - 1)

        self.query_one("#heading", Label).update(Content(self._heading()))

        radius = blast_radius(self.workflow, self.registry)
        lines = [radius.render(), *(f"  {note}" for note in radius.notes())]
        self.query_one("#radius", Static).update("\n".join(lines))

        problems = check(self.workflow, self.registry)
        self.query_one("#problems", Static).update(
            "\n".join(problem.render() for problem in problems)
        )

    # ------------------------------------------------------------- selection

    @property
    def index(self) -> int | None:
        options = self.query_one("#steps", OptionList)
        position = options.highlighted
        if position is None or not (0 <= position < len(self.workflow.steps)):
            return None
        return position

    def _changed(self) -> None:
        self.dirty = True
        self.leaving = False
        self.refresh_view()

    # --------------------------------------------------------------- actions

    def action_add(self) -> None:
        self.app.push_screen(KindPicker(), self._kind_chosen)

    def _kind_chosen(self, kind: str | None) -> None:
        if kind:
            self.app.push_screen(StepForm(kind, self.registry), self._step_added)

    def _step_added(self, step: AnyStep | None) -> None:
        if step is None:
            return
        if any(existing.id == step.id for existing in self.workflow.steps):
            self.notify(f"a step called {step.id} already exists", severity="error", markup=False)
            return
        self.workflow.steps.append(step)
        self._changed()

    def action_edit(self) -> None:
        position = self.index
        if position is None:
            return
        step = self.workflow.steps[position]
        self.app.push_screen(
            StepForm(step.kind, self.registry, step),
            lambda edited, at=position: self._step_edited(at, edited),
        )

    def _step_edited(self, position: int, step: AnyStep | None) -> None:
        if step is None:
            return
        self.workflow.steps[position] = step
        self._changed()

    def action_delete(self) -> None:
        position = self.index
        if position is None:
            return
        removed = self.workflow.steps.pop(position)
        # Leaving a dangling `needs` behind would be a fatal problem the author
        # did not introduce, so the edges go with the step.
        for step in self.workflow.steps:
            step.needs = [need for need in step.needs if need != removed.id]
        self._changed()

    def action_move_up(self) -> None:
        self._move(-1)

    def action_move_down(self) -> None:
        self._move(1)

    def _move(self, delta: int) -> None:
        position = self.index
        if position is None:
            return
        target = position + delta
        if not 0 <= target < len(self.workflow.steps):
            return
        steps = self.workflow.steps
        steps[position], steps[target] = steps[target], steps[position]
        self._changed()
        self.query_one("#steps", OptionList).highlighted = target

    def action_save(self) -> None:
        try:
            path = save(self.workflow, self.settings)
        except Exception as exc:
            self.notify(str(exc), severity="error", markup=False)
            return
        self.dirty = False
        self.leaving = False
        self.refresh_view()
        self.notify(f"saved to {path}", markup=False)

    def action_close(self) -> None:
        if self.dirty and not self.leaving:
            self.leaving = True
            self.notify("unsaved — s to save, or esc again to discard", severity="warning")
            return
        self.dismiss(None)


class _EmptyRegistry:
    """Stands in when the screen is opened without a tool session.

    The designer still works --- every tool step simply validates as unknown,
    which is the truth on a machine with no tools registered.
    """

    def __iter__(self) -> Any:
        return iter(())

    def __contains__(self, name: object) -> bool:
        return False

    def get(self, name: str) -> None:
        return None


def open_designer(app: Any, name: str = "", settings: Any = None) -> None:
    """The route from ``/workflow``: pick a workflow, or name a new one."""
    from altus.core.errors import ConfigError
    from altus.workflow import load

    def start(chosen: str | None) -> None:
        if chosen is None:
            return
        if chosen == NEW:
            app.push_screen(NamePrompt(), named)
            return
        try:
            workflow = load(chosen, settings)
        except ConfigError as exc:
            app.notify(str(exc), severity="error", markup=False)
            return
        app.push_screen(WorkflowScreen(workflow, settings=settings))

    def named(new_name: str | None) -> None:
        if new_name:
            app.push_screen(WorkflowScreen(Workflow(name=new_name), settings=settings))

    existing = list_workflows(settings)
    if not existing:
        app.push_screen(NamePrompt(), named)
        return
    if name:
        start(name)
        return
    app.push_screen(WorkflowPicker(existing), start)
