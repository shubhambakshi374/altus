"""``altus workflow`` --- the headless surface.

Exit codes are the interface here, because the caller is usually a script and
the distinction this increment adds is "somebody has to look at this" (3) versus
"this is broken" (1). That distinction is only real if it is tested.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from altus.cli_workflow import app
from altus.config.loader import config_dir

runner = CliRunner()

WORKFLOW = """
name = "demo"

[[steps]]
id = "look"
kind = "tool"
tool = "list_dir"
args = { path = "." }

[[steps]]
id = "write"
kind = "tool"
needs = ["look"]
tool = "write_file"
args = { path = "out.txt", content = "${look}" }
"""


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workflow on disk and a working directory to run it in."""
    where = config_dir() / "workflows"
    where.mkdir(parents=True, exist_ok=True)
    (where / "demo.toml").write_text(WORKFLOW, encoding="utf-8")
    root = tmp_path / "work"
    root.mkdir()
    (root / "file.txt").write_text("hello", encoding="utf-8")
    monkeypatch.chdir(root)
    return root


def test_list_says_what_each_workflow_could_touch(workspace: Path) -> None:
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "demo" in result.stdout
    assert "blast radius: mutate" in result.stdout


def test_an_unattended_run_parks_and_exits_three(workspace: Path) -> None:
    result = runner.invoke(app, ["run", "demo", "--unattended"])

    assert result.exit_code == 3
    assert "needs approval to create out.txt" in result.stdout
    assert not (workspace / "out.txt").exists()


def test_a_parked_run_is_listed_then_resumed(workspace: Path) -> None:
    assert runner.invoke(app, ["run", "demo", "--unattended"]).exit_code == 3

    listed = runner.invoke(app, ["runs", "--parked"])
    assert listed.exit_code == 0
    run_id = listed.stdout.split()[0]

    resumed = runner.invoke(app, ["resume", run_id, "--yes"])
    assert resumed.exit_code == 0
    assert "completed" in resumed.stdout
    # The first leg's output was replayed rather than recomputed.
    assert "file.txt" in (workspace / "out.txt").read_text(encoding="utf-8")


def test_a_run_that_completes_exits_zero(workspace: Path) -> None:
    result = runner.invoke(app, ["run", "demo", "--yes"])
    assert result.exit_code == 0
    assert (workspace / "out.txt").exists()


def test_a_failing_step_exits_one(workspace: Path) -> None:
    (config_dir() / "workflows" / "broken.toml").write_text(
        'name = "broken"\n\n[[steps]]\nid = "nope"\nkind = "tool"\n'
        'tool = "read_file"\nargs = { path = "missing.md" }\n',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["run", "broken", "--yes"])
    assert result.exit_code == 1


def test_contradictory_flags_are_refused(workspace: Path) -> None:
    result = runner.invoke(app, ["run", "demo", "--unattended", "--yes"])
    assert result.exit_code == 1
    assert "contradict" in result.stderr


def test_an_unknown_workflow_says_so(workspace: Path) -> None:
    result = runner.invoke(app, ["run", "ghost"])
    assert result.exit_code == 1
    assert "no workflow called" in result.stderr


def test_an_unrunnable_workflow_fails_validation(workspace: Path) -> None:
    (config_dir() / "workflows" / "bad.toml").write_text(
        'name = "bad"\n\n[[steps]]\nid = "a"\nkind = "tool"\ntool = "no_such_tool"\n',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["validate", "bad"])
    assert result.exit_code == 2
    assert "no tool called" in result.stdout


def test_inputs_are_given_as_pairs(workspace: Path) -> None:
    (config_dir() / "workflows" / "greet.toml").write_text(
        'name = "greet"\n\n[inputs.who]\ndescription = "a name"\nrequired = true\n\n'
        '[[steps]]\nid = "write"\nkind = "tool"\ntool = "write_file"\n'
        'args = { path = "hi.txt", content = "${inputs.who}" }\n',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["run", "greet", "who=acme/api", "--yes"])

    assert result.exit_code == 0
    assert (workspace / "hi.txt").read_text(encoding="utf-8") == "acme/api"


def test_a_missing_required_input_refuses_before_anything_runs(workspace: Path) -> None:
    (config_dir() / "workflows" / "greet.toml").write_text(
        'name = "greet"\n\n[inputs.who]\ndescription = "a name"\nrequired = true\n\n'
        '[[steps]]\nid = "write"\nkind = "tool"\ntool = "write_file"\n'
        'args = { path = "hi.txt", content = "${inputs.who}" }\n',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["run", "greet", "--yes"])

    assert result.exit_code == 1
    assert "needs a value for: who" in result.stderr
    assert not (workspace / "hi.txt").exists()


def test_serve_with_nothing_to_watch_says_so(workspace: Path) -> None:
    result = runner.invoke(app, ["serve", "--once"])
    assert result.exit_code == 0
    assert "no workflow declares a trigger" in result.stdout


def test_serve_lists_what_it_is_watching(workspace: Path) -> None:
    text = WORKFLOW.replace(
        'name = "demo"', 'name = "demo"\n\n[[triggers]]\nkind = "schedule"\nevery = "1h"'
    )
    (config_dir() / "workflows" / "demo.toml").write_text(text, encoding="utf-8")

    result = runner.invoke(app, ["serve", "--once"])
    assert result.exit_code == 0
    assert "demo #1: every 1h" in result.stdout
    # First sight is not an event, so nothing ran.
    assert "running demo" not in result.stdout
