"""What a run emits as it happens.

The same shape as ``core/events.py``: a discriminated union, yielded by an
async generator, so a caller can render progress without the engine knowing
anything about a terminal. It is also exactly what gets written to the run
record --- one union serving the live view and the audit trail means the thing
you watched and the thing you can read back afterwards cannot disagree.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from altus.cloud.base import Sensitivity


class RunStarted(BaseModel):
    type: Literal["run_started"] = "run_started"
    run_id: str
    workflow: str
    started: str
    """ISO 8601, UTC."""
    steps: list[str] = Field(default_factory=list)
    """In the order the engine settled on, which is not the file's order."""
    blast: Sensitivity = Sensitivity.READ
    parallel: int = 1
    """How many steps this run was allowed to have in flight at once. In the
    record because "these two happened in this order" and "these two happened
    at the same time" are different facts about what was done, and a reader a
    month later cannot tell them apart from timestamps alone."""
    fingerprint: str = ""
    """A hash of the workflow file as it was when the run started.

    The reason a parked run can be resumed safely: the outputs already in this
    record were produced by a particular file, and continuing against an edited
    one would be the engine finishing a plan nobody looked at. Empty on records
    written before this existed, which are therefore not resumable --- and say
    so rather than resuming against a guess.
    """
    inputs: dict[str, str] = Field(default_factory=dict)
    """The inputs as resolved at the start, so a resume substitutes the same
    values rather than re-reading `@git.origin` in whatever checkout happens to
    be current when somebody gets round to approving it."""


class StepStarted(BaseModel):
    type: Literal["step_started"] = "step_started"
    step: str
    kind: str
    index: int
    total: int
    wave: int = 1
    """Which dependency frontier this step belongs to. Steps sharing a wave
    have no path between them, so with ``parallel`` above 1 they may have been
    running together --- and the record says so rather than implying an order
    that did not exist."""
    detail: str = ""
    """The subject, after substitution: the tool name, the resolved prompt."""


class StepWaiting(BaseModel):
    """One poll that did not satisfy the condition yet.

    Emitted so a run that is waiting twenty minutes for CI reads as waiting
    rather than as hung, and so the record afterwards shows how long it
    actually took rather than only that it eventually worked.
    """

    type: Literal["step_waiting"] = "step_waiting"
    step: str
    attempt: int
    elapsed: float
    detail: str = ""


class RunResumed(BaseModel):
    """A parked run picked up again, appended to the same record.

    The same record rather than a new one: what happened is one run with a gap
    in the middle where it waited for a person, and two files would make it two
    half-runs neither of which reads like the thing that was done.
    """

    type: Literal["run_resumed"] = "run_resumed"
    run_id: str
    workflow: str
    started: str
    steps: list[str] = Field(default_factory=list)
    """What is left to run, not what the run originally had."""
    blast: Sensitivity = Sensitivity.READ
    parallel: int = 1
    at: str = ""
    """The step it parked on, which is the first one to run again."""


class StepParked(BaseModel):
    """A step that stopped at the gate because the run was unattended.

    Carries the question the gate would have asked --- the tool, where it would
    land, how sensitive it is --- so the person who resumes sees what it wanted
    to do rather than "something needed approval".
    """

    type: Literal["step_parked"] = "step_parked"
    step: str
    tool: str = ""
    action: str = ""
    path: str = ""
    """What it would act on. A filesystem tool fills this and leaves `target`
    empty; a cloud tool fills both."""
    target: str = ""
    sensitivity: Sensitivity = Sensitivity.MUTATE
    detail: str = ""


class StepFinished(BaseModel):
    type: Literal["step_finished"] = "step_finished"
    step: str
    ok: bool
    summary: str = ""
    output: str = ""
    seconds: float = 0.0
    attempts: int = 1
    """More than one only for a waiting step."""
    denied: bool = False
    """The user refused it at the gate, as opposed to it failing."""


class StepSkipped(BaseModel):
    type: Literal["step_skipped"] = "step_skipped"
    step: str
    reason: str


class RunFinished(BaseModel):
    type: Literal["run_finished"] = "run_finished"
    run_id: str
    state: Literal["completed", "failed", "denied", "cancelled", "parked"]
    """``parked`` is the one that is not an ending: the run stopped at a gate
    with nobody there, and `altus workflow resume` picks it up."""
    ran: int = 0
    skipped: int = 0
    seconds: float = 0.0
    detail: str = ""


RunEvent = Annotated[
    RunStarted
    | RunResumed
    | StepStarted
    | StepWaiting
    | StepFinished
    | StepSkipped
    | StepParked
    | RunFinished,
    Field(discriminator="type"),
]


def parse_event(payload: dict[str, Any]) -> Any:
    """Rebuild one event from a run record line."""
    from pydantic import TypeAdapter

    return TypeAdapter(RunEvent).validate_python(payload)
