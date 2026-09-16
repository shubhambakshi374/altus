"""The CLI fallback: kubectl, helm, kustomize.

Last resort, not the front door. Everything the native tools can express should
go through them --- they return structured objects, dry-run before mutating,
redact secrets, and classify what they are about to do. A shelled-out command
gives up all four, so the tool description asks the model to justify reaching
for it and the justification is shown to the user in the prompt.

The security property that matters here is simple and absolute: **a command is
an argv list handed to execve, never a string handed to a shell.** There is no
``sh -c`` anywhere in this module. A model-generated argument containing
``; rm -rf /`` arrives at kubectl as one literal argument and does nothing.
"""

from __future__ import annotations

import asyncio
import shutil
from typing import Any, ClassVar

from altus.cloud.base import Sensitivity
from altus.tools.approval import ApprovalRequest, Decision
from altus.tools.base import BaseTool, ToolContext, ToolOutcome

MAX_OUTPUT = 40_000
DEFAULT_TIMEOUT = 120.0

#: Subcommands that only read. Everything not listed is treated as a change,
#: so a subcommand we have never heard of is gated rather than waved through.
READ_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "kubectl": frozenset(
        {
            "get",
            "describe",
            "logs",
            "explain",
            "api-resources",
            "api-versions",
            "version",
            "cluster-info",
            "top",
            "diff",
            "events",
            "auth",
        }
    ),
    "helm": frozenset({"list", "ls", "status", "get", "history", "show", "search", "version"}),
    "kustomize": frozenset({"build", "version", "cfg"}),
}

#: Subcommands that are privileged whatever else they look like.
PRIVILEGED_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "kubectl": frozenset(
        {"exec", "attach", "port-forward", "proxy", "cp", "drain", "cordon", "uncordon", "taint"}
    ),
    "helm": frozenset({"plugin"}),
    "kustomize": frozenset(),
}

#: Flags we set ourselves from session state. A model supplying its own is
#: either confused or retargeting the command at a cluster the user did not
#: approve, and the prompt would then name the wrong blast radius.
RESERVED_FLAGS = (
    "--kubeconfig",
    "--context",
    "--kube-context",
    "--as",
    "--as-group",
    "--token",
    # AWS: these retarget the command at another account or identity, which
    # would make the approval prompt name the wrong blast radius.
    "--profile",
    "--region",
    "--endpoint-url",
    "--ca-bundle",
    # Azure: the same argument. One credential commonly sees many
    # subscriptions, and Altus acts in exactly one --- so a command that picks
    # its own would be approved against a target it is not going to touch.
    "--subscription",
    "--tenant",
    # GCP: the same argument again. --impersonate-service-account is the
    # sharpest of the four, because it changes *who* the command runs as
    # without changing anything the prompt would otherwise show.
    "--project",
    "--account",
    "--impersonate-service-account",
    "--configuration",
)


class CliTool(BaseTool):
    """One allowlisted binary."""

    binary: ClassVar[str] = ""
    read_only: ClassVar[bool] = False
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Arguments, one per element. Not a shell line.",
            },
            "reason": {
                "type": "string",
                "description": "Why the native k8s_* tools cannot do this. Shown to the user.",
            },
        },
        "required": ["args"],
    }

    def classify(self, args: list[str]) -> Sensitivity:
        positional = [a for a in args if not a.startswith("-")]
        subcommand = positional[0] if positional else ""
        if subcommand in PRIVILEGED_SUBCOMMANDS.get(self.binary, frozenset()):
            return Sensitivity.PRIVILEGED
        if self.binary == "aws":
            # Ask the same classifier the SDK path uses, rather than keeping a
            # second table that would drift from it. Two answers for
            # `terminate-instances` depending on which door it came through is
            # exactly the kind of gap a gate is supposed not to have.
            return _classify_aws(positional)
        if self.binary == "az":
            return _classify_azure(positional)
        if self.binary == "gcloud":
            return _classify_gcloud(positional)
        if any(a == "--raw" for a in args):
            # kubectl --raw reaches any API path with any verb, unclassified.
            return Sensitivity.PRIVILEGED
        if subcommand in READ_SUBCOMMANDS.get(self.binary, frozenset()):
            return Sensitivity.READ
        return Sensitivity.MUTATE

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        raw = args.get("args")
        if not isinstance(raw, list) or not raw or not all(isinstance(a, str) for a in raw):
            return ToolOutcome.error(
                "args must be a non-empty array of strings, one argument per element. "
                "There is no shell, so pipes and redirection do not work here."
            )
        argv = [str(a) for a in raw]

        # Read from the context, not from a `cli` attribute that never existed
        # --- the old check was reading None and passing every time, so the
        # allowlist was enforced only by registration.
        allowlist = getattr(ctx.cloud, "cli_allowlist", ()) or ()
        if allowlist and self.binary not in allowlist:
            return ToolOutcome.error(
                f"{self.binary} is not in [cloud] cli_allowlist", summary="not allowed"
            )
        offenders = [a for a in argv if a.split("=")[0] in RESERVED_FLAGS]
        if offenders:
            return ToolOutcome.error(
                f"{', '.join(offenders)} is set by Altus from the session's context — "
                "remove it. Switch cluster with k8s_use_context instead, which asks first.",
                summary="reserved flag",
            )
        path = shutil.which(self.binary)
        if path is None:
            return ToolOutcome.error(
                f"{self.binary} is not installed or not on PATH", summary="not found"
            )

        context_name = ctx.cloud.kube_context or ""
        full = [*argv]
        if context_name and self.binary in {"kubectl", "helm"}:
            full = [*argv, "--context", context_name]
        elif self.binary == "aws":
            region = getattr(ctx.cloud, "aws_region", "") or ""
            if region:
                full = [*argv, "--region", region]
        elif self.binary == "az":
            subscription = getattr(ctx.cloud, "azure_subscription", "") or ""
            if subscription:
                full = [*argv, "--subscription", subscription]
        elif self.binary == "gcloud":
            project = getattr(ctx.cloud, "gcp_project", "") or ""
            if project:
                full = [*argv, "--project", project]

        sensitivity = self.classify(argv)
        if sensitivity.needs_approval:
            reason = str(args.get("reason") or "").strip()
            decision = await ctx.approvals.request(
                ApprovalRequest(
                    tool=self.name,
                    action="run",
                    path=f"{self.binary} {' '.join(argv)}",
                    target=f"cluster {context_name or '(kubeconfig default)'}",
                    diff=f"$ {self.binary} {' '.join(full)}\n"
                    + (
                        f"\nreason given: {reason}"
                        if reason
                        else "\n(no reason given for using the CLI over the native tools)"
                    ),
                    dry_run="a shelled-out command is not dry-run; Altus cannot see what it will do",
                    recoverability="unknown — Altus does not model what this command changes",
                    destructive=True,
                    sensitivity=sensitivity,
                )
            )
            if decision is Decision.DENY:
                return ToolOutcome.rejected("The user rejected this command.")

        try:
            code, out, err = await _execute(path, full, timeout_seconds=DEFAULT_TIMEOUT)
        except Exception as exc:
            return ToolOutcome.error(f"{self.binary} failed to start: {exc}", summary="failed")

        from altus.cloud.redact import redact_text

        body = redact_text(out, enabled=ctx.cloud.redact_secrets)
        errors = redact_text(err, enabled=ctx.cloud.redact_secrets)
        if len(body) > MAX_OUTPUT:
            body = body[:MAX_OUTPUT] + "\n[truncated]"
        parts = [f"$ {self.binary} {' '.join(full)}", body or "(no output)"]
        if errors.strip():
            parts.append(f"stderr:\n{errors}")
        return ToolOutcome(
            content="\n".join(parts),
            is_error=code != 0,
            summary=f"{self.binary} exit {code}",
        )


#: `aws s3 ls` and friends, whose verbs are not the API operation name.
AWS_SHORTHAND: dict[tuple[str, str], str] = {
    ("s3", "ls"): "ListBuckets",
    ("s3", "cp"): "PutObject",
    ("s3", "mv"): "PutObject",
    ("s3", "rm"): "DeleteObject",
    ("s3", "rb"): "DeleteBucket",
    ("s3", "mb"): "CreateBucket",
    ("s3", "sync"): "PutObject",
}


def _classify_aws(positional: list[str]) -> Sensitivity:
    """`aws <service> <verb>` through the SDK's own classifier.

    The CLI's kebab-case verb is the API operation name with hyphens, so the
    two can share one table --- which is the point. An unrecognised shape falls
    through to the classifier's own fail-closed answer.
    """
    from altus.cloud.aws import classify

    if len(positional) < 2:
        return Sensitivity.MUTATE
    service, verb = positional[0], positional[1]
    operation = AWS_SHORTHAND.get((service, verb)) or "".join(
        part.capitalize() for part in verb.split("-")
    )
    return classify(service, operation)


#: `az <group...> <verb>` mapped onto the ARM types the SDK path talks about,
#: so one command cannot get two different answers depending on which door it
#: came through. Only the groups people actually reach for: anything absent
#: fails closed rather than being guessed at.
AZURE_TYPES: dict[str, str] = {
    "account": "Microsoft.Resources/subscriptions",
    "group": "Microsoft.Resources/subscriptions/resourceGroups",
    "resource": "Microsoft.Resources/resources",
    "deployment": "Microsoft.Resources/deployments",
    "tag": "Microsoft.Resources/tags",
    "vm": "Microsoft.Compute/virtualMachines",
    "vmss": "Microsoft.Compute/virtualMachineScaleSets",
    "disk": "Microsoft.Compute/disks",
    "snapshot": "Microsoft.Compute/snapshots",
    "image": "Microsoft.Compute/images",
    "storage account": "Microsoft.Storage/storageAccounts",
    "storage account keys": "Microsoft.Storage/storageAccounts",
    "storage container": "Microsoft.Storage/storageAccounts/blobServices/containers",
    "keyvault": "Microsoft.KeyVault/vaults",
    "keyvault secret": "Microsoft.KeyVault/vaults/secrets",
    "keyvault key": "Microsoft.KeyVault/vaults/keys",
    "keyvault certificate": "Microsoft.KeyVault/vaults/certificates",
    "role assignment": "Microsoft.Authorization/roleAssignments",
    "role definition": "Microsoft.Authorization/roleDefinitions",
    "lock": "Microsoft.Authorization/locks",
    "policy assignment": "Microsoft.Authorization/policyAssignments",
    "identity": "Microsoft.ManagedIdentity/userAssignedIdentities",
    "network vnet": "Microsoft.Network/virtualNetworks",
    "network vnet subnet": "Microsoft.Network/virtualNetworks/subnets",
    "network nsg": "Microsoft.Network/networkSecurityGroups",
    "network nsg rule": "Microsoft.Network/networkSecurityGroups/securityRules",
    "network nic": "Microsoft.Network/networkInterfaces",
    "network public-ip": "Microsoft.Network/publicIPAddresses",
    "network lb": "Microsoft.Network/loadBalancers",
    "network firewall": "Microsoft.Network/azureFirewalls",
    "aks": "Microsoft.ContainerService/managedClusters",
    "acr": "Microsoft.ContainerRegistry/registries",
    "webapp": "Microsoft.Web/sites",
    "functionapp": "Microsoft.Web/sites",
    "appservice plan": "Microsoft.Web/serverfarms",
    "sql server": "Microsoft.Sql/servers",
    "sql db": "Microsoft.Sql/servers/databases",
    "cosmosdb": "Microsoft.DocumentDB/databaseAccounts",
    "redis": "Microsoft.Cache/redis",
    "eventhubs namespace": "Microsoft.EventHub/namespaces",
    "monitor diagnostic-settings": "Microsoft.Insights/diagnosticSettings",
}

#: Commands whose ARM operation is nothing like their name. Every one of these
#: reads like an innocent `show` or `list` and hands back a live credential.
AZURE_OPERATIONS: dict[tuple[str, str], str] = {
    ("storage account keys", "list"): "Microsoft.Storage/storageAccounts/listKeys/action",
    ("storage account keys", "renew"): "Microsoft.Storage/storageAccounts/regenerateKey/action",
    (
        "storage account",
        "show-connection-string",
    ): "Microsoft.Storage/storageAccounts/listKeys/action",
    ("aks", "get-credentials"): (
        "Microsoft.ContainerService/managedClusters/listClusterUserCredential/action"
    ),
    ("acr", "login"): "Microsoft.ContainerRegistry/registries/listCredentials/action",
    ("acr credential", "show"): "Microsoft.ContainerRegistry/registries/listCredentials/action",
    ("webapp deployment list-publishing-profiles", "show"): "Microsoft.Web/sites/publishxml/action",
}

#: The az verbs that map onto an ARM verb. Anything else is taken as an action
#: named by the verb itself.
AZURE_VERBS: dict[str, str] = {
    "list": "read",
    "show": "read",
    "get": "read",
    "exists": "read",
    "wait": "read",
    "create": "write",
    "update": "write",
    "set": "write",
    "add": "write",
    "import": "write",
    "delete": "delete",
    "remove": "delete",
    "purge": "delete",
}


def _classify_azure(positional: list[str]) -> Sensitivity:
    """`az <group...> <verb>` through the SDK's own classifier.

    An unmapped group is PRIVILEGED. That is what catches `az login`,
    `az ad ...`, and every command a future CLI release adds --- the cases
    where guessing low is unrecoverable, and the reason the table is a short
    allowlist rather than an attempt at completeness.

    An unmapped *verb* under a mapped group is deliberately **not** privileged,
    which is a considered departure from the original plan. Treating it that
    way would demand a typed confirmation for `az vm start`, and the AWS work
    measured exactly where that leads: when every Delete* came out privileged,
    2,281 of them including DeleteTag, the challenge stopped meaning anything.
    So an unknown verb becomes an ARM action and `classify` judges it --- which
    already returns PRIVILEGED for key material and SENSITIVE_READ for the
    credential-shaped ones, and MUTATE otherwise. MUTATE still prompts, with
    the whole command shown.
    """
    from altus.cloud.azure import classify

    if len(positional) < 2:
        return Sensitivity.PRIVILEGED
    verb = positional[-1]
    group = " ".join(positional[:-1])

    override = AZURE_OPERATIONS.get((group, verb))
    if override:
        return classify(override)

    base = AZURE_TYPES.get(group)
    if base is None:
        return Sensitivity.PRIVILEGED

    segment = AZURE_VERBS.get(verb)
    operation = f"{base}/{segment}" if segment else f"{base}/{verb}/action"
    return classify(operation)


#: `gcloud <group...> <verb>` mapped onto the discovery collections the API
#: path talks about, so one command cannot get two different answers depending
#: on which door it came through. Only the groups people actually reach for:
#: anything absent fails closed rather than being guessed at.
GCLOUD_COLLECTIONS: dict[str, str] = {
    "projects": "cloudresourcemanager.projects",
    "config": "cloudresourcemanager.projects",
    "compute instances": "compute.instances",
    "compute disks": "compute.disks",
    "compute snapshots": "compute.snapshots",
    "compute images": "compute.images",
    "compute networks": "compute.networks",
    "compute networks subnets": "compute.subnetworks",
    "compute firewall-rules": "compute.firewalls",
    "compute addresses": "compute.addresses",
    "compute forwarding-rules": "compute.forwardingRules",
    "compute routes": "compute.routes",
    "compute regions": "compute.regions",
    "compute zones": "compute.zones",
    "storage buckets": "storage.buckets",
    "iam service-accounts": "iam.projects.serviceAccounts",
    "iam service-accounts keys": "iam.projects.serviceAccounts.keys",
    "iam roles": "iam.projects.roles",
    "kms keys": "cloudkms.projects.locations.keyRings.cryptoKeys",
    "kms keyrings": "cloudkms.projects.locations.keyRings",
    "secrets": "secretmanager.projects.secrets",
    "secrets versions": "secretmanager.projects.secrets.versions",
    "container clusters": "container.projects.locations.clusters",
    "sql instances": "sqladmin.instances",
    "pubsub topics": "pubsub.projects.topics",
    "pubsub subscriptions": "pubsub.projects.subscriptions",
    "functions": "cloudfunctions.projects.locations.functions",
    "run services": "run.projects.locations.services",
    "logging sinks": "logging.projects.sinks",
    "services": "serviceusage.services",
}

#: The gcloud verbs that map onto a discovery method name.
GCLOUD_VERBS: dict[str, str] = {
    "list": "list",
    "describe": "get",
    "get": "get",
    "create": "insert",
    "add": "insert",
    "delete": "delete",
    "remove": "delete",
    "update": "patch",
    "set": "patch",
    "add-iam-policy-binding": "setIamPolicy",
    "remove-iam-policy-binding": "setIamPolicy",
    "set-iam-policy": "setIamPolicy",
    "get-iam-policy": "getIamPolicy",
}

#: Commands whose API method is nothing like their name, and which hand back a
#: live credential --- the gcloud equivalents of `az storage account keys list`.
GCLOUD_OPERATIONS: dict[tuple[str, str], str] = {
    ("container clusters", "get-credentials"): "container.projects.locations.clusters.get",
    ("secrets versions", "access"): "secretmanager.projects.secrets.versions.access",
    ("iam service-accounts keys", "create"): "iam.projects.serviceAccounts.keys.create",
}


def _classify_gcloud(positional: list[str]) -> Sensitivity:
    """`gcloud <group...> <verb>` through the API's own classifier.

    An unmapped group is PRIVILEGED. That is what catches `gcloud auth`,
    `gcloud organizations`, and every command a future release adds --- and it
    is why the table is a short allowlist rather than an attempt at
    completeness.

    An unmapped *verb* under a mapped group is deliberately not privileged,
    the same considered departure the `az` classifier makes. Demanding a typed
    confirmation for `gcloud compute instances start` would train people to
    type through the challenge, which is what the AWS measurement showed when
    all 2,281 Delete* operations came out privileged.
    """
    from altus.cloud.gcp import classify

    if len(positional) < 2:
        return Sensitivity.PRIVILEGED
    verb = positional[-1]

    # Groups are one to three words, so try the longest prefix first: both
    # `compute networks` and `compute networks subnets` are real.
    for depth in range(len(positional) - 1, 0, -1):
        group = " ".join(positional[:depth])
        if group not in GCLOUD_COLLECTIONS:
            continue
        override = GCLOUD_OPERATIONS.get((group, verb))
        if override:
            return classify(override)
        method = GCLOUD_VERBS.get(verb, verb)
        return classify(f"{GCLOUD_COLLECTIONS[group]}.{method}")
    return Sensitivity.PRIVILEGED


async def _execute(path: str, argv: list[str], *, timeout_seconds: float) -> tuple[int, str, str]:
    """create_subprocess_exec, never create_subprocess_shell.

    The distinction is the whole security model of this module: exec takes an
    argument vector, so no part of ``argv`` is ever parsed as syntax.
    """
    proc = await asyncio.create_subprocess_exec(
        path,
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"timed out after {timeout_seconds:g}s"
    return (
        proc.returncode or 0,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )


class KubectlTool(CliTool):
    name: ClassVar[str] = "k8s_kubectl"
    binary: ClassVar[str] = "kubectl"
    description: ClassVar[str] = (
        "Run kubectl when — and only when — the native k8s_* tools cannot "
        "express what you need. They are better: structured output, a server "
        "dry run before every change, secret redaction, and an approval prompt "
        "that names what will happen. Say in `reason` why they are not enough; "
        "the user sees it. Arguments are a list, not a shell line, and "
        "--context is supplied by Altus."
    )


class HelmTool(CliTool):
    name: ClassVar[str] = "helm"
    binary: ClassVar[str] = "helm"
    description: ClassVar[str] = (
        "Run helm. A release install or upgrade changes many objects at once "
        "and Altus cannot dry-run it for you, so prefer `helm template` piped "
        "into k8s_apply when you want the change reviewed object by object."
    )


class AwsCliTool(CliTool):
    name: ClassVar[str] = "aws_cli"
    binary: ClassVar[str] = "aws"
    description: ClassVar[str] = (
        "Run the AWS CLI when — and only when — aws_call cannot express what "
        "you need. It almost always can: aws_explain gives you the exact "
        "parameter names, the response comes back structured and redacted, and "
        "the approval prompt says what was checked. The CLI gives up all four. "
        "Say in `reason` why it is necessary; the user sees it. --profile and "
        "--region are supplied by Altus."
    )


class KustomizeTool(CliTool):
    name: ClassVar[str] = "kustomize"
    binary: ClassVar[str] = "kustomize"
    description: ClassVar[str] = (
        "Run kustomize, usually `build` to render an overlay. Rendering is a "
        "read; feed the result to k8s_apply so the change is reviewed."
    )


class AzureCliTool(CliTool):
    name: ClassVar[str] = "azure_cli"
    binary: ClassVar[str] = "az"
    description: ClassVar[str] = (
        "Run the Azure CLI when — and only when — the azure_* tools cannot "
        "express what you need. They are better: azure_explain resolves the "
        "api-version for you, azure_write shows the user a real What-If diff "
        "before anything changes, output comes back structured and redacted, "
        "and the prompt says what was checked. The CLI gives up all four. Say "
        "in `reason` why it is necessary; the user sees it. --subscription is "
        "supplied by Altus."
    )


class GcloudTool(CliTool):
    name: ClassVar[str] = "gcp_cli"
    binary: ClassVar[str] = "gcloud"
    description: ClassVar[str] = (
        "Run gcloud when — and only when — the gcp_* tools cannot express what "
        "you need. They are better: gcp_explain gives you the exact method "
        "contract from a document that ships on disk, output comes back "
        "structured and redacted, and the approval prompt says what was "
        "checked. The CLI gives up all three. Say in `reason` why it is "
        "necessary; the user sees it. --project is supplied by Altus."
    )


def cli_tools() -> list[CliTool]:
    return [
        KubectlTool(),
        HelmTool(),
        KustomizeTool(),
        AwsCliTool(),
        AzureCliTool(),
        GcloudTool(),
    ]
