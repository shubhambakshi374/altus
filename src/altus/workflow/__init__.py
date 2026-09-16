"""Workflows: the composable half of the software factory.

Headless on purpose. Everything here runs without a terminal attached, which
is what ``tests/test_layering.py`` has been guarding since Phase 1 so that the
engine --- and anything else that drives a workflow --- never has to import
Textual to do it.
"""

from altus.workflow.blast import Blast, blast_radius, step_level
from altus.workflow.engine import RunRefused, RunState, order, run_workflow
from altus.workflow.events import (
    RunEvent,
    RunFinished,
    RunStarted,
    StepFinished,
    StepSkipped,
    StepStarted,
)
from altus.workflow.inputs import missing as missing_inputs
from altus.workflow.inputs import resolve as resolve_inputs
from altus.workflow.models import (
    AgentStep,
    AnyStep,
    ApprovalStep,
    Input,
    Step,
    ToolStep,
    Wait,
    Workflow,
    describe,
    valid_slug,
)
from altus.workflow.refs import refs_in, substitute
from altus.workflow.runs import RunRecorder, list_runs, read_run, runs_dir, summarise
from altus.workflow.store import (
    copy_template,
    list_workflows,
    load,
    parse,
    path_for,
    render,
    save,
    template,
    template_text,
    templates,
    workflows_dir,
)
from altus.workflow.validate import Problem, check, fatal, runnable

__all__ = [
    "AgentStep",
    "AnyStep",
    "ApprovalStep",
    "Blast",
    "Input",
    "Problem",
    "RunEvent",
    "RunFinished",
    "RunRecorder",
    "RunRefused",
    "RunStarted",
    "RunState",
    "Step",
    "StepFinished",
    "StepSkipped",
    "StepStarted",
    "ToolStep",
    "Wait",
    "Workflow",
    "blast_radius",
    "check",
    "copy_template",
    "describe",
    "fatal",
    "list_runs",
    "list_workflows",
    "load",
    "missing_inputs",
    "order",
    "parse",
    "path_for",
    "read_run",
    "refs_in",
    "render",
    "resolve_inputs",
    "run_workflow",
    "runnable",
    "runs_dir",
    "save",
    "step_level",
    "substitute",
    "summarise",
    "template",
    "template_text",
    "templates",
    "valid_slug",
    "workflows_dir",
]
