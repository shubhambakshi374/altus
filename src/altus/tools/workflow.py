"""Letting the model write a workflow, behind the same gate as any other write.

This is the one tool in Altus that writes outside the workspace. Every other
write is sandboxed by ``workspace.py``; this one lands in the config directory
by design, because that is where workflows live.

It is also the one write whose *contents* are the dangerous part. A workflow
file is a queued set of actions against real infrastructure, so approving one
is approving all of them --- which is why the request carries the workflow's
rolled-up blast radius as its own sensitivity. A drafted workflow containing a
privileged step demands the typed challenge, exactly as calling that step
directly would.

Saving is not running. Nothing here executes a step, and the prompt says so.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from altus.cloud.base import Sensitivity
from altus.tools.approval import ApprovalRequest, Decision
from altus.tools.base import BaseTool, ToolContext, ToolOutcome
from altus.workflow import Blast, Problem, blast_radius, check, path_for, render, save
from altus.workflow.models import Workflow

STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["id", "kind"],
    "properties": {
        "id": {
            "type": "string",
            "description": "Lowercase letters, digits, '-' and '_'. Unique in the workflow.",
        },
        "kind": {"type": "string", "enum": ["tool", "agent", "approval"]},
        "needs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Ids of steps that must finish first. Omit for the first step.",
        },
        "tool": {"type": "string", "description": "kind=tool: a registered tool name."},
        "args": {"type": "object", "description": "kind=tool: arguments for that tool."},
        "prompt": {"type": "string", "description": "kind=agent: what to work out."},
        "tools": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "kind=agent: the tools it may use. Omitting this lets it use every "
                "tool in the session, which makes the step as dangerous as the "
                "worst one — name them wherever you can."
            ),
        },
        "message": {"type": "string", "description": "kind=approval: what the human is deciding."},
    },
}


class WorkflowSaveTool(BaseTool):
    name: ClassVar[str] = "workflow_save"
    description: ClassVar[str] = (
        "Write a workflow the user has been describing to a file they can run "
        "later. Always asks the user first, showing the whole file and its "
        "blast radius. Saving does not run anything."
    )
    read_only: ClassVar[bool] = False
    dispatches: ClassVar[bool] = True
    """The steps decide: a workflow of reads and one of RBAC writes are the
    same call to this tool."""

    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Lowercase slug; also the filename."},
            "description": {"type": "string"},
            "steps": {"type": "array", "items": STEP_SCHEMA},
        },
        "required": ["name", "steps"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        settings = getattr(ctx, "workflow_settings", None)
        if settings is not None and not getattr(settings, "allow_model_authoring", True):
            return ToolOutcome.error(
                "[workflow] allow_model_authoring is false, so workflows are "
                "written by hand here. Describe the steps and the user can add "
                "them with /workflow.",
                summary="authoring off",
            )

        try:
            workflow = Workflow.model_validate(
                {
                    "name": args.get("name") or "",
                    "description": args.get("description") or "",
                    "steps": args.get("steps") or [],
                }
            )
        except Exception as exc:
            return ToolOutcome.error(f"that is not a usable workflow: {exc}", summary="invalid")

        registry = getattr(ctx, "registry", None)
        problems = check(workflow, registry) if registry is not None else []
        broken = [problem for problem in problems if problem.fatal]
        if broken:
            # Refused rather than saved-with-warnings: an unrunnable workflow
            # that sits on disk looking finished is worse than one that was
            # never written, and the model can fix these without the user.
            return ToolOutcome.error(
                "this workflow cannot run as written:\n"
                + "\n".join(problem.render() for problem in broken),
                summary="not runnable",
            )

        try:
            path = path_for(workflow.name, settings)
        except Exception as exc:
            return ToolOutcome.error(str(exc), summary="bad name")
        replacing = path.exists()

        radius = blast_radius(workflow, registry) if registry is not None else None
        level = radius.level if radius is not None else Sensitivity.MUTATE

        decision = await ctx.approvals.request(
            ApprovalRequest(
                tool=self.name,
                action="overwrite" if replacing else "save",
                path=f"{workflow.name}.toml",
                target=_target(radius),
                diff=render(workflow),
                dry_run=_explain(radius, problems, path),
                recoverability=(
                    "the workflow it replaces is not kept"
                    if replacing
                    else "nothing is run; delete the file to undo"
                ),
                destructive=replacing,
                sensitivity=level,
            )
        )
        if decision is Decision.DENY:
            return ToolOutcome.rejected(f"the user declined to save {workflow.name}")

        try:
            written = save(workflow, settings)
        except Exception as exc:
            return ToolOutcome.error(f"could not write the workflow: {exc}", summary="failed")
        if registry is not None:
            # Drafting is over. Leaving this registered would mean one /workflow
            # new widened the tool list for the rest of the session.
            registry.remove(self.name)
        return ToolOutcome(
            content=(
                f"saved {workflow.name} ({len(workflow.steps)} steps) to {written}. "
                "Nothing has run: /workflow opens it, and the engine that runs it "
                "is not built yet."
            ),
            summary=workflow.name,
        )


def _target(radius: Blast | None) -> str:
    """The blast radius is this write's target: it is what approving buys."""
    return radius.render() if radius is not None else "workflow file"


def _explain(radius: Blast | None, problems: list[Problem], path: Path) -> str:
    """What was checked, in the words of what actually ran."""
    lines = [
        "This writes a file. It does not run anything, now or on approval.",
        f"Destination: {path}",
    ]
    if radius is not None:
        lines += [f"  {note}" for note in radius.notes()]
    warnings = [problem for problem in problems if not problem.fatal]
    if warnings:
        lines.append("Validation warnings:")
        lines += [problem.render() for problem in warnings]
    elif radius is not None:
        lines.append("Validation: every step resolves against this session's tools.")
    return "\n".join(lines)
