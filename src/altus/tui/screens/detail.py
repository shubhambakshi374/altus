"""One object, in full, reached by clicking it on a map.

Read-only by construction. A node names the tool that opens it, and the only
tools named are reads --- ``k8s_get`` and ``aws_call``, both classified READ.
No approval is involved and neither gate is touched. That is the reason
drill-down is safe behind a single click: there is nothing here that could
change anything.

The reader is carried on the node rather than inferred from its kind, because
inferring would mean the front end guessing which cloud a graph came from, and
guessing wrong would send an AWS instance id to a Kubernetes tool.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Label, Static

#: What each reader needs, keyed by tool. Only reads appear here, and adding a
#: writer would be a bug rather than a feature.
READERS: dict[str, str] = {
    "k8s_get": "Kubernetes",
    "aws_call": "AWS",
    "azure_get": "Azure",
    "gcp_call": "GCP",
}

#: Which read answers for an AWS node kind. AWS has no single "get this ARN"
#: call, so a node's kind decides the operation.
AWS_READERS: dict[str, tuple[str, str]] = {
    "Instance": ("ec2", "DescribeInstances"),
    "Vpc": ("ec2", "DescribeVpcs"),
    "Subnet": ("ec2", "DescribeSubnets"),
    "SecurityGroup": ("ec2", "DescribeSecurityGroups"),
    "Volume": ("ec2", "DescribeVolumes"),
    "LoadBalancer": ("elbv2", "DescribeLoadBalancers"),
    "DBInstance": ("rds", "DescribeDBInstances"),
    "Function": ("lambda", "ListFunctions"),
    "Bucket": ("s3", "ListBuckets"),
}


#: Which namespace and type answer for an Azure node kind. A subnet is not a
#: resource of its own in ARM --- it lives on its virtual network --- so the
#: parent is what gets read.
AZURE_READERS: dict[str, tuple[str, str]] = {
    "VirtualMachine": ("Microsoft.Compute", "virtualMachines"),
    "VirtualNetwork": ("Microsoft.Network", "virtualNetworks"),
    "Subnet": ("Microsoft.Network", "virtualNetworks"),
    "NetworkInterface": ("Microsoft.Network", "networkInterfaces"),
    "NetworkSecurityGroup": ("Microsoft.Network", "networkSecurityGroups"),
    "PublicIP": ("Microsoft.Network", "publicIPAddresses"),
}


#: Which method reads an item behind a GCP node kind. GCP names a resource by
#: project plus zone or region, and the scope segment carries whichever applies.
GCP_READERS: dict[str, tuple[str, str]] = {
    "Instance": ("compute.instances.list", "zone"),
    "Network": ("compute.networks.list", ""),
    "Subnetwork": ("compute.subnetworks.list", "region"),
    "Firewall": ("compute.firewalls.list", ""),
    "ForwardingRule": ("compute.forwardingRules.list", "region"),
}


class NodeDetail(ModalScreen[None]):
    """The object behind a node, fetched when the screen opens."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Back"),
        Binding("q", "close", "Back"),
    ]

    DEFAULT_CSS = """
    NodeDetail { align: center middle; }
    NodeDetail > Vertical {
        width: 90%; height: 86%;
        border: round $accent; background: $surface; padding: 1 2;
    }
    NodeDetail .subject { text-style: bold; }
    NodeDetail .hint { color: $text-muted; padding-top: 1; }
    NodeDetail VerticalScroll { height: 1fr; }
    """

    def __init__(self, node_id: str, label: str, reader: str = "") -> None:
        super().__init__()
        self.node_id = node_id
        self.label = label
        self.reader = reader or "k8s_get"

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.label or self.node_id, classes="subject", markup=False)
            with VerticalScroll():
                yield Static("loading…", id="body", markup=False)
            yield Label("escape to go back", classes="hint", markup=False)

    def on_mount(self) -> None:
        self.run_worker(self._load(), exclusive=True)

    async def _load(self) -> None:
        from altus.cloud.k8s import split_node_id

        body = self.query_one("#body", Static)
        if self.reader not in READERS:
            body.update(f"cannot open this node: {self.reader!r} is not a reader")
            return

        parts = split_node_id(self.node_id)
        if parts is None:
            body.update(f"cannot read {self.node_id!r}: not a Kind/scope/name identity")
            return

        registry = getattr(self.app, "registry", None)
        context = getattr(self.app, "tool_ctx", None)
        if registry is None or context is None or self.reader not in registry:
            body.update(f"{READERS[self.reader]} tools are not available in this session.")
            return

        outcome = await registry.execute(self.reader, self._args(parts), context)
        body.update(outcome.content or "(empty)")

    def _args(self, parts: tuple[str, str, str]) -> dict[str, str]:
        """The identity is Kind/scope/name in all four clouds; only the
        parameter names differ, and the scope means namespace in Kubernetes,
        region in AWS, resource group in Azure and a zone or region in GCP."""
        kind, scope, name = parts
        if self.reader == "gcp_call":
            method, scope_name = GCP_READERS.get(kind, ("compute.instances.list", "zone"))
            args = {"method": method}
            # `global` is not a zone or a region --- sending it as one is a 400,
            # and the list call answers project-wide without it.
            if scope_name and scope and scope != "global":
                args["params"] = {scope_name: scope}  # type: ignore[assignment]
            return args
        if self.reader == "azure_get":
            namespace, resource_type = AZURE_READERS.get(kind, ("Microsoft.Resources", "resources"))
            args = {"namespace": namespace, "type": resource_type}
            if scope:
                args["group"] = scope
            return args
        if self.reader == "aws_call":
            service, operation = AWS_READERS.get(kind, ("ec2", "DescribeInstances"))
            args = {"service": service, "operation": operation}
            if scope:
                args["region"] = scope
            return args
        args = {"kind": kind, "name": name}
        if scope:
            args["namespace"] = scope
        return args

    def action_close(self) -> None:
        self.dismiss()
