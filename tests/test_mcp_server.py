"""Altus as an MCP server: what it offers, and what it refuses to.

The exposure policy is pinned by name rather than by count. A count catches a
tool disappearing; only a list catches the wrong one appearing, and "the wrong
one appeared" is the failure that matters here --- this surface hands other
people's software a process holding four sets of cloud credentials.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from altus.cloud.base import Sensitivity
from altus.config.models import CloudSettings, ExposeSettings, ShellSettings
from altus.mcp.expose import (
    DROPPED,
    NEVER,
    annotations_for,
    exposed,
    why_not,
    withheld,
)
from altus.tools.base import ToolContext, sensitivity_of
from altus.tools.registry import ToolRegistry, default_registry
from altus.workspace import Workspace

#: Everything a default server offers. Written out because this list is the
#: security boundary, and a diff on it is the review.
OFFERED = {
    "aws_call",
    "aws_can_i",
    "aws_cost",
    "aws_explain",
    "aws_inventory",
    "aws_quotas",
    "aws_regions",
    "aws_services",
    "aws_topology",
    "aws_whoami",
    "azure_can_i",
    "azure_cost",
    "azure_explain",
    "azure_get",
    "azure_inventory",
    "azure_providers",
    "azure_query",
    "azure_quotas",
    "azure_subscriptions",
    "azure_topology",
    "azure_whoami",
    "gcp_apis",
    "gcp_assets",
    "gcp_call",
    "gcp_can_i",
    "gcp_cost",
    "gcp_explain",
    "gcp_inventory",
    "gcp_projects",
    "gcp_quotas",
    "gcp_topology",
    "gcp_whoami",
    "git_diff",
    "git_log",
    "git_status",
    "k8s_api_resources",
    "k8s_can_i",
    "k8s_contexts",
    "k8s_events",
    "k8s_explain",
    "k8s_get",
    "k8s_list",
    "k8s_logs",
    "k8s_rollout_status",
    "k8s_storage",
    "k8s_top",
    "k8s_topology",
    "k8s_usage",
    "k8s_wait",
}


def registry() -> ToolRegistry:
    """Everything a real session has, CLI fallbacks and shell included --- the
    point is what happens to those, so a registry without them proves nothing."""
    return default_registry(cloud=CloudSettings(), shell=ShellSettings(allow=["make", "pytest"]))


# ------------------------------------------------------------------ the surface


def test_the_default_surface_is_exactly_this() -> None:
    assert {tool.name for tool in exposed(registry(), ExposeSettings())} == OFFERED


def test_everything_offered_by_default_is_a_read() -> None:
    for tool in exposed(registry(), ExposeSettings()):
        assert sensitivity_of(tool) is Sensitivity.READ, tool.name


def test_writes_add_the_cloud_and_git_mutations_and_nothing_else() -> None:
    with_writes = {tool.name for tool in exposed(registry(), ExposeSettings(allow_writes=True))}
    added = with_writes - OFFERED

    assert added == {
        "aws_write",
        "azure_action",
        "azure_delete",
        "azure_write",
        "gcp_write",
        "git_branch",
        "git_commit",
        "git_push",
        "k8s_apply",
        "k8s_create",
        "k8s_delete",
        "k8s_drain",
        "k8s_node",
        "k8s_patch",
        "k8s_replace",
        "k8s_rollout",
        "k8s_scale",
    }


# -------------------------------------------------------------- what never goes


@pytest.mark.parametrize("name", sorted(NEVER))
def test_the_never_list_survives_every_way_of_asking(name: str) -> None:
    """The three things somebody would try: turn writes on, name it in `extra`,
    and both at once. An allowlist a config file can talk past is not one."""
    wide_open = ExposeSettings(allow_writes=True, extra=[name], workflows=True)
    offered = {tool.name for tool in exposed(registry(), wide_open)}

    assert name not in offered
    assert why_not(name, wide_open, registry()) == NEVER[name]


def test_the_cli_fallbacks_are_refused_structurally() -> None:
    """By what they are, not by their names --- a binary added to that layer
    later is covered without anybody remembering to update a list."""
    wide_open = ExposeSettings(allow_writes=True, extra=["k8s_kubectl", "aws_cli"])
    offered = {tool.name for tool in exposed(registry(), wide_open)}

    assert not offered & {"k8s_kubectl", "helm", "kustomize", "aws_cli", "azure_cli", "gcp_cli"}
    assert "shells out to kubectl" in why_not("k8s_kubectl", wide_open, registry())


def test_no_tool_that_relays_to_a_vendor_server_is_ever_offered() -> None:
    """The confused deputy this whole module exists to prevent: a client
    reaching CrowdStrike through Altus's credentials."""
    wide_open = ExposeSettings(allow_writes=True)
    offered = {tool.name for tool in exposed(registry(), wide_open)}

    assert not any(name.startswith("mcp_") for name in offered)


def test_the_filesystem_is_absent_in_both_directions() -> None:
    wide_open = ExposeSettings(allow_writes=True)
    offered = {tool.name for tool in exposed(registry(), wide_open)}

    assert not offered & {"read_file", "list_dir", "glob", "grep"}
    assert not offered & {"write_file", "edit_file", "delete_path"}


# ------------------------------------------------------------- what can be moved


@pytest.mark.parametrize("name", sorted(DROPPED))
def test_a_dropped_tool_can_be_added_back_by_name(name: str) -> None:
    offered = {tool.name for tool in exposed(registry(), ExposeSettings(extra=[name]))}
    assert name in offered


def test_deny_removes_something_that_would_be_offered() -> None:
    settings = ExposeSettings(deny=["aws_cost"])
    offered = {tool.name for tool in exposed(registry(), settings)}

    assert "aws_cost" not in offered
    assert "deny" in why_not("aws_cost", settings, registry())


def test_deny_beats_extra() -> None:
    settings = ExposeSettings(extra=["grep"], deny=["grep"])
    assert "grep" not in {tool.name for tool in exposed(registry(), settings)}


# --------------------------------------------------------------- the annotations


def test_annotations_never_advertise_a_write_as_read_only() -> None:
    """The invariant we hold vendor servers to, applied to ourselves. Ours are
    computed from the same classifier /tools prints, not typed by hand."""
    for tool in registry():
        hints = annotations_for(tool)
        assert hints["readOnlyHint"] is (sensitivity_of(tool) is Sensitivity.READ), tool.name
        assert hints["readOnlyHint"] is not hints["destructiveHint"], tool.name


def test_cloud_tools_say_they_touch_the_outside_world() -> None:
    assert annotations_for(registry().get("k8s_topology"))["openWorldHint"]
    assert not annotations_for(registry().get("read_file"))["openWorldHint"]


# ------------------------------------------------------------------- the reasons


def test_everything_withheld_says_why() -> None:
    """A caller that names a withheld tool learns *that* it was withheld and
    why --- "unknown tool" would be a lie about a tool that exists."""
    for name, reason in withheld(registry(), ExposeSettings()):
        assert reason, name
        assert len(reason) > 20, name


def test_a_tool_that_does_not_exist_says_so_rather_than_pretending() -> None:
    assert "no tool called" in why_not("nonsense_tool", ExposeSettings(), registry())


# --------------------------------------------------------------- on the wire


@asynccontextmanager
async def connected(
    registry: ToolRegistry,
    ctx: ToolContext,
    settings: ExposeSettings,
    *,
    config: object = None,
    elicit: object = None,
) -> AsyncIterator[object]:
    """A real client talking to a real server over in-memory pipes.

    Driven through the protocol rather than by calling the handlers, because
    the handlers are the easy half: what this catches is a shape the SDK
    rejects, which is exactly what asserting on our own functions would miss.
    """
    import anyio
    from mcp import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    from altus.mcp.serve import build

    server = build(registry, ctx, settings, config=config)
    async with (
        create_client_server_memory_streams() as ((cr, cw), (sr, sw)),
        anyio.create_task_group() as group,
    ):

        async def run() -> None:
            await server.run(sr, sw, server.create_initialization_options())

        group.start_soon(run)
        async with ClientSession(cr, cw, elicitation_callback=elicit) as session:
            await session.initialize()
            yield session
        group.cancel_scope.cancel()


def session_context(tmp_path: Path, policy: object = None) -> ToolContext:
    from altus.tools.approval import RecordingPolicy

    return ToolContext(
        workspace=Workspace(root=tmp_path),
        approvals=policy or RecordingPolicy(),
    )


async def test_a_client_sees_the_curated_surface(tmp_path: Path) -> None:
    async with connected(registry(), session_context(tmp_path), ExposeSettings()) as client:
        listed = await client.list_tools()

    names = {tool.name for tool in listed.tools}
    assert names == OFFERED
    assert not names & set(NEVER)


async def test_the_annotations_reach_the_client(tmp_path: Path) -> None:
    """A client applying the same suspicion to us that we apply to vendors gets
    the same answer either way, because ours are computed."""
    async with connected(registry(), session_context(tmp_path), ExposeSettings()) as client:
        listed = await client.list_tools()

    topology = next(tool for tool in listed.tools if tool.name == "k8s_topology")
    assert topology.annotations is not None
    assert topology.annotations.read_only_hint is True
    assert topology.annotations.destructive_hint is False
    assert topology.annotations.open_world_hint is True


async def test_a_read_runs_through_the_same_registry(tmp_path: Path) -> None:
    async with connected(registry(), session_context(tmp_path), ExposeSettings()) as client:
        result = await client.call_tool("k8s_contexts", {})

    assert result.content
    assert result.content[0].type == "text"


async def test_a_withheld_tool_says_it_was_withheld(tmp_path: Path) -> None:
    """Not "unknown tool" --- that would be a lie about a tool that exists, and
    would send the caller hunting for a typo that is not there."""
    async with connected(registry(), session_context(tmp_path), ExposeSettings()) as client:
        result = await client.call_tool("mcp_do", {"server": "github", "tool": "x"})

    assert result.is_error
    text = result.content[0].text
    assert "not offered" in text
    assert "credentials" in text


async def test_a_mutation_is_not_even_listed_without_allow_writes(tmp_path: Path) -> None:
    async with connected(registry(), session_context(tmp_path), ExposeSettings()) as client:
        listed = await client.list_tools()
        result = await client.call_tool("k8s_delete", {"kind": "Pod", "name": "x"})

    assert "k8s_delete" not in {tool.name for tool in listed.tools}
    assert result.is_error
    assert "allow_writes" in result.content[0].text


async def test_the_server_says_what_it_is_for(tmp_path: Path) -> None:
    async with connected(registry(), session_context(tmp_path), ExposeSettings()) as client:
        assert "redacted" in (client.instructions or "")
