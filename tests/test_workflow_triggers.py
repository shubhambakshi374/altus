"""Triggers: what starts a run when nobody asks for one.

Driven by a fake clock, because the thing being tested is "has enough time
passed" and a test that waits is a test nobody runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import ValidationError

from altus.tools.base import BaseTool, ToolContext, ToolOutcome
from altus.tools.registry import ToolRegistry, default_registry
from altus.workflow.models import Input, ToolStep, Trigger, Workflow, duration
from altus.workflow.triggers import Watchpost, next_look, poll, triggered
from altus.workflow.validate import check, fatal
from altus.workspace import Workspace


class Answer(BaseTool):
    """A read whose answer the test controls."""

    name: ClassVar[str] = "look_outside"
    description: ClassVar[str] = "answers whatever it was told to"
    read_only: ClassVar[bool] = True
    input_schema: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    def __init__(self, answer: str = "nothing") -> None:
        self.answer = answer
        self.asked = 0

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        self.asked += 1
        return ToolOutcome(content=self.answer, summary="looked")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(workspace=Workspace(root=tmp_path))


def watching(tool: str = "look_outside", **over: Any) -> Workflow:
    return Workflow(
        name="watcher",
        inputs={"found": Input(description="what changed")},
        triggers=[Trigger(kind="watch", every="30s", tool=tool, into="found", **over)],
        steps=[ToolStep(id="act", tool="list_dir", args={"path": "."})],
    )


# ------------------------------------------------------------------- the shape


def test_a_duration_needs_a_unit() -> None:
    """A bare number is ambiguous between seconds and minutes, and the
    ambiguity is a factor of sixty."""
    assert duration("15m") == 900.0
    assert duration("1h") == 3600.0
    with pytest.raises(ValueError):
        duration("15")


def test_nothing_may_be_polled_faster_than_the_floor() -> None:
    with pytest.raises(ValidationError):
        Trigger(kind="schedule", every="5s")


def test_a_schedule_fires_on_time_alone() -> None:
    with pytest.raises(ValidationError):
        Trigger(kind="schedule", every="1h", tool="read_file", into="x")


def test_a_watch_needs_something_to_look_at_and_somewhere_to_put_it() -> None:
    with pytest.raises(ValidationError):
        Trigger(kind="watch", every="1h", tool="read_file")


# -------------------------------------------------------------------- checking


def test_watching_a_mutation_is_refused() -> None:
    """A watch calls the same thing forever, and nothing about "tell me when
    this changes" implies anybody wanted a write repeated."""
    registry = default_registry(kubernetes=False, aws=False, azure=False, gcp=False, mcp=False)
    problems = fatal(check(watching("write_file"), registry))

    assert problems
    assert "watch a read instead" in problems[0].message


def test_watching_into_an_undeclared_input_is_refused() -> None:
    registry = ToolRegistry([Answer()])
    workflow = Workflow(
        name="watcher",
        triggers=[Trigger(kind="watch", every="30s", tool="look_outside", into="nowhere")],
        steps=[ToolStep(id="act", tool="look_outside", args={})],
    )
    problems = fatal(check(workflow, registry))

    assert problems
    assert "does not declare" in problems[0].message


def test_watching_a_tool_that_is_not_here_is_refused() -> None:
    problems = fatal(check(watching("no_such_tool"), ToolRegistry([Answer()])))
    assert problems
    assert "no tool called" in problems[0].message


# --------------------------------------------------------------------- polling


async def test_starting_the_supervisor_is_not_itself_an_event(
    tmp_path: Path, ctx: ToolContext
) -> None:
    """Otherwise a restart --- after a deploy, after a crash --- is a way to
    run everything at once."""
    clock = Clock()
    workflow = Workflow(
        name="nightly",
        triggers=[Trigger(kind="schedule", every="1h")],
        steps=[ToolStep(id="act", tool="look_outside", args={})],
    )
    post = Watchpost(path=tmp_path / "state.json")

    assert await poll([workflow], ToolRegistry([Answer()]), ctx, post, now=clock) == []
    clock.tick(3601)
    fires = await poll([workflow], ToolRegistry([Answer()]), ctx, post, now=clock)
    assert [f.workflow.name for f in fires] == ["nightly"]
    assert "scheduled" in fires[0].why


async def test_a_schedule_does_not_fire_twice_in_one_interval(
    tmp_path: Path, ctx: ToolContext
) -> None:
    clock = Clock()
    workflow = Workflow(
        name="nightly",
        triggers=[Trigger(kind="schedule", every="1h")],
        steps=[ToolStep(id="act", tool="look_outside", args={})],
    )
    post = Watchpost(path=tmp_path / "state.json")
    registry = ToolRegistry([Answer()])

    await poll([workflow], registry, ctx, post, now=clock)
    clock.tick(3601)
    assert await poll([workflow], registry, ctx, post, now=clock)
    clock.tick(60)
    assert await poll([workflow], registry, ctx, post, now=clock) == []


async def test_a_watch_fires_on_a_change_and_carries_what_changed(
    tmp_path: Path, ctx: ToolContext
) -> None:
    clock = Clock()
    tool = Answer("no detections")
    registry = ToolRegistry([tool])
    post = Watchpost(path=tmp_path / "state.json")
    workflow = watching()

    # First look: recorded, not fired. There is no previous answer to differ from.
    await poll([workflow], registry, ctx, post, now=clock)
    clock.tick(31)
    assert await poll([workflow], registry, ctx, post, now=clock) == []

    tool.answer = "CVE-2026-1 on api-7f"
    clock.tick(31)
    fires = await poll([workflow], registry, ctx, post, now=clock)

    assert len(fires) == 1
    assert fires[0].inputs == {"found": "CVE-2026-1 on api-7f"}
    assert "answered differently" in fires[0].why

    # Same answer next time round: nothing new happened.
    clock.tick(31)
    assert await poll([workflow], registry, ctx, post, now=clock) == []


async def test_a_watch_is_not_looked_at_more_often_than_it_asked(
    tmp_path: Path, ctx: ToolContext
) -> None:
    clock = Clock()
    tool = Answer("a")
    post = Watchpost(path=tmp_path / "state.json")
    workflow = watching()

    await poll([workflow], ToolRegistry([tool]), ctx, post, now=clock)
    for _ in range(5):
        clock.tick(5)
        await poll([workflow], ToolRegistry([tool]), ctx, post, now=clock)
    assert tool.asked == 0

    clock.tick(31)
    await poll([workflow], ToolRegistry([tool]), ctx, post, now=clock)
    assert tool.asked == 1


async def test_a_tool_that_fails_fires_nothing(tmp_path: Path, ctx: ToolContext) -> None:
    """A watch that fired on "I could not look" would turn an outage into a
    run against stale information."""

    class Broken(Answer):
        async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
            return ToolOutcome.error("the API is down", summary="unreachable")

    clock = Clock()
    post = Watchpost(path=tmp_path / "state.json")
    registry = ToolRegistry([Broken()])

    await poll([watching()], registry, ctx, post, now=clock)
    clock.tick(31)
    assert await poll([watching()], registry, ctx, post, now=clock) == []


async def test_state_outlives_the_process(tmp_path: Path, ctx: ToolContext) -> None:
    """Restarting must not re-fire what already fired."""
    clock = Clock()
    where = tmp_path / "state.json"
    workflow = Workflow(
        name="nightly",
        triggers=[Trigger(kind="schedule", every="1h")],
        steps=[ToolStep(id="act", tool="look_outside", args={})],
    )
    registry = ToolRegistry([Answer()])

    await poll([workflow], registry, ctx, Watchpost.load(where), now=clock)
    clock.tick(60)
    assert await poll([workflow], registry, ctx, Watchpost.load(where), now=clock) == []
    clock.tick(3601)
    assert await poll([workflow], registry, ctx, Watchpost.load(where), now=clock)


async def test_an_unreadable_state_file_does_not_stop_the_supervisor(tmp_path: Path) -> None:
    where = tmp_path / "state.json"
    where.write_text("{not json", encoding="utf-8")
    assert Watchpost.load(where).data == {}


def test_only_workflows_with_triggers_are_watched() -> None:
    plain = Workflow(name="plain", steps=[ToolStep(id="a", tool="look_outside", args={})])
    assert [w.name for w in triggered([plain, watching()])] == ["watcher"]


def test_the_supervisor_looks_as_often_as_the_keenest_trigger() -> None:
    assert next_look([watching()]) == 30.0
    assert next_look([]) == 30.0
