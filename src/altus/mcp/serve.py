"""Altus as an MCP server.

The other half of ``session.py``. That module makes Altus a client of nine
vendor servers; this one makes it a server for whatever wants to call it ---
Claude Code, Claude Desktop, another agent --- offering the part of Altus
nobody else has: cloud inventory and topology across four clouds, cost, quotas,
``can-i``, schema lookup, and the workflows built on top of them.

What travels and what does not is decided entirely by ``expose.py``, which is
testable without a transport. This module is the wiring, and it holds three
properties the rest of Altus has held since Phase 2a:

**The same registry, the same gate, the same redaction.** ``on_call_tool``
calls ``registry.execute`` --- the function the TUI, the agent loop, the
workflow engine and the CLI all call. A call arriving over a pipe is not a
second code path with its own idea of what is allowed, because two doors onto
one question is how the same question gets two answers.

**A withheld tool says it was withheld.** Naming something in ``NEVER`` comes
back with the reason, not "unknown tool" --- which would be a lie about a tool
that exists, and would send the caller hunting for a typo.

**Nothing is offered that the person running this has not switched on.**
``[mcp.expose] enabled`` and running ``altus mcp serve`` are two separate
decisions, and writes are a third.

Transport is stdio only. That is how a client spawns a server, and the server
then lives inside the client's own process boundary. An HTTP port would be an
unauthenticated door into four clouds, and authenticating it is the separate
surface that also kept webhooks out of the workflow engine.
"""

from __future__ import annotations

import logging
from typing import Any

from altus.mcp.expose import annotations_for, exposed, why_not

log = logging.getLogger(__name__)

MAX_RESULT = 100_000
"""What one call may hand back. The caller pays for every byte in its own
context window, exactly as Altus pays for a vendor server's answer."""

INSTRUCTIONS = """\
Altus exposes DevOps reads across Kubernetes, AWS, Azure and GCP, plus any
workflows this machine has saved. Results are redacted before they leave: a
secret's value becomes a marker, so a field you can see the name of may not
have a value you can read.

Tools whose names you can see are the only ones there are. Altus deliberately
withholds anything that relays to another vendor's server, runs a command, or
writes a file; calling one by name will tell you why rather than pretending it
does not exist.
"""


def build(registry: Any, ctx: Any, settings: Any, *, config: Any = None) -> Any:
    """The MCP server, wired to this session's registry and context."""
    import mcp.types as types
    from mcp.server.lowlevel import Server

    from altus import __version__

    async def on_list_tools(request: Any, _params: Any = None) -> Any:
        return types.ListToolsResult(
            tools=_tools(registry, settings, config, getattr(request, "session", None))
        )

    async def on_call_tool(request: Any, params: Any) -> Any:
        return await _call(request, params, registry, ctx, settings, config)

    async def on_list_resources(_ctx: Any, _params: Any = None) -> Any:
        return types.ListResourcesResult(resources=_resources(config))

    async def on_read_resource(_ctx: Any, params: Any) -> Any:
        return _read_resource(str(params.uri), config)

    return Server(
        name="altus",
        version=__version__,
        instructions=INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        on_list_resources=on_list_resources,
        on_read_resource=on_read_resource,
    )


async def serve_stdio(registry: Any, ctx: Any, settings: Any, *, config: Any = None) -> None:
    """Run until the client closes the pipe."""
    from mcp.server.stdio import stdio_server

    server = build(registry, ctx, settings, config=config)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


# --------------------------------------------------------------------- tools


def _tools(registry: Any, settings: Any, config: Any, session: Any = None) -> list[Any]:
    """What this client is offered.

    A client that cannot elicit is not shown the mutating tools at all. They
    would be refused on the way in anyway, and a tool listed but never callable
    is a capability that fails at the moment somebody trusted it --- the same
    argument that keeps an unrunnable workflow off the list.
    """
    import mcp.types as types

    asks = session is None or can_elicit(session)
    found = [
        types.Tool(
            name=tool.name,
            description=tool.description,
            input_schema=tool.input_schema,
            annotations=_hints(tool),
        )
        for tool in exposed(registry, settings)
        if tool.read_only or asks
    ]
    found += _workflow_tools(registry, settings, config, asks=asks)
    return found


async def _call(
    request: Any, params: Any, registry: Any, ctx: Any, settings: Any, config: Any
) -> Any:
    name = str(getattr(params, "name", ""))
    args = dict(getattr(params, "arguments", None) or {})

    if name.startswith(WORKFLOW_PREFIX):
        return await _run_workflow_tool(request, name, args, registry, ctx, settings, config)

    refusal = why_not(name, settings, registry)
    if refusal:
        # Said out loud rather than hidden. A caller that knows a tool exists
        # and is being withheld can ask the operator for it; one told "unknown
        # tool" goes looking for a typo that is not there. This comes first
        # because it is the more specific answer: "Altus never offers this"
        # is more use than "nobody is here to approve it".
        return _text(f"{name} is not offered by this server: {refusal}", error=True)

    tool = registry.get(name)
    if (
        tool is not None
        and not tool.read_only
        and not can_elicit(getattr(request, "session", None))
    ):
        # Checked before the tool is entered, so the refusal can say why nobody
        # was asked rather than looking like a denial somebody made.
        return _text(f"{name} changes things, and {NO_HUMAN}", error=True)

    ctx = _with_gate(ctx, request, settings)
    outcome = await registry.execute(name, args, ctx)
    # `visual` deliberately does not travel: it is built for a terminal, and a
    # chart re-rendered as a wall of text is worse than the text alone.
    return _text(outcome.content[:MAX_RESULT] or outcome.summary, error=outcome.is_error)


def _hints(tool: Any) -> Any:
    """`annotations_for` keys the protocol's way; the SDK models are snake_case
    and serialise back to camelCase. Written out rather than splatted, so the
    correspondence is visible at the one place the two spellings meet."""
    import mcp.types as types

    hints = annotations_for(tool)
    return types.ToolAnnotations(
        read_only_hint=hints["readOnlyHint"],
        destructive_hint=hints["destructiveHint"],
        open_world_hint=hints["openWorldHint"],
    )


def _text(text: str, *, error: bool = False) -> Any:
    import mcp.types as types

    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=error)


# ----------------------------------------------------------------- resources


def _resources(config: Any) -> list[Any]:
    """Run records and workflow sources, so a client that started something can
    read what happened without spending a tool call on it."""
    import mcp.types as types

    from altus.workflow import list_runs, list_workflows

    found = [
        types.Resource(
            uri=f"altus://workflows/{name}",
            name=name,
            description="a workflow as written",
            mime_type="text/plain",
        )
        for name in list_workflows(getattr(config, "workflow", None))
    ]
    found += [
        types.Resource(
            uri=f"altus://runs/{run_id}",
            name=run_id,
            description="one run, event by event",
            mime_type="application/jsonl",
        )
        for run_id in list_runs(limit=20)
    ]
    return found


def _read_resource(uri: str, config: Any) -> Any:
    import mcp.types as types

    from altus.core.errors import AltusError
    from altus.workflow import path_for, read_run, runs_dir

    def contents(text: str, mime: str) -> Any:
        return types.ReadResourceResult(
            contents=[types.TextResourceContents(uri=uri, mime_type=mime, text=text)]
        )

    rest = uri.removeprefix("altus://")
    kind, _, name = rest.partition("/")
    try:
        if kind == "workflows":
            return contents(
                path_for(name, getattr(config, "workflow", None)).read_text(encoding="utf-8"),
                "text/plain",
            )
        if kind == "runs":
            read_run(name)  # refuses a traversal and an unreadable id
            return contents(
                (runs_dir() / f"{name}.jsonl").read_text(encoding="utf-8"), "application/jsonl"
            )
    except (AltusError, OSError) as exc:
        return contents(f"{uri} could not be read: {exc}", "text/plain")
    return contents(f"{uri} is not something this server serves", "text/plain")


# --------------------------------------------------------------- the gate


NO_HUMAN = (
    "this client did not declare the elicitation capability, so there is "
    "nobody here to ask. Altus will not approve a change on a human's behalf, "
    "and it will not assume the client asked one --- that is a claim about "
    "software it cannot inspect."
)


def can_elicit(session: Any) -> bool:
    """Did the client say it can put a question in front of a person?

    Asked before a mutating tool is entered rather than after it fails, so the
    refusal can say *why* nobody was asked instead of looking like a denial
    somebody made.
    """
    import mcp.types as types

    try:
        return bool(
            session.check_client_capability(
                types.ClientCapabilities(
                    elicitation=types.ElicitationCapability(form=types.FormElicitationCapability())
                )
            )
        )
    except Exception:
        # A client that cannot be asked about its capabilities is a client that
        # cannot be asked anything.
        return False


class ElicitApprovals:
    """The approval gate, one hop further away.

    An ``ApprovalPolicy`` like ``DenyAll``, ``AllowAll`` and ``ParkOnApproval``,
    except that the human it asks is sitting in front of somebody else's client.
    The question carries the same four things the TUI modal shows --- what,
    where, what was previewed, and what it costs to undo --- including the
    sentence admitting when nothing was previewed at all.

    There is no ``ALLOW_ALWAYS`` here. A standing grant is scoped to a session
    with a person in it, and this session has a person only for as long as each
    question is on their screen.
    """

    def __init__(self, session: Any, request_id: Any = None) -> None:
        self.session = session
        self.request_id = request_id

    async def request(self, req: Any) -> Any:
        from altus.tools.approval import Decision

        if not can_elicit(self.session):
            return Decision.DENY
        try:
            answer = await self.session.elicit_form(
                message=self.describe(req),
                requested_schema=self.schema(req),
                related_request_id=self.request_id,
            )
        except Exception as exc:
            log.warning("elicitation failed, refusing: %s", exc)
            return Decision.DENY
        if getattr(answer, "action", "") != "accept":
            return Decision.DENY
        content = dict(getattr(answer, "content", None) or {})
        if req.needs_challenge:
            return (
                Decision.ALLOW
                if str(content.get("confirm", "")).strip() == req.path
                else Decision.DENY
            )
        return Decision.ALLOW if content.get("approve") is True else Decision.DENY

    def describe(self, req: Any) -> str:
        """The same prompt the modal shows, as prose."""
        lines = [req.summary]
        if req.target:
            lines.append(f"target: {req.target}")
        for block in (req.dry_run, req.diff):
            if block:
                lines.append(str(block)[:4000])
        if req.recoverability:
            lines.append(f"undo: {req.recoverability}")
        if req.protected:
            lines.append("This target is protected by [cloud.protected].")
        return "\n\n".join(lines)

    def schema(self, req: Any) -> dict[str, Any]:
        """Yes/no, or the typed challenge --- which does not get easier for
        having arrived over a socket."""
        if req.needs_challenge:
            return {
                "type": "object",
                "properties": {
                    "confirm": {
                        "type": "string",
                        "title": f"Type {req.path} exactly to allow this",
                        "description": f"This is {req.sensitivity.value}. Type {req.path}.",
                    }
                },
                "required": ["confirm"],
            }
        return {
            "type": "object",
            "properties": {
                "approve": {"type": "boolean", "title": "Allow this?", "default": False}
            },
            "required": ["approve"],
        }


def _with_gate(ctx: Any, request: Any, settings: Any) -> Any:
    """Point this call's approvals at the client's human."""
    from dataclasses import replace

    return replace(
        ctx,
        approvals=ElicitApprovals(
            getattr(request, "session", None), getattr(request, "request_id", None)
        ),
    )


# ----------------------------------------------------------------- workflows

WORKFLOW_PREFIX = "workflow_"


def _workflows(settings: Any, config: Any, registry: Any) -> list[tuple[Any, Any]]:
    """``(workflow, blast)`` for every saved workflow this server would offer.

    A workflow that cannot run here is not offered: advertising one whose tools
    this machine does not have is a capability that fails at the moment
    somebody trusted it.
    """
    from altus.core.errors import AltusError
    from altus.workflow import blast_radius, list_workflows, load, runnable

    if not getattr(settings, "workflows", False):
        return []
    found: list[tuple[Any, Any]] = []
    for name in list_workflows(getattr(config, "workflow", None)):
        try:
            workflow = load(name, getattr(config, "workflow", None))
        except AltusError as exc:
            log.warning("not offering workflow %s: %s", name, exc)
            continue
        if not runnable(workflow, registry):
            continue
        blast = blast_radius(workflow, registry)
        found.append((workflow, blast))
    return found


def _workflow_tools(registry: Any, settings: Any, config: Any, *, asks: bool = True) -> list[Any]:
    """One tool per saved workflow: a named capability rather than a string the
    caller has to guess."""
    import mcp.types as types

    from altus.cloud.base import Sensitivity

    tools: list[Any] = []
    for workflow, blast in _workflows(settings, config, registry):
        reads = blast.level is Sensitivity.READ
        if not reads and not (getattr(settings, "allow_writes", False) and asks):
            continue
        tools.append(
            types.Tool(
                name=f"{WORKFLOW_PREFIX}{workflow.name}",
                description=(
                    f"{workflow.description or workflow.name}. "
                    f"{len(workflow.steps)} steps; {blast.render()}. "
                    "Each step that changes anything asks first."
                ),
                input_schema=_workflow_schema(workflow),
                annotations=types.ToolAnnotations(
                    read_only_hint=reads,
                    destructive_hint=not reads,
                    open_world_hint=True,
                ),
            )
        )
    return tools


def _workflow_schema(workflow: Any) -> dict[str, Any]:
    properties = {
        name: {"type": "string", "description": spec.description or name}
        for name, spec in workflow.inputs.items()
    }
    required = [
        name for name, spec in workflow.inputs.items() if spec.required and not spec.default
    ]
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


async def _run_workflow_tool(
    request: Any,
    name: str,
    args: dict[str, Any],
    registry: Any,
    ctx: Any,
    settings: Any,
    config: Any,
) -> Any:
    """Run a saved workflow, with both gates pointing at the client's human."""
    from altus.core.errors import AltusError
    from altus.workflow import RunRefused, resolve_inputs, run_workflow

    wanted = name.removeprefix(WORKFLOW_PREFIX)
    offered = {w.name: (w, b) for w, b in _workflows(settings, config, registry)}
    if wanted not in offered:
        return _text(f"{name} is not offered by this server", error=True)
    workflow, blast = offered[wanted]

    from altus.cloud.base import Sensitivity

    if blast.level is not Sensitivity.READ:
        if not getattr(settings, "allow_writes", False):
            return _text(
                f"{name} changes things, and [mcp.expose] allow_writes is false",
                error=True,
            )
        if not can_elicit(getattr(request, "session", None)):
            return _text(f"{name} cannot run here: {NO_HUMAN}", error=True)

    try:
        inputs = resolve_inputs(workflow, {k: str(v) for k, v in args.items()})
    except AltusError as exc:
        return _text(str(exc), error=True)

    gated = _with_gate(ctx, request, settings)
    lines: list[str] = []
    try:
        async for event in run_workflow(
            workflow,
            registry,
            gated,
            confirm=_plan_gate(request, blast),
            inputs=inputs,
        ):
            line = _event_line(event)
            if line:
                lines.append(line)
    except RunRefused as refused:
        return _text(str(refused), error=True)
    text = "\n".join(lines)[:MAX_RESULT]
    failed = any(line.startswith("failed") or line.startswith("denied") for line in lines)
    return _text(text, error=failed)


def _plan_gate(request: Any, blast: Any) -> Any:
    """The run-level question: the whole plan, once, before anything starts.

    Unchanged from the TUI in everything but where the person is standing ---
    agreeing to six actions individually, in sequence, with no sight of the
    whole is how consent gets worn down.
    """

    async def confirm(started: Any, workflow: Any) -> bool:
        from altus.workflow import render

        session = getattr(request, "session", None)
        if session is None or not can_elicit(session):
            # A read-only workflow needs no run gate; anything else was already
            # refused before reaching here.
            return bool(blast.level.value == "read")
        answer = await session.elicit_form(
            message=(
                f"Run {workflow.name}? {len(started.steps)} steps, {blast.render()}.\n\n"
                f"{render(workflow)[:4000]}\n\n"
                "Each step that changes anything will still ask for itself."
            ),
            requested_schema={
                "type": "object",
                "properties": {
                    "approve": {"type": "boolean", "title": "Start this run?", "default": False}
                },
                "required": ["approve"],
            },
            related_request_id=getattr(request, "request_id", None),
        )
        return getattr(answer, "action", "") == "accept" and bool(
            dict(getattr(answer, "content", None) or {}).get("approve")
        )

    return confirm


def _event_line(event: Any) -> str:
    match event.type:
        case "step_started":
            return f"  -> {event.step}: {event.detail}"
        case "step_waiting":
            return f"     waiting ({event.attempt}, {event.elapsed:g}s)"
        case "step_finished":
            return f"  {'ok' if event.ok else 'FAILED'} {event.step}: {event.summary}"
        case "step_skipped":
            return f"  ~ {event.step}: {event.reason}"
        case "step_parked":
            return f"  paused {event.step}: needs approval to {event.action}"
        case "run_finished":
            return f"{event.state}: {event.ran} ran, {event.skipped} skipped. {event.detail}"
        case _:
            return ""
