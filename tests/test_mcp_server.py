"""Altus as an MCP server: what it offers, and what it refuses to.

The exposure policy is pinned by name rather than by count. A count catches a
tool disappearing; only a list catches the wrong one appearing, and "the wrong
one appeared" is the failure that matters here --- this surface hands other
people's software a process holding four sets of cloud credentials.
"""

from __future__ import annotations

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
from altus.tools.base import sensitivity_of
from altus.tools.registry import ToolRegistry, default_registry

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
