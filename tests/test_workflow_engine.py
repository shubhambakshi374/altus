"""Running a workflow.

Four questions were left open when the designer shipped, and these are the
tests that pin the answers: how a step's output reaches the next, what a
failure means for the rest, whether an approval mid-run stops everything, and
what the record says afterwards --- especially about a run that did not finish.
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from pathlib import Path

import pytest

from altus.core.events import MessageEnd, MessageStart, TextDelta
from altus.core.session import Session
from altus.core.types import StopReason, Usage
from altus.tools.approval import AllowAll, Decision, RecordingPolicy
from altus.tools.base import ToolContext
from altus.tools.registry import ToolRegistry, default_registry
from altus.workflow import (
    ApprovalStep,
    RunRecorder,
    RunRefused,
    ToolStep,
    Workflow,
    order,
    read_run,
    run_workflow,
    summarise,
)
from altus.workflow.models import AgentStep
from altus.workspace import Workspace
from tests.conftest import FakeProvider


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "notes.md").write_text("hello from step one\n", encoding="utf-8")
    return root


@pytest.fixture
def registry() -> ToolRegistry:
    """Only the filesystem tools: real, fast, and no cloud anywhere near them."""
    return default_registry(kubernetes=False, aws=False, azure=False, gcp=False, mcp=False)


def context(tree: Path, registry: ToolRegistry, policy=None) -> ToolContext:  # type: ignore[no-untyped-def]
    return ToolContext(
        workspace=Workspace(root=tree),
        approvals=policy or AllowAll(),
        registry=registry,
    )


async def collect(*args, **kwargs):  # type: ignore[no-untyped-def]
    kwargs.setdefault("record", False)
    return [event async for event in run_workflow(*args, **kwargs)]


def kinds(events, wanted: str):  # type: ignore[no-untyped-def]
    return [event for event in events if event.type == wanted]


def read(path: str = "notes.md", **over):  # type: ignore[no-untyped-def]
    return ToolStep(tool="read_file", args={"path": path}, **over)


# ----------------------------------------------------------------------- order


def test_steps_run_in_dependency_order_not_file_order() -> None:
    workflow = Workflow(
        name="x",
        steps=[
            read(id="last", needs=["middle"]),
            read(id="first"),
            read(id="middle", needs=["first"]),
        ],
    )
    assert [step.id for step in order(workflow)] == ["first", "middle", "last"]


def test_independent_steps_keep_the_order_they_were_written_in() -> None:
    """Deterministic on purpose: two runs of one workflow that ordered their
    steps differently would make comparing two records meaningless."""
    workflow = Workflow(name="x", steps=[read(id="a"), read(id="b"), read(id="c")])
    assert [step.id for step in order(workflow)] == ["a", "b", "c"]


# ------------------------------------------------------------------ data flow


async def test_a_steps_output_reaches_the_next_one(tree: Path, registry: ToolRegistry) -> None:
    workflow = Workflow(
        name="x",
        steps=[
            read(id="source"),
            ToolStep(
                id="sink",
                needs=["source"],
                tool="write_file",
                args={"path": "out.md", "content": "got: ${source}"},
            ),
        ],
    )
    await collect(workflow, registry, context(tree, registry))
    assert "hello from step one" in (tree / "out.md").read_text(encoding="utf-8")


async def test_a_reference_to_a_step_it_does_not_depend_on_refuses_to_run(
    tree: Path, registry: ToolRegistry
) -> None:
    """Ordering in the file guarantees nothing --- the engine runs the graph.

    A reference to a merely-earlier step would work until somebody reordered
    two independent steps, which is the worst kind of bug to ship.
    """
    workflow = Workflow(
        name="x",
        steps=[
            read(id="source"),
            ToolStep(id="sink", tool="write_file", args={"path": "o", "content": "${source}"}),
        ],
    )
    with pytest.raises(RunRefused, match="needs"):
        await collect(workflow, registry, context(tree, registry))
    assert not (tree / "o").exists()


async def test_nothing_runs_when_validation_fails(tree: Path, registry: ToolRegistry) -> None:
    workflow = Workflow(name="x", steps=[ToolStep(id="a", tool="no_such_tool")])
    with pytest.raises(RunRefused):
        await collect(workflow, registry, context(tree, registry))


# --------------------------------------------------------------------- failure


async def test_a_failure_stops_the_run_and_names_what_did_not_happen(
    tree: Path, registry: ToolRegistry
) -> None:
    """A trail that cannot say what did *not* happen is not a trail."""
    workflow = Workflow(
        name="x",
        steps=[
            read(id="ok"),
            read("gone.md", id="boom", needs=["ok"]),
            read(id="after", needs=["boom"]),
        ],
    )
    events = await collect(workflow, registry, context(tree, registry))

    (skipped,) = kinds(events, "step_skipped")
    assert skipped.step == "after"
    (done,) = kinds(events, "run_finished")
    assert done.state == "failed"
    assert done.ran == 2 and done.skipped == 1
    assert "boom" in done.detail


async def test_on_error_continue_carries_on_but_dependents_still_skip(
    tree: Path, registry: ToolRegistry
) -> None:
    """`continue` means the run survives, not that the failure did not matter:
    a step that needed the output still has nothing to work from."""
    workflow = Workflow(
        name="x",
        steps=[
            read("gone.md", id="boom", on_error="continue"),
            read(id="dependent", needs=["boom"]),
            read(id="independent"),
        ],
    )
    events = await collect(workflow, registry, context(tree, registry))

    (done,) = kinds(events, "run_finished")
    assert done.state == "completed"
    assert [s.step for s in kinds(events, "step_skipped")] == ["dependent"]
    assert [f.step for f in kinds(events, "step_finished") if f.ok] == ["independent"]


# -------------------------------------------------------------------- approval


async def test_an_approval_step_that_is_declined_stops_the_run(
    tree: Path, registry: ToolRegistry
) -> None:
    policy = RecordingPolicy(decision=Decision.DENY)
    workflow = Workflow(
        name="x",
        steps=[
            read(id="one"),
            ApprovalStep(id="gate", needs=["one"], message="go on?"),
            ToolStep(
                id="after", needs=["gate"], tool="write_file", args={"path": "o", "content": "x"}
            ),
        ],
    )
    events = await collect(workflow, registry, context(tree, registry, policy))

    (done,) = kinds(events, "run_finished")
    assert done.state == "denied"
    assert not (tree / "o").exists()


async def test_the_approval_prompt_does_not_pretend_to_have_previewed_anything(
    tree: Path, registry: ToolRegistry
) -> None:
    policy = RecordingPolicy()
    workflow = Workflow(name="x", steps=[ApprovalStep(id="gate", message="look ok?")])
    await collect(workflow, registry, context(tree, registry, policy))

    (request,) = policy.seen
    assert "look ok?" in request.dry_run
    assert "Nothing was previewed" in request.dry_run
    assert request.diff == ""


async def test_every_mutation_still_asks_for_itself(tree: Path, registry: ToolRegistry) -> None:
    """The engine adds a gate, it does not replace one. A run-level yes must
    not become standing consent for each write inside it."""
    policy = RecordingPolicy()
    workflow = Workflow(
        name="x",
        steps=[
            ToolStep(id="a", tool="write_file", args={"path": "one", "content": "1"}),
            ToolStep(id="b", needs=["a"], tool="write_file", args={"path": "two", "content": "2"}),
        ],
    )
    await collect(workflow, registry, context(tree, registry, policy))
    assert [request.tool for request in policy.seen] == ["write_file", "write_file"]


async def test_a_run_can_be_refused_before_anything_starts(
    tree: Path, registry: ToolRegistry
) -> None:
    """Agreeing to six actions one at a time, with no sight of the whole, is
    how consent gets worn down. So there is one question up front too."""
    seen: list = []

    async def refuse(started, workflow):  # type: ignore[no-untyped-def]
        seen.append(started)
        return False

    workflow = Workflow(
        name="x", steps=[ToolStep(id="a", tool="write_file", args={"path": "o", "content": "x"})]
    )
    with pytest.raises(RunRefused):
        await collect(workflow, registry, context(tree, registry), confirm=refuse)

    assert seen[0].blast.value == "mutate", "and it is told the blast radius first"
    assert not (tree / "o").exists()


# ----------------------------------------------------------------- agent steps


def fake_session() -> Session:
    return Session(provider="fake", model="fake-1", max_tokens=64)


def answering(text: str) -> FakeProvider:
    return FakeProvider(
        [
            MessageStart(model="fake-1"),
            TextDelta(text=text),
            MessageEnd(stop_reason=StopReason.END_TURN, usage=Usage()),
        ]
    )


async def test_an_agent_step_answers_and_its_answer_flows_on(
    tree: Path, registry: ToolRegistry
) -> None:
    workflow = Workflow(
        name="x",
        steps=[
            AgentStep(id="think", prompt="say something", tools=[]),
            ToolStep(
                id="save",
                needs=["think"],
                tool="write_file",
                args={"path": "out.md", "content": "${think}"},
            ),
        ],
    )
    await collect(
        workflow,
        registry,
        context(tree, registry),
        provider=answering("the answer"),
        session=fake_session(),
    )
    assert (tree / "out.md").read_text(encoding="utf-8") == "the answer"


async def test_an_agent_step_starts_from_nothing_rather_than_the_users_chat(
    tree: Path, registry: ToolRegistry
) -> None:
    """A workflow that behaved differently depending on what was said to Altus
    beforehand would not be a workflow."""
    provider = answering("fine")
    session = fake_session()
    session.append(__import__("altus.core.types", fromlist=["Message"]).Message.user("unrelated"))

    workflow = Workflow(name="x", steps=[AgentStep(id="think", prompt="the only thing")])
    await collect(workflow, registry, context(tree, registry), provider=provider, session=session)

    (request,) = provider.calls
    assert [message.text for message in request.messages] == ["the only thing"]


async def test_an_agent_step_only_gets_the_tools_it_named(
    tree: Path, registry: ToolRegistry
) -> None:
    """The whole reason naming them is worth an author's while."""
    provider = answering("done")
    workflow = Workflow(name="x", steps=[AgentStep(id="think", prompt="go", tools=["read_file"])])
    await collect(
        workflow, registry, context(tree, registry), provider=provider, session=fake_session()
    )
    (request,) = provider.calls
    assert [tool.name for tool in request.tools] == ["read_file"]


async def test_an_agent_step_without_a_provider_fails_clearly(
    tree: Path, registry: ToolRegistry
) -> None:
    workflow = Workflow(name="x", steps=[AgentStep(id="think", prompt="go")])
    events = await collect(workflow, registry, context(tree, registry))
    (finished,) = kinds(events, "step_finished")
    assert not finished.ok
    assert "needs a model" in finished.output


# -------------------------------------------------------------------- the record


async def test_the_record_holds_every_event_in_order(
    tree: Path, registry: ToolRegistry, tmp_path: Path
) -> None:
    root = tmp_path / "runs"
    workflow = Workflow(name="recorded", steps=[read(id="a"), read(id="b", needs=["a"])])

    run_id = ""
    async for event in run_workflow(workflow, registry, context(tree, registry), runs_root=root):
        if event.type == "run_started":
            run_id = event.run_id

    events = read_run(run_id, root)
    assert [event.type for event in events] == [
        "run_started",
        "step_started",
        "step_finished",
        "step_started",
        "step_finished",
        "run_finished",
    ]
    assert "completed" in summarise(events)


def test_a_run_with_no_ending_reads_as_interrupted(tmp_path: Path) -> None:
    """The one case the record exists for. Calling this "completed" would be
    the trail lying about exactly the run you went looking for."""
    from altus.workflow.events import RunStarted

    recorder = RunRecorder("r_half", tmp_path)
    recorder.write(RunStarted(run_id="r_half", workflow="nightly", started="2026-01-01T00:00:00"))
    assert "interrupted" in summarise(read_run("r_half", tmp_path))


def test_a_truncated_line_does_not_make_the_record_unreadable(tmp_path: Path) -> None:
    from altus.workflow.events import RunStarted

    recorder = RunRecorder("r_torn", tmp_path)
    recorder.write(RunStarted(run_id="r_torn", workflow="x", started="2026-01-01T00:00:00"))
    with recorder.path.open("a", encoding="utf-8") as fh:
        fh.write('{"type": "step_star')
    assert len(read_run("r_torn", tmp_path)) == 1


def test_a_recorder_that_cannot_write_does_not_take_the_run_with_it(tmp_path: Path) -> None:
    """Losing the trail is bad. Losing a half-applied change because the disk
    filled up is worse."""
    from altus.workflow.events import RunStarted

    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    recorder = RunRecorder("r_x", blocker)
    recorder.write(RunStarted(run_id="r_x", workflow="x", started="2026-01-01T00:00:00"))
    assert recorder.broken


async def test_a_cancelled_run_still_leaves_a_record(
    tree: Path, registry: ToolRegistry, tmp_path: Path
) -> None:
    root = tmp_path / "runs"
    workflow = Workflow(name="slow", steps=[read(id="a"), read(id="b", needs=["a"])])
    run_id = ""

    async def drive() -> None:
        nonlocal run_id
        # aclosing, so the generator is closed at a point this test controls
        # rather than whenever it is collected. The TUI drives it the same way.
        stream = run_workflow(workflow, registry, context(tree, registry), runs_root=root)
        async with aclosing(stream) as events:
            async for event in events:
                if event.type == "run_started":
                    run_id = event.run_id
                if event.type == "step_finished":
                    raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await drive()

    events = read_run(run_id, root)
    assert events[-1].type == "run_finished"
    assert events[-1].state == "cancelled"


# --------------------------------------------------------------- the run screen


def make_app(tree: Path):  # type: ignore[no-untyped-def]
    from altus.config.models import Config
    from altus.tools.approval import SessionApprovals
    from altus.tui.app import AltusApp

    app = AltusApp(config=Config(), provider=FakeProvider(), workspace_root=str(tree))
    app.approvals = SessionApprovals(AllowAll())
    app.tool_ctx.approvals = app.approvals
    return app


async def drive(app, screen, pilot) -> None:  # type: ignore[no-untyped-def]
    await app.push_screen(screen)
    for _ in range(80):
        await pilot.pause()
        if screen.finished:
            return
    raise AssertionError("the run never finished")


def rows(screen) -> str:  # type: ignore[no-untyped-def]
    return "\n".join(str(widget.render()) for widget in screen.query(".step"))


def outcome(screen) -> str:  # type: ignore[no-untyped-def]
    from textual.widgets import Static

    return str(screen.query_one("#outcome", Static).render())


async def test_the_screen_shows_each_step_resolving(tree: Path) -> None:
    from altus.tui.screens.run import RunScreen

    workflow = Workflow(
        name="demo",
        steps=[
            read(id="ok"),
            read("gone.md", id="boom", needs=["ok"]),
            read(id="never", needs=["boom"]),
        ],
    )
    app = make_app(tree)
    async with app.run_test() as pilot:
        screen = RunScreen(workflow)
        await drive(app, screen, pilot)

        shown = rows(screen)
        assert "✓ ok" in shown
        assert "✗ boom" in shown
        assert "the run stopped at boom" in shown
        assert "Stopped by a failure" in outcome(screen)
        assert "record:" in outcome(screen), "and where to read it back"


async def test_the_run_gate_fires_before_any_step(tree: Path) -> None:
    """Agreeing to six actions one at a time, with no sight of the whole, is
    how consent gets worn down. So there is one question up front too."""
    from altus.tools.approval import RecordingPolicy, SessionApprovals
    from altus.tui.screens.run import RunScreen

    policy = RecordingPolicy()
    app = make_app(tree)
    async with app.run_test() as pilot:
        app.approvals = SessionApprovals(policy)
        app.tool_ctx.approvals = app.approvals
        workflow = Workflow(name="demo", steps=[read(id="a"), read(id="b", needs=["a"])])
        await drive(app, RunScreen(workflow), pilot)

    first = policy.seen[0]
    assert first.tool == "workflow" and first.action == "run"
    assert "Nothing has run yet" in first.dry_run
    assert "a" in first.diff and "b" in first.diff, "the plan, in the order it will run"


async def test_a_privileged_workflow_demands_the_typed_challenge_to_run(
    tree: Path, registry: ToolRegistry
) -> None:
    """Nothing in the run screen knows this rule. It goes through the one gate,
    so it inherits the rule the rest of Altus already follows."""
    from altus.tools.approval import RecordingPolicy, SessionApprovals
    from altus.tui.screens.run import RunScreen

    policy = RecordingPolicy()
    app = make_app(tree)
    async with app.run_test() as pilot:
        app.approvals = SessionApprovals(policy)
        app.tool_ctx.approvals = app.approvals
        app.registry.add(_privileged_tool())
        workflow = Workflow(name="demo", steps=[ToolStep(id="a", tool="danger_tool")])
        await drive(app, RunScreen(workflow), pilot)

    assert policy.seen[0].needs_challenge
    assert not policy.seen[0].may_grant_always


def _privileged_tool():  # type: ignore[no-untyped-def]
    from altus.cloud.base import Sensitivity
    from altus.tools.base import BaseTool, ToolOutcome

    class Danger(BaseTool):
        name = "danger_tool"
        read_only = False

        @classmethod
        def static_sensitivity(cls) -> Sensitivity:
            return Sensitivity.PRIVILEGED

        async def run(self, args, ctx):  # type: ignore[no-untyped-def]
            return ToolOutcome(content="done")

    return Danger()


async def test_declining_the_run_gate_runs_nothing(tree: Path) -> None:
    from altus.tools.approval import Decision, RecordingPolicy, SessionApprovals
    from altus.tui.screens.run import RunScreen

    app = make_app(tree)
    async with app.run_test() as pilot:
        app.approvals = SessionApprovals(RecordingPolicy(decision=Decision.DENY))
        app.tool_ctx.approvals = app.approvals
        workflow = Workflow(
            name="demo",
            steps=[ToolStep(id="a", tool="write_file", args={"path": "o", "content": "x"})],
        )
        screen = RunScreen(workflow)
        await drive(app, screen, pilot)

        assert "not started" in outcome(screen)
        assert not (tree / "o").exists()


async def test_past_runs_are_listed_and_readable(tree: Path) -> None:
    from altus.tui.commands import dispatch
    from altus.tui.screens.run import RunScreen

    app = make_app(tree)
    async with app.run_test() as pilot:
        await drive(app, RunScreen(Workflow(name="demo", steps=[read(id="a")])), pilot)

        listing = await dispatch(app, app.commands, "/workflow runs")
        assert "demo" in listing.body and "completed" in listing.body

        run_id = next(word for word in listing.body.split() if word.startswith("r_"))
        one = await dispatch(app, app.commands, f"/workflow runs {run_id}")
        assert "a" in one.body and "read_file" in one.body


async def test_runs_says_so_when_there_are_none() -> None:
    from altus.config.models import Config
    from altus.tui.app import AltusApp
    from altus.tui.commands import dispatch

    app = AltusApp(config=Config(), provider=FakeProvider())
    async with app.run_test():
        result = await dispatch(app, app.commands, "/workflow runs")
        assert "No runs recorded yet" in result.body
