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


# ------------------------------------------------------------- the designer


def make_app():  # type: ignore[no-untyped-def]
    from altus.config.models import Config
    from altus.tui.app import AltusApp
    from tests.test_tui import FakeProvider

    return AltusApp(config=Config(), provider=FakeProvider())


async def test_the_designer_opens_adds_a_step_and_saves(registry: ToolRegistry) -> None:
    """The whole loop, through the real screens rather than their internals.

    This is the test that would have caught the step form saving a prompt onto
    an approval step, which is why the kind is chosen before the form is built.
    """
    from altus.tui.screens.workflow import WorkflowScreen

    app = make_app()
    async with app.run_test() as pilot:
        app.registry = registry  # type: ignore[assignment]
        screen = WorkflowScreen(Workflow(name="nightly"))
        await app.push_screen(screen)
        await pilot.pause()

        screen.workflow.steps.append(ToolStep(id="look", tool="k8s_topology"))
        screen._changed()
        await pilot.pause()
        assert screen.dirty

        screen.action_save()
        await pilot.pause()
        assert not screen.dirty
        assert load("nightly").steps[0].id == "look"


async def test_deleting_a_step_takes_its_edges_with_it(registry: ToolRegistry) -> None:
    """Otherwise the author is left with a fatal `needs` they never wrote."""
    from textual.widgets import OptionList

    from altus.tui.screens.workflow import WorkflowScreen

    workflow = Workflow(
        name="x",
        steps=[
            ToolStep(id="first", tool="k8s_get"),
            ToolStep(id="second", needs=["first"], tool="k8s_get"),
        ],
    )
    app = make_app()
    async with app.run_test() as pilot:
        app.registry = registry  # type: ignore[assignment]
        screen = WorkflowScreen(workflow)
        await app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#steps", OptionList).highlighted = 0
        screen.action_delete()
        await pilot.pause()

        assert workflow.ids == ["second"]
        assert workflow.steps[0].needs == []
        assert not fatal(check(workflow, registry))


async def test_escape_with_unsaved_changes_does_not_discard_on_the_first_press(
    registry: ToolRegistry,
) -> None:
    from altus.tui.screens.workflow import WorkflowScreen

    app = make_app()
    async with app.run_test() as pilot:
        app.registry = registry  # type: ignore[assignment]
        screen = WorkflowScreen(Workflow(name="x", steps=[ApprovalStep(id="a")]))
        await app.push_screen(screen)
        await pilot.pause()
        screen._changed()

        screen.action_close()
        await pilot.pause()
        assert app.screen is screen, "a single escape must not throw away the work"

        screen.action_close()
        await pilot.pause()
        assert app.screen is not screen


async def test_the_step_form_refuses_arguments_that_are_not_a_json_object(
    registry: ToolRegistry,
) -> None:
    from textual.widgets import Input, Label

    from altus.tui.widgets.step_form import StepForm

    app = make_app()
    async with app.run_test() as pilot:
        form = StepForm("tool", registry)
        await app.push_screen(form)
        await pilot.pause()
        form.query_one("#id", Input).value = "look"
        form.query_one("#tool", Input).value = "k8s_get"
        form.query_one("#args", Input).value = "[1, 2]"
        form.action_submit()
        await pilot.pause()

        assert app.screen is form, "a bad form does not dismiss"
        assert "JSON object" in str(form.query_one("#problem", Label).render())


async def test_the_tool_picker_shows_what_each_tool_costs(registry: ToolRegistry) -> None:
    """The point of the picker: the level is visible at the moment of choosing,
    not discovered later at a gate."""
    from altus.tui.widgets.step_form import ToolPicker

    picker = ToolPicker(registry)
    rows = dict(picker.rows)
    assert rows["k8s_topology"] == "read"
    assert rows["k8s_apply"] == "mutate — arguments decide"


# --------------------------------------------------- conversational drafting


def make_ctx(registry: ToolRegistry, policy=None, settings=None):  # type: ignore[no-untyped-def]
    from altus.config.models import WorkflowSettings
    from altus.tools.approval import RecordingPolicy
    from altus.workspace import Workspace

    return ToolContext(
        workspace=Workspace(root=__import__("pathlib").Path.cwd()),
        approvals=policy or RecordingPolicy(),
        registry=registry,
        workflow_settings=settings or WorkflowSettings(),
    )


def draft(**over):  # type: ignore[no-untyped-def]
    payload = {
        "name": "nightly",
        "steps": [
            {"id": "look", "kind": "tool", "tool": "k8s_topology"},
            {"id": "ask", "kind": "approval", "needs": ["look"], "message": "ok?"},
        ],
    }
    payload.update(over)
    return payload


async def test_saving_a_drafted_workflow_asks_first_and_shows_the_whole_file(
    registry: ToolRegistry,
) -> None:
    """Approving a workflow is approving every action queued inside it, so the
    request carries the file itself rather than a summary of it."""
    from altus.tools.approval import RecordingPolicy
    from altus.tools.workflow import WorkflowSaveTool

    policy = RecordingPolicy()
    ctx = make_ctx(registry, policy)
    outcome = await WorkflowSaveTool().run(draft(), ctx)

    assert not outcome.is_error
    (request,) = policy.seen
    assert "k8s_topology" in request.diff
    assert "does not run anything" in request.dry_run
    assert load("nightly").ids == ["look", "ask"]


async def test_a_declined_save_writes_nothing(registry: ToolRegistry) -> None:
    from altus.tools.approval import Decision, RecordingPolicy
    from altus.tools.workflow import WorkflowSaveTool

    ctx = make_ctx(registry, RecordingPolicy(decision=Decision.DENY))
    outcome = await WorkflowSaveTool().run(draft(), ctx)

    assert outcome.denied
    assert list_workflows() == []


async def test_a_privileged_workflow_demands_the_typed_challenge(
    registry: ToolRegistry,
) -> None:
    """The point of rolling the level up. A drafted workflow containing a
    privileged step is as serious as calling that step directly, and the gate
    has to treat it that way or the file becomes a way around the gate."""
    from altus.tools.approval import RecordingPolicy
    from altus.tools.workflow import WorkflowSaveTool

    policy = RecordingPolicy()
    ctx = make_ctx(registry, policy)
    await WorkflowSaveTool().run(
        draft(steps=[{"id": "shell", "kind": "tool", "tool": "k8s_exec"}]), ctx
    )

    (request,) = policy.seen
    assert request.sensitivity is Sensitivity.PRIVILEGED
    assert request.needs_challenge
    assert not request.may_grant_always


async def test_an_unrunnable_workflow_is_refused_rather_than_saved(
    registry: ToolRegistry,
) -> None:
    """A broken workflow sitting on disk looking finished is worse than one
    that was never written, and the model can fix this without the user."""
    from altus.tools.approval import RecordingPolicy
    from altus.tools.workflow import WorkflowSaveTool

    policy = RecordingPolicy()
    ctx = make_ctx(registry, policy)
    outcome = await WorkflowSaveTool().run(
        draft(steps=[{"id": "a", "kind": "tool", "tool": "no_such_tool"}]), ctx
    )

    assert outcome.is_error
    assert policy.seen == [], "and the user is not asked about it"
    assert list_workflows() == []


async def test_a_name_that_would_escape_the_directory_is_refused(
    registry: ToolRegistry,
) -> None:
    """Reachable by a prompt: the model chooses this string."""
    from altus.tools.approval import RecordingPolicy
    from altus.tools.workflow import WorkflowSaveTool

    policy = RecordingPolicy()
    outcome = await WorkflowSaveTool().run(
        draft(name="../../../.ssh/authorized_keys"), make_ctx(registry, policy)
    )
    assert outcome.is_error
    assert policy.seen == []


async def test_authoring_can_be_switched_off_entirely(registry: ToolRegistry) -> None:
    from altus.config.models import WorkflowSettings
    from altus.tools.approval import RecordingPolicy
    from altus.tools.workflow import WorkflowSaveTool

    policy = RecordingPolicy()
    ctx = make_ctx(registry, policy, WorkflowSettings(allow_model_authoring=False))
    outcome = await WorkflowSaveTool().run(draft(), ctx)

    assert outcome.is_error
    assert policy.seen == []


async def test_the_tool_takes_itself_back_out_once_a_workflow_is_saved(
    registry: ToolRegistry,
) -> None:
    """Otherwise one `/workflow new` widens the tool list for the rest of the
    session, which is the thing registering it late was meant to avoid."""
    from altus.tools.workflow import WorkflowSaveTool

    registry.add(WorkflowSaveTool())
    assert "workflow_save" in registry
    await WorkflowSaveTool().run(draft(), make_ctx(registry))
    assert "workflow_save" not in registry


async def test_workflow_new_registers_the_tool_and_hands_over_the_turn() -> None:
    from altus.tui.commands import dispatch

    app = make_app()
    async with app.run_test() as pilot:
        asked: list[str] = []

        async def fake_ask(text: str) -> bool:
            asked.append(text)
            return True

        app.ask_from_command = fake_ask  # type: ignore[method-assign]
        await dispatch(app, app.commands, "/workflow new deploy the API to staging")
        await pilot.pause()

        assert "workflow_save" in app.registry
        assert "deploy the API to staging" in asked[0]
        assert "workflow_save" in asked[0], "and it is told how to finish"


async def test_starting_a_new_session_drops_the_drafting_tool() -> None:
    from altus.tools.workflow import WorkflowSaveTool
    from altus.tui.screens.chat import ChatScreen

    app = make_app()
    async with app.run_test() as pilot:
        app.registry.add(WorkflowSaveTool())
        screen = app.screen
        assert isinstance(screen, ChatScreen)
        await screen.action_new_session()
        await pilot.pause()
        assert "workflow_save" not in app.registry
