"""The derived views: inventory, topology, cost and quota.

Where the terminal-graphics work pays off for the fourth time --- the visual
model and both render back ends already exist and need nothing new.

Cost is the odd one out, and not by choice. GCP has **no spend API**: Cloud
Billing exposes account metadata and SKU pricing and not a cent of actual
cost. Real spend lives only in a BigQuery export the account owner configures,
so ``gcp_cost`` reads that when it is configured and says so plainly when it is
not, rather than inventing a number.
"""

from __future__ import annotations

from typing import Any, ClassVar

from altus.core.visuals import Bar, Bars, Chart, GraphEdge, GraphNode, ResourceGraph, Series, Table
from altus.tools.base import ToolContext, ToolOutcome
from altus.tools.gcp.base import GcpTool


#: Node identities stay Kind/scope/name --- the same three-part shape the other
#: three clouds use --- so ``split_node_id`` and the drill-down path work
#: unchanged. The scope is the region or zone here.
def node_id(kind: str, scope: str, name: str) -> str:
    return f"{kind}/{scope}/{name}"


def _last(url: str) -> str:
    """GCP references other resources by full URL; the name is the last segment."""
    return str(url or "").rsplit("/", 1)[-1]


class GcpInventoryTool(GcpTool):
    name: ClassVar[str] = "gcp_inventory"
    description: ClassVar[str] = (
        "What exists in this project: every resource, its type and location, "
        "in one table. Uses Cloud Asset Inventory when it is available and "
        "falls back to listing the core services one by one when it is not — "
        "and says which it did, because the two do not see the same things."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": 'Asset query, e.g. "state:RUNNING"'},
        },
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        provider = await self.provider(ctx)
        if isinstance(provider, ToolOutcome):
            return provider
        project = self.project_for(args, ctx, provider)
        if not project:
            return ToolOutcome.error(
                "no project is selected. Call gcp_projects first.", summary="no project"
            )

        rows, note = await self._from_assets(provider, str(args.get("query") or ""))
        if rows is None:
            rows, note = await self._from_services(provider, project)
        if not rows:
            return ToolOutcome(content=f"nothing found in {project}\n{note}", summary="empty")

        table = Table(
            title=f"inventory — {project}",
            columns=["type", "name", "location", "state"],
            rows=rows,
            caption=note,
        )
        return ToolOutcome(
            content=table.to_text(max_rows=80), summary=f"{len(rows)} resources", visual=table
        )

    async def _from_assets(self, provider: Any, query: str) -> tuple[list[list[str]] | None, str]:
        try:
            payload = await provider.assets(query)
        except Exception:
            return None, ""
        return [
            [
                str(item.get("assetType", "")).rsplit("/", 1)[-1],
                str(item.get("displayName") or _last(str(item.get("name", "")))),
                str(item.get("location", "")),
                str(item.get("state", "")),
            ]
            for item in payload.get("results") or []
        ], "via Cloud Asset Inventory"

    async def _from_services(self, provider: Any, project: str) -> tuple[list[list[str]], str]:
        """The fallback, when Cloud Asset is off or refused.

        Narrower than the asset search by construction, so the caption says so
        --- reporting fewer resources without explaining why is how someone
        concludes a thing is gone when it is only unlisted.
        """
        rows: list[list[str]] = []
        missed: list[str] = []

        async def gather(method: str, params: dict[str, Any], build: Any) -> None:
            try:
                payload = await provider.call(method, params)
            except Exception as exc:
                missed.append(f"{method.split('.')[0]}: {str(exc)[:60]}")
                return
            rows.extend(build(payload))

        await gather(
            "compute.instances.aggregatedList",
            {"project": project},
            lambda p: [
                [
                    "Instance",
                    str(i.get("name", "")),
                    _last(str(i.get("zone", ""))),
                    str(i.get("status", "")),
                ]
                for scope in (p.get("items") or {}).values()
                for i in (scope.get("instances") or [])
            ],
        )
        await gather(
            "storage.buckets.list",
            {"project": project},
            lambda p: [
                ["Bucket", str(b.get("name", "")), str(b.get("location", "")), ""]
                for b in p.get("items") or []
            ],
        )
        await gather(
            "sqladmin.instances.list",
            {"project": project},
            lambda p: [
                [
                    "SqlInstance",
                    str(i.get("name", "")),
                    str(i.get("region", "")),
                    str(i.get("state", "")),
                ]
                for i in p.get("items") or []
            ],
        )

        note = "Cloud Asset unavailable — listed Compute, Storage and Cloud SQL only"
        if missed:
            note += f"; {'; '.join(missed[:3])}"
        return rows, note


class GcpTopologyTool(GcpTool):
    name: ClassVar[str] = "gcp_topology"
    description: ClassVar[str] = (
        "A birds-eye view of the project's network: which VPC networks hold "
        "which subnetworks, what runs in them, and which firewall rules and "
        "forwarding rules reach them. Use this to understand how something is "
        "exposed, or what a change would touch."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "network": {"type": "string", "description": "Limit to one VPC network by name."},
        },
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        provider = await self.provider(ctx)
        if isinstance(provider, ToolOutcome):
            return provider
        project = self.project_for(args, ctx, provider)
        if not project:
            return ToolOutcome.error(
                "no project is selected. Call gcp_projects first.", summary="no project"
            )
        wanted = str(args.get("network") or "").strip()

        async def fetch(method: str, key: str = "items") -> list[dict[str, Any]]:
            try:
                payload = await provider.call(method, {"project": project})
            except Exception:
                return []  # partial permissions are normal; draw what we can
            items = payload.get(key)
            if isinstance(items, dict):  # aggregatedList
                return [
                    entry
                    for scope in items.values()
                    for entry in (scope.get("instances") or scope.get("subnetworks") or [])
                ]
            return list(items or [])

        networks = await fetch("compute.networks.list")
        subnetworks = await fetch("compute.subnetworks.aggregatedList")
        instances = await fetch("compute.instances.aggregatedList")
        firewalls = await fetch("compute.firewalls.list")
        forwarding = await fetch("compute.forwardingRules.aggregatedList")

        if wanted:
            networks = [n for n in networks if n.get("name") == wanted]
            keep = {n.get("selfLink") for n in networks}
            subnetworks = [s for s in subnetworks if s.get("network") in keep]
            firewalls = [f for f in firewalls if f.get("network") in keep]

        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []
        by_link: dict[str, str] = {}

        def add(kind: str, name: str, scope: str, status: str = "", detail: str = "") -> str:
            ident = node_id(kind, scope, name)
            nodes.append(
                GraphNode(
                    id=ident,
                    kind=kind,
                    name=name,
                    namespace=scope,
                    status=status,
                    detail=detail,
                    reader="gcp_call",
                )
            )
            return ident

        for network in networks:
            ident = add(
                "Network",
                str(network.get("name", "")),
                "global",
                detail="auto" if network.get("autoCreateSubnetworks") else "custom",
            )
            by_link[str(network.get("selfLink", ""))] = ident

        for subnet in subnetworks:
            region = _last(str(subnet.get("region", "")))
            ident = add(
                "Subnetwork",
                str(subnet.get("name", "")),
                region,
                detail=str(subnet.get("ipCidrRange", "")),
            )
            by_link[str(subnet.get("selfLink", ""))] = ident
            parent = by_link.get(str(subnet.get("network", "")))
            if parent:
                edges.append(GraphEdge(source=parent, target=ident, relation="owns"))

        for instance in instances:
            zone = _last(str(instance.get("zone", "")))
            ident = add(
                "Instance",
                str(instance.get("name", "")),
                zone,
                status=str(instance.get("status", "")),
                detail=_last(str(instance.get("machineType", ""))),
            )
            by_link[str(instance.get("selfLink", ""))] = ident
            interfaces = instance.get("networkInterfaces") or []
            parent = next(
                (
                    by_link[key]
                    for key in (str(i.get("subnetwork", "")) for i in interfaces)
                    if key in by_link
                ),
                "",
            ) or next(
                (
                    by_link[key]
                    for key in (str(i.get("network", "")) for i in interfaces)
                    if key in by_link
                ),
                "",
            )
            if parent:
                edges.append(GraphEdge(source=parent, target=ident, relation="owns"))
            # An external IP is what makes an instance reachable from outside,
            # so it is worth saying on the node rather than buried in a detail.
            if any(i.get("accessConfigs") for i in interfaces):
                nodes[-1] = nodes[-1].model_copy(
                    update={"detail": f"{nodes[-1].detail} · external IP".strip(" ·")}
                )

        # Firewalls and forwarding rules are what make this a graph rather than
        # a tree: both cut across the network hierarchy.
        for rule in firewalls:
            parent = by_link.get(str(rule.get("network", "")))
            if parent is None:
                continue
            allowed = ", ".join(
                f"{a.get('IPProtocol', '')}:{','.join(str(p) for p in a.get('ports') or []) or '*'}"
                for a in (rule.get("allowed") or [])[:2]
            )
            ident = add("Firewall", str(rule.get("name", "")), "global", detail=allowed)
            edges.append(GraphEdge(source=ident, target=parent, relation="secures"))

        for rule in forwarding:
            parent = by_link.get(str(rule.get("network", "")))
            ident = add(
                "ForwardingRule",
                str(rule.get("name", "")),
                _last(str(rule.get("region", ""))) or "global",
                detail=str(rule.get("IPAddress", "")),
            )
            if parent:
                edges.append(GraphEdge(source=ident, target=parent, relation="exposes"))

        if not nodes:
            return ToolOutcome(content=f"no network resources found in {project}", summary="empty")

        counts = {kind: sum(1 for n in nodes if n.kind == kind) for kind in {n.kind for n in nodes}}
        graph = ResourceGraph(
            title=f"{project} — network",
            nodes=nodes,
            edges=edges,
            caption=" · ".join(f"{count} {kind}" for kind, count in sorted(counts.items())),
        )
        return ToolOutcome(content=graph.to_text(), summary=f"{len(nodes)} resources", visual=graph)


class GcpCostTool(GcpTool):
    name: ClassVar[str] = "gcp_cost"
    description: ClassVar[str] = (
        "Spend over time, broken down by service. GCP has no cost API — real "
        "spend exists only in a BigQuery billing export the account owner "
        "configures — so this needs [cloud.gcp] billing_export_table set. "
        "Without it, it says so and shows configured budgets instead."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "How far back. Default 30."},
            "top": {"type": "integer", "description": "How many services to plot. Default 5."},
        },
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        provider = await self.provider(ctx)
        if isinstance(provider, ToolOutcome):
            return provider
        project = self.project_for(args, ctx, provider)
        settings = getattr(ctx.cloud, "gcp_settings", None)
        table_name = str(getattr(settings, "billing_export_table", "") or "").strip()

        if not table_name:
            return await self._budgets(provider, project)

        days = max(1, min(int(args.get("days") or 30), 365))
        if not _is_safe_table(table_name):
            return ToolOutcome.error(
                f"[cloud.gcp] billing_export_table is {table_name!r}, which is not a "
                "project.dataset.table name.",
                summary="bad table",
            )

        sql = (
            "SELECT FORMAT_DATE('%Y-%m-%d', DATE(usage_start_time)) AS day, "
            "service.description AS service, SUM(cost) AS cost "
            f"FROM `{table_name}` "
            f"WHERE usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY) "
            "GROUP BY day, service ORDER BY day"
        )
        try:
            payload = await provider.call(
                "bigquery.jobs.query",
                {"projectId": project, "body": {"query": sql, "useLegacySql": False}},
            )
        except Exception as exc:
            return ToolOutcome.error(
                f"the billing export query failed: {exc}. This assumes the standard "
                "export schema (usage_start_time, service.description, cost); a detailed "
                "export has different columns.",
                summary="failed",
            )
        return self._chart(payload, days, int(args.get("top") or 5))

    def _chart(self, payload: dict[str, Any], days: int, top: int) -> ToolOutcome:
        fields = [str(f.get("name", "")) for f in (payload.get("schema") or {}).get("fields") or []]
        rows = payload.get("rows") or []
        if not fields or not rows:
            return ToolOutcome(content="the billing export returned no rows", summary="no data")
        index = {name: position for position, name in enumerate(fields)}

        by_service: dict[str, list[tuple[float, float]]] = {}
        for row in rows:
            cells = [c.get("v") for c in row.get("f") or []]
            try:
                amount = float(cells[index["cost"]])
            except TypeError, ValueError, KeyError, IndexError:
                continue
            stamp = _epoch(str(cells[index.get("day", 0)]))
            service = str(cells[index.get("service", 1)] or "unknown")
            by_service.setdefault(service, []).append((stamp, amount))

        if not by_service:
            return ToolOutcome(content="the billing export returned no cost", summary="no data")
        ranked = sorted(by_service.items(), key=lambda kv: -sum(a for _s, a in kv[1]))
        series = [
            Series(
                label=name,
                points=[a for _s, a in sorted(points)],
                at=[s for s, _a in sorted(points)],
            )
            for name, points in ranked[: max(1, min(top, 8))]
        ]
        total = sum(a for _n, points in ranked for _s, a in points)
        chart = Chart(
            title=f"spend, last {days} days",
            series=series,
            caption=f"{total:,.2f} total across {len(ranked)} services (from the billing export)",
        )
        return ToolOutcome(
            content=chart.to_text(), summary=f"{total:,.2f} over {days}d", visual=chart
        )

    async def _budgets(self, provider: Any, project: str) -> ToolOutcome:
        """The zero-config fallback, and an honest statement of the gap."""
        preamble = (
            "GCP has no cost API: Cloud Billing exposes account metadata and SKU "
            "pricing, not actual spend. Real spend lives only in a BigQuery billing "
            "export. Set [cloud.gcp] billing_export_table to chart it.\n"
        )
        try:
            info = await provider.call(
                "cloudbilling.projects.getBillingInfo", {"name": f"projects/{project}"}
            )
            account = str(info.get("billingAccountName", ""))
            if not account:
                return ToolOutcome(
                    content=preamble + "\nThis project has no billing account.",
                    summary="no billing",
                )
            budgets = await provider.call(
                "billingbudgets.billingAccounts.budgets.list", {"parent": account}
            )
        except Exception as exc:
            return ToolOutcome(
                content=f"{preamble}\nBudgets could not be read either: {exc}", summary="no cost"
            )

        rows = [
            [
                str(b.get("displayName", "")),
                str(((b.get("amount") or {}).get("specifiedAmount") or {}).get("units", "")),
                str(((b.get("amount") or {}).get("specifiedAmount") or {}).get("currencyCode", "")),
                str(len(b.get("thresholdRules") or [])),
            ]
            for b in budgets.get("budgets") or []
        ]
        if not rows:
            return ToolOutcome(
                content=preamble + "\nNo budgets are configured either.", summary="no cost"
            )
        table = Table(
            title="budgets",
            columns=["budget", "amount", "currency", "alerts"],
            rows=rows,
            caption="configured budgets, not actual spend",
        )
        return ToolOutcome(
            content=preamble + "\n" + table.to_text(max_rows=40),
            summary=f"{len(rows)} budgets",
            visual=table,
        )


def _is_safe_table(name: str) -> bool:
    """`project.dataset.table`, and nothing that could close the backtick.

    The table name comes from the user's own config rather than the model, but
    it is interpolated into SQL, and a name that terminated the quoting early
    would change what the query means.
    """
    import re

    return bool(re.fullmatch(r"[A-Za-z0-9_\-]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_\-]+", name))


def _epoch(day: str) -> float:
    from datetime import UTC, datetime

    try:
        return datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp()
    except ValueError:
        return 0.0


class GcpQuotasTool(GcpTool):
    name: ClassVar[str] = "gcp_quotas"
    description: ClassVar[str] = (
        "Compute quotas and how close this project is to them, with real "
        "current usage against each ceiling. The answer to 'why did that fail "
        "to start' when nothing looks wrong. Give a region for regional "
        "quotas; omit it for the project-wide ones."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "region": {"type": "string", "description": "e.g. europe-west1"},
            "used_only": {
                "type": "boolean",
                "description": "Only quotas with something consumed. Default true.",
            },
        },
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        provider = await self.provider(ctx)
        if isinstance(provider, ToolOutcome):
            return provider
        project = self.project_for(args, ctx, provider)
        if not project:
            return ToolOutcome.error(
                "no project is selected. Call gcp_projects first.", summary="no project"
            )
        region = str(args.get("region") or "").strip()

        try:
            if region:
                payload = await provider.call(
                    "compute.regions.get", {"project": project, "region": region}
                )
            else:
                payload = await provider.call("compute.projects.get", {"project": project})
        except Exception as exc:
            return ToolOutcome.error(f"could not read quotas: {exc}", summary="failed")

        used_only = args.get("used_only", True)
        bars: list[Bar] = []
        for quota in payload.get("quotas") or []:
            limit = float(quota.get("limit") or 0)
            usage = float(quota.get("usage") or 0)
            if limit <= 0 or (used_only and usage <= 0):
                continue
            bars.append(Bar(label=str(quota.get("metric", ""))[:40], value=usage, limit=limit))

        if not bars:
            where = region or "the project"
            return ToolOutcome(content=f"nothing is consuming a quota in {where}", summary="none")

        bars.sort(key=lambda b: -(b.value / (b.limit or 1)))
        tightest = bars[0]
        chart = Bars(
            title=f"compute quotas — {region or project}",
            bars=bars[:25],
            caption=f"tightest: {tightest.label} at "
            f"{tightest.value / (tightest.limit or 1):.0%} of its ceiling",
        )
        return ToolOutcome(content=chart.to_text(), summary=f"{len(bars)} quotas", visual=chart)
