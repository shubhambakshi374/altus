"""Running a workflow.

Four decisions were deferred out of the designer increment on purpose, because
each one is a real choice and none of them is better made under the pressure of
having already shipped half an executor. All four are settled here.

Inputs are seeded into the same substitution table the steps write to, so
``${inputs.repo}`` costs no new mechanism: it is an output that happened to be
known before the run began.

**How a step's output reaches the next.** ``${step}`` substitution, and nothing
else --- see ``refs.py`` for why an expression language was the wrong trade.
A step may also *wait*: run again until its output satisfies a condition, which
is what "check CI has passed" and "check the PR is merged" actually mean. Only
reads may wait, and the condition is two forms with no operators.

**What happens when step 3 of 6 fails.** The run stops, and everything
downstream is *skipped* rather than failed: a step that never ran did not fail,
and a record that says otherwise is a record nobody can reason about later. A
step may opt into ``on_error = "continue"`` where the rest genuinely does not
depend on it.

**Whether an approval mid-run blocks the whole workflow.** Yes, and the
existing per-call gate is untouched: the engine calls ``registry.execute``, so
every mutation prompts exactly as it does in chat. What the engine adds is one
run-level confirmation before anything starts --- because agreeing to six
actions individually, in sequence, with no sight of the whole, is how consent
gets worn down. A denial at either level stops the run.

**How a run is recorded.** Every event is written to a JSONL run record as it
happens (``runs.py``), so an interrupted run leaves a readable trail rather
than nothing.

Steps run in dependency order, one wave at a time, and one at a time within a
wave unless the workflow asks for more. ``needs`` describes a DAG, so the steps
in a wave have no path between them and could run together --- but that is
``parallel`` in the workflow file rather than a default, because the author is
the one who knows whether two steps with no dependency edge are *really*
independent, and because a sequential run is the one whose record reads like
what happened.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from altus.cloud.base import Sensitivity
from altus.core.session import Session
from altus.core.types import Role
from altus.tools.approval import Parked
from altus.tools.base import ToolContext
from altus.tools.registry import ToolRegistry
from altus.workflow.blast import blast_radius
from altus.workflow.events import (
    RunEvent,
    RunFinished,
    RunResumed,
    RunStarted,
    StepFinished,
    StepParked,
    StepSkipped,
    StepStarted,
    StepWaiting,
)
from altus.workflow.models import AgentStep, AnyStep, ApprovalStep, ToolStep, Workflow
from altus.workflow.refs import substitute
from altus.workflow.runs import RunRecorder
from altus.workflow.store import fingerprint
from altus.workflow.validate import check
from altus.workflow.validate import fatal as fatal_problems

MAX_OUTPUT = 40_000
"""What one step may hand on. A step's output becomes another step's argument
and, for an agent step, part of a prompt somebody pays for."""

MAX_AGENT_ITERATIONS = 15

_DONE = object()
"""Put on the queue when a wave's task group has closed."""


class RunRefused(Exception):
    """The workflow was not started. Carries the reason, already phrased."""


@dataclass(frozen=True)
class Resume:
    """Everything a parked run needs to be picked up where it stopped."""

    run_id: str
    outputs: dict[str, str]
    """What the earlier steps produced, replayed from the record."""
    completed: set[str]
    """Steps that already finished. They are not run again --- a resume that
    re-ran a commit would be the safety mechanism causing the damage."""
    at: str = ""
    """The step it parked on."""


@dataclass
class RunState:
    """What has happened so far. The engine's only memory."""

    run_id: str
    outputs: dict[str, str] = field(default_factory=dict)
    failed: set[str] = field(default_factory=set)
    skipped: set[str] = field(default_factory=set)
    ran: int = 0
    done: set[str] = field(default_factory=set)
    """Steps that produced a result, whatever that result was. Distinct from
    ``ran`` because the skip loop needs names, not a count."""
    parked_at: str = ""
    """The step that reached a gate with nobody there to answer. It produced no
    output and is the first thing a resume runs again."""
    stopped_at: str = ""
    """The step whose failure ended the run, set by whichever failing step got
    there first. Also the signal to every step in the same wave that has not
    started yet: a run that is stopping does not begin anything new."""
    stopped_by: tuple[str, bool] = ("", False)
    """``(summary, denied)`` from that step, for the closing event."""

    @property
    def stopping(self) -> bool:
        return bool(self.stopped_at)

    def blocked_by(self, step: AnyStep) -> str:
        """Which dependency stops this step, if any."""
        for need in step.needs:
            if need in self.failed:
                return f"{need} failed"
            if need in self.skipped:
                return f"{need} was skipped"
        return ""


def waves(workflow: Workflow) -> list[list[AnyStep]]:
    """Dependency frontiers: each list is steps with no path between them.

    Deterministic on purpose --- ties broken by the file's order --- because
    two runs of the same workflow must produce the same record, or comparing
    two runs tells you nothing.
    """
    remaining = list(workflow.steps)
    done: set[str] = set()
    found: list[list[AnyStep]] = []
    while remaining:
        ready = [
            s
            for s in remaining
            if all(n in done or n not in {x.id for x in remaining} for n in s.needs)
        ]
        if not ready:
            # A cycle. `check` refuses to run these, so reaching here means a
            # caller skipped validation; degrade to file order rather than spin.
            found.append(remaining)
            return found
        found.append(ready)
        for step in ready:
            done.add(step.id)
            remaining.remove(step)
    return found


def order(workflow: Workflow) -> list[AnyStep]:
    """The same graph flattened: what a one-at-a-time run does, in order."""
    return [step for wave in waves(workflow) for step in wave]


async def run_workflow(
    workflow: Workflow,
    registry: ToolRegistry,
    ctx: ToolContext,
    *,
    provider: Any = None,
    session: Session | None = None,
    confirm: Any = None,
    record: bool = True,
    runs_root: Path | None = None,
    sleep: Any = None,
    now: Any = None,
    inputs: dict[str, str] | None = None,
    resume: Resume | None = None,
) -> AsyncGenerator[RunEvent]:
    """Execute ``workflow``, yielding one event per thing that happens.

    ``confirm`` is an async callable taking the ``RunStarted`` event and the
    workflow, returning False to refuse.

    ``sleep`` and ``now`` exist so a test can drive a twenty-minute CI wait in
    no time at all. Nothing else should pass them.

    The engine writes its own record rather than taking one, because the
    consumer cannot be trusted to write the line that matters most: a run is
    usually cancelled *through* its consumer, so the consumer is the thing
    being torn down at the exact moment the cancellation needs recording. The
    first version took a recorder from the caller and lost every cancelled run
    this way. ``record=False`` is for tests and for a caller that genuinely
    wants no trail.
    """
    problems = fatal_problems(check(workflow, registry))
    if problems:
        raise RunRefused(
            f"{workflow.name} cannot run as written:\n"
            + "\n".join(problem.render() for problem in problems)
        )

    sleep = sleep or asyncio.sleep
    now = now or time.monotonic
    frontiers = waves(workflow)
    steps = [step for wave in frontiers for step in wave]
    state = RunState(run_id=resume.run_id if resume is not None else _new_run_id())
    # Inputs seed the substitution table, so `${inputs.repo}` needs no new
    # mechanism --- it is an output that was known before the run started.
    state.outputs.update(inputs or {})
    if resume is not None:
        # A resumed run inherits what the first leg produced and does not run
        # any of it again: re-running a commit to get back to where the run
        # stopped would be the safety mechanism causing the damage.
        state.outputs.update(resume.outputs)
        state.done |= resume.completed
    began = now()
    # The same record, appended to. What happened is one run with a gap in the
    # middle where it waited for a person; two files would make it two
    # half-runs, neither of which reads like the thing that was done.
    recorder = RunRecorder(state.run_id, runs_root) if record else None

    when = datetime.now(UTC).isoformat(timespec="seconds")
    blast = blast_radius(workflow, registry).level
    started: Any
    if resume is None:
        started = RunStarted(
            run_id=state.run_id,
            workflow=workflow.name,
            started=when,
            steps=[step.id for step in steps],
            blast=blast,
            parallel=workflow.parallel,
            fingerprint=fingerprint(workflow),
            inputs=dict(inputs or {}),
        )
    else:
        started = RunResumed(
            run_id=state.run_id,
            workflow=workflow.name,
            started=when,
            steps=[step.id for step in steps if step.id not in state.done],
            blast=blast,
            parallel=workflow.parallel,
            at=resume.at,
        )

    if confirm is not None and not await confirm(started, workflow):
        raise RunRefused(f"{workflow.name} was not started.")

    _record(recorder, started)
    yield started

    state_name: str = "completed"
    detail = ""
    limit = asyncio.Semaphore(workflow.parallel)
    ctx = _serialised(ctx) if workflow.parallel > 1 else ctx
    position = {step.id: number for number, step in enumerate(steps, 1)}
    queue: asyncio.Queue[Any] = asyncio.Queue()

    def emit(event: Any) -> None:
        """Record first, then hand to the consumer. Same order in both places."""
        _record(recorder, event)
        queue.put_nowait(event)

    async def drive(step: AnyStep, wave: int) -> None:
        """One step, start to finish, from inside its own task."""
        async with limit:
            if state.stopping:
                # A step in this wave already failed in a way that stops the
                # run. Nothing new begins; the skip loop below names this one.
                return
            args = substitute(_subject(step), state.outputs)
            emit(
                StepStarted(
                    step=step.id,
                    kind=step.kind,
                    index=position[step.id],
                    total=len(steps),
                    wave=wave,
                    detail=_detail(step, args),
                )
            )

            clock = now()
            attempts = 0
            while True:
                attempts += 1
                try:
                    ok, summary, output, denied = await _run_step(
                        step, args, registry, ctx, provider=provider, session=session
                    )
                except Parked as parked:
                    # Nobody was there to answer. The step produced nothing and
                    # is not marked done, so a resume runs it again from the
                    # start --- with a person at the gate this time.
                    emit(
                        StepParked(
                            step=step.id,
                            tool=parked.request.tool,
                            action=parked.request.action,
                            path=parked.request.path,
                            target=parked.request.target,
                            sensitivity=parked.request.sensitivity,
                            detail=parked.request.summary,
                        )
                    )
                    if not state.parked_at:
                        state.parked_at = step.id
                    if not state.stopping:
                        state.stopped_at = step.id
                        state.stopped_by = (f"needs approval to {parked.request.action}", False)
                    return
                if step.wait is None or not ok or step.wait.satisfied(output):
                    break
                elapsed = now() - clock
                if elapsed >= step.wait.timeout:
                    ok = False
                    summary = f"gave up after {elapsed:.0f}s and {attempts} attempts"
                    break
                emit(
                    StepWaiting(
                        step=step.id,
                        attempt=attempts,
                        elapsed=round(elapsed, 1),
                        detail=step.wait.describe(),
                    )
                )
                await sleep(min(step.wait.interval, step.wait.timeout - elapsed))

            finished = StepFinished(
                step=step.id,
                ok=ok,
                summary=summary,
                output=output[:MAX_OUTPUT],
                seconds=round(now() - clock, 2),
                attempts=attempts,
                denied=denied,
            )
            emit(finished)

            state.ran += 1
            state.done.add(step.id)
            state.outputs[step.id] = finished.output
            if ok:
                return
            state.failed.add(step.id)
            if step.on_error == "continue":
                return
            if not state.stopping:
                # First failure wins. A second one arriving from a sibling task
                # would otherwise rename the reason the run ended.
                state.stopped_at = step.id
                state.stopped_by = (summary, denied)

    try:
        for number, wave in enumerate(frontiers, 1):
            pending: list[AnyStep] = []
            for step in wave:
                if step.id in state.done:
                    continue  # already run, in the leg this one is resuming
                blocker = state.blocked_by(step)
                if blocker:
                    state.skipped.add(step.id)
                    event = StepSkipped(step=step.id, reason=blocker)
                    _record(recorder, event)
                    yield event
                    continue
                pending.append(step)
            if not pending:
                continue

            async def run_wave(pending: list[AnyStep] = pending, wave: int = number) -> None:
                """The task group is entered and exited by this task and no other.

                That is the whole reason this is a task rather than an `async
                with` around the yield below: a group must be exited by the
                task that entered it, and the generator spends most of a wave
                suspended at a `yield` while its consumer renders.
                """
                try:
                    async with asyncio.TaskGroup() as group:
                        for step in pending:
                            group.create_task(drive(step, wave))
                finally:
                    queue.put_nowait(_DONE)

            driver = asyncio.create_task(run_wave())
            try:
                while True:
                    event = await queue.get()
                    if event is _DONE:
                        break
                    yield event
                # Surfaces a bug in the engine itself. A step's own failure is
                # data, not an exception, so anything arriving here is ours.
                await driver
            finally:
                if not driver.done():
                    driver.cancel()
                    await asyncio.gather(driver, return_exceptions=True)

            if state.stopping:
                break

        if state.stopping:
            summary, denied = state.stopped_by
            state_name = "denied" if denied else "failed"
            if state.parked_at:
                state_name = "parked"
            detail = f"{state.stopped_at}: {summary}"
            # Everything a stopping failure prevented is skipped *by name*.
            # Letting the loop simply end would leave those steps in the record
            # as neither run nor skipped, and a trail that cannot say what did
            # not happen is not a trail.
            for later in steps:
                if later.id in state.done or later.id in state.skipped:
                    continue
                if later.id == state.parked_at:
                    # Already accounted for by its own event, and calling it
                    # skipped would be the record saying it will not happen.
                    continue
                state.skipped.add(later.id)
                halted = StepSkipped(step=later.id, reason=f"the run stopped at {state.stopped_at}")
                _record(recorder, halted)
                yield halted
    except asyncio.CancelledError, GeneratorExit:
        # Both, and the difference is where the cancellation lands. A task
        # cancelled while the engine is awaiting a step gets CancelledError
        # thrown in here; one cancelled while the engine is suspended at a
        # yield --- which is most of the time, because the consumer is doing
        # the rendering --- is closed instead, and closing raises GeneratorExit.
        # Catching only the first left exactly the runs this record exists for
        # with no ending written. Callers should drive this under
        # `contextlib.aclosing` so the close happens at a point they choose.
        interrupted = RunFinished(
            run_id=state.run_id,
            state="cancelled",
            ran=state.ran,
            skipped=len(state.skipped),
            seconds=round(now() - began, 2),
            detail="interrupted",
        )
        _record(recorder, interrupted)
        # Yielding here would be swallowed by the unwinding; the record is what
        # survives a cancelled run, which is the reason it is written eagerly.
        raise

    done = RunFinished(
        run_id=state.run_id,
        state=state_name,  # type: ignore[arg-type]
        ran=state.ran,
        skipped=len(state.skipped),
        seconds=round(now() - began, 2),
        detail=detail,
    )
    _record(recorder, done)
    yield done


# ------------------------------------------------------------------- resuming


def replay(events: list[Any]) -> Resume | None:
    """Rebuild what a parked run had done, from its own record.

    Returns None for a record that is not a parked run --- which includes a run
    that completed, one that failed, and one the process was killed in the
    middle of. Only a run that stopped *at a gate* has a defined place to pick
    up: everything else has an unfinished step whose effect nobody knows.
    """
    ending = _last(events, "run_finished")
    parked = _last(events, "step_parked")
    if ending is None or ending.state != "parked" or parked is None:
        return None
    outputs: dict[str, str] = {}
    completed: set[str] = set()
    for event in events:
        if event.type == "step_finished" and event.ok:
            outputs[event.step] = event.output
            completed.add(event.step)
    start = next((e for e in events if e.type == "run_started"), None)
    if start is not None:
        outputs.update(start.inputs)
    return Resume(run_id=ending.run_id, outputs=outputs, completed=completed, at=parked.step)


def _last(events: list[Any], kind: str) -> Any:
    return next((e for e in reversed(events) if e.type == kind), None)


def resumable(events: list[Any], workflow: Workflow) -> str:
    """Why this run cannot be resumed, or "" when it can.

    The fingerprint is the load-bearing check. The outputs in the record were
    produced by a particular file, and continuing against an edited one would
    be the engine finishing a plan nobody looked at --- so an edit refuses the
    resume and says which file moved, rather than doing its best.
    """
    start = next((e for e in events if e.type == "run_started"), None)
    if start is None:
        return "this record has no beginning, so there is nothing to resume"
    if start.workflow != workflow.name:
        return f"this run was {start.workflow}, not {workflow.name}"
    if not start.fingerprint:
        return (
            "this run was recorded before runs carried a fingerprint, so there "
            "is no way to tell whether the workflow still says what it said "
            "then. Start it again rather than resuming it."
        )
    if start.fingerprint != fingerprint(workflow):
        return (
            f"{workflow.name} has changed since this run started. The steps "
            "already done were planned from a different file, so resuming would "
            "finish a plan nobody approved. Start it again."
        )
    if replay(events) is None:
        return "this run is not parked, so there is nothing waiting for approval"
    return ""


async def resume_workflow(
    run_id: str,
    workflow: Workflow,
    registry: ToolRegistry,
    ctx: ToolContext,
    **kwargs: Any,
) -> AsyncGenerator[RunEvent]:
    """Pick up a parked run, with a person at the gate this time.

    Everything else is ``run_workflow``: the same steps, the same gates, the
    same record --- appended to rather than replaced.
    """
    from contextlib import aclosing

    from altus.workflow.runs import read_run

    runs_root = kwargs.get("runs_root")
    events = read_run(run_id, runs_root)
    problem = resumable(events, workflow)
    if problem:
        raise RunRefused(f"{run_id} cannot be resumed: {problem}")
    resume = replay(events)
    assert resume is not None  # `resumable` just said so

    kwargs.setdefault("record", True)
    async with aclosing(run_workflow(workflow, registry, ctx, resume=resume, **kwargs)) as stream:
        async for event in stream:
            yield event


# ---------------------------------------------------------------- approvals


class _OneAtATime:
    """Approval prompts, strictly one after another.

    Concurrency here is for waiting on other people's APIs, never for asking a
    person two questions at once. ``SessionApprovals`` already serialises the
    interactive path; this is for every other policy a run might be handed, and
    it wraps rather than replaces so a standing grant still works exactly as it
    did.
    """

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        self._lock = asyncio.Lock()

    async def request(self, req: Any) -> Any:
        async with self._lock:
            return await self._delegate.request(req)


def _serialised(ctx: ToolContext) -> ToolContext:
    from dataclasses import replace

    return replace(ctx, approvals=_OneAtATime(ctx.approvals))


# ------------------------------------------------------------------ one step


async def _run_step(
    step: AnyStep,
    subject: Any,
    registry: ToolRegistry,
    ctx: ToolContext,
    *,
    provider: Any,
    session: Session | None,
) -> tuple[bool, str, str, bool]:
    """``(ok, summary, output, denied)``. Never raises for a step's own failure."""
    if isinstance(step, ToolStep):
        outcome = await registry.execute(step.tool, subject, ctx)
        return (
            not outcome.is_error,
            outcome.summary or step.tool,
            outcome.content,
            outcome.denied,
        )

    if isinstance(step, ApprovalStep):
        return await _ask(step, subject, ctx)

    assert isinstance(step, AgentStep)
    return await _think(step, subject, registry, ctx, provider=provider, session=session)


async def _ask(step: ApprovalStep, message: str, ctx: ToolContext) -> tuple[bool, str, str, bool]:
    """An explicit human gate, distinct from the per-call one.

    It carries no diff and no dry run because it previews nothing: it is the
    author saying "somebody look at what just happened before the rest runs",
    and the prompt says exactly that rather than borrowing the shape of a
    prompt that did check something.
    """
    from altus.tools.approval import ApprovalRequest, Decision

    decision = await ctx.approvals.request(
        ApprovalRequest(
            tool="workflow",
            action="continue",
            path=step.id,
            target="this workflow run",
            dry_run=(
                f"{message}\n\nThis is a checkpoint the workflow's author put here. "
                "Nothing was previewed: approving lets the remaining steps run, "
                "each of which still asks for itself."
            ),
            sensitivity=Sensitivity.MUTATE,
        )
    )
    if decision is Decision.DENY:
        return False, "declined", "", True
    return True, "approved", "", False


async def _think(
    step: AgentStep,
    prompt: str,
    registry: ToolRegistry,
    ctx: ToolContext,
    *,
    provider: Any,
    session: Session | None,
) -> tuple[bool, str, str, bool]:
    """One agent step: a fresh conversation, a narrowed tool set, one answer.

    Fresh rather than continuing the user's chat, because a workflow that
    behaved differently depending on what was said to Altus before it ran would
    not be a workflow. The tools are narrowed to what the step named, which is
    the whole reason naming them is worth the author's while.
    """
    from altus.agent import run_agent
    from altus.core.events import StreamError

    if provider is None or session is None:
        return (
            False,
            "no provider",
            "an agent step needs a model to run, and this run was started without one.",
            False,
        )

    # None means every tool; a list means exactly those, and an empty list
    # means none --- which is a step that can only write prose.
    narrowed = (
        registry
        if step.tools is None
        else ToolRegistry([tool for tool in registry if tool.name in set(step.tools)])
    )
    turn = session.model_copy(
        deep=True,
        update={
            "id": f"wf_{step.id}",
            "messages": [],
            "title": None,
            "system": None,
            "tools_enabled": bool(len(narrowed)),
        },
    )
    turn.append(_user(prompt))

    errors: list[str] = []
    async for event in run_agent(
        provider, turn, narrowed, ctx, max_iterations=MAX_AGENT_ITERATIONS
    ):
        if isinstance(event, StreamError):
            errors.append(event.message)

    answer = "\n".join(
        message.text for message in turn.messages if message.role is Role.ASSISTANT and message.text
    ).strip()
    if errors:
        return False, "model error", "\n".join(errors), False
    if not answer:
        return False, "no answer", "the model returned nothing for this step", False
    return True, "answered", answer, False


def _user(text: str) -> Any:
    from altus.core.types import Message

    return Message.user(text)


# ----------------------------------------------------------------- plumbing


def _subject(step: AnyStep) -> Any:
    """The part of a step that references get substituted into."""
    if isinstance(step, ToolStep):
        return dict(step.args)
    if isinstance(step, AgentStep):
        return step.prompt
    return step.message


def _detail(step: AnyStep, subject: Any) -> str:
    if isinstance(step, ToolStep):
        return step.tool
    text = " ".join(str(subject).split())
    return text if len(text) <= 80 else text[:79] + "…"


def _record(recorder: RunRecorder | None, event: Any) -> None:
    if recorder is not None:
        recorder.write(event)


def _new_run_id() -> str:
    from altus.core.types import new_id

    return new_id("r_")
