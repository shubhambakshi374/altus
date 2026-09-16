"""Shared plumbing for the MCP tools.

Four tools, no matter how many servers are connected. Seven servers' worth of
schemas is well over two hundred entries --- more context than everything else
Altus registers put together --- so the inventory is searched through a tool
rather than pushed into the prompt, the way ``gcp_explain`` and ``gcp_call``
already split introspection from invocation.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from altus.cloud.base import ProtectionMode, Sensitivity
from altus.cloud.redact import redact, redact_text
from altus.mcp.catalog import CATALOG, ServerSpec, server_spec
from altus.mcp.classify import classify, manifest, target_for, why_unknown
from altus.tools.base import BaseTool, ToolContext, ToolOutcome

MAX_CONTENT = 24_000


class McpTool(BaseTool):
    """Resolve a provider, and refuse clearly when there is none."""

    read_only: ClassVar[bool] = True

    def provider(self, ctx: ToolContext) -> Any | ToolOutcome:
        found = getattr(ctx.cloud, "mcp", None)
        if found is None:
            return ToolOutcome.error(
                "no MCP layer is configured. Install the extra with "
                "`uv sync --extra mcp`, then see /mcp.",
                summary="unavailable",
            )
        return found

    def settings(self, ctx: ToolContext) -> Any:
        return getattr(ctx.cloud, "mcp_settings", None)

    def enabled_servers(self, ctx: ToolContext) -> list[ServerSpec]:
        """Which servers this session offers at all.

        An explicit ``servers`` list wins; otherwise it is whichever have
        credentials --- the user's "if present". A server switched off here is
        never listed and never callable, so the model is not told it exists.
        """
        settings = self.settings(ctx)
        chosen = list(getattr(settings, "servers", ()) or ())
        out: list[ServerSpec] = []
        for spec in CATALOG:
            per = settings.for_server(spec.id) if settings is not None else None
            if per is not None and per.enabled is False:
                continue
            if chosen:
                if spec.id in chosen:
                    out.append(spec)
            elif (per is not None and per.enabled is True) or spec.available():
                out.append(spec)
        return out

    def resolve(self, server: str, ctx: ToolContext) -> ServerSpec | ToolOutcome:
        spec = server_spec(server)
        if spec is None:
            known = ", ".join(s.id for s in CATALOG)
            return ToolOutcome.error(
                f"{server!r} is not a server Altus ships. Available: {known}",
                summary="unknown server",
            )
        if spec not in self.enabled_servers(ctx):
            return ToolOutcome.error(
                f"{server} is not enabled in this session --- {spec.missing_hint}. See /mcp.",
                summary="not enabled",
            )
        return spec

    def scope_for(self, server: str, ctx: ToolContext) -> str:
        provider = getattr(ctx.cloud, "mcp", None)
        return str(getattr(provider, "scopes", {}).get(server, "")) if provider else ""

    def scrub(self, text: str, ctx: ToolContext) -> str:
        """Redaction is the primary control here, not defence in depth.

        A tool result goes verbatim to whichever LLM provider is active, and
        these servers return issue bodies, log lines and warehouse rows --- the
        places credentials actually get pasted.

        Both paths are needed. ``redact`` walks a parsed structure and is what
        catches the ``{"name": "API_TOKEN", "value": ...}`` shape that MCP
        results are full of; it does nothing at all to a string, so a JSON
        result is parsed first and anything else falls through to
        ``redact_text``. Running only the structured pass over text --- which
        is what the first version of this did --- reads as working and redacts
        nothing.
        """
        if not ctx.cloud.redact_secrets:
            return text
        if text.lstrip()[:1] in "{[":
            try:
                parsed = json.loads(text)
            except ValueError:
                pass
            else:
                return json.dumps(redact(parsed), indent=2, default=str)
        return redact_text(text)

    def cap_rows(self, text: str, ctx: ToolContext) -> str:
        """Truncate a result to ``max_rows`` lines.

        Snowflake and Databricks answer questions with rows rather than
        metadata, so this is the difference between a query and an exfiltration.
        """
        settings = self.settings(ctx)
        limit = int(getattr(settings, "max_rows", 200) or 200)
        lines = text.splitlines()
        if len(lines) <= limit:
            return text
        kept = "\n".join(lines[:limit])
        return f"{kept}\n\n[truncated: showing {limit} of {len(lines)} rows]"


class McpMutatingTool(McpTool):
    """Resolve, preflight, classify, ask, act. Nothing is sent before the
    approval returns."""

    read_only: ClassVar[bool] = False
    action: ClassVar[str] = "call"

    def writes_allowed(self, ctx: ToolContext) -> str:
        """``[mcp] allow_writes`` cannot work by withholding a tool --- the same
        ``mcp_do`` comments on an issue and deletes a repository --- so it is
        checked here, as its cloud namesakes are."""
        settings = self.settings(ctx)
        if settings is not None and not getattr(settings, "allow_writes", True):
            return "writes through MCP are disabled ([mcp] allow_writes = false)"
        return ""

    async def preflight(self, provider: Any, server: str, tool: str, args: dict[str, Any]) -> str:
        """What could be checked before asking. On this surface, very little.

        MCP has no dry-run: there is no ``validateOnly``, no What-If, nothing
        to ask a server to simulate. The only honest preflight is the current
        state, where the manifest names a read that fetches it. Every branch
        here says "no preview exists", because none of them is a preview.
        """
        unknown = why_unknown(server, tool, self._scope(provider, server))
        if unknown:
            return (
                f"no preview exists: MCP has no dry-run. This tool is not in Altus's "
                f"manifest for {server}, so it is treated as privileged --- {unknown}. "
                f"What it changes is not known in advance."
            )
        before = manifest(server).before.get(tool)
        if before:
            try:
                current = await provider.call(server, before, self._before_args(args))
            except Exception as exc:
                return (
                    f"no preview exists: MCP has no dry-run, and the current state "
                    f"could not be read either ({before} failed: {exc})."
                )
            return (
                f"no preview exists: MCP has no dry-run. Current state, via {before}:\n"
                f"{current[:2000]}"
            )
        info = await self._info(provider, server, tool)
        if info is not None and info.destructive_hint:
            return (
                "no preview exists: MCP has no dry-run, and the server declares this destructive."
            )
        return (
            "no preview exists: MCP has no dry-run, and this server declares nothing "
            "about this tool."
        )

    @staticmethod
    def _scope(provider: Any, server: str) -> str:
        return str(getattr(provider, "scopes", {}).get(server, ""))

    @staticmethod
    def _before_args(args: dict[str, Any]) -> dict[str, Any]:
        """Only the identifying arguments. A read given a write's body would
        either fail validation or, worse, mean something else entirely."""
        drop = {"body", "content", "text", "fields", "update", "data", "patch", "sql", "query"}
        return {k: v for k, v in args.items() if k not in drop}

    @staticmethod
    async def _info(provider: Any, server: str, tool: str) -> Any:
        try:
            return await provider.tool(server, tool)
        except Exception:
            # Failing to reach the server here must not look like a verdict.
            return None

    async def confirm(
        self,
        ctx: ToolContext,
        *,
        server: str,
        tool: str,
        args: dict[str, Any],
        sensitivity: Sensitivity,
        dry_run: str,
    ) -> ToolOutcome | None:
        """Ask. Returns an outcome when refused, None when cleared."""
        from altus.tools.approval import ApprovalRequest, Decision

        target = target_for(server, args, scope=self.scope_for(server, ctx))
        rules = ctx.cloud.protection
        protected = bool(rules and rules.matches(target))
        if protected and rules.mode is ProtectionMode.DENY:
            return ToolOutcome.rejected(
                f"{target.render()} is protected with mode = deny, so this was refused "
                f"without asking."
            )
        decision = await ctx.approvals.request(
            ApprovalRequest(
                tool=self.name,
                action=self.action,
                path=f"{server}.{tool}",
                target=target.render(),
                diff=_summarise(args),
                dry_run=dry_run,
                recoverability=_recoverability(server, tool),
                destructive=sensitivity is Sensitivity.PRIVILEGED,
                protected=protected,
                sensitivity=sensitivity,
            )
        )
        if decision is Decision.DENY:
            return ToolOutcome.rejected(f"{server}.{tool} was not approved")
        return None


def _summarise(args: dict[str, Any]) -> str:
    """The arguments, as a human would skim them."""
    if not args:
        return "(no arguments)"
    lines = []
    for key, value in args.items():
        text = str(value)
        lines.append(f"{key}: {text if len(text) <= 300 else text[:300] + ' …'}")
    return "\n".join(lines)


def _recoverability(server: str, tool: str) -> str:
    """Whether this could be undone --- the key fact for a delete."""
    level = classify(server, tool)
    if level is Sensitivity.PRIVILEGED:
        return "may not be reversible; MCP offers no undo and no preview"
    return "reversible only by whatever the product itself offers"
