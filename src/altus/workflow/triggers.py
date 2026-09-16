"""What starts a run when nobody asks for one.

Two kinds, declared in the workflow file itself so the thing that starts a run
and the run it starts are one artefact somebody can read in one sitting. A
``schedule`` fires on an interval. A ``watch`` polls a read and fires when the
answer *changes*, seeding that answer into a declared input so the run can act
on what changed rather than going to look a second time.

Three decisions that are easy to get wrong and expensive to get wrong:

**First sight is not a change.** A watch that has never looked before records
what it sees and fires nothing, and a schedule that has never fired records the
time and waits out its interval. Otherwise starting the supervisor --- or
restarting it after a deploy, or a crash --- is a way to run everything at
once, which turns an operational hiccup into a fleet of unattended runs.

**The state is on disk, keyed by workflow and position.** Restarting must not
re-fire what already fired, so "when did this last go off" has to outlive the
process that noticed.

**Nothing here approves anything.** A trigger decides *when*; the gate still
decides *whether*, and a run started this way carries ``ParkOnApproval``, so
anything needing a person stops and waits for one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from altus.config.loader import data_dir
from altus.workflow.models import Trigger, Workflow

log = logging.getLogger(__name__)

MAX_SEEN = 4096
"""Longest output a watch will hash. The hash is over the whole answer, and a
tool that returns a megabyte every fifteen minutes should not be paid for in
memory as well as in API calls."""


def state_path() -> Path:
    return data_dir() / "triggers.json"


@dataclass
class Watchpost:
    """When each trigger last fired, and what it last saw.

    Held as one small JSON file rather than one per trigger: it is read and
    written whole every poll, and a directory of fragments would make a partly
    written state indistinguishable from a trigger nobody has configured yet.
    """

    path: Path = field(default_factory=state_path)
    data: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> Watchpost:
        where = path or state_path()
        try:
            return cls(path=where, data=dict(json.loads(where.read_text(encoding="utf-8"))))
        except OSError, ValueError:
            # A missing file is the normal first run. An unreadable one is
            # treated the same way on purpose: the worst it costs is one
            # suppressed first-sight, and the alternative is a supervisor that
            # will not start because of a file it wrote itself.
            return cls(path=where)

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        except OSError as exc:
            log.warning("trigger state could not be written to %s: %s", self.path, exc)

    def entry(self, workflow: str, index: int) -> dict[str, Any]:
        return self.data.setdefault(f"{workflow}#{index}", {})


@dataclass(frozen=True)
class Fire:
    """One run a trigger wants started."""

    workflow: Workflow
    trigger: int
    why: str
    inputs: dict[str, str] = field(default_factory=dict)
    """Keyed by input *name*, not ``inputs.<name>``: these go through
    ``inputs.resolve`` with everything else, so a watch cannot smuggle in a
    value for something the workflow never declared."""

    def render(self) -> str:
        return f"{self.workflow.name}: {self.why}"


async def poll(
    workflows: list[Workflow],
    registry: Any,
    ctx: Any,
    post: Watchpost,
    *,
    now: Any = None,
) -> list[Fire]:
    """One look at every trigger. Returns what should run, and saves state."""
    clock = now or time.time
    found: list[Fire] = []
    for workflow in workflows:
        for index, trigger in enumerate(workflow.triggers, 1):
            try:
                fire = await _check(workflow, trigger, index, registry, ctx, post, clock)
            except Exception as exc:
                # One bad trigger must not stop the rest of them being looked at.
                log.warning("trigger %d of %s could not be checked: %s", index, workflow.name, exc)
                continue
            if fire is not None:
                found.append(fire)
    post.save()
    return found


async def _check(
    workflow: Workflow,
    trigger: Trigger,
    index: int,
    registry: Any,
    ctx: Any,
    post: Watchpost,
    clock: Any,
) -> Fire | None:
    entry = post.entry(workflow.name, index)
    when = clock()
    last = entry.get("at")
    if last is None:
        # Never looked. Record the moment and wait out the interval, so that
        # starting the supervisor is not itself an event.
        entry["at"] = when
        return None
    if when - float(last) < trigger.seconds:
        return None
    entry["at"] = when

    if trigger.kind == "schedule":
        return Fire(workflow=workflow, trigger=index, why=f"scheduled: {trigger.describe()}")

    outcome = await registry.execute(trigger.tool, dict(trigger.args), ctx)
    if outcome.is_error:
        log.warning("%s: %s could not be read: %s", workflow.name, trigger.tool, outcome.summary)
        return None
    answer = outcome.content[:MAX_SEEN]
    digest = hashlib.sha256(answer.encode("utf-8")).hexdigest()[:16]
    seen = entry.get("seen")
    entry["seen"] = digest
    if seen is None or seen == digest:
        # First sight, or the same answer as last time. Neither is a change,
        # and a watch fires on change.
        return None
    return Fire(
        workflow=workflow,
        trigger=index,
        why=f"{trigger.tool} answered differently",
        inputs={trigger.into: answer},
    )


def triggered(workflows: list[Workflow]) -> list[Workflow]:
    return [workflow for workflow in workflows if workflow.triggers]


def next_look(workflows: list[Workflow], floor: float = 30.0) -> float:
    """How long to sleep between polls: the shortest interval anything asks for.

    Polling faster than the keenest trigger would burn API calls to learn
    nothing; polling slower would make `every = "30s"` a lie.
    """
    intervals = [t.seconds for w in workflows for t in w.triggers]
    return max(floor, min(intervals)) if intervals else floor
