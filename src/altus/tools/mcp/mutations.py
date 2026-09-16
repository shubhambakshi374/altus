"""The one tool that changes something, and the weakest preflight of the five.

Kubernetes has ``dryRun=All``, Azure has a property-level What-If diff, AWS has
``DryRun`` on 4.3% of operations and GCP ``validateOnly`` on 1.9%. MCP has
nothing: no preview mechanism exists in the protocol at all. So every prompt
this tool raises says "no preview exists", and the only substance it can offer
is the current state, fetched through the read the manifest names.

Saying that plainly matters more here than anywhere else. A prompt that looked
like the other four would imply a check that cannot happen.
"""

from __future__ import annotations

from typing import Any, ClassVar

from altus.mcp.classify import classify
from altus.tools.base import ToolContext, ToolOutcome
from altus.tools.mcp.base import MAX_CONTENT, McpMutatingTool
from altus.tools.mcp.reads import READABLE


class McpDoTool(McpMutatingTool):
    name: ClassVar[str] = "mcp_do"
    description: ClassVar[str] = (
        "Call an MCP tool that changes something — comment on an issue, open a "
        "pull request, update a dashboard, transition a ticket. Always asks "
        "first. MCP has no dry-run, so the prompt shows the current state where "
        "one can be read and says so plainly when it cannot."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "server": {"type": "string", "description": "Server id, e.g. github or atlassian."},
            "tool": {"type": "string", "description": "Tool name, exactly as mcp_tools gave it."},
            "arguments": {"type": "object", "description": "Arguments for the tool."},
        },
        "required": ["server", "tool"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        provider = self.provider(ctx)
        if isinstance(provider, ToolOutcome):
            return provider
        blocked = self.writes_allowed(ctx)
        if blocked:
            return ToolOutcome.error(blocked, summary="writes disabled")

        server = str(args.get("server") or "")
        tool = str(args.get("tool") or "")
        if not server or not tool:
            return ToolOutcome.error("server and tool are both required", summary="bad arguments")
        spec = self.resolve(server, ctx)
        if isinstance(spec, ToolOutcome):
            return spec

        call_args = dict(args.get("arguments") or {})
        scope = self.scope_for(server, ctx)
        info = await self._info(provider, server, tool)
        level = classify(server, tool, info, scope=scope)
        if level in READABLE:
            return ToolOutcome.error(
                f"{server}.{tool} is a read, so it belongs in mcp_call --- which needs "
                f"no approval and costs the user nothing to answer.",
                summary="use mcp_call",
            )

        # Everything above this line is local. The only thing that reaches the
        # server before the approval returns is the manifest's own before-read.
        dry_run = await self.preflight(provider, server, tool, call_args)
        refusal = await self.confirm(
            ctx,
            server=server,
            tool=tool,
            args=call_args,
            sensitivity=level,
            dry_run=dry_run,
        )
        if refusal is not None:
            return refusal

        try:
            raw = await provider.call(server, tool, call_args)
        except Exception as exc:
            return ToolOutcome.error(f"{server}.{tool} failed: {exc}", summary="failed")
        text = self.cap_rows(self.scrub(raw, ctx), ctx)
        return ToolOutcome(content=text[:MAX_CONTENT] or "done", summary=f"{server}.{tool}")
