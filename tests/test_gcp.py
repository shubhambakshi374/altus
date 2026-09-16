"""GCP. No project is contacted.

Google ships its API contracts on disk --- 600 discovery documents covering
335 APIs --- so the classification tests sweep a real corpus, the way
``test_aws.py`` sweeps botocore's. Azure needed a generated grammar because its
catalog is a network call; GCP does not.

Anything that would reach the network goes through a stub in the shape of
``tests/test_azure.py``'s FakeAzure.
"""

from __future__ import annotations

import functools

import pytest

from altus.cloud import gcp
from altus.cloud.base import Sensitivity

PROJECT_SCOPE = "projects/demo-project"
FOLDER_SCOPE = "folders/12345"
ORG_SCOPE = "organizations/98765"


@functools.cache
def corpus() -> tuple[str, ...]:
    """Every distinct method id across every shipped document.

    Distinct: an API ships several versions and the same id appears in each, so
    the raw entry count (26,438) double-counts. What matters for classification
    is the set of ids, which is smaller.
    """
    found: set[str] = set()
    for api, versions in gcp._catalog().items():
        for version in versions:
            try:
                found.update(gcp.method_index(api, version))
            except Exception:
                continue
    return tuple(sorted(found))


def test_the_corpus_ships_on_disk() -> None:
    """The fact the whole approach rests on. If this ever fails, the classifier
    is guessing rather than parsing."""
    assert len(gcp.available_apis()) > 300
    assert len(corpus()) > 15_000
    assert "compute.instances.delete" in corpus()


# -------------------------------------------------------------- introspection


def test_discovery_introspection_is_real() -> None:
    """gcp_explain reads Compute's actual shipped document, not a fixture."""
    described = gcp.describe_method("compute.instances.delete")
    assert described["http_method"] == "DELETE"
    assert described["sensitivity"] == "privileged"
    assert {"project", "zone", "instance"} <= set(described["parameters"])
    assert described["parameters"]["instance"]["required"]
    assert "delete" in described["documentation"].casefold()


def test_an_unknown_method_is_reported_not_invented() -> None:
    with pytest.raises(LookupError):
        gcp.describe_method("compute.instances.frobnicate")
    assert gcp.lookup("nosuchapi.things.get") is None


def test_validate_only_is_asked_of_the_document() -> None:
    """1.9% of methods take one, so assuming it from the service would mean
    either a skipped preflight or a parameter error."""
    assert gcp.supports_validate_only("compute.instances.delete") == ""
    withit = [m for m in corpus()[:6000] if gcp.supports_validate_only(m)]
    assert withit, "no method in the sample declares a validateOnly parameter"


def test_the_preview_really_is_the_weakest_of_the_four() -> None:
    """Measured, not assumed --- it is what the approval prompt has to admit.

    AWS could preview 4.3% of its operations and said so; GCP can preview
    fewer, and the prompt says so in turn.
    """
    sample = corpus()
    covered = sum(1 for m in sample if gcp.supports_validate_only(m))
    assert covered / len(sample) < 0.05


# --------------------------------------------------------------- classification


@pytest.mark.parametrize(
    ("method_id", "expected"),
    [
        # Ordinary reads stay reads --- reading the IAM policy is how you
        # understand a project.
        ("compute.instances.list", Sensitivity.READ),
        ("compute.instances.get", Sensitivity.READ),
        ("storage.buckets.getIamPolicy", Sensitivity.READ),
        ("compute.instances.testIamPermissions", Sensitivity.READ),
        ("cloudresourcemanager.projects.get", Sensitivity.READ),
        # Reads that hand back secret material.
        ("secretmanager.projects.secrets.versions.access", Sensitivity.SENSITIVE_READ),
        ("container.projects.locations.clusters.get", Sensitivity.SENSITIVE_READ),
        # setIamPolicy, anywhere, on anything.
        ("storage.buckets.setIamPolicy", Sensitivity.PRIVILEGED),
        ("compute.instances.setIamPolicy", Sensitivity.PRIVILEGED),
        ("cloudresourcemanager.projects.setIamPolicy", Sensitivity.PRIVILEGED),
        # Credential minting.
        ("iam.projects.serviceAccounts.keys.create", Sensitivity.PRIVILEGED),
        ("iamcredentials.projects.serviceAccounts.generateAccessToken", Sensitivity.PRIVILEGED),
        # Identity-service writes.
        ("iam.projects.serviceAccounts.create", Sensitivity.PRIVILEGED),
        ("cloudkms.projects.locations.keyRings.cryptoKeys.create", Sensitivity.PRIVILEGED),
        # Network exposure.
        ("compute.firewalls.insert", Sensitivity.PRIVILEGED),
        ("compute.instances.addAccessConfig", Sensitivity.PRIVILEGED),
        # Irreversible destruction of something stateful.
        ("compute.instances.delete", Sensitivity.PRIVILEGED),
        ("storage.buckets.delete", Sensitivity.PRIVILEGED),
        # Ordinary changes.
        ("compute.instances.insert", Sensitivity.MUTATE),
        ("compute.instances.setLabels", Sensitivity.MUTATE),
        ("compute.instances.start", Sensitivity.MUTATE),
        ("compute.instances.stop", Sensitivity.MUTATE),
    ],
)
def test_classification(method_id: str, expected: Sensitivity) -> None:
    assert gcp.classify(method_id, PROJECT_SCOPE) is expected


def test_an_unknown_method_fails_closed() -> None:
    """A typo, or an API newer than the pinned client. Guessing low there is
    unrecoverable, so it takes the strictest level rather than the second."""
    for method_id in ("compute.instances.frobnicate", "brandnewapi.things.delete", "", "nonsense"):
        assert gcp.classify(method_id, PROJECT_SCOPE) is Sensitivity.PRIVILEGED


@pytest.mark.parametrize(
    ("scope", "level"),
    [
        (PROJECT_SCOPE, "project"),
        (FOLDER_SCOPE, "folder"),
        (ORG_SCOPE, "organization"),
        ("", "unknown"),
    ],
)
def test_scope_level(scope: str, level: str) -> None:
    assert gcp.scope_level(scope) == level


def test_scope_escalates_the_same_method() -> None:
    """The axis AWS did not have and Azure did: the same insert creates one
    firewall rule in a project and reaches every project under a folder."""
    method = "compute.instances.setLabels"
    assert gcp.classify(method, PROJECT_SCOPE) is Sensitivity.MUTATE
    assert gcp.classify(method, FOLDER_SCOPE) is Sensitivity.PRIVILEGED
    assert gcp.classify(method, ORG_SCOPE) is Sensitivity.PRIVILEGED


# ------------------------------------------------------------------ the sweep


def test_no_change_is_ever_classified_read() -> None:
    """The invariant the whole corpus sweep exists to protect.

    A non-GET method that is not one of the known POST reads must never come
    back READ --- that would be a change made with nobody asked.
    """
    offenders = []
    for method_id in corpus():
        found = gcp.lookup(method_id)
        if found is None or str(found.get("httpMethod", "")).upper() == "GET":
            continue
        if method_id.rsplit(".", 1)[-1] in gcp.POST_READ_VERBS:
            continue
        if gcp.classify(method_id, PROJECT_SCOPE) is Sensitivity.READ:
            offenders.append(method_id)
    assert offenders == []


def test_the_five_get_methods_that_actually_write() -> None:
    """Measured across all 10,987 GET methods: exactly five have a write-shaped
    name. The measurement that found them is the test that keeps them pinned."""
    for method_id in gcp.GET_BUT_WRITES:
        assert gcp.lookup(method_id) is not None, f"{method_id} left the corpus"
        assert str(gcp.lookup(method_id)["httpMethod"]).upper() == "GET"
        assert gcp.classify(method_id, PROJECT_SCOPE) is not Sensitivity.READ


def test_every_set_iam_policy_is_privileged() -> None:
    """It is how a bucket becomes world-readable and how anyone grants
    themselves owner. Never merely MUTATE, on any service."""
    seen = 0
    for method_id in corpus():
        if not method_id.endswith(".setIamPolicy"):
            continue
        seen += 1
        assert gcp.classify(method_id, PROJECT_SCOPE) is Sensitivity.PRIVILEGED, method_id
    assert seen > 200


def test_every_credential_minting_method_is_privileged() -> None:
    for method_id in gcp.CREDENTIAL_MINTING:
        assert gcp.lookup(method_id) is not None, f"{method_id} left the corpus"
        assert gcp.classify(method_id, PROJECT_SCOPE) is Sensitivity.PRIVILEGED


def test_the_challenge_still_means_something() -> None:
    """The AWS lesson, restated as an assertion.

    There, every Delete* came out privileged --- 2,281 of them including
    DeleteTag --- and a challenge that fires on everything trains people to
    type through it. The first cut of this classifier matched the stateful
    pattern against the whole method path, and since nearly every GCP id
    contains `projects`, 1,332 of 1,809 deletes were privileged, including
    `operations.delete`, which removes a bookkeeping record.
    """
    deletes = [m for m in corpus() if m.rsplit(".", 1)[-1] == "delete"]
    privileged = [m for m in deletes if gcp.classify(m, PROJECT_SCOPE) is Sensitivity.PRIVILEGED]
    assert len(privileged) / len(deletes) < 0.30

    # The specific shapes that went wrong, pinned.
    assert gcp.classify("compute.instances.delete", PROJECT_SCOPE) is Sensitivity.PRIVILEGED
    assert (
        gcp.classify("agentregistry.projects.locations.operations.delete", PROJECT_SCOPE)
        is Sensitivity.MUTATE
    )


def test_privileged_stays_a_small_minority() -> None:
    """Measured at 5.7%, against AWS's 6.2% --- deliberately the same ballpark,
    because the two gates cost the user the same thing."""
    counts: dict[Sensitivity, int] = dict.fromkeys(Sensitivity, 0)
    for method_id in corpus():
        counts[gcp.classify(method_id, PROJECT_SCOPE)] += 1
    total = sum(counts.values())
    assert counts[Sensitivity.PRIVILEGED] / total < 0.10
    assert all(counts[level] > 0 for level in Sensitivity)


# ------------------------------------------------------------------ plumbing


@pytest.mark.parametrize(
    ("method_id", "permission"),
    [
        ("compute.instances.delete", "compute.instances.delete"),
        ("storage.buckets.get", "storage.buckets.get"),
        ("iam.projects.serviceAccounts.keys.create", "iam.keys.create"),
        (
            "secretmanager.projects.locations.secrets.versions.access",
            "secretmanager.versions.access",
        ),
    ],
)
def test_permission_for(method_id: str, permission: str) -> None:
    """GCP's permission strings read almost exactly like its method ids, which
    is what makes testIamPermissions usable as a preflight at all."""
    assert gcp.permission_for(method_id) == permission


def test_target_for_feeds_the_protection_rules() -> None:
    """Protecting a project by id starts working with no new config --- the
    third cloud in a row that ProtectedSettings.accounts has covered for free."""
    from altus.cloud.base import ProtectionRules

    rules = ProtectionRules.build([], ["demo-project"], "confirm")
    assert rules.matches(gcp.target_for("demo-project", "europe-west1", "europe-west1-b"))
    assert not rules.matches(gcp.target_for("other-project", "europe-west1"))


# ------------------------------------------------------------------ the tools

from typing import Any  # noqa: E402

from altus.config.models import CloudSettings, GcpSettings  # noqa: E402
from altus.tools import default_registry  # noqa: E402
from altus.tools.base import CloudContext, ToolContext  # noqa: E402
from altus.tools.gcp import gcp_tools  # noqa: E402
from altus.workspace import Workspace  # noqa: E402

IDENTITY = {"account": "dev@example.com", "project": "demo-project", "source": "Credentials"}


class FakeGcp:
    """A GcpProvider's shape, recording every call."""

    def __init__(self, responses: dict[str, Any] | None = None, *, fail: str = "") -> None:
        self.responses = responses or {}
        self.fail = fail
        self.project = "demo-project"
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.permissions: list[str] = []
        self.asset_error: Exception | None = None

    def default_project(self) -> str:
        return self.project

    async def whoami(self) -> dict[str, str]:
        return dict(IDENTITY)

    async def projects(self) -> list[dict[str, str]]:
        return [
            {"id": "demo-project", "name": "Demo", "state": "ACTIVE"},
            {"id": "prod-project", "name": "Prod", "state": "ACTIVE"},
        ]

    async def call(self, method_id: str, params: dict[str, Any] | None = None, **kw: Any) -> Any:
        self.calls.append((method_id, dict(params or {})))
        if self.fail and self.fail == method_id:
            raise RuntimeError("boom")
        if method_id.endswith(".testIamPermissions"):
            return {"permissions": list(self.permissions)}
        return dict(self.responses.get(method_id, {}))

    async def assets(self, query: str = "", asset_types: Any = None, scope: str = "") -> Any:
        self.calls.append(("cloudasset.resources.searchAll", {"query": query, "scope": scope}))
        if self.asset_error is not None:
            raise self.asset_error
        return dict(self.responses.get("assets", {"results": []}))

    async def test_permissions(self, method_id: str, resource: str) -> list[str]:
        self.calls.append((f"{method_id}#test", {"resource": resource}))
        return list(self.permissions)

    @property
    def changing(self) -> list[str]:
        """Calls that were not reads. The deny sweep asserts this is empty."""
        return [
            m
            for m, _p in self.calls
            if not m.endswith("#test")
            and gcp.classify(m) not in (Sensitivity.READ, Sensitivity.SENSITIVE_READ)
        ]


def context(
    tmp_path: Any,
    provider: FakeGcp | None = None,
    *,
    settings: GcpSettings | None = None,
    protection: Any = None,
    approvals: Any = None,
) -> ToolContext:
    from altus.tools.approval import AllowAll

    return ToolContext(
        workspace=Workspace([tmp_path]),
        approvals=approvals or AllowAll(),
        cloud=CloudContext(
            gcp=provider,
            gcp_project="demo-project",
            gcp_settings=settings or GcpSettings(),
            protection=protection,
        ),
    )


def tool(name: str) -> Any:
    found = next((t for t in gcp_tools() if t.name == name), None)
    assert found is not None, name
    return found


async def test_whoami_names_the_project(tmp_path: Any) -> None:
    outcome = await tool("gcp_whoami").run({}, context(tmp_path, FakeGcp()))
    assert "demo-project" in outcome.content
    assert "dev@example.com" in outcome.content
    assert "PROTECTED" not in outcome.content


async def test_whoami_warns_when_the_project_is_protected(tmp_path: Any) -> None:
    from altus.cloud.base import ProtectionRules

    rules = ProtectionRules.build([], ["demo-project"], "confirm")
    outcome = await tool("gcp_whoami").run({}, context(tmp_path, FakeGcp(), protection=rules))
    assert "PROTECTED" in outcome.content


async def test_tools_refuse_clearly_without_a_session(tmp_path: Any) -> None:
    outcome = await tool("gcp_whoami").run({}, context(tmp_path, None))
    assert outcome.is_error
    assert "uv sync --extra gcp" in outcome.content


async def test_projects_marks_the_active_one(tmp_path: Any) -> None:
    outcome = await tool("gcp_projects").run({}, context(tmp_path, FakeGcp()))
    assert "prod-project" in outcome.content
    assert "→" in outcome.content


async def test_apis_lists_what_actually_ships(tmp_path: Any) -> None:
    outcome = await tool("gcp_apis").run({"filter": "compute"}, context(tmp_path, FakeGcp()))
    assert "compute" in outcome.content


async def test_explain_reads_the_real_document(tmp_path: Any) -> None:
    """Not a fixture: this is Compute's own shipped contract."""
    outcome = await tool("gcp_explain").run(
        {"method": "compute.instances.delete"}, context(tmp_path, FakeGcp())
    )
    assert "privileged" in outcome.content
    assert "DELETE" in outcome.content
    assert "* instance" in outcome.content
    assert "dry run: not available" in outcome.content


async def test_explain_lists_a_service(tmp_path: Any) -> None:
    outcome = await tool("gcp_explain").run(
        {"api": "compute", "filter": "instances.get"}, context(tmp_path, FakeGcp())
    )
    assert "compute.instances.get" in outcome.content
    assert outcome.visual is not None


async def test_explain_reports_an_unknown_method(tmp_path: Any) -> None:
    outcome = await tool("gcp_explain").run(
        {"method": "compute.instances.frobnicate"}, context(tmp_path, FakeGcp())
    )
    assert outcome.is_error
    assert "gcp_explain" in outcome.content


async def test_call_refuses_anything_that_changes_something(tmp_path: Any) -> None:
    provider = FakeGcp()
    outcome = await tool("gcp_call").run(
        {"method": "compute.instances.delete"}, context(tmp_path, provider)
    )
    assert outcome.is_error
    assert "gcp_write" in outcome.content
    assert provider.calls == []


async def test_call_supplies_the_project_the_session_already_knows(tmp_path: Any) -> None:
    provider = FakeGcp({"compute.instances.list": {"items": []}})
    await tool("gcp_call").run({"method": "compute.instances.list"}, context(tmp_path, provider))
    assert provider.calls[0][1]["project"] == "demo-project"


async def test_call_does_not_invent_a_parameter_the_method_lacks(tmp_path: Any) -> None:
    """cloudresourcemanager.projects.search takes no `project`, and sending one
    is a 400 rather than a helpful default."""
    provider = FakeGcp()
    await tool("gcp_call").run(
        {"method": "cloudresourcemanager.projects.search"}, context(tmp_path, provider)
    )
    assert "project" not in provider.calls[0][1]


async def test_call_redacts_before_the_model_sees_it(tmp_path: Any) -> None:
    from altus.cloud.redact import MARKER

    provider = FakeGcp({"compute.instances.list": {"items": [{"password": "hunter2"}]}})
    outcome = await tool("gcp_call").run(
        {"method": "compute.instances.list"}, context(tmp_path, provider)
    )
    assert "hunter2" not in outcome.content
    assert MARKER in outcome.content


async def test_can_i_reports_each_decision(tmp_path: Any) -> None:
    provider = FakeGcp()
    provider.permissions = ["compute.instances.delete"]
    outcome = await tool("gcp_can_i").run(
        {"permissions": ["compute.instances.delete", "storage.buckets.setIamPolicy"]},
        context(tmp_path, provider),
    )
    assert "allowed" in outcome.content
    assert "denied" in outcome.content
    assert outcome.summary == "1/2 allowed"


async def test_assets_explains_a_disabled_api_rather_than_returning_nothing(
    tmp_path: Any,
) -> None:
    """Cloud Asset is off by default on most projects and the raw 403 does not
    say so. The hint is the difference between "broken" and "one command away"."""
    provider = FakeGcp()
    provider.asset_error = RuntimeError(
        "Cloud Asset API has not been used in project demo-project before or it is disabled"
    )
    outcome = await tool("gcp_assets").run({}, context(tmp_path, provider))
    assert outcome.is_error
    assert "gcloud services enable cloudasset.googleapis.com" in outcome.content


async def test_assets_tabulates_what_it_finds(tmp_path: Any) -> None:
    provider = FakeGcp(
        {
            "assets": {
                "results": [
                    {
                        "assetType": "compute.googleapis.com/Instance",
                        "displayName": "web-1",
                        "location": "europe-west1-b",
                        "state": "RUNNING",
                    }
                ]
            }
        }
    )
    outcome = await tool("gcp_assets").run({}, context(tmp_path, provider))
    assert "web-1" in outcome.content
    assert "Instance" in outcome.content
    assert outcome.visual is not None


READ_TOOLS = {
    "gcp_whoami",
    "gcp_projects",
    "gcp_apis",
    "gcp_explain",
    "gcp_call",
    "gcp_can_i",
    "gcp_assets",
    "gcp_inventory",
    "gcp_topology",
    "gcp_cost",
    "gcp_quotas",
}


def test_read_only_is_declared_correctly_on_every_tool() -> None:
    """`read_only` is what a read-only registry filters on, so it has to be
    right on all of them --- in both directions."""
    for found in gcp_tools():
        assert found.read_only == (found.name in READ_TOOLS), found.name


def test_the_registry_registers_gcp_when_asked(tmp_path: Any) -> None:
    registry = default_registry(
        kubernetes=False, aws=False, azure=False, gcp=True, cloud=CloudSettings()
    )
    assert "gcp_call" in registry
    assert "gcp_assets" in registry
    assert "aws_call" not in registry


def test_the_registry_leaves_gcp_out_when_it_is_not(tmp_path: Any) -> None:
    """The native tools go, the CLI fallback stays --- `gcloud` is a binary on
    PATH, not the Python client, so it is gated by cli_allowlist."""
    registry = default_registry(
        kubernetes=False, aws=False, azure=False, gcp=False, mcp=False, cloud=CloudSettings()
    )
    native = {n for n in registry.names if n.startswith("gcp_")} - {"gcp_cli"}
    assert native == set()


# ------------------------------------------------------------ curated views

NETWORK_LINK = "https://www.googleapis.com/compute/v1/projects/demo-project/global/networks/vpc"
SUBNET_LINK = (
    "https://www.googleapis.com/compute/v1/projects/demo-project/regions/europe-west1"
    "/subnetworks/web"
)

TOPOLOGY = {
    "compute.networks.list": {
        "items": [{"name": "vpc", "selfLink": NETWORK_LINK, "autoCreateSubnetworks": False}]
    },
    "compute.subnetworks.aggregatedList": {
        "items": {
            "regions/europe-west1": {
                "subnetworks": [
                    {
                        "name": "web",
                        "selfLink": SUBNET_LINK,
                        "network": NETWORK_LINK,
                        "region": ".../regions/europe-west1",
                        "ipCidrRange": "10.0.1.0/24",
                    }
                ]
            }
        }
    },
    "compute.instances.aggregatedList": {
        "items": {
            "zones/europe-west1-b": {
                "instances": [
                    {
                        "name": "web-1",
                        "zone": ".../zones/europe-west1-b",
                        "status": "RUNNING",
                        "machineType": ".../machineTypes/e2-medium",
                        "networkInterfaces": [
                            {"subnetwork": SUBNET_LINK, "accessConfigs": [{"natIP": "34.1.2.3"}]}
                        ],
                    }
                ]
            }
        }
    },
    "compute.firewalls.list": {
        "items": [
            {
                "name": "allow-ssh",
                "network": NETWORK_LINK,
                "allowed": [{"IPProtocol": "tcp", "ports": ["22"]}],
            }
        ]
    },
    "compute.forwardingRules.aggregatedList": {
        "items": {
            "regions/europe-west1": {
                "subnetworks": [
                    {
                        "name": "lb",
                        "network": NETWORK_LINK,
                        "IPAddress": "35.1.2.3",
                        "region": ".../regions/europe-west1",
                    }
                ]
            }
        }
    },
}


async def test_inventory_prefers_the_asset_search(tmp_path: Any) -> None:
    provider = FakeGcp(
        {
            "assets": {
                "results": [
                    {
                        "assetType": "compute.googleapis.com/Instance",
                        "displayName": "web-1",
                        "location": "europe-west1-b",
                        "state": "RUNNING",
                    }
                ]
            }
        }
    )
    outcome = await tool("gcp_inventory").run({}, context(tmp_path, provider))
    assert "web-1" in outcome.content
    assert "Cloud Asset Inventory" in outcome.content


async def test_inventory_falls_back_and_says_that_it_did(tmp_path: Any) -> None:
    """Reporting fewer resources without explaining why is how someone
    concludes a thing is gone when it is only unlisted."""
    provider = FakeGcp(
        {
            "compute.instances.aggregatedList": {
                "items": {
                    "zones/a": {
                        "instances": [{"name": "web-1", "zone": ".../zones/a", "status": "RUNNING"}]
                    }
                }
            }
        }
    )
    provider.asset_error = RuntimeError("SERVICE_DISABLED")
    outcome = await tool("gcp_inventory").run({}, context(tmp_path, provider))
    assert "web-1" in outcome.content
    assert "Cloud Asset unavailable" in outcome.content


async def test_topology_builds_the_network_hierarchy(tmp_path: Any) -> None:
    provider = FakeGcp(TOPOLOGY)
    outcome = await tool("gcp_topology").run({}, context(tmp_path, provider))
    graph = outcome.visual
    assert graph is not None

    kinds = {n.kind for n in graph.nodes}
    assert kinds == {"Network", "Subnetwork", "Instance", "Firewall", "ForwardingRule"}
    relations = {(e.relation, e.source.split("/")[0], e.target.split("/")[0]) for e in graph.edges}
    assert ("owns", "Network", "Subnetwork") in relations
    assert ("owns", "Subnetwork", "Instance") in relations
    # The two that make it a graph rather than a tree.
    assert ("secures", "Firewall", "Network") in relations
    assert ("exposes", "ForwardingRule", "Network") in relations


async def test_topology_marks_an_instance_reachable_from_outside(tmp_path: Any) -> None:
    """An external IP is the thing you most want to see on a topology map, so
    it belongs on the node rather than buried in a field."""
    provider = FakeGcp(TOPOLOGY)
    outcome = await tool("gcp_topology").run({}, context(tmp_path, provider))
    assert outcome.visual is not None
    instance = next(n for n in outcome.visual.nodes if n.kind == "Instance")
    assert "external IP" in instance.detail


async def test_topology_nodes_carry_a_reader(tmp_path: Any) -> None:
    provider = FakeGcp(TOPOLOGY)
    outcome = await tool("gcp_topology").run({}, context(tmp_path, provider))
    assert outcome.visual is not None
    assert {n.reader for n in outcome.visual.nodes} == {"gcp_call"}
    for node in outcome.visual.nodes:
        assert node.id.count("/") == 2, node.id


async def test_cost_says_gcp_has_no_cost_api_when_nothing_is_configured(
    tmp_path: Any,
) -> None:
    """The honest answer, and the one that must never be a made-up number."""
    provider = FakeGcp(
        {
            "cloudbilling.projects.getBillingInfo": {"billingAccountName": "billingAccounts/X"},
            "billingbudgets.billingAccounts.budgets.list": {
                "budgets": [
                    {
                        "displayName": "monthly",
                        "amount": {"specifiedAmount": {"units": "500", "currencyCode": "EUR"}},
                        "thresholdRules": [{}, {}],
                    }
                ]
            },
        }
    )
    outcome = await tool("gcp_cost").run({}, context(tmp_path, provider))
    assert "GCP has no cost API" in outcome.content
    assert "billing_export_table" in outcome.content
    assert "monthly" in outcome.content  # the budget fallback still answers
    assert "bigquery.jobs.query" not in [m for m, _p in provider.calls]


async def test_cost_charts_the_billing_export_when_it_is_configured(tmp_path: Any) -> None:
    payload = {
        "schema": {"fields": [{"name": "day"}, {"name": "service"}, {"name": "cost"}]},
        "rows": [
            {"f": [{"v": "2026-09-01"}, {"v": "Compute Engine"}, {"v": "12.5"}]},
            {"f": [{"v": "2026-09-02"}, {"v": "Compute Engine"}, {"v": "13.5"}]},
            {"f": [{"v": "2026-09-01"}, {"v": "Cloud Storage"}, {"v": "1.0"}]},
        ],
    }
    provider = FakeGcp({"bigquery.jobs.query": payload})
    settings = GcpSettings(billing_export_table="demo-project.billing.gcp_billing_export_v1_ABC")
    outcome = await tool("gcp_cost").run({}, context(tmp_path, provider, settings=settings))
    chart = outcome.visual
    assert chart is not None
    assert [s.label for s in chart.series] == ["Compute Engine", "Cloud Storage"]
    assert chart.series[0].points == [12.5, 13.5]
    assert chart.series[0].timed


async def test_cost_refuses_a_table_name_that_could_break_out_of_the_query(
    tmp_path: Any,
) -> None:
    """It comes from config rather than the model, but it is interpolated into
    SQL and a name that closed the backtick would change what the query means."""
    provider = FakeGcp()
    settings = GcpSettings(billing_export_table="a.b.c` UNION SELECT * FROM `x.y.z")
    outcome = await tool("gcp_cost").run({}, context(tmp_path, provider, settings=settings))
    assert outcome.is_error
    assert provider.calls == []


async def test_quotas_plot_real_usage_against_the_ceiling(tmp_path: Any) -> None:
    """GCP reports usage and limit both, as Azure does and AWS could not."""
    provider = FakeGcp(
        {
            "compute.regions.get": {
                "quotas": [
                    {"metric": "CPUS", "usage": 90, "limit": 100},
                    {"metric": "DISKS_TOTAL_GB", "usage": 20, "limit": 500},
                    {"metric": "UNUSED", "usage": 0, "limit": 10},
                ]
            }
        }
    )
    outcome = await tool("gcp_quotas").run({"region": "europe-west1"}, context(tmp_path, provider))
    chart = outcome.visual
    assert chart is not None
    assert [b.value for b in chart.bars] == [90.0, 20.0]  # the unused one is dropped
    assert chart.bars[0].limit == 100.0  # tightest first
    assert "90%" in chart.caption


def test_the_drill_down_knows_how_to_read_a_gcp_node() -> None:
    from altus.tui.screens.detail import GCP_READERS, READERS, NodeDetail

    assert READERS["gcp_call"] == "GCP"
    screen = NodeDetail("Instance/europe-west1-b/web-1", "web-1", reader="gcp_call")
    assert screen._args(("Instance", "europe-west1-b", "web-1")) == {
        "method": "compute.instances.list",
        "params": {"zone": "europe-west1-b"},
    }
    # `global` is not a zone: sending it as one is a 400.
    assert screen._args(("Firewall", "global", "allow-ssh")) == {"method": "compute.firewalls.list"}
    assert set(GCP_READERS) >= {"Instance", "Network", "Firewall"}


# ------------------------------------------------------------------ mutations


class WritableGcp(FakeGcp):
    """FakeGcp plus the preflight surfaces, each independently steerable."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.protection: dict[str, Any] = {}
        self.liens: list[dict[str, str]] = []
        self.validate_error: Exception | None = None

    async def call(self, method_id: str, params: dict[str, Any] | None = None, **kw: Any) -> Any:
        arguments = dict(params or {})
        if arguments.get("validateOnly") or arguments.get("dryRun"):
            self.calls.append((f"{method_id}#validate", arguments))
            if self.validate_error is not None:
                raise self.validate_error
            return {}
        if method_id == "cloudresourcemanager.liens.list":
            self.calls.append((method_id, arguments))
            return {"liens": list(self.liens)}
        if method_id.endswith(".get") and self.protection:
            self.calls.append((method_id, arguments))
            return dict(self.protection)
        return await super().call(method_id, params, **kw)

    @property
    def changing(self) -> list[str]:
        """Calls that actually changed something --- a validateOnly probe does
        not count, and neither does a read."""
        out = []
        for method, _params in self.calls:
            if method.endswith(("#validate", "#test")):
                continue
            if gcp.classify(method) in (Sensitivity.READ, Sensitivity.SENSITIVE_READ):
                continue
            out.append(method)
        return out


def deny() -> Any:
    from altus.tools.approval import Decision, RecordingPolicy

    return RecordingPolicy(decision=Decision.DENY)


def allow() -> Any:
    from altus.tools.approval import Decision, RecordingPolicy

    return RecordingPolicy(decision=Decision.ALLOW)


def write_tool() -> Any:
    from altus.tools.gcp.mutations import GcpWriteTool

    return GcpWriteTool()


async def test_the_deny_sweep(tmp_path: Any) -> None:
    """The assertion that caught most of the bugs in all three previous clouds.

    Refused, gcp_write must have issued nothing but reads and preflight probes.
    A change escaping before the approval returns is the single failure this
    whole layer exists to prevent.
    """
    for args in (
        {"method": "compute.instances.delete", "params": {"zone": "z", "instance": "web-1"}},
        {"method": "compute.instances.setLabels", "params": {"zone": "z", "instance": "web-1"}},
        {"method": "storage.buckets.setIamPolicy", "params": {"bucket": "b"}},
    ):
        provider = WritableGcp()
        policy = deny()
        outcome = await write_tool().run(args, context(tmp_path, provider, approvals=policy))
        assert outcome.is_error or "rejected" in outcome.content.casefold()
        assert provider.changing == [], f"{args['method']} issued {provider.changing}"
        assert len(policy.seen) == 1


async def test_the_prompt_admits_there_is_no_preview(tmp_path: Any) -> None:
    """98% of Google's methods cannot be validated without running them, and
    the prompt has to say so rather than imply a check that never happened."""
    provider = WritableGcp()
    policy = allow()
    await write_tool().run(
        {"method": "compute.instances.delete", "params": {"zone": "z", "instance": "web-1"}},
        context(tmp_path, provider, approvals=policy),
    )
    preflight = policy.seen[0].dry_run
    assert "no preview exists" in preflight
    assert "not known in advance" in preflight
    # It may say a validation *cannot* happen; it must never say one did.
    assert "succeeded" not in preflight


async def test_a_method_that_can_be_validated_says_so_instead(tmp_path: Any) -> None:
    method = next(
        (
            m
            for m in corpus()
            if gcp.supports_validate_only(m) and gcp.classify(m) is Sensitivity.MUTATE
        ),
        "",
    )
    assert method, "no validatable mutating method in the corpus"
    provider = WritableGcp()
    policy = allow()
    await write_tool().run({"method": method}, context(tmp_path, provider, approvals=policy))
    preflight = policy.seen[0].dry_run
    assert "succeeded" in preflight
    assert "no preview exists" not in preflight
    assert any(m.endswith("#validate") for m, _p in provider.calls)


async def test_deletion_protection_refuses_before_anyone_is_asked(tmp_path: Any) -> None:
    """GCP's nearest equivalent to an Azure lock: a real "this call will fail"
    signal, so prompting would spend the user's attention on nothing."""
    provider = WritableGcp()
    provider.protection = {"name": "web-1", "deletionProtection": True}
    policy = allow()
    outcome = await write_tool().run(
        {"method": "compute.instances.delete", "params": {"zone": "z", "instance": "web-1"}},
        context(tmp_path, provider, approvals=policy),
    )
    assert outcome.is_error
    assert outcome.summary == "protected"
    assert "deletionProtection" in outcome.content
    assert policy.seen == []
    assert provider.changing == []


async def test_an_unreadable_resource_is_not_treated_as_protected(tmp_path: Any) -> None:
    """Plenty of identities may delete something they cannot read, and refusing
    on "I could not check" would make the tool useless on those."""
    provider = WritableGcp(fail="compute.instances.get")
    policy = allow()
    outcome = await write_tool().run(
        {"method": "compute.instances.delete", "params": {"zone": "z", "instance": "web-1"}},
        context(tmp_path, provider, approvals=policy),
    )
    assert not outcome.is_error
    assert "could not be checked" in policy.seen[0].dry_run


async def test_a_lien_refuses_a_project_delete(tmp_path: Any) -> None:
    provider = WritableGcp()
    provider.liens = [{"name": "liens/keep", "reason": "production"}]
    policy = allow()
    outcome = await write_tool().run(
        {
            "method": "cloudresourcemanager.projects.delete",
            "params": {"name": "projects/demo-project"},
        },
        context(tmp_path, provider, approvals=policy),
    )
    assert outcome.is_error
    assert outcome.summary == "lien"
    assert "production" in outcome.content
    assert policy.seen == []
    assert provider.changing == []


async def test_liens_are_only_asked_about_where_they_apply(tmp_path: Any) -> None:
    """Nothing but a project delete is lien-protected, so asking elsewhere
    would be a wasted call on every single change."""
    provider = WritableGcp()
    await write_tool().run(
        {"method": "compute.instances.setLabels", "params": {"zone": "z", "instance": "w"}},
        context(tmp_path, provider, approvals=allow()),
    )
    assert "cloudresourcemanager.liens.list" not in [m for m, _p in provider.calls]


async def test_the_permission_check_reaches_the_prompt(tmp_path: Any) -> None:
    provider = WritableGcp()
    provider.permissions = ["compute.instances.setLabels"]
    policy = allow()
    await write_tool().run(
        {"method": "compute.instances.setLabels", "params": {"zone": "z", "instance": "w"}},
        context(tmp_path, provider, approvals=policy),
    )
    assert "you hold compute.instances.setLabels" in policy.seen[0].dry_run


async def test_a_missing_permission_is_said_plainly(tmp_path: Any) -> None:
    provider = WritableGcp()
    provider.permissions = []
    policy = allow()
    await write_tool().run(
        {"method": "compute.instances.setLabels", "params": {"zone": "z", "instance": "w"}},
        context(tmp_path, provider, approvals=policy),
    )
    assert "do NOT hold" in policy.seen[0].dry_run


async def test_a_privileged_change_demands_the_name_typed(tmp_path: Any) -> None:
    provider = WritableGcp()
    policy = allow()
    await write_tool().run(
        {"method": "storage.buckets.setIamPolicy", "params": {"bucket": "b"}},
        context(tmp_path, provider, approvals=policy),
    )
    request = policy.seen[0]
    assert request.sensitivity is Sensitivity.PRIVILEGED
    assert request.needs_challenge
    assert not request.may_grant_always


async def test_a_protected_project_escalates_an_ordinary_change(tmp_path: Any) -> None:
    from altus.cloud.base import ProtectionRules

    provider = WritableGcp()
    policy = allow()
    rules = ProtectionRules.build([], ["demo-project"], "confirm")
    await write_tool().run(
        {"method": "compute.instances.setLabels", "params": {"zone": "z", "instance": "w"}},
        context(tmp_path, provider, approvals=policy, protection=rules),
    )
    assert policy.seen[0].protected
    assert policy.seen[0].needs_challenge


async def test_protected_deny_refuses_outright(tmp_path: Any) -> None:
    from altus.cloud.base import ProtectionRules

    provider = WritableGcp()
    policy = allow()
    rules = ProtectionRules.build([], ["demo-project"], "deny")
    outcome = await write_tool().run(
        {"method": "compute.instances.setLabels", "params": {"zone": "z", "instance": "w"}},
        context(tmp_path, provider, approvals=policy, protection=rules),
    )
    assert outcome.is_error
    assert policy.seen == []
    assert provider.changing == []


async def test_an_unknown_method_is_refused_before_anything_is_sent(tmp_path: Any) -> None:
    provider = WritableGcp()
    outcome = await write_tool().run(
        {"method": "compute.instances.frobnicate"}, context(tmp_path, provider)
    )
    assert outcome.is_error
    assert provider.calls == []


async def test_a_read_is_sent_back_to_the_tool_that_does_not_prompt(tmp_path: Any) -> None:
    provider = WritableGcp()
    outcome = await write_tool().run(
        {"method": "compute.instances.list"}, context(tmp_path, provider)
    )
    assert outcome.is_error
    assert "gcp_call" in outcome.content
    assert provider.calls == []


async def test_iam_writes_can_be_switched_off_at_the_gate(tmp_path: Any) -> None:
    """It cannot work by withholding a tool --- the same gcp_write sets a label
    and a bucket's IAM policy --- so it is checked where the decision is made."""
    provider = WritableGcp()
    settings = GcpSettings(allow_iam_writes=False)
    outcome = await write_tool().run(
        {"method": "storage.buckets.setIamPolicy", "params": {"bucket": "b"}},
        context(tmp_path, provider, settings=settings, approvals=allow()),
    )
    assert outcome.is_error
    assert "[cloud.gcp]" in outcome.content
    assert provider.changing == []

    # And the same tool still writes an ordinary resource.
    provider = WritableGcp()
    outcome = await write_tool().run(
        {"method": "compute.instances.setLabels", "params": {"zone": "z", "instance": "w"}},
        context(tmp_path, provider, settings=settings, approvals=allow()),
    )
    assert not outcome.is_error


async def test_delete_can_be_switched_off_at_the_gate(tmp_path: Any) -> None:
    provider = WritableGcp()
    settings = GcpSettings(allow_delete=False)
    outcome = await write_tool().run(
        {"method": "compute.instances.delete", "params": {"zone": "z", "instance": "w"}},
        context(tmp_path, provider, settings=settings, approvals=allow()),
    )
    assert outcome.is_error
    assert provider.changing == []


def test_allow_writes_removes_the_tool_entirely() -> None:
    """A class that is off is never registered, so the model is not told it
    exists --- deliberately stronger than refusing at call time."""
    assert "gcp_write" in {t.name for t in gcp_tools(GcpSettings())}
    assert "gcp_write" not in {t.name for t in gcp_tools(GcpSettings(allow_writes=False))}


def test_a_read_only_registry_carries_no_gcp_change(tmp_path: Any) -> None:
    registry = default_registry(
        writes=False, kubernetes=False, aws=False, azure=False, gcp=True, cloud=CloudSettings()
    )
    assert "gcp_call" in registry
    assert "gcp_write" not in registry


# ---------------------------------------------------- the gcloud CLI fallback


def gcloud_tool() -> Any:
    from altus.tools.cli import GcloudTool

    return GcloudTool()


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (["compute", "instances", "list"], Sensitivity.READ),
        (["compute", "instances", "describe"], Sensitivity.READ),
        (["projects", "list"], Sensitivity.READ),
        (["storage", "buckets", "list"], Sensitivity.READ),
        (["storage", "buckets", "get-iam-policy"], Sensitivity.READ),
        # Reads shaped like reads that hand back a live credential.
        (["container", "clusters", "get-credentials"], Sensitivity.SENSITIVE_READ),
        (["secrets", "versions", "access"], Sensitivity.SENSITIVE_READ),
        # Ordinary changes.
        (["compute", "instances", "create"], Sensitivity.MUTATE),
        (["compute", "instances", "start"], Sensitivity.MUTATE),
        (["compute", "instances", "stop"], Sensitivity.MUTATE),
        # The dangerous end.
        (["compute", "instances", "delete"], Sensitivity.PRIVILEGED),
        (["storage", "buckets", "delete"], Sensitivity.PRIVILEGED),
        (["storage", "buckets", "add-iam-policy-binding"], Sensitivity.PRIVILEGED),
        (["projects", "add-iam-policy-binding"], Sensitivity.PRIVILEGED),
        (["iam", "service-accounts", "keys", "create"], Sensitivity.PRIVILEGED),
        (["compute", "firewall-rules", "create"], Sensitivity.PRIVILEGED),
    ],
)
def test_gcloud_classification(command: list[str], expected: Sensitivity) -> None:
    assert gcloud_tool().classify(command) is expected


@pytest.mark.parametrize(
    "command",
    [
        ["auth", "login"],
        ["auth", "application-default", "login"],
        ["organizations", "list"],
        ["brand-new-service", "list"],
        ["compute"],
    ],
)
def test_an_unmapped_gcloud_group_fails_closed(command: list[str]) -> None:
    """The table is a short allowlist, not an attempt at completeness. That is
    what catches `gcloud auth`, `gcloud organizations`, and every command a
    future release adds."""
    assert gcloud_tool().classify(command) is Sensitivity.PRIVILEGED


def test_a_longer_group_wins_over_a_shorter_one() -> None:
    """Both `compute networks` and `compute networks subnets` are real groups,
    so the longest matching prefix has to win or subnets would be read as a
    verb on networks."""
    assert gcloud_tool().classify(["compute", "networks", "subnets", "list"]) is Sensitivity.READ
    assert gcloud_tool().classify(["compute", "networks", "list"]) is Sensitivity.READ


@pytest.mark.parametrize(
    ("command", "method"),
    [
        (["compute", "instances", "delete"], "compute.instances.delete"),
        (["compute", "instances", "list"], "compute.instances.list"),
        (["compute", "instances", "start"], "compute.instances.start"),
        (["storage", "buckets", "add-iam-policy-binding"], "storage.buckets.setIamPolicy"),
        (["iam", "service-accounts", "keys", "create"], "iam.projects.serviceAccounts.keys.create"),
        (["secrets", "versions", "access"], "secretmanager.projects.secrets.versions.access"),
    ],
)
def test_the_cli_and_the_api_agree(command: list[str], method: str) -> None:
    """One command must not get two different answers depending on which door
    it came through. This is the test that would have caught the AWS drift bug
    had it existed then."""
    assert gcloud_tool().classify(command) is gcp.classify(method)


@pytest.mark.parametrize(
    "flag", ["--project", "--account", "--impersonate-service-account", "--configuration"]
)
async def test_gcloud_refuses_a_flag_that_retargets_it(tmp_path: Any, flag: str) -> None:
    """Every one of these changes the project or the identity, so a command
    carrying its own would be approved against a target it is not going to
    touch. --impersonate-service-account is the sharpest: it changes who the
    command runs as without changing anything else the prompt would show."""
    ctx = context(tmp_path, FakeGcp())
    ctx.cloud.cli_allowlist = ("gcloud",)
    outcome = await gcloud_tool().run(
        {"args": ["compute", "instances", "list", flag, "other"]}, ctx
    )
    assert outcome.is_error
    assert outcome.summary == "reserved flag"
    assert flag in outcome.content


async def test_gcloud_is_handed_the_session_project(tmp_path: Any, monkeypatch: Any) -> None:
    """Supplied by Altus, the way kubectl is given --context."""
    import altus.tools.cli as cli_module

    seen: list[list[str]] = []

    async def fake_execute(path: str, argv: list[str], **kw: Any) -> tuple[int, str, str]:
        seen.append(argv)
        return 0, "[]", ""

    monkeypatch.setattr(cli_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cli_module, "_execute", fake_execute)

    ctx = context(tmp_path, FakeGcp())
    ctx.cloud.cli_allowlist = ("gcloud",)
    outcome = await gcloud_tool().run({"args": ["compute", "instances", "list"]}, ctx)
    assert not outcome.is_error
    assert seen == [["compute", "instances", "list", "--project", "demo-project"]]


def test_gcloud_can_be_switched_off_without_taking_kubectl(tmp_path: Any) -> None:
    settings = CloudSettings()
    settings.gcp.allow_cli = False
    names = default_registry(
        kubernetes=False, aws=False, azure=False, gcp=False, mcp=False, cloud=settings
    ).names
    assert "gcp_cli" not in names
    assert "k8s_kubectl" in names


# ------------------------------------------------------------ the front door


def test_the_gcp_dashboard_uses_the_gcp_panels() -> None:
    from altus.tui.screens.dashboard import GCP_PANELS, PANELS_BY_CLOUD, DashboardScreen

    screen = DashboardScreen("demo-project", cloud="gcp")
    assert screen.panels == GCP_PANELS
    assert {tool for _title, tool, _args in GCP_PANELS} <= {
        "gcp_topology",
        "gcp_inventory",
        "gcp_cost",
        "gcp_whoami",
    }
    assert set(PANELS_BY_CLOUD) == {"k8s", "aws", "azure", "gcp"}
    assert all(args == {} for _title, _tool, args in GCP_PANELS)
