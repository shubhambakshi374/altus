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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Step ids and workflow names share one shape: lowercase, no separators that
#: mean anything to a filesystem. The conversational door has the *model*
#: choosing both, so a name that can contain ``..`` or ``/`` is a path
#: traversal with an LLM holding the pen.
SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

MAX_NAME = 64


def valid_slug(value: str) -> bool:
    return bool(value) and len(value) <= MAX_NAME and SLUG.match(value) is not None


class Wait(BaseModel):
    """Run this step again until something is true of its output.

    Deliberately not an expression. Two forms, no operators, no comparisons,
    no negation: ``until`` names a JSON key that must be present and non-empty,
    or ``contains`` is a literal substring of the output. The moment this grows
    ``!=`` it is an expression language, and a workflow whose shape depends on
    run-time values can no longer have its blast radius worked out before it
    runs --- which is the property the last three increments were built to keep.

    ``until`` searches the whole output, so it belongs on a call that returns
    one thing. Polling ``actions_list`` for ``conclusion`` would be satisfied
    by *any* finished run in the list; polling ``actions_get`` for a single run
    id is the question that was actually meant.
    """

    model_config = ConfigDict(extra="forbid")

    until: str = ""
    """A JSON key that must appear with a non-empty value."""
    contains: str = ""
    """A literal substring that must appear in the output."""
    interval: float = 30.0
    timeout: float = 1800.0

    @model_validator(mode="after")
    def _exactly_one_condition(self) -> Wait:
        if bool(self.until) == bool(self.contains):
            raise ValueError("a wait needs exactly one of 'until' or 'contains'")
        if self.interval <= 0 or self.timeout <= 0:
            raise ValueError("interval and timeout must both be positive")
        return self

    def satisfied(self, output: str) -> bool:
        if self.contains:
            return self.contains in output
        return _has_value(_parsed(output), self.until)

    def describe(self) -> str:
        what = f"{self.until!r} appears" if self.until else f"{self.contains!r} appears"
        return f"waiting until {what}, every {self.interval:g}s, giving up after {self.timeout:g}s"


def _parsed(output: str) -> Any:
    import json

    try:
        return json.loads(output)
    except ValueError:
        # Not JSON. A key cannot be found in prose, and guessing at one with a
        # regex would make `until` mean something different depending on what
        # the server happened to return.
        return None


def _has_value(payload: Any, key: str) -> bool:
    """Is ``key`` anywhere in here with a non-empty value?

    Recursive because the interesting field is usually nested --- a workflow
    run's ``conclusion`` sits inside the run object, not at the top.
    """
    if isinstance(payload, dict):
        for name, value in payload.items():
            if name == key and value not in (None, "", [], {}):
                return True
            if _has_value(value, key):
                return True
        return False
    if isinstance(payload, list):
        return any(_has_value(item, key) for item in payload)
    return False


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    needs: list[str] = Field(default_factory=list)
    wait: Wait | None = None
    """Poll this step until its output satisfies a condition. Reads only ---
    ``validate`` refuses it on anything that changes something, because
    polling a mutation means calling it repeatedly and nothing about "check
    until it is done" implies anybody wanted that."""
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


class Input(BaseModel):
    """One value a workflow is run against.

    Without these a workflow is one file per repository, which is not a
    workflow. Available to every step as ``${inputs.<name>}`` with no ``needs``
    entry, because an input is known before the first step rather than
    produced by one.
    """

    model_config = ConfigDict(extra="forbid")

    description: str = ""
    default: str = ""
    """A literal, or ``@git.origin`` for the checkout's own remote."""
    required: bool = False


class Workflow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    inputs: dict[str, Input] = Field(default_factory=dict)
    steps: list[AnyStep] = Field(default_factory=list)

    @field_validator("inputs")
    @classmethod
    def _input_names_are_slugs(cls, value: dict[str, Input]) -> dict[str, Input]:
        bad = sorted(name for name in value if not valid_slug(name))
        if bad:
            raise ValueError(f"input names must be slugs: {', '.join(bad)}")
        return value

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
        text = f"{step.tool} (waits)" if step.wait is not None else step.tool
    elif isinstance(step, AgentStep):
        text = step.prompt
    else:
        text = step.message or "(no message)"
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"
