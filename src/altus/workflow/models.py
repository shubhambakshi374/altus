"""What a workflow is.

Three step kinds, because three is what Altus can actually do today: call one
of its registered tools, ask the model to work something out, or stop and let a
human decide. Shell execution and parallel fan-out are deliberately absent ---
the roadmap sequences both after the engine, and a shell step needs a sandbox
and an approval design roughly the size of one of the clouds.

Steps carry ``needs`` rather than relying on their order in the file. The
engine will want a DAG, and retrofitting dependency edges later would mean
rewriting every workflow anyone had written in the meantime.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Step ids and workflow names share one shape: lowercase, no separators that
#: mean anything to a filesystem. The conversational door has the *model*
#: choosing both, so a name that can contain ``..`` or ``/`` is a path
#: traversal with an LLM holding the pen.
SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

MAX_NAME = 64


def valid_slug(value: str) -> bool:
    return bool(value) and len(value) <= MAX_NAME and SLUG.match(value) is not None


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    needs: list[str] = Field(default_factory=list)
    on_error: Literal["stop", "continue"] = "stop"
    """What a failure here means for the rest of the run.

    ``stop`` is the default because a workflow is an ordered thing: step 4
    usually assumes step 3 worked, and carrying on past a failure is how a
    half-applied change gets made. ``continue`` is for the steps where it is
    genuinely true that the rest does not depend on them --- a notification, a
    best-effort snapshot.
    """

    @field_validator("id")
    @classmethod
    def _id_is_a_slug(cls, value: str) -> str:
        if not valid_slug(value):
            raise ValueError(
                f"{value!r} is not a usable step id: lowercase letters, digits, "
                "'-' and '_' only, starting with a letter or digit"
            )
        return value


class ToolStep(Step):
    """One registered tool, with its arguments."""

    kind: Literal["tool"] = "tool"
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class AgentStep(Step):
    """The model, given a prompt and optionally a narrowed tool set."""

    kind: Literal["agent"] = "agent"
    prompt: str
    tools: list[str] = Field(default_factory=list)
    """Empty means every tool the session has. Naming tools narrows it."""


class ApprovalStep(Step):
    """Stop. A human decides whether the rest of the workflow runs."""

    kind: Literal["approval"] = "approval"
    message: str = ""


AnyStep = Annotated[ToolStep | AgentStep | ApprovalStep, Field(discriminator="kind")]


class Workflow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    steps: list[AnyStep] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name_is_a_slug(cls, value: str) -> str:
        if not valid_slug(value):
            raise ValueError(
                f"{value!r} is not a usable workflow name: lowercase letters, "
                "digits, '-' and '_' only, starting with a letter or digit"
            )
        return value

    def step(self, step_id: str) -> AnyStep | None:
        return next((s for s in self.steps if s.id == step_id), None)

    @property
    def ids(self) -> list[str]:
        return [s.id for s in self.steps]


def describe(step: AnyStep, width: int = 40) -> str:
    """The one-line subject of a step, for a list or a table."""
    if isinstance(step, ToolStep):
        text = step.tool
    elif isinstance(step, AgentStep):
        text = step.prompt
    else:
        text = step.message or "(no message)"
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"
