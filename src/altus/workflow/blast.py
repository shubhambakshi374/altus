"""How much damage a workflow could do, and how much of that is actually known.

Every surface in Altus is held to the same rule: say which check ran, and never
imply one that did not. A designer is the first place that rule applies to an
action *nobody has taken yet*, which makes the honest half harder and more
important. Three things keep a printed level from buying false confidence:

* ``aws_write`` reaches 19,189 operations and ``k8s_apply`` writes both a
  ConfigMap and a ClusterRoleBinding, so for those the arguments decide and
  anything computed now is a floor;
* an ``agent`` step that may use every tool is, by construction, as dangerous
  as the most dangerous tool in the session --- and which ones it reaches is
  the model's choice at run time, not the author's;
* a step naming a tool this machine has never heard of has no level at all.

So ``Blast`` reports a level *and* the step ids behind each of those, and
``certain`` is false whenever any of them is non-empty.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from altus.cloud.base import Sensitivity
from altus.tools.base import dispatches, sensitivity_of
from altus.workflow.models import AgentStep, AnyStep, ApprovalStep, ToolStep, Workflow

_RANK: dict[Sensitivity, int] = {
    Sensitivity.READ: 0,
    Sensitivity.SENSITIVE_READ: 1,
    Sensitivity.MUTATE: 2,
    Sensitivity.PRIVILEGED: 3,
}


def strictest(levels: list[Sensitivity]) -> Sensitivity:
    return max(levels, key=lambda level: _RANK[level], default=Sensitivity.READ)


@dataclass(frozen=True)
class Blast:
    """A workflow's blast radius, with its own uncertainty attached."""

    level: Sensitivity
    unresolved: tuple[str, ...] = ()
    """Steps whose arguments could make them stricter than the level shown."""
    open_ended: tuple[str, ...] = ()
    """Agent steps free to reach tools their author did not name."""
    unknown: tuple[str, ...] = ()
    """Steps naming a tool this session does not have."""

    @property
    def certain(self) -> bool:
        return not (self.unresolved or self.open_ended or self.unknown)

    def notes(self) -> list[str]:
        """Why the level above is a floor. Empty when it is not."""
        lines: list[str] = []
        if self.unresolved:
            lines.append(
                _one_or_many(
                    self.unresolved,
                    "{names} dispatches — what it actually does is decided by its "
                    "arguments, so this could be stricter when it runs",
                    "{names} dispatch — what they actually do is decided by their "
                    "arguments, so this could be stricter when they run",
                )
            )
        if self.open_ended:
            lines.append(
                _one_or_many(
                    self.open_ended,
                    "{names} may use any tool in the session — name tools on the "
                    "step to narrow that",
                    "{names} may use any tool in the session — name tools on those "
                    "steps to narrow that",
                )
            )
        if self.unknown:
            lines.append(
                _one_or_many(
                    self.unknown,
                    "{names} names a tool this session does not have, so it "
                    "contributes nothing to the level above",
                    "{names} name a tool this session does not have, so they "
                    "contribute nothing to the level above",
                )
            )
        return lines

    def render(self) -> str:
        head = f"blast radius: {self.level.value}"
        return head if self.certain else f"{head} (at least)"


def _one_or_many(ids: tuple[str, ...], singular: str, plural: str) -> str:
    """Agreement matters here. These notes are the honest half of a level
    somebody is about to act on, and prose that reads as sloppy gets skimmed."""
    template = singular if len(ids) == 1 else plural
    return template.format(names=_steps(ids))


def _steps(ids: tuple[str, ...]) -> str:
    listed = ", ".join(ids)
    return f"step {listed}" if len(ids) == 1 else f"steps {listed}"


def step_level(step: AnyStep, registry: Any) -> tuple[Sensitivity, str]:
    """One step's level, and which caveat it earns ("", "dispatch", "open", "unknown")."""
    if isinstance(step, ApprovalStep):
        # Stopping to ask changes nothing by itself.
        return Sensitivity.READ, ""

    if isinstance(step, ToolStep):
        tool = registry.get(step.tool)
        if tool is None:
            return Sensitivity.READ, "unknown"
        return sensitivity_of(tool), ("dispatch" if dispatches(tool) else "")

    assert isinstance(step, AgentStep)
    if step.tools:
        named = [registry.get(name) for name in step.tools]
        known = [tool for tool in named if tool is not None]
        if not known:
            return Sensitivity.READ, "unknown"
        # A narrowed agent step is still the model choosing among what it was
        # given, so it reaches the strictest of them --- but only those.
        return strictest([sensitivity_of(tool) for tool in known]), "dispatch"
    return strictest([sensitivity_of(tool) for tool in registry]), "open"


def blast_radius(workflow: Workflow, registry: Any) -> Blast:
    levels: list[Sensitivity] = []
    buckets: dict[str, list[str]] = {"dispatch": [], "open": [], "unknown": []}
    for step in workflow.steps:
        level, caveat = step_level(step, registry)
        if caveat != "unknown":
            levels.append(level)
        if caveat:
            buckets[caveat].append(step.id)
    return Blast(
        level=strictest(levels),
        unresolved=tuple(buckets["dispatch"]),
        open_ended=tuple(buckets["open"]),
        unknown=tuple(buckets["unknown"]),
    )
