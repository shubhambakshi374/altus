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

    async def on_list_tools(_ctx: Any, _params: Any = None) -> Any:
        return types.ListToolsResult(tools=_tools(registry, settings, config))

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


def _tools(registry: Any, settings: Any, config: Any) -> list[Any]:
    import mcp.types as types

    found = [
        types.Tool(
            name=tool.name,
            description=tool.description,
            input_schema=tool.input_schema,
            annotations=_hints(tool),
        )
        for tool in exposed(registry, settings)
    ]
    found += _workflow_tools(registry, settings, config)
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
        # tool" goes looking for a typo that is not there.
        return _text(f"{name} is not offered by this server: {refusal}", error=True)

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


# ----------------------------------------------------------------- workflows

WORKFLOW_PREFIX = "workflow_"


def _workflow_tools(registry: Any, settings: Any, config: Any) -> list[Any]:
    """One tool per saved workflow. Filled in by stage 3."""
    return []


async def _run_workflow_tool(
    request: Any,
    name: str,
    args: dict[str, Any],
    registry: Any,
    ctx: Any,
    settings: Any,
    config: Any,
) -> Any:
    return _text(f"{name} is not offered by this server", error=True)


def _with_gate(ctx: Any, request: Any, settings: Any) -> Any:
    """Filled in by stage 3: the approval gate, travelling to the client's human."""
    return ctx
