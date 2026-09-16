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
