"""Cloud foundations: classification, redaction, protection, kubeconfig.

The redaction and classification tests are the load-bearing ones. Tool results
are transmitted to whichever LLM provider is active, so a gap here is a
credential leaving the machine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from altus.cloud import aws as aws_cloud
from altus.cloud import kube as kube_cloud
from altus.cloud.base import (
    INTEGRATIONS,
    CloudTarget,
    ProtectionMode,
    ProtectionRules,
    Sensitivity,
    integration,
)
from altus.cloud.redact import MARKER, redact, redact_text

# ------------------------------------------------------------- classification


@pytest.mark.parametrize(
    ("service", "operation", "expected"),
    [
        ("ec2", "DescribeInstances", Sensitivity.READ),
        ("s3", "ListBuckets", Sensitivity.READ),
        ("s3", "GetObject", Sensitivity.READ),
        ("cloudwatch", "GetMetricData", Sensitivity.READ),
        ("lambda", "Invoke", Sensitivity.MUTATE),
        ("s3", "PutObject", Sensitivity.MUTATE),
        ("sts", "GetSessionToken", Sensitivity.SENSITIVE_READ),
        ("sts", "AssumeRole", Sensitivity.PRIVILEGED),
        ("secretsmanager", "GetSecretValue", Sensitivity.SENSITIVE_READ),
        ("ecr", "GetAuthorizationToken", Sensitivity.SENSITIVE_READ),
        ("ssm", "GetParameter", Sensitivity.SENSITIVE_READ),
        ("ec2", "GetPasswordData", Sensitivity.SENSITIVE_READ),
        ("iam", "CreateAccessKey", Sensitivity.PRIVILEGED),
        ("eks", "DescribeCluster", Sensitivity.SENSITIVE_READ),
        # Reading the authorization graph is how you understand an account.
        ("iam", "ListRoles", Sensitivity.READ),
        ("iam", "GetRole", Sensitivity.READ),
        ("organizations", "ListAccounts", Sensitivity.READ),
        # Writing it is not.
        ("iam", "AttachRolePolicy", Sensitivity.PRIVILEGED),
        ("iam", "DeleteRole", Sensitivity.PRIVILEGED),
        ("organizations", "LeaveOrganization", Sensitivity.PRIVILEGED),
        ("kms", "ScheduleKeyDeletion", Sensitivity.PRIVILEGED),
        ("sts", "AssumeRoleWithWebIdentity", Sensitivity.PRIVILEGED),
        # Destroying something that holds data.
        ("ec2", "TerminateInstances", Sensitivity.PRIVILEGED),
        ("rds", "DeleteDBInstance", Sensitivity.PRIVILEGED),
        ("s3", "DeleteBucket", Sensitivity.PRIVILEGED),
        ("logs", "DeleteLogGroup", Sensitivity.PRIVILEGED),
        # Opening something to the network.
        ("ec2", "AuthorizeSecurityGroupIngress", Sensitivity.PRIVILEGED),
        ("s3", "PutBucketPolicy", Sensitivity.PRIVILEGED),
        ("lambda", "AddPermission", Sensitivity.PRIVILEGED),
        # Ordinary changes stay ordinary.
        ("ec2", "CreateTags", Sensitivity.MUTATE),
        ("ec2", "DeleteTags", Sensitivity.MUTATE),
        ("cloudwatch", "DeleteAlarms", Sensitivity.MUTATE),
        ("s3", "PutObject", Sensitivity.MUTATE),
    ],
)
def test_aws_classification(service: str, operation: str, expected: Sensitivity) -> None:
    assert aws_cloud.classify(service, operation) is expected


def test_unknown_verbs_fail_closed() -> None:
    """To PRIVILEGED, not MUTATE. AWS ships new operations constantly, and a
    verb nobody anticipated is where guessing low is unrecoverable."""
    for operation in ("FrobnicateWidget", "YeetInstance", "Whatever"):
        assert aws_cloud.classify("madeup", operation) is Sensitivity.PRIVILEGED


def test_no_identity_write_is_merely_a_mutation() -> None:
    """The whole corpus. A write to IAM, STS, Organizations or KMS decides who
    may do what --- none of them may sit at the same level as tagging a
    volume, where a single keypress is enough."""
    leaked: list[str] = []
    for service in sorted(aws_cloud.PRIVILEGED_SERVICES):
        try:
            candidates = aws_cloud.operations(service)
        except Exception:
            continue  # not every name in the set is a botocore service
        for operation in candidates:
            if operation.startswith(aws_cloud.READ_PREFIXES):
                continue
            if aws_cloud.classify(service, operation) is not Sensitivity.PRIVILEGED:
                leaked.append(f"{service}:{operation}")
    assert leaked == [], f"identity writes below PRIVILEGED: {leaked[:10]}"


def test_the_privileged_tier_stays_rare_enough_to_mean_something() -> None:
    """A challenge that fires on everything trains people to type through it.

    Measured across all 19,189 operations. The number is asserted loosely ---
    the point is to notice if a future rule makes half of AWS privileged, not
    to pin an exact count.
    """
    import botocore.session

    session = botocore.session.get_session()
    counts = {level: 0 for level in Sensitivity}
    for service in session.get_available_services():
        try:
            model = session.get_service_model(service)
        except Exception:
            continue
        for operation in model.operation_names:
            counts[aws_cloud.classify(service, operation)] += 1

    total = sum(counts.values())
    assert total > 15_000, "the corpus should be most of AWS"
    share = counts[Sensitivity.PRIVILEGED] / total
    assert 0.01 < share < 0.15, f"privileged is {share:.1%} of operations"


def test_destruction_is_judged_by_what_is_destroyed() -> None:
    """Losing a CloudWatch alarm is an inconvenience; losing a database is
    not. Before this distinction existed every Delete* came out privileged,
    which made the distinction decide nothing."""
    ordinary = ("cloudwatch:DeleteAlarms", "ec2:DeleteTags", "ec2:DeleteSecurityGroup")
    grave = ("rds:DeleteDBInstance", "s3:DeleteBucket", "efs:DeleteFileSystem")
    for qualified in ordinary:
        service, operation = qualified.split(":")
        assert aws_cloud.classify(service, operation) is Sensitivity.MUTATE, qualified
    for qualified in grave:
        service, operation = qualified.split(":")
        assert aws_cloud.classify(service, operation) is Sensitivity.PRIVILEGED, qualified


def test_no_credential_shaped_operation_is_classified_read() -> None:
    """The whole corpus, not a sample: a leak here mints credentials silently."""
    leaked: list[str] = []
    for service in aws_cloud.available_services():
        try:
            operations = aws_cloud.operations(service)
        except Exception:
            continue
        for operation in operations:
            if aws_cloud.classify(service, operation) is not Sensitivity.READ:
                continue
            if any(
                word in operation
                for word in ("Credential", "Token", "Password", "Secret", "Session", "PrivateKey")
            ):
                leaked.append(f"{service}:{operation}")
    assert leaked == [], f"credential-shaped operations classified as plain reads: {leaked[:10]}"


def test_only_approval_free_level_is_read() -> None:
    assert Sensitivity.READ.needs_approval is False
    assert Sensitivity.SENSITIVE_READ.needs_approval is True
    assert Sensitivity.MUTATE.needs_approval is True


def test_describe_operation_reports_required_params_and_docs() -> None:
    described = aws_cloud.describe_operation("ec2", "TerminateInstances")
    assert described["sensitivity"] == "privileged"
    assert [k for k, v in described["parameters"].items() if v["required"]] == ["InstanceIds"]
    assert described["documentation"]
    assert "<" not in described["documentation"], "HTML must be stripped"


def test_python_method_name_conversion() -> None:
    assert aws_cloud.python_method("DescribeInstances") == "describe_instances"
    assert aws_cloud.python_method("GetObject") == "get_object"


@pytest.mark.parametrize(
    ("verb", "kind", "subresource", "expected"),
    [
        # Ordinary reads and writes.
        ("get", "Pod", "", Sensitivity.READ),
        ("list", "Deployment", "", Sensitivity.READ),
        ("watch", "Pod", "", Sensitivity.READ),
        ("delete", "Deployment", "", Sensitivity.MUTATE),
        ("apply", "Deployment", "", Sensitivity.MUTATE),
        ("patch", "Deployment", "", Sensitivity.MUTATE),
        ("replace", "ConfigMap", "", Sensitivity.MUTATE),
        # Credential material, whatever the verb.
        ("get", "Secret", "", Sensitivity.SENSITIVE_READ),
        ("list", "Secret", "", Sensitivity.SENSITIVE_READ),
        ("get", "ServiceAccount", "", Sensitivity.SENSITIVE_READ),
        # The subresource outranks the verb --- this is the whole point.
        ("get", "Pod", "exec", Sensitivity.PRIVILEGED),
        ("get", "Pod", "attach", Sensitivity.PRIVILEGED),
        ("get", "Pod", "portforward", Sensitivity.PRIVILEGED),
        ("create", "ServiceAccount", "token", Sensitivity.PRIVILEGED),
        ("create", "Pod", "eviction", Sensitivity.PRIVILEGED),
        ("update", "CertificateSigningRequest", "approval", Sensitivity.PRIVILEGED),
        # ...but an ordinary subresource still follows its verb.
        ("get", "Deployment", "status", Sensitivity.READ),
        ("get", "Deployment", "scale", Sensitivity.READ),
        ("patch", "Deployment", "scale", Sensitivity.MUTATE),
        # Writing the authorization graph, or a node, is privileged.
        ("patch", "ClusterRoleBinding", "", Sensitivity.PRIVILEGED),
        ("create", "Role", "", Sensitivity.PRIVILEGED),
        ("patch", "Node", "", Sensitivity.PRIVILEGED),
        ("delete", "CustomResourceDefinition", "", Sensitivity.PRIVILEGED),
        # ...but reading it is not.
        ("get", "ClusterRoleBinding", "", Sensitivity.READ),
        ("list", "Node", "", Sensitivity.READ),
        # One call, an unbounded number of objects.
        ("deletecollection", "Pod", "", Sensitivity.PRIVILEGED),
        # Unknown verbs fail closed to the strictest level.
        ("frobnicate", "Pod", "", Sensitivity.PRIVILEGED),
        ("", "Pod", "", Sensitivity.PRIVILEGED),
    ],
)
def test_k8s_classification(verb: str, kind: str, subresource: str, expected: Sensitivity) -> None:
    assert kube_cloud.classify(verb, kind, subresource) is expected


def test_every_privileged_subresource_is_privileged_under_every_verb() -> None:
    """The subresource decides alone. A read verb must not launder one."""
    for subresource in kube_cloud.PRIVILEGED_SUBRESOURCES:
        for verb in ("get", "list", "watch", "create", "update", "patch", "delete"):
            assert kube_cloud.classify(verb, "Pod", subresource) is Sensitivity.PRIVILEGED, (
                f"{verb} pods/{subresource} was not privileged"
            )


def test_subresource_matching_ignores_case_and_leading_slash() -> None:
    for form in ("exec", "EXEC", "/exec"):
        assert kube_cloud.classify("get", "Pod", form) is Sensitivity.PRIVILEGED


def test_privileged_needs_a_challenge_and_lesser_levels_do_not() -> None:
    assert Sensitivity.PRIVILEGED.needs_challenge
    for level in (Sensitivity.READ, Sensitivity.SENSITIVE_READ, Sensitivity.MUTATE):
        assert not level.needs_challenge
    # Adding the tier must not have changed what needs approval at all.
    assert not Sensitivity.READ.needs_approval
    for level in (Sensitivity.SENSITIVE_READ, Sensitivity.MUTATE, Sensitivity.PRIVILEGED):
        assert level.needs_approval


# ------------------------------------------------------------------ redaction


def test_kubernetes_secret_is_scrubbed_but_keeps_its_shape() -> None:
    secret = {
        "kind": "Secret",
        "metadata": {"name": "db-creds", "namespace": "default"},
        "data": {"password": "aHVudGVyMg==", "username": "YWRtaW4="},
    }
    out = redact(secret)
    assert out["data"] == {"password": MARKER, "username": MARKER}
    assert out["metadata"]["name"] == "db-creds", "names are not secret"
    assert "aHVudGVyMg==" not in str(out)


def test_configmap_is_left_alone() -> None:
    cm = {"kind": "ConfigMap", "data": {"LOG_LEVEL": "debug", "REPLICAS": "3"}}
    assert redact(cm) == cm


def test_sts_response_is_scrubbed() -> None:
    response = {
        "Credentials": {
            "AccessKeyId": "ASIAEXAMPLE",
            "SecretAccessKey": "wJalrXUtnFEMI",
            "SessionToken": "FQoGZXIvYXdzEBYa",
        }
    }
    out = redact(response)
    assert out["Credentials"]["SecretAccessKey"] == MARKER
    assert out["Credentials"]["SessionToken"] == MARKER
    assert "wJalrXUtnFEMI" not in str(out)


def test_nested_and_listed_secrets_are_reached() -> None:
    payload = {"items": [{"spec": {"env": [{"name": "API_TOKEN", "value": "abc"}]}}]}
    out = redact(payload)
    assert "abc" not in str(out) or out["items"][0]["spec"]["env"][0]["value"] == MARKER


def test_key_name_lists_are_not_mistaken_for_secrets() -> None:
    """`keys` holding names, and `PublicKey`, are not secret material."""
    payload = {"keys": ["password", "username"], "PublicKey": "ssh-rsa AAAA", "KeyId": "k-1"}
    out = redact(payload)
    assert out["keys"] == ["password", "username"]
    assert out["KeyId"] == "k-1"


def test_redaction_can_be_disabled() -> None:
    secret = {"kind": "Secret", "data": {"password": "x"}}
    assert redact(secret, enabled=False) == secret


def test_text_redaction_catches_loose_credentials() -> None:
    text = (
        "export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIabcdefgh\n"
        "token ASIAIOSFODNN7EXAMPLE\n"
        "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9\n"
    )
    out = redact_text(text)
    assert "wJalrXUtnFEMIabcdefgh" not in out
    assert "ASIAIOSFODNN7EXAMPLE" not in out
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in out


def test_text_redaction_strips_private_keys() -> None:
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"
    assert "MIIEow" not in redact_text(pem)


# ----------------------------------------------------------------- protection


def test_protection_matches_case_insensitively() -> None:
    """A real cluster is as likely to be AKS_EU_PROD as prod-eu."""
    rules = ProtectionRules(patterns=("*prod*",))
    assert rules.matches(CloudTarget("k8s", "AKS_EU_PROD"))
    assert rules.matches(CloudTarget("k8s", "prod-eu-west"))
    assert rules.matches(CloudTarget("k8s", "Production-Cluster"))
    assert not rules.matches(CloudTarget("k8s", "AKS_QAM"))
    assert not rules.matches(CloudTarget("k8s", "staging"))


def test_protection_matches_any_part_of_the_target() -> None:
    rules = ProtectionRules(patterns=("*prod*",))
    assert rules.matches(CloudTarget("k8s", "cluster-a", "eu", "prod-namespace"))
    assert rules.matches(CloudTarget("aws", "111122223333", "prod-region"))


def test_protection_matches_account_ids_exactly() -> None:
    rules = ProtectionRules(accounts=("123456789012",))
    assert rules.matches(CloudTarget("aws", "123456789012", "eu-west-1"))
    assert not rules.matches(CloudTarget("aws", "999988887777", "eu-west-1"))


def test_protection_default_is_confirm_not_deny() -> None:
    assert ProtectionRules().mode is ProtectionMode.CONFIRM


def test_config_defaults_protect_production() -> None:
    from altus.config import Config

    protected = Config().cloud.protected
    rules = ProtectionRules.build(protected.patterns, protected.accounts, protected.mode)
    assert rules.matches(CloudTarget("k8s", "AKS_EU_PROD")), "out-of-the-box protection"


def test_target_renders_the_blast_radius() -> None:
    target = CloudTarget("k8s", "prod-eu", "cluster-1", "payments")
    assert target.render() == "k8s: prod-eu · cluster-1 · payments"


# ---------------------------------------------------------------- kubeconfig


@pytest.fixture
def kubeconfig(tmp_path: Path) -> Path:
    path = tmp_path / "config"
    path.write_text(
        """
apiVersion: v1
kind: Config
current-context: staging
clusters:
- name: prod-cluster
  cluster: {server: https://prod.example.com}
- name: staging-cluster
  cluster: {server: https://staging.example.com}
contexts:
- name: AKS_EU_PROD
  context: {cluster: prod-cluster, user: u1, namespace: payments}
- name: staging
  context: {cluster: staging-cluster, user: u1}
users:
- name: u1
  user: {token: secret-token-value}
""".strip()
    )
    return path


def test_contexts_are_listed_without_touching_the_file(
    kubeconfig: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Selecting a context must never rewrite the user's kubeconfig."""
    monkeypatch.setenv("KUBECONFIG", str(kubeconfig))
    before = kubeconfig.read_bytes()

    contexts, active = kube_cloud.list_contexts()

    assert {c.name for c in contexts} == {"AKS_EU_PROD", "staging"}
    assert active == "staging"
    prod = next(c for c in contexts if c.name == "AKS_EU_PROD")
    assert prod.cluster == "prod-cluster"
    assert prod.namespace == "payments"
    assert kubeconfig.read_bytes() == before, "kubeconfig was modified"


def test_listed_context_targets_are_protection_matched(
    kubeconfig: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KUBECONFIG", str(kubeconfig))
    contexts, _ = kube_cloud.list_contexts()
    rules = ProtectionRules(patterns=("*prod*",))
    protected = {c.name for c in contexts if rules.matches(c.target())}
    assert protected == {"AKS_EU_PROD"}


def test_missing_kubeconfig_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KUBECONFIG", str(tmp_path / "nope"))
    contexts, active = kube_cloud.list_contexts()
    assert contexts == [] and active is None


def test_malformed_kubeconfig_is_skipped_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = tmp_path / "bad"
    bad.write_text("this is not: [valid yaml")
    monkeypatch.setenv("KUBECONFIG", str(bad))
    contexts, _ = kube_cloud.list_contexts()
    assert contexts == []


def test_context_target_includes_namespace(
    kubeconfig: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KUBECONFIG", str(kubeconfig))
    contexts, _ = kube_cloud.list_contexts()
    prod = next(c for c in contexts if c.name == "AKS_EU_PROD")
    assert prod.target().render() == "k8s: AKS_EU_PROD · prod-cluster · payments"
    assert prod.target("other").scope == "other"


# --------------------------------------------------------------- integrations


def test_integrations_report_availability_and_install_hints() -> None:
    assert {i.name for i in INTEGRATIONS} == {"k8s", "aws", "azure", "gcp", "mcp"}
    entry = integration("k8s")
    assert entry is not None
    assert "--extra k8s" in entry.install_hint


def test_only_clouds_can_be_logged_in_to() -> None:
    """MCP is an extra to install, not a cloud to sign in to, so `/login` must
    not offer it --- `auth.status` has no per-cloud handler for it."""
    from altus.cloud.base import cloud_integrations

    assert {i.name for i in cloud_integrations()} == {"k8s", "aws", "azure", "gcp"}


def test_missing_integration_is_reported_not_crashed(monkeypatch: pytest.MonkeyPatch) -> None:
    from altus.cloud.base import Integration

    absent = Integration("nope", "nope", ("definitely_not_a_module",), "Nothing")
    assert absent.available is False
    assert "--extra nope" in absent.install_hint


def test_auth_status_never_needs_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opening the auth panel must not cost four cloud round trips."""
    import socket

    from altus.cloud.auth import all_status

    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("status() made a network call")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    statuses = all_status()
    assert {s.cloud for s in statuses} == {"k8s", "aws", "azure", "gcp"}


def test_name_value_pairs_redact_the_value_not_the_label() -> None:
    """`{"name": "API_TOKEN", "value": ...}` hides the indicator in a sibling.

    The label must survive, or the model cannot tell the pairs apart and will
    report "there are two secrets" instead of naming which is which.
    """
    payload = {
        "env": [
            {"name": "API_TOKEN", "value": "abc123"},
            {"name": "LOG_LEVEL", "value": "debug"},
        ],
        "Tags": [
            {"Key": "db_password", "Value": "hunter2"},
            {"Key": "env", "Value": "prod"},
        ],
    }
    out = redact(payload)
    assert out["env"][0] == {"name": "API_TOKEN", "value": MARKER}
    assert out["env"][1] == {"name": "LOG_LEVEL", "value": "debug"}
    assert out["Tags"][0] == {"Key": "db_password", "Value": MARKER}
    assert out["Tags"][1] == {"Key": "env", "Value": "prod"}


def test_a_bare_key_field_is_still_treated_as_secret() -> None:
    """Outside a name/value pair, `key` may well hold key material."""
    assert redact({"key": "-----BEGIN RSA PRIVATE KEY-----"})["key"] == MARKER


# --------------------------------------------------------------- kube scope


def test_global_scope_writes_only_current_context(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Opt-in write. Everything else in the file must survive untouched."""
    import yaml

    from altus.cloud.kube import set_current_context

    path = tmp_path / "config"
    original = (
        "apiVersion: v1\nkind: Config\ncurrent-context: staging\n"
        "preferences:\n  colors: true\n"
        "clusters:\n- name: c\n  cluster: {server: 'https://x', insecure-skip-tls-verify: true}\n"
        "contexts:\n- name: AKS_EU_PROD\n  context: {cluster: c, user: u, namespace: payments}\n"
        "- name: staging\n  context: {cluster: c, user: u}\n"
        "users:\n- name: u\n  user: {token: keep-me}\n"
    )
    path.write_text(original)
    path.chmod(0o600)
    monkeypatch.setenv("KUBECONFIG", str(path))

    set_current_context("AKS_EU_PROD")

    after = yaml.safe_load(path.read_text())
    before = yaml.safe_load(original)
    assert after["current-context"] == "AKS_EU_PROD"
    before.pop("current-context")
    rest = dict(after)
    rest.pop("current-context")
    assert rest == before, "only current-context may change"
    assert after["users"][0]["user"]["token"] == "keep-me"
    assert path.stat().st_mode & 0o777 == 0o600, "permissions preserved"
    assert [p.name for p in tmp_path.iterdir()] == ["config"], "no temp file left behind"


def test_global_scope_rejects_an_unknown_context(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from altus.cloud.kube import set_current_context

    path = tmp_path / "config"
    path.write_text(
        "apiVersion: v1\nkind: Config\ncurrent-context: a\n"
        "contexts:\n- name: a\n  context: {cluster: c, user: u}\n"
    )
    monkeypatch.setenv("KUBECONFIG", str(path))
    before = path.read_bytes()
    with pytest.raises(KeyError):
        set_current_context("nope")
    assert path.read_bytes() == before, "a rejected write must change nothing"


def test_default_scope_is_wai_only() -> None:
    from altus.config import Config

    assert Config().cloud.kube_context_scope == "altus"


@pytest.mark.parametrize(
    ("line", "leaks"),
    [
        ("DB_PASSWORD=hunter2", False),
        ("AWS_SESSION_TOKEN=abc123", False),
        ("api_key: sk-live-9", False),
        ("CLIENT_SECRET=shh", False),
        ("PATH=/usr/local/bin:/usr/bin", True),
        ("HOSTNAME=web-7d9-aaa", True),
        ("KUBERNETES_SERVICE_PORT=443", True),
        ("PUBLIC_KEY=ssh-rsa AAAA", True),
    ],
)
def test_env_style_output_is_scrubbed_without_eating_ordinary_variables(
    line: str, leaks: bool
) -> None:
    """`k8s_exec -- env` is one of the first things anyone runs on a broken
    pod, and its output goes straight to the model provider."""
    out = redact_text(line)
    value = line.split("=", 1)[-1].split(":", 1)[-1].strip()
    if leaks:
        assert value in out, f"{line} is not a secret and must survive"
    else:
        assert value not in out, f"{line} leaked"
        assert MARKER in out


# ------------------------------------------------------------- aws sessions


class FakeAwsClient:
    """A botocore client's shape, without a network or an account."""

    def __init__(self, pages: dict[str, list[dict[str, object]]] | None = None) -> None:
        self.pages = pages or {}
        self.calls: list[tuple[str, dict[str, object]]] = []

    def can_paginate(self, method: str) -> bool:
        return method in self.pages

    def get_paginator(self, method: str) -> FakeAwsClient:
        self._method = method
        return self

    def paginate(self, **kwargs: object) -> list[dict[str, object]]:
        self.calls.append((self._method, kwargs))
        return list(self.pages[self._method])

    def get_caller_identity(self) -> dict[str, str]:
        self.calls.append(("get_caller_identity", {}))
        return {
            "Account": "123456789012",
            "Arn": "arn:aws:iam::123456789012:user/dev",
            "UserId": "A",
        }

    def describe_regions(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("describe_regions", kwargs))
        return {"Regions": [{"RegionName": "eu-west-1"}]}


def provider_with(client: FakeAwsClient) -> aws_cloud.AwsProvider:
    provider = aws_cloud.AwsProvider(region="eu-west-1")
    provider._clients[("sts", "eu-west-1")] = client
    provider._clients[("ec2", "eu-west-1")] = client
    provider._session = type("S", (), {"region_name": "eu-west-1"})()
    return provider


async def test_whoami_is_cached_because_every_preflight_needs_it() -> None:
    """The ARN is what SimulatePrincipalPolicy uses as PolicySourceArn, so it
    is asked for constantly and must not be a call each time."""
    client = FakeAwsClient()
    provider = provider_with(client)
    first = await provider.whoami()
    await provider.whoami()
    assert first["account"] == "123456789012"
    assert [name for name, _ in client.calls].count("get_caller_identity") == 1


async def test_reset_drops_the_cached_client() -> None:
    """A cached client is bound to the credentials it was built with; reusing
    one after a profile change would call the account you just left."""
    provider = provider_with(FakeAwsClient())
    await provider.whoami()
    provider.reset()
    assert provider._clients == {}
    assert provider._identity is None


async def test_results_are_capped_and_the_cap_is_declared() -> None:
    """Returning the first page silently would have the model reason about a
    partial answer as though it were the whole one."""
    pages = {"describe_instances": [{"Reservations": [{"n": i}]} for i in range(20)]}
    provider = provider_with(FakeAwsClient(pages))
    out = await provider.call("ec2", "DescribeInstances", limit=5)
    assert len(out["Reservations"]) == 5
    assert "_truncated" in out


async def test_pagination_bookkeeping_never_reaches_the_model() -> None:
    pages = {
        "describe_instances": [
            {"Reservations": [{"n": 1}], "NextToken": "abc", "ResponseMetadata": {"RequestId": "x"}}
        ]
    }
    provider = provider_with(FakeAwsClient(pages))
    out = await provider.call("ec2", "DescribeInstances")
    assert out["Reservations"] == [{"n": 1}]
    assert "NextToken" not in out and "ResponseMetadata" not in out


async def test_an_unpaginated_operation_still_works() -> None:
    client = FakeAwsClient()
    provider = provider_with(client)
    out = await provider.call("ec2", "DescribeRegions")
    assert out["Regions"] == [{"RegionName": "eu-west-1"}]


def test_the_operation_name_becomes_botocore_s_method_name() -> None:
    assert aws_cloud.python_method("DescribeInstances") == "describe_instances"
    assert aws_cloud.python_method("GetCallerIdentity") == "get_caller_identity"
