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

Steps run one at a time in dependency order. ``needs`` describes a DAG and the
engine could run independent branches concurrently; it does not, because a
sequential run is the one whose record reads like what happened.
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
from altus.tools.base import ToolContext
from altus.tools.registry import ToolRegistry
from altus.workflow.blast import blast_radius
from altus.workflow.events import (
    RunEvent,
    RunFinished,
    RunStarted,
    StepFinished,
    StepSkipped,
    StepStarted,
    StepWaiting,
)
from altus.workflow.models import AgentStep, AnyStep, ApprovalStep, ToolStep, Workflow
from altus.workflow.refs import substitute
from altus.workflow.runs import RunRecorder
from altus.workflow.validate import check
from altus.workflow.validate import fatal as fatal_problems

MAX_OUTPUT = 40_000
"""What one step may hand on. A step's output becomes another step's argument
and, for an agent step, part of a prompt somebody pays for."""

MAX_AGENT_ITERATIONS = 15


class RunRefused(Exception):
    """The workflow was not started. Carries the reason, already phrased."""


@dataclass
class RunState:
    """What has happened so far. The engine's only memory."""

    run_id: str
    outputs: dict[str, str] = field(default_factory=dict)
    failed: set[str] = field(default_factory=set)
    skipped: set[str] = field(default_factory=set)
    ran: int = 0

    def blocked_by(self, step: AnyStep) -> str:
        """Which dependency stops this step, if any."""
        for need in step.needs:
            if need in self.failed:
                return f"{need} failed"
            if need in self.skipped:
                return f"{need} was skipped"
        return ""


def order(workflow: Workflow) -> list[AnyStep]:
    """Dependency order, ties broken by the file's order.

    Deterministic on purpose: two runs of the same workflow must produce the
    same record, or comparing two runs tells you nothing.
    """
    remaining = list(workflow.steps)
    done: set[str] = set()
    ordered: list[AnyStep] = []
    while remaining:
        ready = [
            s
            for s in remaining
            if all(n in done or n not in {x.id for x in remaining} for n in s.needs)
        ]
        if not ready:
            # A cycle. `check` refuses to run these, so reaching here means a
            # caller skipped validation; degrade to file order rather than spin.
            ordered.extend(remaining)
            return ordered
        for step in ready:
            ordered.append(step)
            done.add(step.id)
            remaining.remove(step)
    return ordered


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
    steps = order(workflow)
    state = RunState(run_id=_new_run_id())
    # Inputs seed the substitution table, so `${inputs.repo}` needs no new
    # mechanism --- it is an output that was known before the run started.
    state.outputs.update(inputs or {})
    began = now()
    recorder = RunRecorder(state.run_id, runs_root) if record else None

    started = RunStarted(
        run_id=state.run_id,
        workflow=workflow.name,
        started=datetime.now(UTC).isoformat(timespec="seconds"),
        steps=[step.id for step in steps],
        blast=blast_radius(workflow, registry).level,
    )

    if confirm is not None and not await confirm(started, workflow):
        raise RunRefused(f"{workflow.name} was not started.")

    _record(recorder, started)
    yield started

    state_name: str = "completed"
    detail = ""
    try:
        for index, step in enumerate(steps, 1):
            blocker = state.blocked_by(step)
            if blocker:
                state.skipped.add(step.id)
                event = StepSkipped(step=step.id, reason=blocker)
                _record(recorder, event)
                yield event
                continue

            args = substitute(_subject(step), state.outputs)
            begin = StepStarted(
                step=step.id,
                kind=step.kind,
                index=index,
                total=len(steps),
                detail=_detail(step, args),
            )
            _record(recorder, begin)
            yield begin

            clock = now()
            attempts = 0
            while True:
                attempts += 1
                ok, summary, output, denied = await _run_step(
                    step, args, registry, ctx, provider=provider, session=session
                )
                if step.wait is None or not ok or step.wait.satisfied(output):
                    break
                elapsed = now() - clock
                if elapsed >= step.wait.timeout:
                    ok = False
                    summary = f"gave up after {elapsed:.0f}s and {attempts} attempts"
                    break
                waiting = StepWaiting(
                    step=step.id,
                    attempt=attempts,
                    elapsed=round(elapsed, 1),
                    detail=step.wait.describe(),
                )
                _record(recorder, waiting)
                yield waiting
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
            _record(recorder, finished)
            yield finished

            state.ran += 1
            state.outputs[step.id] = finished.output
            if ok:
                continue
            state.failed.add(step.id)
            if step.on_error == "continue":
                continue
            state_name = "denied" if denied else "failed"
            detail = f"{step.id}: {summary}"
            # Everything after a stopping failure is skipped *by name*. Letting
            # the loop fall out here instead would leave those steps in the
            # record as neither run nor skipped, and a trail that cannot say
            # what did not happen is not a trail.
            for later in steps[index:]:
                state.skipped.add(later.id)
                halted = StepSkipped(step=later.id, reason=f"the run stopped at {step.id}")
                _record(recorder, halted)
                yield halted
            break
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
