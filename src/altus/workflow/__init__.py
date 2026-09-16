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
from altus.workflow.models import (
    AgentStep,
    AnyStep,
    ApprovalStep,
    Step,
    ToolStep,
    Workflow,
    describe,
    valid_slug,
)
from altus.workflow.refs import refs_in, substitute
from altus.workflow.runs import RunRecorder, list_runs, read_run, runs_dir, summarise
from altus.workflow.store import (
    list_workflows,
    load,
    parse,
    path_for,
    render,
    save,
    workflows_dir,
)
from altus.workflow.validate import Problem, check, fatal, runnable

__all__ = [
    "AgentStep",
    "AnyStep",
    "ApprovalStep",
    "Blast",
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
    "Workflow",
    "blast_radius",
    "check",
    "describe",
    "fatal",
    "list_runs",
    "list_workflows",
    "load",
    "order",
    "parse",
    "path_for",
    "read_run",
    "refs_in",
    "render",
    "run_workflow",
    "runnable",
    "runs_dir",
    "save",
    "step_level",
    "substitute",
    "summarise",
    "valid_slug",
    "workflows_dir",
]
