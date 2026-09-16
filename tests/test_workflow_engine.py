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
from typing import ClassVar

import pytest

from altus.core.events import MessageEnd, MessageStart, TextDelta
from altus.core.session import Session
from altus.core.types import StopReason, Usage
from altus.tools.approval import AllowAll, Decision, ParkOnApproval, RecordingPolicy
from altus.tools.base import BaseTool, ToolContext, ToolOutcome
from altus.tools.registry import ToolRegistry, default_registry
from altus.workflow import (
    ApprovalStep,
    RunRecorder,
    RunRefused,
    ToolStep,
    Workflow,
    check,
    fatal,
    order,
    read_run,
    run_workflow,
    summarise,
)
from altus.workflow.models import AgentStep, Wait
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


# ------------------------------------------------------------ steps that wait


class Polling(BaseTool):
    """Answers with nothing until the nth call. Stands in for CI."""

    name = "ci_status"
    read_only = True

    def __init__(self, ready_on: int = 3) -> None:
        self.ready_on = ready_on
        self.calls = 0

    async def run(self, args: dict, ctx: ToolContext) -> ToolOutcome:
        import json

        self.calls += 1
        done = self.calls >= self.ready_on
        return ToolOutcome(
            content=json.dumps({"run": {"id": 7, "conclusion": "success" if done else None}})
        )


def fake_clock():  # type: ignore[no-untyped-def]
    """A clock that advances 45s per reading, and a sleep that records.

    A twenty-minute CI wait has to be testable in no time at all, and a test
    that actually slept would either be slow or would pin an interval nobody
    wants pinned.
    """
    ticks = iter(range(0, 10_000_000, 45))
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    return (lambda: next(ticks)), sleep, slept


async def test_a_waiting_step_polls_until_the_key_appears(tree: Path) -> None:
    tool = Polling(ready_on=3)
    registry = ToolRegistry([tool])
    now, sleep, slept = fake_clock()
    workflow = Workflow(
        name="ci",
        steps=[ToolStep(id="ci", tool="ci_status", wait=Wait(until="conclusion", interval=30))],
    )
    events = await collect(workflow, registry, context(tree, registry), sleep=sleep, now=now)

    assert [e.attempt for e in kinds(events, "step_waiting")] == [1, 2]
    (finished,) = kinds(events, "step_finished")
    assert finished.ok and finished.attempts == 3
    assert tool.calls == 3
    assert slept == [30.0, 30.0]


async def test_the_condition_finds_a_key_nested_in_the_answer(tree: Path) -> None:
    """The interesting field is never at the top --- a run's `conclusion` sits
    inside the run object."""
    assert Wait(until="conclusion").satisfied('{"run": {"conclusion": "failure"}}')
    assert not Wait(until="conclusion").satisfied('{"run": {"conclusion": null}}')
    assert not Wait(until="conclusion").satisfied('{"run": {}}')


def test_a_condition_cannot_be_found_in_prose() -> None:
    """`until` names a JSON key. Guessing at one with a regex would make it
    mean something different depending on what the server returned."""
    assert not Wait(until="conclusion").satisfied("conclusion: success")
    assert Wait(contains="conclusion: success").satisfied("conclusion: success")


def test_a_wait_needs_exactly_one_condition() -> None:
    for bad in ({}, {"until": "x", "contains": "y"}):
        with pytest.raises(ValueError, match="exactly one"):
            Wait(**bad)


async def test_a_wait_that_never_comes_true_gives_up_and_stops_the_run(
    tree: Path,
) -> None:
    registry = ToolRegistry([Polling(ready_on=99)])
    now, sleep, _ = fake_clock()
    workflow = Workflow(
        name="ci",
        steps=[
            ToolStep(id="ci", tool="ci_status", wait=Wait(until="never", interval=30, timeout=100)),
            ToolStep(id="after", needs=["ci"], tool="ci_status"),
        ],
    )
    events = await collect(workflow, registry, context(tree, registry), sleep=sleep, now=now)

    (finished,) = [e for e in kinds(events, "step_finished") if e.step == "ci"]
    assert not finished.ok
    assert "gave up" in finished.summary
    assert [e.step for e in kinds(events, "step_skipped")] == ["after"]


async def test_only_a_read_may_wait(tree: Path, registry: ToolRegistry) -> None:
    """Waiting means calling the same thing over and over. Nothing about
    "check until it is done" implies anybody wanted a mutation repeated."""
    workflow = Workflow(
        name="x",
        steps=[
            ToolStep(
                id="poll",
                tool="write_file",
                args={"path": "o", "content": "x"},
                wait=Wait(until="done"),
            )
        ],
    )
    problems = fatal(check(workflow, registry))
    assert problems and "poll a read instead" in problems[0].message
    with pytest.raises(RunRefused):
        await collect(workflow, registry, context(tree, registry))


async def test_only_a_tool_step_may_wait(tree: Path, registry: ToolRegistry) -> None:
    workflow = Workflow(
        name="x", steps=[AgentStep(id="think", prompt="go", wait=Wait(contains="done"))]
    )
    problems = fatal(check(workflow, registry))
    assert problems and "only a tool step can wait" in problems[0].message


async def test_a_failing_step_is_not_retried_by_a_wait(tree: Path) -> None:
    """A wait is not a retry. A step that failed did not produce an answer the
    condition could be true of, and calling it again is a different feature
    with a different set of questions."""

    class Broken(BaseTool):
        name = "broken"
        read_only = True

        def __init__(self) -> None:
            self.calls = 0

        async def run(self, args: dict, ctx: ToolContext) -> ToolOutcome:
            self.calls += 1
            return ToolOutcome.error("nope")

    tool = Broken()
    registry = ToolRegistry([tool])
    now, sleep, _ = fake_clock()
    workflow = Workflow(
        name="x", steps=[ToolStep(id="a", tool="broken", wait=Wait(until="x", interval=1))]
    )
    await collect(workflow, registry, context(tree, registry), sleep=sleep, now=now)
    assert tool.calls == 1


# ---------------------------------------------------------------- inputs


def with_inputs(**over):  # type: ignore[no-untyped-def]
    from altus.workflow import Input

    return Workflow(
        name="x",
        inputs={
            "repo": Input(description="owner/name", default="acme/api"),
            "image": Input(description="container image", required=True),
            **over,
        },
        steps=[
            ToolStep(
                id="save",
                tool="write_file",
                args={"path": "out.md", "content": "${inputs.repo} @ ${inputs.image}"},
            )
        ],
    )


async def test_an_input_reaches_every_step_without_a_needs_entry(
    tree: Path, registry: ToolRegistry
) -> None:
    """An input is known before the first step runs rather than produced by
    one, so demanding a dependency on it would be nonsense."""
    from altus.workflow import resolve_inputs

    workflow = with_inputs()
    values = resolve_inputs(workflow, {"image": "acme/api:1.2"})
    assert not fatal(check(workflow, registry))

    await collect(workflow, registry, context(tree, registry), inputs=values)
    assert (tree / "out.md").read_text(encoding="utf-8") == "acme/api @ acme/api:1.2"


def test_a_missing_required_input_refuses_rather_than_substituting_nothing() -> None:
    """Approving a run whose targets were still blank would be approving
    nothing at all."""
    from altus.core.errors import ConfigError
    from altus.workflow import missing_inputs, resolve_inputs

    assert missing_inputs(with_inputs(), {}) == ["image"]
    with pytest.raises(ConfigError, match="needs a value for: image"):
        resolve_inputs(with_inputs(), {})


def test_an_input_nobody_declared_is_refused_not_ignored() -> None:
    """A misspelt `repo=` that silently does nothing runs the workflow against
    whatever the default was, which is the worst of the three outcomes."""
    from altus.core.errors import ConfigError
    from altus.workflow import resolve_inputs

    with pytest.raises(ConfigError, match="declares no input called 'rep'"):
        resolve_inputs(with_inputs(), {"rep": "x", "image": "y"})


def test_a_reference_to_an_undeclared_input_is_fatal(registry: ToolRegistry) -> None:
    workflow = Workflow(
        name="x",
        steps=[ToolStep(id="a", tool="read_file", args={"path": "${inputs.ghost}"})],
    )
    problems = fatal(check(workflow, registry))
    assert problems and "not an input this workflow declares" in problems[0].message


def test_the_current_repo_default_reads_the_checkouts_own_remote(tmp_path: Path) -> None:
    """`@git.origin` is the "current repo" affordance, and the only dynamic
    default there is --- each one is something that can resolve differently on
    two machines, which is what stops a workflow file being portable."""
    import subprocess

    from altus.workflow.inputs import git_origin

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:acme/api.git"], cwd=root, check=True
    )
    assert git_origin(root) == "acme/api"


def test_a_checkout_with_no_remote_resolves_to_nothing_rather_than_failing(
    tmp_path: Path,
) -> None:
    """A workflow carrying this default may still be run with an explicit
    value, and refusing here would make that impossible."""
    from altus.workflow.inputs import git_origin

    assert git_origin(tmp_path) == ""


def test_an_unknown_dynamic_default_is_refused() -> None:
    from altus.core.errors import ConfigError
    from altus.workflow import Input, resolve_inputs

    workflow = Workflow(name="x", inputs={"a": Input(default="@whatever.magic")})
    with pytest.raises(ConfigError, match="not a default Altus knows"):
        resolve_inputs(workflow, {})


def test_inputs_survive_the_file(tmp_path: Path) -> None:
    from altus.workflow import parse, render

    workflow = with_inputs()
    assert parse(render(workflow), name="x") == workflow


def test_a_step_key_order_does_not_shift_under_the_reader() -> None:
    """The format people diff and commit. tomli_w decides between a block and
    a one-line inline table by a heuristic about what the values happen to
    contain, so the same workflow rendered either way depending on whether a
    step had a `needs` entry."""
    from altus.workflow import render

    text = render(
        Workflow(
            name="x",
            steps=[
                ToolStep(
                    id="ci",
                    tool="read_file",
                    args={"path": "a"},
                    wait=Wait(until="conclusion"),
                    on_error="continue",
                )
            ],
        )
    )
    body = [line.split(" =")[0] for line in text.splitlines() if " = " in line]
    assert body == ["name", "id", "kind", "tool", "args", "wait", "on_error"]
    assert "[[steps]]" in text


# --------------------------------------------------------------- concurrency


class SlowTool(BaseTool):
    """A read that takes a while, so "together" and "one after another" differ."""

    name: ClassVar[str] = "slow_read"
    description: ClassVar[str] = "waits, then answers"
    read_only: ClassVar[bool] = True
    input_schema: ClassVar[dict] = {"type": "object", "properties": {"delay": {"type": "number"}}}

    def __init__(self) -> None:
        self.in_flight = 0
        self.most = 0

    async def run(self, args, ctx):  # type: ignore[no-untyped-def]
        self.in_flight += 1
        self.most = max(self.most, self.in_flight)
        try:
            await asyncio.sleep(float(args.get("delay") or 0.05))
        finally:
            self.in_flight -= 1
        return ToolOutcome(content="done", summary="done")


def independent(count: int, **over) -> Workflow:  # type: ignore[no-untyped-def]
    return Workflow(
        name="fan",
        steps=[ToolStep(id=f"s{n}", tool="slow_read", args={}) for n in range(1, count + 1)],
        **over,
    )


async def test_waves_group_steps_with_no_path_between_them() -> None:
    from altus.workflow.engine import waves

    workflow = Workflow(
        name="dag",
        steps=[
            ToolStep(id="a", tool="read_file", args={}),
            ToolStep(id="b", tool="read_file", args={}),
            ToolStep(id="c", tool="read_file", args={}, needs=["a", "b"]),
        ],
    )
    assert [[s.id for s in wave] for wave in waves(workflow)] == [["a", "b"], ["c"]]


async def test_parallel_runs_a_wave_together(tree: Path) -> None:
    tool = SlowTool()
    registry = ToolRegistry([tool])
    events = await collect(independent(3, parallel=3), registry, context(tree, registry))

    assert tool.most == 3
    assert len(kinds(events, "step_finished")) == 3
    assert {e.wave for e in kinds(events, "step_started")} == {1}


async def test_one_at_a_time_is_still_one_at_a_time(tree: Path) -> None:
    """The default has to mean today's behaviour, or every workflow written
    before this existed quietly changed meaning."""
    tool = SlowTool()
    registry = ToolRegistry([tool])
    events = await collect(independent(3), registry, context(tree, registry))

    assert tool.most == 1
    assert [e.step for e in kinds(events, "step_started")] == ["s1", "s2", "s3"]


async def test_the_record_says_how_many_could_run_at_once(tree: Path, tmp_path: Path) -> None:
    tool = SlowTool()
    registry = ToolRegistry([tool])
    events = await collect(independent(2, parallel=2), registry, context(tree, registry))

    started = kinds(events, "run_started")[0]
    assert started.parallel == 2


async def test_a_failure_in_a_wave_lets_what_is_running_finish(tree: Path) -> None:
    """Already in flight is not the same as not yet begun.

    Killing a sibling mid-mutation to honour "the run stops here" would be
    worse than letting it land, so a step that has started finishes; a step
    that has not is skipped by name.
    """
    registry = ToolRegistry(
        [
            SlowTool(),
            *default_registry(kubernetes=False, aws=False, azure=False, gcp=False, mcp=False),
        ]
    )
    workflow = Workflow(
        name="fail",
        parallel=3,
        steps=[
            ToolStep(id="slow", tool="slow_read", args={}),
            read("missing.md", id="bad"),
            read(id="after", needs=["bad"]),
        ],
    )
    events = await collect(workflow, registry, context(tree, registry))

    finished = kinds(events, "run_finished")[0]
    assert finished.state == "failed"
    assert finished.detail.startswith("bad:")
    assert {e.step for e in kinds(events, "step_finished")} == {"slow", "bad"}
    assert [e.step for e in kinds(events, "step_skipped")] == ["after"]


async def test_nothing_new_starts_once_the_run_is_stopping(tree: Path) -> None:
    """Sequential-but-independent: three steps in one wave, the first fails.

    With parallel = 1 the second and third have not begun, so they are skipped
    by name --- exactly what a linear run has always done.
    """
    registry = default_registry(kubernetes=False, aws=False, azure=False, gcp=False, mcp=False)
    workflow = Workflow(
        name="fail",
        steps=[read("missing.md", id="bad"), read(id="b"), read(id="c")],
    )
    events = await collect(workflow, registry, context(tree, registry))

    assert [e.step for e in kinds(events, "step_finished")] == ["bad"]
    assert [e.step for e in kinds(events, "step_skipped")] == ["b", "c"]


async def test_two_steps_in_one_wave_never_prompt_at_once(tree: Path) -> None:
    """Concurrency is for waiting on somebody else's API, not for asking a
    person two questions simultaneously."""

    class Watcher:
        def __init__(self) -> None:
            self.inside = 0
            self.most = 0

        async def request(self, req):  # type: ignore[no-untyped-def]
            self.inside += 1
            self.most = max(self.most, self.inside)
            await asyncio.sleep(0.02)
            self.inside -= 1
            return Decision.ALLOW

    watcher = Watcher()
    registry = default_registry(kubernetes=False, aws=False, azure=False, gcp=False, mcp=False)
    workflow = Workflow(
        name="two-gates",
        parallel=2,
        steps=[
            ToolStep(id="one", tool="write_file", args={"path": "a.txt", "content": "a"}),
            ToolStep(id="two", tool="write_file", args={"path": "b.txt", "content": "b"}),
        ],
    )
    await collect(workflow, registry, context(tree, registry, watcher))

    assert watcher.most == 1


async def test_cancelling_a_parallel_run_still_writes_an_ending(tree: Path, tmp_path: Path) -> None:
    tool = SlowTool()
    registry = ToolRegistry([tool])
    runs = tmp_path / "runs"

    async def go() -> None:
        async with aclosing(
            run_workflow(
                independent(3, parallel=3),
                registry,
                context(tree, registry),
                record=True,
                runs_root=runs,
            )
        ) as events:
            async for _ in events:
                pass

    task = asyncio.create_task(go())
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    run_id = sorted(p.stem for p in runs.glob("*.jsonl"))[0]
    replay = read_run(run_id, runs)
    assert replay[-1].type == "run_finished"
    assert replay[-1].state == "cancelled"
    assert replay[-1].detail == "interrupted"
    assert "cancelled" in summarise(replay)


# ------------------------------------------------------------------- parking


def gated(**over):  # type: ignore[no-untyped-def]
    return ToolStep(tool="write_file", args={"path": "out.txt", "content": "hi"}, **over)


async def park(workflow: Workflow, registry, tree: Path, runs: Path):  # type: ignore[no-untyped-def]
    return await collect(
        workflow,
        registry,
        context(tree, registry, ParkOnApproval()),
        record=True,
        runs_root=runs,
    )


async def test_an_unattended_run_stops_at_the_gate_rather_than_guessing(
    tree: Path, tmp_path: Path, registry
) -> None:
    """Not a denial. A denial is an answer somebody gave; this is nobody
    having been asked, and a record that conflates them records a decision
    that was never made."""
    runs = tmp_path / "runs"
    workflow = Workflow(name="park", steps=[read(id="look"), gated(id="write", needs=["look"])])
    events = await park(workflow, registry, tree, runs)

    parked = kinds(events, "step_parked")
    assert [e.step for e in parked] == ["write"]
    assert parked[0].tool == "write_file"
    assert parked[0].path == "out.txt"
    assert parked[0].detail == "create out.txt"
    assert kinds(events, "run_finished")[0].state == "parked"
    assert not (tree / "out.txt").exists()


async def test_the_parked_step_is_not_called_skipped(tree: Path, tmp_path: Path, registry) -> None:
    """Skipped means "this will not happen". It is waiting, which is different."""
    runs = tmp_path / "runs"
    workflow = Workflow(
        name="park",
        steps=[
            read(id="look"),
            gated(id="write", needs=["look"]),
            read(id="after", needs=["write"]),
        ],
    )
    events = await park(workflow, registry, tree, runs)

    assert [e.step for e in kinds(events, "step_skipped")] == ["after"]


async def test_a_parked_run_is_listed_as_waiting(tree: Path, tmp_path: Path, registry) -> None:
    from altus.workflow.runs import parked_runs

    runs = tmp_path / "runs"
    await park(Workflow(name="park", steps=[gated(id="write")]), registry, tree, runs)

    waiting = parked_runs(runs)
    assert len(waiting) == 1
    assert "parked" in waiting[0][1]


async def test_resume_finishes_the_run_with_a_person_at_the_gate(
    tree: Path, tmp_path: Path, registry
) -> None:
    from altus.workflow.engine import resume_workflow

    runs = tmp_path / "runs"
    workflow = Workflow(
        name="park",
        steps=[
            read(id="look"),
            ToolStep(
                id="write",
                tool="write_file",
                args={"path": "out.txt", "content": "${look}"},
                needs=["look"],
            ),
        ],
    )
    first = await park(workflow, registry, tree, runs)
    run_id = kinds(first, "run_started")[0].run_id

    events = [
        event
        async for event in resume_workflow(
            run_id,
            workflow,
            registry,
            context(tree, registry),
            runs_root=runs,
        )
    ]

    assert kinds(events, "run_resumed")[0].at == "write"
    assert [e.step for e in kinds(events, "step_finished")] == ["write"]
    assert kinds(events, "run_finished")[0].state == "completed"
    # The earlier step's output was replayed from the record, not recomputed.
    assert "hello from step one" in (tree / "out.txt").read_text(encoding="utf-8")


async def test_a_resumed_run_appends_to_the_same_record(
    tree: Path, tmp_path: Path, registry
) -> None:
    from altus.workflow.engine import resume_workflow
    from altus.workflow.runs import is_parked

    runs = tmp_path / "runs"
    workflow = Workflow(name="park", steps=[gated(id="write")])
    run_id = kinds(await park(workflow, registry, tree, runs), "run_started")[0].run_id

    async for _ in resume_workflow(
        run_id, workflow, registry, context(tree, registry), runs_root=runs
    ):
        pass

    replay = read_run(run_id, runs)
    assert [e.type for e in replay].count("run_finished") == 2
    assert not is_parked(replay)
    assert "completed" in summarise(replay)
    assert len(list(runs.glob("*.jsonl"))) == 1


async def test_a_completed_step_is_never_run_twice(tree: Path, tmp_path: Path) -> None:
    """A resume that re-ran a commit to get back to where it stopped would be
    the safety mechanism causing the damage."""
    from altus.workflow.engine import resume_workflow

    counter = SlowTool()
    registry = ToolRegistry(
        [counter, *default_registry(kubernetes=False, aws=False, azure=False, gcp=False, mcp=False)]
    )
    calls = {"n": 0}
    original = counter.run

    async def counting(args, ctx):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return await original(args, ctx)

    counter.run = counting  # type: ignore[method-assign]

    runs = tmp_path / "runs"
    workflow = Workflow(
        name="park",
        steps=[
            ToolStep(id="slow", tool="slow_read", args={"delay": 0.01}),
            gated(id="write", needs=["slow"]),
        ],
    )
    run_id = kinds(await park(workflow, registry, tree, runs), "run_started")[0].run_id
    assert calls["n"] == 1

    async for _ in resume_workflow(
        run_id, workflow, registry, context(tree, registry), runs_root=runs
    ):
        pass
    assert calls["n"] == 1


async def test_an_edited_workflow_refuses_the_resume(tree: Path, tmp_path: Path, registry) -> None:
    """The steps already done were planned from a different file. Finishing
    the new plan would be approving something nobody looked at."""
    from altus.workflow.engine import resume_workflow

    runs = tmp_path / "runs"
    workflow = Workflow(name="park", steps=[read(id="look"), gated(id="write", needs=["look"])])
    run_id = kinds(await park(workflow, registry, tree, runs), "run_started")[0].run_id

    edited = Workflow(
        name="park",
        steps=[
            read(id="look"),
            ToolStep(
                id="write",
                tool="write_file",
                args={"path": "somewhere-else.txt", "content": "hi"},
                needs=["look"],
            ),
        ],
    )
    with pytest.raises(RunRefused) as refused:
        async for _ in resume_workflow(
            run_id, edited, registry, context(tree, registry), runs_root=runs
        ):
            pass
    assert "has changed" in str(refused.value)


async def test_a_run_that_is_not_parked_cannot_be_resumed(
    tree: Path, tmp_path: Path, registry
) -> None:
    from altus.workflow.engine import resume_workflow

    runs = tmp_path / "runs"
    workflow = Workflow(name="fine", steps=[read(id="look")])
    events = await collect(workflow, registry, context(tree, registry), record=True, runs_root=runs)
    run_id = kinds(events, "run_started")[0].run_id

    with pytest.raises(RunRefused) as refused:
        async for _ in resume_workflow(
            run_id, workflow, registry, context(tree, registry), runs_root=runs
        ):
            pass
    assert "not parked" in str(refused.value)


async def test_inputs_are_replayed_rather_than_recomputed(
    tree: Path, tmp_path: Path, registry
) -> None:
    """`@git.origin` resolved in one checkout must not silently re-resolve in
    whatever checkout happens to be current when somebody approves."""
    from altus.workflow.engine import resume_workflow
    from altus.workflow.models import Input

    runs = tmp_path / "runs"
    workflow = Workflow(
        name="park",
        inputs={"who": Input(description="a name")},
        steps=[
            ToolStep(
                id="write",
                tool="write_file",
                args={"path": "out.txt", "content": "${inputs.who}"},
            )
        ],
    )
    events = await collect(
        workflow,
        registry,
        context(
            tree, registry, __import__("altus.tools.approval", fromlist=["x"]).ParkOnApproval()
        ),
        record=True,
        runs_root=runs,
        inputs={"inputs.who": "acme/api"},
    )
    run_id = kinds(events, "run_started")[0].run_id

    async for _ in resume_workflow(
        run_id, workflow, registry, context(tree, registry), runs_root=runs
    ):
        pass
    assert (tree / "out.txt").read_text(encoding="utf-8") == "acme/api"
