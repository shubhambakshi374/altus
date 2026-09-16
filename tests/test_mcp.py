"""The MCP classifier, over the manifests that actually ship.

Unlike the four clouds there is no corpus to sweep, so these tests do the next
best thing: they assert the shipped manifests are internally consistent, that
the classifier only ever escalates what a manifest says, and that the two
places where a vendor's own label disagrees with ours stay decided our way.
"""

from __future__ import annotations

import tomllib

import pytest

from altus.cloud.base import ProtectionRules, Sensitivity
from altus.mcp.catalog import CATALOG, DATABRICKS_SCOPES, Transport, server_spec
from altus.mcp.classify import (
    MANIFESTS,
    ToolInfo,
    classify,
    drift,
    manifest,
    target_for,
    why_unknown,
)

SERVERS = [spec.id for spec in CATALOG]


# --- the catalog ---------------------------------------------------------


def test_catalog_ids_are_unique() -> None:
    assert len(SERVERS) == len(set(SERVERS))


@pytest.mark.parametrize("server", SERVERS)
def test_every_catalogued_server_ships_a_manifest(server: str) -> None:
    assert (MANIFESTS / f"{server}.toml").is_file()


@pytest.mark.parametrize("server", SERVERS)
def test_a_server_says_how_to_reach_it_and_how_to_authenticate(server: str) -> None:
    spec = server_spec(server)
    assert spec is not None
    assert spec.products and spec.summary and spec.reference
    if spec.transport is Transport.HTTP:
        assert spec.url
    else:
        assert spec.command
    assert spec.env, "detection needs at least one credential variable to look for"


@pytest.mark.parametrize("server", SERVERS)
def test_availability_never_hits_the_network(server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """`/mcp` calls this for every server; it must not be able to hang."""
    spec = server_spec(server)
    assert spec is not None
    for name in spec.env:
        monkeypatch.delenv(name, raising=False)
    assert spec.available() is False
    assert spec.missing_hint


# --- the manifests -------------------------------------------------------


@pytest.mark.parametrize("server", SERVERS)
def test_manifest_loads_and_declares_its_provenance(server: str) -> None:
    table = manifest(server)
    assert table.source in {"measured", "documented"}
    assert table.recorded and table.reference
    assert table.target_fields, "without target fields a prompt cannot name the blast radius"


@pytest.mark.parametrize("server", SERVERS)
def test_every_before_read_exists_and_is_a_read(server: str) -> None:
    """A preflight that calls a write to find out about a write is a bug."""
    table = manifest(server)
    for mutating, before in table.before.items():
        assert mutating in table.tools, f"{server}: {mutating} is not in the manifest"
        assert before in table.tools, f"{server}: before-read {before} is not in the manifest"
        assert table.tools[before] is Sensitivity.READ, f"{server}: {before} is not a read"


@pytest.mark.parametrize("server", SERVERS)
def test_manifest_sensitivities_are_the_four_known_levels(server: str) -> None:
    raw = tomllib.loads((MANIFESTS / f"{server}.toml").read_text(encoding="utf-8"))
    for name, level in dict(raw.get("tools", {})).items():
        assert level in {s.value for s in Sensitivity}, f"{server}.{name} = {level!r}"


@pytest.mark.parametrize("server", SERVERS)
def test_classify_never_returns_less_than_the_manifest_says(server: str) -> None:
    """The sweep. Every rule in `classify` escalates; none may relax."""
    order = [
        Sensitivity.READ,
        Sensitivity.SENSITIVE_READ,
        Sensitivity.MUTATE,
        Sensitivity.PRIVILEGED,
    ]
    for tool, listed in manifest(server).tools.items():
        got = classify(server, tool)
        assert order.index(got) >= order.index(listed), f"{server}.{tool} relaxed to {got}"


# --- the four levels, over real tool names -------------------------------


@pytest.mark.parametrize(
    ("server", "tool", "expected"),
    [
        # plain reads stay plain
        ("github", "list_issues", Sensitivity.READ),
        ("grafana", "query_prometheus", Sensitivity.READ),
        ("datadog", "search_datadog_logs", Sensitivity.READ),
        ("newrelic", "list_recent_issues", Sensitivity.READ),
        ("atlassian", "getJiraIssue", Sensitivity.READ),
        # a read that hands back credential material
        ("github", "get_secret_scanning_alert", Sensitivity.SENSITIVE_READ),
        ("datadog", "datadog_secrets_scan", Sensitivity.SENSITIVE_READ),
        # caller-supplied queries against a data plane
        ("newrelic", "execute_nrql_query", Sensitivity.SENSITIVE_READ),
        ("grafana", "query_sql", Sensitivity.SENSITIVE_READ),
        ("datadog", "ddsql_run_query", Sensitivity.SENSITIVE_READ),
        # ordinary writes
        ("github", "add_issue_comment", Sensitivity.MUTATE),
        ("github", "merge_pull_request", Sensitivity.MUTATE),
        ("atlassian", "createJiraIssue", Sensitivity.MUTATE),
        ("grafana", "update_dashboard", Sensitivity.MUTATE),
        # rewriting who may do what
        ("github", "create_repository_ruleset", Sensitivity.PRIVILEGED),
        # deleting something stateful
        ("github", "delete_repository", Sensitivity.PRIVILEGED),
        ("github", "delete_file", Sensitivity.PRIVILEGED),
        # arbitrary execution, whatever the vendor calls it
        ("datadog", "execute_code", Sensitivity.PRIVILEGED),
        (
            "datadog",
            "datadog_remote_action_restricted_shell_run_command",
            Sensitivity.PRIVILEGED,
        ),
        ("snowflake", "system_execute_sql", Sensitivity.PRIVILEGED),
    ],
)
def test_the_scope_table(server: str, tool: str, expected: Sensitivity) -> None:
    assert classify(server, tool) is expected


def test_a_challenge_stays_worth_reading() -> None:
    """Deleting a repository is privileged; deleting an annotation is not.

    The AWS work established why: a challenge that fires on everything teaches
    people to type through it. `delete_annotation` and `delete_repository` are
    both deletes, and only one of them destroys state anyone will miss.
    """
    assert classify("grafana", "delete_annotation") is Sensitivity.MUTATE
    assert classify("grafana", "delete_snapshot") is Sensitivity.MUTATE
    assert classify("github", "delete_repository") is Sensitivity.PRIVILEGED


# --- failing closed, and the drift alarm ---------------------------------


def test_a_tool_the_manifest_has_never_seen_is_privileged() -> None:
    assert classify("github", "nuke_everything_v2") is Sensitivity.PRIVILEGED
    assert "manifest" in why_unknown("github", "nuke_everything_v2")


def test_a_server_altus_does_not_ship_is_privileged() -> None:
    assert classify("some-random-server", "read_thing") is Sensitivity.PRIVILEGED
    assert "not a server Altus ships" in why_unknown("some-random-server", "read_thing")


def test_a_known_tool_has_no_unknown_reason() -> None:
    assert why_unknown("github", "list_issues") == ""


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ("vector-search/main/default", Sensitivity.READ),
        ("genie/01ef", Sensitivity.READ),
        ("functions/main/default", Sensitivity.PRIVILEGED),
        ("", Sensitivity.PRIVILEGED),
    ],
)
def test_databricks_capability_comes_from_the_endpoint(scope: str, expected: Sensitivity) -> None:
    """Names are the customer's there, so the URL is the only honest signal.

    Failing every unnamed tool closed would demand a typed challenge to run a
    vector search, which is the challenge-fatigue failure again.
    """
    assert classify("databricks", "some_index_the_customer_named", scope=scope) is expected


def test_every_databricks_scope_explains_itself() -> None:
    for level, why in DATABRICKS_SCOPES.values():
        assert isinstance(level, Sensitivity)
        assert why and why[0].islower()


# --- annotations are a ceiling, never a floor ----------------------------


def test_a_server_calling_a_read_a_write_escalates_it() -> None:
    info = ToolInfo("list_issues", read_only_hint=False)
    assert classify("github", "list_issues", info) is Sensitivity.MUTATE


def test_destructive_hint_escalates_too() -> None:
    info = ToolInfo("list_issues", destructive_hint=True)
    assert classify("github", "list_issues", info) is Sensitivity.MUTATE


def test_a_server_calling_a_write_a_read_changes_nothing() -> None:
    """The half of the signal we must not take: a remote process does not get
    to talk its way down to a read."""
    info = ToolInfo("delete_repository", read_only_hint=True, destructive_hint=False)
    assert classify("github", "delete_repository", info) is Sensitivity.PRIVILEGED
    info = ToolInfo("add_issue_comment", read_only_hint=True)
    assert classify("github", "add_issue_comment", info) is Sensitivity.MUTATE


def test_drift_reports_both_kinds() -> None:
    reported = drift(
        "github",
        [
            ToolInfo("list_issues"),
            ToolInfo("brand_new_tool"),
            ToolInfo("get_me", read_only_hint=False),
        ],
    )
    assert len(reported) == 2
    assert any("brand_new_tool" in line and "not in the manifest" in line for line in reported)
    assert any("get_me" in line and "declares it a write" in line for line in reported)


def test_no_drift_when_the_server_agrees() -> None:
    assert drift("github", [ToolInfo("list_issues", read_only_hint=True)]) == []


# --- targets -------------------------------------------------------------


def test_target_names_the_blast_radius() -> None:
    target = target_for("github", {"owner": "acme", "repo": "billing"})
    assert target.cloud == "github"
    assert target.render() == "github: acme · billing"


def test_a_production_target_is_protected_with_no_new_config() -> None:
    """`ProtectedSettings` ships `*prod*`, and it now covers MCP for free."""
    rules = ProtectionRules.build(["*prod*"], [], "confirm")
    hit = target_for("snowflake", {"database": "PROD_ANALYTICS", "schema": "public"})
    miss = target_for("snowflake", {"database": "dev_analytics", "schema": "public"})
    assert rules.matches(hit)
    assert not rules.matches(miss)


def test_missing_target_arguments_do_not_crash() -> None:
    assert target_for("github", {}).render() == "github"
