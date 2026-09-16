"""The workflow artifact: what it is, where it lives, and how dangerous it is.

Nothing here runs a step --- there is no engine yet, and these tests are the
guard that stops one being implied. What they do assert is that the designer
can never overstate what it knows: every level it prints is either certain or
carries the reason it is only a floor.
"""

from __future__ import annotations

import pytest

from altus.cloud.base import Sensitivity
from altus.core.errors import ConfigError
from altus.tools.base import BaseTool, ToolContext, ToolOutcome, dispatches, sensitivity_of
from altus.tools.registry import ToolRegistry, default_registry
from altus.workflow import (
    AgentStep,
    ApprovalStep,
    ToolStep,
    Workflow,
    blast_radius,
    check,
    fatal,
    list_workflows,
    load,
    parse,
    path_for,
    render,
    save,
)

# ----------------------------------------------------------------- fake tools


class FakeTool(BaseTool):
    async def run(self, args: dict, ctx: ToolContext) -> ToolOutcome:  # pragma: no cover
        return ToolOutcome(content="")


def fake(name: str, *, level: Sensitivity = Sensitivity.READ, varies: bool = False) -> BaseTool:
    """A tool with a declared level, so a test does not depend on a real one."""
    return type(
        f"Fake_{name}",
        (FakeTool,),
        {
            "name": name,
            "read_only": level is Sensitivity.READ,
            "dispatches": varies,
            "static_sensitivity": classmethod(lambda cls, _level=level: _level),
        },
    )()


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry(
        [
            fake("k8s_topology"),
            fake("k8s_get"),
            fake("k8s_apply", level=Sensitivity.MUTATE, varies=True),
            fake("k8s_exec", level=Sensitivity.PRIVILEGED, varies=True),
            fake("mcp_call", level=Sensitivity.SENSITIVE_READ),
        ]
    )


# --------------------------------------------------------------------- models


def test_a_workflow_round_trips_through_toml_unchanged() -> None:
    """Including `needs`, which is the part the engine will depend on."""
    original = Workflow(
        name="deploy-api",
        description="ship the API",
        steps=[
            ToolStep(id="inventory", tool="k8s_topology", args={"namespace": "default"}),
            AgentStep(id="plan", needs=["inventory"], prompt="what changed?", tools=["k8s_get"]),
            ApprovalStep(id="approve", needs=["plan"], message="ship it?"),
            ToolStep(id="apply", needs=["approve"], tool="k8s_apply"),
        ],
    )
    assert parse(render(original), name="deploy-api") == original


@pytest.mark.parametrize("bad", ["../escape", "Deploy", "deploy/api", "", "-lead", "a" * 65])
def test_names_and_ids_that_could_escape_a_directory_are_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        Workflow(name=bad)


def test_an_unknown_step_kind_is_refused_rather_than_guessed() -> None:
    """A `shell` step is coming one day. Until it does, it must not parse into
    something that looks runnable."""
    with pytest.raises(ConfigError, match="not a usable workflow"):
        parse('name = "x"\n[[steps]]\nid = "a"\nkind = "shell"\ncmd = "rm -rf /"', name="x")


# ---------------------------------------------------------------------- store


def test_the_filename_wins_over_a_stale_name_inside_the_file() -> None:
    """A file copied to a new name becomes that workflow.

    Trusting the key instead would let a copy silently overwrite its original
    on the next save.
    """
    assert parse('name = "old"\n[[steps]]\nid="a"\nkind="approval"', name="new").name == "new"


def test_save_refuses_a_name_that_would_leave_the_directory() -> None:
    """`workflow_save` is driven by the model, so this is reachable by a prompt."""
    with pytest.raises(ConfigError):
        path_for("../../.ssh/authorized_keys")


def test_save_then_list_then_load(tmp_path) -> None:
    workflow = Workflow(name="nightly", steps=[ApprovalStep(id="go", message="?")])
    written = save(workflow)
    assert written.exists()
    assert list_workflows() == ["nightly"]
    assert load("nightly") == workflow


def test_loading_something_that_is_not_a_workflow_says_so(tmp_path) -> None:
    save(Workflow(name="wrong", steps=[ApprovalStep(id="a")]))
    path_for("wrong").write_text("name = 'wrong'\nsteps = 'not a list'\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not a usable workflow"):
        load("wrong")


# ------------------------------------------------------------------ validation


def test_a_workflow_with_no_steps_has_nothing_to_run(registry: ToolRegistry) -> None:
    assert fatal(check(Workflow(name="empty"), registry))


def test_a_cycle_is_reported_with_both_ends_named(registry: ToolRegistry) -> None:
    """ "There is a cycle" leaves the author to find it by hand."""
    workflow = Workflow(
        name="loop",
        steps=[
            ToolStep(id="a", needs=["b"], tool="k8s_get"),
            ToolStep(id="b", needs=["a"], tool="k8s_get"),
        ],
    )
    (problem,) = [p for p in check(workflow, registry) if "circular" in p.message]
    assert "a" in problem.message and "b" in problem.message


def test_needs_pointing_nowhere_is_fatal(registry: ToolRegistry) -> None:
    workflow = Workflow(name="x", steps=[ToolStep(id="a", needs=["ghost"], tool="k8s_get")])
    assert any(p.fatal and "ghost" in p.message for p in check(workflow, registry))


def test_a_step_cannot_depend_on_itself(registry: ToolRegistry) -> None:
    workflow = Workflow(name="x", steps=[ToolStep(id="a", needs=["a"], tool="k8s_get")])
    assert any(p.fatal and "itself" in p.message for p in check(workflow, registry))


def test_a_duplicate_id_is_fatal(registry: ToolRegistry) -> None:
    workflow = Workflow(
        name="x",
        steps=[ToolStep(id="a", tool="k8s_get"), ToolStep(id="a", tool="k8s_get")],
    )
    assert any(p.fatal for p in check(workflow, registry))


def test_a_tool_that_exists_nowhere_is_fatal(registry: ToolRegistry) -> None:
    workflow = Workflow(name="x", steps=[ToolStep(id="a", tool="not_a_tool")])
    assert any(p.fatal and "not_a_tool" in p.message for p in check(workflow, registry))


def test_a_tool_from_an_uninstalled_extra_is_a_warning_not_an_error(
    registry: ToolRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workflow is fine; this machine is missing an extra.

    Collapsing the two would make a workflow unvalidatable anywhere but the
    laptop that wrote it, which defeats the point of a file you can commit.
    """
    from altus.cloud.base import Integration

    monkeypatch.setattr(
        "altus.cloud.base.integration",
        lambda name: Integration(
            name="gcp",
            extra="gcp",
            modules=("no_such_module_anywhere",),
            summary="Google Cloud",
        ),
    )
    workflow = Workflow(name="x", steps=[ToolStep(id="a", tool="gcp_write")])
    (problem,) = check(workflow, registry)
    assert not problem.fatal
    assert "uv sync --extra gcp" in problem.message


def test_an_agent_step_naming_an_absent_tool_still_runs(registry: ToolRegistry) -> None:
    workflow = Workflow(name="x", steps=[AgentStep(id="a", prompt="go", tools=["nope_gone"])])
    (problem,) = check(workflow, registry)
    assert not problem.fatal


# ---------------------------------------------------------------- blast radius


def test_the_level_is_the_strictest_step(registry: ToolRegistry) -> None:
    workflow = Workflow(
        name="x",
        steps=[
            ToolStep(id="look", tool="k8s_topology"),
            ToolStep(id="read", tool="mcp_call"),
            ToolStep(id="change", tool="k8s_apply"),
        ],
    )
    assert blast_radius(workflow, registry).level is Sensitivity.MUTATE


def test_a_dispatching_step_makes_the_level_a_floor(registry: ToolRegistry) -> None:
    """The whole point. `k8s_apply` writes a ConfigMap and a ClusterRoleBinding,
    and which one is decided by arguments nobody has written yet."""
    workflow = Workflow(name="x", steps=[ToolStep(id="change", tool="k8s_apply")])
    radius = blast_radius(workflow, registry)
    assert radius.unresolved == ("change",)
    assert not radius.certain
    assert "(at least)" in radius.render()


def test_a_tool_already_at_the_top_of_the_scale_is_not_unresolved(
    registry: ToolRegistry,
) -> None:
    """PRIVILEGED is the ceiling, so there is nothing left to escalate to."""
    workflow = Workflow(name="x", steps=[ToolStep(id="shell", tool="k8s_exec")])
    radius = blast_radius(workflow, registry)
    assert radius.level is Sensitivity.PRIVILEGED
    assert radius.unresolved == ()
    assert radius.certain


def test_an_agent_step_with_no_named_tools_reaches_the_worst_of_them(
    registry: ToolRegistry,
) -> None:
    """An unrestricted agent step is as dangerous as the session's worst tool.

    That is uncomfortable and it is true, and saying it is the only thing that
    makes narrowing `tools` worth an author's while.
    """
    workflow = Workflow(name="x", steps=[AgentStep(id="think", prompt="do it")])
    radius = blast_radius(workflow, registry)
    assert radius.level is Sensitivity.PRIVILEGED
    assert radius.open_ended == ("think",)
    assert "any tool in the session" in " ".join(radius.notes())


def test_naming_tools_narrows_an_agent_step(registry: ToolRegistry) -> None:
    workflow = Workflow(name="x", steps=[AgentStep(id="think", prompt="do it", tools=["k8s_get"])])
    assert blast_radius(workflow, registry).level is Sensitivity.READ


def test_a_step_naming_an_unknown_tool_contributes_nothing_and_says_so(
    registry: ToolRegistry,
) -> None:
    """Fail-closed would read PRIVILEGED for a typo, which is crying wolf.

    Reporting it separately is the honest version: the level below is real,
    and this step is simply not in it.
    """
    workflow = Workflow(
        name="x",
        steps=[ToolStep(id="look", tool="k8s_topology"), ToolStep(id="huh", tool="gone")],
    )
    radius = blast_radius(workflow, registry)
    assert radius.level is Sensitivity.READ
    assert radius.unknown == ("huh",)
    assert not radius.certain


def test_an_approval_step_changes_nothing_by_itself(registry: ToolRegistry) -> None:
    workflow = Workflow(name="x", steps=[ApprovalStep(id="ask", message="?")])
    assert blast_radius(workflow, registry).level is Sensitivity.READ


# ------------------------------------------------- one door onto "how dangerous"


def test_every_real_tool_has_a_derivable_sensitivity() -> None:
    """The two-doors regression. `/tools` and the designer must agree, which
    they can only do by asking the same function."""
    for tool in default_registry():
        level = sensitivity_of(tool)
        assert isinstance(level, Sensitivity)
        assert (level is Sensitivity.READ) == tool.read_only


def test_azure_verbs_are_not_run_through_the_kubernetes_classifier() -> None:
    """`azure_write` declares verb="write", which Kubernetes has never heard of.

    Feeding it to the Kubernetes classifier hit the unknown-verb fallback and
    reported every Azure mutation as privileged --- which is what `/tools`
    printed before `sensitivity_of` existed.
    """
    registry = default_registry()
    for name in ("azure_write", "azure_delete", "azure_action"):
        tool = registry.get(name)
        if tool is None:
            pytest.skip("azure extra not installed")
        assert sensitivity_of(tool) is Sensitivity.MUTATE
        assert dispatches(tool), "one tool over the whole ARM surface"


def test_the_kubernetes_subresource_still_outranks_its_verb() -> None:
    registry = default_registry()
    tool = registry.get("k8s_exec")
    if tool is None:
        pytest.skip("k8s extra not installed")
    assert sensitivity_of(tool) is Sensitivity.PRIVILEGED
