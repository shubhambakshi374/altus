"""Reads that need no approval: identity, introspection, and any read method.

``gcp_explain`` is the cheapest of the four clouds' introspection tools,
because Google ships its contracts on disk --- 600 discovery documents inside
the client library. No network call, no catalog to fetch, and the same
documents the classifier parses.
"""

from __future__ import annotations

from typing import Any, ClassVar

from altus.cloud import gcp as api
from altus.cloud.base import Sensitivity
from altus.core.visuals import Table
from altus.tools.base import ToolContext, ToolOutcome
from altus.tools.gcp.base import GcpTool

MAX_CONTENT = 24_000


def _as_yaml(payload: Any) -> str:
    import yaml

    return str(
        yaml.safe_dump(payload, default_flow_style=False, sort_keys=False, allow_unicode=True)
    )


class GcpWhoamiTool(GcpTool):
    name: ClassVar[str] = "gcp_whoami"
    description: ClassVar[str] = (
        "Which Google Cloud project and identity this session is using. Call "
        "this before anything else if unsure where you are — every other GCP "
        "tool acts in exactly one project, and this names it."
    )
    input_schema: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        provider = await self.provider(ctx)
        if isinstance(provider, ToolOutcome):
            return provider
        identity = await self.identity(ctx, provider)
        if isinstance(identity, ToolOutcome):
            return identity

        project = self.project_for(args, ctx, provider)
        rules = ctx.cloud.protection
        protected = bool(project) and bool(rules and rules.matches(api.target_for(project)))
        lines = [
            f"project   {project or '(none selected — call gcp_projects)'}",
            f"account   {identity['account'] or '(not named by these credentials)'}",
            f"source    {identity['source']}",
        ]
        if protected:
            lines.append("⚠ PROTECTED: changes here need a typed confirmation")
        return ToolOutcome(content="\n".join(lines), summary=project or "no project")


class GcpProjectsTool(GcpTool):
    name: ClassVar[str] = "gcp_projects"
    description: ClassVar[str] = (
        "Every project these credentials can see. Application Default "
        "Credentials often reach many, and Altus acts in exactly one at a "
        "time — the user switches it with /gcp project <id>."
    )
    input_schema: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        provider = await self.provider(ctx)
        if isinstance(provider, ToolOutcome):
            return provider
        try:
            found = await provider.projects()
        except Exception as exc:
            return ToolOutcome.error(f"could not list projects: {exc}", summary="failed")

        active = self.project_for(args, ctx, provider)
        rows = [
            ["→" if entry["id"] == active else "", entry["name"], entry["id"], entry["state"]]
            for entry in found
        ]
        if not rows:
            return ToolOutcome(content="these credentials see no projects", summary="none")
        table = Table(title="projects", columns=["", "name", "id", "state"], rows=rows)
        return ToolOutcome(
            content=table.to_text(max_rows=60), summary=f"{len(rows)} projects", visual=table
        )


class GcpApisTool(GcpTool):
    name: ClassVar[str] = "gcp_apis"
    description: ClassVar[str] = (
        "List the Google APIs this client can call, optionally filtered. Use "
        "it to find the right API name before gcp_explain or gcp_call. These "
        "ship with the library, so the list is what is actually callable — an "
        "API newer than the installed client will not be here."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"filter": {"type": "string", "description": "Substring to match."}},
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        needle = str(args.get("filter", "")).casefold()
        names = [a for a in api.available_apis() if not needle or needle in a]
        shown = names[:200]
        body = "\n".join(shown) or "(none matched)"
        if len(names) > len(shown):
            body += f"\n[{len(names) - len(shown)} more; narrow the filter]"
        return ToolOutcome(content=body, summary=f"{len(names)} APIs")


class GcpExplainTool(GcpTool):
    name: ClassVar[str] = "gcp_explain"
    description: ClassVar[str] = (
        "The exact contract for a method, from the discovery document that "
        "ships with the client: which parameters it takes, which are required, "
        "its HTTP verb, whether it supports a validateOnly dry run, and how "
        "sensitive it is. Call this BEFORE gcp_call or gcp_write — it is "
        "authoritative and costs no network call. Give `api` alone to list its "
        "methods."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "api": {"type": "string", "description": "e.g. compute, storage, iam"},
            "method": {
                "type": "string",
                "description": "Full dotted id, e.g. compute.instances.delete",
            },
            "filter": {"type": "string", "description": "Substring, when listing methods."},
        },
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        method = str(args.get("method", "")).strip()
        if method:
            return self._describe(method)
        service = str(args.get("api", "")).strip()
        if not service:
            return ToolOutcome.error(
                "give either `api` to list methods, or `method` to explain one"
            )
        return self._list(service, str(args.get("filter", "")))

    def _describe(self, method: str) -> ToolOutcome:
        try:
            found = api.describe_method(method)
        except LookupError as exc:
            return ToolOutcome.error(
                f"{exc}. Call gcp_explain with `api` alone to see what it offers.",
                summary="unknown",
            )
        lines = [
            f"{found['id']}  [{found['sensitivity']}]  {found['http_method']} {found['path']}",
            f"  {found['documentation']}",
        ]
        if found["validate_only"]:
            lines.append(
                f"  dry run: pass {found['validate_only']}=true to validate without applying"
            )
        else:
            lines.append("  dry run: not available for this method")
        if found["request_body"]:
            lines.append("  takes a request body: pass it as `body`")
        lines.append("")
        for name, info in sorted(found["parameters"].items()):
            mark = "*" if info["required"] else " "
            lines.append(f"  {mark} {name:<26} {info['type']:<10} {info['documentation'][:88]}")
        lines.append("\n  * = required")
        return ToolOutcome(content="\n".join(lines), summary=method)

    def _list(self, service: str, needle: str) -> ToolOutcome:
        try:
            found = api.methods(service)
        except LookupError as exc:
            return ToolOutcome.error(str(exc), summary="unknown")
        lowered = needle.casefold()
        if lowered:
            found = [m for m in found if lowered in m.casefold()]
        rows = [[m, api.classify(m).value] for m in found[:150]]
        if not rows:
            return ToolOutcome.error(f"no methods matched in {service}", summary="none")
        table = Table(title=f"{service} methods", columns=["method", "sensitivity"], rows=rows)
        body = table.to_text(max_rows=150)
        if len(found) > 150:
            body += f"\n[{len(found) - 150} more; narrow with `filter`]"
        return ToolOutcome(content=body, summary=f"{len(found)} methods", visual=table)


class GcpCallTool(GcpTool):
    name: ClassVar[str] = "gcp_call"
    description: ClassVar[str] = (
        "Call any read-only Google Cloud method and get the response back. "
        "Paginated and capped automatically. Call gcp_explain first for the "
        "parameter names — they are case-sensitive and the client rejects "
        "unknown ones. Anything that changes something goes through gcp_write, "
        "which asks."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "method": {"type": "string", "description": "e.g. compute.instances.list"},
            "params": {"type": "object", "description": "Method parameters, exactly named."},
        },
        "required": ["method"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        method = str(args.get("method", "")).strip()
        if not method:
            return ToolOutcome.error("method is required, e.g. compute.instances.list")
        params = args.get("params") or {}
        if not isinstance(params, dict):
            return ToolOutcome.error("params must be an object")

        sensitivity = api.classify(method)
        if sensitivity not in (Sensitivity.READ, Sensitivity.SENSITIVE_READ):
            return ToolOutcome.error(
                f"{method} classifies as {sensitivity.value} — it changes something. "
                "Use gcp_write, which shows you what it could check first.",
                summary="wrong tool",
            )

        provider = await self.provider(ctx)
        if isinstance(provider, ToolOutcome):
            return provider
        project = self.project_for(args, ctx, provider)
        arguments = dict(params)
        # Nearly every GCP read takes the project as a parameter, and the model
        # should not have to repeat what the session already knows.
        if project and "project" not in arguments and "project" in _parameters(method):
            arguments["project"] = project

        try:
            payload = await provider.call(method, arguments)
        except Exception as exc:
            return ToolOutcome.error(f"{method} failed: {exc}", summary="failed")

        body = _as_yaml(self.scrub(payload, ctx))
        if len(body) > MAX_CONTENT:
            body = body[:MAX_CONTENT] + "\n[truncated]"
        return ToolOutcome(content=f"# {method}\n{body}", summary=method)


def _parameters(method_id: str) -> set[str]:
    found = api.lookup(method_id)
    return set((found or {}).get("parameters") or {})


class GcpCanITool(GcpTool):
    name: ClassVar[str] = "gcp_can_i"
    description: ClassVar[str] = (
        "Ask IAM which of a set of permissions this identity holds on a "
        "resource, BEFORE attempting something. Cheaper and clearer than "
        "discovering a 403 halfway through a plan, and unlike AWS's equivalent "
        "it needs no special permission to ask about yourself."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "permissions": {
                "type": "array",
                "items": {"type": "string"},
                "description": 'e.g. ["compute.instances.delete", "storage.buckets.setIamPolicy"]',
            },
            "resource": {
                "type": "string",
                "description": "What to ask about. Defaults to the active project.",
            },
        },
        "required": ["permissions"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        wanted = args.get("permissions")
        if not isinstance(wanted, list) or not wanted:
            return ToolOutcome.error("permissions must be a non-empty array of permission strings")

        provider = await self.provider(ctx)
        if isinstance(provider, ToolOutcome):
            return provider
        project = self.project_for(args, ctx, provider)
        resource = str(args.get("resource") or "").strip() or f"projects/{project}"
        if not project and not args.get("resource"):
            return ToolOutcome.error(
                "no project is selected and no resource was given.", summary="no scope"
            )

        try:
            payload = await provider.call(
                "cloudresourcemanager.projects.testIamPermissions",
                {"resource": resource, "body": {"permissions": [str(p) for p in wanted]}},
            )
        except Exception as exc:
            return ToolOutcome.error(
                f"the permission check could not run: {exc}", summary="cannot check"
            )

        held = {str(p) for p in payload.get("permissions") or []}
        rows = [[str(p), "allowed" if str(p) in held else "denied"] for p in wanted]
        table = Table(
            title=f"permissions on {resource}", columns=["permission", "decision"], rows=rows
        )
        return ToolOutcome(
            content=table.to_text(max_rows=60),
            summary=f"{len(held)}/{len(rows)} allowed",
            visual=table,
        )


class GcpAssetsTool(GcpTool):
    name: ClassVar[str] = "gcp_assets"
    description: ClassVar[str] = (
        "Search every resource in the project at once through Cloud Asset "
        "Inventory — far faster than listing each service. Use it for 'what "
        "have we got' or 'find everything with label X'. Needs the Cloud Asset "
        "API enabled; it says so plainly when that is missing rather than "
        "returning an empty answer."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": 'Asset query, e.g. "state:RUNNING" or "labels.env:prod"',
            },
            "asset_types": {
                "type": "array",
                "items": {"type": "string"},
                "description": 'e.g. ["compute.googleapis.com/Instance"]',
            },
            "scope": {
                "type": "string",
                "description": "Widen beyond the active project, e.g. organizations/123.",
            },
        },
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        provider = await self.provider(ctx)
        if isinstance(provider, ToolOutcome):
            return provider
        project = self.project_for(args, ctx, provider)
        scope = str(args.get("scope") or "").strip()
        if not scope and not project:
            return ToolOutcome.error(
                "no project is selected. Call gcp_projects first.", summary="no project"
            )

        try:
            payload = await provider.assets(
                str(args.get("query") or ""),
                [str(t) for t in args.get("asset_types") or []] or None,
                scope,
            )
        except Exception as exc:
            return ToolOutcome.error(_asset_hint(exc), summary="failed")

        results = payload.get("results") or []
        if not results:
            return ToolOutcome(content="nothing matched", summary="no assets")
        rows = [
            [
                str(item.get("assetType", "")).rsplit("/", 1)[-1],
                str(item.get("displayName") or str(item.get("name", "")).rsplit("/", 1)[-1]),
                str(item.get("location", "")),
                str(item.get("state", "")),
            ]
            for item in results
        ]
        table = Table(
            title=f"assets — {scope or project}",
            columns=["type", "name", "location", "state"],
            rows=rows,
        )
        return ToolOutcome(
            content=table.to_text(max_rows=80), summary=f"{len(rows)} assets", visual=table
        )


def _asset_hint(exc: Exception) -> str:
    """Cloud Asset is off by default on most projects, and the raw 403 does not
    say so. A hint is the difference between "broken" and "one command away"."""
    text = str(exc)
    if "has not been used" in text or "is disabled" in text or "SERVICE_DISABLED" in text:
        return (
            "the Cloud Asset API is not enabled on this project. Enable it with: "
            "gcloud services enable cloudasset.googleapis.com — or use gcp_call "
            "against each service instead."
        )
    if "403" in text or "PERMISSION_DENIED" in text:
        return (
            f"Cloud Asset refused this: {text[:200]}. It needs "
            "cloudasset.assets.searchAllResources, which many project-level roles lack."
        )
    return f"the asset search failed: {text}"
