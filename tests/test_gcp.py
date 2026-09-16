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


def test_read_only_is_declared_correctly_on_every_tool() -> None:
    assert all(t.read_only for t in gcp_tools())


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
        kubernetes=False, aws=False, azure=False, gcp=False, cloud=CloudSettings()
    )
    native = {n for n in registry.names if n.startswith("gcp_")} - {"gcp_cli"}
    assert native == set()
