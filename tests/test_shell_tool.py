"""Running a command.

Driven against real processes in tmp_path rather than mocks: the property under
test is what ``execve`` does with an argument containing ``;``, and a mock
would be asserting my belief about the operating system rather than the
operating system.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from altus.cloud.base import Sensitivity
from altus.config.models import ShellSettings, ToolSettings
from altus.tools.approval import Decision, RecordingPolicy
from altus.tools.base import ToolContext, sensitivity_of
from altus.tools.registry import default_registry
from altus.tools.shell import ShellTool
from altus.workspace import Workspace


def context(root: Path, allow: list[str], policy: RecordingPolicy | None = None) -> ToolContext:
    return ToolContext(
        workspace=Workspace(root=root),
        approvals=policy or RecordingPolicy(),
        shell_settings=ShellSettings(allow=allow),
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "sub").mkdir()
    return tmp_path


# ------------------------------------------------------------- the shell that isn't


async def test_a_semicolon_is_one_literal_argument(workspace: Path) -> None:
    """The whole security claim of this module, asserted rather than asserted about.

    If anything here ever reached `sh -c`, this test would delete the workspace.
    """
    victim = workspace / "keep-me"
    victim.write_text("still here", encoding="utf-8")

    outcome = await ShellTool().run(
        {"argv": ["echo", "hello; rm -rf ."]}, context(workspace, ["echo"])
    )

    assert not outcome.is_error
    assert outcome.content == "hello; rm -rf ."
    assert victim.read_text(encoding="utf-8") == "still here"


async def test_argv_must_be_a_list_of_strings(workspace: Path) -> None:
    outcome = await ShellTool().run({"argv": "echo hello"}, context(workspace, ["echo"]))
    assert outcome.is_error
    assert "no shell here" in outcome.content


# ----------------------------------------------------------------- the allowlist


async def test_a_binary_off_the_list_is_refused_without_asking(workspace: Path) -> None:
    """Refused, not gated. The prompt is for *what* make will do, not whether
    curl was meant."""
    policy = RecordingPolicy()
    outcome = await ShellTool().run(
        {"argv": ["curl", "https://example.invalid"]}, context(workspace, ["echo"], policy)
    )

    assert outcome.is_error
    assert "not in [tools.shell] allow" in outcome.content
    assert policy.seen == []


async def test_a_path_is_not_a_name(workspace: Path) -> None:
    """`./echo` is not `echo`, and an allowlist of names cannot vouch for it."""
    outcome = await ShellTool().run({"argv": ["./echo", "hi"]}, context(workspace, ["./echo"]))
    assert outcome.is_error
    assert "bare binary name" in outcome.summary or "bare binary name" in outcome.content


async def test_an_empty_allowlist_leaves_the_tool_unregistered() -> None:
    off = default_registry(kubernetes=False, aws=False, azure=False, gcp=False, mcp=False)
    assert "shell_run" not in off

    on = default_registry(
        kubernetes=False,
        aws=False,
        azure=False,
        gcp=False,
        mcp=False,
        shell=ShellSettings(allow=["make"]),
    )
    assert "shell_run" in on


async def test_a_read_only_registry_has_no_shell() -> None:
    registry = default_registry(
        writes=False,
        kubernetes=False,
        aws=False,
        azure=False,
        gcp=False,
        mcp=False,
        shell=ShellSettings(allow=["make"]),
    )
    assert "shell_run" not in registry


# ------------------------------------------------------------------ the sandbox


async def test_cwd_outside_the_workspace_is_an_error_not_a_prompt(workspace: Path) -> None:
    policy = RecordingPolicy()
    outcome = await ShellTool().run(
        {"argv": ["echo", "hi"], "cwd": "../.."}, context(workspace, ["echo"], policy)
    )
    assert outcome.is_error
    assert policy.seen == []


async def test_it_runs_where_it_was_told(workspace: Path) -> None:
    outcome = await ShellTool().run({"argv": ["pwd"], "cwd": "sub"}, context(workspace, ["pwd"]))
    assert not outcome.is_error
    assert outcome.content.endswith("sub")


# -------------------------------------------------------------------- the gate


async def test_it_always_asks_and_says_nothing_was_previewed(workspace: Path) -> None:
    policy = RecordingPolicy()
    await ShellTool().run(
        {"argv": ["echo", "hi"], "reason": "checking the tests still pass"},
        context(workspace, ["echo"], policy),
    )

    assert len(policy.seen) == 1
    request = policy.seen[0]
    assert request.tool == "shell_run"
    assert "echo hi" in request.path
    assert "checking the tests still pass" in request.dry_run
    assert "Not a dry run" in request.dry_run
    assert request.sensitivity is Sensitivity.MUTATE


async def test_a_refusal_runs_nothing(workspace: Path) -> None:
    made = workspace / "made-by-touch"
    policy = RecordingPolicy(decision=Decision.DENY)
    outcome = await ShellTool().run(
        {"argv": ["touch", made.name]}, context(workspace, ["touch"], policy)
    )
    assert outcome.denied
    assert not made.exists()


async def test_a_failing_command_is_an_error_carrying_its_output(workspace: Path) -> None:
    outcome = await ShellTool().run({"argv": ["ls", "no-such-thing"]}, context(workspace, ["ls"]))
    assert outcome.is_error
    assert "exit" in outcome.summary


async def test_a_command_that_hangs_is_stopped(workspace: Path) -> None:
    outcome = await ShellTool().run(
        {"argv": ["sleep", "30"], "timeout": 1}, context(workspace, ["sleep"])
    )
    assert outcome.is_error
    assert "still running" in outcome.content


async def test_the_allowlist_is_checked_again_at_call_time(workspace: Path) -> None:
    """Registration is not a safety mechanism. `cli.py` shipped an allowlist
    check that read an attribute which never existed, and for a while the list
    was enforced only by which tools got registered."""
    ctx = ToolContext(workspace=Workspace(root=workspace), approvals=RecordingPolicy())
    outcome = await ShellTool().run({"argv": ["echo", "hi"]}, ctx)
    assert outcome.is_error
    assert "(nothing)" in outcome.content


# ---------------------------------------------------------------- classification


def test_it_is_a_mutation_whose_arguments_decide_the_rest() -> None:
    from altus.tools.base import dispatches

    assert sensitivity_of(ShellTool()) is Sensitivity.MUTATE
    assert dispatches(ShellTool())


def test_settings_default_to_no_shell_at_all() -> None:
    assert ToolSettings().shell.allow == []


def test_a_workflow_with_a_shell_step_reports_a_floor() -> None:
    """`(at least)`, always: what `make` does is decided by the Makefile."""
    from altus.workflow.blast import blast_radius
    from altus.workflow.models import ToolStep, Workflow

    registry = default_registry(
        kubernetes=False,
        aws=False,
        azure=False,
        gcp=False,
        mcp=False,
        shell=ShellSettings(allow=["make"]),
    )
    workflow = Workflow(
        name="build",
        steps=[ToolStep(id="build", tool="shell_run", args={"argv": ["make", "test"]})],
    )
    blast = blast_radius(workflow, registry)

    assert blast.level is Sensitivity.MUTATE
    assert blast.unresolved == ("build",)
    assert blast.render().endswith("(at least)")
