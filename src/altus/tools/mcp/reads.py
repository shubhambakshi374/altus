"""Reads: what is connected, what it offers, and calling the safe half of it.

``mcp_tools`` is the ``gcp_explain`` of this surface and carries more weight
than its cloud equivalents, because here it is the *only* way the model learns
a tool exists. Nothing about the seven servers is in the system prompt.
"""

from __future__ import annotations

from typing import Any, ClassVar

from altus.cloud.base import Sensitivity
from altus.core.visuals import Table
from altus.mcp.catalog import catalog
from altus.mcp.classify import classify, drift, manifest
from altus.tools.base import ToolContext, ToolOutcome
from altus.tools.mcp.base import MAX_CONTENT, McpTool

READABLE = (Sensitivity.READ, Sensitivity.SENSITIVE_READ)


class McpServersTool(McpTool):
    name: ClassVar[str] = "mcp_servers"
    description: ClassVar[str] = (
        "Which MCP servers this session can reach — GitHub, Jira and Bitbucket "
        "via Atlassian, Grafana, Datadog, New Relic, Snowflake, Databricks — "
        "and what each one covers. Call this before mcp_tools if unsure what is "
        "available."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "check": {
                "type": "boolean",
                "description": (
                    "Connect to each enabled server and compare its live tool list "
                    "against Altus's manifest. Slower, and the only way to see drift."
                ),
            }
        },
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        provider = self.provider(ctx)
        if isinstance(provider, ToolOutcome):
            return provider
        enabled = {spec.id for spec in self.enabled_servers(ctx)}
        rows: list[list[str]] = []
        notes: list[str] = []
        for spec in catalog(self.settings(ctx)):
            state = "enabled" if spec.id in enabled else f"off — {spec.missing_hint}"
            table = manifest(spec.id)
            rows.append(
                [
                    spec.id,
                    ", ".join(spec.products),
                    state,
                    f"{len(table.tools)} ({table.source})",
                ]
            )
            if spec.notes and spec.id in enabled:
                notes.append(f"{spec.id}: {spec.notes}")

        if args.get("check"):
            for server in sorted(enabled):
                try:
                    live = await provider.tools(server)
                except Exception as exc:
                    notes.append(f"{server}: could not connect — {exc}")
                    continue
                found = drift(server, list(live), scope=self.scope_for(server, ctx))
                notes.append(
                    f"{server}: {len(live)} tools live, {len(found)} disagreeing with the manifest"
                )
                notes += [f"  {line}" for line in found[:20]]
                if len(found) > 20:
                    notes.append(f"  … and {len(found) - 20} more")

        visual = Table(
            title="MCP servers",
            columns=["server", "products", "state", "manifest"],
            rows=rows,
            caption="tool counts are Altus's manifest, not the live server — pass check to compare",
        )
        body = [visual.to_text(max_rows=20)]
        body.append(
            "\nResults from these servers reach the model, and are redacted and row-capped "
            "on the way. Anything a query returns leaves this machine."
        )
        if notes:
            body.append("\n" + "\n".join(notes))
        return ToolOutcome(
            content="\n".join(body)[:MAX_CONTENT],
            summary=f"{len(enabled)} of 7 enabled",
            visual=visual,
        )


class McpToolsTool(McpTool):
    name: ClassVar[str] = "mcp_tools"
    description: ClassVar[str] = (
        "Search what the connected MCP servers can do, by keyword. Returns tool "
        "names with their server, description, and whether calling one needs "
        "approval. This is the only way to discover MCP tools — none of them are "
        "in the system prompt."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Keyword to match against tool names and descriptions.",
            },
            "server": {"type": "string", "description": "Restrict to one server id."},
            "limit": {"type": "integer", "description": "Maximum tools to return (default 40)."},
        },
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        provider = self.provider(ctx)
        if isinstance(provider, ToolOutcome):
            return provider
        wanted = str(args.get("server") or "")
        if wanted:
            spec = self.resolve(wanted, ctx)
            if isinstance(spec, ToolOutcome):
                return spec
            specs = [spec]
        else:
            specs = self.enabled_servers(ctx)
        if not specs:
            return ToolOutcome.error(
                "no MCP servers are enabled in this session. See /mcp for what is "
                "missing — usually a credential.",
                summary="none enabled",
            )

        query = str(args.get("query") or "").casefold()
        limit = int(args.get("limit") or 40)
        rows: list[list[str]] = []
        failed: list[str] = []
        for spec in specs:
            try:
                live = await provider.tools(spec.id)
            except Exception as exc:
                failed.append(f"{spec.id}: {exc}")
                continue
            scope = self.scope_for(spec.id, ctx)
            for info in live:
                haystack = f"{info.name} {info.description}".casefold()
                if query and query not in haystack:
                    continue
                level = classify(spec.id, info.name, info, scope=scope)
                rows.append(
                    [
                        spec.id,
                        info.name,
                        level.value,
                        _one_line(info.description),
                    ]
                )

        total = len(rows)
        rows.sort(key=lambda r: (r[0], r[1]))
        visual = Table(
            title=f"MCP tools matching {query!r}" if query else "MCP tools",
            columns=["server", "tool", "sensitivity", "what it does"],
            rows=rows[:limit],
            caption=(
                f"{total} matched; read and sensitive_read run through mcp_call, "
                f"mutate and privileged through mcp_do"
            ),
        )
        body = visual.to_text(max_rows=limit)
        if failed:
            body += "\n\nnot reached: " + "; ".join(failed)
        return ToolOutcome(
            content=body[:MAX_CONTENT],
            summary=f"{total} tools",
            visual=visual,
        )


class McpCallTool(McpTool):
    """Structurally read-only: it refuses anything that is not a read.

    That is what makes ``read_only = True`` honest here, and it is why a
    ``writes=False`` registry can carry this tool and not ``mcp_do`` --- the
    same split ``gcp_call`` and ``gcp_write`` have.
    """

    name: ClassVar[str] = "mcp_call"
    description: ClassVar[str] = (
        "Call a read-only MCP tool — fetch an issue, query metrics, search logs, "
        "list dashboards. Find the tool name with mcp_tools first. Anything that "
        "changes something is refused here; use mcp_do for those."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "server": {"type": "string", "description": "Server id, e.g. github or grafana."},
            "tool": {"type": "string", "description": "Tool name, exactly as mcp_tools gave it."},
            "arguments": {"type": "object", "description": "Arguments for the tool."},
        },
        "required": ["server", "tool"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        provider = self.provider(ctx)
        if isinstance(provider, ToolOutcome):
            return provider
        server = str(args.get("server") or "")
        tool = str(args.get("tool") or "")
        if not server or not tool:
            return ToolOutcome.error("server and tool are both required", summary="bad arguments")
        spec = self.resolve(server, ctx)
        if isinstance(spec, ToolOutcome):
            return spec

        scope = self.scope_for(server, ctx)
        info = None
        try:
            info = await provider.tool(server, tool)
        except Exception as exc:
            return ToolOutcome.error(f"could not reach {server}: {exc}", summary="unreachable")
        if info is None:
            return ToolOutcome.error(
                f"{server} does not offer a tool called {tool!r}. Search with mcp_tools.",
                summary="no such tool",
            )

        level = classify(server, tool, info, scope=scope)
        if level not in READABLE:
            return ToolOutcome.error(
                f"{server}.{tool} is classified {level.value}, so it cannot run through "
                f"mcp_call. Use mcp_do, which asks before acting.",
                summary="not a read",
            )

        try:
            raw = await provider.call(server, tool, dict(args.get("arguments") or {}))
        except Exception as exc:
            return ToolOutcome.error(f"{server}.{tool} failed: {exc}", summary="failed")
        text = self.cap_rows(self.scrub(raw, ctx), ctx)
        return ToolOutcome(content=text[:MAX_CONTENT], summary=f"{server}.{tool}")


def _one_line(text: str, width: int = 70) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"
