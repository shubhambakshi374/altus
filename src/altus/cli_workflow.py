"""``altus workflow`` --- running a workflow with no terminal UI attached.

The TUI has driven workflows since the designer shipped; this is the same
engine with nothing rendering it, which is what a trigger, a cron entry and a
CI job all need. It is deliberately a sibling of ``cli.py`` rather than more of
it: the workflow surface is a dozen commands on its own.

Exit codes are the interface here, because the caller is usually a script:

====  ====================================================================
0     the run completed
1     a step failed, or the command could not do what was asked
2     the workflow was refused --- invalid, or declined at the gate
3     the run **parked**: it reached something needing approval and nobody
      was there. Distinct from failure on purpose, because "somebody has to
      look at this" and "this is broken" call for different things.
====  ====================================================================
"""

from __future__ import annotations

import asyncio
import sys
from typing import Annotated, Any

import typer

from altus.core.errors import AltusError
from altus.workflow import (
    Workflow,
    blast_radius,
    check,
    fatal,
    list_workflows,
    load,
    parked_runs,
    read_run,
    render,
    resume_workflow,
    run_workflow,
    summarise,
)

app = typer.Typer(help="Compose, run and resume workflows without the TUI.")

COMPLETED, FAILED, REFUSED, PARKED = 0, 1, 2, 3


# ----------------------------------------------------------------- inspecting


@app.command("list")
def workflow_list() -> None:
    """Every workflow this machine has, and whether it would run."""
    config, registry, _ = _session()
    names = list_workflows(config.workflow)
    if not names:
        typer.echo("no workflows yet. /workflow new <name> in the TUI writes one.")
        return
    for name in names:
        try:
            workflow = load(name, config.workflow)
        except AltusError as exc:
            typer.secho(f"  {name:<20} unreadable: {exc}", fg=typer.colors.RED)
            continue
        blast = blast_radius(workflow, registry)
        state = "" if not fatal(check(workflow, registry)) else "  ✗ will not run"
        triggers = f"  ({len(workflow.triggers)} trigger(s))" if workflow.triggers else ""
        typer.echo(f"  {name:<20} {len(workflow.steps)} steps  {blast.render()}{triggers}{state}")


@app.command("show")
def workflow_show(name: str) -> None:
    """The workflow as written, plus what running it could touch."""
    config, registry, _ = _session()
    workflow = _load(name, config)
    typer.echo(render(workflow).rstrip())
    blast = blast_radius(workflow, registry)
    typer.echo(f"\n{blast.render()}")
    for note in blast.notes():
        typer.secho(f"  — {note}", fg=typer.colors.BRIGHT_BLACK)
    if workflow.parallel > 1:
        typer.echo(f"  — up to {workflow.parallel} steps run at once")
    for number, trigger in enumerate(workflow.triggers, 1):
        typer.echo(f"  — trigger {number}: {trigger.describe()}")


@app.command("validate")
def workflow_validate(name: str) -> None:
    """Every disagreement between the file and this machine."""
    config, registry, _ = _session()
    workflow = _load(name, config)
    problems = check(workflow, registry)
    if not problems:
        typer.secho(f"{name} is runnable.", fg=typer.colors.GREEN)
        return
    for problem in problems:
        colour = typer.colors.RED if problem.fatal else typer.colors.YELLOW
        typer.secho(problem.render(), fg=colour)
    if fatal(problems):
        raise typer.Exit(REFUSED)


# --------------------------------------------------------------------- running


@app.command("run")
def workflow_run(
    name: str,
    values: Annotated[
        list[str] | None,
        typer.Argument(help="Inputs, as key=value. E.g. repo=acme/api"),
    ] = None,
    unattended: Annotated[
        bool,
        typer.Option(
            "--unattended",
            help="Nobody is watching: park at anything needing approval "
            "instead of prompting, and exit 3.",
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Approve everything. Says what it approved."),
    ] = False,
) -> None:
    """Run a workflow."""
    if unattended and yes:
        _fail(
            "--unattended and --yes contradict each other: one waits for a person, the other is one"
        )
    config, registry, ctx = _session(unattended=unattended, yes=yes, prompting=True)
    workflow = _load(name, config)
    from altus.workflow import resolve_inputs

    try:
        inputs = resolve_inputs(workflow, _pairs(values or []))
    except AltusError as exc:
        _fail(str(exc))
        return
    raise typer.Exit(asyncio.run(_drive(run_workflow(workflow, registry, ctx, inputs=inputs))))


@app.command("runs")
def workflow_runs(
    parked: Annotated[
        bool, typer.Option("--parked", help="Only runs waiting for a person.")
    ] = False,
    limit: int = 20,
) -> None:
    """Past runs, newest first."""
    from altus.workflow import list_runs

    if parked:
        waiting = parked_runs(limit=limit)
        if not waiting:
            typer.echo("nothing is waiting for approval.")
            return
        for run_id, line in waiting:
            typer.echo(f"  {run_id}  {line}")
        return
    for run_id in list_runs(limit=limit):
        typer.echo(f"  {run_id}  {summarise(read_run(run_id))}")


@app.command("resume")
def workflow_resume(
    run_id: str,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Approve everything.")] = False,
) -> None:
    """Pick up a parked run, with a person at the gate this time."""
    config, registry, ctx = _session(yes=yes, prompting=True)
    events = read_run(run_id)
    start = next((e for e in events if e.type == "run_started"), None)
    if start is None:
        _fail(f"{run_id} has no beginning, so there is nothing to resume")
        return
    workflow = _load(start.workflow, config)
    raise typer.Exit(asyncio.run(_drive(resume_workflow(run_id, workflow, registry, ctx))))


# -------------------------------------------------------------------- triggers


@app.command("serve")
def workflow_serve(
    once: Annotated[
        bool, typer.Option("--once", help="Check every trigger once and stop.")
    ] = False,
) -> None:
    """Watch every workflow's triggers and start runs when they fire.

    A foreground supervisor, not a daemon. Nothing is installed into launchd or
    systemd --- if you want that, wrap this command, and the decision about
    what runs unattended on this machine stays yours to make explicitly.

    Runs started here are unattended by construction: anything needing approval
    parks, and `altus workflow runs --parked` lists what is waiting.
    """
    config, registry, ctx = _session(unattended=True)
    from altus.workflow.triggers import triggered

    workflows: list[Workflow] = []
    for name in list_workflows(config.workflow):
        try:
            workflows.append(load(name, config.workflow))
        except AltusError as exc:
            typer.secho(f"skipping {name}: {exc}", fg=typer.colors.YELLOW, err=True)
    watched = triggered(workflows)
    if not watched:
        typer.echo("no workflow declares a trigger. Nothing to watch.")
        return
    for workflow in watched:
        for number, trigger in enumerate(workflow.triggers, 1):
            typer.echo(f"  {workflow.name} #{number}: {trigger.describe()}")
    try:
        asyncio.run(_serve(watched, registry, ctx, once=once))
    except KeyboardInterrupt:
        typer.echo("\nstopped.")


async def _serve(workflows: list[Workflow], registry: Any, ctx: Any, *, once: bool) -> None:
    from altus.workflow import resolve_inputs
    from altus.workflow.triggers import Watchpost, next_look, poll

    post = Watchpost.load()
    gap = next_look(workflows)
    typer.secho(f"watching, looking every {gap:g}s. Ctrl-C to stop.", fg=typer.colors.BRIGHT_BLACK)
    while True:
        for fire in await poll(workflows, registry, ctx, post):
            typer.secho(f"→ {fire.render()}", fg=typer.colors.CYAN)
            try:
                inputs = resolve_inputs(fire.workflow, fire.inputs)
            except AltusError as exc:
                typer.secho(f"  not started: {exc}", fg=typer.colors.YELLOW, err=True)
                continue
            code = await _drive(run_workflow(fire.workflow, registry, ctx, inputs=inputs))
            if code == PARKED:
                typer.secho(
                    "  waiting for approval: altus workflow runs --parked",
                    fg=typer.colors.YELLOW,
                )
        if once:
            return
        await asyncio.sleep(gap)


# -------------------------------------------------------------------- plumbing


async def _drive(stream: Any) -> int:
    """Render a run as it happens, and turn how it ended into an exit code."""
    from contextlib import aclosing

    from altus.workflow import RunRefused

    state = "cancelled"
    try:
        async with aclosing(stream) as events:
            async for event in events:
                line, colour = _line(event)
                if line:
                    typer.secho(line, fg=colour)
                if event.type == "run_finished":
                    state = event.state
    except RunRefused as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        return REFUSED
    except KeyboardInterrupt:
        return FAILED
    return {"completed": COMPLETED, "parked": PARKED, "denied": REFUSED}.get(state, FAILED)


def _line(event: Any) -> tuple[str, Any]:
    if event.type in ("run_started", "run_resumed"):
        verb = "running" if event.type == "run_started" else "resuming"
        return f"{verb} {event.workflow}: {len(event.steps)} steps, {event.blast.value}", None
    if event.type == "step_started":
        return f"  → {event.step:<18} {event.detail}", typer.colors.BRIGHT_BLACK
    if event.type == "step_waiting":
        return f"    waiting ({event.attempt}, {event.elapsed:g}s)", typer.colors.BRIGHT_BLACK
    if event.type == "step_finished":
        mark = "✓" if event.ok else "✗"
        colour = typer.colors.GREEN if event.ok else typer.colors.RED
        return f"  {mark} {event.step:<18} {event.summary}", colour
    if event.type == "step_parked":
        return (
            f"  ⏸ {event.step:<18} needs approval to {event.action} {event.path or event.target}",
            typer.colors.YELLOW,
        )
    if event.type == "step_skipped":
        return f"  ~ {event.step:<18} {event.reason}", typer.colors.BRIGHT_BLACK
    if event.type == "run_finished":
        detail = f" — {event.detail}" if event.detail else ""
        return f"{event.state}: {event.ran} ran, {event.skipped} skipped{detail}", None
    return "", None


def _session(
    *, unattended: bool = False, yes: bool = False, prompting: bool = False
) -> tuple[Any, Any, Any]:
    """Config, registry and tool context for a headless run.

    ``unattended`` is the whole parking story in one argument: the policy it
    installs raises rather than answering, so nothing is approved by a process
    that has nobody to ask.

    ``prompting`` marks the commands that can actually run something. The
    inspecting ones never reach a gate, and warning them about stdin would be
    noise on every `workflow list`.
    """
    from altus.agent import build_tool_context, build_workspace
    from altus.config import load_config
    from altus.tools.approval import AllowAll, DenyAll, ParkOnApproval, SessionApprovals
    from altus.tools.registry import default_registry

    config = load_config()
    workspace = build_workspace(config)
    registry = default_registry(
        cloud=config.cloud, mcp_settings=config.mcp, shell=config.tools.shell
    )
    policy: Any
    if unattended:
        policy = ParkOnApproval()
    elif yes:
        policy = AllowAll()
    else:
        policy = _Terminal()
    ctx = build_tool_context(
        config, workspace, approvals=SessionApprovals(policy), registry=registry
    )
    if prompting and not sys.stdin.isatty() and not (unattended or yes):
        # Nothing can be answered, so say which flag was wanted rather than
        # failing closed at the first mutation with no explanation.
        typer.secho(
            "note: stdin is not a terminal, so anything needing approval will be "
            "refused. --unattended parks instead; --yes approves.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        ctx = build_tool_context(
            config, workspace, approvals=SessionApprovals(DenyAll()), registry=registry
        )
    return config, registry, ctx


class _Terminal:
    """The gate, on a terminal. Typed challenges are honoured."""

    async def request(self, req: Any) -> Any:
        from altus.tools.approval import Decision

        typer.echo("")
        typer.secho(f"  {req.summary}", fg=typer.colors.YELLOW, bold=True)
        if req.target:
            typer.echo(f"  target: {req.target}")
        for block in (req.dry_run, req.diff):
            if block:
                typer.echo(_indent(block))
        if req.recoverability:
            typer.echo(f"  undo: {req.recoverability}")
        if req.needs_challenge:
            typed = typer.prompt(f"  type {req.path!r} to confirm", default="", show_default=False)
            return Decision.ALLOW if typed.strip() == req.path else Decision.DENY
        return Decision.ALLOW if typer.confirm("  allow?", default=False) else Decision.DENY


def _indent(text: str, width: int = 2000) -> str:
    return "\n".join(f"  {line}" for line in text[:width].splitlines())


def _pairs(values: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            _fail(f"{value!r} is not an input: they are written key=value")
        key, _, rest = value.partition("=")
        out[key.strip()] = rest
    return out


def _load(name: str, config: Any) -> Workflow:
    try:
        return load(name, config.workflow)
    except AltusError as exc:
        _fail(str(exc))
        raise  # unreachable; _fail exits


def _fail(message: str) -> None:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(FAILED)
