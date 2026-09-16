"""Watching a workflow run.

One row per step, updated as the engine yields. The rows are the same events
that go into the run record, so what you watched and what you can read back
afterwards cannot disagree.

The run-level gate goes through the ordinary approval machinery rather than a
bespoke "are you sure" --- which means a workflow whose blast radius is
privileged, or whose target is protected, demands the typed challenge here for
exactly the same reason a single privileged call does. Nothing new had to be
invented for that, which is the point of having one gate.
"""

from __future__ import annotations

from contextlib import aclosing
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.screen import Screen
from textual.widgets import Label, Static

from altus.workflow import Workflow, blast_radius, describe, run_workflow
from altus.workflow.engine import RunRefused

MARKS = {"pending": "·", "running": "▸", "ok": "✓", "failed": "✗", "skipped": "~"}


class RunScreen(Screen[None]):
    """A workflow running, step by step."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Back"),
        Binding("q", "close", "Back"),
        Binding("ctrl+c", "stop", "Stop"),
    ]

    DEFAULT_CSS = """
    RunScreen { background: $surface; }
    RunScreen > Vertical { height: 1fr; padding: 1 2; }
    RunScreen .title { text-style: bold; }
    RunScreen .hint { color: $text-muted; }
    RunScreen .step { height: auto; }
    RunScreen .step-running { color: $accent; }
    RunScreen .step-ok { color: $success; }
    RunScreen .step-failed { color: $error; }
    RunScreen .step-skipped { color: $text-muted; }
    RunScreen #outcome { height: auto; padding-top: 1; }
    RunScreen VerticalScroll { height: 1fr; }
    """

    def __init__(self, workflow: Workflow, *, settings: Any = None) -> None:
        super().__init__()
        self.workflow = workflow
        self.settings = settings
        self.run_id = ""
        self.finished = False

    def compose(self) -> ComposeResult:
        radius = blast_radius(self.workflow, getattr(self.app, "registry", None) or ())
        with Vertical():
            yield Label(Content(f"Running {self.workflow.name}"), classes="title")
            yield Static(
                "\n".join([radius.render(), *(f"  {note}" for note in radius.notes())]),
                classes="hint",
                markup=False,
            )
            with VerticalScroll():
                for index, step in enumerate(self.workflow.steps):
                    yield Static(
                        self._row(step.id, "pending", describe(step)),
                        classes="step",
                        id=f"s{index}",
                        markup=False,
                    )
            yield Static("", id="outcome", markup=False)
            yield Label("esc to go back · ctrl+c to stop", classes="hint")

    def on_mount(self) -> None:
        self.run_worker(self._drive(), exclusive=True, group="workflow-run")

    # ------------------------------------------------------------------ rows

    def _row(self, step_id: str, state: str, detail: str, extra: str = "") -> str:
        return f"  {MARKS[state]} {step_id:<18} {detail:<44} {extra}".rstrip()

    def _widget(self, step_id: str) -> Static | None:
        index = next((i for i, s in enumerate(self.workflow.steps) if s.id == step_id), None)
        return None if index is None else self.query_one(f"#s{index}", Static)

    def _set(self, step_id: str, state: str, detail: str, extra: str = "") -> None:
        widget = self._widget(step_id)
        if widget is None:
            return
        widget.update(self._row(step_id, state, detail, extra))
        widget.set_classes(f"step step-{state}" if state != "pending" else "step")
        widget.scroll_visible(animate=False)

    def _say(self, text: str, classes: str = "") -> None:
        outcome = self.query_one("#outcome", Static)
        outcome.update(text)
        outcome.set_classes(classes)

    # ------------------------------------------------------------------- run

    async def _drive(self) -> None:
        app = self.app
        registry = getattr(app, "registry", None)
        ctx = getattr(app, "tool_ctx", None)
        if registry is None or ctx is None:
            self._say("no tool session, so nothing can run", "step-failed")
            self.finished = True
            return

        details = {step.id: describe(step) for step in self.workflow.steps}
        stream = run_workflow(
            self.workflow,
            registry,
            ctx,
            provider=getattr(app, "provider", None),
            session=getattr(app, "session", None),
            confirm=self._confirm,
        )
        try:
            # aclosing so a cancelled run is closed here rather than whenever
            # the generator happens to be collected --- that close is what
            # writes the cancellation into the record.
            async with aclosing(stream) as events:
                async for event in events:
                    self._apply(event, details)
        except RunRefused as refused:
            self._say(str(refused), "step-skipped")
        except Exception as exc:
            self._say(f"the run could not continue: {exc}", "step-failed")
        finally:
            self.finished = True

    def _apply(self, event: Any, details: dict[str, str]) -> None:
        match event.type:
            case "run_started":
                self.run_id = event.run_id
                self._say(f"run {event.run_id}")
            case "step_started":
                self._set(event.step, "running", event.detail or details.get(event.step, ""))
            case "step_finished":
                state = "ok" if event.ok else "failed"
                extra = f"{event.summary}  {event.seconds}s"
                self._set(event.step, state, details.get(event.step, ""), extra)
            case "step_skipped":
                self._set(event.step, "skipped", details.get(event.step, ""), event.reason)
            case "run_finished":
                self._finish(event)

    def _finish(self, event: Any) -> None:
        from altus.workflow.runs import runs_dir

        words = {
            "completed": "Completed",
            "failed": "Stopped by a failure",
            "denied": "Stopped: declined",
            "cancelled": "Stopped: interrupted",
        }
        lines = [
            f"{words.get(event.state, event.state)} — {event.ran} steps ran, "
            f"{event.skipped} skipped, {event.seconds}s",
        ]
        if event.detail:
            lines.append(f"  {event.detail}")
        lines.append(f"  record: {runs_dir() / (event.run_id + '.jsonl')}")
        self._say("\n".join(lines), "step-ok" if event.state == "completed" else "step-failed")

    async def _confirm(self, started: Any, workflow: Workflow) -> bool:
        """One question before anything happens, through the ordinary gate.

        Going through ``ApprovalRequest`` rather than a bespoke prompt is what
        makes a privileged workflow demand the typed challenge here without
        this screen knowing the rule.
        """
        from altus.tools.approval import ApprovalRequest, Decision
        from altus.workflow.models import AgentStep

        ctx = getattr(self.app, "tool_ctx", None)
        if ctx is None:
            return False
        thinking = sum(1 for step in workflow.steps if isinstance(step, AgentStep))
        cost = f"\n{thinking} of them call the model, which costs you money." if thinking else ""
        decision = await ctx.approvals.request(
            ApprovalRequest(
                tool="workflow",
                action="run",
                path=workflow.name,
                target=started.blast.value,
                diff=_plan(workflow),
                dry_run=(
                    "Nothing has run yet, and nothing here was previewed.\n"
                    f"{len(workflow.steps)} steps, in this order.{cost}\n"
                    "Every step that changes something still asks for itself "
                    "as it is reached; this is the question about the run."
                ),
                sensitivity=started.blast,
            )
        )
        return decision is not Decision.DENY

    # --------------------------------------------------------------- actions

    def action_stop(self) -> None:
        if self.finished:
            return
        self.workers.cancel_group(self, "workflow-run")
        self._say("stopping…", "step-skipped")

    def action_close(self) -> None:
        if not self.finished:
            self.action_stop()
            return
        self.dismiss(None)


def _plan(workflow: Workflow) -> str:
    """The steps, in the order they will run, for the approval prompt."""
    from altus.workflow import order

    return "\n".join(
        f"{index:>2}  {step.id:<16} {step.kind:<9} {describe(step)}"
        for index, step in enumerate(order(workflow), 1)
    )
