"""The workflow artifact: what it is, where it lives, and how dangerous it is.

Defining one, not running one --- ``tests/test_workflow_engine.py`` covers
that. What these assert is that the designer can never overstate what it
knows: every level it prints is either certain or carries the reason it is
only a floor.
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


# ------------------------------------------------------------- the text surface


async def dispatch_command(app, text: str):  # type: ignore[no-untyped-def]
    from altus.tui.commands import dispatch

    return await dispatch(app, app.commands, text)


async def seed(app, *workflows: Workflow) -> None:  # type: ignore[no-untyped-def]
    for workflow in workflows:
        save(workflow, app.config.workflow)


async def test_list_says_where_to_start_when_there_are_none() -> None:
    app = make_app()
    async with app.run_test():
        result = await dispatch_command(app, "/workflow list")
        assert "No workflows yet" in result.body
        assert "/workflow new" in result.body


async def test_list_flags_a_workflow_that_cannot_run(registry: ToolRegistry) -> None:
    """Otherwise the list quietly disagrees with /workflow validate: a broken
    workflow of reads shows a calm "read" and nothing else."""
    app = make_app()
    async with app.run_test():
        app.registry = registry  # type: ignore[assignment]
        await seed(
            app,
            Workflow(name="fine", steps=[ToolStep(id="a", tool="k8s_get")]),
            Workflow(name="broken", steps=[ToolStep(id="a", tool="gone")]),
        )
        body = (await dispatch_command(app, "/workflow list")).body
        assert "broken" in body and "will not run" in body
        assert "fine" in body
        assert body.count("will not run") == 1


async def test_show_prints_every_step_with_its_own_level(registry: ToolRegistry) -> None:
    app = make_app()
    async with app.run_test():
        app.registry = registry  # type: ignore[assignment]
        await seed(
            app,
            Workflow(
                name="mixed",
                steps=[
                    ToolStep(id="look", tool="k8s_topology"),
                    ToolStep(id="change", needs=["look"], tool="k8s_apply"),
                ],
            ),
        )
        body = (await dispatch_command(app, "/workflow show mixed")).body
        assert "look" in body and "read" in body
        assert "change" in body and "mutate" in body
        assert "after look" in body


async def test_validate_reports_a_runnable_workflow_as_runnable(
    registry: ToolRegistry,
) -> None:
    app = make_app()
    async with app.run_test():
        app.registry = registry  # type: ignore[assignment]
        await seed(app, Workflow(name="ok", steps=[ToolStep(id="a", tool="k8s_get")]))
        result = await dispatch_command(app, "/workflow validate ok")
        assert result.severity == "information"
        assert "runnable" in result.body


async def test_validate_distinguishes_a_warning_from_a_blocker(
    registry: ToolRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app()
    async with app.run_test():
        app.registry = registry  # type: ignore[assignment]
        await seed(app, Workflow(name="broken", steps=[ToolStep(id="a", tool="gone")]))
        result = await dispatch_command(app, "/workflow validate broken")
        assert result.severity == "error"
        assert "cannot run as written" in result.body


async def test_run_opens_the_run_screen(registry: ToolRegistry) -> None:
    from altus.tui.screens.run import RunScreen

    app = make_app()
    async with app.run_test() as pilot:
        app.registry = registry  # type: ignore[assignment]
        await seed(app, Workflow(name="ok", steps=[ToolStep(id="a", tool="k8s_get")]))
        await dispatch_command(app, "/workflow run ok")
        await pilot.pause()

        assert any(isinstance(screen, RunScreen) for screen in app.screen_stack)
        # And it is already asking, before a single step has run.
        assert type(app.screen).__name__ == "ApprovalModal"


async def test_running_one_that_does_not_exist_says_so() -> None:
    app = make_app()
    async with app.run_test():
        result = await dispatch_command(app, "/workflow run nope")
        assert result.severity == "error"


async def test_path_names_the_file_without_needing_it_to_exist() -> None:
    app = make_app()
    async with app.run_test():
        body = (await dispatch_command(app, "/workflow path never-written")).body
        assert body.endswith("never-written.toml")


async def test_path_still_refuses_a_traversing_name() -> None:
    app = make_app()
    async with app.run_test():
        result = await dispatch_command(app, "/workflow path ../../etc/passwd")
        assert result.severity == "error"


async def test_opening_one_that_does_not_exist_says_so() -> None:
    app = make_app()
    async with app.run_test():
        result = await dispatch_command(app, "/workflow show nope")
        assert result.severity == "error"
        assert "nope" in result.body


async def test_workflows_can_be_switched_off_entirely() -> None:
    app = make_app()
    async with app.run_test():
        app.config.workflow.enabled = False
        result = await dispatch_command(app, "/workflow list")
        assert result.severity == "warning"


# ------------------------------------------------------------- the templates


def real_registry() -> ToolRegistry:
    """Everything a normal session would have, so a template is checked against
    the tool names it will actually meet."""
    return default_registry()


@pytest.mark.parametrize("name", sorted(__import__("altus.workflow", fromlist=["x"]).templates()))
def test_every_shipped_template_runs_as_written(name: str) -> None:
    """A template that does not validate is worse than no template: it teaches
    the format wrong and fails at the moment somebody trusted it."""
    from altus.workflow import blast_radius, check, fatal, template

    workflow = template(name)
    registry = real_registry()
    problems = check(workflow, registry)
    assert not fatal(problems), [p.render() for p in problems]
    assert workflow.steps
    assert workflow.description, "a template that cannot say what it does is not one"
    blast_radius(workflow, registry)


@pytest.mark.parametrize("name", sorted(__import__("altus.workflow", fromlist=["x"]).templates()))
def test_every_template_keeps_its_prose_when_copied(name: str, tmp_path) -> None:
    """Copied as text, not parsed and re-rendered. The comments explaining why
    each step is there are the part a reader needs most, and a round trip
    through the model would drop every one of them."""
    from altus.workflow import copy_template, path_for, template_text

    copy_template(name, "mine")
    written = path_for("mine").read_text(encoding="utf-8")
    assert written.count("#") == template_text(name).count("#")
    assert 'name = "mine"' in written
    path_for("mine").unlink()


def test_copying_over_an_existing_workflow_is_refused(tmp_path) -> None:
    from altus.workflow import copy_template

    copy_template("vuln-fix", "mine")
    with pytest.raises(ConfigError, match="already exists"):
        copy_template("jira-bug", "mine")


def test_an_unknown_template_lists_the_real_ones() -> None:
    from altus.workflow import template

    with pytest.raises(ConfigError, match="vuln-fix"):
        template("no-such-template")


def test_a_template_name_that_would_escape_the_directory_is_refused() -> None:
    from altus.workflow import template_text

    with pytest.raises(ConfigError):
        template_text("../../../etc/passwd")


def test_the_vuln_fix_template_asks_two_sources_by_role() -> None:
    """Falcon's data is host- and image-centric and contains no repository
    identifier, so a workflow claiming to find "the repo's vulnerabilities in
    CrowdStrike" would be inventing a join. This one asks both and says which
    it is asking."""
    from altus.workflow import template

    workflow = template("vuln-fix")
    servers = {
        step.args.get("server")
        for step in workflow.steps
        if isinstance(step, ToolStep) and step.tool in {"mcp_call", "mcp_do"}
    }
    assert {"github", "crowdstrike"} <= servers
    runtime = workflow.step("runtime")
    assert runtime is not None
    assert runtime.on_error == "continue", "a repo with no deployed image still gets reviewed"


def test_an_agent_step_can_say_it_needs_no_tools_at_all() -> None:
    """Three states, and the difference between the last two is the whole
    reason `tools` is not a plain list: omitted means every tool and is
    therefore as dangerous as the worst one, while empty means none and is a
    read however long the prompt is."""
    from altus.workflow import blast_radius

    registry = real_registry()
    everything = Workflow(name="a", steps=[AgentStep(id="s", prompt="go")])
    nothing = Workflow(name="b", steps=[AgentStep(id="s", prompt="go", tools=[])])

    assert blast_radius(everything, registry).level is Sensitivity.PRIVILEGED
    assert blast_radius(nothing, registry).level is Sensitivity.READ
    assert blast_radius(nothing, registry).certain


def test_the_step_form_can_express_all_three_tool_states() -> None:
    from altus.tui.widgets.step_form import _parse_tools, _tools_text

    assert _parse_tools("") is None
    assert _parse_tools("none") == []
    assert _parse_tools("read_file, grep") == ["read_file", "grep"]
    assert _tools_text(AgentStep(id="a", prompt="p")) == ""
    assert _tools_text(AgentStep(id="a", prompt="p", tools=[])) == "none"
