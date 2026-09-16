"""Is this workflow runnable? Answered before any engine exists.

Nothing here executes a step. It reads the file the way the engine eventually
will and reports every disagreement it finds, each one naming the step it came
from --- "invalid workflow" tells an author nothing they can act on.

The fatal/warning split is the load-bearing part. A tool that exists nowhere is
a typo and the workflow can never run. A tool from an extra this machine has
not installed is a different fact entirely: the workflow is fine, and it is
this laptop that is missing ``uv sync --extra gcp``. Collapsing the two would
make a workflow unvalidatable anywhere except on the machine that wrote it,
which defeats the point of a file you can check into a repo.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from altus.workflow.models import AgentStep, ApprovalStep, ToolStep, Workflow
from altus.workflow.refs import ancestors, refs_in

#: A tool's prefix names the integration that ships it. Used only to tell a
#: typo from an extra that is not installed.
PREFIXES = ("k8s", "aws", "azure", "gcp", "mcp")


@dataclass(frozen=True)
class Problem:
    message: str
    step_id: str = ""
    fatal: bool = True

    def render(self) -> str:
        mark = "✗" if self.fatal else "!"
        where = f"{self.step_id}: " if self.step_id else ""
        return f"  {mark} {where}{self.message}"


def check(workflow: Workflow, registry: Any) -> list[Problem]:
    """Every problem, fatal ones first within each step's own order."""
    found: list[Problem] = []
    if not workflow.steps:
        found.append(Problem("a workflow with no steps has nothing to run"))
        return found

    found += _duplicate_ids(workflow)
    found += _dangling_needs(workflow)
    cycles = _cycles(workflow)
    found += cycles
    found += _missing_tools(workflow, registry)
    # Reference checking walks `needs`, so it is meaningless on a graph that
    # does not terminate. Skipping it keeps a cycle from also producing a pile
    # of confusing reference errors that vanish once the cycle is fixed.
    if not cycles:
        found += _bad_references(workflow)
    found += _bad_waits(workflow, registry)
    return found


def fatal(problems: list[Problem]) -> list[Problem]:
    return [problem for problem in problems if problem.fatal]


def runnable(workflow: Workflow, registry: Any) -> bool:
    return not fatal(check(workflow, registry))


# ------------------------------------------------------------------ structure


def _duplicate_ids(workflow: Workflow) -> list[Problem]:
    seen: set[str] = set()
    found: list[Problem] = []
    for step in workflow.steps:
        if step.id in seen:
            found.append(Problem("this id is used by an earlier step too", step.id))
        seen.add(step.id)
    return found


def _dangling_needs(workflow: Workflow) -> list[Problem]:
    ids = set(workflow.ids)
    found: list[Problem] = []
    for step in workflow.steps:
        for need in step.needs:
            if need == step.id:
                found.append(Problem("a step cannot depend on itself", step.id))
            elif need not in ids:
                found.append(Problem(f"needs {need!r}, which is not a step here", step.id))
    return found


def _cycles(workflow: Workflow) -> list[Problem]:
    """Kahn's algorithm; whatever never drains is in a cycle.

    The cycle is then walked so the message can name the loop in order. A
    report that says only "there is a cycle" leaves the author to find it by
    hand in a file they already believed was acyclic.
    """
    ids = set(workflow.ids)
    edges = {
        step.id: [n for n in step.needs if n in ids and n != step.id] for step in workflow.steps
    }
    pending = dict(edges)
    while True:
        ready = [node for node, needs in pending.items() if not needs]
        if not ready:
            break
        for node in ready:
            pending.pop(node)
        for needs in pending.values():
            needs[:] = [need for need in needs if need not in ready]
    if not pending:
        return []
    loop = _walk(pending)
    return [Problem(f"circular dependency: {' → '.join(loop)}", loop[0])]


def _walk(pending: dict[str, list[str]]) -> list[str]:
    """One concrete cycle among the nodes that never drained."""
    start = sorted(pending)[0]
    path = [start]
    seen = {start}
    node = start
    while True:
        following = [n for n in pending[node] if n in pending]
        if not following:
            break
        node = following[0]
        if node in seen:
            path = path[path.index(node) :] if node in path else path
            break
        path.append(node)
        seen.add(node)
    return [*path, path[0]]


# ---------------------------------------------------------------------- tools


def _missing_tools(workflow: Workflow, registry: Any) -> list[Problem]:
    found: list[Problem] = []
    for step in workflow.steps:
        if isinstance(step, ToolStep):
            names = [(step.tool, True)]
        elif isinstance(step, AgentStep):
            # A named-but-absent tool does not stop an agent step running; it
            # just runs with less than its author intended. Hence not fatal.
            names = [(name, False) for name in step.tools]
        else:
            continue
        for name, required in names:
            problem = _tool_problem(name, step.id, registry, required=required)
            if problem is not None:
                found.append(problem)
    return found


def _tool_problem(name: str, step_id: str, registry: Any, *, required: bool) -> Problem | None:
    if name in registry:
        return None
    prefix = name.split("_", 1)[0]
    if prefix in PREFIXES:
        from altus.cloud.base import integration

        entry = integration(prefix)
        if entry is not None and not entry.available:
            return Problem(
                f"{name!r} comes from an integration this machine does not have "
                f"({entry.install_hint}). The workflow itself is fine.",
                step_id,
                fatal=False,
            )
    return Problem(
        f"no tool called {name!r} is registered in this session",
        step_id,
        fatal=required,
    )


# ----------------------------------------------------------------- references


def _bad_references(workflow: Workflow) -> list[Problem]:
    """``${x}`` is only legitimate when ``x`` is guaranteed to have run.

    Ordering in the file guarantees nothing --- the engine runs the graph, not
    the list --- so a reference to a step that is merely *earlier* would work
    until somebody reordered two independent steps. Requiring the dependency
    makes the data flow and the execution order the same statement.
    """
    needs = {step.id: list(step.needs) for step in workflow.steps}
    ids = set(needs)
    found: list[Problem] = []
    for step in workflow.steps:
        available = ancestors(step.id, needs)
        for name in sorted(_referenced(step)):
            if name == step.id:
                found.append(Problem(f"${{{name}}} refers to this step's own output", step.id))
            elif name not in ids:
                found.append(Problem(f"${{{name}}} names a step that does not exist", step.id))
            elif name not in available:
                found.append(
                    Problem(
                        f"${{{name}}} is not guaranteed to have run — add {name!r} to needs",
                        step.id,
                    )
                )
    return found


def _referenced(step: AgentStep | ApprovalStep | ToolStep) -> set[str]:
    if isinstance(step, ToolStep):
        return refs_in(step.args)
    if isinstance(step, AgentStep):
        return refs_in(step.prompt)
    return refs_in(step.message)


# ---------------------------------------------------------------------- waits


def _bad_waits(workflow: Workflow, registry: Any) -> list[Problem]:
    """A step that polls must be a read.

    Waiting means calling the same thing over and over until something is
    true of the answer. Nothing about "check until it is done" implies anybody
    wanted the call repeated, and on a mutation that is what it would do ---
    so this is refused rather than warned about. Checkable before the run
    because `sensitivity_of` answers from the tool, not from the arguments.
    """
    from altus.tools.base import sensitivity_of

    found: list[Problem] = []
    for step in workflow.steps:
        if step.wait is None:
            continue
        if not isinstance(step, ToolStep):
            found.append(
                Problem(
                    f"only a tool step can wait; this one is {_article(step.kind)} {step.kind} step",
                    step.id,
                )
            )
            continue
        tool = registry.get(step.tool) if registry is not None else None
        if tool is None:
            continue  # already reported as a missing tool
        level = sensitivity_of(tool)
        if level.needs_approval:
            found.append(
                Problem(
                    f"{step.tool} is {level.value}, and a waiting step is called "
                    "repeatedly --- poll a read instead",
                    step.id,
                )
            )
    return found


def _article(word: str) -> str:
    return "an" if word[:1] in "aeiou" else "a"
