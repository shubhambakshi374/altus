"""The MCP classifier, over the manifests that actually ship.

Unlike the four clouds there is no corpus to sweep, so these tests do the next
best thing: they assert the shipped manifests are internally consistent, that
the classifier only ever escalates what a manifest says, and that the two
places where a vendor's own label disagrees with ours stay decided our way.
"""

from __future__ import annotations

import json
import tomllib
from functools import cache
from pathlib import Path
from typing import Any

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
from altus.mcp.session import (
    McpError,
    McpProvider,
    _stdio_params,
    credential,
    credentials,
    missing_credentials,
)
from altus.tools.base import CloudContext, ToolContext
from altus.workspace import Workspace

SERVERS = [spec.id for spec in CATALOG]


# --- the catalog ---------------------------------------------------------


def test_catalog_ids_are_unique() -> None:
    assert len(SERVERS) == len(set(SERVERS))


@pytest.mark.parametrize("server", SERVERS)
def test_every_catalogued_server_ships_a_manifest(server: str) -> None:
    assert (MANIFESTS / f"{server}.toml").is_file()


#: Servers whose endpoint is created per instance and therefore has no default.
#: ServiceNow's MCP endpoint is a service record an administrator makes in MCP
#: Server Console, so there is no path to ship.
NO_DEFAULT_URL = {"servicenow"}


@pytest.mark.parametrize("server", SERVERS)
def test_a_server_says_how_to_reach_it_and_how_to_authenticate(server: str) -> None:
    spec = server_spec(server)
    assert spec is not None
    assert spec.products and spec.summary and spec.reference
    if spec.transport is Transport.HTTP:
        if server in NO_DEFAULT_URL:
            # Allowed only where there is genuinely nothing to guess, and then
            # the notes have to say so --- shipping an invented endpoint is how
            # the New Relic launch command got into a release.
            assert not spec.url
            assert "url" in spec.notes, "say where the user is meant to get it"
        else:
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
    assert table.source in {"derived", "documented", "curated"}
    assert table.recorded and table.reference
    if not table.target_fields:
        # A manifest may have no target fields, but only when nothing in a
        # call's arguments names the blast radius --- CrowdStrike's are all
        # opaque ids. Then the fail-closed path has to be the one that fires:
        # an unresolved target is treated as protected at the gate, which
        # `test_an_unresolved_target_is_treated_as_protected` pins.
        assert not target_for(server, {"id": "x", "ids": ["y"]}).resolved
    if table.source == "derived":
        assert table.upstream_ref, "a derived table must say which ref it was read from"
        assert table.upstream_ref != "main", "pin a release, not a moving branch"


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


def test_a_server_with_no_manifest_is_privileged() -> None:
    """The reason names what is missing rather than how it came to be missing,
    because the same sentence has to be true of a server Altus has never heard
    of and of one the user configured under [mcp.custom]."""
    assert classify("some-random-server", "read_thing") is Sensitivity.PRIVILEGED
    assert "no manifest" in why_unknown("some-random-server", "read_thing")


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
    where = target_for("github", {"owner": "acme", "repo": "billing"})
    assert where.resolved
    assert where.target.cloud == "github"
    assert where.render() == "github: acme · billing"


def test_a_production_target_is_protected_with_no_new_config() -> None:
    """`ProtectedSettings` ships `*prod*`, and it now covers MCP for free."""
    rules = ProtectionRules.build(["*prod*"], [], "confirm")
    hit = target_for("snowflake", {"database": "PROD_ANALYTICS", "schema": "public"})
    miss = target_for("snowflake", {"database": "dev_analytics", "schema": "public"})
    assert hit.resolved and miss.resolved
    assert rules.matches(hit.target)
    assert not rules.matches(miss.target)


def test_an_unresolvable_target_is_not_silently_unprotected() -> None:
    """The fail-open that shipped: no argument matched, so no pattern matched,
    so protection did not fire --- on the one control meant to stop a
    production mistake. An unresolved target is now its own state."""
    where = target_for("github", {})
    assert not where.resolved
    assert where.render() == "github: (unknown)"
    assert "could not be determined" in where.unknown_reason


# --- credentials and transport -------------------------------------------


def test_the_environment_beats_the_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring

    spec = server_spec("github")
    assert spec is not None
    keyring.set_password("altus", "mcp:github:GITHUB_PERSONAL_ACCESS_TOKEN", "stored")
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "exported")
    assert credential(spec, "GITHUB_PERSONAL_ACCESS_TOKEN") == "exported"
    monkeypatch.delenv("GITHUB_PERSONAL_ACCESS_TOKEN")
    assert credential(spec, "GITHUB_PERSONAL_ACCESS_TOKEN") == "stored"


def test_a_broken_keyring_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A locked or missing backend must degrade to "no credential", not crash."""
    import keyring

    def boom(_service: str, _user: str) -> str:
        raise RuntimeError("no backend")

    monkeypatch.setattr(keyring, "get_password", boom)
    spec = server_spec("github")
    assert spec is not None
    assert credential(spec, "GITHUB_PERSONAL_ACCESS_TOKEN") is None


def test_datadog_needs_both_of_its_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """One of a two-header pair is not partial credentials, it is none."""
    spec = server_spec("datadog")
    assert spec is not None
    monkeypatch.setenv("DD_API_KEY", "k")
    monkeypatch.delenv("DD_APPLICATION_KEY", raising=False)
    assert missing_credentials(spec) == ("DD_APPLICATION_KEY",)
    monkeypatch.setenv("DD_APPLICATION_KEY", "a")
    assert missing_credentials(spec) == ()


async def test_a_call_without_credentials_never_opens_a_connection() -> None:
    provider = McpProvider()
    with pytest.raises(McpError, match="no credentials"):
        await provider.call("github", "list_issues", {})


async def test_an_unshipped_server_is_refused_before_anything_is_dialled() -> None:
    provider = McpProvider()
    with pytest.raises(McpError, match="not a server Altus ships"):
        await provider.call("evil-corp", "do_thing", {})


def test_an_unfilled_endpoint_is_a_configuration_error_not_a_request() -> None:
    """Snowflake's URL contains the customer's own account. Sending a request
    at a URL still containing `{account}` would just be a confusing 404."""
    provider = McpProvider()
    spec = server_spec("snowflake")
    assert spec is not None
    with pytest.raises(McpError, match="account"):
        provider.url_for(spec)
    provider.urls["snowflake"] = "https://acme.snowflakecomputing.com/api/v2/mcp"
    assert provider.url_for(spec).endswith("/api/v2/mcp")


def test_the_databricks_scope_lands_in_the_url() -> None:
    provider = McpProvider(scopes={"databricks": "genie/01ef"})
    spec = server_spec("databricks")
    assert spec is not None
    provider.urls["databricks"] = "https://acme.databricks.com/api/2.0/mcp/{scope}"
    assert provider.url_for(spec).endswith("/api/2.0/mcp/genie/01ef")


def test_a_stdio_server_is_handed_only_what_it_needs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Passing a subprocess os.environ would hand it every other credential
    on the machine --- the AWS keys, the Anthropic key, all of it."""
    monkeypatch.setenv("GRAFANA_API_KEY", "g")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    spec = server_spec("grafana")
    assert spec is not None
    params = _stdio_params(spec, credentials(spec))
    assert params.env is not None
    assert set(params.env) == {"PATH", "GRAFANA_API_KEY"}


def test_annotations_are_read_off_the_real_sdk_shape() -> None:
    """The SDK names these read_only_hint; the wire format says readOnlyHint.

    Reading only one spelling means every annotation arrives as None, which
    would silently disable the escalation rule rather than fail visibly.
    """
    from mcp.types import Tool, ToolAnnotations

    tool = Tool(
        name="delete_repository",
        description="Delete a repository",
        inputSchema={"type": "object"},
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
    )
    info = ToolInfo.from_mcp(tool)
    assert info.name == "delete_repository"
    assert info.read_only_hint is False
    assert info.destructive_hint is True
    assert info.server_says_write


def test_a_tool_without_annotations_says_nothing_either_way() -> None:
    from mcp.types import Tool

    info = ToolInfo.from_mcp(Tool(name="list_issues", inputSchema={"type": "object"}))
    assert info.read_only_hint is None
    assert info.server_says_write is False


# --- the tool layer ------------------------------------------------------


class FakeMcp:
    """A provider that records everything, and never opens a socket.

    Shaped like `test_azure.py`'s FakeAzure: the point of it is that `calls`
    is the evidence for the deny sweep.
    """

    def __init__(self, tools: dict[str, list[ToolInfo]] | None = None) -> None:
        self.scopes: dict[str, str] = {}
        self.urls: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.results: dict[str, str] = {}
        self.tools_by_server: dict[str, list[ToolInfo]] = tools or {
            "github": [
                ToolInfo("list_issues", "List issues"),
                ToolInfo("issue_read", "Read one issue"),
                ToolInfo("get_file_contents", "Read a file"),
                ToolInfo("add_issue_comment", "Comment on an issue"),
                ToolInfo("issue_write", "Create or edit an issue"),
                ToolInfo("delete_repository", "Delete a repository", destructive_hint=True),
                ToolInfo("brand_new_tool", "Shipped after the manifest was written"),
            ]
        }

    async def tools(self, server: str) -> tuple[ToolInfo, ...]:
        return tuple(self.tools_by_server.get(server, ()))

    async def tool(self, server: str, name: str) -> ToolInfo | None:
        return next((t for t in await self.tools(server) if t.name == name), None)

    async def call(self, server: str, tool: str, args: dict[str, Any]) -> str:
        self.calls.append((server, tool, args))
        return self.results.get(tool, f"{tool} ok")


def make_ctx(
    provider: FakeMcp,
    *,
    policy: Any = None,
    servers: tuple[str, ...] = ("github",),
    **settings: Any,
) -> ToolContext:
    from altus.config.models import McpSettings
    from altus.tools.approval import AllowAll

    return ToolContext(
        workspace=Workspace(Path.cwd()),
        approvals=policy or AllowAll(),
        cloud=CloudContext(
            mcp=provider,
            mcp_settings=McpSettings(servers=list(servers), **settings),
            protection=ProtectionRules.build(["*prod*"], [], "confirm"),
        ),
    )


def test_four_tools_however_many_servers_connect() -> None:
    """Seven servers publish well over two hundred tools between them. The
    whole point of the meta-tool shape is that none of that is in context."""
    from altus.config.models import McpSettings
    from altus.tools.mcp import mcp_tools as build

    assert [t.name for t in build(McpSettings())] == [
        "mcp_servers",
        "mcp_tools",
        "mcp_call",
        "mcp_do",
    ]


def test_switches_remove_exactly_their_tools() -> None:
    from altus.config.models import McpSettings
    from altus.tools.mcp import mcp_tools as build

    assert [t.name for t in build(McpSettings(allow_writes=False))] == [
        "mcp_servers",
        "mcp_tools",
        "mcp_call",
    ]
    assert build(McpSettings(enabled=False)) == []


def test_a_read_only_registry_carries_no_mcp_do() -> None:
    from altus.config.models import McpSettings
    from altus.tools.registry import default_registry

    registry = default_registry(
        writes=False,
        kubernetes=False,
        aws=False,
        azure=False,
        gcp=False,
        mcp=True,
        mcp_settings=McpSettings(),
    )
    assert "mcp_call" in registry
    assert "mcp_do" not in registry


async def test_mcp_call_refuses_a_write_without_contacting_the_server() -> None:
    from altus.tools.mcp.reads import McpCallTool

    provider = FakeMcp()
    out = await McpCallTool().run(
        {"server": "github", "tool": "add_issue_comment", "arguments": {"body": "hi"}},
        make_ctx(provider),
    )
    assert out.is_error
    assert "mcp_do" in out.content
    assert provider.calls == []


async def test_mcp_do_sends_a_read_back_to_mcp_call() -> None:
    from altus.tools.mcp.mutations import McpDoTool

    provider = FakeMcp()
    out = await McpDoTool().run({"server": "github", "tool": "list_issues"}, make_ctx(provider))
    assert out.is_error
    assert "mcp_call" in out.content
    assert provider.calls == []


async def test_the_deny_sweep() -> None:
    """Refused, and the only thing that reached the server was the before-read.

    This is the assertion that caught most of the bugs in all four clouds.
    """
    from altus.tools.approval import Decision, RecordingPolicy
    from altus.tools.mcp.mutations import McpDoTool

    policy = RecordingPolicy(decision=Decision.DENY)
    provider = FakeMcp()
    out = await McpDoTool().run(
        {"server": "github", "tool": "issue_write", "arguments": {"owner": "acme", "repo": "b"}},
        make_ctx(provider, policy=policy),
    )
    assert out.denied
    assert [tool for _s, tool, _a in provider.calls] == ["issue_read"]


async def test_the_prompt_never_claims_a_preview() -> None:
    from altus.tools.approval import RecordingPolicy
    from altus.tools.mcp.mutations import McpDoTool

    policy = RecordingPolicy()
    provider = FakeMcp()
    provider.results["issue_read"] = "title: Billing is down\nstate: open"
    await McpDoTool().run(
        {
            "server": "github",
            "tool": "issue_write",
            "arguments": {"owner": "acme", "repo": "billing"},
        },
        make_ctx(provider, policy=policy),
    )
    request = policy.seen[0]
    assert "no preview exists" in request.dry_run
    assert "validated" not in request.dry_run
    assert "Billing is down" in request.dry_run
    assert request.target == "github: acme · billing"


async def test_a_tool_the_manifest_never_saw_says_so_in_the_prompt() -> None:
    from altus.tools.approval import RecordingPolicy
    from altus.tools.mcp.mutations import McpDoTool

    policy = RecordingPolicy()
    provider = FakeMcp()
    await McpDoTool().run(
        {"server": "github", "tool": "brand_new_tool", "arguments": {"owner": "acme"}},
        make_ctx(provider, policy=policy),
    )
    request = policy.seen[0]
    assert request.sensitivity is Sensitivity.PRIVILEGED
    assert request.needs_challenge
    assert not request.may_grant_always
    assert "not in Altus's manifest" in request.dry_run


async def test_a_protected_target_demands_a_typed_confirmation() -> None:
    from altus.tools.approval import RecordingPolicy
    from altus.tools.mcp.mutations import McpDoTool

    policy = RecordingPolicy()
    provider = FakeMcp()
    await McpDoTool().run(
        {
            "server": "github",
            "tool": "add_issue_comment",
            "arguments": {"owner": "acme", "repo": "prod-billing"},
        },
        make_ctx(provider, policy=policy),
    )
    assert policy.seen[0].protected
    assert policy.seen[0].needs_challenge


async def test_writes_disabled_refuses_before_anything_is_resolved() -> None:
    from altus.tools.mcp.mutations import McpDoTool

    provider = FakeMcp()
    out = await McpDoTool().run(
        {"server": "github", "tool": "add_issue_comment"},
        make_ctx(provider, allow_writes=False),
    )
    assert out.is_error
    assert "allow_writes" in out.content
    assert provider.calls == []


async def test_a_json_result_is_redacted_structurally() -> None:
    """The name/value shape --- which is what these servers actually return."""
    from altus.tools.mcp.reads import McpCallTool

    provider = FakeMcp()
    provider.results["list_issues"] = json.dumps(
        [{"name": "API_TOKEN", "value": "sk-live-abcdef"}, {"name": "REGION", "value": "eu-west-1"}]
    )
    out = await McpCallTool().run({"server": "github", "tool": "list_issues"}, make_ctx(provider))
    assert "sk-live-abcdef" not in out.content
    assert "redacted" in out.content
    assert "eu-west-1" in out.content, "redaction must not blank things that are not secret"


async def test_a_text_result_falls_through_to_the_text_scrubber() -> None:
    """`redact` does nothing to a string. Running only the structured pass over
    free text reads as working and redacts nothing at all."""
    from altus.tools.mcp.reads import McpCallTool

    provider = FakeMcp()
    provider.results["list_issues"] = "PATH=/usr/bin\nDB_PASSWORD=hunter2\nstate: open"
    out = await McpCallTool().run({"server": "github", "tool": "list_issues"}, make_ctx(provider))
    assert "hunter2" not in out.content
    assert "/usr/bin" in out.content


async def test_rows_are_capped() -> None:
    """Snowflake and Databricks answer with rows, and every row reaches the
    model provider."""
    from altus.tools.mcp.reads import McpCallTool

    provider = FakeMcp()
    provider.results["list_issues"] = "\n".join(f"row {i}" for i in range(500))
    out = await McpCallTool().run(
        {"server": "github", "tool": "list_issues"}, make_ctx(provider, max_rows=10)
    )
    assert "showing 10 of 500 rows" in out.content
    assert "row 11" not in out.content


async def test_mcp_tools_reports_sensitivity_for_every_tool() -> None:
    from altus.tools.mcp.reads import McpToolsTool

    out = await McpToolsTool().run({}, make_ctx(FakeMcp()))
    assert out.visual is not None
    levels = {row[2] for row in out.visual.rows}
    assert levels == {"read", "mutate", "privileged"}


async def test_mcp_servers_says_where_results_go() -> None:
    from altus.tools.mcp.reads import McpServersTool

    out = await McpServersTool().run({}, make_ctx(FakeMcp()))
    assert "leaves this machine" in out.content
    assert "documented" in out.content


async def test_mcp_servers_check_reports_drift() -> None:
    from altus.tools.mcp.reads import McpServersTool

    out = await McpServersTool().run({"check": True}, make_ctx(FakeMcp()))
    assert "brand_new_tool" in out.content
    assert "not in the manifest" in out.content


async def test_a_server_with_no_credentials_is_not_callable() -> None:
    from altus.tools.mcp.reads import McpCallTool

    provider = FakeMcp()
    out = await McpCallTool().run(
        {"server": "grafana", "tool": "list_datasources"},
        make_ctx(provider, servers=("github",)),
    )
    assert out.is_error
    assert "not enabled" in out.summary
    assert provider.calls == []


# --- /mcp ----------------------------------------------------------------


def test_mcp_is_listed_in_the_command_registry() -> None:
    from altus.tui.commands.builtin import build_registry

    registry = build_registry()
    assert "mcp" in registry.commands
    assert registry.commands["mcp"].handler is not None


async def test_the_mcp_command_lists_every_server_and_what_is_missing() -> None:
    """With no credentials anywhere --- which is what the test harness
    guarantees --- every server must be reported as absent with the variable
    to set, rather than silently omitted."""
    from tests.test_tui import _notices, _send, make_app

    app = make_app()
    async with app.run_test() as pilot:
        await _send(pilot, "/mcp")
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        rendered = _notices(pilot)
    for server in SERVERS:
        assert server in rendered
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" in rendered
    assert "no dry-run" in rendered


async def test_the_mcp_command_says_when_the_extra_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_tui import _notices, _send, make_app

    app = make_app()
    async with app.run_test() as pilot:
        pilot.app.tool_ctx.cloud.mcp = None
        await _send(pilot, "/mcp")
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        assert "uv sync --extra mcp" in _notices(pilot)


def test_both_doors_agree_on_what_is_enabled() -> None:
    """`/mcp` and `mcp_servers` must not answer this differently. The gcloud
    classifier already taught that lesson once."""
    from altus.config.models import McpServerSettings, McpSettings
    from altus.mcp.catalog import enabled_servers as shared
    from altus.tools.mcp.reads import McpServersTool

    for settings in (
        McpSettings(),
        McpSettings(servers=["github", "grafana"]),
        McpSettings(servers=["github"], github=McpServerSettings(enabled=False)),
        McpSettings(datadog=McpServerSettings(enabled=True)),
    ):
        ctx = ToolContext(
            workspace=Workspace(Path.cwd()),
            cloud=CloudContext(mcp=FakeMcp(), mcp_settings=settings),
        )
        assert [s.id for s in McpServersTool().enabled_servers(ctx)] == [
            s.id for s in shared(settings)
        ]


def test_datadog_is_told_it_needs_both_keys() -> None:
    """One of a two-header pair is not partial credentials, and "or" sends the
    user off to set one of two things and find it still does not work."""
    spec = server_spec("datadog")
    assert spec is not None
    assert spec.missing_hint == "set DD_API_KEY and DD_APPLICATION_KEY"
    github = server_spec("github")
    assert github is not None
    assert " or " in github.missing_hint


def test_the_readme_count_is_the_real_count() -> None:
    """The README cites 437 shipped manifest entries. A number in
    documentation that nothing checks is a number that goes stale."""
    from pathlib import Path as P

    total = sum(len(manifest(server).tools) for server in SERVERS)
    readme = (P(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
    assert f"{total} tool" in readme, f"README does not cite the real total, {total}"


# --- the failure path ----------------------------------------------------


def test_explain_digs_the_real_cause_out_of_a_task_group() -> None:
    """Both transports run inside anyio task groups, so a plain 401 arrives as
    "unhandled errors in a TaskGroup (1 sub-exception)", which tells nobody
    anything about a bad token."""
    from altus.mcp.session import explain

    group = ExceptionGroup("unhandled errors in a TaskGroup", [ValueError("401 Unauthorized")])
    assert explain(group) == "ValueError: 401 Unauthorized"
    nested = ExceptionGroup("outer", [ExceptionGroup("inner", [RuntimeError("boom")])])
    assert explain(nested) == "RuntimeError: boom"
    assert explain(ValueError("plain")) == "ValueError: plain"


def test_a_connection_that_fails_midway_cleans_up_in_its_own_task() -> None:
    """The regression for the bug that shipped.

    An earlier `_Session` drove __aenter__/__aexit__ by hand. Both transports
    wrap an anyio task group, and a task group must be exited by the task that
    entered it --- so when a connection failed *after* opening, cleanup was
    finalized by the garbage collector in another task and anyio raised
    "Attempted to exit cancel scope in a different task" on top of the real
    error, burying it.

    Three details here look odd, and every one of them is why the bug survived
    review. It needs a transport that opens and *then* fails, because a command
    that cannot start at all unwinds cleanly --- hence a process that starts,
    prints something that is not JSON-RPC, and exits. The failure never reaches
    the caller, so there is nothing to assert on. And it only shows up once the
    loop shuts down, which pytest-asyncio's loop never does mid-test --- so this
    runs under its own `asyncio.run`, in its own interpreter, and reads stderr.
    The in-process version of this test passed against the broken code.
    """
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent("""
        import asyncio, contextlib
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client
        from altus.mcp.session import _open

        async def main():
            params = StdioServerParameters(command="/bin/echo", args=["not-json"], env={})
            with contextlib.suppress(BaseException):
                async with _open(stdio_client(params)):
                    pass

        asyncio.run(main())
    """)
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=90
    )
    assert "cancel scope" not in done.stderr, (
        f"cleanup escaped the task that opened it:\n{done.stderr[-1500:]}"
    )


async def test_a_call_whose_blast_radius_is_unknown_demands_a_challenge() -> None:
    """Protection fails closed now. A call with no recognisable target could be
    landing anywhere, which is not a reason to accept a single keypress."""
    from altus.tools.approval import RecordingPolicy
    from altus.tools.mcp.mutations import McpDoTool

    policy = RecordingPolicy()
    provider = FakeMcp()
    await McpDoTool().run(
        {"server": "github", "tool": "add_issue_comment", "arguments": {"note": "hi"}},
        make_ctx(provider, policy=policy),
    )
    request = policy.seen[0]
    assert request.protected
    assert request.needs_challenge
    assert not request.may_grant_always
    assert request.target == "github: (unknown)"
    assert "could not be determined" in request.dry_run


async def test_a_resolvable_target_still_takes_a_keypress() -> None:
    """The fail-closed change must not make every write a challenge."""
    from altus.tools.approval import RecordingPolicy
    from altus.tools.mcp.mutations import McpDoTool

    policy = RecordingPolicy()
    await McpDoTool().run(
        {
            "server": "github",
            "tool": "add_issue_comment",
            "arguments": {"owner": "acme", "repo": "billing"},
        },
        make_ctx(FakeMcp(), policy=policy),
    )
    request = policy.seen[0]
    assert not request.protected
    assert not request.needs_challenge
    assert request.target == "github: acme · billing"


# --- the generator -------------------------------------------------------


@cache
def _refresh_module() -> Any:
    """Load `scripts/refresh_manifests.py`, which is dev tooling outside the
    package --- not shipped in the wheel, so there is nothing to import."""
    import importlib.util
    import sys

    path = Path(__file__).parent.parent / "scripts" / "refresh_manifests.py"
    spec = importlib.util.spec_from_file_location("altus_refresh_manifests", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_derived_manifest_covers_what_the_hand_written_one_missed() -> None:
    """The drift that prompted deriving these at all: the hand-written GitHub
    manifest had 82 of 122 tools, so one tool in three demanded a typed
    challenge to file an issue."""
    table = manifest("github")
    assert table.source == "derived"
    assert len(table.tools) > 110
    assert classify("github", "create_issue") is Sensitivity.MUTATE
    assert classify("github", "search_users") is Sensitivity.READ
    assert classify("github", "list_secret_scanning_alerts") is Sensitivity.SENSITIVE_READ


def test_an_adapter_that_finds_nothing_is_an_error_not_an_empty_manifest() -> None:
    """The failure that would look exactly like success. A scraper writing
    `tools = {}` classifies every tool on that server as privileged, and a
    green run would report it as clean."""
    refresh = _refresh_module()

    with pytest.raises(refresh.RefreshError, match="no tools at all"):
        refresh.Generated(tools={})


def test_overrides_survive_regeneration_and_beat_generated_entries() -> None:
    """The generator decides only read-or-write. Anything that needed judgement
    lives in [overrides], above [tools], and a regeneration must never undo it."""
    refresh = _refresh_module()

    made = refresh.Generated(
        tools={"run_thing": "read"}, target_fields=("owner",), upstream_ref="v1"
    )
    rendered = refresh.render(
        "demo",
        {"overrides": {"run_thing": "privileged"}, "reference": "x", "_header": "# demo"},
        made,
    )
    assert "[overrides]" in rendered
    assert 'run_thing = "privileged"' in rendered
    parsed = tomllib.loads(rendered)
    assert parsed["tools"]["run_thing"] == "read"
    assert parsed["overrides"]["run_thing"] == "privileged"


def test_an_override_for_a_vanished_tool_stops_the_refresh() -> None:
    """A vendor renaming something we deliberately escalated is exactly the
    case that must not pass silently."""
    refresh = _refresh_module()

    made = refresh.Generated(tools={"still_here": "read"}, upstream_ref="v1")
    with pytest.raises(refresh.RefreshError, match="no longer exist upstream"):
        refresh.render("demo", {"overrides": {"gone_away": "privileged"}}, made)


# --- CrowdStrike and ServiceNow ------------------------------------------


def test_the_crowdstrike_adapter_applies_the_prefix_the_server_applies() -> None:
    """The mistake this would have been. `_add_tool` registers
    `f"falcon_{name}"`, so a manifest built from the names in the source
    matches nothing the server ever publishes --- and a manifest matching
    nothing looks exactly like a server with 166 unknown tools, which is to
    say it looks like the thing the manifest exists to prevent.
    """
    refresh = _refresh_module()
    sample = """
class Spotlight:
    def register_tools(self, server):
        self._add_tool(server=server, method=self.search_vulnerabilities,
                       name="search_vulnerabilities")
        self._add_tool(server=server, method=self.update_thing, name="update_thing",
                       annotations=ToolAnnotations(readOnlyHint=False))

    def register_resources(self, server):
        self._add_resource(server, TextResource(name="search_vulnerabilities_fql_guide"))
"""
    tools = _run_crowdstrike_adapter(refresh, sample)
    assert tools == {
        "falcon_search_vulnerabilities": "read",
        "falcon_update_thing": "mutate",
    }


def test_the_crowdstrike_adapter_ignores_resources() -> None:
    """The same modules build `TextResource(name=...)` objects for their FQL
    guides. Those are resources, not tools, and a regex over `name="..."`
    would have swept every one of them into the tool table."""
    refresh = _refresh_module()
    tools = _run_crowdstrike_adapter(
        refresh,
        "def register_resources(self, s):\n"
        '    self._add_resource(s, TextResource(name="some_fql_guide"))\n'
        "def register_tools(self, s):\n"
        '    self._add_tool(server=s, method=self.x, name="real_tool")\n',
    )
    assert tools == {"falcon_real_tool": "read"}


def _run_crowdstrike_adapter(refresh: Any, source: str) -> dict[str, str]:
    """Drive the adapter over one in-memory module, never the network."""
    import unittest.mock

    with unittest.mock.patch.object(
        refresh, "_falcon_sources", lambda _path: [("modules/sample.py", source)]
    ):
        return dict(refresh.crowdstrike_adapter().tools)


def test_a_tool_name_that_is_not_a_literal_stops_the_refresh() -> None:
    """A computed name means the derivation is no longer complete, and an
    incomplete derived manifest is worse than an honest curated one."""
    refresh = _refresh_module()
    with pytest.raises(refresh.RefreshError, match="not a literal"):
        _run_crowdstrike_adapter(refresh, "self._add_tool(server=s, method=m, name=PREFIX + x)\n")


def test_real_time_response_is_privileged_whatever_upstream_calls_it() -> None:
    """CrowdStrike annotates `execute_rtr_read_only_command` read-only because
    the *command* only reads. It is still an arbitrary command run on
    somebody's host, which is the same argument that promotes Datadog's
    `execute_code` and makes `pods/exec` outrank its verb.
    """
    assert manifest("crowdstrike").tools["falcon_execute_rtr_read_only_command"] == (
        Sensitivity.MUTATE
    ), "upstream's own reading, kept in [tools]"
    assert classify("crowdstrike", "falcon_execute_rtr_read_only_command") is (
        Sensitivity.PRIVILEGED
    ), "and overridden, because the override is what ships"
    for tool in ("falcon_init_rtr_session", "falcon_run_rtr_read_only_command_and_wait"):
        assert classify("crowdstrike", tool) is Sensitivity.PRIVILEGED


def test_a_control_that_disables_a_control_is_classed_with_what_it_disables() -> None:
    """The Azure resource-lock argument, applied to an EDR: removing a lock is
    how you get around a lock. An exclusion blinds the sensor and a prevention
    policy action turns protection off on every host it covers."""
    for tool in (
        "falcon_create_exclusion",
        "falcon_delete_exclusions",
        "falcon_perform_policy_action",
        "falcon_delete_policies",
        "falcon_update_quarantined_files",
    ):
        assert classify("crowdstrike", tool) is Sensitivity.PRIVILEGED, tool


def test_ordinary_crowdstrike_writes_are_not_escalated() -> None:
    """The other half of the argument. Promoting everything to privileged is
    challenge fatigue, which is how a challenge stops being read."""
    assert classify("crowdstrike", "falcon_create_case") is Sensitivity.MUTATE
    assert classify("crowdstrike", "falcon_update_detections") is Sensitivity.MUTATE
    assert classify("crowdstrike", "falcon_search_vulnerabilities") is Sensitivity.READ


def test_servicenow_cannot_have_a_name_manifest_and_says_so() -> None:
    """Role-based tool packages mean one instance's tools are not another's.
    That is Snowflake's situation, not GitHub's: not stale, impossible."""
    spec = server_spec("servicenow")
    assert spec is not None
    assert manifest("servicenow").tools == {}
    assert spec.unknown is Sensitivity.PRIVILEGED
    assert "role-based" in spec.unknown_why
    assert classify("servicenow", "anything_at_all") is Sensitivity.PRIVILEGED


def test_servicenow_ships_no_endpoint_because_there_is_none_to_ship() -> None:
    """The New Relic lesson: an invented endpoint is worse than none. The MCP
    endpoint is a service record an administrator creates on their instance."""
    spec = server_spec("servicenow")
    assert spec is not None
    assert spec.url == ""
    assert "MCP Server Console" in spec.notes
    assert "dynamic client registration" in spec.notes, "and why OAuth is not used"


# --- the custom door -----------------------------------------------------


def custom_config(**over: Any) -> Any:
    from altus.config.models import Config, McpCustomSettings

    config = Config()
    config.mcp.custom["darktrace"] = McpCustomSettings(
        **{"url": "https://mcp.internal/dt", "env": ["DARKTRACE_TOKEN"], **over}
    )
    return config.mcp


def test_a_custom_server_joins_the_catalogue_but_is_marked_as_custom() -> None:
    """The catalogue can never be finished --- Darktrace has no official MCP
    server today --- and the alternative to a door is somebody forking Altus."""
    from altus.mcp.catalog import catalog, custom_servers

    settings = custom_config()
    assert [spec.id for spec in catalog(settings)][-1] == "darktrace"
    (spec,) = custom_servers(settings)
    assert spec.custom and spec.source == "custom"
    assert server_spec("darktrace", settings) is not None
    assert server_spec("darktrace") is None, "and never without being configured"


def test_every_tool_on_a_custom_server_fails_closed() -> None:
    """Altus has read no manifest, no documentation and no source for it, so
    the only honest position is that any tool could do anything."""
    assert classify("darktrace", "get_device_summary") is Sensitivity.PRIVILEGED
    assert classify("darktrace", "list_model_breaches") is Sensitivity.PRIVILEGED


def test_a_custom_server_can_lower_its_own_tools_but_not_raise_its_trust() -> None:
    """Annotations stay a ceiling. A custom server saying "this is read-only"
    is believed downward, which is the only direction that is safe."""
    said_read_only = ToolInfo(name="get_device", read_only_hint=True)
    said_write = ToolInfo(name="get_device", read_only_hint=False)
    assert classify("darktrace", "get_device", said_read_only) is Sensitivity.PRIVILEGED
    assert classify("darktrace", "get_device", said_write) is Sensitivity.PRIVILEGED


def test_a_stdio_custom_server_is_detected_from_its_command() -> None:
    from altus.mcp.catalog import Transport, custom_servers

    (spec,) = custom_servers(custom_config(url="", command=["my-mcp-server"]))
    assert spec.transport is Transport.STDIO
    assert spec.command == ("my-mcp-server",)


def test_a_custom_servers_url_reaches_the_provider_the_same_way() -> None:
    """One door onto per-server settings, so nothing downstream has to know
    which kind of server it is holding."""
    assert custom_config().for_server("darktrace").url == "https://mcp.internal/dt"
    assert "darktrace" in custom_config().server_ids


def test_a_server_with_no_manifest_says_why_rather_than_showing_a_zero() -> None:
    """ "0 tools in Altus's manifest" reads like a manifest that happens to be
    empty rather than one that cannot be written. ServiceNow's cannot."""
    spec = server_spec("servicenow")
    assert spec is not None
    assert manifest("servicenow").tools == {}
    assert spec.unknown_why, "which is the sentence /mcp and the prompt both show"
