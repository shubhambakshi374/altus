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


class StepStarted(BaseModel):
    type: Literal["step_started"] = "step_started"
    step: str
    kind: str
    index: int
    total: int
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
    state: Literal["completed", "failed", "denied", "cancelled"]
    ran: int = 0
    skipped: int = 0
    seconds: float = 0.0
    detail: str = ""


RunEvent = Annotated[
    RunStarted | StepStarted | StepWaiting | StepFinished | StepSkipped | RunFinished,
    Field(discriminator="type"),
]


def parse_event(payload: dict[str, Any]) -> Any:
    """Rebuild one event from a run record line."""
    from pydantic import TypeAdapter

    return TypeAdapter(RunEvent).validate_python(payload)
