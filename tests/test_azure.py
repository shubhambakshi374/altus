"""Azure. No subscription is contacted.

The classification tests are the interesting half. botocore ships all 19,189
AWS operations on disk, so ``test_cloud.py`` can sweep them offline; Azure's
equivalent catalog is a network call, so there is nothing to sweep in CI.

The replacement is not weaker. Azure's operation grammar is regular and
closed --- ``{namespace}/{type}/{read|write|delete|action}`` --- so the sweep
below generates the cross product and asserts invariants over all of it. A
curated table pins the operations that actually matter.
"""

from __future__ import annotations

import itertools

import pytest

from altus.cloud import azure as az
from altus.cloud.base import Sensitivity

RESOURCE_SCOPE = (
    "/subscriptions/0000/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm"
)
GROUP_SCOPE = "/subscriptions/0000/resourceGroups/rg"
SUBSCRIPTION_SCOPE = "/subscriptions/0000"
MANAGEMENT_SCOPE = "/providers/Microsoft.Management/managementGroups/mg"


# ------------------------------------------------------------------- grammar


@pytest.mark.parametrize(
    ("operation", "namespace", "type_path", "verb", "action"),
    [
        (
            "Microsoft.Compute/virtualMachines/read",
            "Microsoft.Compute",
            "virtualMachines",
            "read",
            "",
        ),
        (
            "Microsoft.Compute/virtualMachines/start/action",
            "Microsoft.Compute",
            "virtualMachines",
            "action",
            "start",
        ),
        (
            "Microsoft.Network/virtualNetworks/subnets/write",
            "Microsoft.Network",
            "virtualNetworks/subnets",
            "write",
            "",
        ),
    ],
)
def test_parse_operation(
    operation: str, namespace: str, type_path: str, verb: str, action: str
) -> None:
    parsed = az.parse_operation(operation)
    assert parsed is not None
    assert (parsed.namespace, parsed.type_path, parsed.verb, parsed.action) == (
        namespace,
        type_path,
        verb,
        action,
    )


@pytest.mark.parametrize(
    "operation", ["", "/", "Microsoft.Compute", "Microsoft.Compute/vm/frobnicate"]
)
def test_unparseable_operations(operation: str) -> None:
    assert az.parse_operation(operation) is None
    assert az.classify(operation) is Sensitivity.PRIVILEGED


@pytest.mark.parametrize(
    ("scope", "level"),
    [
        (RESOURCE_SCOPE, "resource"),
        (GROUP_SCOPE, "resource-group"),
        (SUBSCRIPTION_SCOPE, "subscription"),
        (MANAGEMENT_SCOPE, "management-group"),
        ("", "unknown"),
    ],
)
def test_scope_level(scope: str, level: str) -> None:
    assert az.scope_level(scope) == level


# -------------------------------------------------------------- the classifier


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        # Ordinary reads stay reads --- listing role assignments is how you
        # understand a subscription.
        ("Microsoft.Compute/virtualMachines/read", Sensitivity.READ),
        ("Microsoft.Authorization/roleAssignments/read", Sensitivity.READ),
        ("Microsoft.Storage/storageAccounts/read", Sensitivity.READ),
        # Credential-shaped actions. Azure's sts:GetSessionToken.
        ("Microsoft.Storage/storageAccounts/listKeys/action", Sensitivity.SENSITIVE_READ),
        (
            "Microsoft.DocumentDB/databaseAccounts/listConnectionStrings/action",
            Sensitivity.SENSITIVE_READ,
        ),
        (
            "Microsoft.ContainerService/managedClusters/listClusterAdminCredential/action",
            Sensitivity.SENSITIVE_READ,
        ),
        ("Microsoft.Web/sites/publishxml/action", Sensitivity.SENSITIVE_READ),
        ("Microsoft.KeyVault/vaults/secrets/read", Sensitivity.SENSITIVE_READ),
        # Authorization writes.
        ("Microsoft.Authorization/roleAssignments/write", Sensitivity.PRIVILEGED),
        ("Microsoft.Authorization/roleDefinitions/delete", Sensitivity.PRIVILEGED),
        ("Microsoft.Authorization/locks/delete", Sensitivity.PRIVILEGED),
        ("Microsoft.ManagedIdentity/userAssignedIdentities/write", Sensitivity.PRIVILEGED),
        ("Microsoft.KeyVault/vaults/write", Sensitivity.PRIVILEGED),
        # Key material.
        ("Microsoft.Storage/storageAccounts/regenerateKey/action", Sensitivity.PRIVILEGED),
        # Network exposure.
        (
            "Microsoft.Network/networkSecurityGroups/securityRules/write",
            Sensitivity.PRIVILEGED,
        ),
        ("Microsoft.Network/publicIPAddresses/write", Sensitivity.PRIVILEGED),
        ("Microsoft.Sql/servers/firewallRules/write", Sensitivity.PRIVILEGED),
        # Irreversible destruction of something stateful.
        ("Microsoft.Sql/servers/databases/delete", Sensitivity.PRIVILEGED),
        ("Microsoft.Compute/virtualMachines/delete", Sensitivity.PRIVILEGED),
        # Ordinary changes.
        ("Microsoft.Compute/virtualMachines/write", Sensitivity.MUTATE),
        ("Microsoft.Compute/virtualMachines/start/action", Sensitivity.MUTATE),
        ("Microsoft.Insights/diagnosticSettings/delete", Sensitivity.MUTATE),
        ("Microsoft.Resources/tags/write", Sensitivity.MUTATE),
    ],
)
def test_classification(operation: str, expected: Sensitivity) -> None:
    assert az.classify(operation, RESOURCE_SCOPE) is expected


def test_wildcards_are_privileged() -> None:
    """A wildcard in a role definition covers everything under it."""
    for operation in ("*", "Microsoft.Compute/*", "*/read", "Microsoft.Compute/virtualMachines/*"):
        assert az.classify(operation, RESOURCE_SCOPE) is Sensitivity.PRIVILEGED


def test_scope_escalates_the_same_operation() -> None:
    """The axis AWS did not have: blast radius is a function of scope too.

    The same delete removes one diagnostic setting at a resource and every
    diagnostic setting in a subscription one level up.
    """
    operation = "Microsoft.Insights/diagnosticSettings/delete"
    assert az.classify(operation, RESOURCE_SCOPE) is Sensitivity.MUTATE
    assert az.classify(operation, GROUP_SCOPE) is Sensitivity.MUTATE
    assert az.classify(operation, SUBSCRIPTION_SCOPE) is Sensitivity.PRIVILEGED
    assert az.classify(operation, MANAGEMENT_SCOPE) is Sensitivity.PRIVILEGED


def test_data_actions_classify_one_level_stricter() -> None:
    """A data action reads the contents, not the container."""
    operation = "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read"
    assert az.classify(operation, RESOURCE_SCOPE) is Sensitivity.READ
    assert az.classify(operation, RESOURCE_SCOPE, data_action=True) is Sensitivity.SENSITIVE_READ


# ------------------------------------------------------------------ the sweep

NAMESPACES = (
    "Microsoft.Compute",
    "Microsoft.Storage",
    "Microsoft.Network",
    "Microsoft.Sql",
    "Microsoft.Web",
    "Microsoft.KeyVault",
    "Microsoft.Authorization",
    "Microsoft.ManagedIdentity",
    "Microsoft.ContainerService",
    "Microsoft.DocumentDB",
    "Microsoft.Insights",
    "Microsoft.Resources",
    "Microsoft.EventHub",
    "Microsoft.RecoveryServices",
    "Microsoft.AAD",
    "Microsoft.Management",
)

TYPES = (
    "virtualMachines",
    "storageAccounts",
    "networkSecurityGroups/securityRules",
    "servers/databases",
    "sites",
    "vaults/secrets",
    "roleAssignments",
    "locks",
    "managedClusters",
    "diagnosticSettings",
    "tags",
    "namespaces",
    "userAssignedIdentities",
    "publicIPAddresses",
)

ACTIONS = ("start", "restart", "listKeys", "listConnectionStrings", "regenerateKey", "failover")

SCOPES = (RESOURCE_SCOPE, GROUP_SCOPE, SUBSCRIPTION_SCOPE, MANAGEMENT_SCOPE, "")


def _corpus() -> list[tuple[str, str]]:
    """Every operation the grammar can express over the known vocabulary."""
    out: list[tuple[str, str]] = []
    for namespace, type_path, scope in itertools.product(NAMESPACES, TYPES, SCOPES):
        for verb in ("read", "write", "delete"):
            out.append((f"{namespace}/{type_path}/{verb}", scope))
        for action in ACTIONS:
            out.append((f"{namespace}/{type_path}/{action}/action", scope))
    return out


def test_the_corpus_is_worth_sweeping() -> None:
    assert len(_corpus()) > 5_000


def test_no_change_is_ever_classified_read() -> None:
    """The invariant the AWS corpus sweep exists to protect, restated.

    A write, a delete, or a non-credential action must never come back READ ---
    that would be a change made with nobody asked.
    """
    for operation, scope in _corpus():
        parsed = az.parse_operation(operation)
        assert parsed is not None
        if parsed.verb == "read":
            continue
        found = az.classify(operation, scope)
        assert found is not Sensitivity.READ, f"{operation} at {scope or '(no scope)'}"


def test_every_authorization_write_is_privileged() -> None:
    """Never merely MUTATE: these decide who may do what, and one of them
    removes the lock that protects everything else."""
    for operation, scope in _corpus():
        parsed = az.parse_operation(operation)
        assert parsed is not None
        if parsed.namespace.casefold() not in az.PRIVILEGED_NAMESPACES:
            continue
        if parsed.verb == "read" or az._is_credential_read(parsed.action):
            continue
        assert az.classify(operation, scope) is Sensitivity.PRIVILEGED, operation


def test_every_credential_action_is_at_least_sensitive() -> None:
    """Shaped like a read, and capable of minting access that outlives the
    conversation. Classifying one as a plain read is the whole failure mode."""
    for operation, scope in _corpus():
        parsed = az.parse_operation(operation)
        assert parsed is not None
        if not az._is_credential_read(parsed.action):
            continue
        assert az.classify(operation, scope) is not Sensitivity.READ, operation


def test_the_common_case_is_not_escalated() -> None:
    """A challenge that fires on everything trains people to type through it.

    The AWS work found exactly this: every ``Delete*`` came out privileged,
    2,281 of them including DeleteTag and DeleteAlarm, so the typed
    confirmation stopped meaning anything. The check that matters is not a
    ratio --- it is that an ordinary change to an ordinary resource, at the
    scope real calls are made at, stays MUTATE.
    """
    ordinary = [
        "Microsoft.Compute/virtualMachines/write",
        "Microsoft.Web/sites/write",
        "Microsoft.Insights/diagnosticSettings/write",
        "Microsoft.Resources/tags/write",
        "Microsoft.Compute/virtualMachines/start/action",
        "Microsoft.Compute/virtualMachines/restart/action",
        "Microsoft.Web/sites/restart/action",
        "Microsoft.Insights/diagnosticSettings/delete",
    ]
    for operation in ordinary:
        assert az.classify(operation, RESOURCE_SCOPE) is Sensitivity.MUTATE, operation


def test_the_corpus_reaches_every_level() -> None:
    """A sanity check on the sweep itself, not on the real world.

    The corpus pairs every namespace with every type, so it contains plenty of
    combinations that do not exist --- ``Microsoft.Compute/roleAssignments``
    and the like --- and its privileged share is an artifact of that, not a
    rate anyone would see. What it is good for is proving no level is
    unreachable and no branch is dead.
    """
    counts: dict[Sensitivity, int] = dict.fromkeys(Sensitivity, 0)
    for operation, scope in _corpus():
        counts[az.classify(operation, scope)] += 1
    assert all(counts[level] > 0 for level in Sensitivity), counts


# ------------------------------------------------------------- resource ids


@pytest.mark.parametrize(
    ("resource_id", "expected"),
    [
        (
            RESOURCE_SCOPE,
            {
                "subscription": "0000",
                "group": "rg",
                "namespace": "Microsoft.Compute",
                "type": "virtualMachines",
                "name": "vm",
            },
        ),
        (
            "/subscriptions/1/resourceGroups/g/providers/Microsoft.Network/virtualNetworks/v/subnets/s",
            {
                "subscription": "1",
                "group": "g",
                "namespace": "Microsoft.Network",
                "type": "virtualNetworks/subnets",
                "name": "s",
            },
        ),
        (
            SUBSCRIPTION_SCOPE,
            {"subscription": "0000", "group": "", "namespace": "", "type": "", "name": ""},
        ),
    ],
)
def test_parse_resource_id(resource_id: str, expected: dict[str, str]) -> None:
    assert az.parse_resource_id(resource_id) == expected


def test_parse_resource_id_survives_nonsense() -> None:
    """A bad id is something to report to the model, not an exception that
    aborts a turn it could have recovered from."""
    assert az.parse_resource_id("not-an-id")["subscription"] == ""


@pytest.mark.parametrize(
    ("resource_id", "verb", "action", "expected"),
    [
        (RESOURCE_SCOPE, "delete", "", "Microsoft.Compute/virtualMachines/delete"),
        (RESOURCE_SCOPE, "action", "start", "Microsoft.Compute/virtualMachines/start/action"),
        (SUBSCRIPTION_SCOPE, "read", "", ""),
    ],
)
def test_operation_for(resource_id: str, verb: str, action: str, expected: str) -> None:
    """ARM takes a URL and RBAC talks about operations; something has to
    translate, and every preflight depends on it."""
    assert az.operation_for(resource_id, verb, action) == expected


def test_target_for_feeds_the_protection_rules() -> None:
    """Protecting a subscription by id starts working with no new config ---
    ProtectedSettings.accounts already holds opaque ids."""
    from altus.cloud.base import ProtectionRules

    rules = ProtectionRules.build([], ["0000"], "confirm")
    assert rules.matches(az.target_for("0000", "westeurope", "rg"))
    assert not rules.matches(az.target_for("1111", "westeurope", "rg"))


# ------------------------------------------------------------------ the tools

from typing import Any, ClassVar  # noqa: E402

from altus.config.models import AzureSettings, CloudSettings  # noqa: E402
from altus.tools import default_registry  # noqa: E402
from altus.tools.azure import azure_tools  # noqa: E402
from altus.tools.base import CloudContext, ToolContext  # noqa: E402
from altus.workspace import Workspace  # noqa: E402

IDENTITY = {
    "tenant": "t-0000",
    "principal": "dev@example.com",
    "object_id": "o-1111",
    "subscription": "sub-0000",
}


class FakeAzure:
    """An AzureProvider's shape, recording every request."""

    def __init__(self, responses: dict[str, Any] | None = None, *, fail: str = "") -> None:
        self.responses = responses or {}
        self.fail = fail
        self.subscription = "sub-0000"
        self.requests: list[tuple[str, str]] = []
        self.locks_found: list[dict[str, str]] = []
        self.access = "Allowed"
        self.graph_queries: list[str] = []

    async def whoami(self) -> dict[str, str]:
        return dict(IDENTITY)

    async def subscriptions(self) -> list[dict[str, str]]:
        return [
            {"id": "sub-0000", "name": "dev", "state": "Enabled", "tenant": "t-0000"},
            {"id": "sub-9999", "name": "prod", "state": "Enabled", "tenant": "t-0000"},
        ]

    async def providers(self, namespace: str = "") -> dict[str, Any]:
        if not namespace:
            return {"value": [{"namespace": "Microsoft.Compute"}, {"namespace": "Microsoft.Web"}]}
        return {
            "resourceTypes": [
                {
                    "resourceType": "virtualMachines",
                    "apiVersions": ["2024-07-01-preview", "2024-03-01"],
                    "locations": ["westeurope", "eastus"],
                }
            ]
        }

    async def provider_operations(self, namespace: str) -> dict[str, Any]:
        return {
            "name": namespace,
            "resourceTypes": [
                {
                    "name": "virtualMachines",
                    "operations": [
                        {
                            "name": f"{namespace}/virtualMachines/read",
                            "description": "Get the properties of a virtual machine",
                            "isDataAction": False,
                        },
                        {
                            "name": f"{namespace}/virtualMachines/delete",
                            "description": "Delete a virtual machine",
                            "isDataAction": False,
                        },
                    ],
                }
            ],
        }

    #: Types the provider manifest lists. Anything else has to raise rather
    #: than invent a version --- a call pinned to a wrong one fails in a way
    #: that looks like the resource is gone.
    KNOWN_TYPES: ClassVar[dict[str, str]] = {
        "virtualmachines": "2024-03-01",
        "locations/usages": "2023-07-01",
    }

    async def api_version_for(self, namespace: str, resource_type: str) -> str:
        found = self.KNOWN_TYPES.get(resource_type.casefold())
        if found is None:
            raise az.ArmError(404, "UnknownResourceType", f"no such type {resource_type}")
        return found

    async def call(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        self.requests.append((method.upper(), path))
        if self.fail and self.fail in path:
            raise az.ArmError(403, "AuthorizationFailed", "denied")
        return dict(self.responses.get(path, {"value": []}))

    async def graph(self, query: str, subscriptions: list[str] | None = None) -> dict[str, Any]:
        self.graph_queries.append(query)
        return dict(self.responses.get("graph", {"data": []}))

    async def locks(self, scope: str) -> list[dict[str, str]]:
        return list(self.locks_found)

    async def check_access(self, scope: str, actions: list[str]) -> list[dict[str, str]]:
        return [{"action": a, "decision": self.access} for a in actions]

    @property
    def methods(self) -> set[str]:
        return {method for method, _path in self.requests}


def context(
    tmp_path: Any,
    provider: FakeAzure | None = None,
    *,
    settings: AzureSettings | None = None,
    protection: Any = None,
    approvals: Any = None,
) -> ToolContext:
    from altus.tools.approval import AllowAll

    return ToolContext(
        workspace=Workspace([tmp_path]),
        approvals=approvals or AllowAll(),
        cloud=CloudContext(
            azure=provider,
            azure_subscription="sub-0000",
            azure_settings=settings or AzureSettings(),
            protection=protection,
        ),
    )


def tool(name: str) -> Any:
    found = next((t for t in azure_tools() if t.name == name), None)
    assert found is not None, name
    return found


async def test_whoami_names_the_subscription(tmp_path: Any) -> None:
    outcome = await tool("azure_whoami").run({}, context(tmp_path, FakeAzure()))
    assert "sub-0000" in outcome.content
    assert "dev@example.com" in outcome.content
    assert "PROTECTED" not in outcome.content


async def test_whoami_warns_when_the_subscription_is_protected(tmp_path: Any) -> None:
    from altus.cloud.base import ProtectionRules

    rules = ProtectionRules.build([], ["sub-0000"], "confirm")
    outcome = await tool("azure_whoami").run({}, context(tmp_path, FakeAzure(), protection=rules))
    assert "PROTECTED" in outcome.content


async def test_tools_refuse_clearly_without_a_session(tmp_path: Any) -> None:
    """A missing extra is a visible absence, not an import error at call time."""
    outcome = await tool("azure_whoami").run({}, context(tmp_path, None))
    assert outcome.is_error
    assert "uv sync --extra azure" in outcome.content


async def test_subscriptions_marks_the_active_one(tmp_path: Any) -> None:
    outcome = await tool("azure_subscriptions").run({}, context(tmp_path, FakeAzure()))
    assert "sub-9999" in outcome.content
    assert "→" in outcome.content


async def test_providers_lists_api_versions(tmp_path: Any) -> None:
    outcome = await tool("azure_providers").run(
        {"namespace": "Microsoft.Compute"}, context(tmp_path, FakeAzure())
    )
    assert "virtualMachines" in outcome.content
    assert "2024-07-01-preview" in outcome.content


async def test_explain_classifies_each_operation(tmp_path: Any) -> None:
    outcome = await tool("azure_explain").run(
        {"namespace": "Microsoft.Compute", "type": "virtualMachines"},
        context(tmp_path, FakeAzure()),
    )
    assert "api-version: 2024-03-01" in outcome.content
    assert "read" in outcome.content
    assert "privileged" in outcome.content  # the delete


async def test_get_issues_only_get(tmp_path: Any) -> None:
    """The read-only property is structural, not a rule the tool remembers.

    ARM's read verb is GET, and the operations that look like reads but hand
    back credentials are POSTs --- so they cannot arrive here at all.
    """
    provider = FakeAzure({RESOURCE_SCOPE: {"name": "vm", "location": "westeurope"}})
    outcome = await tool("azure_get").run({"id": RESOURCE_SCOPE}, context(tmp_path, provider))
    assert not outcome.is_error
    assert provider.methods == {"GET"}
    assert "api-version 2024-03-01" in outcome.content


async def test_get_resolves_the_api_version_it_was_not_given(tmp_path: Any) -> None:
    provider = FakeAzure()
    await tool("azure_get").run(
        {"namespace": "Microsoft.Compute", "type": "virtualMachines"},
        context(tmp_path, provider),
    )
    assert provider.requests == [
        ("GET", "/subscriptions/sub-0000/providers/Microsoft.Compute/virtualMachines")
    ]


async def test_get_reports_an_unresolvable_type_instead_of_guessing(tmp_path: Any) -> None:
    """Guessing an api-version fails in a way that looks like the resource is
    gone, which is the worst available answer."""
    provider = FakeAzure()
    outcome = await tool("azure_get").run(
        {"namespace": "Microsoft.Compute", "type": "nonesuch"}, context(tmp_path, provider)
    )
    assert outcome.is_error
    assert provider.requests == []


async def test_get_rejects_something_that_is_not_a_resource_id(tmp_path: Any) -> None:
    outcome = await tool("azure_get").run({"id": "web1"}, context(tmp_path, FakeAzure()))
    assert outcome.is_error
    assert "not an ARM resource id" in outcome.content


async def test_get_redacts_before_the_model_sees_it(tmp_path: Any) -> None:
    """Tool results are transmitted to the LLM provider, so redaction is the
    control that matters, not an extra."""
    from altus.cloud.redact import MARKER

    provider = FakeAzure({RESOURCE_SCOPE: {"properties": {"adminPassword": "hunter2"}}})
    outcome = await tool("azure_get").run({"id": RESOURCE_SCOPE}, context(tmp_path, provider))
    assert "hunter2" not in outcome.content
    assert MARKER in outcome.content


async def test_query_runs_kql_against_the_active_subscription(tmp_path: Any) -> None:
    provider = FakeAzure({"graph": {"data": [{"name": "web1", "location": "westeurope"}]}})
    outcome = await tool("azure_query").run(
        {"query": "resources | project name, location"}, context(tmp_path, provider)
    )
    assert "web1" in outcome.content
    assert outcome.visual is not None
    assert provider.graph_queries == ["resources | project name, location"]


async def test_can_i_reports_the_decision_and_the_sensitivity(tmp_path: Any) -> None:
    outcome = await tool("azure_can_i").run(
        {"actions": ["Microsoft.Compute/virtualMachines/delete"]},
        context(tmp_path, FakeAzure()),
    )
    assert "Allowed" in outcome.content
    assert "privileged" in outcome.content
    assert outcome.summary == "1/1 allowed"


READ_TOOLS = {
    "azure_whoami",
    "azure_subscriptions",
    "azure_providers",
    "azure_explain",
    "azure_get",
    "azure_query",
    "azure_can_i",
    "azure_inventory",
    "azure_topology",
    "azure_cost",
    "azure_quotas",
}


def test_read_only_is_declared_correctly_on_every_tool() -> None:
    """`read_only` is what a read-only registry filters on, so it has to be
    right on all of them --- in both directions."""
    for found in azure_tools():
        assert found.read_only == (found.name in READ_TOOLS), found.name


def test_the_registry_registers_azure_when_it_is_asked_to(tmp_path: Any) -> None:
    registry = default_registry(kubernetes=False, aws=False, azure=True, cloud=CloudSettings())
    assert "azure_get" in registry
    assert "azure_query" in registry
    assert "aws_call" not in registry


def test_the_registry_leaves_azure_out_when_it_is_not(tmp_path: Any) -> None:
    """The native tools go, the CLI fallback stays.

    `az` is a binary on PATH, not the Python SDK, so it is gated by
    cli_allowlist rather than by whether azure-identity is installed --- the
    same way kubectl survives a registry built without the Kubernetes SDK.
    """
    registry = default_registry(kubernetes=False, aws=False, azure=False, cloud=CloudSettings())
    native = {name for name in registry.names if name.startswith("azure_")} - {"azure_cli"}
    assert native == set()


# ------------------------------------------------------------ curated views

VNET_ID = (
    "/subscriptions/sub-0000/resourceGroups/rg/providers/Microsoft.Network/virtualNetworks/vnet"
)
SUBNET_ID = f"{VNET_ID}/subnets/web"
NIC_ID = (
    "/subscriptions/sub-0000/resourceGroups/rg/providers/Microsoft.Network/networkInterfaces/nic1"
)
PIP_ID = (
    "/subscriptions/sub-0000/resourceGroups/rg/providers/Microsoft.Network/publicIPAddresses/pip1"
)
NSG_ID = "/subscriptions/sub-0000/resourceGroups/rg/providers/Microsoft.Network/networkSecurityGroups/nsg"
VM_ID = "/subscriptions/sub-0000/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/web1"

TOPOLOGY_ROWS = [
    {
        "id": VNET_ID,
        "name": "vnet",
        "type": "microsoft.network/virtualNetworks",
        "location": "westeurope",
        "resourceGroup": "rg",
        "properties": {
            "addressSpace": {"addressPrefixes": ["10.0.0.0/16"]},
            "subnets": [
                {"id": SUBNET_ID, "name": "web", "properties": {"addressPrefix": "10.0.1.0/24"}}
            ],
        },
    },
    {
        "id": NIC_ID,
        "name": "nic1",
        "type": "microsoft.network/networkInterfaces",
        "location": "westeurope",
        "resourceGroup": "rg",
        "properties": {
            "ipConfigurations": [
                {
                    "properties": {
                        "subnet": {"id": SUBNET_ID},
                        "publicIPAddress": {"id": PIP_ID},
                    }
                }
            ]
        },
    },
    {
        "id": PIP_ID,
        "name": "pip1",
        "type": "microsoft.network/publicIPAddresses",
        "location": "westeurope",
        "resourceGroup": "rg",
        "properties": {"ipAddress": "20.1.2.3"},
    },
    {
        "id": NSG_ID,
        "name": "nsg",
        "type": "microsoft.network/networkSecurityGroups",
        "location": "westeurope",
        "resourceGroup": "rg",
        "properties": {"subnets": [{"id": SUBNET_ID}], "networkInterfaces": []},
    },
    {
        "id": VM_ID,
        "name": "web1",
        "type": "microsoft.compute/virtualMachines",
        "location": "westeurope",
        "resourceGroup": "rg",
        "properties": {
            "hardwareProfile": {"vmSize": "Standard_D2s_v3"},
            "networkProfile": {"networkInterfaces": [{"id": NIC_ID}]},
        },
    },
]


async def test_inventory_is_one_query_not_a_walk(tmp_path: Any) -> None:
    provider = FakeAzure(
        {
            "graph": {
                "data": [
                    {
                        "name": "web1",
                        "type": "microsoft.compute/virtualMachines",
                        "resourceGroup": "rg",
                        "location": "westeurope",
                    }
                ]
            }
        }
    )
    outcome = await tool("azure_inventory").run({}, context(tmp_path, provider))
    assert "web1" in outcome.content
    assert "virtualMachines" in outcome.content
    assert len(provider.graph_queries) == 1
    assert provider.requests == []


async def test_inventory_escapes_a_group_name_into_kql(tmp_path: Any) -> None:
    """A quote that terminated the literal early would let a model-supplied
    name change what the query means."""
    provider = FakeAzure()
    await tool("azure_inventory").run({"group": "rg' or true or '"}, context(tmp_path, provider))
    assert "\\'" in provider.graph_queries[0]


async def test_topology_builds_the_network_hierarchy(tmp_path: Any) -> None:
    provider = FakeAzure({"graph": {"data": TOPOLOGY_ROWS}})
    outcome = await tool("azure_topology").run({}, context(tmp_path, provider))
    graph = outcome.visual
    assert graph is not None

    kinds = {node.kind for node in graph.nodes}
    assert kinds == {
        "VirtualNetwork",
        "Subnet",
        "NetworkInterface",
        "PublicIP",
        "NetworkSecurityGroup",
        "VirtualMachine",
    }
    relations = {(e.relation, e.source.split("/")[0], e.target.split("/")[0]) for e in graph.edges}
    # Containment: vnet owns subnet, subnet owns nic, nic owns vm.
    assert ("owns", "VirtualNetwork", "Subnet") in relations
    assert ("owns", "Subnet", "NetworkInterface") in relations
    assert ("owns", "NetworkInterface", "VirtualMachine") in relations
    # The two relations that make it a graph rather than a tree.
    assert ("secures", "NetworkSecurityGroup", "Subnet") in relations
    assert ("exposes", "PublicIP", "NetworkInterface") in relations


async def test_topology_nodes_carry_a_reader_so_a_click_can_open_them(tmp_path: Any) -> None:
    """The drill-down seam added for AWS, used unchanged. A node names the
    tool that reads it rather than the front end guessing from the kind ---
    guessing wrong would send an ARM id to a Kubernetes tool."""
    provider = FakeAzure({"graph": {"data": TOPOLOGY_ROWS}})
    outcome = await tool("azure_topology").run({}, context(tmp_path, provider))
    assert outcome.visual is not None
    assert {node.reader for node in outcome.visual.nodes} == {"azure_get"}
    for node in outcome.visual.nodes:
        assert node.id.count("/") == 2, node.id


async def test_topology_drops_an_unattached_security_group(tmp_path: Any) -> None:
    """Noise on a topology map, and the AWS version drops them for the same
    reason."""
    rows = [row for row in TOPOLOGY_ROWS if row["id"] != NSG_ID]
    rows.append({**TOPOLOGY_ROWS[3], "properties": {"subnets": [], "networkInterfaces": []}})
    provider = FakeAzure({"graph": {"data": rows}})
    outcome = await tool("azure_topology").run({}, context(tmp_path, provider))
    assert outcome.visual is not None
    assert not any(node.kind == "NetworkSecurityGroup" for node in outcome.visual.nodes)


COST_PAYLOAD = {
    "properties": {
        "columns": [
            {"name": "Cost"},
            {"name": "UsageDate"},
            {"name": "ServiceName"},
            {"name": "Currency"},
        ],
        "rows": [
            [12.5, 20260101, "Virtual Machines", "EUR"],
            [4.0, 20260101, "Storage", "EUR"],
            [13.5, 20260102, "Virtual Machines", "EUR"],
        ],
    }
}


async def test_cost_charts_each_service_on_shared_axes(tmp_path: Any) -> None:
    provider = FakeAzure(
        {"/subscriptions/sub-0000/providers/Microsoft.CostManagement/query": COST_PAYLOAD}
    )
    outcome = await tool("azure_cost").run({}, context(tmp_path, provider))
    chart = outcome.visual
    assert chart is not None
    assert [s.label for s in chart.series] == ["Virtual Machines", "Storage"]
    assert chart.series[0].points == [12.5, 13.5]
    assert chart.series[0].timed  # a time axis, not just a shape
    assert "EUR" in outcome.summary


async def test_cost_reads_columns_by_name_not_position(tmp_path: Any) -> None:
    """Cost Management's column order is not contractual, and reading by
    position would silently chart the date as the amount."""
    shuffled = {
        "properties": {
            "columns": [
                {"name": "ServiceName"},
                {"name": "Currency"},
                {"name": "UsageDate"},
                {"name": "Cost"},
            ],
            "rows": [["Virtual Machines", "EUR", 20260101, 12.5]],
        }
    }
    provider = FakeAzure(
        {"/subscriptions/sub-0000/providers/Microsoft.CostManagement/query": shuffled}
    )
    outcome = await tool("azure_cost").run({}, context(tmp_path, provider))
    assert outcome.visual is not None
    assert outcome.visual.series[0].points == [12.5]


async def test_cost_says_so_when_there_is_nothing(tmp_path: Any) -> None:
    provider = FakeAzure(
        {"/subscriptions/sub-0000/providers/Microsoft.CostManagement/query": {"properties": {}}}
    )
    outcome = await tool("azure_cost").run({}, context(tmp_path, provider))
    assert "no cost data" in outcome.content


async def test_quotas_plot_real_usage_against_the_ceiling(tmp_path: Any) -> None:
    """The AWS version could only plot the quota and had to say so in its
    caption. Azure reports what is consumed, so Bar.limit means what it was
    built to mean."""
    usages = {
        "value": [
            {
                "name": {"localizedValue": "Total Regional vCPUs"},
                "currentValue": 90,
                "limit": 100,
                "unit": "Count",
            },
            {
                "name": {"localizedValue": "Standard DSv3 Family vCPUs"},
                "currentValue": 2,
                "limit": 50,
                "unit": "Count",
            },
            {
                "name": {"localizedValue": "Unused Family"},
                "currentValue": 0,
                "limit": 10,
                "unit": "Count",
            },
        ]
    }
    path = "/subscriptions/sub-0000/providers/Microsoft.Compute/locations/westeurope/usages"
    provider = FakeAzure({path: usages})
    outcome = await tool("azure_quotas").run({"region": "westeurope"}, context(tmp_path, provider))
    chart = outcome.visual
    assert chart is not None
    assert [bar.value for bar in chart.bars] == [90.0, 2.0]  # the unused one is dropped
    assert chart.bars[0].limit == 100.0  # tightest first
    assert "90%" in chart.caption


def test_every_curated_view_is_registered() -> None:
    assert {t.name for t in azure_tools()} >= {
        "azure_inventory",
        "azure_topology",
        "azure_cost",
        "azure_quotas",
    }


def test_the_drill_down_knows_how_to_read_an_azure_node() -> None:
    """detail.py dispatches on the node's own reader. A node whose kind is not
    mapped still opens --- it falls back to a generic resource read rather than
    to a tool for another cloud."""
    from altus.tui.screens.detail import AZURE_READERS, READERS, NodeDetail

    assert READERS["azure_get"] == "Azure"
    screen = NodeDetail("VirtualMachine/rg/web1", "web1", reader="azure_get")
    assert screen._args(("VirtualMachine", "rg", "web1")) == {
        "namespace": "Microsoft.Compute",
        "type": "virtualMachines",
        "group": "rg",
    }
    assert screen._args(("Nonesuch", "rg", "x"))["namespace"] == "Microsoft.Resources"
    assert set(AZURE_READERS) >= {"VirtualMachine", "Subnet", "NetworkSecurityGroup"}


async def test_a_map_that_does_not_fit_says_so(tmp_path: Any) -> None:
    """Found by looking at the rendered output rather than by a test.

    The VNet → subnet → NIC → VM chain is four deep, and in 26 rows the last
    box is dropped while the caption goes on counting it. A map that quietly
    leaves nodes out claims a completeness it does not have.
    """
    from altus.render.cells import draw_graph
    from altus.render.palette import DARK

    provider = FakeAzure({"graph": {"data": TOPOLOGY_ROWS}})
    outcome = await tool("azure_topology").run({}, context(tmp_path, provider))
    assert outcome.visual is not None

    def render(height: int) -> str:
        grid = draw_graph(outcome.visual, width=96, height=height, palette=DARK)
        return "\n".join("".join(cell.char for cell in row) for row in grid.rows)

    cramped = render(26)
    assert "web1" not in cramped  # the machine did not fit
    assert "not shown" in cramped  # and the map admits it

    roomy = render(40)
    assert "web1" in roomy
    assert "not shown" not in roomy


# ------------------------------------------------------------------ mutations

WHAT_IF_RESULT = {
    "status": "Succeeded",
    "properties": {
        "changes": [
            {
                "resourceId": VM_ID,
                "changeType": "Modify",
                "delta": [
                    {
                        "path": "properties.hardwareProfile.vmSize",
                        "propertyChangeType": "Modify",
                        "before": "Standard_D2s_v3",
                        "after": "Standard_D4s_v3",
                    }
                ],
            }
        ]
    },
}


class WritableAzure(FakeAzure):
    """FakeAzure plus the preflight surfaces, each independently steerable.

    Separate because the failure that matters is a change escaping *before* an
    approval, and that is only visible if every mutating path records itself.
    """

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.what_if_result: dict[str, Any] | None = WHAT_IF_RESULT
        self.what_if_error = ""
        self.what_if_calls = 0

    async def what_if(self, scope: str, resource: dict[str, Any], **kw: Any) -> dict[str, Any]:
        self.what_if_calls += 1
        if self.what_if_error:
            raise az.ArmError(403, "AuthorizationFailed", self.what_if_error)
        return dict(self.what_if_result or {})

    @property
    def changing(self) -> list[tuple[str, str]]:
        """Requests that were not reads. The deny sweep asserts this is empty."""
        return [(m, p) for m, p in self.requests if m != "GET"]


def deny() -> Any:
    from altus.tools.approval import Decision, RecordingPolicy

    return RecordingPolicy(decision=Decision.DENY)


def allow() -> Any:
    from altus.tools.approval import Decision, RecordingPolicy

    return RecordingPolicy(decision=Decision.ALLOW)


async def test_the_deny_sweep(tmp_path: Any) -> None:
    """The assertion that caught most of the Kubernetes and AWS bugs.

    Every mutating tool, refused: not one of them may have issued anything but
    a read. A change that escapes before the approval returns is the single
    failure this whole layer exists to prevent.
    """
    cases: list[tuple[str, dict[str, Any]]] = [
        ("azure_write", {"id": VM_ID, "body": {"location": "westeurope"}}),
        ("azure_delete", {"id": VM_ID}),
        ("azure_action", {"id": VM_ID, "action": "start"}),
    ]
    for name, args in cases:
        provider = WritableAzure()
        policy = deny()
        outcome = await tool(name).run(args, context(tmp_path, provider, approvals=policy))
        assert outcome.is_error or "rejected" in outcome.content.casefold(), name
        assert provider.changing == [], f"{name} issued {provider.changing}"
        assert len(policy.seen) == 1, name


async def test_write_puts_a_real_diff_in_the_prompt(tmp_path: Any) -> None:
    """What-If is why the Azure gate can promise more than the AWS one."""
    provider = WritableAzure()
    policy = allow()
    outcome = await tool("azure_write").run(
        {
            "id": VM_ID,
            "body": {"properties": {"hardwareProfile": {"vmSize": "Standard_D4s_v3"}}},
            "reason": "the box is undersized",
        },
        context(tmp_path, provider, approvals=policy),
    )
    assert not outcome.is_error
    assert provider.what_if_calls == 1

    request = policy.seen[0]
    assert "What-If ran" in request.dry_run
    assert "1 Modify" in request.dry_run
    assert "no preview exists" not in request.dry_run
    assert "Standard_D2s_v3" in request.diff and "Standard_D4s_v3" in request.diff
    assert "the box is undersized" in request.diff
    # And only after the approval returned.
    assert provider.changing == [("PUT", VM_ID)]


async def test_a_what_if_that_could_not_run_never_reads_as_no_changes(tmp_path: Any) -> None:
    """The one sentence the prompt must never get wrong.

    Plenty of identities can write a resource and cannot ask what writing it
    would do. Reporting that as "no changes" would buy false confidence at
    exactly the moment of consent.
    """
    provider = WritableAzure()
    provider.what_if_error = "you may not run What-If here"
    policy = allow()
    await tool("azure_write").run(
        {"id": VM_ID, "body": {"location": "westeurope"}},
        context(tmp_path, provider, approvals=policy),
    )
    preflight = policy.seen[0].dry_run
    assert "could not run" in preflight
    assert "not known in advance" in preflight
    assert "no changes" not in preflight


async def test_what_if_reporting_no_changes_says_exactly_that(tmp_path: Any) -> None:
    provider = WritableAzure()
    provider.what_if_result = {"properties": {"changes": []}}
    policy = allow()
    await tool("azure_write").run(
        {"id": VM_ID, "body": {"location": "westeurope"}},
        context(tmp_path, provider, approvals=policy),
    )
    assert "reported no changes" in policy.seen[0].dry_run


async def test_delete_and_action_admit_there_is_no_preview(tmp_path: Any) -> None:
    for name, args in (
        ("azure_delete", {"id": VM_ID}),
        ("azure_action", {"id": VM_ID, "action": "start"}),
    ):
        provider = WritableAzure()
        policy = allow()
        await tool(name).run(args, context(tmp_path, provider, approvals=policy))
        preflight = policy.seen[0].dry_run
        assert "no preview exists" in preflight, name
        assert "What-If" not in preflight, name
        assert provider.what_if_calls == 0, name


async def test_a_lock_refuses_before_anyone_is_asked(tmp_path: Any) -> None:
    """A lock means the call will fail, so prompting would spend the user's
    attention on a decision that does not exist. AWS had no equivalent check
    at all."""
    provider = WritableAzure()
    provider.locks_found = [
        {"name": "prod-guard", "level": "CanNotDelete", "notes": "", "scope": "/rg"}
    ]
    policy = allow()
    outcome = await tool("azure_delete").run(
        {"id": VM_ID}, context(tmp_path, provider, approvals=policy)
    )
    assert outcome.is_error
    assert "prod-guard" in outcome.content
    assert outcome.summary == "locked"
    assert policy.seen == []
    assert provider.changing == []


async def test_an_unreadable_lock_is_not_treated_as_a_lock(tmp_path: Any) -> None:
    """Plenty of working identities cannot list locks, and refusing on "I could
    not check" would make the tool useless on those."""

    class NoLockAccess(WritableAzure):
        async def locks(self, scope: str) -> list[dict[str, str]]:
            raise az.ArmError(403, "AuthorizationFailed", "denied")

    provider = NoLockAccess()
    policy = allow()
    outcome = await tool("azure_delete").run(
        {"id": VM_ID}, context(tmp_path, provider, approvals=policy)
    )
    assert not outcome.is_error
    assert "lock check could not run" in policy.seen[0].dry_run


async def test_rbac_refusal_stops_before_the_prompt(tmp_path: Any) -> None:
    provider = WritableAzure()
    provider.access = "NotAllowed"
    policy = allow()
    outcome = await tool("azure_delete").run(
        {"id": VM_ID}, context(tmp_path, provider, approvals=policy)
    )
    assert outcome.is_error
    assert outcome.summary == "refused"
    assert policy.seen == []
    assert provider.changing == []


async def test_a_delete_of_something_stateful_demands_the_name_typed(tmp_path: Any) -> None:
    """PRIVILEGED means a typed confirmation and forbids a standing grant --- a
    blanket `allow always` on azure_delete would be indistinguishable from
    having no gate."""
    provider = WritableAzure()
    policy = allow()
    await tool("azure_delete").run({"id": VM_ID}, context(tmp_path, provider, approvals=policy))
    request = policy.seen[0]
    assert request.sensitivity is Sensitivity.PRIVILEGED
    assert request.needs_challenge
    assert not request.may_grant_always


async def test_a_protected_subscription_escalates_an_ordinary_change(tmp_path: Any) -> None:
    from altus.cloud.base import ProtectionRules

    provider = WritableAzure()
    policy = allow()
    rules = ProtectionRules.build([], ["sub-0000"], "confirm")
    await tool("azure_action").run(
        {"id": VM_ID, "action": "start"},
        context(tmp_path, provider, approvals=policy, protection=rules),
    )
    assert policy.seen[0].protected
    assert policy.seen[0].needs_challenge


async def test_protected_deny_refuses_outright(tmp_path: Any) -> None:
    from altus.cloud.base import ProtectionRules

    provider = WritableAzure()
    policy = allow()
    rules = ProtectionRules.build([], ["sub-0000"], "deny")
    outcome = await tool("azure_action").run(
        {"id": VM_ID, "action": "start"},
        context(tmp_path, provider, approvals=policy, protection=rules),
    )
    assert outcome.is_error
    assert policy.seen == []
    assert provider.changing == []


async def test_a_bad_id_is_reported_before_anything_is_sent(tmp_path: Any) -> None:
    provider = WritableAzure()
    outcome = await tool("azure_delete").run({"id": "web1"}, context(tmp_path, provider))
    assert outcome.is_error
    assert provider.requests == []


ROLE_ASSIGNMENT_ID = (
    "/subscriptions/sub-0000/resourceGroups/rg/providers"
    "/Microsoft.Authorization/roleAssignments/ra1"
)


async def test_rbac_writes_can_be_switched_off_at_the_gate(tmp_path: Any) -> None:
    """It cannot work by withholding a tool --- the same azure_write sets a tag
    and a role assignment --- so it is checked where the decision is made."""

    class KnowsRoleAssignments(WritableAzure):
        KNOWN_TYPES: ClassVar[dict[str, str]] = {"roleassignments": "2022-04-01"}

    provider = KnowsRoleAssignments()
    policy = allow()
    settings = AzureSettings(allow_rbac_writes=False)
    outcome = await tool("azure_write").run(
        {"id": ROLE_ASSIGNMENT_ID, "body": {"properties": {}}},
        context(tmp_path, provider, settings=settings, approvals=policy),
    )
    assert outcome.is_error
    assert "[cloud.azure]" in outcome.content
    assert provider.changing == []

    # And the same tool still writes an ordinary resource.
    provider = WritableAzure()
    outcome = await tool("azure_write").run(
        {"id": VM_ID, "body": {"location": "westeurope"}},
        context(tmp_path, provider, settings=settings, approvals=allow()),
    )
    assert not outcome.is_error


@pytest.mark.parametrize(
    ("settings", "present", "absent"),
    [
        (AzureSettings(), {"azure_write", "azure_delete", "azure_action"}, set()),
        (AzureSettings(allow_delete=False), {"azure_write", "azure_action"}, {"azure_delete"}),
        (
            AzureSettings(allow_writes=False),
            {"azure_get", "azure_query"},
            {"azure_write", "azure_delete", "azure_action"},
        ),
    ],
)
def test_each_switch_removes_exactly_its_tools(
    settings: AzureSettings, present: set[str], absent: set[str]
) -> None:
    """A class that is off is never registered, so the model is not told it
    exists --- deliberately stronger than refusing at call time."""
    names = {t.name for t in azure_tools(settings)}
    assert present <= names
    assert not (absent & names)


def test_a_read_only_registry_carries_no_azure_change(tmp_path: Any) -> None:
    """`writes=False` has to mean no writes, not no file writes. The AWS work
    found a read-only registry still carrying aws_write."""
    registry = default_registry(
        writes=False, kubernetes=False, aws=False, azure=True, cloud=CloudSettings()
    )
    assert "azure_get" in registry
    for name in ("azure_write", "azure_delete", "azure_action"):
        assert name not in registry


# ------------------------------------------------------- the az CLI fallback


def az_tool() -> Any:
    from altus.tools.cli import AzureCliTool

    return AzureCliTool()


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (["vm", "list"], Sensitivity.READ),
        (["vm", "show"], Sensitivity.READ),
        (["account", "list"], Sensitivity.READ),
        (["network", "nsg", "list"], Sensitivity.READ),
        # Reads shaped like reads that hand back a live credential.
        (["storage", "account", "keys", "list"], Sensitivity.SENSITIVE_READ),
        (["storage", "account", "show-connection-string"], Sensitivity.SENSITIVE_READ),
        (["keyvault", "secret", "show"], Sensitivity.SENSITIVE_READ),
        (["aks", "get-credentials"], Sensitivity.SENSITIVE_READ),
        # Ordinary changes.
        (["vm", "create"], Sensitivity.MUTATE),
        (["vm", "start"], Sensitivity.MUTATE),
        (["vm", "restart"], Sensitivity.MUTATE),
        (["tag", "update"], Sensitivity.MUTATE),
        # The dangerous end.
        (["vm", "delete"], Sensitivity.PRIVILEGED),
        (["group", "delete"], Sensitivity.PRIVILEGED),
        (["role", "assignment", "create"], Sensitivity.PRIVILEGED),
        (["lock", "delete"], Sensitivity.PRIVILEGED),
        (["network", "nsg", "rule", "create"], Sensitivity.PRIVILEGED),
        (["storage", "account", "keys", "renew"], Sensitivity.PRIVILEGED),
    ],
)
def test_az_classification(command: list[str], expected: Sensitivity) -> None:
    assert az_tool().classify(command) is expected


@pytest.mark.parametrize(
    "command",
    [
        ["login"],
        ["ad", "sp", "create-for-rbac"],
        ["logout"],
        ["brand-new-service", "list"],
        ["vm"],
    ],
)
def test_an_unmapped_az_group_fails_closed(command: list[str]) -> None:
    """The table is a short allowlist, not an attempt at completeness. That is
    what catches `az login`, `az ad ...`, and every command a future CLI
    release adds."""
    assert az_tool().classify(command) is Sensitivity.PRIVILEGED


def test_an_unmapped_verb_under_a_known_group_is_not_escalated() -> None:
    """A considered departure from the plan, for the reason the AWS work
    measured: a challenge that fires on everything stops working. An unknown
    verb becomes an ARM action, which still prompts."""
    assert az_tool().classify(["vm", "redeploy"]) is Sensitivity.MUTATE
    assert az_tool().classify(["vm", "regenerate-key"]) is Sensitivity.PRIVILEGED


@pytest.mark.parametrize(
    ("command", "operation"),
    [
        (["vm", "delete"], "Microsoft.Compute/virtualMachines/delete"),
        (["vm", "start"], "Microsoft.Compute/virtualMachines/start/action"),
        (["vm", "list"], "Microsoft.Compute/virtualMachines/read"),
        (["role", "assignment", "create"], "Microsoft.Authorization/roleAssignments/write"),
        (["keyvault", "secret", "show"], "Microsoft.KeyVault/vaults/secrets/read"),
        (["sql", "db", "delete"], "Microsoft.Sql/servers/databases/delete"),
    ],
)
def test_the_cli_and_the_sdk_agree(command: list[str], operation: str) -> None:
    """One command must not get two different answers depending on which door
    it came through. This is the test that would have caught the AWS drift bug
    had it existed then."""
    assert az_tool().classify(command) is az.classify(operation)


async def test_az_refuses_a_subscription_it_was_handed(tmp_path: Any) -> None:
    """One credential sees many subscriptions and Altus acts in exactly one, so
    a command that picks its own would be approved against a target it is not
    going to touch."""
    ctx = context(tmp_path, FakeAzure())
    ctx.cloud.cli_allowlist = ("az",)
    outcome = await az_tool().run({"args": ["vm", "list", "--subscription", "sub-9999"]}, ctx)
    assert outcome.is_error
    assert outcome.summary == "reserved flag"
    assert "--subscription" in outcome.content


async def test_az_is_handed_the_session_subscription(tmp_path: Any, monkeypatch: Any) -> None:
    """Supplied by Altus, the way kubectl is given --context.

    Both the binary lookup and the exec are stubbed so this asserts on every
    machine rather than skipping wherever the CLI happens not to be installed.
    """
    import altus.tools.cli as cli_module

    seen: list[list[str]] = []

    async def fake_execute(path: str, argv: list[str], **kw: Any) -> tuple[int, str, str]:
        seen.append(argv)
        return 0, "[]", ""

    monkeypatch.setattr(cli_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cli_module, "_execute", fake_execute)

    ctx = context(tmp_path, FakeAzure())
    ctx.cloud.cli_allowlist = ("az",)
    outcome = await az_tool().run({"args": ["vm", "list"]}, ctx)
    assert not outcome.is_error
    assert seen == [["vm", "list", "--subscription", "sub-0000"]]


def test_az_can_be_switched_off_without_taking_kubectl(tmp_path: Any) -> None:
    """Kubernetes gates the whole CLI layer because that switch predates the
    others. Azure only gates its own binary."""
    settings = CloudSettings()
    settings.azure.allow_cli = False
    names = default_registry(kubernetes=False, aws=False, azure=False, cloud=settings).names
    assert "azure_cli" not in names
    assert "k8s_kubectl" in names


# ------------------------------------------------------------ the front door


def test_the_azure_dashboard_uses_the_azure_panels() -> None:
    from altus.tui.screens.dashboard import AZURE_PANELS, PANELS_BY_CLOUD, DashboardScreen

    screen = DashboardScreen("sub-0000", cloud="azure")
    assert screen.panels == AZURE_PANELS
    assert {tool for _title, tool, _args in AZURE_PANELS} <= {
        "azure_topology",
        "azure_inventory",
        "azure_cost",
        "azure_whoami",
    }
    assert set(PANELS_BY_CLOUD) == {"k8s", "aws", "azure", "gcp"}


def test_only_kubernetes_panels_take_a_scope_argument() -> None:
    """AWS reads its region and Azure its subscription from the session, so
    passing one per panel would send an argument the tool does not declare."""
    from altus.tui.screens.dashboard import AZURE_PANELS

    assert all(args == {} for _title, _tool, args in AZURE_PANELS)
